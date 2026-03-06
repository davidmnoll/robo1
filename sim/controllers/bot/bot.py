"""
Simple placeholder controller used by both arena robots.

Each robot moves forward and steers away from obstacles detected by the
front IR sensors. When a controller argument like "ROBOT_ID=bot_alpha"
is supplied from the world file, it is exposed via the ROBOT_ID
environment variable for future ROS integrations.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Optional

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


def build_logger(robot_id: str) -> Callable[[str], None]:
    log_dir = Path(os.getenv("BOT_LOG_DIR", "/tmp/bot_logs"))
    with contextlib.suppress(OSError):
        log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{robot_id}.log"

    def _log(message: str) -> None:
        text = f"[{robot_id}] {message}"
        print(text, flush=True)
        with contextlib.suppress(OSError):
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")

    return _log


class CameraStreamer:
    def __init__(
        self,
        robot_id: str,
        host: str,
        port: int,
        log_fn: Callable[[str], None],
        max_attempts: int = 30,
    ) -> None:
        if roslibpy is None:
            raise RuntimeError("roslibpy unavailable; cannot stream camera")
        self.robot_id = robot_id
        self.log = log_fn
        self.ros: Optional[roslibpy.Ros] = None
        self.topic: Optional[roslibpy.Topic] = None
        self._publish_count = 0
        for attempt in range(1, max_attempts + 1):
            self.log(f"connecting to rosbridge ({host}:{port}) attempt {attempt}/{max_attempts}")
            candidate = roslibpy.Ros(host=host, port=port, is_secure=False)
            try:
                candidate.run()
            except Exception as exc:
                self.log(f"rosbridge connection failed: {exc}")
                time.sleep(1.0)
                continue
            for _ in range(100):
                if candidate.is_connected:
                    break
                time.sleep(0.1)
            if candidate.is_connected:
                self.ros = candidate
                break
            self.log("rosbridge still unavailable; retrying in 1s")
            time.sleep(1.0)
        if not self.ros or not self.ros.is_connected:
            raise RuntimeError("rosbridge connection timed out")
        topic_name = f"/{robot_id}/camera/image_raw"
        self.topic = roslibpy.Topic(
            self.ros,
            topic_name,
            "sensor_msgs/msg/Image",
        )
        self.log(f"rosbridge camera stream via {host}:{port}")

    def publish(self, camera: Camera, sim_time: float) -> None:
        image_data = camera.getImage()
        if image_data is None:
            return
        width = camera.getWidth()
        height = camera.getHeight()
        if not self.topic:
            raise RuntimeError("rosbridge topic unavailable")
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
        self.topic.publish(message)
        self._publish_count += 1
        if self._publish_count % 30 == 0:
            self.log(f"published {self._publish_count} camera frames")

    def shutdown(self) -> None:
        if self.topic:
            with contextlib.suppress(Exception):
                self.topic.unsubscribe()
        if self.ros:
            with contextlib.suppress(Exception):
                self.ros.terminate()


def create_cmd_velocity_listener(
    robot_id: str,
    host: str,
    port: int,
    log_fn: Callable[[str], None],
    max_attempts: int = 30,
) -> Optional[tuple[roslibpy.Ros, roslibpy.Topic, Dict[str, float]]]:
    if roslibpy is None:
        log_fn("roslibpy unavailable; cmd_vel listener disabled")
        return None
    ros: Optional[roslibpy.Ros] = None
    for attempt in range(1, max_attempts + 1):
        log_fn(f"connecting to cmd_vel on rosbridge attempt {attempt}/{max_attempts}")
        candidate = roslibpy.Ros(host=host, port=port, is_secure=False)
        try:
            candidate.run()
        except Exception as exc:
            log_fn(f"failed to connect to rosbridge for cmd_vel: {exc}")
            time.sleep(1.0)
            continue
        for _ in range(100):
            if candidate.is_connected:
                break
            time.sleep(0.1)
        if candidate.is_connected:
            ros = candidate
            break
        log_fn("rosbridge unreachable for cmd_vel; retrying in 1s")
        time.sleep(1.0)
    if ros is None or not ros.is_connected:
        log_fn("cmd_vel listener failed after repeated attempts")
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
    log_fn("subscribed to cmd_vel")

    return ros, topic, latest


def main() -> None:
    robot = Robot()
    args = parse_controller_args(sys.argv[1:])
    if args:
        os.environ.update(args)
    robot_id = args.get("ROBOT_ID", robot.getName())
    ros_host = os.getenv("ROS_BRIDGE_HOST", "localhost")
    ros_port = int(os.getenv("ROS_BRIDGE_PORT", "9090"))
    log = build_logger(robot_id)
    camera_streamer: CameraStreamer | None = None
    while camera_streamer is None:
        try:
            camera_streamer = CameraStreamer(robot_id, ros_host, ros_port, log)
        except RuntimeError as exc:
            log(f"camera streamer init failed: {exc}; retrying in 2s")
            time.sleep(2.0)
    cmd_listener = create_cmd_velocity_listener(robot_id, ros_host, ros_port, log)

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
