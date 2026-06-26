import asyncio
import contextlib
import json
import logging
import os
import secrets
import struct
import time as _time
from collections import defaultdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Annotated, Any, Dict, Optional, TypeVar

import numpy as np
from aiortc import AudioStreamTrack, MediaStreamTrack, RTCDataChannel, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRelay
from av import AudioFrame
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
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
from pydantic import BaseModel, Field, ValidationError, constr, field_validator
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
    stun_server: str = Field("", alias="STUN_SERVER")


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
    chat_messages = relationship("ChatMessage", back_populates="lobby", cascade="all, delete-orphan")
    virtual_elements = relationship("VirtualWorldElement", back_populates="lobby", cascade="all, delete-orphan")
    virtual_players = relationship("VirtualPlayer", back_populates="lobby", cascade="all, delete-orphan")


class Bot(Base):
    __tablename__ = "bots"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    ros_namespace = Column(String(255), nullable=False, index=True)
    description = Column(Text, nullable=True)
    volume = Column(Float, nullable=False, default=1.0, server_default=text("1.0"))
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


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    lobby_id = Column(Integer, ForeignKey("lobbies.id"), nullable=False, index=True)
    user_id = Column(String(255), nullable=True)  # null for system messages
    user_name = Column(String(255), nullable=True)
    message_type = Column(String(32), default="text")  # "text" or "system"
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    lobby = relationship("Lobby", back_populates="chat_messages")


class VirtualWorldElement(Base):
    """Static elements in the virtual world: walls, fruits, notes, etc."""
    __tablename__ = "virtual_world_elements"

    id = Column(Integer, primary_key=True, index=True)
    lobby_id = Column(Integer, ForeignKey("lobbies.id"), nullable=False, index=True)
    element_type = Column(String(32), nullable=False)  # "wall", "fruit", "note"
    x = Column(Float, nullable=False)
    y = Column(Float, nullable=False)
    z = Column(Float, nullable=False, default=0.0)
    width = Column(Float, nullable=True)
    height = Column(Float, nullable=True)
    depth = Column(Float, nullable=True)
    rotation = Column(Float, nullable=False, default=0.0)
    data = Column(Text, nullable=True)  # JSON for element-specific data
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String(255), nullable=True)

    lobby = relationship("Lobby", back_populates="virtual_elements")


class VirtualPlayer(Base):
    """Virtual players (cubes) that can be spawned and controlled in the virtual world."""
    __tablename__ = "virtual_players"

    id = Column(Integer, primary_key=True, index=True)
    lobby_id = Column(Integer, ForeignKey("lobbies.id"), nullable=False, index=True)
    namespace = Column(String(255), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False)
    x = Column(Float, nullable=False, default=0.0)
    y = Column(Float, nullable=False, default=0.0)
    z = Column(Float, nullable=False, default=0.5)
    yaw = Column(Float, nullable=False, default=0.0)
    color = Column(String(7), nullable=False, default="#3b82f6")
    created_at = Column(DateTime, default=datetime.utcnow)
    is_deleted = Column(Boolean, nullable=False, default=False)

    lobby = relationship("Lobby", back_populates="virtual_players")


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gateway")
active_robot_streams: Dict[str, set[str]] = defaultdict(set)
robot_heartbeats: Dict[str, datetime] = {}

# SFU state
media_relay = MediaRelay()
# Incoming video track from forwarder per robot (Hop 1)
robot_incoming_tracks: Dict[str, MediaStreamTrack] = {}
# Incoming audio track from forwarder per robot (Hop 1)
robot_incoming_audio_tracks: Dict[str, MediaStreamTrack] = {}
# Hop 1 PeerConnection from forwarder per robot
robot_forwarder_pcs: Dict[str, RTCPeerConnection] = {}
# Hop 2 PeerConnections (one per browser viewer) per robot
robot_browser_pcs: Dict[str, list[RTCPeerConnection]] = defaultdict(list)
# Event set when forwarder track arrives (for browser waiters)
robot_track_ready: Dict[str, asyncio.Event] = {}
robot_audio_track_ready: Dict[str, asyncio.Event] = {}
command_subscribers: Dict[str, set[WebSocket]] = defaultdict(set)
websocket_robot_map: Dict[int, set[str]] = {}
# Internal bridge websockets (for sending start_stream/stop_stream)
bridge_websockets: set[WebSocket] = set()
command_ws_lock = asyncio.Lock()
# Track which lobbies have active ros-bridge connections
connected_lobby_ids: set[int] = set()
# Telemetry subscribers - maps robot_id to set of websockets
telemetry_subscribers: Dict[str, set[WebSocket]] = defaultdict(set)
telemetry_ws_lock = asyncio.Lock()
# Latest telemetry per robot for initial state on connect
latest_telemetry: Dict[str, Dict[str, Any]] = {}
# Lobby status subscribers (browser clients)
lobby_status_subscribers: set[WebSocket] = set()
lobby_status_lock = asyncio.Lock()
# Chat subscribers - maps lobby_id to set of WebSockets
chat_subscribers: Dict[int, set[WebSocket]] = defaultdict(set)
chat_lock = asyncio.Lock()


# Browser audio relay tracks - for forwarding browser mic audio to ros-bridge (Hop 1)
browser_audio_relay_tracks: Dict[str, "BrowserAudioRelayTrack"] = {}

# Active browser audio forwarding tasks (key: "robot_id:user_email")
browser_audio_forward_tasks: Dict[str, asyncio.Task] = {}

# Track browser PCs by user for renegotiation (key: "robot_id:user_email")
browser_user_pcs: Dict[str, RTCPeerConnection] = {}
# Robot audio broadcasters - consumes Hop1 audio and broadcasts to Hop2 subscribers
robot_audio_broadcasters: Dict[str, "RobotAudioBroadcaster"] = {}

# Data channels for control and telemetry relay
# Hop1: ros-bridge <-> API
hop1_telemetry_channels: Dict[str, RTCDataChannel] = {}  # robot_id -> channel from ros-bridge
hop1_control_channels: Dict[str, RTCDataChannel] = {}    # robot_id -> channel to ros-bridge
hop1_map_channels: Dict[str, RTCDataChannel] = {}        # robot_id -> map channel from ros-bridge
# Hop2: API <-> Browser(s)
hop2_telemetry_channels: Dict[str, list[RTCDataChannel]] = defaultdict(list)  # robot_id -> list of channels to browsers
hop2_control_channels: Dict[str, list[RTCDataChannel]] = defaultdict(list)    # robot_id -> list of channels from browsers
hop2_map_channels: Dict[str, list[RTCDataChannel]] = defaultdict(list)        # robot_id -> list of map channels to browsers

# Audio routing preferences per user (key: "robot_id:user_email")
# Value: {"to_group": bool, "to_robot": bool}
user_audio_routing: Dict[str, dict] = {}


