"""
Simple placeholder controller used by both arena robots.

Each robot moves forward and steers away from obstacles detected by the
front IR sensors. When a controller argument like "ROBOT_ID=bot_alpha"
is supplied from the world file, it is exposed via the ROBOT_ID
environment variable for future ROS integrations.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
import time
from typing import Dict, Optional
from urllib import error, request

from controller import Camera, Robot

try:
    import roslibpy
except ImportError:  # pragma: no cover - optional dependency in sim env
    roslibpy = None


def parse_controller_args(args: list[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for arg in args:
        if "=" not in arg:
            continue
        key, value = arg.split("=", 1)
        result[key.strip()] = value.strip()
    return result


class CameraStreamer:
    def __init__(self, robot_id: str, host: str, port: int, api_base: Optional[str]) -> None:
        self.robot_id = robot_id
        self.mode = "disabled"
        self.api_base = api_base
        self.ros = None
        self.topic = None
        if roslibpy is not None:
            self.ros = roslibpy.Ros(host=host, port=port, is_secure=False)
            try:
                self.ros.run()
            except Exception:
                self.ros = None
            if self.ros:
                for _ in range(100):
                    if self.ros.is_connected:
                        break
                    time.sleep(0.05)
            if self.ros and self.ros.is_connected:
                topic_name = f"/{robot_id}/camera/image_raw"
                self.topic = roslibpy.Topic(
                    self.ros,
                    topic_name,
                    "sensor_msgs/msg/Image",
                )
                self.mode = "ros"
        if self.mode != "ros" and self.api_base:
            self.mode = "http"

    def publish(self, camera: Camera, sim_time: float) -> None:
        if self.mode == "ros" and self.topic:
            image_data = camera.getImage()
            if image_data is None:
                return
            width = camera.getWidth()
            height = camera.getHeight()
            secs = int(sim_time)
            nsecs = int((sim_time - secs) * 1e9)
            message = {
                "header": {
                    "stamp": {"sec": secs, "nanosec": nsecs},
                    "frame_id": f"{self.robot_id}_camera",
                },
                "height": height,
                "width": width,
                "encoding": "BGRA8",
                "is_bigendian": 0,
                "step": width * 4,
                "data": list(image_data),
            }
            try:
                self.topic.publish(message)
            except Exception:
                self.mode = "disabled"
        elif self.mode == "http" and self.api_base:
            image_data = camera.getImage()
            if image_data is None:
                return
            payload = {
                "width": camera.getWidth(),
                "height": camera.getHeight(),
                "format": "bgra",
                "image": base64.b64encode(image_data).decode("ascii"),
                "sim_time": sim_time,
            }
            data = json.dumps(payload).encode("utf-8")
            endpoint = f"{self.api_base}/robots/{self.robot_id}/frame"
            req = request.Request(
                endpoint,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            try:
                request.urlopen(req, timeout=0.2)
            except error.URLError:
                pass

    def shutdown(self) -> None:
        if self.topic:
            with contextlib.suppress(Exception):
                self.topic.unsubscribe()
        if self.ros:
            with contextlib.suppress(Exception):
                self.ros.terminate()


def create_cmd_velocity_listener(robot_id: str, host: str, port: int):
    if roslibpy is None:
        return None
    ros = roslibpy.Ros(host=host, port=port, is_secure=False)
    try:
        ros.run()
    except Exception:
        return None
    for _ in range(100):
        if ros.is_connected:
            break
        time.sleep(0.05)
    if not ros.is_connected:
        ros.terminate()
        return None

    latest = {"linear": 0.0, "angular": 0.0, "timestamp": 0.0}

    def _callback(message: Dict[str, Dict[str, float]]) -> None:
        linear = message.get("linear", {}).get("x", 0.0)
        angular = message.get("angular", {}).get("z", 0.0)
        latest["linear"] = float(linear)
        latest["angular"] = float(angular)
        latest["timestamp"] = time.time()

    topic = roslibpy.Topic(
        ros,
        f"/{robot_id}/cmd_vel",
        "geometry_msgs/msg/Twist",
    )
    topic.subscribe(_callback)

    return ros, topic, latest


def main() -> None:
    robot = Robot()
    args = parse_controller_args(sys.argv[1:])
    if args:
        os.environ.update(args)
    robot_id = args.get("ROBOT_ID", robot.getName())
    ros_host = os.getenv("ROS_BRIDGE_HOST", "localhost")
    ros_port = int(os.getenv("ROS_BRIDGE_PORT", "9090"))
    api_base = (
        os.getenv("API_BASE_URL")
        or os.getenv("API_BASE")
        or "http://localhost:8081/api"
    )
    camera_streamer = CameraStreamer(robot_id, ros_host, ros_port, api_base)
    cmd_listener = create_cmd_velocity_listener(robot_id, ros_host, ros_port)

    timestep = int(robot.getBasicTimeStep())
    left_motor = robot.getDevice("left wheel motor")
    right_motor = robot.getDevice("right wheel motor")
    left_motor.setPosition(float("inf"))
    right_motor.setPosition(float("inf"))

    sensors = []
    for index in range(8):
        sensor = robot.getDevice(f"ps{index}")
        sensor.enable(timestep)
        sensors.append(sensor)

    camera: Optional[Camera] = None
    try:
        camera = robot.getDevice("camera")
        if camera:
            camera.enable(timestep)
    except Exception:  # pylint: disable=broad-except
        camera = None

    forward_speed = 4.0
    turn_speed = 3.0
    frame_interval = max(1, int(500 / timestep))
    frame_tick = 0
    command_timeout = 0.5

    while robot.step(timestep) != -1:
        front_left = sensors[7].getValue()
        front_right = sensors[0].getValue()

        left_speed = forward_speed
        right_speed = forward_speed

        if front_left > 90 or front_right > 90:
            if front_left > front_right:
                left_speed = -turn_speed
                right_speed = turn_speed
            else:
                left_speed = turn_speed
                right_speed = -turn_speed

        if cmd_listener:
            _, _, latest = cmd_listener
            if time.time() - latest["timestamp"] < command_timeout:
                linear = latest["linear"]
                angular = latest["angular"]
                base_speed = linear * 20.0
                turn = angular * 10.0
                left_speed = base_speed - turn
                right_speed = base_speed + turn

        left_motor.setVelocity(left_speed)
        right_motor.setVelocity(right_speed)

        if camera and robot_id:
            frame_tick += 1
            if frame_tick % frame_interval == 0:
                camera_streamer.publish(camera, robot.getTime())

        if robot_id and robot.getTime() % 10 < timestep / 1000:
            robot.wwiSendText(f"{robot_id}: running")

    camera_streamer.shutdown()
    if cmd_listener:
        ros, topic, _ = cmd_listener
        with contextlib.suppress(Exception):
            topic.unsubscribe()
        with contextlib.suppress(Exception):
            ros.terminate()


if __name__ == "__main__":
    main()
