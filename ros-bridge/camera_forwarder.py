from __future__ import annotations

import asyncio
import fractions
import json
import os
import queue
import re
import threading
import time
import urllib.parse
from typing import Dict, Set

import numpy as np
import requests
import rclpy
import websocket
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, AudioStreamTrack
from av import VideoFrame, AudioFrame
from geometry_msgs.msg import Twist
from rclpy.node import Node
from rclpy.publisher import Publisher
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from rclpy.subscription import Subscription
from sensor_msgs.msg import Image
from std_msgs.msg import String, UInt8MultiArray


_CAMERA_TOPIC_RE = re.compile(r"^/([^/]+)/camera/image_raw$")
_TELEMETRY_TOPIC_RE = re.compile(r"^/([^/]+)/telemetry$")
_AUDIO_RAW_TOPIC_RE = re.compile(r"^/([^/]+)/audio_raw$")

# QoS for audio - BEST_EFFORT for low latency
_AUDIO_QOS = QoSProfile(
    reliability=ReliabilityPolicy.BEST_EFFORT,
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
)
_DISCOVERY_INTERVAL = 3.0

# QoS for camera - RELIABLE to match usb_cam publisher
_CAMERA_QOS = QoSProfile(
    reliability=ReliabilityPolicy.RELIABLE,
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
)

# For local dev, empty ICE servers forces host-only candidates (no STUN)
# Set USE_STUN=1 environment variable to enable STUN for production
_DEFAULT_ICE_SERVERS = [
    RTCIceServer(urls="stun:stun.l.google.com:19302"),
] if os.getenv("USE_STUN") else []


def _convert_image_to_bgra(width: int, height: int, encoding: str, payload: bytes) -> np.ndarray:
    """Convert a ROS Image payload to a BGRA numpy array."""
    if width <= 0 or height <= 0:
        raise ValueError("frame dimensions must be positive")
    encoding_normalized = (encoding or "bgra8").strip().lower()
    pixel_count = width * height
    buffer = np.frombuffer(payload, dtype=np.uint8)
    if buffer.size % pixel_count != 0:
        raise ValueError("payload size does not align with frame dimensions")

    def _with_alpha(channels: np.ndarray) -> np.ndarray:
        alpha = np.full((height, width, 1), 255, dtype=np.uint8)
        return np.concatenate((channels, alpha), axis=2)

    if encoding_normalized == "bgra8":
        return buffer.reshape((height, width, 4))
    if encoding_normalized == "rgba8":
        rgba = buffer.reshape((height, width, 4))
        return rgba[..., [2, 1, 0, 3]]
    if encoding_normalized in {"bgr8", "rgb8"}:
        channels = buffer.reshape((height, width, 3))
        if encoding_normalized == "rgb8":
            channels = channels[..., ::-1]
        return _with_alpha(channels)
    if encoding_normalized in {"mono8", "8uc1"}:
        gray = buffer.reshape((height, width, 1))
        mono = np.repeat(gray, 3, axis=2)
        return _with_alpha(mono)

    channel_count = buffer.size // pixel_count
    if channel_count == 4:
        return buffer.reshape((height, width, 4))
    if channel_count == 3:
        channels = buffer.reshape((height, width, 3))
        return _with_alpha(channels)
    if channel_count == 1:
        gray = buffer.reshape((height, width, 1))
        mono = np.repeat(gray, 3, axis=2)
        return _with_alpha(mono)

    raise ValueError(f"unsupported encoding '{encoding_normalized}' with {channel_count} channels")


class RosVideoTrack(VideoStreamTrack):
    """Bridges ROS2 Image callbacks to aiortc VideoFrames.

    One track per robot, feeding the single forwarder->server PC.
    Uses threading-safe queue since frames arrive from the ROS thread.
    """

    kind = "video"

    def __init__(self, robot_id: str) -> None:
        super().__init__()
        self.robot_id = robot_id
        self._queue: queue.Queue[VideoFrame] = queue.Queue(maxsize=1)

    def push_frame(self, frame: VideoFrame) -> None:
        """Called from the ROS callback thread. Thread-safe."""
        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            pass

    async def recv(self) -> VideoFrame:
        while True:
            try:
                frame = self._queue.get_nowait()
                frame.pts, frame.time_base = await self.next_timestamp()
                return frame
            except queue.Empty:
                await asyncio.sleep(0.01)


