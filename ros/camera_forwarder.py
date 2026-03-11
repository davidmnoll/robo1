from __future__ import annotations

import asyncio
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
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame
from geometry_msgs.msg import Twist
from rclpy.node import Node
from rclpy.publisher import Publisher
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from rclpy.subscription import Subscription
from sensor_msgs.msg import Image
from std_msgs.msg import String


_CAMERA_TOPIC_RE = re.compile(r"^/([^/]+)/camera/image_raw$")
_TELEMETRY_TOPIC_RE = re.compile(r"^/([^/]+)/telemetry$")
_DISCOVERY_INTERVAL = 3.0

# QoS for camera - BEST_EFFORT to match publisher (drop frames rather than queue)
_CAMERA_QOS = QoSProfile(
    reliability=ReliabilityPolicy.BEST_EFFORT,
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
)

_DEFAULT_ICE_SERVERS = [
    RTCIceServer(urls="stun:stun.l.google.com:19302"),
]


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
    """Bridges ROS2 Image callbacks to aiortc VideoFrames."""

    kind = "video"

    def __init__(self, robot_id: str, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self.robot_id = robot_id
        self._loop = loop
        self._queue: asyncio.Queue[VideoFrame] = asyncio.Queue(maxsize=1)

    def push_frame(self, width: int, height: int, encoding: str, data: bytes) -> None:
        """Called from the ROS callback thread. Converts and enqueues a frame."""
        try:
            array = _convert_image_to_bgra(width, height, encoding, data)
        except ValueError:
            return
        frame = VideoFrame.from_ndarray(array, format="bgra")
        # Drop old frame if queue is full (keep latest only)
        try:
            self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            pass

    async def recv(self) -> VideoFrame:
        frame = await self._queue.get()
        frame.pts, frame.time_base = await self.next_timestamp()
        return frame


class RobotBridgeNode(Node):
    def __init__(self, aio_loop: asyncio.AbstractEventLoop) -> None:
        super().__init__("robot_bridge")
        self._aio_loop = aio_loop

        api_base = os.getenv("API_BASE_URL", "http://robot-gateway:8080/api").rstrip("/")
        self.api_base = api_base
        self.telemetry_base = f"{api_base}/internal/telemetry"
        self.api_key = os.getenv("LOBBY_KEY") or os.getenv("ROS_PUSH_KEY", "")
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

        # WebRTC: track video tracks and peer connections per robot
        self._video_tracks: Dict[str, RosVideoTrack] = {}
        self._peer_connections: Dict[str, list[RTCPeerConnection]] = {}
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
            # Always subscribe to telemetry for discovered robots
            telemetry_topic = f"/{robot}/telemetry"
            self.telemetry_subscriptions[robot] = self.create_subscription(
                String,
                telemetry_topic,
                lambda msg, ns=robot: self._handle_telemetry(ns, msg),
                10,
            )

        for robot in gone_robots:
            self.get_logger().info(f"Robot disappeared: {robot}")
            self._stop_streaming(robot)
            pub = self.command_publishers.pop(robot, None)
            if pub:
                self.destroy_publisher(pub)
            tel_sub = self.telemetry_subscriptions.pop(robot, None)
            if tel_sub:
                self.destroy_subscription(tel_sub)

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
        topic = f"/{robot}/camera/image_raw"
        self.camera_subscriptions[robot] = self.create_subscription(
            Image,
            topic,
            lambda msg, ns=robot: self._handle_frame(ns, msg),
            _CAMERA_QOS,
        )
        self.streaming_robots.add(robot)
        self.get_logger().info(f"Started streaming {topic}")

    def _stop_streaming(self, robot: str) -> None:
        if robot not in self.streaming_robots:
            return
        sub = self.camera_subscriptions.pop(robot, None)
        if sub:
            self.destroy_subscription(sub)
        self.streaming_robots.discard(robot)
        # Close all peer connections for this robot
        pcs = self._peer_connections.pop(robot, [])
        for pc in pcs:
            asyncio.run_coroutine_threadsafe(pc.close(), self._aio_loop)
        self._video_tracks.pop(robot, None)
        self.get_logger().info(f"Stopped streaming /{robot}/camera/image_raw")

    # ── Frame handling (feed into WebRTC tracks) ─────────────────────

    def _handle_frame(self, robot_id: str, msg: Image) -> None:
        track = self._video_tracks.get(robot_id)
        if track is None:
            return
        raw = bytes(msg.data)
        encoding = msg.encoding or "bgra8"
        track.push_frame(msg.width, msg.height, encoding, raw)

    # ── ICE server config ───────────────────────────────────────────

    def _fetch_ice_servers(self) -> list[RTCIceServer]:
        """Fetch ICE server config from the API (with TURN credentials)."""
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

    # ── WebRTC signaling ─────────────────────────────────────────────

    def _handle_webrtc_offer(self, payload: dict) -> None:
        """Handle a WebRTC offer relayed from the API."""
        robot = (payload.get("robot") or "").strip()
        signaling_id = payload.get("signaling_id", "")
        sdp = payload.get("sdp", "")
        offer_type = payload.get("offer_type", "offer")

        if not robot or not sdp:
            self.get_logger().warning("Invalid webrtc_offer: missing robot or sdp")
            return

        if robot not in self.discovered_robots:
            self.get_logger().warning(f"WebRTC offer for unknown robot: {robot}")
            self._send_ws_message({
                "type": "webrtc_answer",
                "signaling_id": signaling_id,
                "error": f"robot {robot} not found",
            })
            return

        # Ensure we're streaming this robot's camera
        self._start_streaming(robot)

        # Fetch fresh TURN credentials if we haven't yet or periodically
        if not self._ice_servers_fetched:
            self._fetch_ice_servers()

        # Ensure we have a video track for this robot
        if robot not in self._video_tracks:
            self._video_tracks[robot] = RosVideoTrack(robot, self._aio_loop)

        track = self._video_tracks[robot]

        # Create peer connection and answer on the asyncio loop
        future = asyncio.run_coroutine_threadsafe(
            self._create_peer_connection(robot, signaling_id, sdp, offer_type, track),
            self._aio_loop,
        )
        # Don't block — the coroutine sends the answer via WS when ready

    async def _create_peer_connection(
        self,
        robot: str,
        signaling_id: str,
        sdp: str,
        offer_type: str,
        track: RosVideoTrack,
    ) -> None:
        ice_config = RTCConfiguration(iceServers=list(self._ice_servers))
        pc = RTCPeerConnection(configuration=ice_config)

        if robot not in self._peer_connections:
            self._peer_connections[robot] = []
        self._peer_connections[robot].append(pc)

        @pc.on("connectionstatechange")
        async def _on_state_change() -> None:
            state = pc.connectionState
            self.get_logger().info(f"WebRTC state for {robot}: {state}")
            if state in {"failed", "closed", "disconnected"}:
                pcs = self._peer_connections.get(robot, [])
                if pc in pcs:
                    pcs.remove(pc)
                await pc.close()
                # Notify API that a viewer disconnected
                self._send_ws_message({
                    "type": "webrtc_disconnected",
                    "robot": robot,
                })
                # If no more peer connections, stop streaming
                if not self._peer_connections.get(robot):
                    self._video_tracks.pop(robot, None)

        try:
            pc.addTrack(track)
            offer = RTCSessionDescription(sdp=sdp, type=offer_type)
            await pc.setRemoteDescription(offer)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

            self._send_ws_message({
                "type": "webrtc_answer",
                "signaling_id": signaling_id,
                "robot": robot,
                "sdp": pc.localDescription.sdp,
                "answer_type": pc.localDescription.type,
            })
            self.get_logger().info(f"Sent WebRTC answer for {robot} (signaling_id={signaling_id})")
        except Exception as exc:
            self.get_logger().error(f"Failed to create WebRTC answer for {robot}: {exc}")
            self._send_ws_message({
                "type": "webrtc_answer",
                "signaling_id": signaling_id,
                "error": str(exc),
            })
            pcs = self._peer_connections.get(robot, [])
            if pc in pcs:
                pcs.remove(pc)
            await pc.close()

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

        elif msg_type == "stop_stream":
            robot = (payload.get("robot") or "").strip()
            if robot:
                self.get_logger().info(f"Server requested stream stop for {robot}")
                self._stop_streaming(robot)

        elif msg_type == "webrtc_offer":
            self.get_logger().info(f"Received WebRTC offer for {payload.get('robot')}")
            self._handle_webrtc_offer(payload)

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
