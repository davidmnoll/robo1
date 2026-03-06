import asyncio
import contextlib
import base64
import binascii
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple, TypeVar

import numpy as np
import roslibpy
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
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship, selectinload


IdentifierStr = constr(min_length=3)


class SeedUserConfig(BaseModel):
    email: IdentifierStr
    password: constr(min_length=1, max_length=32)


class SeedLobbyConfig(BaseModel):
    name: str
    ros_host: str
    ros_port: int
    description: Optional[str] = None
    access_key: Optional[str] = None
    owner_email: IdentifierStr


class SeedBotConfig(BaseModel):
    name: str
    ros_namespace: str
    lobby_name: str
    owner_email: IdentifierStr
    description: Optional[str] = None


class Settings(BaseSettings):
    ros_bridge_host: str = Field("localhost", alias="ROS_BRIDGE_HOST")
    ros_bridge_port: int = Field(9090, alias="ROS_BRIDGE_PORT")
    ros_push_key: str = Field("local-dev-key", alias="ROS_PUSH_KEY")
    gateway_name: str = Field("gateway-1", alias="GATEWAY_NAME")
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["*"], alias="CORS_ALLOW_ORIGINS")
    database_url: str = Field("postgresql+asyncpg://robot:robot@localhost:5432/robotarena", alias="DATABASE_URL")
    secret_key: str = Field("super-secret-key", alias="SECRET_KEY")
    access_token_expire_minutes: int = Field(60, alias="ACCESS_TOKEN_EXPIRE_MINUTES")
    seed_users_json: Optional[str] = Field(None, alias="SEED_USERS_JSON")
    seed_lobbies_json: Optional[str] = Field(None, alias="SEED_LOBBIES_JSON")
    seed_bots_json: Optional[str] = Field(None, alias="SEED_BOTS_JSON")


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

    lobby = relationship("Lobby", back_populates="bots")


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


logger = logging.getLogger("gateway")
logging.basicConfig(level=logging.INFO)
frame_queues: Dict[str, asyncio.Queue[Tuple[int, int, bytes]]] = {}
peer_connections: set[RTCPeerConnection] = set()

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
    name: str
    ros_host: str
    ros_port: int
    description: Optional[str] = None


class BotCreate(BaseModel):
    lobby_id: int
    name: constr(min_length=1, strip_whitespace=True)
    ros_namespace: constr(min_length=1, strip_whitespace=True)
    description: Optional[str] = None


class LobbyOut(BaseModel):
    id: int
    name: str
    ros_host: str
    ros_port: int
    description: Optional[str]
    access_key: Optional[str]
    owner_email: IdentifierStr
    created_at: datetime


class BotOut(BaseModel):
    id: int
    name: str
    ros_namespace: str
    description: Optional[str]
    lobby_id: int
    lobby_name: str
    owner_email: IdentifierStr
    created_at: datetime


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
    result = await session.execute(
        select(Lobby).options(selectinload(Lobby.owner)).order_by(Lobby.created_at.desc())
    )
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
        name=payload.name,
        ros_host=payload.ros_host,
        ros_port=payload.ros_port,
        description=payload.description,
        access_key=access_key,
        owner_id=current_user.id,
    )
    session.add(lobby)
    await session.commit()
    await session.refresh(lobby)
    lobby.owner = current_user
    return lobby_to_out(lobby, current_user)


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
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="ROS namespace already registered")
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