class RosAudioTrack(AudioStreamTrack):
    """Bridges ROS2 audio callbacks to aiortc AudioFrames.

    Receives raw PCM audio (48kHz mono S16_LE) from ROS and converts to WebRTC audio.
    Splits into 20ms chunks (960 samples) for Opus compatibility.
    """

    kind = "audio"
    SAMPLES_PER_FRAME = 960  # 20ms at 48kHz

    def __init__(self, robot_id: str, sample_rate: int = 48000, channels: int = 1) -> None:
        super().__init__()
        self.robot_id = robot_id
        self.sample_rate = sample_rate
        self.channels = channels
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=50)
        self._buffer = np.array([], dtype=np.int16)
        self._pts = 0
        self._frames_sent = 0

    def push_audio(self, data: bytes) -> None:
        """Called from the ROS callback thread with raw PCM bytes. Thread-safe."""
        try:
            samples = np.frombuffer(data, dtype=np.int16)
            # Append to buffer
            self._buffer = np.concatenate([self._buffer, samples])
            # Extract 20ms chunks
            while len(self._buffer) >= self.SAMPLES_PER_FRAME:
                chunk = self._buffer[:self.SAMPLES_PER_FRAME]
                self._buffer = self._buffer[self.SAMPLES_PER_FRAME:]
                # Drop old if queue full
                if self._queue.qsize() >= 45:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        pass
                self._queue.put_nowait(chunk)
        except Exception as e:
            print(f"[RosAudioTrack] push_audio error: {e}", flush=True)

    async def recv(self) -> AudioFrame:
        if self._frames_sent == 0:
            print(f"[RosAudioTrack] recv() started for {self.robot_id}", flush=True)
        while True:
            try:
                samples = self._queue.get_nowait()
                # Reshape for mono (1 channel)
                samples = samples.reshape(1, -1)
                # Create AudioFrame
                frame = AudioFrame.from_ndarray(samples, format="s16", layout="mono")
                frame.sample_rate = self.sample_rate
                frame.pts = self._pts
                self._pts += self.SAMPLES_PER_FRAME
                self._frames_sent += 1
                if self._frames_sent <= 3 or self._frames_sent % 500 == 0:
                    rms = np.sqrt(np.mean(samples.astype(np.float32)**2))
                    print(f"[RosAudioTrack] frame {self._frames_sent} for {self.robot_id}, rms={rms:.1f}", flush=True)
                return frame
            except queue.Empty:
                await asyncio.sleep(0.02)
            except Exception as e:
                print(f"[RosAudioTrack] recv error: {e}", flush=True)
                await asyncio.sleep(0.1)