class RobotAudioBroadcaster:
    """Consumes audio from Hop1 source track and broadcasts to multiple Hop2 relay tracks.

    This bypasses MediaRelay which has known issues with audio tracks.
    """

    def __init__(self, source_track: MediaStreamTrack, robot_id: str) -> None:
        self.source_track = source_track
        self.robot_id = robot_id
        self._subscribers: list[asyncio.Queue] = []
        self._running = False
        self._task: asyncio.Task | None = None
        self._frame_count = 0
        logger.info("RobotAudioBroadcaster created for %s", robot_id)

    def start(self) -> None:
        """Start the consumer task."""
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._consume_loop())
            logger.info("RobotAudioBroadcaster started for %s", self.robot_id)

    def stop(self) -> None:
        """Stop the consumer task."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        # Signal all subscribers to stop
        for q in self._subscribers:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        logger.info("RobotAudioBroadcaster stopped for %s", self.robot_id)

    def subscribe(self) -> asyncio.Queue:
        """Create a new subscriber queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._subscribers.append(q)
        logger.info("RobotAudioBroadcaster: new subscriber for %s (total=%d)",
                    self.robot_id, len(self._subscribers))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Remove a subscriber queue."""
        if q in self._subscribers:
            self._subscribers.remove(q)
            logger.info("RobotAudioBroadcaster: removed subscriber for %s (total=%d)",
                        self.robot_id, len(self._subscribers))

    async def _consume_loop(self) -> None:
        """Continuously read from source track and broadcast to subscribers."""
        logger.info("RobotAudioBroadcaster: starting consume loop for %s, track state=%s",
                    self.robot_id, getattr(self.source_track, 'readyState', 'unknown'))
        try:
            while self._running:
                # Check track state
                state = getattr(self.source_track, 'readyState', 'unknown')
                if state != 'live':
                    logger.warning("RobotAudioBroadcaster: track not live for %s (state=%s), waiting...",
                                   self.robot_id, state)
                    await asyncio.sleep(0.1)
                    continue
                try:
                    # Use timeout to detect if recv() is blocking forever
                    try:
                        frame = await asyncio.wait_for(self.source_track.recv(), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.warning("RobotAudioBroadcaster: recv() timed out for %s (no frames in 5s), track state=%s",
                                       self.robot_id, getattr(self.source_track, 'readyState', 'unknown'))
                        continue
                    self._frame_count += 1

                    if self._frame_count <= 3 or self._frame_count % 100 == 0:
                        logger.info("RobotAudioBroadcaster: frame %d for %s, subscribers=%d",
                                    self._frame_count, self.robot_id, len(self._subscribers))

                    # Broadcast to all subscribers
                    for q in self._subscribers:
                        try:
                            q.put_nowait(frame)
                        except asyncio.QueueFull:
                            # Drop oldest frame and add new one
                            try:
                                q.get_nowait()
                                q.put_nowait(frame)
                            except (asyncio.QueueEmpty, asyncio.QueueFull):
                                pass
                except Exception as e:
                    if self._running:
                        import traceback
                        logger.error("RobotAudioBroadcaster: error for %s: %s (type=%s)",
                                     self.robot_id, e, type(e).__name__)
                        logger.error("RobotAudioBroadcaster traceback: %s", traceback.format_exc())
                    break
        finally:
            logger.info("RobotAudioBroadcaster: consume loop ended for %s", self.robot_id)


class RobotAudioRelayTrack(AudioStreamTrack):
    """Relay track that receives audio from a broadcaster queue and sends to browser."""

    kind = "audio"

    def __init__(self, queue: asyncio.Queue, robot_id: str) -> None:
        super().__init__()
        self._queue = queue
        self.robot_id = robot_id
        self._frame_count = 0
        self._pts = 0
        logger.info("RobotAudioRelayTrack created for %s", robot_id)

    async def recv(self) -> AudioFrame:
        """Get next frame from the queue."""
        frame = await self._queue.get()

        if frame is None:
            raise Exception("Audio stream ended")

        self._frame_count += 1

        # Update pts for this consumer's timeline
        frame.pts = self._pts
        self._pts += frame.samples

        if self._frame_count <= 3 or self._frame_count % 100 == 0:
            logger.info("RobotAudioRelayTrack: frame %d for %s", self._frame_count, self.robot_id)

        return frame


class Hop1AudioTrack(AudioStreamTrack):
    """Wraps the incoming audio track from Hop1 to log RMS and forward frames.

    This sits between the raw WebRTC track and consumers (MediaRelay or direct).
    Used to verify audio is actually arriving at the API server from ros-bridge.
    """

    kind = "audio"

    def __init__(self, source_track: MediaStreamTrack, robot_id: str) -> None:
        super().__init__()
        self.source_track = source_track
        self.robot_id = robot_id
        self._frame_count = 0
        logger.info("Hop1AudioTrack created for %s (wrapping incoming Hop1 audio)", robot_id)

    async def recv(self) -> AudioFrame:
        """Get frame from source, extract mono from stereo to fix Opus duplication."""
        frame = await self.source_track.recv()
        self._frame_count += 1

        # Convert to numpy
        arr = frame.to_ndarray()

        # Log format for debugging
        if self._frame_count <= 5 or self._frame_count % 100 == 0:
            try:
                rms = np.sqrt(np.mean(arr.astype(np.float32)**2))
                logger.info("Hop1AudioTrack: frame %d for %s - shape=%s, rms=%.1f, samples=%d, rate=%d",
                            self._frame_count, self.robot_id, arr.shape, rms, frame.samples, frame.sample_rate)
            except Exception as e:
                logger.warning("Hop1AudioTrack: couldn't compute RMS for %s: %s", self.robot_id, e)

        # Extract mono from stereo to undo Opus duplication
        # ros-bridge sends interleaved stereo (L,R,L,R), extract left channel
        samples = arr.flatten()
        if len(samples) > frame.samples:
            # Interleaved stereo - extract left channel
            samples = samples[::2]

        # Create mono frame for downstream
        mono_frame = AudioFrame.from_ndarray(
            samples.reshape(1, -1).astype(np.int16), format="s16", layout="mono"
        )
        mono_frame.sample_rate = frame.sample_rate
        mono_frame.pts = frame.pts

        return mono_frame


class AudioLoggingTrack(AudioStreamTrack):
    """Wrapper that logs RMS of audio frames from MediaRelay for debugging."""

    kind = "audio"

    def __init__(self, source_track: MediaStreamTrack, robot_id: str) -> None:
        super().__init__()
        self.source_track = source_track
        self.robot_id = robot_id
        self._frame_count = 0
        logger.info("AudioLoggingTrack created for %s (wrapping MediaRelay output)", robot_id)

    async def recv(self) -> AudioFrame:
        """Forward frame from source and log RMS."""
        if self._frame_count == 0:
            logger.info("AudioLoggingTrack: recv() called first time for %s", self.robot_id)
        frame = await self.source_track.recv()
        if self._frame_count == 0:
            logger.info("AudioLoggingTrack: got first frame for %s", self.robot_id)
        self._frame_count += 1

        # Log RMS for first few frames and every 100th
        if self._frame_count <= 5 or self._frame_count % 100 == 0:
            try:
                arr = frame.to_ndarray()
                rms = np.sqrt(np.mean(arr.astype(np.float32)**2))
                logger.info("AudioLoggingTrack: frame %d for %s - rms=%.1f, samples=%d, rate=%d",
                            self._frame_count, self.robot_id, rms, frame.samples, frame.sample_rate)
            except Exception as e:
                logger.warning("AudioLoggingTrack: couldn't compute RMS for %s: %s", self.robot_id, e)

        return frame


class BrowserAudioRelayTrack(AudioStreamTrack):
    """Relay track that receives audio from browser and sends to ros-bridge.

    This track is added to Hop 1 (API -> ros-bridge) to send browser audio.
    Audio frames are pushed from Hop 2 when browser sends mic data.
    Splits incoming frames into 20ms chunks (960 samples) for Opus compatibility.
    """

    kind = "audio"
    SAMPLES_PER_FRAME = 960  # 20ms at 48kHz - required for Opus

    def __init__(self, robot_id: str, sample_rate: int = 48000, channels: int = 1) -> None:
        super().__init__()
        self.robot_id = robot_id
        self.sample_rate = sample_rate
        self.channels = channels
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._pts = 0
        self._running = True
        self._push_count = 0
        self._recv_count = 0
        self._buffer = np.array([], dtype=np.int16)  # Buffer for splitting frames

    def push_frame(self, frame: AudioFrame) -> None:
        """Push an audio frame to the relay queue (called from Hop 2 track handler).

        Normalizes frame to mono 48kHz s16 format and splits into 20ms chunks.
        """
        try:
            # Convert frame to numpy
            arr = frame.to_ndarray()  # shape depends on format

            self._push_count += 1
            # Log RMS for first few frames and periodically
            if self._push_count <= 5 or self._push_count % 100 == 0:
                rms = np.sqrt(np.mean(arr.astype(np.float32)**2))
                logger.info("BrowserAudioRelay push_frame %d for %s: shape=%s, dtype=%s, rms=%.1f, min=%d, max=%d, frame.samples=%d",
                            self._push_count, self.robot_id, arr.shape, arr.dtype, rms, arr.min(), arr.max(), frame.samples)

            # Handle different stereo formats
            if arr.shape[0] > 1:
                # Planar stereo (2, N): take left channel
                samples = arr[0].astype(np.int16)
            elif arr.shape[1] > frame.samples:
                # Packed/interleaved stereo (1, N*2): extract left channel (every other sample)
                samples = arr.flatten()[::2].astype(np.int16)
            else:
                # Mono (1, N): use as-is
                samples = arr.flatten()

            self._buffer = np.concatenate([self._buffer, samples])

            # Split buffer into 20ms chunks (960 samples)
            while len(self._buffer) >= self.SAMPLES_PER_FRAME:
                chunk = self._buffer[:self.SAMPLES_PER_FRAME]
                self._buffer = self._buffer[self.SAMPLES_PER_FRAME:]

                # Create interleaved stereo to work around Opus sample duplication
                # PyAV packed stereo expects shape (1, samples*2) with L,R,L,R pattern
                chunk_interleaved = np.empty(len(chunk) * 2, dtype=np.int16)
                chunk_interleaved[0::2] = chunk  # Left channel
                chunk_interleaved[1::2] = chunk  # Right channel (same data)
                audio_frame = AudioFrame.from_ndarray(
                    chunk_interleaved.reshape(1, -1), format="s16", layout="stereo"
                )
                audio_frame.sample_rate = self.sample_rate

                try:
                    self._queue.put_nowait(audio_frame)
                except asyncio.QueueFull:
                    # Drop oldest frame if queue is full
                    try:
                        self._queue.get_nowait()
                        self._queue.put_nowait(audio_frame)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass
        except Exception as e:
            logger.warning("BrowserAudioRelay push_frame error for %s: %s", self.robot_id, e)

    async def recv(self) -> AudioFrame:
        """Receive the next audio frame to send to ros-bridge."""
        while self._running:
            try:
                frame = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                # Update pts for proper timing
                frame.pts = self._pts
                self._pts += frame.samples

                self._recv_count += 1
                # Log RMS for first few frames and periodically
                if self._recv_count <= 5 or self._recv_count % 100 == 0:
                    try:
                        arr = frame.to_ndarray()
                        rms = np.sqrt(np.mean(arr.astype(np.float32)**2))
                        logger.info("BrowserAudioRelay recv %d for %s: rms=%.1f, samples=%d, shape=%s, qsize=%d",
                                    self._recv_count, self.robot_id, rms, frame.samples, arr.shape, self._queue.qsize())
                    except Exception as e:
                        logger.warning("BrowserAudioRelay recv logging error: %s", e)

                return frame
            except asyncio.TimeoutError:
                # Generate silence in stereo to match push_frame format
                silence = np.zeros(1920, dtype=np.int16)  # 20ms stereo at 48kHz (interleaved)
                frame = AudioFrame.from_ndarray(silence.reshape(1, -1), format="s16", layout="stereo")
                frame.sample_rate = self.sample_rate
                frame.pts = self._pts
                self._pts += frame.samples
                return frame

    def stop(self) -> None:
        """Stop the relay track."""
        self._running = False
        super().stop()


class GroupAudioMixer:
    """Mixes audio from multiple users for group calling.

    Each user's audio frames are stored in a per-user buffer.
    When mixed audio is requested, all users' recent frames are combined.
    """

    SAMPLES_PER_FRAME = 960  # 20ms at 48kHz
    MAX_BUFFER_FRAMES = 5   # Keep last 100ms of audio per user

    def __init__(self, robot_id: str) -> None:
        self.robot_id = robot_id
        # user_key -> list of recent audio frames (as numpy arrays)
        self._user_buffers: Dict[str, list[np.ndarray]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._push_count = 0
        logger.info("GroupAudioMixer created for %s", robot_id)

    def push_frame(self, user_key: str, frame: AudioFrame) -> None:
        """Push an audio frame from a user to the mixer."""
        try:
            arr = frame.to_ndarray()
            # Convert to mono if needed
            if arr.shape[0] > 1:
                samples = arr[0].astype(np.int16)
            elif len(arr.shape) > 1 and arr.shape[1] > frame.samples:
                samples = arr.flatten()[::2].astype(np.int16)
            else:
                samples = arr.flatten().astype(np.int16)

            # Store in user's buffer
            self._user_buffers[user_key].append(samples)
            # Keep only recent frames
            while len(self._user_buffers[user_key]) > self.MAX_BUFFER_FRAMES:
                self._user_buffers[user_key].pop(0)

            self._push_count += 1
            if self._push_count % 500 == 1:
                logger.info("GroupAudioMixer %s: push from %s, active_users=%d",
                           self.robot_id, user_key, len(self._user_buffers))
        except Exception as e:
            logger.warning("GroupAudioMixer push error for %s: %s", self.robot_id, e)

    def get_mixed_frame(self, exclude_user: str = None) -> np.ndarray:
        """Get mixed audio from all users, optionally excluding one user.

        Returns a mono int16 numpy array of SAMPLES_PER_FRAME samples.
        """
        mixed = np.zeros(self.SAMPLES_PER_FRAME, dtype=np.float32)
        contributing_users = 0

        for user_key, frames in self._user_buffers.items():
            if exclude_user and user_key == exclude_user:
                continue
            if not frames:
                continue
            # Use the most recent frame from this user
            frame_data = frames[-1]
            # Pad or truncate to SAMPLES_PER_FRAME
            if len(frame_data) >= self.SAMPLES_PER_FRAME:
                mixed += frame_data[:self.SAMPLES_PER_FRAME].astype(np.float32)
            else:
                mixed[:len(frame_data)] += frame_data.astype(np.float32)
            contributing_users += 1

        # Normalize if multiple users to avoid clipping
        if contributing_users > 1:
            mixed = mixed / contributing_users

        # Clip to int16 range and convert
        mixed = np.clip(mixed, -32768, 32767).astype(np.int16)
        return mixed

    def remove_user(self, user_key: str) -> None:
        """Remove a user's audio buffer when they disconnect."""
        self._user_buffers.pop(user_key, None)
        logger.info("GroupAudioMixer %s: removed user %s, remaining=%d",
                   self.robot_id, user_key, len(self._user_buffers))

    def has_audio(self, exclude_user: str = None) -> bool:
        """Check if there's any audio to mix (excluding a specific user)."""
        for user_key, frames in self._user_buffers.items():
            if exclude_user and user_key == exclude_user:
                continue
            if frames:
                return True
        return False


class GroupAudioRelayTrack(AudioStreamTrack):
    """Relay track that provides mixed group audio to a specific user.

    Pulls mixed audio from GroupAudioMixer, excluding the receiving user's own audio.
    """

    kind = "audio"
    SAMPLES_PER_FRAME = 960

    def __init__(self, mixer: GroupAudioMixer, user_key: str, sample_rate: int = 48000) -> None:
        super().__init__()
        self.mixer = mixer
        self.user_key = user_key
        self.sample_rate = sample_rate
        self._pts = 0
        self._running = True
        self._recv_count = 0
        logger.info("GroupAudioRelayTrack created for user %s on robot %s",
                   user_key, mixer.robot_id)

    async def recv(self) -> AudioFrame:
        """Get the next mixed audio frame (excluding self)."""
        if not self._running:
            raise Exception("Track stopped")

        # Small delay to allow frames to accumulate
        await asyncio.sleep(0.02)  # 20ms

        self._recv_count += 1

        # Get mixed audio excluding our own
        mixed = self.mixer.get_mixed_frame(exclude_user=self.user_key)

        # Create stereo frame (duplicate mono to both channels)
        stereo = np.empty(len(mixed) * 2, dtype=np.int16)
        stereo[0::2] = mixed
        stereo[1::2] = mixed

        frame = AudioFrame.from_ndarray(stereo.reshape(1, -1), format="s16", layout="stereo")
        frame.sample_rate = self.sample_rate
        frame.pts = self._pts
        self._pts += self.SAMPLES_PER_FRAME

        if self._recv_count % 500 == 1:
            rms = np.sqrt(np.mean(mixed.astype(np.float32)**2))
            has_audio = self.mixer.has_audio(exclude_user=self.user_key)
            logger.info("GroupAudioRelayTrack recv %d for %s: rms=%.1f, has_other_audio=%s",
                       self._recv_count, self.user_key, rms, has_audio)

        return frame

    def stop(self) -> None:
        """Stop the relay track."""
        self._running = False
        super().stop()