@app.post("/api/internal/lobbies/{lobby_name}/online")
async def register_lobby_online(
    lobby_name: str,
    payload: LobbyOnlineRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(Lobby).where(Lobby.name == lobby_name)
    result = await session.execute(stmt)
    lobby = result.scalar_one_or_none()
    if lobby is None:
        raise HTTPException(status_code=404, detail="Lobby not found")
    if lobby.access_key != payload.access_key:
        raise HTTPException(status_code=403, detail="Invalid lobby key")
    task: asyncio.Task | None = getattr(app.state, "ros_monitor_task", None)
    if task is None or task.done():
        if task and task.done() and task.exception():
            logger.warning("Previous ROS monitor task ended with error: %s", task.exception())
        logger.info("Received lobby %s online notification; starting ROS monitor", lobby_name)
        app.state.ros_monitor_task = asyncio.create_task(monitor_ros_connection())
    return {"status": "monitoring", "lobby": lobby_name}


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
    return LobbyOut(
        id=lobby.id,
        name=lobby.name,
        ros_host=lobby.ros_host,
        ros_port=lobby.ros_port,
        description=lobby.description,
        access_key=key,
        owner_email=owner_email,
        created_at=lobby.created_at,
    )


def bot_to_out(bot: Bot) -> BotOut:
    lobby = bot.lobby
    owner_email = lobby.owner.email if lobby and lobby.owner else ""
    lobby_name = lobby.name if lobby else ""
    return BotOut(
        id=bot.id,
        name=bot.name,
        ros_namespace=bot.ros_namespace,
        description=bot.description,
        lobby_id=bot.lobby_id,
        lobby_name=lobby_name,
        owner_email=owner_email,
        created_at=bot.created_at,
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
                    ros_host=entry.ros_host,
                    ros_port=entry.ros_port,
                    description=entry.description,
                    access_key=desired_key,
                    owner_id=owner.id,
                )
                session.add(lobby)
                logger.info("Seeded lobby %s", entry.name)
            else:
                changed = False
                if lobby.owner_id != owner.id:
                    lobby.owner_id = owner.id
                    changed = True
                if lobby.ros_host != entry.ros_host:
                    lobby.ros_host = entry.ros_host
                    changed = True
                if lobby.ros_port != entry.ros_port:
                    lobby.ros_port = entry.ros_port
                    changed = True
                if lobby.description != entry.description:
                    lobby.description = entry.description
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
                if changed:
                    logger.info("Synchronized seed bot %s", entry.name)
        await session.commit()


async def ros_client() -> roslibpy.Ros:
    ros: roslibpy.Ros | None = getattr(app.state, "ros_client", None)
    if ros is None or not ros.is_connected:
        raise HTTPException(status_code=503, detail="ROS bridge unavailable")
    return ros


@app.on_event("startup")
async def startup_event() -> None:
    await prepare_database()
    await apply_seed_data()
    app.state.ros_monitor_task = None


@app.on_event("shutdown")
async def shutdown_event() -> None:
    monitor: asyncio.Task | None = getattr(app.state, "ros_monitor_task", None)
    if monitor:
        monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor
    ros: roslibpy.Ros | None = getattr(app.state, "ros_client", None)
    if ros:
        ros.terminate()
        logger.info("Disconnected from ROS bridge")
    for pc in list(peer_connections):
        await pc.close()
    peer_connections.clear()


@app.get("/api/health")
async def health() -> dict[str, Any]:
    ros: roslibpy.Ros | None = getattr(app.state, "ros_client", None)
    return {
        "status": "ok",
        "ros_connected": bool(ros and ros.is_connected),
        "gateway": settings.gateway_name,
    }


def publish_twist(ros: roslibpy.Ros, robot_id: str, cmd: TwistCommand) -> None:
    topic_name = f"/{robot_id}/cmd_vel"
    ros_topic = roslibpy.Topic(
        ros,
        topic_name,
        "geometry_msgs/msg/Twist",
    )
    ros_topic.publish(
        {
            "linear": {"x": cmd.linear_x, "y": cmd.linear_y, "z": cmd.linear_z},
            "angular": {"x": cmd.angular_x, "y": cmd.angular_y, "z": cmd.angular_z},
        }
    )
    ros_topic.unadvertise()


@app.post("/api/robots/{robot_id}/cmd_vel")
async def send_cmd_vel(robot_id: str, cmd: TwistCommand, ros: roslibpy.Ros = Depends(ros_client)) -> dict[str, Any]:
    publish_twist(ros, robot_id, cmd)
    return {"robot": robot_id, "status": "queued"}


@app.post("/api/internal/frames/{robot_id}")
async def ingest_camera_frame(
    robot_id: str,
    payload: FramePayload,
    x_api_key: str = Header(default=""),
) -> dict[str, Any]:
    if settings.ros_push_key and x_api_key != settings.ros_push_key:
        raise HTTPException(status_code=403, detail="invalid push key")
    try:
        image_bytes = base64.b64decode(payload.data)
    except binascii.Error as exc:
        raise HTTPException(status_code=400, detail=f"invalid frame payload: {exc}") from exc
    queue = get_frame_queue(robot_id)
    if queue.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    queue.put_nowait((payload.width, payload.height, image_bytes))
    return {"robot": robot_id, "status": "queued"}


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
async def start_webrtc(robot_id: str, offer: WebRTCOffer) -> dict[str, str]:
    queue = get_frame_queue(robot_id)
    if queue.empty():
        logger.warning("No frames received yet for %s; WebRTC stream may be blank", robot_id)
    pc = RTCPeerConnection()
    peer_connections.add(pc)

    @pc.on("connectionstatechange")
    async def _on_state_change() -> None:
        if pc.connectionState in {"failed", "closed"}:
            peer_connections.discard(pc)
            await pc.close()

    pc.addTrack(RobotVideoTrack(robot_id))
    rtc_offer = RTCSessionDescription(sdp=offer.sdp, type=offer.type)
    await pc.setRemoteDescription(rtc_offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


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


async def monitor_ros_connection(delay: int = 5) -> None:
    while True:
        ros = await _connect_ros()
        if ros is None:
            await asyncio.sleep(delay)
            continue
        app.state.ros_client = ros
        logger.info(
            "Gateway %s connected to ROS bridge %s:%s",
            settings.gateway_name,
            settings.ros_bridge_host,
            settings.ros_bridge_port,
        )
        try:
            while ros.is_connected:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            ros.terminate()
            raise
        finally:
            ros.terminate()
            if getattr(app.state, "ros_client", None) is ros:
                app.state.ros_client = None
            logger.warning("ROS bridge connection lost; retrying in %ss", delay)
        await asyncio.sleep(delay)


async def _connect_ros(max_wait: float = 10.0) -> roslibpy.Ros | None:
    try:
        ros = roslibpy.Ros(
            host=settings.ros_bridge_host,
            port=settings.ros_bridge_port,
            is_secure=False,
        )
        ros.run()
        elapsed = 0.0
        while not ros.is_connected and elapsed < max_wait:
            await asyncio.sleep(0.1)
            elapsed += 0.1
        if ros.is_connected:
            return ros
        ros.terminate()
    except Exception as exc:  # pragma: no cover - network failures
        logger.warning("ROS bridge connection attempt failed: %s", exc)
    return None
