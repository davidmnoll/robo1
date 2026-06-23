/**
 * Robot Control Protocol - Shared Type Definitions
 *
 * This file defines the message schemas for communication between:
 * - Client (browser) - source of truth for desired state (inputs)
 * - Server (API) - relay, fan-out, persistence
 * - Robot - source of truth for actual state (sensors)
 *
 * All messages are JSON-encoded and wrapped in a MessageEnvelope.
 */

// ============================================================================
// MESSAGE ENVELOPE
// ============================================================================

export interface MessageEnvelope {
  type: MessageType;
  robot_id: string;
  timestamp: number;  // Unix timestamp in milliseconds
  seq?: number;       // Optional sequence number for ordering
  payload: unknown;   // Type depends on message type
}

export type MessageType =
  // Client → Robot (via server)
  | 'controller_state'
  | 'command'
  // Robot → Client (via server)
  | 'robot_state'
  | 'telemetry'
  | 'map_update'
  | 'error'
  // Server management
  | 'subscribe'
  | 'unsubscribe'
  | 'heartbeat';

// ============================================================================
// CONTROLLER STATE (Client → Robot)
// Normalized gamepad-style input that works across all input devices
// ============================================================================

export interface ControllerState {
  // Analog axes (-1.0 to 1.0)
  axes: {
    left_x: number;       // Left stick horizontal (strafe/steering)
    left_y: number;       // Left stick vertical (forward/back)
    right_x: number;      // Right stick horizontal (camera pan/turn)
    right_y: number;      // Right stick vertical (camera tilt)
    left_trigger: number; // 0.0 to 1.0 (brake/reverse modifier)
    right_trigger: number; // 0.0 to 1.0 (throttle/boost)
  };

  // Digital buttons (true = pressed)
  buttons: {
    a: boolean;           // Primary action
    b: boolean;           // Secondary action / cancel
    x: boolean;           // Tertiary action
    y: boolean;           // Quaternary action
    left_bumper: boolean;
    right_bumper: boolean;
    back: boolean;        // Select/back
    start: boolean;       // Start/menu
    left_stick: boolean;  // L3
    right_stick: boolean; // R3
    dpad_up: boolean;
    dpad_down: boolean;
    dpad_left: boolean;
    dpad_right: boolean;
  };

  // Input source metadata
  source: InputSource;
}

export type InputSource =
  | 'keyboard'
  | 'gamepad'
  | 'touch'      // Virtual joystick
  | 'tilt'       // Phone accelerometer
  | 'mouse';

// Default/zero controller state
export const ZERO_CONTROLLER_STATE: ControllerState = {
  axes: {
    left_x: 0, left_y: 0,
    right_x: 0, right_y: 0,
    left_trigger: 0, right_trigger: 0,
  },
  buttons: {
    a: false, b: false, x: false, y: false,
    left_bumper: false, right_bumper: false,
    back: false, start: false,
    left_stick: false, right_stick: false,
    dpad_up: false, dpad_down: false,
    dpad_left: false, dpad_right: false,
  },
  source: 'keyboard',
};

// ============================================================================
// ROBOT STATE (Robot → Client)
// Actual state from sensors and hardware
// ============================================================================

export interface RobotState {
  // Identity
  robot_id: string;
  robot_type: string;  // e.g., 'rosmaster_a1', 'turtlebot3', 'drone'

  // Connection
  connected: boolean;
  last_seen: number;  // Unix timestamp ms

  // Pose (from odometry/SLAM)
  pose: Pose3D;

  // Velocity (current motion)
  velocity: Velocity;

  // Hardware state
  servos: ServoState;
  battery: BatteryState;

  // Status
  mode: RobotMode;
  errors: string[];
}

export interface Pose3D {
  // Position (meters, relative to origin/map frame)
  x: number;
  y: number;
  z: number;

  // Orientation (radians)
  roll: number;   // Rotation around X
  pitch: number;  // Rotation around Y
  yaw: number;    // Rotation around Z (heading)
}

export interface Velocity {
  // Linear velocity (m/s)
  linear_x: number;  // Forward/back
  linear_y: number;  // Left/right (for omnidirectional)
  linear_z: number;  // Up/down (for drones)

  // Angular velocity (rad/s)
  angular_x: number;  // Roll rate
  angular_y: number;  // Pitch rate
  angular_z: number;  // Yaw rate (turn rate)
}

export interface ServoState {
  // Camera servos (degrees, 0-180)
  camera_pan: number;
  camera_tilt: number;

  // Steering (for Ackermann, degrees or normalized)
  steering_angle?: number;

  // Additional servos (robot-specific)
  [key: string]: number | undefined;
}

export interface BatteryState {
  voltage: number;        // Volts
  percentage?: number;    // 0-100 if known
  charging?: boolean;
  cells?: number;         // Number of cells (e.g., 3S = 3)
  low_warning?: boolean;  // True if below threshold
}

export type RobotMode =
  | 'idle'
  | 'manual'        // Human control via controller
  | 'autonomous'    // Self-driving / following waypoints
  | 'mapping'       // SLAM mapping mode
  | 'docking'       // Returning to dock
  | 'emergency_stop';

// ============================================================================
// TELEMETRY (Robot → Client)
// High-frequency sensor updates (sent separately from full state)
// ============================================================================

export interface Telemetry {
  timestamp: number;

  // IMU data
  imu?: IMUData;

  // Wheel encoders
  encoders?: EncoderData;

  // Distance sensors
  distance_sensors?: DistanceSensorData;
}

export interface IMUData {
  // Acceleration (m/s²)
  accel_x: number;
  accel_y: number;
  accel_z: number;

