import asyncio
import base64
import contextlib
import logging
import time
from typing import Any, Dict, Tuple

import httpx
import numpy as np
import roslibpy
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ros_bridge_host: str = Field("localhost", alias="ROS_BRIDGE_HOST")
    ros_bridge_port: int = Field(9090, alias="ROS_BRIDGE_PORT")
    ros_proxy_url: str = Field("http://ros-core:8080/authorize", alias="ROS_PROXY_URL")
    ros_proxy_key: str = Field("local-dev-key", alias="ROS_PROXY_KEY")
    gateway_name: str = Field("gateway-1", alias="GATEWAY_NAME")
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["*"], alias="CORS_ALLOW_ORIGINS")


settings = Settings()
logger = logging.getLogger("gateway")
logging.basicConfig(level=logging.INFO)
frame_queues: Dict[str, asyncio.Queue[Tuple[int, int, bytes]]] = {}
peer_connections: set[RTCPeerConnection] = set()
camera_subscriptions: Dict[str, roslibpy.Topic] = {}

app = FastAPI(title="Robot Gateway API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


class TwistCommand(BaseModel):
    linear_x: float = 0.0
    linear_y: float = 0.0
    linear_z: float = 0.0
    angular_x: float = 0.0
    angular_y: float = 0.0
    angular_z: float = 0.0


class RobotFramePayload(BaseModel):
    width: int
    height: int
    format: str = "bgra"
    image: str  # base64


class WebRTCOffer(BaseModel):
    sdp: str
    type: str


async def ensure_proxy_access() -> None:
    """Hit the ROS proxy to prove this gateway is allowed to publish."""
    if not settings.ros_proxy_url:
        return
    headers = {"x-api-key": settings.ros_proxy_key} if settings.ros_proxy_key else {}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(settings.ros_proxy_url, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Skipping proxy authorization check: %s", exc)


async def ros_client() -> roslibpy.Ros:
    ros: roslibpy.Ros | None = getattr(app.state, "ros_client", None)
    if ros is None or not ros.is_connected:
        raise HTTPException(status_code=503, detail="ROS bridge unavailable")
    return ros


@app.on_event("startup")
async def startup_event() -> None:
    await ensure_proxy_access()
    ros = roslibpy.Ros(
        host=settings.ros_bridge_host,
        port=settings.ros_bridge_port,
        is_secure=False,
    )
    ros.run()
    for _ in range(100):
        if ros.is_connected:
            break
        await asyncio.sleep(0.1)
    if not ros.is_connected:
        raise RuntimeError("Failed to connect to ROS bridge")
    app.state.ros_client = ros
    logger.info(
        "Gateway %s connected to ROS bridge %s:%s",
        settings.gateway_name,
        settings.ros_bridge_host,
        settings.ros_bridge_port,
    )


@app.on_event("shutdown")
async def shutdown_event() -> None:
    ros: roslibpy.Ros | None = getattr(app.state, "ros_client", None)
    if ros:
        ros.terminate()
        logger.info("Disconnected from ROS bridge")
    for topic in camera_subscriptions.values():
        with contextlib.suppress(Exception):
            topic.unsubscribe()
    camera_subscriptions.clear()
    for pc in list(peer_connections):
        await pc.close()
    peer_connections.clear()


@app.get("/api/health")
async def health() -> dict[str, Any]:
    ros: roslibpy.Ros | None = getattr(app.state, "ros_client", None)
    return {
        "status": "ok",
        "ros_connected": bool(ros and ros.is_connected),
        "gateway": settings.gateway_name,
    }


def publish_twist(ros: roslibpy.Ros, robot_id: str, cmd: TwistCommand) -> None:
    topic_name = f"/{robot_id}/cmd_vel"
    ros_topic = roslibpy.Topic(
        ros,
        topic_name,
        "geometry_msgs/msg/Twist",
    )
    ros_topic.publish(
        {
            "linear": {"x": cmd.linear_x, "y": cmd.linear_y, "z": cmd.linear_z},
            "angular": {"x": cmd.angular_x, "y": cmd.angular_y, "z": cmd.angular_z},
        }
    )
    ros_topic.unadvertise()


@app.post("/api/robots/{robot_id}/cmd_vel")
async def send_cmd_vel(robot_id: str, cmd: TwistCommand, ros: roslibpy.Ros = Depends(ros_client)) -> dict[str, Any]:
    publish_twist(ros, robot_id, cmd)
    return {"robot": robot_id, "status": "queued"}


@app.websocket("/api/ws/{robot_id}")
async def websocket_proxy(websocket: WebSocket, robot_id: str) -> None:
    await websocket.accept()
    await websocket.send_json(
        {
            "robot": robot_id,
            "message": "WebSocket channel established. Implement telemetry fan-out here.",
        }
    )
    try:
        while True:
            data = await websocket.receive_text()
            logger.debug("Received WS payload for %s: %s", robot_id, data)
    except WebSocketDisconnect:
        logger.info("Client disconnected from %s WS", robot_id)


@app.post("/api/robots/{robot_id}/frame")
async def ingest_frame(robot_id: str, payload: RobotFramePayload) -> dict[str, str]:
    try:
        image_bytes = base64.b64decode(payload.image)
    except base64.binascii.Error as exc:
        raise HTTPException(status_code=400, detail="invalid base64 image") from exc
    queue = get_frame_queue(robot_id)
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    await queue.put((payload.width, payload.height, image_bytes))
    return {"status": "accepted"}


@app.post("/api/robots/{robot_id}/webrtc")
async def start_webrtc(robot_id: str, offer: WebRTCOffer) -> dict[str, str]:
    ensure_camera_subscription(robot_id)
    queue = get_frame_queue(robot_id)
    if queue.empty():
        logger.warning("No frames received yet for %s; WebRTC stream may be blank", robot_id)
    pc = RTCPeerConnection()
    peer_connections.add(pc)

    @pc.on("connectionstatechange")
    async def _on_state_change() -> None:
        if pc.connectionState in {"failed", "closed"}:
            peer_connections.discard(pc)
            await pc.close()

    pc.addTrack(RobotVideoTrack(robot_id))
    rtc_offer = RTCSessionDescription(sdp=offer.sdp, type=offer.type)
    await pc.setRemoteDescription(rtc_offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
def get_frame_queue(robot_id: str) -> asyncio.Queue[Tuple[int, int, bytes]]:
    if robot_id not in frame_queues:
        frame_queues[robot_id] = asyncio.Queue(maxsize=1)
    return frame_queues[robot_id]


class RobotVideoTrack(VideoStreamTrack):
    def __init__(self, robot_id: str):
        super().__init__()
        self.robot_id = robot_id

    async def recv(self) -> VideoFrame:
        queue = get_frame_queue(self.robot_id)
        width, height, payload = await queue.get()
        array = np.frombuffer(payload, dtype=np.uint8).reshape((height, width, 4))
        frame = VideoFrame.from_ndarray(array, format="bgra")
        frame.pts, frame.time_base = await self.next_timestamp()
        return frame


def ensure_camera_subscription(robot_id: str) -> None:
    if robot_id in camera_subscriptions:
        return
    ros: roslibpy.Ros | None = getattr(app.state, "ros_client", None)
    if ros is None or not ros.is_connected:
        logger.warning("ROS bridge unavailable; cannot subscribe to %s camera", robot_id)
        return
    topic_name = f"/{robot_id}/camera/image_raw"
    topic = roslibpy.Topic(ros, topic_name, "sensor_msgs/msg/Image")

    def _callback(message: Dict[str, Any]) -> None:
        width = message.get("width")
        height = message.get("height")
        data = message.get("data")
        if width is None or height is None or data is None:
            return
        try:
            image_bytes = bytes(data)
        except (TypeError, ValueError):
            return
        queue = get_frame_queue(robot_id)
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        queue.put_nowait((width, height, image_bytes))

    try:
        topic.subscribe(_callback)
        camera_subscriptions[robot_id] = topic
        logger.info("Subscribed to %s camera topic", topic_name)
    except Exception as exc:  # pragma: no cover - rosbridge errors
        logger.warning("Failed to subscribe to %s: %s", topic_name, exc)