class MixedAudioTrack(AudioStreamTrack):
    """Mixes robot audio with group audio (other users) into a single track.

    This allows sending both robot audio and group call audio to the browser
    without needing a second audio track (which breaks SDP negotiation).
    """

    kind = "audio"
    SAMPLES_PER_FRAME = 960

    def __init__(
        self,
        robot_track: MediaStreamTrack,
        mixer: GroupAudioMixer,
        user_key: str,
        sample_rate: int = 48000,
    ) -> None:
        super().__init__()
        self.robot_track = robot_track
        self.mixer = mixer
        self.user_key = user_key
        self.sample_rate = sample_rate
        self._pts = 0
        self._running = True
        self._recv_count = 0
        logger.info("MixedAudioTrack created for user %s on robot %s",
                   user_key, mixer.robot_id)

    async def recv(self) -> AudioFrame:
        """Get the next mixed audio frame (robot + group, excluding self)."""
        if not self._running:
            raise Exception("Track stopped")

        self._recv_count += 1

        # Get robot audio frame
        try:
            robot_frame = await self.robot_track.recv()
            robot_arr = robot_frame.to_ndarray()
            # Convert to mono if stereo
            if robot_arr.shape[0] > 1:
                robot_samples = robot_arr[0].astype(np.float32)
            else:
                robot_samples = robot_arr.flatten().astype(np.float32)
            # Ensure correct length
            if len(robot_samples) > self.SAMPLES_PER_FRAME:
                robot_samples = robot_samples[:self.SAMPLES_PER_FRAME]
            elif len(robot_samples) < self.SAMPLES_PER_FRAME:
                robot_samples = np.pad(robot_samples, (0, self.SAMPLES_PER_FRAME - len(robot_samples)))
        except Exception as e:
            # If robot track fails, use silence
            if self._recv_count % 500 == 1:
                logger.warning("MixedAudioTrack: robot track error: %s", e)
            robot_samples = np.zeros(self.SAMPLES_PER_FRAME, dtype=np.float32)

        # Get group audio (other users, excluding self)
        group_samples = self.mixer.get_mixed_frame(exclude_user=self.user_key).astype(np.float32)

        # Mix: average if both have audio, otherwise use whichever has audio
        robot_rms = np.sqrt(np.mean(robot_samples**2))
        group_rms = np.sqrt(np.mean(group_samples**2))

        if robot_rms > 100 and group_rms > 100:
            # Both have audio - mix them (average)
            mixed = (robot_samples + group_samples) / 2
        elif group_rms > 100:
            # Only group audio
            mixed = group_samples
        else:
            # Only robot audio (or silence)
            mixed = robot_samples

        # Clip and convert to int16
        mixed = np.clip(mixed, -32768, 32767).astype(np.int16)

        # Create stereo frame
        stereo = np.empty(len(mixed) * 2, dtype=np.int16)
        stereo[0::2] = mixed
        stereo[1::2] = mixed

        frame = AudioFrame.from_ndarray(stereo.reshape(1, -1), format="s16", layout="stereo")
        frame.sample_rate = self.sample_rate
        frame.pts = self._pts
        self._pts += self.SAMPLES_PER_FRAME

        if self._recv_count % 500 == 1:
            logger.info("MixedAudioTrack recv %d for %s: robot_rms=%.1f, group_rms=%.1f",
                       self._recv_count, self.user_key, robot_rms, group_rms)

        return frame

    def stop(self) -> None:
        """Stop the mixed track."""
        self._running = False
        super().stop()


# Group audio mixers per robot
group_audio_mixers: Dict[str, GroupAudioMixer] = {}


def get_or_create_group_mixer(robot_id: str) -> GroupAudioMixer:
    """Get or create a group audio mixer for a robot."""
    if robot_id not in group_audio_mixers:
        group_audio_mixers[robot_id] = GroupAudioMixer(robot_id)
    return group_audio_mixers[robot_id]


class ControlAggregator:
    """Aggregates control commands from multiple users and sends averaged commands.

    Each user's most recent command is stored. Periodically, all commands are
    averaged and sent to the robot. Opposing inputs cancel out.
    """

    SEND_INTERVAL = 0.05  # 20Hz - send averaged command every 50ms
    STALE_TIMEOUT = 0.2   # Commands older than 200ms are ignored

    def __init__(self, robot_id: str) -> None:
        self.robot_id = robot_id
        # user_key -> (timestamp, parsed command)
        self._user_commands: Dict[str, tuple[float, dict]] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._send_count = 0
        logger.info("ControlAggregator created for %s", robot_id)

    def start(self) -> None:
        """Start the aggregation loop."""
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._send_loop())
            logger.info("ControlAggregator started for %s", self.robot_id)

    def stop(self) -> None:
        """Stop the aggregation loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("ControlAggregator stopped for %s", self.robot_id)

    def push_command(self, user_key: str, message: str) -> None:
        """Store a control command from a user.

        Joy commands are stored for averaging. Other commands (like camera_ptz)
        are forwarded immediately since they shouldn't be averaged.
        """
        try:
            cmd = json.loads(message)

            # Non-joy commands (camera_ptz, etc.) should be forwarded immediately
            if cmd.get("type") != "joy":
                channel = hop1_control_channels.get(self.robot_id)
                if channel and channel.readyState == "open":
                    channel.send(message)
                    logger.debug("ControlAggregator: forwarded %s command for %s",
                                cmd.get("type"), self.robot_id)
                return

            # Store joy commands for averaging
            self._user_commands[user_key] = (_time.time(), cmd)
        except json.JSONDecodeError:
            logger.warning("ControlAggregator: invalid JSON from %s", user_key)

    def remove_user(self, user_key: str) -> None:
        """Remove a user's commands when they disconnect."""
        self._user_commands.pop(user_key, None)
        logger.info("ControlAggregator %s: removed user %s", self.robot_id, user_key)

    async def _send_loop(self) -> None:
        """Periodically compute averaged command and send to robot."""
        while self._running:
            try:
                await asyncio.sleep(self.SEND_INTERVAL)
                self._send_averaged_command()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("ControlAggregator send loop error: %s", e)

    def _send_averaged_command(self) -> None:
        """Compute averaged command from all users and send to robot."""
        now = _time.time()
        valid_commands = []

        # Collect non-stale commands
        for user_key, (timestamp, cmd) in list(self._user_commands.items()):
            if now - timestamp > self.STALE_TIMEOUT:
                # Remove stale command
                self._user_commands.pop(user_key, None)
            elif cmd.get("type") == "joy":
                valid_commands.append(cmd)

        if not valid_commands:
            return  # No active commands

        # Average the axes
        num_users = len(valid_commands)
        averaged_axes = [0.0] * 6
        for cmd in valid_commands:
            axes = cmd.get("axes", [])
            for i in range(min(len(axes), 6)):
                averaged_axes[i] += axes[i]

        # Divide by number of users (averaging)
        averaged_axes = [a / num_users for a in averaged_axes]

        # For buttons, use OR (any user pressing counts)
        merged_buttons = [0] * 12
        for cmd in valid_commands:
            buttons = cmd.get("buttons", [])
            for i in range(min(len(buttons), 12)):
                if buttons[i]:
                    merged_buttons[i] = 1

        # Create averaged command
        averaged_cmd = {
            "type": "joy",
            "axes": averaged_axes,
            "buttons": merged_buttons,
            "timestamp": int(now * 1000),
            "users": num_users,  # Include for debugging
        }

        # Send to robot
        channel = hop1_control_channels.get(self.robot_id)
        if channel and channel.readyState == "open":
            try:
                channel.send(json.dumps(averaged_cmd))
                self._send_count += 1
                if self._send_count % 100 == 1:
                    logger.info("ControlAggregator %s: sent cmd #%d (users=%d, axes=%.2f,%.2f)",
                               self.robot_id, self._send_count, num_users,
                               averaged_axes[1], averaged_axes[3])
            except Exception as e:
                logger.warning("ControlAggregator send error: %s", e)


# Control aggregators per robot
control_aggregators: Dict[str, ControlAggregator] = {}


def get_or_create_control_aggregator(robot_id: str) -> ControlAggregator:
    """Get or create a control aggregator for a robot."""
    if robot_id not in control_aggregators:
        agg = ControlAggregator(robot_id)
        agg.start()
        control_aggregators[robot_id] = agg
    return control_aggregators[robot_id]


# STUN whitelist: maps IP -> expiry timestamp
stun_whitelist: Dict[str, float] = {}
STUN_WHITELIST_TTL = 3600  # 1 hour

# STUN protocol constants
_STUN_MAGIC_COOKIE = 0x2112A442
_STUN_BINDING_REQUEST = 0x0001
_STUN_BINDING_RESPONSE = 0x0101
_STUN_ATTR_XOR_MAPPED_ADDRESS = 0x0020
_STUN_HEADER_SIZE = 20


def allow_stun_ip(ip: str) -> None:
    """Add an IP to the STUN whitelist with TTL."""
    if ip and ip not in ("unknown", "127.0.0.1", "::1"):
        stun_whitelist[ip] = _time.time() + STUN_WHITELIST_TTL