  // Gyroscope (rad/s)
  gyro_x: number;
  gyro_y: number;
  gyro_z: number;

  // Magnetometer (if available)
  mag_x?: number;
  mag_y?: number;
  mag_z?: number;
}

export interface EncoderData {
  // Wheel velocities (rad/s or m/s depending on robot)
  left: number;
  right: number;
  // For omnidirectional
  front_left?: number;
  front_right?: number;
  rear_left?: number;
  rear_right?: number;
}

export interface DistanceSensorData {
  // Distance readings (meters), undefined if no obstacle detected
  front?: number;
  rear?: number;
  left?: number;
  right?: number;
  // Array for LIDAR scan (if available)
  scan?: number[];
  scan_angle_min?: number;  // radians
  scan_angle_max?: number;
  scan_angle_increment?: number;
}

// ============================================================================
// MAP DATA (Robot → Client)
// SLAM occupancy grid, sent on change or periodically
// ============================================================================

export interface MapUpdate {
  // Map metadata
  resolution: number;  // Meters per cell
  width: number;       // Cells
  height: number;      // Cells

  // Origin (position of cell 0,0 in world frame)
  origin: {
    x: number;
    y: number;
    yaw: number;
  };

  // Occupancy data: -1 = unknown, 0 = free, 100 = occupied
  // Encoded as base64 for efficiency, or as array for simplicity
  data: number[] | string;
  encoding?: 'raw' | 'base64' | 'rle';  // Run-length encoding for compression

  // Incremental update (if only part of map changed)
  incremental?: boolean;
  region?: { x: number; y: number; width: number; height: number };
}

// ============================================================================
// COMMANDS (Client → Robot)
// Discrete commands (not continuous control)
// ============================================================================

export interface Command {
  id: string;          // Unique command ID for acknowledgment
  action: CommandAction;
  params?: Record<string, unknown>;
}

export type CommandAction =
  | 'set_mode'         // params: { mode: RobotMode }
  | 'calibrate'        // params: { sensor: string }
  | 'home'             // Return to origin
  | 'dock'             // Return to charging dock
  | 'emergency_stop'
  | 'clear_errors'
  | 'set_waypoint'     // params: { x, y, yaw }
  | 'cancel_waypoint'
  | 'start_mapping'
  | 'stop_mapping'
  | 'save_map'         // params: { name: string }
  | 'load_map'         // params: { name: string }
  | 'beep'             // params: { duration_ms: number }
  | 'set_led';         // params: { r, g, b } or { pattern: string }

export interface CommandAck {
  command_id: string;
  status: 'received' | 'executing' | 'completed' | 'failed';
  error?: string;
}

// ============================================================================
// KEYBOARD MAPPING
// Default keyboard to controller state mapping
// ============================================================================

export interface KeyMapping {
  key: string;
  axis?: keyof ControllerState['axes'];
  axisValue?: number;  // Value when pressed (-1, 1, etc.)
  button?: keyof ControllerState['buttons'];
}

export const DEFAULT_KEY_MAPPINGS: KeyMapping[] = [
  // WASD → Left stick
  { key: 'w', axis: 'left_y', axisValue: 1.0 },
  { key: 's', axis: 'left_y', axisValue: -1.0 },
  { key: 'a', axis: 'left_x', axisValue: -1.0 },
  { key: 'd', axis: 'left_x', axisValue: 1.0 },

  // Arrow keys → Right stick
  { key: 'ArrowUp', axis: 'right_y', axisValue: 1.0 },
  { key: 'ArrowDown', axis: 'right_y', axisValue: -1.0 },
  { key: 'ArrowLeft', axis: 'right_x', axisValue: -1.0 },
  { key: 'ArrowRight', axis: 'right_x', axisValue: 1.0 },

  // Triggers
  { key: 'Shift', axis: 'left_trigger', axisValue: 1.0 },
  { key: ' ', axis: 'right_trigger', axisValue: 1.0 },  // Space

  // Buttons
  { key: 'e', button: 'a' },
  { key: 'q', button: 'b' },
  { key: 'r', button: 'x' },
  { key: 'f', button: 'y' },
  { key: 'Tab', button: 'back' },
  { key: 'Escape', button: 'start' },
  { key: '1', button: 'dpad_left' },
  { key: '2', button: 'dpad_up' },
  { key: '3', button: 'dpad_down' },
  { key: '4', button: 'dpad_right' },
];

// ============================================================================
// GAMEPAD MAPPING
// Standard gamepad axis/button indices to our named fields
// Based on "Standard Gamepad" mapping (Xbox-style layout)
// https://w3c.github.io/gamepad/#remapping
// ============================================================================

export const STANDARD_GAMEPAD_MAPPING = {
  axes: {
    0: 'left_x',      // Left stick X
    1: 'left_y',      // Left stick Y (often inverted)
    2: 'right_x',     // Right stick X
    3: 'right_y',     // Right stick Y (often inverted)
  },
  buttons: {
    0: 'a',           // A (bottom)
    1: 'b',           // B (right)
    2: 'x',           // X (left)
    3: 'y',           // Y (top)
    4: 'left_bumper',
    5: 'right_bumper',
    6: 'left_trigger',  // Analog, use value
    7: 'right_trigger', // Analog, use value
    8: 'back',
    9: 'start',
    10: 'left_stick',
    11: 'right_stick',
    12: 'dpad_up',
    13: 'dpad_down',
    14: 'dpad_left',
    15: 'dpad_right',
  },
  // Axes that need Y inversion (push up = negative by default)
  invertY: [1, 3],
} as const;
