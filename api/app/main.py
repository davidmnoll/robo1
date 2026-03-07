import asyncio
import contextlib
import base64
import binascii
import gzip
import json
import logging
import secrets
from collections import defaultdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Optional, Tuple, TypeVar

import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from jose import JWTError, jwt
with contextlib.suppress(ImportError):
    import bcrypt as _bcrypt

    if _bcrypt and not hasattr(_bcrypt, "__about__"):
        # bcrypt 4.2+ removed the __about__ module attribute, but passlib<1.8 still
        # expects it when selecting a backend. Provide a shim exposing __version__.
        class _About:  # minimalist shim for Passlib's version probe
            def __init__(self, version: str):
                self.__version__ = version

        version = getattr(_bcrypt, "__version__", "0")
        _bcrypt.__about__ = _About(version)
    if _bcrypt and hasattr(_bcrypt, "hashpw"):
        _orig_hashpw = _bcrypt.hashpw

        def _hashpw_with_trunc(secret: bytes, config: bytes) -> bytes:
            try:
                return _orig_hashpw(secret, config)
            except ValueError as exc:
                if "longer than 72 bytes" not in str(exc):
                    raise
                return _orig_hashpw(secret[:72], config)

        _bcrypt.hashpw = _hashpw_with_trunc
from passlib.context import CryptContext
from pydantic import BaseModel, Field, ValidationError, constr
from pydantic_settings import BaseSettings
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, delete, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship, selectinload


IdentifierStr = constr(min_length=3)


class SeedUserConfig(BaseModel):
    email: IdentifierStr
    password: constr(min_length=1, max_length=32)


class SeedLobbyConfig(BaseModel):
    name: str
    description: Optional[str] = None
    access_key: Optional[str] = None
    owner_email: IdentifierStr
    is_public: bool = False


class SeedBotConfig(BaseModel):
    name: str
    ros_namespace: str
    lobby_name: str
    owner_email: IdentifierStr
    description: Optional[str] = None


class Settings(BaseSettings):
    ros_push_key: str = Field("local-dev-key", alias="ROS_PUSH_KEY")
    gateway_name: str = Field("gateway-1", alias="GATEWAY_NAME")
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["*"], alias="CORS_ALLOW_ORIGINS")
    database_url: str = Field("postgresql+asyncpg://robot:robot@localhost:5432/robotarena", alias="DATABASE_URL")
    secret_key: str = Field("super-secret-key", alias="SECRET_KEY")
    access_token_expire_minutes: int = Field(60, alias="ACCESS_TOKEN_EXPIRE_MINUTES")
    seed_users_json: Optional[str] = Field(None, alias="SEED_USERS_JSON")
    seed_lobbies_json: Optional[str] = Field(None, alias="SEED_LOBBIES_JSON")
    seed_bots_json: Optional[str] = Field(None, alias="SEED_BOTS_JSON")
    heartbeat_timeout_seconds: int = Field(30, alias="HEARTBEAT_TIMEOUT_SECONDS")
    command_retention_seconds: int = Field(120, alias="COMMAND_RETENTION_SECONDS")


settings = Settings()
SeedModelT = TypeVar("SeedModelT", bound=BaseModel)


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.database_url, echo=False, future=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    lobbies = relationship("Lobby", back_populates="owner", cascade="all, delete-orphan")