def _get_client_ip(request: Request) -> str:
    """Extract real client IP, respecting X-Forwarded-For from reverse proxy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # First IP in the chain is the original client
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    if request.client:
        return request.client.host
    return ""


def _cleanup_stun_whitelist() -> None:
    now = _time.time()
    expired = [ip for ip, exp in stun_whitelist.items() if exp < now]
    for ip in expired:
        stun_whitelist.pop(ip, None)


class StunProtocol(asyncio.DatagramProtocol):
    """Minimal STUN server that only responds to whitelisted IPs."""

    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        client_ip, client_port = addr[0], addr[1]

        # Check whitelist
        if client_ip not in stun_whitelist or stun_whitelist[client_ip] < _time.time():
            return  # silently drop

        # Validate STUN binding request
        if len(data) < _STUN_HEADER_SIZE:
            return
        msg_type, msg_len, magic = struct.unpack_from("!HHI", data, 0)
        if msg_type != _STUN_BINDING_REQUEST or magic != _STUN_MAGIC_COOKIE:
            return
        transaction_id = data[8:20]

        # Build XOR-MAPPED-ADDRESS attribute (IPv4)
        xor_port = client_port ^ (_STUN_MAGIC_COOKIE >> 16)
        ip_parts = [int(p) for p in client_ip.split(".")]
        ip_int = (ip_parts[0] << 24) | (ip_parts[1] << 16) | (ip_parts[2] << 8) | ip_parts[3]
        xor_ip = ip_int ^ _STUN_MAGIC_COOKIE
        attr_value = struct.pack("!xBHI", 0x01, xor_port, xor_ip)  # family=IPv4
        attr = struct.pack("!HH", _STUN_ATTR_XOR_MAPPED_ADDRESS, len(attr_value)) + attr_value

        # Build response
        resp_header = struct.pack("!HHI", _STUN_BINDING_RESPONSE, len(attr), _STUN_MAGIC_COOKIE)
        resp = resp_header + transaction_id + attr
        self.transport.sendto(resp, addr)


app = FastAPI(title="Robot Gateway API", version="0.1.0")
cors_allow_origins = settings.cors_allow_origins
allow_credentials = True
if "*" in cors_allow_origins:
    # Wildcard origins are incompatible with credentialed requests per the CORS spec.
    allow_credentials = False
    cors_allow_origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=allow_credentials,
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


class TelemetryPayload(BaseModel):
    linear_speed: float = 0.0
    angular_speed: float = 0.0
    timestamp: float | None = None


class UserOut(BaseModel):
    id: int
    email: IdentifierStr


class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    user: UserOut


class RegisterRequest(BaseModel):
    email: Annotated[str, Field(min_length=3, description="Username (min 3 characters)")]
    password: Annotated[str, Field(min_length=6, max_length=32, description="Password (6-32 characters)")]

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if len(v) < 3:
            raise ValueError("Username must be at least 3 characters")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        if len(v) > 32:
            raise ValueError("Password must be at most 32 characters")
        return v


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
    volume: Optional[float] = 1.0


class BotUpdate(BaseModel):
    name: Optional[constr(min_length=1, strip_whitespace=True)] = None
    ros_namespace: Optional[constr(min_length=1, strip_whitespace=True)] = None
    description: Optional[str] = None
    volume: Optional[float] = None


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
    bot_namespaces: list[str]  # List of ros_namespace for status updates
    ros_connected: bool  # True if any bot in lobby has active ROS bridge connection


class BotOut(BaseModel):
    id: int
    name: str
    ros_namespace: str
    description: Optional[str]
    volume: float
    lobby_id: int
    lobby_name: str
    owner_email: IdentifierStr
    created_at: datetime
    is_deleted: bool
    active_streamers: list[str]


class LobbyDetailOut(LobbyOut):
    bots: list[BotOut]
    virtual_players: list[VirtualPlayerOut] = []


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


class ChatMessageCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)


class ChatMessageOut(BaseModel):
    id: int
    lobby_id: int
    user_id: Optional[str]
    user_name: Optional[str]
    message_type: str
    content: str
    created_at: datetime


class VirtualWorldElementCreate(BaseModel):
    element_type: constr(min_length=1, max_length=32)
    x: float
    y: float
    z: float = 0.0
    width: Optional[float] = None
    height: Optional[float] = None
    depth: Optional[float] = None
    rotation: float = 0.0
    data: Optional[str] = None


class VirtualWorldElementOut(BaseModel):
    id: int
    lobby_id: int
    element_type: str
    x: float
    y: float
    z: float
    width: Optional[float]
    height: Optional[float]
    depth: Optional[float]
    rotation: float
    data: Optional[str]
    created_at: datetime
    created_by: Optional[str]


class VirtualPlayerCreate(BaseModel):
    name: constr(min_length=1, max_length=255)
    x: float = 0.0
    y: float = 0.0
    z: float = 0.5
    yaw: float = 0.0
    color: str = "#3b82f6"


class VirtualPlayerOut(BaseModel):
    id: int
    lobby_id: int
    namespace: str
    name: str
    x: float
    y: float
    z: float
    yaw: float
    color: str
    created_at: datetime
    is_deleted: bool


class VirtualWorldOut(BaseModel):
    elements: list[VirtualWorldElementOut]
    players: list[VirtualPlayerOut]


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
async def register_user(
    payload: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    email = payload.email.lower()
    existing = await session.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")
    user = User(email=email, password_hash=hash_password(payload.password))
    session.add(user)
    await session.commit()
    await session.refresh(user)
    allow_stun_ip(_get_client_ip(request))
    return create_token_response(user)


@app.post("/api/auth/login", response_model=TokenResponse)
async def login_user(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    email = payload.email.lower()
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    allow_stun_ip(_get_client_ip(request))
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
        .options(
            selectinload(Lobby.owner),
            selectinload(Lobby.bots).selectinload(Bot.lobby),
            selectinload(Lobby.virtual_players),
        )
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


# -----------------------------------------------------------------------------
# Chat Message Endpoints
# -----------------------------------------------------------------------------


@app.get("/api/lobbies/{lobby_id}/messages")
async def get_lobby_messages(
    lobby_id: int,
    limit: int = 50,
    before: Optional[datetime] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Get chat messages for a lobby."""
    lobby = await session.get(Lobby, lobby_id)
    if not lobby or lobby.is_deleted:
        raise HTTPException(status_code=404, detail="Lobby not found")
    if not lobby.is_public and lobby.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Lobby is private")

    query = (
        select(ChatMessage)
        .where(ChatMessage.lobby_id == lobby_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(min(limit, 100))
    )
    if before:
        query = query.where(ChatMessage.created_at < before)

    result = await session.execute(query)
    messages = result.scalars().all()

    return {
        "items": [
            ChatMessageOut(
                id=msg.id,
                lobby_id=msg.lobby_id,
                user_id=msg.user_id,
                user_name=msg.user_name,
                message_type=msg.message_type,
                content=msg.content,
                created_at=msg.created_at,
            )
            for msg in reversed(messages)  # Return oldest first for display
        ]
    }


@app.post("/api/lobbies/{lobby_id}/messages", response_model=ChatMessageOut)
async def create_lobby_message(
    lobby_id: int,
    payload: ChatMessageCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ChatMessageOut:
    """Create a new chat message in a lobby."""
    lobby = await session.get(Lobby, lobby_id)
    if not lobby or lobby.is_deleted:
        raise HTTPException(status_code=404, detail="Lobby not found")
    if not lobby.is_public and lobby.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Lobby is private")

    message = ChatMessage(
        lobby_id=lobby_id,
        user_id=str(current_user.id),
        user_name=current_user.email,
        message_type="text",
        content=payload.content.strip(),
    )
    session.add(message)
    await session.commit()
    await session.refresh(message)

    # Broadcast to WebSocket subscribers
    await broadcast_chat_message(lobby_id, message)

    return ChatMessageOut(
        id=message.id,
        lobby_id=message.lobby_id,
        user_id=message.user_id,
        user_name=message.user_name,
        message_type=message.message_type,
        content=message.content,
        created_at=message.created_at,
    )


async def create_virtual_player_ws(lobby_id: int, name: str, color: str) -> Optional[VirtualPlayer]:
    """Create a virtual player via WebSocket request."""
    try:
        async with AsyncSessionLocal() as session:
            # Verify lobby exists
            lobby = await session.get(Lobby, lobby_id)
            if not lobby or lobby.is_deleted:
                return None

            # Generate unique namespace
            namespace = f"virtual_{secrets.token_hex(8)}"

            player = VirtualPlayer(
                lobby_id=lobby_id,
                namespace=namespace,
                name=name.strip() if name else "Player",
                x=0.0,
                y=0.0,
                z=0.5,
                yaw=0.0,
                color=color if color else "#3b82f6",
            )
            session.add(player)
            await session.commit()
            await session.refresh(player)
            return player
    except Exception as e:
        logger.error("Failed to create virtual player: %s", e)
        return None


async def delete_virtual_player_ws(namespace: str) -> Optional[int]:
    """Delete a virtual player via WebSocket request. Returns lobby_id if successful."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(VirtualPlayer)
                .where(VirtualPlayer.namespace == namespace)
                .where(VirtualPlayer.is_deleted.is_(False))
            )
            player = result.scalar_one_or_none()
            if not player:
                return None

            lobby_id = player.lobby_id
            player.is_deleted = True
            await session.commit()
            return lobby_id
    except Exception as e:
        logger.error("Failed to delete virtual player: %s", e)
        return None


async def notify_virtual_player_created(lobby_id: int, player: VirtualPlayer) -> None:
    """Notify connected Tauri hosts about a new virtual player."""
    message = {
        "type": "virtual_player_created",
        "lobby_id": lobby_id,
        "player": {
            "id": player.id,
            "namespace": player.namespace,
            "name": player.name,
            "x": player.x,
            "y": player.y,
            "z": player.z,
            "yaw": player.yaw,
            "color": player.color,
        },
    }
    async with lobby_status_lock:
        dead = []
        for ws in lobby_status_subscribers:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            lobby_status_subscribers.discard(ws)


async def notify_virtual_player_deleted(lobby_id: int, namespace: str) -> None:
    """Notify connected Tauri hosts about a deleted virtual player."""
    message = {
        "type": "virtual_player_deleted",
        "lobby_id": lobby_id,
        "namespace": namespace,
    }
    async with lobby_status_lock:
        dead = []
        for ws in lobby_status_subscribers:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            lobby_status_subscribers.discard(ws)


# -----------------------------------------------------------------------------
# Virtual World WebSocket Helper Functions
# -----------------------------------------------------------------------------


async def send_world_state(websocket: WebSocket, lobby_id: int) -> None:
    """Send full world state to a client."""
    async with AsyncSessionLocal() as session:
        elements_result = await session.execute(
            select(VirtualWorldElement)
            .where(VirtualWorldElement.lobby_id == lobby_id)
            .order_by(VirtualWorldElement.created_at)
        )
        elements = elements_result.scalars().all()

        players_result = await session.execute(
            select(VirtualPlayer)
            .where(VirtualPlayer.lobby_id == lobby_id)
            .where(VirtualPlayer.is_deleted.is_(False))
            .order_by(VirtualPlayer.created_at)
        )
        players = players_result.scalars().all()

    message = {
        "type": "world_state",
        "lobby_id": lobby_id,
        "elements": [
            {
                "id": e.id,
                "element_type": e.element_type,
                "x": e.x,
                "y": e.y,
                "z": e.z,
                "width": e.width,
                "height": e.height,
                "depth": e.depth,
                "rotation": e.rotation,
                "data": e.data,
            }
            for e in elements
        ],
        "players": [
            {
                "id": p.id,
                "namespace": p.namespace,
                "name": p.name,
                "x": p.x,
                "y": p.y,
                "z": p.z,
                "yaw": p.yaw,
                "color": p.color,
            }
            for p in players
        ],
    }
    await websocket.send_json(message)


async def create_virtual_element(lobby_id: int, message: dict) -> Optional[VirtualWorldElement]:
    """Create a new virtual world element from WebSocket message."""
    try:
        async with AsyncSessionLocal() as session:
            element = VirtualWorldElement(
                lobby_id=lobby_id,
                element_type=message.get("element_type", "wall"),
                x=float(message.get("x", 0)),
                y=float(message.get("y", 0)),
                z=float(message.get("z", 0)),
                width=message.get("width"),
                height=message.get("height"),
                depth=message.get("depth"),
                rotation=float(message.get("rotation", 0)),
                data=message.get("data"),
                created_by=message.get("created_by"),
            )
            session.add(element)
            await session.commit()
            await session.refresh(element)
            return element
    except Exception as e:
        logger.error("Failed to create virtual element: %s", e)
        return None


async def update_virtual_element(lobby_id: int, message: dict) -> Optional[VirtualWorldElement]:
    """Update an existing virtual world element."""
    element_id = message.get("element_id")
    if not element_id:
        return None

    try:
        async with AsyncSessionLocal() as session:
            element = await session.get(VirtualWorldElement, element_id)
            if not element or element.lobby_id != lobby_id:
                return None

            if "x" in message:
                element.x = float(message["x"])
            if "y" in message:
                element.y = float(message["y"])
            if "z" in message:
                element.z = float(message["z"])
            if "width" in message:
                element.width = message["width"]
            if "height" in message:
                element.height = message["height"]
            if "depth" in message:
                element.depth = message["depth"]
            if "rotation" in message:
                element.rotation = float(message["rotation"])
            if "data" in message:
                element.data = message["data"]

            await session.commit()
            await session.refresh(element)
            return element
    except Exception as e:
        logger.error("Failed to update virtual element: %s", e)
        return None


async def remove_virtual_element(lobby_id: int, element_id: int) -> bool:
    """Remove a virtual world element."""
    if not element_id:
        return False

    try:
        async with AsyncSessionLocal() as session:
            element = await session.get(VirtualWorldElement, element_id)
            if not element or element.lobby_id != lobby_id:
                return False

            await session.delete(element)
            await session.commit()
            return True
    except Exception as e:
        logger.error("Failed to remove virtual element: %s", e)
        return False


async def broadcast_world_element_change(lobby_id: int, change_type: str, element: VirtualWorldElement) -> None:
    """Broadcast element change to all lobby subscribers."""
    message = {
        "type": change_type,
        "lobby_id": lobby_id,
        "element": {
            "id": element.id,
            "element_type": element.element_type,
            "x": element.x,
            "y": element.y,
            "z": element.z,
            "width": element.width,
            "height": element.height,
            "depth": element.depth,
            "rotation": element.rotation,
            "data": element.data,
        },
    }
    async with lobby_status_lock:
        dead = []
        for ws in lobby_status_subscribers:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            lobby_status_subscribers.discard(ws)


async def broadcast_world_element_removed(lobby_id: int, element_id: int) -> None:
    """Broadcast element removal to all lobby subscribers."""
    message = {
        "type": "element_removed",
        "lobby_id": lobby_id,
        "element_id": element_id,
    }
    async with lobby_status_lock:
        dead = []
        for ws in lobby_status_subscribers:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            lobby_status_subscribers.discard(ws)


async def update_virtual_player_state(lobby_id: int, message: dict) -> None:
    """Update virtual player position and broadcast to subscribers."""
    namespace = message.get("namespace")
    if not namespace:
        return

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(VirtualPlayer)
                .where(VirtualPlayer.namespace == namespace)
                .where(VirtualPlayer.lobby_id == lobby_id)
                .where(VirtualPlayer.is_deleted.is_(False))
            )
            player = result.scalar_one_or_none()
            if not player:
                return

            if "x" in message:
                player.x = float(message["x"])
            if "y" in message:
                player.y = float(message["y"])
            if "z" in message:
                player.z = float(message["z"])
            if "yaw" in message:
                player.yaw = float(message["yaw"])

            await session.commit()

        # Broadcast player state to lobby subscribers
        state_message = {
            "type": "player_state",
            "lobby_id": lobby_id,
            "namespace": namespace,
            "x": message.get("x"),
            "y": message.get("y"),
            "z": message.get("z"),
            "yaw": message.get("yaw"),
        }
        async with lobby_status_lock:
            dead = []
            for ws in lobby_status_subscribers:
                try:
                    await ws.send_json(state_message)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                lobby_status_subscribers.discard(ws)
    except Exception as e:
        logger.error("Failed to update virtual player state: %s", e)


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
    existing = await session.execute(
        select(Bot).where(Bot.ros_namespace == normalized_namespace, Bot.lobby_id == lobby.id)
    )
    bot = existing.scalar_one_or_none()
    if bot and not bot.is_deleted:
        raise HTTPException(status_code=400, detail="ROS namespace already registered in this lobby")
    if bot and bot.is_deleted:
        bot.name = payload.name.strip()
        bot.description = payload.description
        bot.volume = payload.volume if payload.volume is not None else 1.0
        bot.lobby_id = lobby.id
        bot.is_deleted = False
    else:
        bot = Bot(
            name=payload.name.strip(),
            ros_namespace=normalized_namespace,
            description=payload.description,
            volume=payload.volume if payload.volume is not None else 1.0,
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
            existing = await session.execute(
                select(Bot).where(Bot.ros_namespace == new_ns, Bot.lobby_id == bot.lobby_id)
            )
            ns_bot = existing.scalar_one_or_none()
            if ns_bot and ns_bot.id != bot.id:
                raise HTTPException(status_code=400, detail="ROS namespace already registered in this lobby")
            bot.ros_namespace = new_ns
    if payload.volume is not None:
        bot.volume = max(0.0, min(1.0, payload.volume))
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
    active_bots = [bot for bot in bots if not getattr(bot, "is_deleted", False)]
    bot_count = len(active_bots)
    bot_namespaces = [bot.ros_namespace for bot in active_bots]
    is_owner = lobby.owner_id == current_user.id
    # Check if this lobby has an active ros-bridge command websocket connection
    ros_connected = lobby.id in connected_lobby_ids
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
        bot_namespaces=bot_namespaces,
        ros_connected=ros_connected,
    )


def lobby_detail_to_out(lobby: Lobby, current_user: User) -> LobbyDetailOut:
    base = lobby_to_out(lobby, current_user)
    bots = [bot_to_out(bot) for bot in getattr(lobby, "bots", []) if not bot.is_deleted]
    virtual_players = [
        virtual_player_to_out(vp)
        for vp in getattr(lobby, "virtual_players", [])
        if not vp.is_deleted
    ]
    data = base.model_dump()
    data["bots"] = bots
    data["virtual_players"] = virtual_players
    return LobbyDetailOut(**data)


def virtual_player_to_out(player: VirtualPlayer) -> VirtualPlayerOut:
    return VirtualPlayerOut(
        id=player.id,
        lobby_id=player.lobby_id,
        namespace=player.namespace,
        name=player.name,
        x=player.x,
        y=player.y,
        z=player.z,
        yaw=player.yaw,
        color=player.color,
        created_at=player.created_at,
        is_deleted=bool(player.is_deleted),
    )


def virtual_element_to_out(element: VirtualWorldElement) -> VirtualWorldElementOut:
    return VirtualWorldElementOut(
        id=element.id,
        lobby_id=element.lobby_id,
        element_type=element.element_type,
        x=element.x,
        y=element.y,
        z=element.z,
        width=element.width,
        height=element.height,
        depth=element.depth,
        rotation=element.rotation,
        data=element.data,
        created_at=element.created_at,
        created_by=element.created_by,
    )


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
        volume=bot.volume if bot.volume is not None else 1.0,
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
        bridge_websockets.discard(websocket)


async def notify_bridge_stream(robot_id: str, active: bool) -> None:
    """Send start_stream/stop_stream to all connected bridge websockets."""
    msg_type = "start_stream" if active else "stop_stream"
    payload = {"type": msg_type, "robot": robot_id}
    async with command_ws_lock:
        sockets = list(bridge_websockets)
    for ws in sockets:
        try:
            await ws.send_json(payload)
        except Exception:
            pass


async def broadcast_robot_command(command: RobotCommand) -> None:
    payload = {
        "type": "command",
        "robot": command.robot_namespace,
        "command": robot_command_to_out(command).model_dump(mode="json"),
    }
    logger.info("Broadcasting command %s to %s subscribers", command.id, command.robot_namespace)
    async with command_ws_lock:
        sockets = list(command_subscribers.get(command.robot_namespace, set()))
    for websocket in sockets:
        peer = websocket.client or ("unknown", 0)
        try:
            await websocket.send_json(payload)
        except RuntimeError:
            # websocket likely closed; cleanup asynchronously
            logger.warning("Websocket runtime error for %s:%s; unregistering", peer[0], peer[1])
            await unregister_robot_ws(websocket)
        except Exception as exc:
            logger.warning("Websocket send error for %s:%s: %s", peer[0], peer[1], exc)
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


async def require_internal_api_key(provided: str, session: Optional[AsyncSession] = None) -> int:
    """Validate lobby access key. Returns lobby_id."""
    provided_key = (provided or "").strip()
    if not provided_key:
        raise HTTPException(status_code=403, detail="missing lobby key")
    owns_session = False
    if session is None:
        session = AsyncSessionLocal()
        owns_session = True
    try:
        stmt = select(Lobby.id).where(Lobby.access_key == provided_key).where(Lobby.is_deleted.is_(False))
        result = await session.execute(stmt)
        lobby_id = result.scalar_one_or_none()
        if lobby_id is None:
            raise HTTPException(status_code=403, detail="invalid lobby key")
        return lobby_id
    finally:
        if owns_session and session is not None:
            await session.close()


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
                await conn.execute(
                    text(
                        "ALTER TABLE bots ADD COLUMN IF NOT EXISTS volume FLOAT NOT NULL DEFAULT 1.0"
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
            desired_key = entry.access_key or secrets.token_urlsafe(16)
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
            result = await session.execute(
                select(Bot).where(Bot.ros_namespace == namespace, Bot.lobby_id == lobby.id)
            )
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


async def ensure_default_lobby() -> None:
    """Create a default lobby if none exist, with a generated access key."""
    async with AsyncSessionLocal() as session:
        # Check if any non-deleted lobbies exist
        result = await session.execute(
            select(Lobby).where(Lobby.is_deleted.is_(False)).limit(1)
        )
        if result.scalar_one_or_none() is not None:
            return  # Lobbies exist, nothing to do

        # Need a user to own the default lobby
        user_result = await session.execute(select(User).limit(1))
        owner = user_result.scalar_one_or_none()
        if owner is None:
            logger.warning(
                "No users exist, cannot create default lobby. "
                "Create a user first or provide SEED_USERS_JSON."
            )
            return

        access_key = secrets.token_urlsafe(16)
        lobby = Lobby(
            name="default",
            description="Auto-created default lobby",
            access_key=access_key,
            owner_id=owner.id,
            ros_host="internal",
            ros_port=0,
            is_public=True,
        )
        session.add(lobby)
        await session.commit()
        logger.info("=" * 60)
        logger.info("Created default lobby with access key: %s", access_key)
        logger.info("Set LOBBY_KEY=%s on your robot/bridge to connect", access_key)
        logger.info("=" * 60)


@app.on_event("startup")
async def startup_event() -> None:
    await prepare_database()
    await apply_seed_data()
    await ensure_default_lobby()

    # Start STUN server on UDP 3478
    loop = asyncio.get_event_loop()
    try:
        transport, _protocol = await loop.create_datagram_endpoint(
            StunProtocol, local_addr=("0.0.0.0", 3478)
        )
        app.state.stun_transport = transport
        logger.info("STUN server listening on UDP 3478")
    except OSError as exc:
        logger.warning("Failed to start STUN server: %s", exc)
        app.state.stun_transport = None

    # Periodic whitelist cleanup
    async def _cleanup_loop() -> None:
        while True:
            await asyncio.sleep(300)
            _cleanup_stun_whitelist()

    app.state.stun_cleanup_task = asyncio.create_task(_cleanup_loop())


@app.on_event("shutdown")
async def shutdown_event() -> None:
    # Stop STUN server
    if hasattr(app.state, "stun_transport") and app.state.stun_transport:
        app.state.stun_transport.close()
    if hasattr(app.state, "stun_cleanup_task"):
        app.state.stun_cleanup_task.cancel()
    # Close all SFU peer connections
    for pc in robot_forwarder_pcs.values():
        await pc.close()
    robot_forwarder_pcs.clear()
    for pcs in robot_browser_pcs.values():
        for pc in pcs:
            await pc.close()
    robot_browser_pcs.clear()
    robot_incoming_tracks.clear()
    robot_incoming_audio_tracks.clear()


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

def _get_ice_servers() -> dict[str, Any]:
    """Return ICE server config pointing to our own STUN server."""
    stun_host = settings.stun_server
    if stun_host:
        return {"iceServers": [{"urls": f"stun:{stun_host}"}]}
    return {"iceServers": [{"urls": "stun:stun.l.google.com:19302"}]}


@app.get("/api/ice-servers")
async def get_ice_servers(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Return ICE server config; also whitelists the caller for STUN."""
    allow_stun_ip(_get_client_ip(request))
    return _get_ice_servers()


@app.get("/api/internal/ice-servers")
async def get_internal_ice_servers(
    request: Request,
    x_api_key: str = Header(default=""),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return ICE server config for internal bridge clients; whitelists the caller."""
    await require_internal_api_key(x_api_key, session)
    allow_stun_ip(_get_client_ip(request))
    return _get_ice_servers()


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


@app.post("/api/internal/telemetry/{robot_id}")
async def ingest_telemetry(
    robot_id: str,
    payload: TelemetryPayload,
    x_api_key: str = Header(default=""),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await require_internal_api_key(x_api_key, session)
    update_robot_heartbeat(robot_id)
    # Store latest telemetry
    telemetry_data = {
        "linear_speed": payload.linear_speed,
        "angular_speed": payload.angular_speed,
        "timestamp": payload.timestamp,
    }
    latest_telemetry[robot_id] = telemetry_data
    # Broadcast to subscribers
    await broadcast_telemetry(robot_id, telemetry_data)
    return {"robot": robot_id, "status": "ok"}


async def broadcast_telemetry(robot_id: str, data: Dict[str, Any]) -> None:
    """Broadcast telemetry to all subscribed websockets."""
    payload = {"type": "telemetry", "robot": robot_id, **data}
    async with telemetry_ws_lock:
        sockets = list(telemetry_subscribers.get(robot_id, set()))
    for websocket in sockets:
        try:
            await websocket.send_json(payload)
        except Exception:
            async with telemetry_ws_lock:
                telemetry_subscribers[robot_id].discard(websocket)




@app.websocket("/api/internal/ws/lobbies")
async def robot_command_bridge(websocket: WebSocket) -> None:
    api_key = websocket.query_params.get("api_key") or websocket.headers.get("x-api-key", "")
    async with AsyncSessionLocal() as session:
        lobby_id = await require_internal_api_key(api_key or "", session)
    await websocket.accept()
    peer = websocket.client or ("unknown", 0)
    # Use forwarded header if behind reverse proxy, else direct peer IP
    ws_forwarded = websocket.headers.get("x-forwarded-for")
    ws_client_ip = ws_forwarded.split(",")[0].strip() if ws_forwarded else peer[0]
    allow_stun_ip(ws_client_ip)
    logger.info("Command websocket connected from %s:%s (lobby_id=%s)", ws_client_ip, peer[1], lobby_id)
    # Mark lobby as online
    connected_lobby_ids.add(lobby_id)
    await broadcast_lobby_connection(lobby_id, True)
    try:
        while True:
            message = await websocket.receive_json()
            msg_type = message.get("type")
            if msg_type == "register_robots":
                robots = [value.strip() for value in message.get("robots", []) if value.strip()]
                async with command_ws_lock:
                    bridge_websockets.add(websocket)
                for robot in robots:
                    update_robot_heartbeat(robot)
                # Auto-create Bot entries for new robot namespaces (scoped to this lobby)
                if robots:
                    async with AsyncSessionLocal() as session:
                        for ns in robots:
                            existing = await session.execute(
                                select(Bot.id).where(
                                    Bot.ros_namespace == ns,
                                    Bot.lobby_id == lobby_id,
                                )
                            )
                            if existing.scalar_one_or_none() is None:
                                bot = Bot(
                                    name=ns.strip("/").replace("/", "_"),
                                    ros_namespace=ns,
                                    lobby_id=lobby_id,
                                )
                                session.add(bot)
                                logger.info("Auto-created bot '%s' in lobby %s", ns, lobby_id)
                            else:
                                # Un-delete if previously soft-deleted
                                await session.execute(
                                    Bot.__table__.update()
                                    .where(Bot.ros_namespace == ns)
                                    .where(Bot.lobby_id == lobby_id)
                                    .values(is_deleted=False)
                                )
                        await session.commit()
                # Tell the bridge which robots currently have active viewers
                for robot in robots:
                    if active_robot_streams.get(robot):
                        await websocket.send_json({"type": "start_stream", "robot": robot})
                await websocket.send_json({"type": "registered", "robots": robots})
                logger.info("Bridge %s:%s registered robots: %s", peer[0], peer[1], robots)
            elif msg_type == "subscribe":
                robots = [value.strip() for value in message.get("robots", []) if value.strip()]
                await register_robot_ws(websocket, robots)
                for robot in robots:
                    update_robot_heartbeat(robot)
                await send_pending_commands_to_connection(websocket, robots)
                await websocket.send_json({"type": "subscribed", "robots": robots})
                logger.info("Registered websocket %s:%s for robots: %s", peer[0], peer[1], robots)
            elif msg_type == "heartbeat":
                robots = [value.strip() for value in message.get("robots", []) if value.strip()]
                if not robots:
                    async with command_ws_lock:
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
            elif msg_type == "webrtc_offer":
                # Forwarder is sending us its Hop 1 offer
                await handle_forwarder_offer(websocket, message)
            # Virtual World message handlers
            elif msg_type == "register_virtual_players":
                # Tauri host registering virtual players (auto-create like robots)
                players_data = message.get("players", [])
                created_players = []
                async with AsyncSessionLocal() as session:
                    for p_data in players_data:
                        namespace = p_data.get("namespace", f"virtual_{secrets.token_hex(8)}")
                        name = p_data.get("name", namespace)
                        color = p_data.get("color", "#3b82f6")
                        # Check if already exists
                        existing = await session.execute(
                            select(VirtualPlayer.id).where(
                                VirtualPlayer.namespace == namespace,
                                VirtualPlayer.lobby_id == lobby_id,
                            )
                        )
                        if existing.scalar_one_or_none() is None:
                            player = VirtualPlayer(
                                lobby_id=lobby_id,
                                namespace=namespace,
                                name=name,
                                color=color,
                            )
                            session.add(player)
                            await session.flush()
                            await session.refresh(player)
                            created_players.append(player)
                            logger.info("Auto-created virtual player '%s' in lobby %s", namespace, lobby_id)
                        else:
                            # Un-delete if previously soft-deleted
                            await session.execute(
                                VirtualPlayer.__table__.update()
                                .where(VirtualPlayer.namespace == namespace)
                                .where(VirtualPlayer.lobby_id == lobby_id)
                                .values(is_deleted=False)
                            )
                    await session.commit()
                # Notify about created players
                for player in created_players:
                    await notify_virtual_player_created(lobby_id, player)
                await websocket.send_json({"type": "registered", "virtual_players": [p.namespace for p in created_players]})
            elif msg_type == "get_world_state":
                # Send full world state to the requesting client
                await send_world_state(websocket, lobby_id)
            elif msg_type == "add_element":
                # Tauri host adding a world element
                element = await create_virtual_element(lobby_id, message)
                if element:
                    await broadcast_world_element_change(lobby_id, "element_added", element)
                    await websocket.send_json({"type": "ack", "msg_type": "add_element", "element_id": element.id})
            elif msg_type == "update_element":
                # Tauri host updating a world element
                element = await update_virtual_element(lobby_id, message)
                if element:
                    await broadcast_world_element_change(lobby_id, "element_updated", element)
                    await websocket.send_json({"type": "ack", "msg_type": "update_element", "element_id": element.id})
                else:
                    await websocket.send_json({"type": "error", "error": "element not found"})
            elif msg_type == "remove_element":
                # Tauri host removing a world element
                element_id = message.get("element_id")
                if await remove_virtual_element(lobby_id, element_id):
                    await broadcast_world_element_removed(lobby_id, element_id)
                    await websocket.send_json({"type": "ack", "msg_type": "remove_element", "element_id": element_id})
                else:
                    await websocket.send_json({"type": "error", "error": "element not found"})
            elif msg_type == "player_state":
                # Virtual player position/state updates from Tauri host
                await update_virtual_player_state(lobby_id, message)
            else:
                await websocket.send_json({"type": "error", "error": "unknown message", "payload": message})
    except WebSocketDisconnect:
        pass
    finally:
        connected_lobby_ids.discard(lobby_id)
        await broadcast_lobby_connection(lobby_id, False)
        await unregister_robot_ws(websocket)


async def broadcast_lobby_connection(lobby_id: int, connected: bool) -> None:
    """Broadcast lobby connection status change to all subscribers."""
    message = {"type": "lobby_status", "lobby_id": lobby_id, "connected": connected}
    async with lobby_status_lock:
        dead = []
        for ws in lobby_status_subscribers:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            lobby_status_subscribers.discard(ws)


async def broadcast_lobby_status(robot: str, connected: bool) -> None:
    """Broadcast robot connection status change to all lobby subscribers."""
    message = {"type": "robot_status", "robot": robot, "connected": connected}
    async with lobby_status_lock:
        dead = []
        for ws in lobby_status_subscribers:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            lobby_status_subscribers.discard(ws)


async def broadcast_chat_message(lobby_id: int, message: "ChatMessage") -> None:
    """Broadcast a chat message to all subscribers of a lobby."""
    payload = {
        "type": "chat_message",
        "lobby_id": lobby_id,
        "message": {
            "id": message.id,
            "lobby_id": message.lobby_id,
            "user_id": message.user_id,
            "user_name": message.user_name,
            "message_type": message.message_type,
            "content": message.content,
            "created_at": message.created_at.isoformat(),
        },
    }
    async with chat_lock:
        dead = []
        for ws in chat_subscribers.get(lobby_id, set()):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            chat_subscribers[lobby_id].discard(ws)
        if not chat_subscribers.get(lobby_id):
            chat_subscribers.pop(lobby_id, None)


async def create_system_message(lobby_id: int, content: str) -> None:
    """Create and broadcast a system message for a lobby."""
    async with AsyncSessionLocal() as session:
        message = ChatMessage(
            lobby_id=lobby_id,
            user_id=None,
            user_name=None,
            message_type="system",
            content=content,
        )
        session.add(message)
        await session.commit()
        await session.refresh(message)
        await broadcast_chat_message(lobby_id, message)


async def get_lobby_id_for_robot(robot_namespace: str) -> Optional[int]:
    """Get the lobby_id for a robot by its namespace."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Bot.lobby_id)
            .where(Bot.ros_namespace == robot_namespace)
            .where(Bot.is_deleted.is_(False))
            .limit(1)
        )
        row = result.first()
        return row[0] if row else None


async def notify_robot_connected(robot_namespace: str) -> None:
    """Send a system message when a robot connects."""
    lobby_id = await get_lobby_id_for_robot(robot_namespace)
    if lobby_id:
        # Get bot name for a friendlier message
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Bot.name)
                .where(Bot.ros_namespace == robot_namespace)
                .where(Bot.is_deleted.is_(False))
                .limit(1)
            )
            row = result.first()
            bot_name = row[0] if row else robot_namespace
        await create_system_message(lobby_id, f"{bot_name} connected")


