from __future__ import annotations

import base64
import gzip
import json
import os
import queue
import threading
import time
import urllib.parse
from typing import Dict, List

import requests
import rclpy
import websocket
from geometry_msgs.msg import Twist
from rclpy.node import Node
from rclpy.publisher import Publisher
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from sensor_msgs.msg import Image


class RobotBridgeNode(Node):
    def __init__(self) -> None:
        super().__init__("robot_bridge")
        namespaces = os.getenv("CAMERA_NAMESPACES", "")
        self.namespaces: List[str] = [value.strip() for value in namespaces.split(",") if value.strip()]
        if not self.namespaces:
            raise RuntimeError("CAMERA_NAMESPACES env var is empty; cannot forward streams")

        api_base = os.getenv("API_BASE_URL", "http://robot-gateway:8080/api").rstrip("/")
        push_base = f"{api_base}/internal/frames"
        self.api_base = api_base
        self.command_base = f"{api_base}/internal/robots".rstrip("/")
        self.push_base_url = push_base
        self.api_key = os.getenv("LOBBY_KEY") or os.getenv("ROS_PUSH_KEY", "")
        self.headers = {"x-api-key": self.api_key} if self.api_key else {}
        self.session = requests.Session()
        self.heartbeat_interval = float(os.getenv("COMMAND_HEARTBEAT_INTERVAL", "5"))
        self.command_queue: queue.Queue[Dict] = queue.Queue()
        self.ws_lock = threading.Lock()
        self.ws_app: websocket.WebSocketApp | None = None

        # QoS for camera - BEST_EFFORT to match publisher (drop frames rather than queue)
        camera_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1
        )

        self.command_publishers: Dict[str, Publisher] = {}
        for namespace in self.namespaces:
            topic = f"/{namespace}/camera/image_raw"
            self.create_subscription(
                Image,
                topic,
                lambda msg, ns=namespace: self._handle_frame(ns, msg),
                camera_qos,
            )
            cmd_topic = f"/{namespace}/cmd_vel"
            self.command_publishers[namespace] = self.create_publisher(Twist, cmd_topic, 10)
            self.get_logger().info(
                f"Forwarding {topic} and pulling commands for {cmd_topic} -> {self.command_base}/{namespace}"
            )

        self.create_timer(0.1, self.flush_command_queue)
        self.create_timer(self.heartbeat_interval, self.send_heartbeats)
        self.ws_thread = threading.Thread(target=self._run_command_socket, daemon=True)
        self.ws_thread.start()

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
            resp = self.session.post(url, json=payload, headers=self.headers, timeout=2)
            resp.raise_for_status()
            self.get_logger().debug(
                f"Pushed frame for {robot_id} ({msg.width}x{msg.height} encoded as {payload['encoding']})"
            )
        except requests.RequestException as exc:
            self.get_logger().warning(f"Failed to push frame for {robot_id}: {exc}")

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
        self._send_ws_message({"type": "subscribe", "robots": self.namespaces})

    def _ws_on_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            self.get_logger().warning("Received invalid JSON on command websocket")
            return
        if payload.get("type") == "command":
            robot = payload.get("robot")
            cmd = payload.get("command") or {}
            linear_x = float(cmd.get("linear_x", 0.0))
            angular_z = float(cmd.get("angular_z", 0.0))
            self.get_logger().info(
                f"Queued command {cmd.get('id')} for {robot} (lin_x={linear_x:.3f} ang_z={angular_z:.3f})"
            )
            self.command_queue.put(payload)

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
        self._send_ws_message({"type": "heartbeat", "robots": self.namespaces})


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
