"""Server-side slam_toolbox management for the ros-bridge.

Runs one slam_toolbox pose-graph SLAM node per streaming robot (off the Pi),
using the robot's `/scan` + `/odom` + TF. Responsibilities:

  * spawn / tear down the slam_toolbox process per robot
  * look up the drift-corrected robot pose from TF (map -> base_link)
  * forward the OccupancyGrid map + pose upward (via a caller-supplied sender)
    so the API can relay it to browsers (Design B)
  * persist the pose graph across sessions: on start, pull the lobby's stored
    graph from the API and deserialize/continue it; periodically and on stop,
    serialize and push it back. The DB (keyed by the lobby's api key) is the
    source of truth; the on-disk files are just scratch for slam_toolbox.

NOTE: this is ROS runtime glue that can only be fully validated on the robot
(needs a live ROS graph + slam_toolbox + wheel /odom). Integration points are
kept small and the assumptions are logged loudly so bring-up is debuggable.

Multi-robot caveat: TF frames (odom/base_link/map) are not namespaced by the
robot, so this currently assumes a single active robot per ROS graph / lobby.
Per-robot frame prefixes are a follow-up for true multi-robot lobbies.
"""

from __future__ import annotations

import base64
import os
import signal
import subprocess
import threading
from typing import Callable, Dict, Optional

import numpy as np
import requests
import rclpy
from nav_msgs.msg import OccupancyGrid
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy

# slam_toolbox services (available once ros-humble-slam-toolbox is installed).
try:
    from slam_toolbox.srv import DeserializePoseGraph, SerializePoseGraph
    _HAVE_SLAM_SRV = True
except Exception:  # pragma: no cover - only importable in the humble container
    DeserializePoseGraph = SerializePoseGraph = None  # type: ignore
    _HAVE_SLAM_SRV = False

import tf2_ros


def _yaw_from_quaternion(x: float, y: float, z: float, w: float) -> float:
    """Extract the SE(2) heading (yaw) from a quaternion — avoids a tf_transformations dep."""
    return float(np.arctan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z)))

# Map (OccupancyGrid) is latched/transient-local by slam_toolbox.
_MAP_QOS = QoSProfile(
    reliability=QoSReliabilityPolicy.RELIABLE,
    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
    history=QoSHistoryPolicy.KEEP_LAST,
    depth=1,
)

_SCRATCH_DIR = os.getenv("SLAM_SCRATCH_DIR", "/tmp/slam")
# How often (seconds) to serialize + upload the pose graph while mapping.
_SERIALIZE_INTERVAL = float(os.getenv("SLAM_SERIALIZE_INTERVAL", "30"))
# Downsample factor for the map we forward to browsers (mirror the API tuning).
_MAP_DOWNSAMPLE = int(os.getenv("SLAM_MAP_DOWNSAMPLE", "4"))


