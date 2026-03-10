from __future__ import annotations

import base64
import gzip
import json
import os
import queue
import re
import threading
import time
import urllib.parse
from typing import Dict, Set

import requests
import rclpy
import websocket
from geometry_msgs.msg import Twist
from rclpy.node import Node
from rclpy.publisher import Publisher
from rclpy.subscription import Subscription
from sensor_msgs.msg import Image


_CAMERA_TOPIC_RE = re.compile(r"^/([^/]+)/camera/image_raw$")
_DISCOVERY_INTERVAL = 3.0


class RobotBridgeNode(Node):
    def __init__(self) -> None:
        super().__init__("robot_bridge")

        api_base = os.getenv("API_BASE_URL", "http://robot-gateway:8080/api").rstrip("/")
        self.api_base = api_base
        self.push_base_url = f"{api_base}/internal/frames"
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
        self.command_publishers: Dict[str, Publisher] = {}

        # Timers
        self.create_timer(0.1, self.flush_command_queue)
        self.create_timer(self.heartbeat_interval, self.send_heartbeats)
        self.create_timer(_DISCOVERY_INTERVAL, self._discover_robots)

        # WebSocket thread for commands + stream control
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

        for robot in gone_robots:
            self.get_logger().info(f"Robot disappeared: {robot}")
            self._stop_streaming(robot)
            pub = self.command_publishers.pop(robot, None)
            if pub:
                self.destroy_publisher(pub)

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
            10,
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
        self.get_logger().info(f"Stopped streaming /{robot}/camera/image_raw")

    # ── Frame push ───────────────────────────────────────────────────

    def _handle_frame(self, robot_id: str, msg: Image) -> None:
        raw = bytes(msg.data)
        compressed = gzip.compress(raw)
        b64_payload = base64.b64encode(compressed).decode("ascii")
        payload = {
            "width": msg.width,
            "height": msg.height,
            "encoding": msg.encoding or "bgra8",
            "data": b64_payload,
            "compressed": True,
            "stamp_sec": int(msg.header.stamp.sec) if msg.header.stamp else 0,
            "stamp_nanosec": int(msg.header.stamp.nanosec) if msg.header.stamp else 0,
        }
        url = f"{self.push_base_url}/{robot_id}"
        try:
            resp = self.http_session.post(url, json=payload, headers=self.headers, timeout=2)
            resp.raise_for_status()
            self.get_logger().debug(
                f"Pushed frame for {robot_id} ({msg.width}x{msg.height} encoded as {payload['encoding']})"
            )
        except requests.RequestException as exc:
            self.get_logger().warning(f"Failed to push frame for {robot_id}: {exc}")

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


def main() -> None:
    rclpy.init()
    node = RobotBridgeNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
