# Shared Protocol Definitions

This directory contains the shared type definitions for the robot control protocol.

## Files

- `protocol.ts` - TypeScript definitions for the web client
- `protocol.py` - Python definitions for the server and robot

## Architecture

```
┌─────────────┐         ┌─────────────┐         ┌─────────────┐
│   CLIENT    │         │   SERVER    │         │    ROBOT    │
│  (browser)  │         │   (API)     │         │             │
├─────────────┤         ├─────────────┤         ├─────────────┤
│             │         │             │         │             │
│ Desired     │────────►│   Relay     │────────►│ Execute     │
│ State       │ WS/RTC  │   Fan-out   │  WS     │ Commands    │
│ (inputs)    │         │   Store     │         │             │
│             │         │             │         │             │
│ Actual      │◄────────│             │◄────────│ Actual      │
│ State       │ WS/RTC  │             │  WS     │ State       │
│ (display)   │         │             │         │ (sensors)   │
│             │         │             │         │             │
└─────────────┘         └─────────────┘         └─────────────┘
```

## Key Concepts

### ControllerState

Normalized gamepad-style input that works across all input devices:
- Keyboard (WASD, arrows)
- Gamepad (Xbox, PlayStation)
- Touch (virtual joystick)
- Phone tilt (accelerometer)

All inputs are mapped to the same structure, so the robot doesn't need to know the input source.

### RobotState

Actual state from the robot's sensors:
- Pose (position + orientation)
- Velocity
- Servo positions
- Battery status
- Operating mode
- Errors

### RobotControlProfile

Defines how a specific robot type interprets controller input. Each robot maps controller axes/buttons to its own hardware:

| Robot Type | left_y | left_x | right_x | right_y |
|------------|--------|--------|---------|---------|
| Ackermann  | throttle | steering | cam pan | cam tilt |
| Differential | linear | - | angular | - |
| Drone | altitude | yaw | roll | pitch |
| Arm | base | - | gripper | elbow |

## Message Types

### Client → Robot
- `controller_state` - Continuous input (20-50 Hz)
- `command` - Discrete actions (set mode, calibrate, etc.)

### Robot → Client
- `robot_state` - Full state snapshot (1-10 Hz)
- `telemetry` - High-frequency sensor data (10-50 Hz)
- `map_update` - SLAM occupancy grid (on change)
- `error` - Error notifications

### Server Management
- `subscribe` / `unsubscribe` - Topic subscription
- `heartbeat` - Keep-alive

## Update Rates

| Data | Rate | Transport | Reliability |
|------|------|-----------|-------------|
| Controller | 20-50 Hz | DataChannel/WS | Unreliable OK |
| Video | 30 fps | WebRTC media | Unreliable OK |
| Audio | Continuous | WebRTC media | Unreliable OK |
| Telemetry | 10-50 Hz | WS | Unreliable OK |
| Robot state | 1-10 Hz | WS | Reliable |
| Map | On change | WS | Reliable |
| Commands | On demand | WS | Reliable |