async def notify_robot_disconnected(robot_namespace: str) -> None:
    """Send a system message when a robot disconnects."""
    lobby_id = await get_lobby_id_for_robot(robot_namespace)
    if lobby_id:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Bot.name)
                .where(Bot.ros_namespace == robot_namespace)
                .where(Bot.is_deleted.is_(False))
                .limit(1)
            )
            row = result.first()
            bot_name = row[0] if row else robot_namespace
        await create_system_message(lobby_id, f"{bot_name} disconnected")


async def notify_streaming_started(robot_namespace: str, user_email: str) -> None:
    """Send a system message when a user starts streaming a robot."""
    lobby_id = await get_lobby_id_for_robot(robot_namespace)
    if lobby_id:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Bot.name)
                .where(Bot.ros_namespace == robot_namespace)
                .where(Bot.is_deleted.is_(False))
                .limit(1)
            )
            row = result.first()
            bot_name = row[0] if row else robot_namespace
        await create_system_message(lobby_id, f"{user_email} started streaming {bot_name}")


async def notify_streaming_stopped(robot_namespace: str, user_email: str) -> None:
    """Send a system message when a user stops streaming a robot."""
    lobby_id = await get_lobby_id_for_robot(robot_namespace)
    if lobby_id:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Bot.name)
                .where(Bot.ros_namespace == robot_namespace)
                .where(Bot.is_deleted.is_(False))
                .limit(1)
            )
            row = result.first()
            bot_name = row[0] if row else robot_namespace
        await create_system_message(lobby_id, f"{user_email} stopped streaming {bot_name}")


