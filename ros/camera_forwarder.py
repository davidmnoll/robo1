from __future__ import annotations

import base64
import os
from typing import List

import requests
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image


class CameraForwarder(Node):
    def __init__(self) -> None:
        super().__init__("camera_forwarder")
        namespaces = os.getenv("CAMERA_NAMESPACES", "")
        self.namespaces: List[str] = [value.strip() for value in namespaces.split(",") if value.strip()]
        if not self.namespaces:
            raise RuntimeError("CAMERA_NAMESPACES env var is empty; cannot forward camera streams")
        self.push_base_url = os.getenv(
            "API_PUSH_URL",
            "http://robot-gateway:8080/api/internal/frames",
        ).rstrip("/")
        self.api_key = os.getenv("ROS_PUSH_KEY", "")
        self.headers = {"x-api-key": self.api_key} if self.api_key else {}
        for namespace in self.namespaces:
            topic = f"/{namespace}/camera/image_raw"
            self.create_subscription(Image, topic, lambda msg, ns=namespace: self._handle_frame(ns, msg), 10)
            self.get_logger().info(f"Forwarding {topic} to {self.push_base_url}/{namespace}")

    def _handle_frame(self, robot_id: str, msg: Image) -> None:
        data = base64.b64encode(bytes(msg.data)).decode("ascii")
        payload = {
            "width": msg.width,
            "height": msg.height,
            "encoding": msg.encoding or "bgra8",
            "data": data,
            "stamp_sec": int(msg.header.stamp.sec) if msg.header.stamp else 0,
            "stamp_nanosec": int(msg.header.stamp.nanosec) if msg.header.stamp else 0,
        }
        url = f"{self.push_base_url}/{robot_id}"
        try:
            resp = requests.post(url, json=payload, headers=self.headers, timeout=2)
            resp.raise_for_status()
        except requests.RequestException as exc:
            self.get_logger().warning(f"Failed to push frame for {robot_id}: {exc}")


def main() -> None:
    rclpy.init()
    node = CameraForwarder()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