class Lobby(Base):
    __tablename__ = "lobbies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    ros_host = Column(String(255), nullable=False)
    ros_port = Column(Integer, nullable=False)
    description = Column(Text, nullable=True)
    access_key = Column(String(255), nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_public = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    is_deleted = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    owner = relationship("User", back_populates="lobbies")
    bots = relationship("Bot", back_populates="lobby", cascade="all, delete-orphan")


class Bot(Base):
    __tablename__ = "bots"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    ros_namespace = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    lobby_id = Column(Integer, ForeignKey("lobbies.id"), nullable=False)
    is_deleted = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    lobby = relationship("Lobby", back_populates="bots")


class RobotCommand(Base):
    __tablename__ = "robot_commands"

    id = Column(Integer, primary_key=True, index=True)
    robot_namespace = Column(String(255), nullable=False, index=True)
    linear_x = Column(Float, nullable=False, default=0.0)
    linear_y = Column(Float, nullable=False, default=0.0)
    linear_z = Column(Float, nullable=False, default=0.0)
    angular_x = Column(Float, nullable=False, default=0.0)
    angular_y = Column(Float, nullable=False, default=0.0)
    angular_z = Column(Float, nullable=False, default=0.0)
    status = Column(String(32), nullable=False, default="pending")
    requested_by = Column(String(255), nullable=True)
    message = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    claimed_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


logger = logging.getLogger("gateway")
logging.basicConfig(level=logging.INFO)
frame_queues: Dict[str, asyncio.Queue[Tuple[int, int, bytes]]] = {}
peer_connections: set[RTCPeerConnection] = set()
active_robot_streams: Dict[str, set[str]] = defaultdict(set)
robot_heartbeats: Dict[str, datetime] = {}
command_subscribers: Dict[str, set[WebSocket]] = defaultdict(set)
websocket_robot_map: Dict[int, set[str]] = {}
command_ws_lock = asyncio.Lock()

app = FastAPI(title="Robot Gateway API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


class TwistCommand(BaseModel):
    linear_x: float = 0.0
    linear_y: float = 0.0
    linear_z: float = 0.0
    angular_x: float = 0.0
    angular_y: float = 0.0
    angular_z: float = 0.0


class WebRTCOffer(BaseModel):
    sdp: str
    type: str


class FramePayload(BaseModel):
    width: int
    height: int
    encoding: str
    data: str
    stamp_sec: int | None = None
    stamp_nanosec: int | None = None
    compressed: bool = False


class UserOut(BaseModel):
    id: int
    email: IdentifierStr


class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    user: UserOut


class RegisterRequest(BaseModel):
    email: IdentifierStr
    password: constr(min_length=6, max_length=32)


class LoginRequest(BaseModel):
    email: IdentifierStr
    password: constr(min_length=1, max_length=32)


class LobbyOnlineRequest(BaseModel):
    access_key: constr(min_length=1, max_length=255)


class LobbyCreate(BaseModel):
    name: constr(min_length=1, strip_whitespace=True)
    description: Optional[str] = None
    is_public: bool = False


class LobbyUpdate(BaseModel):
    name: Optional[constr(min_length=1, strip_whitespace=True)] = None
    description: Optional[str] = None
    is_public: Optional[bool] = None


class BotCreate(BaseModel):
    lobby_id: int
    name: constr(min_length=1, strip_whitespace=True)
    ros_namespace: constr(min_length=1, strip_whitespace=True)
    description: Optional[str] = None


class BotUpdate(BaseModel):
    name: Optional[constr(min_length=1, strip_whitespace=True)] = None
    ros_namespace: Optional[constr(min_length=1, strip_whitespace=True)] = None
    description: Optional[str] = None


class LobbyOut(BaseModel):
    id: int
    name: str
    description: Optional[str]
    access_key: Optional[str]
    owner_email: IdentifierStr
    created_at: datetime
    is_public: bool
    is_deleted: bool
    is_owner: bool
    bot_count: int


class BotOut(BaseModel):
    id: int
    name: str
    ros_namespace: str
    description: Optional[str]
    lobby_id: int
    lobby_name: str
    owner_email: IdentifierStr
    created_at: datetime
    is_deleted: bool
    active_streamers: list[str]


class LobbyDetailOut(LobbyOut):
    bots: list[BotOut]


class RobotCommandOut(BaseModel):
    id: int
    robot_namespace: str
    linear_x: float
    linear_y: float
    linear_z: float
    angular_x: float
    angular_y: float
    angular_z: float
    status: str
    requested_by: Optional[str]
    message: Optional[str]
    created_at: datetime
    claimed_at: Optional[datetime]
    completed_at: Optional[datetime]


class RobotCommandDelivery(BaseModel):
    command: Optional[RobotCommandOut]


class RobotCommandComplete(BaseModel):
    status: Optional[str] = None
    message: Optional[str] = None


async def get_current_user(
    authorization: str = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    user = await session.get(User, int(user_id))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


@app.post("/api/auth/register", response_model=TokenResponse)
async def register_user(payload: RegisterRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    email = payload.email.lower()
    existing = await session.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")
    user = User(email=email, password_hash=hash_password(payload.password))
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return create_token_response(user)


@app.post("/api/auth/login", response_model=TokenResponse)
async def login_user(payload: LoginRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    email = payload.email.lower()
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return create_token_response(user)


@app.post("/api/auth/logout")
async def logout_user() -> dict[str, str]:
    # Stateless JWT logout; clients discard the token.
    return {"status": "ok"}


@app.get("/api/lobbies")
async def list_lobbies(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = (
        select(Lobby)
            .options(selectinload(Lobby.owner), selectinload(Lobby.bots))
            .where(Lobby.is_deleted.is_(False))
            .where(or_(Lobby.is_public.is_(True), Lobby.owner_id == current_user.id))
            .order_by(Lobby.created_at.desc())
    )
    result = await session.execute(stmt)
    lobbies = result.scalars().all()
    return {"items": [lobby_to_out(lobby, current_user) for lobby in lobbies]}


@app.post("/api/lobbies", response_model=LobbyOut)
async def create_lobby(
    payload: LobbyCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> LobbyOut:
    access_key = secrets.token_urlsafe(16)
    lobby = Lobby(
        name=payload.name.strip(),
        ros_host="internal",
        ros_port=0,
        description=payload.description,
        access_key=access_key,
        owner_id=current_user.id,
        is_public=payload.is_public,
    )
    session.add(lobby)
    await session.commit()
    await session.refresh(lobby)
    lobby.owner = current_user
    # Ensure relationships are hydrated before serialization to avoid lazy-load errors
    await session.refresh(lobby, attribute_names=["owner", "bots"])
    return lobby_to_out(lobby, current_user)


@app.get("/api/lobbies/{lobby_id}", response_model=LobbyDetailOut)
async def get_lobby_detail(
    lobby_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> LobbyDetailOut:
    stmt = (
        select(Lobby)
        .options(selectinload(Lobby.owner), selectinload(Lobby.bots).selectinload(Bot.lobby))
        .where(Lobby.id == lobby_id)
    )
    result = await session.execute(stmt)
    lobby = result.scalar_one_or_none()
    if not lobby or lobby.is_deleted:
        raise HTTPException(status_code=404, detail="Lobby not found")
    if not lobby.is_public and lobby.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Lobby is private")
    return lobby_detail_to_out(lobby, current_user)


@app.patch("/api/lobbies/{lobby_id}", response_model=LobbyOut)
async def update_lobby(
    lobby_id: int,
    payload: LobbyUpdate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> LobbyOut:
    lobby = await session.get(Lobby, lobby_id)
    if not lobby or lobby.is_deleted:
        raise HTTPException(status_code=404, detail="Lobby not found")
    if lobby.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the lobby owner can update")
    if payload.name is not None:
        lobby.name = payload.name.strip()
    if payload.description is not None:
        lobby.description = payload.description
    if payload.is_public is not None:
        lobby.is_public = payload.is_public
    await session.commit()
    await session.refresh(lobby)
    await session.refresh(lobby, attribute_names=["owner", "bots"])
    return lobby_to_out(lobby, current_user)


@app.delete("/api/lobbies/{lobby_id}")
async def delete_lobby(
    lobby_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    lobby = await session.get(Lobby, lobby_id)
    if not lobby or lobby.is_deleted:
        raise HTTPException(status_code=404, detail="Lobby not found")
    if lobby.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the lobby owner can delete")
    lobby.is_deleted = True
    await session.refresh(lobby, attribute_names=["bots"])
    for bot in lobby.bots:
        bot.is_deleted = True
    await session.commit()
    return {"status": "deleted"}


@app.get("/api/bots")
async def list_bots(
    lobby_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    query = (
        select(Bot)
        .options(selectinload(Bot.lobby).selectinload(Lobby.owner))
        .order_by(Bot.created_at.desc())
        .where(Bot.is_deleted.is_(False))
        .where(Bot.lobby.has(Lobby.is_deleted.is_(False)))
    )
    if lobby_id is not None:
        query = query.where(Bot.lobby_id == lobby_id)
    result = await session.execute(query)
    bots = result.scalars().all()
    return {"items": [bot_to_out(bot) for bot in bots]}


@app.post("/api/bots", response_model=BotOut)
async def create_bot(
    payload: BotCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BotOut:
    lobby_result = await session.execute(
        select(Lobby).options(selectinload(Lobby.owner)).where(Lobby.id == payload.lobby_id)
    )
    lobby = lobby_result.scalar_one_or_none()
    if not lobby:
        raise HTTPException(status_code=404, detail="Lobby not found")
    if lobby.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the lobby owner can register bots")
    normalized_namespace = payload.ros_namespace.strip()
    existing = await session.execute(select(Bot).where(Bot.ros_namespace == normalized_namespace))
    bot = existing.scalar_one_or_none()
    if bot and not bot.is_deleted:
        raise HTTPException(status_code=400, detail="ROS namespace already registered")
    if bot and bot.is_deleted:
        bot.name = payload.name.strip()
        bot.description = payload.description
        bot.lobby_id = lobby.id
        bot.is_deleted = False
    else:
        bot = Bot(
            name=payload.name.strip(),
            ros_namespace=normalized_namespace,
            description=payload.description,
            lobby_id=lobby.id,
        )
        session.add(bot)
    await session.commit()
    await session.refresh(bot)
    bot.lobby = lobby
    return bot_to_out(bot)


@app.patch("/api/bots/{bot_id}", response_model=BotOut)
async def update_bot(
    bot_id: int,
    payload: BotUpdate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BotOut:
    bot = await session.get(Bot, bot_id)
    if not bot or bot.is_deleted:
        raise HTTPException(status_code=404, detail="Bot not found")
    lobby = await session.get(Lobby, bot.lobby_id)
    if not lobby or lobby.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the lobby owner can update bots")
    if payload.name is not None:
        bot.name = payload.name.strip()
    if payload.description is not None:
        bot.description = payload.description
    if payload.ros_namespace is not None:
        new_ns = payload.ros_namespace.strip()
        if new_ns != bot.ros_namespace:
            existing = await session.execute(select(Bot).where(Bot.ros_namespace == new_ns))
            ns_bot = existing.scalar_one_or_none()
            if ns_bot and ns_bot.id != bot.id:
                raise HTTPException(status_code=400, detail="ROS namespace already registered")
            bot.ros_namespace = new_ns
    await session.commit()
    await session.refresh(bot)
    bot.lobby = lobby
    return bot_to_out(bot)


@app.delete("/api/bots/{bot_id}")
async def delete_bot(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    bot = await session.get(Bot, bot_id)
    if not bot or bot.is_deleted:
        raise HTTPException(status_code=404, detail="Bot not found")
    lobby = await session.get(Lobby, bot.lobby_id)
    if not lobby or lobby.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the lobby owner can delete bots")
    bot.is_deleted = True
    await session.commit()
    return {"status": "deleted"}


@app.post("/api/internal/lobbies/{lobby_name}/online")
async def register_lobby_online(
    lobby_name: str,
    payload: LobbyOnlineRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(Lobby).options(selectinload(Lobby.bots)).where(Lobby.name == lobby_name)
    result = await session.execute(stmt)
    lobby = result.scalar_one_or_none()
    if lobby is None or lobby.is_deleted:
        raise HTTPException(status_code=404, detail="Lobby not found")
    if lobby.access_key != payload.access_key:
        raise HTTPException(status_code=403, detail="Invalid lobby key")
    for bot in lobby.bots:
        update_robot_heartbeat(bot.ros_namespace)
    return {"status": "acknowledged", "lobby": lobby_name}


def user_to_out(user: User) -> UserOut:
    return UserOut(id=user.id, email=user.email)


def create_token_response(user: User) -> TokenResponse:
    token = create_access_token({"sub": str(user.id)})
    return TokenResponse(access_token=token, token_type="bearer", user=user_to_out(user))


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (
        expires_delta
        if expires_delta
        else timedelta(minutes=settings.access_token_expire_minutes)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.secret_key, algorithm=ALGORITHM)


def lobby_to_out(lobby: Lobby, current_user: User) -> LobbyOut:
    owner_email = lobby.owner.email if lobby.owner else ""
    key = lobby.access_key if lobby.owner_id == current_user.id else None
    bots = getattr(lobby, "bots", []) or []
    bot_count = sum(1 for bot in bots if not getattr(bot, "is_deleted", False))
    is_owner = lobby.owner_id == current_user.id
    return LobbyOut(
        id=lobby.id,
        name=lobby.name,
        description=lobby.description,
        access_key=key,
        owner_email=owner_email,
        created_at=lobby.created_at,
        is_public=bool(lobby.is_public),
        is_deleted=bool(lobby.is_deleted),
        is_owner=is_owner,
        bot_count=bot_count,
    )


def lobby_detail_to_out(lobby: Lobby, current_user: User) -> LobbyDetailOut:
    base = lobby_to_out(lobby, current_user)
    bots = [bot_to_out(bot) for bot in getattr(lobby, "bots", []) if not bot.is_deleted]
    data = base.model_dump()
    data["bots"] = bots
    return LobbyDetailOut(**data)


def bot_to_out(bot: Bot) -> BotOut:
    lobby = bot.lobby
    owner_email = lobby.owner.email if lobby and lobby.owner else ""
    lobby_name = lobby.name if lobby else ""
    active = sorted(active_robot_streams.get(bot.ros_namespace, set()))
    return BotOut(
        id=bot.id,
        name=bot.name,
        ros_namespace=bot.ros_namespace,
        description=bot.description,
        lobby_id=bot.lobby_id,
        lobby_name=lobby_name,
        owner_email=owner_email,
        created_at=bot.created_at,
        is_deleted=bool(bot.is_deleted),
        active_streamers=active,
    )


def robot_command_to_out(command: RobotCommand) -> RobotCommandOut:
    return RobotCommandOut(
        id=command.id,
        robot_namespace=command.robot_namespace,
        linear_x=command.linear_x,
        linear_y=command.linear_y,
        linear_z=command.linear_z,
        angular_x=command.angular_x,
        angular_y=command.angular_y,
        angular_z=command.angular_z,
        status=command.status,
        requested_by=command.requested_by,
        message=command.message,
        created_at=command.created_at,
        claimed_at=command.claimed_at,
        completed_at=command.completed_at,
    )


async def cleanup_completed_commands(session: AsyncSession, robot_id: str) -> None:
    cutoff = datetime.utcnow() - timedelta(seconds=settings.command_retention_seconds)
    await session.execute(
        delete(RobotCommand)
        .where(RobotCommand.robot_namespace == robot_id)
        .where(RobotCommand.status == "completed")
        .where(RobotCommand.completed_at.isnot(None))
        .where(RobotCommand.completed_at < cutoff)
    )


async def register_robot_ws(websocket: WebSocket, robots: list[str]) -> None:
    if not robots:
        return
    async with command_ws_lock:
        ws_id = id(websocket)
        entry = websocket_robot_map.setdefault(ws_id, set())
        for robot in robots:
            command_subscribers[robot].add(websocket)
            entry.add(robot)


async def unregister_robot_ws(websocket: WebSocket) -> None:
    ws_id = id(websocket)
    async with command_ws_lock:
        subscribed = websocket_robot_map.pop(ws_id, set())
        for robot in subscribed:
            sockets = command_subscribers.get(robot)
            if not sockets:
                continue
            sockets.discard(websocket)
            if not sockets:
                command_subscribers.pop(robot, None)


async def broadcast_robot_command(command: RobotCommand) -> None:
    payload = {
        "type": "command",
        "robot": command.robot_namespace,
        "command": robot_command_to_out(command).model_dump(mode="json"),
    }
    async with command_ws_lock:
        sockets = list(command_subscribers.get(command.robot_namespace, set()))
    for websocket in sockets:
        try:
            await websocket.send_json(payload)
        except RuntimeError:
            # websocket likely closed; cleanup asynchronously
            await unregister_robot_ws(websocket)
        except Exception:
            await unregister_robot_ws(websocket)


async def send_pending_commands_to_connection(websocket: WebSocket, robots: list[str]) -> None:
    if not robots:
        return
    async with AsyncSessionLocal() as session:
        for robot in robots:
            result = await session.execute(
                select(RobotCommand)
                .where(RobotCommand.robot_namespace == robot)
                .where(RobotCommand.status == "pending")
                .order_by(RobotCommand.created_at.asc())
            )
            for command in result.scalars():
                await websocket.send_json(
                    {
                        "type": "command",
                        "robot": robot,
                        "command": robot_command_to_out(command).model_dump(mode="json"),
                    }
                )


def parse_seed_entries(raw: Optional[str], model: type[SeedModelT], label: str) -> list[SeedModelT]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse %s: %s", label, exc)
        return []
    if not isinstance(payload, list):
        logger.error("%s must be a JSON list", label)
        return []
    entries: list[SeedModelT] = []
    for idx, item in enumerate(payload):
        try:
            entries.append(model.model_validate(item))
        except ValidationError as exc:
            logger.error("Invalid %s entry #%s: %s", label, idx, exc)
    return entries


def require_internal_api_key(provided: str) -> None:
    if settings.ros_push_key and provided != settings.ros_push_key:
        raise HTTPException(status_code=403, detail="invalid push key")


def update_robot_heartbeat(robot_id: str) -> None:
    robot_heartbeats[robot_id] = datetime.utcnow()


def seed_users_config() -> list[SeedUserConfig]:
    return parse_seed_entries(settings.seed_users_json, SeedUserConfig, "SEED_USERS_JSON")


def seed_lobbies_config() -> list[SeedLobbyConfig]:
    return parse_seed_entries(settings.seed_lobbies_json, SeedLobbyConfig, "SEED_LOBBIES_JSON")


def seed_bots_config() -> list[SeedBotConfig]:
    return parse_seed_entries(settings.seed_bots_json, SeedBotConfig, "SEED_BOTS_JSON")


async def prepare_database(max_attempts: int = 60, delay: int = 5) -> None:
    attempt = 0
    while True:
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(
                    text(
                        "ALTER TABLE lobbies ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT false"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE lobbies ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT false"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE bots ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT false"
                    )
                )
            logger.info("Database connection established after %s attempt(s)", attempt + 1)
            break
        except Exception as exc:  # pragma: no cover - startup diagnostics
            attempt += 1
            if attempt >= max_attempts:
                logger.error("Database preparation failed after %s attempts: %s", attempt, exc)
                raise
            logger.warning(
                "Database connection attempt %s/%s failed: %s; retrying in %ss",
                attempt,
                max_attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)


async def apply_seed_data() -> None:
    users = seed_users_config()
    lobbies = seed_lobbies_config()
    bots = seed_bots_config()
    if not users and not lobbies and not bots:
        logger.debug("No seed data provided")
        return
    async with AsyncSessionLocal() as session:
        user_cache: dict[str, User] = {}
        lobby_cache: dict[str, Lobby] = {}
        for entry in users:
            email = entry.email.lower()
            result = await session.execute(select(User).where(User.email == email))
            user = result.scalar_one_or_none()
            pwd_hash = hash_password(entry.password)
            if not user:
                user = User(email=email, password_hash=pwd_hash)
                session.add(user)
                await session.flush()
                logger.info("Seeded user %s", email)
            elif not verify_password(entry.password, user.password_hash):
                user.password_hash = pwd_hash
                logger.info("Updated password for seed user %s", email)
            user_cache[email] = user
        for entry in lobbies:
            owner_email = entry.owner_email.lower()
            owner = user_cache.get(owner_email)
            if owner is None:
                owner = (
                    await session.execute(select(User).where(User.email == owner_email))
                ).scalar_one_or_none()
            if owner is None:
                logger.warning(
                    "Skipping seed lobby %s because owner %s does not exist",
                    entry.name,
                    owner_email,
                )
                continue
            desired_key = entry.access_key or settings.ros_push_key or secrets.token_urlsafe(16)
            result = await session.execute(select(Lobby).where(Lobby.name == entry.name))
            lobby = result.scalar_one_or_none()
            if not lobby:
                lobby = Lobby(
                    name=entry.name,
                    description=entry.description,
                    access_key=desired_key,
                    owner_id=owner.id,
                    ros_host="internal",
                    ros_port=0,
                    is_public=entry.is_public,
                )
                session.add(lobby)
                logger.info("Seeded lobby %s", entry.name)
            else:
                changed = False
                if lobby.owner_id != owner.id:
                    lobby.owner_id = owner.id
                    changed = True
                if lobby.description != entry.description:
                    lobby.description = entry.description
                    changed = True
                if lobby.is_public != entry.is_public:
                    lobby.is_public = entry.is_public
                    changed = True
                if lobby.is_deleted:
                    lobby.is_deleted = False
                    changed = True
                if desired_key and lobby.access_key != desired_key:
                    lobby.access_key = desired_key
                    changed = True
                if changed:
                    logger.info("Synchronized seed lobby %s", entry.name)
            lobby_cache[entry.name.lower()] = lobby
        for entry in bots:
            owner_email = entry.owner_email.lower()
            owner = user_cache.get(owner_email)
            if owner is None:
                owner = (
                    await session.execute(select(User).where(User.email == owner_email))
                ).scalar_one_or_none()
            if owner is None:
                logger.warning(
                    "Skipping seed bot %s because owner %s does not exist",
                    entry.name,
                    owner_email,
                )
                continue
            lobby_key = entry.lobby_name.lower()
            lobby = lobby_cache.get(lobby_key)
            if lobby is None:
                lobby = (
                    await session.execute(
                        select(Lobby).options(selectinload(Lobby.owner)).where(Lobby.name == entry.lobby_name)
                    )
                ).scalar_one_or_none()
                if lobby:
                    lobby_cache[lobby_key] = lobby
            if lobby is None:
                logger.warning(
                    "Skipping seed bot %s because lobby %s does not exist",
                    entry.name,
                    entry.lobby_name,
                )
                continue
            if lobby.owner_id != owner.id:
                lobby.owner_id = owner.id
                logger.info("Assigned lobby %s to owner %s for bot seeding", lobby.name, owner.email)
            namespace = entry.ros_namespace.strip()
            result = await session.execute(select(Bot).where(Bot.ros_namespace == namespace))
            bot = result.scalar_one_or_none()
            if not bot:
                bot = Bot(
                    name=entry.name.strip(),
                    ros_namespace=namespace,
                    description=entry.description,
                    lobby_id=lobby.id,
                )
                session.add(bot)
                logger.info("Seeded bot %s", entry.name)
            else:
                changed = False
                normalized_name = entry.name.strip()
                if bot.name != normalized_name:
                    bot.name = normalized_name
                    changed = True
                if bot.description != entry.description:
                    bot.description = entry.description
                    changed = True
                if bot.lobby_id != lobby.id:
                    bot.lobby_id = lobby.id
                    changed = True
                if bot.is_deleted:
                    bot.is_deleted = False
                    changed = True
                if changed:
                    logger.info("Synchronized seed bot %s", entry.name)
        await session.commit()


@app.on_event("startup")
async def startup_event() -> None:
    await prepare_database()
    await apply_seed_data()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    for pc in list(peer_connections):
        await pc.close()
    peer_connections.clear()


@app.get("/api/health")
async def health() -> dict[str, Any]:
    now = datetime.utcnow()
    timeout = timedelta(seconds=settings.heartbeat_timeout_seconds)
    active = [robot for robot, ts in robot_heartbeats.items() if now - ts < timeout]
    return {
        "status": "ok",
        "ros_connected": bool(active),
        "gateway": settings.gateway_name,
        "active_robots": active,
    }

@app.post("/api/robots/{robot_id}/cmd_vel", response_model=RobotCommandOut)
async def send_cmd_vel(
    robot_id: str,
    cmd: TwistCommand,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RobotCommandOut:
    namespace = robot_id.strip()
    if not namespace:
        raise HTTPException(status_code=400, detail="robot namespace required")
    command = RobotCommand(
        robot_namespace=namespace,
        linear_x=cmd.linear_x,
        linear_y=cmd.linear_y,
        linear_z=cmd.linear_z,
        angular_x=cmd.angular_x,
        angular_y=cmd.angular_y,
        angular_z=cmd.angular_z,
        requested_by=current_user.email,
    )
    session.add(command)
    await session.commit()
    await session.refresh(command)
    await cleanup_completed_commands(session, namespace)
    await broadcast_robot_command(command)
    return robot_command_to_out(command)


@app.post("/api/internal/frames/{robot_id}")
async def ingest_camera_frame(
    robot_id: str,
    payload: FramePayload,
    x_api_key: str = Header(default=""),
) -> dict[str, Any]:
    require_internal_api_key(x_api_key)
    update_robot_heartbeat(robot_id)
    try:
        decoded = base64.b64decode(payload.data)
    except binascii.Error as exc:
        raise HTTPException(status_code=400, detail=f"invalid frame payload: {exc}") from exc
    if payload.compressed:
        try:
            image_bytes = gzip.decompress(decoded)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid compressed frame payload: {exc}") from exc
    else:
        image_bytes = decoded
    queue = get_frame_queue(robot_id)
    if queue.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    queue.put_nowait((payload.width, payload.height, image_bytes))
    return {"robot": robot_id, "status": "queued"}


@app.websocket("/api/internal/ws/lobbies")
async def robot_command_bridge(websocket: WebSocket) -> None:
    api_key = websocket.query_params.get("api_key") or websocket.headers.get("x-api-key", "")
    require_internal_api_key(api_key or "")
    await websocket.accept()
    try:
        while True:
            message = await websocket.receive_json()
            msg_type = message.get("type")
            if msg_type == "subscribe":
                robots = [value.strip() for value in message.get("robots", []) if value.strip()]
                await register_robot_ws(websocket, robots)
                for robot in robots:
                    update_robot_heartbeat(robot)
                await send_pending_commands_to_connection(websocket, robots)
                await websocket.send_json({"type": "subscribed", "robots": robots})
            elif msg_type == "heartbeat":
                robots = [value.strip() for value in message.get("robots", []) if value.strip()]
                if not robots:
                    async with robot_ws_lock:
                        robots = list(websocket_robot_map.get(id(websocket), []))
                for robot in robots:
                    update_robot_heartbeat(robot)
                await websocket.send_json({"type": "heartbeat", "robots": robots, "status": "ok"})
            elif msg_type == "complete":
                robot = (message.get("robot") or "").strip()
                command_id = message.get("command_id")
                status = message.get("status") or "completed"
                ack_payload = {"type": "ack", "command_id": command_id, "status": status}
                if not command_id or not robot:
                    ack_payload["error"] = "command_id and robot required"
                    await websocket.send_json(ack_payload)
                    continue
                async with AsyncSessionLocal() as session:
                    command = await session.get(RobotCommand, command_id)
                    if not command or command.robot_namespace != robot:
                        ack_payload["error"] = "command not found"
                        await websocket.send_json(ack_payload)
                        continue
                    command.status = status
                    command.message = message.get("message")
                    command.completed_at = datetime.utcnow()
                    await session.commit()
                    await cleanup_completed_commands(session, robot)
                ack_payload["recorded"] = True
                await websocket.send_json(ack_payload)
            else:
                await websocket.send_json({"type": "error", "error": "unknown message", "payload": message})
    except WebSocketDisconnect:
        pass
    finally:
        await unregister_robot_ws(websocket)


@app.websocket("/api/ws/{robot_id}")
async def websocket_proxy(websocket: WebSocket, robot_id: str) -> None:
    await websocket.accept()
    await websocket.send_json(
        {
            "robot": robot_id,
            "message": "WebSocket channel established. Implement telemetry fan-out here.",
        }
    )
    try:
        while True:
            data = await websocket.receive_text()
            logger.debug("Received WS payload for %s: %s", robot_id, data)
    except WebSocketDisconnect:
        logger.info("Client disconnected from %s WS", robot_id)


@app.post("/api/robots/{robot_id}/webrtc")
async def start_webrtc(
    robot_id: str,
    offer: WebRTCOffer,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    queue = get_frame_queue(robot_id)
    if queue.empty():
        logger.warning("No frames received yet for %s; WebRTC stream may be blank", robot_id)
    pc = RTCPeerConnection()
    peer_connections.add(pc)
    active_added = False

    def _set_active(state: bool) -> None:
        nonlocal active_added
        if state:
            if not active_added:
                active_robot_streams[robot_id].add(current_user.email)
                active_added = True
        else:
            if active_added:
                viewers = active_robot_streams.get(robot_id)
                if viewers is not None:
                    viewers.discard(current_user.email)
                    if not viewers:
                        active_robot_streams.pop(robot_id, None)
                active_added = False

    @pc.on("connectionstatechange")
    async def _on_state_change() -> None:
        state = pc.connectionState
        if state == "connected":
            _set_active(True)
        elif state in {"failed", "closed", "disconnected"}:
            _set_active(False)
            peer_connections.discard(pc)
            await pc.close()

    try:
        pc.addTrack(RobotVideoTrack(robot_id))
        rtc_offer = RTCSessionDescription(sdp=offer.sdp, type=offer.type)
        await pc.setRemoteDescription(rtc_offer)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
    finally:
        if pc.connectionState in {"closed", "failed"}:
            _set_active(False)


def get_frame_queue(robot_id: str) -> asyncio.Queue[Tuple[int, int, bytes]]:
    if robot_id not in frame_queues:
        frame_queues[robot_id] = asyncio.Queue(maxsize=1)
    return frame_queues[robot_id]


class RobotVideoTrack(VideoStreamTrack):
    def __init__(self, robot_id: str):
        super().__init__()
        self.robot_id = robot_id

    async def recv(self) -> VideoFrame:
        queue = get_frame_queue(self.robot_id)
        width, height, payload = await queue.get()
        logger.info("RobotVideoTrack sending frame for %s: %sx%s", self.robot_id, width, height)
        array = np.frombuffer(payload, dtype=np.uint8).reshape((height, width, 4))
        frame = VideoFrame.from_ndarray(array, format="bgra")
        frame.pts, frame.time_base = await self.next_timestamp()
        return frame