@app.websocket("/api/ws/lobby-status")
async def lobby_status_ws(websocket: WebSocket) -> None:
    """WebSocket for browser clients to receive lobby/robot status updates and chat."""
    await websocket.accept()
    async with lobby_status_lock:
        lobby_status_subscribers.add(websocket)
    # Send current connected lobbies and robots
    await websocket.send_json({"type": "connected_lobbies", "lobby_ids": list(connected_lobby_ids)})
    connected_robots = list(robot_forwarder_pcs.keys())
    await websocket.send_json({"type": "connected_robots", "robots": connected_robots})
    # Track which lobbies this websocket is subscribed to for chat
    subscribed_lobbies: set[int] = set()
    try:
        while True:
            text = await websocket.receive_text()
            try:
                data = json.loads(text)
                msg_type = data.get("type")
                lobby_id = data.get("lobby_id")

                if msg_type == "subscribe_chat" and lobby_id:
                    lobby_id = int(lobby_id)
                    async with chat_lock:
                        chat_subscribers[lobby_id].add(websocket)
                    subscribed_lobbies.add(lobby_id)
                    logger.debug("WS subscribed to chat for lobby %d", lobby_id)

                elif msg_type == "unsubscribe_chat" and lobby_id:
                    lobby_id = int(lobby_id)
                    async with chat_lock:
                        chat_subscribers[lobby_id].discard(websocket)
                        if not chat_subscribers[lobby_id]:
                            chat_subscribers.pop(lobby_id, None)
                    subscribed_lobbies.discard(lobby_id)
                    logger.debug("WS unsubscribed from chat for lobby %d", lobby_id)

                elif msg_type == "request_virtual_player" and lobby_id:
                    # Browser requesting a new virtual player
                    lobby_id = int(lobby_id)
                    name = data.get("name", "Player")
                    color = data.get("color", "#3b82f6")
                    player = await create_virtual_player_ws(lobby_id, name, color)
                    if player:
                        await notify_virtual_player_created(lobby_id, player)
                        logger.info("Created virtual player %s in lobby %d", player.namespace, lobby_id)
                    else:
                        await websocket.send_json({"type": "error", "error": "Failed to create virtual player"})

                elif msg_type == "delete_virtual_player":
                    # Browser requesting to delete a virtual player
                    namespace = data.get("namespace")
                    if namespace:
                        lobby_id = await delete_virtual_player_ws(namespace)
                        if lobby_id:
                            await notify_virtual_player_deleted(lobby_id, namespace)
                            logger.info("Deleted virtual player %s", namespace)
                        else:
                            await websocket.send_json({"type": "error", "error": "Virtual player not found"})

            except (json.JSONDecodeError, ValueError):
                pass  # Ignore invalid messages
    except WebSocketDisconnect:
        pass
    finally:
        async with lobby_status_lock:
            lobby_status_subscribers.discard(websocket)
        # Clean up chat subscriptions
        async with chat_lock:
            for lobby_id in subscribed_lobbies:
                chat_subscribers[lobby_id].discard(websocket)
                if not chat_subscribers[lobby_id]:
                    chat_subscribers.pop(lobby_id, None)