class RobotBridgeNode(Node):
    def __init__(self, aio_loop: asyncio.AbstractEventLoop) -> None:
        super().__init__("robot_bridge")
        self._aio_loop = aio_loop

        api_base = os.getenv("API_BASE_URL", "http://robot-gateway:8080/api").rstrip("/")
        self.api_base = api_base
        self.telemetry_base = f"{api_base}/internal/telemetry"
        self.api_key = os.getenv("LOBBY_KEY", "")
        self.headers = {"x-api-key": self.api_key} if self.api_key else {}
        self.http_session = requests.Session()
        self.heartbeat_interval = float(os.getenv("COMMAND_HEARTBEAT_INTERVAL", "5"))

        self.command_queue: queue.Queue[Dict] = queue.Queue()
        self.ws_lock = threading.Lock()
        self.ws_app: websocket.WebSocketApp | None = None

        # Discovered robots and their ROS resources
        self.discovered_robots: Set[str] = set()
        self.streaming_robots: Set[str] = set()
        self.camera_subscriptions: Dict[str, Subscription] = {}
        self.telemetry_subscriptions: Dict[str, Subscription] = {}
        self.command_publishers: Dict[str, Publisher] = {}

        # Audio handling
        self.audio_subscriptions: Dict[str, Subscription] = {}
        self.audio_publishers: Dict[str, Publisher] = {}
        self.audio_streaming_robots: Set[str] = set()

        # SFU: one track and one PC per robot (forwarder -> server)
        self._video_tracks: Dict[str, RosVideoTrack] = {}
        self._audio_tracks: Dict[str, RosAudioTrack] = {}
        self._peer_connections: Dict[str, RTCPeerConnection] = {}
        self._ice_servers: list[RTCIceServer] = list(_DEFAULT_ICE_SERVERS)
        self._ice_servers_fetched = False

        # Timers
        self.create_timer(0.1, self.flush_command_queue)
        self.create_timer(self.heartbeat_interval, self.send_heartbeats)
        self.create_timer(_DISCOVERY_INTERVAL, self._discover_robots)

        # WebSocket thread for commands + stream control + signaling
        self.ws_thread = threading.Thread(target=self._run_command_socket, daemon=True)
        self.ws_thread.start()

        self.get_logger().info("RobotBridge started — discovering robots dynamically")

    # ── Topic discovery ──────────────────────────────────────────────

    def _discover_robots(self) -> None:
        topic_names_and_types = self.get_topic_names_and_types()
        found: Set[str] = set()
        for topic_name, _types in topic_names_and_types:
            m = _CAMERA_TOPIC_RE.match(topic_name)
            if m:
                found.add(m.group(1))

        new_robots = found - self.discovered_robots
        gone_robots = self.discovered_robots - found

        for robot in new_robots:
            self.get_logger().info(f"Discovered robot: {robot}")
            cmd_topic = f"/{robot}/cmd_vel"
            self.command_publishers[robot] = self.create_publisher(Twist, cmd_topic, 10)
            telemetry_topic = f"/{robot}/telemetry"
            self.telemetry_subscriptions[robot] = self.create_subscription(
                String,
                telemetry_topic,
                lambda msg, ns=robot: self._handle_telemetry(ns, msg),
                10,
            )
            # Create audio_play publisher for this robot
            audio_play_topic = f"/{robot}/audio_play"
            self.audio_publishers[robot] = self.create_publisher(
                UInt8MultiArray, audio_play_topic, 10
            )

        for robot in gone_robots:
            self.get_logger().info(f"Robot disappeared: {robot}")
            self._stop_streaming(robot)
            # Audio is now stopped via _stop_streaming
            pub = self.command_publishers.pop(robot, None)
            if pub:
                self.destroy_publisher(pub)
            tel_sub = self.telemetry_subscriptions.pop(robot, None)
            if tel_sub:
                self.destroy_subscription(tel_sub)
            audio_pub = self.audio_publishers.pop(robot, None)
            if audio_pub:
                self.destroy_publisher(audio_pub)

        if new_robots or gone_robots:
            self.discovered_robots = found
            self._send_ws_message({
                "type": "register_robots",
                "robots": sorted(self.discovered_robots),
            })

    # ── Stream control ───────────────────────────────────────────────

    def _start_streaming(self, robot: str) -> None:
        if robot not in self.discovered_robots:
            self.get_logger().warning(f"Cannot stream unknown robot: {robot}")
            return
        if robot in self.streaming_robots:
            return

        # Subscribe to camera topic
        topic = f"/{robot}/camera/image_raw"
        self.camera_subscriptions[robot] = self.create_subscription(
            Image,
            topic,
            lambda msg, ns=robot: self._handle_frame(ns, msg),
            _CAMERA_QOS,
        )
        self.streaming_robots.add(robot)

        # Create video track
        video_track = RosVideoTrack(robot)
        self._video_tracks[robot] = video_track

        # Create audio track and subscribe to audio topic
        audio_track = RosAudioTrack(robot, sample_rate=48000, channels=1)
        self._audio_tracks[robot] = audio_track
        audio_topic = f"/{robot}/audio_raw"
        self.audio_subscriptions[robot] = self.create_subscription(
            UInt8MultiArray,
            audio_topic,
            lambda msg, ns=robot: self._handle_audio_webrtc(ns, msg),
            _AUDIO_QOS,
        )

        if not self._ice_servers_fetched:
            self._fetch_ice_servers()

        asyncio.run_coroutine_threadsafe(
            self._create_forwarder_offer(robot, video_track, audio_track), self._aio_loop
        )
        self.get_logger().info(f"Started streaming {topic} + {audio_topic}")

    def _stop_streaming(self, robot: str) -> None:
        if robot not in self.streaming_robots:
            return
        sub = self.camera_subscriptions.pop(robot, None)
        if sub:
            self.destroy_subscription(sub)
        audio_sub = self.audio_subscriptions.pop(robot, None)
        if audio_sub:
            self.destroy_subscription(audio_sub)
        self.streaming_robots.discard(robot)
        pc = self._peer_connections.pop(robot, None)
        if pc:
            asyncio.run_coroutine_threadsafe(pc.close(), self._aio_loop)
        self._video_tracks.pop(robot, None)
        self._audio_tracks.pop(robot, None)
        self.get_logger().info(f"Stopped streaming /{robot}/camera/image_raw + audio")

    def _handle_audio_webrtc(self, robot_id: str, msg: UInt8MultiArray) -> None:
        """Push incoming robot audio to the WebRTC audio track."""
        track = self._audio_tracks.get(robot_id)
        if track:
            # Debug: log audio reception every ~100 messages (~10 seconds at 10Hz)
            if not hasattr(self, '_audio_count'):
                self._audio_count = {}
            self._audio_count[robot_id] = self._audio_count.get(robot_id, 0) + 1
            if self._audio_count[robot_id] % 100 == 1:
                self.get_logger().info(
                    f"Audio {self._audio_count[robot_id]} for {robot_id}: {len(msg.data)} bytes"
                )
            track.push_audio(bytes(msg.data))

    # ── Frame handling ───────────────────────────────────────────────

    def _handle_frame(self, robot_id: str, msg: Image) -> None:
        track = self._video_tracks.get(robot_id)
        if not track:
            return
        raw = bytes(msg.data)
        encoding = msg.encoding or "bgra8"

        # Debug: log frame info every 30 frames
        if not hasattr(self, '_frame_count'):
            self._frame_count = {}
        self._frame_count[robot_id] = self._frame_count.get(robot_id, 0) + 1
        if self._frame_count[robot_id] % 30 == 1:
            self.get_logger().info(
                f"Frame {self._frame_count[robot_id]} for {robot_id}: "
                f"{msg.width}x{msg.height} {encoding}, {len(raw)} bytes"
            )

        try:
            array = _convert_image_to_bgra(msg.width, msg.height, encoding, raw)
        except ValueError as e:
            self.get_logger().warning(f"Frame conversion error for {robot_id}: {e}")
            return
        frame = VideoFrame.from_ndarray(array, format="bgra")
        track.push_frame(frame)

    # ── ICE server config ────────────────────────────────────────────

    def _fetch_ice_servers(self) -> list[RTCIceServer]:
        """Fetch ICE server config from the API."""
        # Skip fetching if USE_STUN is not set (local dev mode)
        if not os.getenv("USE_STUN"):
            self.get_logger().info("Local dev mode: using host-only ICE (no STUN)")
            self._ice_servers_fetched = True
            return []

        url = f"{self.api_base}/internal/ice-servers"
        try:
            resp = self.http_session.get(url, headers=self.headers, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            servers = []
            for entry in data.get("iceServers", []):
                urls = entry.get("urls")
                if isinstance(urls, str):
                    urls = [urls]
                kwargs: dict = {"urls": urls}
                if entry.get("username"):
                    kwargs["username"] = entry["username"]
                if entry.get("credential"):
                    kwargs["credential"] = entry["credential"]
                servers.append(RTCIceServer(**kwargs))
            if servers:
                self._ice_servers = servers
                self._ice_servers_fetched = True
                self.get_logger().info(f"Fetched ICE servers: {len(servers)} entries")
            return servers
        except Exception as exc:
            self.get_logger().warning(f"Failed to fetch ICE servers: {exc}")
            return list(_DEFAULT_ICE_SERVERS)

    # ── WebRTC: forwarder <-> server (Hop 1, bidirectional) ──────────

    async def _create_forwarder_offer(
        self, robot: str, video_track: RosVideoTrack, audio_track: RosAudioTrack | None = None
    ) -> None:
        """Create a PC, add video and audio tracks, send offer to the server.

        Bidirectional: sends video + robot audio, receives browser audio.
        """
        ice_config = RTCConfiguration(iceServers=list(self._ice_servers))
        pc = RTCPeerConnection(configuration=ice_config)
        self._peer_connections[robot] = pc

        @pc.on("connectionstatechange")
        async def _on_state_change() -> None:
            state = pc.connectionState
            self.get_logger().info(f"SFU Hop1 state for {robot}: {state}")
            if state in {"failed", "closed", "disconnected"}:
                self._peer_connections.pop(robot, None)
                self._video_tracks.pop(robot, None)
                self._audio_tracks.pop(robot, None)
                # Critical: remove from streaming_robots so we can restart streaming
                self.streaming_robots.discard(robot)
                self.get_logger().info(f"Cleaned up streaming state for {robot}")
                await pc.close()

        @pc.on("track")
        def _on_track(track) -> None:
            """Handle incoming audio track from browser (via API relay)."""
            if track.kind == "audio":
                self.get_logger().info(f"Received browser audio track for {robot}")
                # Start a task to consume audio and publish to ROS
                asyncio.ensure_future(self._consume_browser_audio(robot, track))

        # Send video and robot audio
        pc.addTrack(video_track)
        if audio_track:
            # Add audio track with sendrecv direction (send robot audio, receive browser audio)
            transceiver = pc.addTransceiver(audio_track, direction="sendrecv")
            self.get_logger().info(f"Added audio transceiver for {robot}: direction={transceiver.direction}")

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        self._send_ws_message({
            "type": "webrtc_offer",
            "robot": robot,
            "sdp": pc.localDescription.sdp,
            "offer_type": pc.localDescription.type,
        })
        self.get_logger().info(f"Sent SFU offer to server for {robot}")

    def _handle_webrtc_answer(self, payload: dict) -> None:
        """Handle the server's answer to our offer."""
        robot = (payload.get("robot") or "").strip()
        sdp = payload.get("sdp", "")
        answer_type = payload.get("answer_type", "answer")

        if not robot or not sdp:
            error = payload.get("error", "unknown")
            self.get_logger().warning(f"WebRTC answer error for {robot}: {error}")
            return

        pc = self._peer_connections.get(robot)
        if not pc:
            self.get_logger().warning(f"No PC found for WebRTC answer for {robot}")
            return

        self.get_logger().info(f"Received SFU answer for {robot}, setting remote description")
        asyncio.run_coroutine_threadsafe(
            pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=answer_type)),
            self._aio_loop,
        )

    async def _consume_browser_audio(self, robot: str, track) -> None:
        """Consume audio frames from browser and publish to ROS audio_play topic."""
        publisher = self.audio_publishers.get(robot)
        if not publisher:
            self.get_logger().warning(f"No audio publisher for {robot}")
            return

        self.get_logger().info(f"Starting browser audio consumer for {robot}")
        frame_count = 0
        try:
            while True:
                frame = await track.recv()
                frame_count += 1

                # Convert AudioFrame to raw PCM bytes (S16_LE)
                samples = frame.to_ndarray()

                # Debug: log format on first few frames
                if frame_count <= 3:
                    self.get_logger().info(
                        f"Browser audio format for {robot}: dtype={samples.dtype}, shape={samples.shape}, "
                        f"min={samples.min():.2f}, max={samples.max():.2f}"
                    )

                # Convert float to int16 if needed
                if samples.dtype != np.int16:
                    samples = (samples * 32767).astype(np.int16)

                # Flatten to mono if stereo
                if samples.ndim > 1:
                    samples = samples[0]

                # Convert to bytes and publish
                audio_bytes = samples.tobytes()
                msg = UInt8MultiArray()
                msg.data = list(audio_bytes)
                publisher.publish(msg)

                # Log every 100 frames (~2 seconds at 50fps)
                if frame_count % 100 == 1:
                    self.get_logger().info(
                        f"Browser audio frame {frame_count} for {robot}: {len(audio_bytes)} bytes"
                    )
        except Exception as e:
            self.get_logger().info(f"Browser audio consumer ended for {robot}: {e}")

    # ── Telemetry push ───────────────────────────────────────────────

    def _handle_telemetry(self, robot_id: str, msg: String) -> None:
        """Forward telemetry data to the API."""
        url = f"{self.telemetry_base}/{robot_id}"
        try:
            payload = json.loads(msg.data)
            resp = self.http_session.post(url, json=payload, headers=self.headers, timeout=2)
            resp.raise_for_status()
        except (json.JSONDecodeError, requests.RequestException) as exc:
            self.get_logger().debug(f"Failed to push telemetry for {robot_id}: {exc}")

    # ── WebSocket ────────────────────────────────────────────────────

    def _build_command_ws_url(self) -> str:
        parsed = urllib.parse.urlparse(self.api_base)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        path = parsed.path.rstrip("/") + "/internal/ws/lobbies"
        query = ""
        if self.api_key:
            query = urllib.parse.urlencode({"api_key": self.api_key})
        return urllib.parse.urlunparse((scheme, parsed.netloc, path, "", query, ""))

    def _run_command_socket(self) -> None:
        websocket.enableTrace(False)
        while True:
            url = self._build_command_ws_url()
            self.get_logger().info(f"Connecting to command websocket at {url}")
            ws_app = websocket.WebSocketApp(
                url,
                on_open=self._ws_on_open,
                on_message=self._ws_on_message,
                on_close=self._ws_on_close,
                on_error=self._ws_on_error,
            )
            with self.ws_lock:
                self.ws_app = ws_app
            try:
                ws_app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                self.get_logger().warning(f"Command websocket error: {exc}")
            finally:
                with self.ws_lock:
                    self.ws_app = None
            time.sleep(2.0)

    def _ws_on_open(self, ws: websocket.WebSocketApp) -> None:
        self.get_logger().info("Command websocket connected")
        robots = sorted(self.discovered_robots)
        if robots:
            self._send_ws_message({"type": "register_robots", "robots": robots})
            self._send_ws_message({"type": "subscribe", "robots": robots})

    def _ws_on_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            self.get_logger().warning("Received invalid JSON on command websocket")
            return

        msg_type = payload.get("type")

        if msg_type == "command":
            robot = payload.get("robot")
            cmd = payload.get("command") or {}
            linear_x = float(cmd.get("linear_x", 0.0))
            angular_z = float(cmd.get("angular_z", 0.0))
            self.get_logger().info(
                f"Queued command {cmd.get('id')} for {robot} (lin_x={linear_x:.3f} ang_z={angular_z:.3f})"
            )
            self.command_queue.put(payload)

        elif msg_type == "start_stream":
            robot = (payload.get("robot") or "").strip()
            if robot:
                self.get_logger().info(f"Server requested stream start for {robot}")
                self._start_streaming(robot)
                # Audio is now integrated into _start_streaming via WebRTC

        elif msg_type == "stop_stream":
            robot = (payload.get("robot") or "").strip()
            if robot:
                self.get_logger().info(f"Server requested stream stop for {robot}")
                self._stop_streaming(robot)
                # Audio is now stopped via _stop_streaming

        elif msg_type == "webrtc_answer":
            self.get_logger().info(f"Received WebRTC answer for {payload.get('robot')}")
            self._handle_webrtc_answer(payload)

    def _ws_on_close(self, ws: websocket.WebSocketApp, close_status_code, close_msg) -> None:
        self.get_logger().warning(f"Command websocket closed: {close_status_code} {close_msg}")

    def _ws_on_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        self.get_logger().warning(f"Command websocket error: {error}")

    def _send_ws_message(self, payload: Dict) -> None:
        message = json.dumps(payload)
        with self.ws_lock:
            ws = self.ws_app
        if ws is None:
            return
        try:
            ws.send(message)
        except Exception as exc:
            self.get_logger().debug(f"Failed to send command websocket message: {exc}")

    # ── Command forwarding ───────────────────────────────────────────

    def flush_command_queue(self) -> None:
        while not self.command_queue.empty():
            try:
                payload = self.command_queue.get_nowait()
            except queue.Empty:
                break
            robot = payload.get("robot")
            command = payload.get("command") or {}
            publisher = self.command_publishers.get(robot or "")
            if not publisher:
                continue
            twist = Twist()
            twist.linear.x = float(command.get("linear_x", 0.0))
            twist.linear.y = float(command.get("linear_y", 0.0))
            twist.linear.z = float(command.get("linear_z", 0.0))
            twist.angular.x = float(command.get("angular_x", 0.0))
            twist.angular.y = float(command.get("angular_y", 0.0))
            twist.angular.z = float(command.get("angular_z", 0.0))
            publisher.publish(twist)
            command_id = command.get("id")
            if command_id is None:
                continue
            self._send_ws_message(
                {
                    "type": "complete",
                    "robot": robot,
                    "command_id": command_id,
                    "status": "delivered",
                }
            )

    def send_heartbeats(self) -> None:
        robots = sorted(self.discovered_robots)
        if robots:
            self._send_ws_message({"type": "heartbeat", "robots": robots})


def _run_asyncio_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Run the asyncio event loop in a background thread."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


def main() -> None:
    # Start asyncio event loop in a background thread (for aiortc)
    aio_loop = asyncio.new_event_loop()
    aio_thread = threading.Thread(target=_run_asyncio_loop, args=(aio_loop,), daemon=True)
    aio_thread.start()

    rclpy.init()
    node = RobotBridgeNode(aio_loop)
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
        aio_loop.call_soon_threadsafe(aio_loop.stop)


if __name__ == "__main__":
    main()
