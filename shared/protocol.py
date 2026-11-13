"""
Robot Control Protocol - Shared Type Definitions (Python)

This file defines the message schemas for communication between:
- Client (browser) - source of truth for desired state (inputs)
- Server (API) - relay, fan-out, persistence
- Robot - source of truth for actual state (sensors)

All messages are JSON-encoded and wrapped in a MessageEnvelope.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Literal, Optional, Union
import time


# ============================================================================
# ENUMS
# ============================================================================

class MessageType(str, Enum):
    # Client → Robot (via server)
    CONTROLLER_STATE = "controller_state"
    COMMAND = "command"
    # Robot → Client (via server)
    ROBOT_STATE = "robot_state"
    TELEMETRY = "telemetry"
    MAP_UPDATE = "map_update"
    ERROR = "error"
    # Server management
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    HEARTBEAT = "heartbeat"
    # Virtual World (Tauri ↔ API)
    GET_WORLD_STATE = "get_world_state"
    WORLD_STATE = "world_state"
    ADD_ELEMENT = "add_element"
    UPDATE_ELEMENT = "update_element"
    REMOVE_ELEMENT = "remove_element"
    ELEMENT_ADDED = "element_added"
    ELEMENT_UPDATED = "element_updated"
    ELEMENT_REMOVED = "element_removed"
    PLAYER_STATE = "player_state"
    VIRTUAL_PLAYER_CREATED = "virtual_player_created"
    VIRTUAL_PLAYER_DELETED = "virtual_player_deleted"


class InputSource(str, Enum):
    KEYBOARD = "keyboard"
    GAMEPAD = "gamepad"
    TOUCH = "touch"
    TILT = "tilt"
    MOUSE = "mouse"


class RobotMode(str, Enum):
    IDLE = "idle"
    MANUAL = "manual"
    AUTONOMOUS = "autonomous"
    MAPPING = "mapping"
    DOCKING = "docking"
    EMERGENCY_STOP = "emergency_stop"


class CommandAction(str, Enum):
    SET_MODE = "set_mode"
    CALIBRATE = "calibrate"
    HOME = "home"
    DOCK = "dock"
    EMERGENCY_STOP = "emergency_stop"
    CLEAR_ERRORS = "clear_errors"
    SET_WAYPOINT = "set_waypoint"
    CANCEL_WAYPOINT = "cancel_waypoint"
    START_MAPPING = "start_mapping"
    STOP_MAPPING = "stop_mapping"
    SAVE_MAP = "save_map"
    LOAD_MAP = "load_map"
    BEEP = "beep"
    SET_LED = "set_led"


# ============================================================================
# CONTROLLER STATE (Client → Robot)
# ============================================================================

@dataclass
class ControllerAxes:
    """Analog axes, all values -1.0 to 1.0 (triggers 0.0 to 1.0)"""
    left_x: float = 0.0
    left_y: float = 0.0
    right_x: float = 0.0
    right_y: float = 0.0
    left_trigger: float = 0.0
    right_trigger: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {
            "left_x": self.left_x,
            "left_y": self.left_y,
            "right_x": self.right_x,
            "right_y": self.right_y,
            "left_trigger": self.left_trigger,
            "right_trigger": self.right_trigger,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "ControllerAxes":
        return cls(
            left_x=d.get("left_x", 0.0),
            left_y=d.get("left_y", 0.0),
            right_x=d.get("right_x", 0.0),
            right_y=d.get("right_y", 0.0),
            left_trigger=d.get("left_trigger", 0.0),
            right_trigger=d.get("right_trigger", 0.0),
        )


@dataclass
class ControllerButtons:
    """Digital buttons, all boolean"""
    a: bool = False
    b: bool = False
    x: bool = False
    y: bool = False
    left_bumper: bool = False
    right_bumper: bool = False
    back: bool = False
    start: bool = False
    left_stick: bool = False
    right_stick: bool = False
    dpad_up: bool = False
    dpad_down: bool = False
    dpad_left: bool = False
    dpad_right: bool = False

    def to_dict(self) -> Dict[str, bool]:
        return {
            "a": self.a,
            "b": self.b,
            "x": self.x,
            "y": self.y,
            "left_bumper": self.left_bumper,
            "right_bumper": self.right_bumper,
            "back": self.back,
            "start": self.start,
            "left_stick": self.left_stick,
            "right_stick": self.right_stick,
            "dpad_up": self.dpad_up,
            "dpad_down": self.dpad_down,
            "dpad_left": self.dpad_left,
            "dpad_right": self.dpad_right,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "ControllerButtons":
        return cls(**{k: d.get(k, False) for k in cls.__dataclass_fields__})


@dataclass
class ControllerState:
    """Normalized gamepad-style input from any input device"""
    axes: ControllerAxes = field(default_factory=ControllerAxes)
    buttons: ControllerButtons = field(default_factory=ControllerButtons)
    source: InputSource = InputSource.KEYBOARD

    def to_dict(self) -> Dict:
        return {
            "axes": self.axes.to_dict(),
            "buttons": self.buttons.to_dict(),
            "source": self.source.value,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "ControllerState":
        return cls(
            axes=ControllerAxes.from_dict(d.get("axes", {})),
            buttons=ControllerButtons.from_dict(d.get("buttons", {})),
            source=InputSource(d.get("source", "keyboard")),
        )


# ============================================================================
# ROBOT STATE (Robot → Client)
# ============================================================================

@dataclass
class Pose3D:
    """Position and orientation in 3D space"""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "x": self.x, "y": self.y, "z": self.z,
            "roll": self.roll, "pitch": self.pitch, "yaw": self.yaw,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "Pose3D":
        return cls(**{k: d.get(k, 0.0) for k in cls.__dataclass_fields__})


@dataclass
class Velocity:
    """Linear and angular velocities"""
    linear_x: float = 0.0
    linear_y: float = 0.0
    linear_z: float = 0.0
    angular_x: float = 0.0
    angular_y: float = 0.0
    angular_z: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "linear_x": self.linear_x,
            "linear_y": self.linear_y,
            "linear_z": self.linear_z,
            "angular_x": self.angular_x,
            "angular_y": self.angular_y,
            "angular_z": self.angular_z,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "Velocity":
        return cls(**{k: d.get(k, 0.0) for k in cls.__dataclass_fields__})


@dataclass
class ServoState:
    """Servo positions (typically camera pan/tilt)"""
    camera_pan: float = 90.0   # degrees, 0-180
    camera_tilt: float = 90.0  # degrees, 0-180
    steering_angle: Optional[float] = None  # For Ackermann robots
    extra: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        d = {
            "camera_pan": self.camera_pan,
            "camera_tilt": self.camera_tilt,
        }
        if self.steering_angle is not None:
            d["steering_angle"] = self.steering_angle
        d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "ServoState":
        return cls(
            camera_pan=d.get("camera_pan", 90.0),
            camera_tilt=d.get("camera_tilt", 90.0),
            steering_angle=d.get("steering_angle"),
            extra={k: v for k, v in d.items()
                   if k not in ("camera_pan", "camera_tilt", "steering_angle")},
        )


@dataclass
class BatteryState:
    """Battery status"""
    voltage: float = 0.0
    percentage: Optional[float] = None
    charging: Optional[bool] = None
    cells: Optional[int] = None
    low_warning: bool = False

    def to_dict(self) -> Dict:
        d = {"voltage": self.voltage, "low_warning": self.low_warning}
        if self.percentage is not None:
            d["percentage"] = self.percentage
        if self.charging is not None:
            d["charging"] = self.charging
        if self.cells is not None:
            d["cells"] = self.cells
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "BatteryState":
        return cls(
            voltage=d.get("voltage", 0.0),
            percentage=d.get("percentage"),
            charging=d.get("charging"),
            cells=d.get("cells"),
            low_warning=d.get("low_warning", False),
        )


@dataclass
class RobotState:
    """Full robot state snapshot"""
    robot_id: str = ""
    robot_type: str = ""
    connected: bool = False
    last_seen: float = 0.0
    pose: Pose3D = field(default_factory=Pose3D)
    velocity: Velocity = field(default_factory=Velocity)
    servos: ServoState = field(default_factory=ServoState)
    battery: BatteryState = field(default_factory=BatteryState)
    mode: RobotMode = RobotMode.IDLE
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "robot_id": self.robot_id,
            "robot_type": self.robot_type,
            "connected": self.connected,
            "last_seen": self.last_seen,
            "pose": self.pose.to_dict(),
            "velocity": self.velocity.to_dict(),
            "servos": self.servos.to_dict(),
            "battery": self.battery.to_dict(),
            "mode": self.mode.value,
            "errors": self.errors,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "RobotState":
        return cls(
            robot_id=d.get("robot_id", ""),
            robot_type=d.get("robot_type", ""),
            connected=d.get("connected", False),
            last_seen=d.get("last_seen", 0.0),
            pose=Pose3D.from_dict(d.get("pose", {})),
            velocity=Velocity.from_dict(d.get("velocity", {})),
            servos=ServoState.from_dict(d.get("servos", {})),
            battery=BatteryState.from_dict(d.get("battery", {})),
            mode=RobotMode(d.get("mode", "idle")),
            errors=d.get("errors", []),
        )


# ============================================================================
# TELEMETRY (Robot → Client)
# ============================================================================

@dataclass
class IMUData:
    """Inertial measurement unit data"""
    accel_x: float = 0.0
    accel_y: float = 0.0
    accel_z: float = 0.0
    gyro_x: float = 0.0
    gyro_y: float = 0.0
    gyro_z: float = 0.0
    mag_x: Optional[float] = None
    mag_y: Optional[float] = None
    mag_z: Optional[float] = None

    def to_dict(self) -> Dict:
        d = {
            "accel_x": self.accel_x, "accel_y": self.accel_y, "accel_z": self.accel_z,
            "gyro_x": self.gyro_x, "gyro_y": self.gyro_y, "gyro_z": self.gyro_z,
        }
        if self.mag_x is not None:
            d.update({"mag_x": self.mag_x, "mag_y": self.mag_y, "mag_z": self.mag_z})
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "IMUData":
        return cls(
            accel_x=d.get("accel_x", 0.0),
            accel_y=d.get("accel_y", 0.0),
            accel_z=d.get("accel_z", 0.0),
            gyro_x=d.get("gyro_x", 0.0),
            gyro_y=d.get("gyro_y", 0.0),
            gyro_z=d.get("gyro_z", 0.0),
            mag_x=d.get("mag_x"),
            mag_y=d.get("mag_y"),
            mag_z=d.get("mag_z"),
        )


@dataclass
class Telemetry:
    """High-frequency sensor updates"""
    timestamp: float = field(default_factory=lambda: time.time() * 1000)
    imu: Optional[IMUData] = None
    encoders: Optional[Dict[str, float]] = None
    distance_sensors: Optional[Dict[str, float]] = None

    def to_dict(self) -> Dict:
        d = {"timestamp": self.timestamp}
        if self.imu:
            d["imu"] = self.imu.to_dict()
        if self.encoders:
            d["encoders"] = self.encoders
        if self.distance_sensors:
            d["distance_sensors"] = self.distance_sensors
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "Telemetry":
        return cls(
            timestamp=d.get("timestamp", time.time() * 1000),
            imu=IMUData.from_dict(d["imu"]) if "imu" in d else None,
            encoders=d.get("encoders"),
            distance_sensors=d.get("distance_sensors"),
        )


# ============================================================================
# MAP DATA (Robot → Client)
# ============================================================================

@dataclass
class MapUpdate:
    """SLAM occupancy grid update"""
    resolution: float = 0.05  # meters per cell
    width: int = 0
    height: int = 0
    origin_x: float = 0.0
    origin_y: float = 0.0
    origin_yaw: float = 0.0
    data: Union[List[int], str] = field(default_factory=list)
    encoding: Literal["raw", "base64", "rle"] = "raw"
    incremental: bool = False
    region: Optional[Dict[str, int]] = None  # {x, y, width, height}

    def to_dict(self) -> Dict:
        d = {
            "resolution": self.resolution,
            "width": self.width,
            "height": self.height,
            "origin": {
                "x": self.origin_x,
                "y": self.origin_y,
                "yaw": self.origin_yaw,
            },
            "data": self.data,
            "encoding": self.encoding,
            "incremental": self.incremental,
        }
        if self.region:
            d["region"] = self.region
        return d


# ============================================================================
# COMMANDS (Client → Robot)
# ============================================================================

@dataclass
class Command:
    """Discrete command (not continuous control)"""
    id: str
    action: CommandAction
    params: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "action": self.action.value,
            "params": self.params,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "Command":
        return cls(
            id=d.get("id", ""),
            action=CommandAction(d.get("action", "beep")),
            params=d.get("params", {}),
        )


@dataclass
class CommandAck:
    """Command acknowledgment"""
    command_id: str
    status: Literal["received", "executing", "completed", "failed"]
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        d = {"command_id": self.command_id, "status": self.status}
        if self.error:
            d["error"] = self.error
        return d


# ============================================================================
# MESSAGE ENVELOPE
# ============================================================================

@dataclass
class MessageEnvelope:
    """Wrapper for all protocol messages"""
    type: MessageType
    robot_id: str
    timestamp: float = field(default_factory=lambda: time.time() * 1000)
    seq: Optional[int] = None
    payload: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        d = {
            "type": self.type.value,
            "robot_id": self.robot_id,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }
        if self.seq is not None:
            d["seq"] = self.seq
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "MessageEnvelope":
        return cls(
            type=MessageType(d.get("type", "heartbeat")),
            robot_id=d.get("robot_id", ""),
            timestamp=d.get("timestamp", time.time() * 1000),
            seq=d.get("seq"),
            payload=d.get("payload", {}),
        )


# ============================================================================
# ROBOT CONTROL PROFILE
# Maps controller state to robot-specific actions
# ============================================================================

@dataclass
class RobotControlProfile:
    """
    Defines how a specific robot type interprets controller input.
    Each robot type can define its own mapping.
    """
    robot_type: str

    # Axis mappings: controller axis -> robot action
    # e.g., {"left_y": "throttle", "left_x": "steering"}
    axis_mappings: Dict[str, str] = field(default_factory=dict)

    # Axis scales and limits
    axis_scales: Dict[str, float] = field(default_factory=dict)  # multipliers
    axis_limits: Dict[str, tuple] = field(default_factory=dict)  # (min, max)

    # Button mappings: controller button -> command action
    button_mappings: Dict[str, str] = field(default_factory=dict)

    # Dead zones for axes
    dead_zones: Dict[str, float] = field(default_factory=dict)


# Example profile for Rosmaster A1 (Ackermann steering)
ROSMASTER_A1_PROFILE = RobotControlProfile(
    robot_type="rosmaster_a1",
    axis_mappings={
        "left_y": "throttle",       # Forward/back
        "left_x": "steering",       # Wheel angle
        "right_x": "camera_pan",    # Camera horizontal
        "right_y": "camera_tilt",   # Camera vertical
    },
    axis_scales={
        "throttle": 0.5,      # Max 0.5 m/s
        "steering": 30.0,     # Max 30 degrees steering
        "camera_pan": 90.0,   # 90 degree range from center
        "camera_tilt": 45.0,  # 45 degree range from center
    },
    axis_limits={
        "throttle": (-0.5, 0.5),
        "steering": (-30.0, 30.0),
        "camera_pan": (0, 180),
        "camera_tilt": (45, 135),
    },
    button_mappings={
        "a": "beep",
        "b": "emergency_stop",
        "start": "set_mode",
    },
    dead_zones={
        "throttle": 0.1,
        "steering": 0.1,
        "camera_pan": 0.05,
        "camera_tilt": 0.05,
    },
)


# ============================================================================
# VIRTUAL WORLD (Tauri Desktop App ↔ API)
# ============================================================================

class VirtualElementType(str, Enum):
    WALL = "wall"
    FRUIT = "fruit"
    NOTE = "note"
    OBSTACLE = "obstacle"


@dataclass
class VirtualWorldElement:
    """Static element in the virtual 3D world."""
    id: int = 0
    element_type: str = "wall"
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    width: Optional[float] = None
    height: Optional[float] = None
    depth: Optional[float] = None
    rotation: float = 0.0
    data: Optional[str] = None  # JSON for element-specific data

    def to_dict(self) -> Dict:
        d = {
            "id": self.id,
            "element_type": self.element_type,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "rotation": self.rotation,
        }
        if self.width is not None:
            d["width"] = self.width
        if self.height is not None:
            d["height"] = self.height
        if self.depth is not None:
            d["depth"] = self.depth
        if self.data is not None:
            d["data"] = self.data
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "VirtualWorldElement":
        return cls(
            id=d.get("id", 0),
            element_type=d.get("element_type", "wall"),
            x=d.get("x", 0.0),
            y=d.get("y", 0.0),
            z=d.get("z", 0.0),
            width=d.get("width"),
            height=d.get("height"),
            depth=d.get("depth"),
            rotation=d.get("rotation", 0.0),
            data=d.get("data"),
        )


@dataclass
class VirtualPlayer:
    """Virtual player (cube) that can be spawned and controlled in the virtual world."""
    id: int = 0
    namespace: str = ""
    name: str = ""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.5
    yaw: float = 0.0
    color: str = "#3b82f6"

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "namespace": self.namespace,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "yaw": self.yaw,
            "color": self.color,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "VirtualPlayer":
        return cls(
            id=d.get("id", 0),
            namespace=d.get("namespace", ""),
            name=d.get("name", ""),
            x=d.get("x", 0.0),
            y=d.get("y", 0.0),
            z=d.get("z", 0.5),
            yaw=d.get("yaw", 0.0),
            color=d.get("color", "#3b82f6"),
        )


@dataclass
class VirtualWorldState:
    """Full state of the virtual world for a lobby."""
    lobby_id: int = 0
    elements: List[VirtualWorldElement] = field(default_factory=list)
    players: List[VirtualPlayer] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "lobby_id": self.lobby_id,
            "elements": [e.to_dict() for e in self.elements],
            "players": [p.to_dict() for p in self.players],
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "VirtualWorldState":
        return cls(
            lobby_id=d.get("lobby_id", 0),
            elements=[VirtualWorldElement.from_dict(e) for e in d.get("elements", [])],
            players=[VirtualPlayer.from_dict(p) for p in d.get("players", [])],
        )