@app.websocket("/api/ws/{robot_id}")
async def websocket_proxy(websocket: WebSocket, robot_id: str) -> None:
    await websocket.accept()
    # Subscribe to telemetry for this robot
    async with telemetry_ws_lock:
        telemetry_subscribers[robot_id].add(websocket)
    # Send current telemetry state if available
    current_telemetry = latest_telemetry.get(robot_id)
    if current_telemetry:
        await websocket.send_json({
            "type": "telemetry",
            "robot": robot_id,
            **current_telemetry
        })
    else:
        await websocket.send_json({
            "type": "telemetry",
            "robot": robot_id,
            "linear_speed": 0.0,
            "angular_speed": 0.0,
            "message": "Waiting for robot telemetry..."
        })
    try:
        while True:
            data = await websocket.receive_text()
            logger.debug("Received WS payload for %s: %s", robot_id, data)
    except WebSocketDisconnect:
        logger.info("Client disconnected from %s WS", robot_id)
    finally:
        async with telemetry_ws_lock:
            telemetry_subscribers[robot_id].discard(websocket)
            if not telemetry_subscribers[robot_id]:
                telemetry_subscribers.pop(robot_id, None)


async def handle_forwarder_offer(ws: WebSocket, message: dict) -> None:
    """Hop 1: accept the forwarder's WebRTC offer, store the incoming track.

    Bidirectional: receives video + robot audio, sends browser audio.
    Also handles data channels for telemetry (receive) and control (send).
    """
    robot = (message.get("robot") or "").strip()
    sdp = message.get("sdp", "")
    offer_type = message.get("offer_type", "offer")

    if not robot or not sdp:
        await ws.send_json({"type": "webrtc_answer", "robot": robot, "error": "missing robot or sdp"})
        return

    # Tear down any existing Hop 1 PC for this robot (reconnect case)
    old_pc = robot_forwarder_pcs.pop(robot, None)
    if old_pc:
        await old_pc.close()
    robot_incoming_tracks.pop(robot, None)
    robot_incoming_audio_tracks.pop(robot, None)

    # Clean up old browser audio relay track
    old_relay = browser_audio_relay_tracks.pop(robot, None)
    if old_relay:
        old_relay.stop()

    # Clean up old data channels
    hop1_telemetry_channels.pop(robot, None)
    hop1_control_channels.pop(robot, None)
    hop1_map_channels.pop(robot, None)

    pc = RTCPeerConnection()
    robot_forwarder_pcs[robot] = pc
    asyncio.create_task(broadcast_lobby_status(robot, True))
    asyncio.create_task(notify_robot_connected(robot))

    @pc.on("track")
    def on_track(track: MediaStreamTrack) -> None:
        logger.info("SFU: received %s track from forwarder for %s", track.kind, robot)
        if track.kind == "video":
            robot_incoming_tracks[robot] = track
            logger.info("SFU: stored video track for %s (id=%s, type=%s, readyState=%s)",
                        robot, track.id, type(track).__name__, getattr(track, 'readyState', 'unknown'))
            evt = robot_track_ready.get(robot)
            if evt:
                evt.set()
        elif track.kind == "audio":
            # Wrap with Hop1AudioTrack to log RMS values
            wrapped_track = Hop1AudioTrack(track, robot)
            robot_incoming_audio_tracks[robot] = wrapped_track
            logger.info("SFU: stored audio track for %s (wrapped with Hop1AudioTrack)", robot)

            # Set audio track ready event
            audio_evt = robot_audio_track_ready.get(robot)
            if audio_evt:
                audio_evt.set()

    @pc.on("datachannel")
    def on_datachannel(channel: RTCDataChannel) -> None:
        """Handle incoming data channels from ros-bridge (telemetry, map)."""
        logger.info("SFU Hop1: received data channel '%s' from ros-bridge for %s", channel.label, robot)
        if channel.label == "telemetry":
            hop1_telemetry_channels[robot] = channel

            @channel.on("message")
            def on_telemetry_message(message: str) -> None:
                """Relay telemetry from ros-bridge to all connected browsers."""
                _relay_telemetry_to_browsers(robot, message)

            @channel.on("open")
            def on_open() -> None:
                logger.info("SFU Hop1: telemetry data channel open for %s", robot)

            @channel.on("close")
            def on_close() -> None:
                logger.info("SFU Hop1: telemetry data channel closed for %s", robot)
                hop1_telemetry_channels.pop(robot, None)

        elif channel.label == "map":
            hop1_map_channels[robot] = channel

            @channel.on("message")
            def on_map_message(message: str) -> None:
                """Relay map data from ros-bridge to all connected browsers."""
                _relay_map_to_browsers(robot, message)

            @channel.on("open")
            def on_map_open() -> None:
                logger.info("SFU Hop1: map data channel open for %s", robot)

            @channel.on("close")
            def on_map_close() -> None:
                logger.info("SFU Hop1: map data channel closed for %s", robot)
                hop1_map_channels.pop(robot, None)

    @pc.on("connectionstatechange")
    async def on_state() -> None:
        state = pc.connectionState
        logger.info("SFU Hop1 state for %s: %s", robot, state)
        if state in ("failed", "closed", "disconnected"):
            robot_incoming_tracks.pop(robot, None)
            robot_incoming_audio_tracks.pop(robot, None)
            robot_forwarder_pcs.pop(robot, None)
            asyncio.create_task(broadcast_lobby_status(robot, False))
            asyncio.create_task(notify_robot_disconnected(robot))
            # Clean up browser audio relay (browser->robot direction)
            relay = browser_audio_relay_tracks.pop(robot, None)
            if relay:
                relay.stop()
            # Clean up data channels
            hop1_telemetry_channels.pop(robot, None)
            hop1_control_channels.pop(robot, None)
            hop1_map_channels.pop(robot, None)
            hop2_telemetry_channels.pop(robot, None)
            hop2_control_channels.pop(robot, None)
            hop2_map_channels.pop(robot, None)
            # Clean up group audio mixer and control aggregator for this robot
            mixer = group_audio_mixers.pop(robot, None)
            agg = control_aggregators.pop(robot, None)
            if agg:
                agg.stop()
            # Close all Hop 2 PCs for this robot
            browser_pcs = robot_browser_pcs.pop(robot, [])
            for bpc in browser_pcs:
                await bpc.close()
            await pc.close()

    # First set remote description to know what transceivers are available
    offer = RTCSessionDescription(sdp=sdp, type=offer_type)

    await pc.setRemoteDescription(offer)

    # Create browser audio relay track to send browser mic audio to ros-bridge
    browser_relay = BrowserAudioRelayTrack(robot)
    browser_audio_relay_tracks[robot] = browser_relay

    # Add browser audio relay as a new track
    # This creates a new m-line in the answer for sending audio to ros-bridge
    pc.addTrack(browser_relay)
    logger.info("SFU Hop1: added browser audio relay for %s", robot)

    # Create control data channel to send commands to ros-bridge
    control_channel = pc.createDataChannel("control", ordered=False)
    hop1_control_channels[robot] = control_channel

    @control_channel.on("open")
    def on_control_open() -> None:
        logger.info("SFU Hop1: control data channel open for %s", robot)

    @control_channel.on("close")
    def on_control_close() -> None:
        logger.info("SFU Hop1: control data channel closed for %s", robot)
        hop1_control_channels.pop(robot, None)

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    await ws.send_json({
        "type": "webrtc_answer",
        "robot": robot,
        "sdp": pc.localDescription.sdp,
        "answer_type": pc.localDescription.type,
    })
    logger.info("SFU: sent Hop1 answer for %s", robot)


