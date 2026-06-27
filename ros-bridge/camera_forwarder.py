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
from typing import Dict, Optional, Set

import numpy as np
import requests
import rclpy
import websocket
from aiortc import RTCConfiguration, RTCDataChannel, RTCIceServer, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, AudioStreamTrack
from av import VideoFrame, AudioFrame
from sensor_msgs.msg import Joy
from rclpy.node import Node
from rclpy.publisher import Publisher
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from rclpy.subscription import Subscription
from sensor_msgs.msg import Image
from std_msgs.msg import String, UInt8MultiArray, Int32MultiArray
from nav_msgs.msg import OccupancyGrid
import base64


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

                # Create interleaved stereo to work around Opus sample duplication
                # Same fix as browser->robot direction
                chunk_interleaved = np.empty(len(samples) * 2, dtype=np.int16)
                chunk_interleaved[0::2] = samples  # Left channel
                chunk_interleaved[1::2] = samples  # Right channel (same data)

                # Create AudioFrame with interleaved stereo
                frame = AudioFrame.from_ndarray(
                    chunk_interleaved.reshape(1, -1), format="s16", layout="stereo"
                )
                frame.sample_rate = self.sample_rate
                frame.pts = self._pts
                self._pts += self.SAMPLES_PER_FRAME
                self._frames_sent += 1
                if self._frames_sent <= 3 or self._frames_sent % 500 == 0:
                    rms = np.sqrt(np.mean(samples.astype(np.float32)**2))
                    qsize = self._queue.qsize()
                    print(f"[RosAudioTrack] frame {self._frames_sent} for {self.robot_id}, rms={rms:.1f}, qsize={qsize}, stereo_samples={len(chunk_interleaved)}", flush=True)
                return frame
            except queue.Empty:
                await asyncio.sleep(0.01)  # Shorter sleep for lower latency


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
        self.joy_publishers: Dict[str, Publisher] = {}
        self.ptz_publishers: Dict[str, Publisher] = {}
        # Current camera position per robot (for delta-based control)
        self.camera_positions: Dict[str, Dict[str, int]] = {}

        # Audio handling
        self.audio_subscriptions: Dict[str, Subscription] = {}
        self.audio_publishers: Dict[str, Publisher] = {}
        self.audio_streaming_robots: Set[str] = set()

        # Map handling
        self.map_subscriptions: Dict[str, Subscription] = {}
        self._map_channels: Dict[str, RTCDataChannel] = {}

        # SFU: one track and one PC per robot (forwarder -> server)
        self._video_tracks: Dict[str, RosVideoTrack] = {}
        self._audio_tracks: Dict[str, RosAudioTrack] = {}
        self._peer_connections: Dict[str, RTCPeerConnection] = {}
        self._ice_servers: list[RTCIceServer] = list(_DEFAULT_ICE_SERVERS)
        self._ice_servers_fetched = False

        # Data channels for control and telemetry
        self._telemetry_channels: Dict[str, RTCDataChannel] = {}
        self._control_channels: Dict[str, RTCDataChannel] = {}

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
            joy_topic = f"/{robot}/joy"
            self.joy_publishers[robot] = self.create_publisher(Joy, joy_topic, 10)
            ptz_topic = f"/{robot}/camera_ptz"
            self.ptz_publishers[robot] = self.create_publisher(Int32MultiArray, ptz_topic, 10)
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
            pub = self.joy_publishers.pop(robot, None)
            if pub:
                self.destroy_publisher(pub)
            ptz_pub = self.ptz_publishers.pop(robot, None)
            if ptz_pub:
                self.destroy_publisher(ptz_pub)
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

        # Subscribe to map topic for SLAM minimap
        map_topic = f"/{robot}/map"
        self.map_subscriptions[robot] = self.create_subscription(
            OccupancyGrid,
            map_topic,
            lambda msg, ns=robot: self._handle_map(ns, msg),
            10,
        )

        if not self._ice_servers_fetched:
            self._fetch_ice_servers()

        asyncio.run_coroutine_threadsafe(
            self._create_forwarder_offer(robot, video_track, audio_track), self._aio_loop
        )
        self.get_logger().info(f"Started streaming {topic} + {audio_topic} + {map_topic}")

    def _send_reset_commands(self, robot: str) -> None:
        """Send reset commands to stop robot movement and center camera when streaming stops."""
        # Send zero joy command to stop movement
        joy_pub = self.joy_publishers.get(robot)
        if joy_pub:
            joy_msg = Joy()
            joy_msg.header.stamp = self.get_clock().now().to_msg()
            joy_msg.axes = [0.0] * 6  # All axes to zero
            joy_msg.buttons = [0] * 12  # All buttons released
            joy_pub.publish(joy_msg)
            self.get_logger().info(f"Sent reset joy command for {robot}")

        # Send center PTZ command and reset internal tracking
        ptz_pub = self.ptz_publishers.get(robot)
        if ptz_pub:
            ptz_msg = Int32MultiArray()
            ptz_msg.data = [90, 90]  # Center position
            ptz_pub.publish(ptz_msg)
            # Reset internal position tracking
            self.camera_positions[robot] = {"pan": 90, "tilt": 90}
            self.get_logger().info(f"Sent reset PTZ command for {robot}")

    def _stop_streaming(self, robot: str) -> None:
        if robot not in self.streaming_robots:
            return

        # Send reset commands to stop the robot when all streams stop
        self._send_reset_commands(robot)

        sub = self.camera_subscriptions.pop(robot, None)
        if sub:
            self.destroy_subscription(sub)
        audio_sub = self.audio_subscriptions.pop(robot, None)
        if audio_sub:
            self.destroy_subscription(audio_sub)
        map_sub = self.map_subscriptions.pop(robot, None)
        if map_sub:
            self.destroy_subscription(map_sub)
        self.streaming_robots.discard(robot)
        pc = self._peer_connections.pop(robot, None)
        if pc:
            asyncio.run_coroutine_threadsafe(pc.close(), self._aio_loop)
        self._video_tracks.pop(robot, None)
        self._audio_tracks.pop(robot, None)
        # Clean up data channels
        self._telemetry_channels.pop(robot, None)
        self._control_channels.pop(robot, None)
        self._map_channels.pop(robot, None)
        self.get_logger().info(f"Stopped streaming /{robot}/camera/image_raw + audio + map")

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

    def _handle_map(self, robot_id: str, msg: OccupancyGrid) -> None:
        """Send map data via data channel for minimap display."""
        channel = self._map_channels.get(robot_id)
        if not channel or channel.readyState != "open":
            return

        # OccupancyGrid.data is int8[] with values -1 (unknown), 0 (free), 100 (occupied)
        # Convert to uint8: -1 -> 0, 0 -> 1, 100 -> 101 (add 1 to shift range)
        map_bytes = bytes([(v + 1) & 0xFF for v in msg.data])
        map_b64 = base64.b64encode(map_bytes).decode('ascii')

        payload = {
            "type": "map_update",
            "width": msg.info.width,
            "height": msg.info.height,
            "resolution": msg.info.resolution,
            "origin_x": msg.info.origin.position.x,
            "origin_y": msg.info.origin.position.y,
            "data": map_b64,
        }

        try:
            channel.send(json.dumps(payload))
            # Log occasionally
            if not hasattr(self, '_map_count'):
                self._map_count = {}
            self._map_count[robot_id] = self._map_count.get(robot_id, 0) + 1
            if self._map_count[robot_id] % 10 == 1:
                self.get_logger().info(
                    f"Map {self._map_count[robot_id]} for {robot_id}: "
                    f"{msg.info.width}x{msg.info.height}, {len(map_b64)} bytes"
                )
        except Exception as e:
            self.get_logger().debug(f"Map channel send failed for {robot_id}: {e}")

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
        Also creates telemetry data channel and handles incoming control data channel.
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
                # Clean up data channels
                self._telemetry_channels.pop(robot, None)
                self._control_channels.pop(robot, None)
                self._map_channels.pop(robot, None)
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

        @pc.on("datachannel")
        def _on_datachannel(channel: RTCDataChannel) -> None:
            """Handle incoming data channel from API (control commands)."""
            self.get_logger().info(f"Received data channel '{channel.label}' for {robot}")
            if channel.label == "control":
                self._control_channels[robot] = channel

                @channel.on("message")
                def on_control_message(message: str) -> None:
                    """Handle control commands from browser via data channel."""
                    self._handle_control_message(robot, message)

                @channel.on("open")
                def on_open() -> None:
                    self.get_logger().info(f"Control data channel open for {robot}")

                @channel.on("close")
                def on_close() -> None:
                    self.get_logger().info(f"Control data channel closed for {robot}")
                    self._control_channels.pop(robot, None)

        # Send video and robot audio
        pc.addTrack(video_track)
        if audio_track:
            # Add audio track with sendrecv direction (send robot audio, receive browser audio)
            transceiver = pc.addTransceiver(audio_track, direction="sendrecv")
            self.get_logger().info(f"Added audio transceiver for {robot}: direction={transceiver.direction}")

        # Create telemetry data channel (robot -> browser, unordered for low latency)
        telemetry_channel = pc.createDataChannel("telemetry", ordered=False)
        self._telemetry_channels[robot] = telemetry_channel

        @telemetry_channel.on("open")
        def _on_telemetry_open() -> None:
            self.get_logger().info(f"Telemetry data channel open for {robot}")

        @telemetry_channel.on("close")
        def _on_telemetry_close() -> None:
            self.get_logger().info(f"Telemetry data channel closed for {robot}")
            self._telemetry_channels.pop(robot, None)

        # Create map data channel (robot -> browser, for SLAM minimap)
        map_channel = pc.createDataChannel("map", ordered=False)
        self._map_channels[robot] = map_channel

        @map_channel.on("open")
        def _on_map_open() -> None:
            self.get_logger().info(f"Map data channel open for {robot}")

        @map_channel.on("close")
        def _on_map_close() -> None:
            self.get_logger().info(f"Map data channel closed for {robot}")
            self._map_channels.pop(robot, None)

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

                # Debug: log format and RMS on first few frames and periodically
                if frame_count <= 5 or frame_count % 50 == 0:
                    rms = np.sqrt(np.mean(samples.astype(np.float32)**2))
                    self.get_logger().info(
                        f"Browser audio format for {robot}: dtype={samples.dtype}, shape={samples.shape}, "
                        f"rms={rms:.1f}, min={samples.min()}, max={samples.max()}, first_samples={samples.flatten()[:10].tolist()}"
                    )

                # Convert float to int16 if needed
                if samples.dtype != np.int16:
                    samples = (samples * 32767).astype(np.int16)

                # Flatten array
                samples = samples.flatten()

                # Handle interleaved stereo: extract left channel (every other sample)
                # API sends interleaved stereo (L,R,L,R) to work around Opus duplication
                if len(samples) == 1920:
                    samples = samples[::2]  # Extract left channel (960 samples)

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

    # ── Control message handling (via data channel) ─────────────────

    def _handle_control_message(self, robot_id: str, message: str) -> None:
        """Handle control commands received via data channel.

        Publishes Joy messages directly to /{robot}/joy topic.
        The robot-side controller handles velocity ramping/decay.
        """
        try:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "joy":
                publisher = self.joy_publishers.get(robot_id)
                if not publisher:
                    self.get_logger().warning(f"No joy publisher for {robot_id}")
                    return

                # Create and publish Joy message
                joy_msg = Joy()
                joy_msg.header.stamp = self.get_clock().now().to_msg()
                joy_msg.axes = [float(a) for a in data.get("axes", [])]
                joy_msg.buttons = [int(b) for b in data.get("buttons", [])]
                publisher.publish(joy_msg)

                # Log occasionally
                if not hasattr(self, "_dc_cmd_count"):
                    self._dc_cmd_count = {}
                self._dc_cmd_count[robot_id] = self._dc_cmd_count.get(robot_id, 0) + 1
                if self._dc_cmd_count[robot_id] % 50 == 1:
                    axes = joy_msg.axes
                    self.get_logger().info(
                        f"DC joy {self._dc_cmd_count[robot_id]} for {robot_id}: "
                        f"axes[1]={axes[1] if len(axes) > 1 else 0:.2f} "
                        f"axes[3]={axes[3] if len(axes) > 3 else 0:.2f}"
                    )

            elif msg_type == "camera_ptz":
                publisher = self.ptz_publishers.get(robot_id)
                if not publisher:
                    self.get_logger().warning(f"No ptz publisher for {robot_id}")
                    return

                # Initialize camera position if not exists
                if robot_id not in self.camera_positions:
                    self.camera_positions[robot_id] = {"pan": 90, "tilt": 90}
                pos = self.camera_positions[robot_id]

                # Apply deltas if provided (new delta-based control)
                if "pan_delta" in data or "tilt_delta" in data:
                    pan_delta = int(data.get("pan_delta", 0))
                    tilt_delta = int(data.get("tilt_delta", 0))
                    pos["pan"] = max(0, min(180, pos["pan"] + pan_delta))
                    pos["tilt"] = max(0, min(180, pos["tilt"] + tilt_delta))
                else:
                    # Legacy absolute position (backward compatibility)
                    pos["pan"] = int(data.get("pan", pos["pan"]))
                    pos["tilt"] = int(data.get("tilt", pos["tilt"]))

                # Create and publish Int32MultiArray message [pan, tilt]
                ptz_msg = Int32MultiArray()
                ptz_msg.data = [pos["pan"], pos["tilt"]]
                publisher.publish(ptz_msg)

                # Log occasionally
                if not hasattr(self, "_dc_ptz_count"):
                    self._dc_ptz_count = {}
                self._dc_ptz_count[robot_id] = self._dc_ptz_count.get(robot_id, 0) + 1
                if self._dc_ptz_count[robot_id] % 20 == 1:
                    self.get_logger().info(
                        f"DC ptz {self._dc_ptz_count[robot_id]} for {robot_id}: "
                        f"pan={ptz_msg.data[0]}, tilt={ptz_msg.data[1]}"
                    )

        except (json.JSONDecodeError, ValueError) as exc:
            self.get_logger().warning(f"Invalid control message for {robot_id}: {exc}")

    # ── Telemetry push ───────────────────────────────────────────────

    def _handle_telemetry(self, robot_id: str, msg: String) -> None:
        """Forward telemetry data via data channel only."""
        # Only send via data channel - no HTTP fallback
        channel = self._telemetry_channels.get(robot_id)
        if not channel or channel.readyState != "open":
            return  # Drop silently if no channel

        try:
            payload = json.loads(msg.data)
        except json.JSONDecodeError as exc:
            self.get_logger().debug(f"Invalid telemetry JSON for {robot_id}: {exc}")
            return

        try:
            # Add type field for data channel protocol
            dc_payload = {"type": "telemetry", **payload}
            channel.send(json.dumps(dc_payload))
            # Log occasionally
            if not hasattr(self, "_dc_telemetry_count"):
                self._dc_telemetry_count = {}
            self._dc_telemetry_count[robot_id] = self._dc_telemetry_count.get(robot_id, 0) + 1
            if self._dc_telemetry_count[robot_id] % 100 == 1:
                self.get_logger().info(
                    f"DC telemetry {self._dc_telemetry_count[robot_id]} for {robot_id}"
                )
        except Exception as exc:
            self.get_logger().debug(f"Data channel send failed for {robot_id}: {exc}")

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

    # ── Command forwarding (legacy WebSocket path - deprecated) ─────

    def flush_command_queue(self) -> None:
        """Flush legacy command queue. Commands now use data channels."""
        while not self.command_queue.empty():
            try:
                payload = self.command_queue.get_nowait()
            except queue.Empty:
                break
            # Legacy cmd_vel commands are no longer supported
            # Commands should come via data channel as joy messages
            robot = payload.get("robot")
            command = payload.get("command") or {}
            self.get_logger().debug(
                f"Ignoring legacy WS command for {robot}: {command}"
            )
            command_id = command.get("id")
            if command_id is None:
                continue
            self._send_ws_message(
                {
                    "type": "complete",
                    "robot": robot,
                    "command_id": command_id,
                    "status": "deprecated",
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