def _downsample_occupancy(grid: np.ndarray, factor: int) -> np.ndarray:
    """Block-aggregate an occupancy grid (occupied > free > unknown via max)."""
    if factor <= 1:
        return grid
    h, w = grid.shape
    h2, w2 = (h // factor) * factor, (w // factor) * factor
    if h2 == 0 or w2 == 0:
        return grid
    blocks = grid[:h2, :w2].reshape(h2 // factor, factor, w2 // factor, factor)
    return blocks.max(axis=(1, 3))


class _RobotSlam:
    """Per-robot slam_toolbox process + subscriptions + serialize client."""

    def __init__(self, robot: str) -> None:
        self.robot = robot
        self.process: Optional[subprocess.Popen] = None
        self.map_sub = None
        self.serialize_client = None
        self.serialize_timer = None
        self.map_file = os.path.join(_SCRATCH_DIR, robot)  # slam_toolbox adds .posegraph/.data


class SlamManager:
    """Owns slam_toolbox lifecycle for all streaming robots on this bridge."""

    def __init__(
        self,
        node: Node,
        api_base: str,
        headers: Dict[str, str],
        http_session: requests.Session,
        map_sender: Callable[[str, str], None],
    ) -> None:
        self._node = node
        self._api_base = api_base.rstrip("/")
        self._headers = headers
        self._http = http_session
        # map_sender(robot, json_str) forwards a map_update up to the API.
        self._map_sender = map_sender
        self._robots: Dict[str, _RobotSlam] = {}

        # Shared TF buffer for pose lookups (map -> base_link).
        self._tf_buffer = tf2_ros.Buffer()
        self._tf_listener = tf2_ros.TransformListener(self._tf_buffer, node)

        os.makedirs(_SCRATCH_DIR, exist_ok=True)

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self, robot: str) -> None:
        if robot in self._robots:
            return
        self._node.get_logger().info(f"[slam] starting slam_toolbox for {robot}")
        rs = _RobotSlam(robot)
        self._robots[robot] = rs

        self._check_odom(robot)
        had_prior = self._pull_pose_graph(robot, rs.map_file)
        self._spawn(rs, load_map=had_prior)

        # Subscribe to the robot's slam_toolbox map output and forward it up.
        rs.map_sub = self._node.create_subscription(
            OccupancyGrid,
            f"/{robot}/map",
            lambda msg, r=robot: self._on_map(r, msg),
            _MAP_QOS,
        )

        if _HAVE_SLAM_SRV:
            rs.serialize_client = self._node.create_client(
                SerializePoseGraph, f"/{robot}/slam_toolbox/serialize_map"
            )
            rs.serialize_timer = self._node.create_timer(
                _SERIALIZE_INTERVAL, lambda r=robot: self._serialize_and_upload(r)
            )

    def stop(self, robot: str) -> None:
        rs = self._robots.pop(robot, None)
        if rs is None:
            return
        self._node.get_logger().info(f"[slam] stopping slam_toolbox for {robot}")
        # Final serialize + upload so the session is persisted for stitching.
        try:
            self._serialize_and_upload(robot, rs=rs, blocking=True)
        except Exception as e:  # noqa: BLE001
            self._node.get_logger().warning(f"[slam] final serialize failed for {robot}: {e}")

        if rs.serialize_timer is not None:
            rs.serialize_timer.cancel()
        if rs.map_sub is not None:
            self._node.destroy_subscription(rs.map_sub)
        self._kill(rs)

    def shutdown(self) -> None:
        for robot in list(self._robots.keys()):
            self.stop(robot)

    # ── slam_toolbox process ─────────────────────────────────────────────

    def _spawn(self, rs: _RobotSlam, load_map: bool) -> None:
        robot = rs.robot
        params = [
            "ros2", "run", "slam_toolbox", "async_slam_toolbox_node",
            "--ros-args",
            "-r", f"__ns:=/{robot}",
            "-p", "use_sim_time:=false",
            "-p", "odom_frame:=odom",
            "-p", "base_frame:=base_link",
            "-p", "map_frame:=map",
            "-p", f"scan_topic:=/{robot}/scan",
            "-p", "mode:=mapping",
            "-p", "resolution:=0.05",
            # Keep the graph shallow/cheap for a Pi-class robot at room scale.
            "-p", "minimum_travel_distance:=0.2",
            "-p", "minimum_travel_heading:=0.2",
        ]
        if load_map:
            # Continue from the lobby's stored graph (cross-session accumulation).
            params += [
                "-p", f"map_file_name:={rs.map_file}",
                "-p", "map_start_at_dock:=true",
            ]
        rs.process = subprocess.Popen(params, start_new_session=True)
        self._node.get_logger().info(
            f"[slam] spawned slam_toolbox pid={rs.process.pid} for {robot} "
            f"(load_map={load_map})"
        )

    def _kill(self, rs: _RobotSlam) -> None:
        if rs.process is None:
            return
        try:
            os.killpg(os.getpgid(rs.process.pid), signal.SIGINT)
            rs.process.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                os.killpg(os.getpgid(rs.process.pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass
        rs.process = None

    def _check_odom(self, robot: str) -> None:
        """Warn loudly if the robot isn't publishing odom — slam_toolbox needs it."""
        topics = dict(self._node.get_topic_names_and_types())
        odom_candidates = [f"/{robot}/odom", "/odom"]
        if not any(t in topics for t in odom_candidates):
            self._node.get_logger().warning(
                f"[slam] no odom topic found for {robot} (looked for {odom_candidates}); "
                "slam_toolbox needs odom->base_link TF. Pose will be poor until odom exists."
            )

    # ── map forwarding (Design B) ────────────────────────────────────────

    def _on_map(self, robot: str, msg: OccupancyGrid) -> None:
        try:
            w, h = msg.info.width, msg.info.height
            if w == 0 or h == 0:
                return
            # OccupancyGrid data: -1 unknown, 0..100 occupied prob. Row-major.
            grid = np.array(msg.data, dtype=np.int16).reshape(h, w)
            grid = _downsample_occupancy(grid, _MAP_DOWNSAMPLE)
            out_h, out_w = grid.shape
            resolution = msg.info.resolution * _MAP_DOWNSAMPLE

            # Encode like the API's map_update: -1->0, 0->1, ... 100->101.
            grid_uint8 = (np.clip(grid, -1, 100) + 1).astype(np.uint8)
            data_b64 = base64.b64encode(grid_uint8.tobytes()).decode("ascii")

            px, py, ptheta = self._lookup_pose(robot)
            payload = {
                "type": "map_update",
                "width": out_w,
                "height": out_h,
                "resolution": resolution,
                "origin_x": msg.info.origin.position.x,
                "origin_y": msg.info.origin.position.y,
                "robot_x": px,
                "robot_y": py,
                "robot_theta": ptheta,
                "data": data_b64,
            }
            import json
            self._map_sender(robot, json.dumps(payload))
        except Exception as e:  # noqa: BLE001
            self._node.get_logger().warning(f"[slam] map forward failed for {robot}: {e}")

    def _lookup_pose(self, robot: str):
        """Return (x, y, theta) of base_link in the map frame, or map center-ish."""
        try:
            tf = self._tf_buffer.lookup_transform(
                "map", "base_link", rclpy.time.Time()
            )
            t = tf.transform.translation
            q = tf.transform.rotation
            yaw = _yaw_from_quaternion(q.x, q.y, q.z, q.w)
            return float(t.x), float(t.y), yaw
        except Exception:  # noqa: BLE001 - TF may not be ready yet
            return 0.0, 0.0, 0.0

    # ── persistence (pose graph <-> API/DB) ──────────────────────────────

    def _serialize_and_upload(self, robot: str, rs: Optional[_RobotSlam] = None,
                              blocking: bool = False) -> None:
        rs = rs or self._robots.get(robot)
        if rs is None or not _HAVE_SLAM_SRV or rs.serialize_client is None:
            return
        if not rs.serialize_client.service_is_ready():
            return
        req = SerializePoseGraph.Request()
        req.filename = rs.map_file
        future = rs.serialize_client.call_async(req)

        def _after(_fut):
            self._upload_pose_graph(robot, rs.map_file)

        if blocking:
            # On shutdown we can't spin the executor; give it a brief window.
            rclpy.spin_until_future_complete(self._node, future, timeout_sec=8.0)
            self._upload_pose_graph(robot, rs.map_file)
        else:
            future.add_done_callback(_after)

    def _upload_pose_graph(self, robot: str, map_file: str) -> None:
        pose_path, data_path = map_file + ".posegraph", map_file + ".data"
        if not (os.path.exists(pose_path) and os.path.exists(data_path)):
            self._node.get_logger().warning(
                f"[slam] serialize produced no files for {robot} ({pose_path})"
            )
            return
        try:
            with open(pose_path, "rb") as f:
                pose_b64 = base64.b64encode(f.read()).decode("ascii")
            with open(data_path, "rb") as f:
                data_b64 = base64.b64encode(f.read()).decode("ascii")
            resp = self._http.post(
                f"{self._api_base}/internal/slam/pose-graph",
                headers=self._headers,
                json={
                    "posegraph_b64": pose_b64,
                    "data_b64": data_b64,
                    "robot_namespace": robot,
                    "resolution": 0.05,
                },
                timeout=15,
            )
            resp.raise_for_status()
            self._node.get_logger().info(
                f"[slam] uploaded pose graph for {robot} ({resp.json().get('bytes')} bytes)"
            )
        except Exception as e:  # noqa: BLE001
            self._node.get_logger().warning(f"[slam] upload failed for {robot}: {e}")

    def _pull_pose_graph(self, robot: str, map_file: str) -> bool:
        """Fetch this lobby's stored graph and write the scratch files. Returns True if present."""
        try:
            resp = self._http.get(
                f"{self._api_base}/internal/slam/pose-graph",
                headers=self._headers,
                timeout=15,
            )
            if resp.status_code == 404:
                self._node.get_logger().info(f"[slam] no stored pose graph for {robot}'s lobby")
                return False
            resp.raise_for_status()
            body = resp.json()
            with open(map_file + ".posegraph", "wb") as f:
                f.write(base64.b64decode(body["posegraph_b64"]))
            with open(map_file + ".data", "wb") as f:
                f.write(base64.b64decode(body["data_b64"]))
            self._node.get_logger().info(
                f"[slam] loaded prior pose graph for {robot} (updated {body.get('updated_at')})"
            )
            return True
        except Exception as e:  # noqa: BLE001
            self._node.get_logger().warning(f"[slam] pull pose graph failed for {robot}: {e}")
            return False