@app.post("/api/robots/{robot_id}/webrtc")
async def start_webrtc(
    robot_id: str,
    offer: WebRTCOffer,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """Hop 2: relay the forwarder's track to this browser viewer.

    Bidirectional: sends video + robot audio, receives browser audio.
    Also handles data channels for telemetry (send) and control (receive).
    Supports renegotiation (e.g., when browser adds mic track).
    """
    # Check if this is a renegotiation (user already has a PC for this robot)
    user_pc_key = f"{robot_id}:{current_user.email}"
    existing_pc = browser_user_pcs.get(user_pc_key)
    logger.info("SFU Hop2: offer for %s (user=%s, existing_pc=%s, state=%s)",
                robot_id, current_user.email, existing_pc is not None,
                existing_pc.connectionState if existing_pc else "none")

    # Only renegotiate if PC is fully connected (not disconnected/connecting)
    if existing_pc and existing_pc.connectionState == "connected":
        # Renegotiation: reuse existing PC
        logger.info("SFU Hop2: renegotiating for %s (user=%s)", robot_id, current_user.email)
        pc = existing_pc
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type=offer.type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

    # Clean up existing PC if it exists but isn't connected
    if existing_pc:
        logger.info("SFU Hop2: closing stale PC for %s (state=%s)", user_pc_key, existing_pc.connectionState)
        browser_user_pcs.pop(user_pc_key, None)
        pcs = robot_browser_pcs.get(robot_id, [])
        if existing_pc in pcs:
            pcs.remove(existing_pc)
        await existing_pc.close()

    # Track active viewers and trigger stream start if first viewer
    was_empty = not active_robot_streams.get(robot_id)
    active_robot_streams[robot_id].add(current_user.email)
    asyncio.create_task(notify_streaming_started(robot_id, current_user.email))
    if was_empty:
        await notify_bridge_stream(robot_id, True)

    # Wait for forwarder track if not yet available
    incoming_track = robot_incoming_tracks.get(robot_id)
    if not incoming_track:
        evt = robot_track_ready.setdefault(robot_id, asyncio.Event())
        evt.clear()
        try:
            await asyncio.wait_for(evt.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=503, detail="Video stream not available yet")
        incoming_track = robot_incoming_tracks.get(robot_id)
        if not incoming_track:
            raise HTTPException(status_code=503, detail="Video stream not available")

    # Create a relayed copy of the video track for this browser (no re-encoding)
    relayed_video_track = media_relay.subscribe(incoming_track)

    # Use MediaRelay for audio (same as video)
    incoming_audio_track = robot_incoming_audio_tracks.get(robot_id)
    relayed_audio_track = None
    if incoming_audio_track:
        relayed_audio_track = media_relay.subscribe(incoming_audio_track)
        logger.info("SFU Hop2: subscribed audio via MediaRelay for %s", robot_id)
    else:
        logger.warning("SFU Hop2: no audio track for %s", robot_id)

    pc = RTCPeerConnection()
    robot_browser_pcs[robot_id].append(pc)
    browser_user_pcs[user_pc_key] = pc  # Track PC by user for renegotiation
    logger.info("SFU Hop2: stored PC for key=%s, total_user_pcs=%d", user_pc_key, len(browser_user_pcs))

    @pc.on("track")
    def on_browser_track(track: MediaStreamTrack) -> None:
        """Handle incoming audio track from browser mic."""
        if track.kind == "audio":
            logger.info("SFU Hop2: received browser audio track for %s (user=%s)", robot_id, current_user.email)
            # Cancel any existing forwarding task for this user
            old_task = browser_audio_forward_tasks.pop(user_pc_key, None)
            if old_task and not old_task.done():
                logger.info("SFU Hop2: cancelling old audio forward task for %s", user_pc_key)
                old_task.cancel()
            # Start new forwarding task (pass user_key for group audio)
            task = asyncio.ensure_future(_forward_browser_audio(robot_id, track, user_pc_key))
            browser_audio_forward_tasks[user_pc_key] = task

    @pc.on("datachannel")
    def on_browser_datachannel(channel: RTCDataChannel) -> None:
        """Handle incoming data channels from browser (control commands)."""
        logger.info("SFU Hop2: received data channel '%s' from browser for %s (user=%s)",
                    channel.label, robot_id, current_user.email)
        if channel.label == "control":
            hop2_control_channels[robot_id].append(channel)
            # Get control aggregator for multi-user command averaging
            aggregator = get_or_create_control_aggregator(robot_id)

            @channel.on("message")
            def on_control_message(message: str) -> None:
                """Handle control messages: joystick commands and audio routing."""
                try:
                    data = json.loads(message)
                    if data.get("type") == "audio_routing":
                        # Update audio routing preferences for this user
                        user_audio_routing[user_pc_key] = {
                            "to_group": data.get("to_group", False),
                            "to_robot": data.get("to_robot", False),
                        }
                        logger.info("Audio routing for %s: to_group=%s, to_robot=%s",
                                   user_pc_key, data.get("to_group"), data.get("to_robot"))
                        return
                except json.JSONDecodeError:
                    pass
                # Otherwise it's a joystick command
                aggregator.push_command(user_pc_key, message)

            @channel.on("open")
            def on_open() -> None:
                logger.info("SFU Hop2: control data channel open for %s (user=%s)", robot_id, current_user.email)

            @channel.on("close")
            def on_close() -> None:
                logger.info("SFU Hop2: control data channel closed for %s (user=%s)", robot_id, current_user.email)
                if channel in hop2_control_channels.get(robot_id, []):
                    hop2_control_channels[robot_id].remove(channel)
                # Remove user from aggregator
                aggregator.remove_user(user_pc_key)

    @pc.on("connectionstatechange")
    async def on_state() -> None:
        state = pc.connectionState
        logger.info("SFU Hop2 state for %s: %s (pcs=%d, viewers=%s)",
                    robot_id, state, len(robot_browser_pcs.get(robot_id, [])),
                    active_robot_streams.get(robot_id))
        if state in ("failed", "closed", "disconnected"):
            pcs = robot_browser_pcs.get(robot_id, [])
            if pc in pcs:
                pcs.remove(pc)
                logger.info("SFU Hop2: removed pc, remaining=%d", len(pcs))
            # Clean up user PC tracking and audio forward task
            browser_user_pcs.pop(user_pc_key, None)
            old_task = browser_audio_forward_tasks.pop(user_pc_key, None)
            if old_task and not old_task.done():
                old_task.cancel()
            # Note: data channels are cleaned up in their on_close handlers
            await pc.close()
            # Clean up user from group audio mixer and control aggregator
            mixer = group_audio_mixers.get(robot_id)
            if mixer:
                mixer.remove_user(user_pc_key)
            agg = control_aggregators.get(robot_id)
            if agg:
                agg.remove_user(user_pc_key)
            # Release the streaming lock for this user immediately
            active_robot_streams[robot_id].discard(current_user.email)
            asyncio.create_task(notify_streaming_stopped(robot_id, current_user.email))
            logger.info("SFU Hop2: released stream lock for %s (user=%s), remaining viewers=%s",
                        robot_id, current_user.email, active_robot_streams.get(robot_id))
            # Stop robot stream if no more viewers
            remaining = robot_browser_pcs.get(robot_id)
            if not remaining:
                logger.info("SFU Hop2: no viewers for %s, stopping robot stream", robot_id)
                await notify_bridge_stream(robot_id, False)

    pc.addTrack(relayed_video_track)

    # Add mixed audio track (robot audio + group audio from other users)
    # This combines both into a single track to avoid SDP negotiation issues
    if relayed_audio_track:
        group_mixer = get_or_create_group_mixer(robot_id)
        mixed_audio_track = MixedAudioTrack(relayed_audio_track, group_mixer, user_pc_key)
        pc.addTrack(mixed_audio_track)
        logger.info("SFU Hop2: added mixed audio track for %s (user=%s)", robot_id, current_user.email)
    else:
        logger.warning("SFU Hop2: no audio track for %s", robot_id)

    # Create telemetry data channel to send to browser
    telemetry_channel = pc.createDataChannel("telemetry", ordered=False)
    hop2_telemetry_channels[robot_id].append(telemetry_channel)

    @telemetry_channel.on("open")
    def on_telemetry_open() -> None:
        logger.info("SFU Hop2: telemetry data channel open for %s (user=%s)", robot_id, current_user.email)

    @telemetry_channel.on("close")
    def on_telemetry_close() -> None:
        logger.info("SFU Hop2: telemetry data channel closed for %s (user=%s)", robot_id, current_user.email)
        if telemetry_channel in hop2_telemetry_channels.get(robot_id, []):
            hop2_telemetry_channels[robot_id].remove(telemetry_channel)

    # Create map data channel to send SLAM minimap to browser
    map_channel = pc.createDataChannel("map", ordered=False)
    hop2_map_channels[robot_id].append(map_channel)

    @map_channel.on("open")
    def on_map_open() -> None:
        logger.info("SFU Hop2: map data channel open for %s (user=%s)", robot_id, current_user.email)

    @map_channel.on("close")
    def on_map_close() -> None:
        logger.info("SFU Hop2: map data channel closed for %s (user=%s)", robot_id, current_user.email)
        if map_channel in hop2_map_channels.get(robot_id, []):
            hop2_map_channels[robot_id].remove(map_channel)

    browser_offer = RTCSessionDescription(sdp=offer.sdp, type=offer.type)
    await pc.setRemoteDescription(browser_offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


def _relay_telemetry_to_browsers(robot_id: str, message: str) -> None:
    """Relay telemetry from ros-bridge to all connected browsers via data channels."""
    channels = hop2_telemetry_channels.get(robot_id, [])
    if not channels:
        return
    # Log occasionally
    if not hasattr(_relay_telemetry_to_browsers, "_count"):
        _relay_telemetry_to_browsers._count = {}
    _relay_telemetry_to_browsers._count[robot_id] = _relay_telemetry_to_browsers._count.get(robot_id, 0) + 1
    if _relay_telemetry_to_browsers._count[robot_id] % 100 == 1:
        logger.info("DC relay telemetry %d for %s to %d browsers",
                    _relay_telemetry_to_browsers._count[robot_id], robot_id, len(channels))
    # Send to all browser channels
    for channel in channels:
        if channel.readyState == "open":
            try:
                channel.send(message)
            except Exception as e:
                logger.debug("Failed to relay telemetry to browser for %s: %s", robot_id, e)


def _relay_map_to_browsers(robot_id: str, message: str) -> None:
    """Relay map data from ros-bridge to all connected browsers via data channels."""
    channels = hop2_map_channels.get(robot_id, [])
    if not channels:
        return
    # Log occasionally
    if not hasattr(_relay_map_to_browsers, "_count"):
        _relay_map_to_browsers._count = {}
    _relay_map_to_browsers._count[robot_id] = _relay_map_to_browsers._count.get(robot_id, 0) + 1
    if _relay_map_to_browsers._count[robot_id] % 10 == 1:
        logger.info("DC relay map %d for %s to %d browsers",
                    _relay_map_to_browsers._count[robot_id], robot_id, len(channels))
    # Send to all browser channels
    for channel in channels:
        if channel.readyState == "open":
            try:
                channel.send(message)
            except Exception as e:
                logger.debug("Failed to relay map to browser for %s: %s", robot_id, e)


def _relay_control_to_robot(robot_id: str, message: str) -> None:
    """Relay control command from browser to ros-bridge via data channel."""
    channel = hop1_control_channels.get(robot_id)
    if not channel or channel.readyState != "open":
        logger.debug("No control channel for %s, dropping command", robot_id)
        return
    try:
        channel.send(message)
        # Log occasionally
        if not hasattr(_relay_control_to_robot, "_count"):
            _relay_control_to_robot._count = {}
        _relay_control_to_robot._count[robot_id] = _relay_control_to_robot._count.get(robot_id, 0) + 1
        if _relay_control_to_robot._count[robot_id] % 50 == 1:
            logger.info("DC relay control %d for %s", _relay_control_to_robot._count[robot_id], robot_id)
    except Exception as e:
        logger.warning("Failed to relay control to robot %s: %s", robot_id, e)


async def _forward_browser_audio(robot_id: str, track: MediaStreamTrack, user_key: str = None) -> None:
    """Forward audio frames from browser to the robot and group mixer."""
    relay = browser_audio_relay_tracks.get(robot_id)
    if not relay:
        logger.warning("No browser audio relay track for %s", robot_id)
        return

    # Get or create group mixer for this robot
    mixer = get_or_create_group_mixer(robot_id)

    logger.info("Starting browser audio forwarding for %s (user=%s)", robot_id, user_key)
    frame_count = 0
    try:
        while True:
            frame = await track.recv()
            frame_count += 1

            # Log RMS for first few frames and periodically to trace audio levels
            if frame_count <= 5 or frame_count % 100 == 0:
                try:
                    arr = frame.to_ndarray()
                    rms = np.sqrt(np.mean(arr.astype(np.float32)**2))
                    logger.info("_forward_browser_audio frame %d for %s: dtype=%s, shape=%s, rms=%.1f, samples=%d",
                                frame_count, robot_id, arr.dtype, arr.shape, rms, frame.samples)
                except Exception:
                    pass

            # Check routing preferences for this user
            routing = user_audio_routing.get(user_key, {"to_group": False, "to_robot": False})

            # Forward to robot if enabled
            if routing.get("to_robot", False):
                relay.push_frame(frame)

            # Push to group mixer for other users to hear if enabled
            if user_key and routing.get("to_group", False):
                mixer.push_frame(user_key, frame)
    except Exception as e:
        logger.info("Browser audio forwarding ended for %s (user=%s): %s", robot_id, user_key, e)
        # Clean up user from mixer and routing when they disconnect
        if user_key:
            mixer.remove_user(user_key)
            user_audio_routing.pop(user_key, None)
