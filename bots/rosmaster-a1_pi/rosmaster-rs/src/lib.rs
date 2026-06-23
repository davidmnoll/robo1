//! Rust driver for Yahboom Rosmaster robot control board
//!
//! This library provides a clean interface to control Rosmaster robots
//! via serial communication with the STM32-based control board.
//!
//! # Example
//! ```no_run
//! use rosmaster::Rosmaster;
//!
//! let mut bot = Rosmaster::open("/dev/ttyUSB0", 9).unwrap(); // car_type 9 = A1
//! bot.set_car_motion(0.5, 0.0, 0.0).unwrap(); // Move forward
//! bot.set_pwm_servo(1, 90).unwrap(); // Center camera pan
//! ```

use parking_lot::Mutex;
use serialport::SerialPort;
use std::io::{Read, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;
use thiserror::Error;

// Protocol constants
const HEAD: u8 = 0xFF;
const DEVICE_ID: u8 = 0xFC;
const COMPLEMENT: u16 = 257 - DEVICE_ID as u16;

// Function codes
mod func {
    pub const AUTO_REPORT: u8 = 0x01;
    pub const BEEP: u8 = 0x02;
    pub const PWM_SERVO: u8 = 0x03;
    pub const PWM_SERVO_ALL: u8 = 0x04;
    pub const RGB: u8 = 0x05;
    pub const RGB_EFFECT: u8 = 0x06;
    pub const REPORT_SPEED: u8 = 0x0A;
    pub const REPORT_MPU_RAW: u8 = 0x0B;
    pub const REPORT_IMU_ATT: u8 = 0x0C;
    pub const REPORT_ENCODER: u8 = 0x0D;
    pub const REPORT_ICM_RAW: u8 = 0x0E;
    pub const MOTOR: u8 = 0x10;
    pub const CAR_RUN: u8 = 0x11;
    pub const MOTION: u8 = 0x12;
    pub const SET_CAR_TYPE: u8 = 0x15;
    pub const UART_SERVO: u8 = 0x20;
    pub const UART_SERVO_TORQUE: u8 = 0x22;
    pub const AKM_STEER_ANGLE: u8 = 0x31;
    pub const RESET_STATE: u8 = 0x0F;
}

/// Car types supported by Rosmaster
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum CarType {
    X3 = 1,
    X3Plus = 2,
    X1 = 4,
    R2 = 5,
    A1 = 9,
}

impl From<u8> for CarType {
    fn from(v: u8) -> Self {
        match v {
            1 => CarType::X3,
            2 => CarType::X3Plus,
            4 => CarType::X1,
            5 => CarType::R2,
            9 => CarType::A1,
            _ => CarType::X3,
        }
    }
}

/// Errors that can occur when communicating with Rosmaster
#[derive(Error, Debug)]
pub enum Error {
    #[error("Serial port error: {0}")]
    Serial(#[from] serialport::Error),

    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),

    #[error("Invalid parameter: {0}")]
    InvalidParam(String),

    #[error("Timeout waiting for response")]
    Timeout,
}

pub type Result<T> = std::result::Result<T, Error>;

/// IMU data from the robot
#[derive(Debug, Clone, Copy, Default)]
pub struct ImuData {
    /// Accelerometer (m/s²)
    pub accel: [f32; 3],
    /// Gyroscope (rad/s)
    pub gyro: [f32; 3],
    /// Magnetometer
    pub mag: [f32; 3],
    /// Attitude angles (roll, pitch, yaw) in radians
    pub attitude: [f32; 3],
}

/// Motion data from the robot
#[derive(Debug, Clone, Copy, Default)]
pub struct MotionData {
    /// Velocity (vx, vy, vz) in m/s
    pub velocity: [f32; 3],
    /// Battery voltage
    pub battery_voltage: f32,
}

/// Encoder data from the robot
#[derive(Debug, Clone, Copy, Default)]
pub struct EncoderData {
    pub m1: i32,
    pub m2: i32,
    pub m3: i32,
    pub m4: i32,
}

/// Shared state updated by the receiver thread
#[derive(Default)]
struct SharedState {
    imu: ImuData,
    motion: MotionData,
    encoder: EncoderData,
}

/// Rosmaster robot controller
pub struct Rosmaster {
    port: Box<dyn SerialPort>,
    car_type: u8,
    delay: Duration,
    state: Arc<Mutex<SharedState>>,
    receiver_running: Arc<AtomicBool>,
    _receiver_handle: Option<thread::JoinHandle<()>>,
}

impl Rosmaster {
    /// Open a connection to the Rosmaster control board
    ///
    /// # Arguments
    /// * `port_name` - Serial port path (e.g., "/dev/ttyUSB0" or "/dev/myserial")
    /// * `car_type` - Robot type (use 9 for A1)
    pub fn open(port_name: &str, car_type: u8) -> Result<Self> {
        let port = serialport::new(port_name, 115200)
            .timeout(Duration::from_millis(100))
            .open()?;

        tracing::info!("Rosmaster serial opened: {}", port_name);

        let mut bot = Self {
            port,
            car_type,
            delay: Duration::from_micros(2000),
            state: Arc::new(Mutex::new(SharedState::default())),
            receiver_running: Arc::new(AtomicBool::new(false)),
            _receiver_handle: None,
        };

        // Enable servo torque
        bot.set_uart_servo_torque(true)?;

        Ok(bot)
    }

    /// Start the background receiver thread for auto-reported data
    pub fn start_receiver(&mut self) -> Result<()> {
        if self.receiver_running.load(Ordering::SeqCst) {
            return Ok(());
        }

        let port = self.port.try_clone()?;
        let state = Arc::clone(&self.state);
        let running = Arc::clone(&self.receiver_running);

        running.store(true, Ordering::SeqCst);

        let handle = thread::spawn(move || {
            receive_loop(port, state, running);
        });

        self._receiver_handle = Some(handle);
        tracing::info!("Receiver thread started");

        Ok(())
    }

    /// Stop the background receiver thread
    pub fn stop_receiver(&mut self) {
        self.receiver_running.store(false, Ordering::SeqCst);
        if let Some(handle) = self._receiver_handle.take() {
            let _ = handle.join();
        }
    }

    // =========================================================================
    // Motion Control
    // =========================================================================

    /// Set car motion velocity
    ///
    /// # Arguments
    /// * `vx` - Forward/backward velocity (m/s). A1: [-1.8, 1.8]
    /// * `vy` - Steering angle for Ackermann (radians). A1: [-0.045, 0.045]
    /// * `vz` - Angular velocity (rad/s). A1: [-3, 3]
    pub fn set_car_motion(&mut self, vx: f32, vy: f32, vz: f32) -> Result<()> {
        let vx_i = (vx * 1000.0) as i16;
        let vy_i = (vy * 1000.0) as i16;
        let vz_i = (vz * 1000.0) as i16;

        let mut cmd = vec![
            HEAD,
            DEVICE_ID,
            0x00, // length placeholder
            func::MOTION,
            self.car_type,
            (vx_i & 0xFF) as u8,
            (vx_i >> 8) as u8,
            (vy_i & 0xFF) as u8,
            (vy_i >> 8) as u8,
            (vz_i & 0xFF) as u8,
            (vz_i >> 8) as u8,
        ];
        self.send_cmd(&mut cmd)
    }

    /// Stop the car
    pub fn stop(&mut self) -> Result<()> {
        self.set_car_motion(0.0, 0.0, 0.0)
    }

    /// Set car run state (simpler motion control)
    ///
    /// # Arguments
    /// * `state` - 0=stop, 1=forward, 2=backward, 3=left, 4=right, 5=spin left, 6=spin right
    /// * `speed` - Speed value [-100, 100]
    pub fn set_car_run(&mut self, state: u8, speed: i16) -> Result<()> {
        let mut cmd = vec![
            HEAD,
            DEVICE_ID,
            0x00,
            func::CAR_RUN,
            self.car_type,
            state,
            (speed & 0xFF) as u8,
            (speed >> 8) as u8,
        ];
        self.send_cmd(&mut cmd)
    }

    /// Set individual motor PWM speeds
    ///
    /// # Arguments
    /// * `m1, m2, m3, m4` - Motor speeds [-100, 100], 127 = no change
    pub fn set_motor(&mut self, m1: i8, m2: i8, m3: i8, m4: i8) -> Result<()> {
        let mut cmd = vec![
            HEAD,
            DEVICE_ID,
            0x00,
            func::MOTOR,
            m1 as u8,
            m2 as u8,
            m3 as u8,
            m4 as u8,
        ];
        self.send_cmd(&mut cmd)
    }

    // =========================================================================
    // Servo Control (PWM servos for camera pan/tilt)
    // =========================================================================

    /// Set PWM servo angle
    ///
    /// # Arguments
    /// * `servo_id` - Servo ID [1-4]
    /// * `angle` - Angle [0-180]
    pub fn set_pwm_servo(&mut self, servo_id: u8, angle: u8) -> Result<()> {
        if !(1..=4).contains(&servo_id) {
            return Err(Error::InvalidParam("servo_id must be 1-4".into()));
        }
        let angle = angle.min(180);

        let mut cmd = vec![HEAD, DEVICE_ID, 0x00, func::PWM_SERVO, servo_id, angle];
        self.send_cmd(&mut cmd)
    }

    /// Set all PWM servo angles at once
    ///
    /// # Arguments
    /// * `angles` - Array of 4 angles [0-180], use 255 to skip a servo
    pub fn set_pwm_servo_all(&mut self, angles: [u8; 4]) -> Result<()> {
        let mut cmd = vec![
            HEAD,
            DEVICE_ID,
            0x00,
            func::PWM_SERVO_ALL,
            angles[0].min(180),
            angles[1].min(180),
            angles[2].min(180),
            angles[3].min(180),
        ];
        self.send_cmd(&mut cmd)
    }

    // =========================================================================
    // Ackermann Steering (for A1/R2)
    // =========================================================================

    /// Set Ackermann steering angle relative to default
    ///
    /// # Arguments
    /// * `angle` - Steering angle [-45, 45] degrees, negative=left, positive=right
    pub fn set_steering_angle(&mut self, angle: i8) -> Result<()> {
        let angle = angle.clamp(-45, 45);
        let mut cmd = vec![
            HEAD,
            DEVICE_ID,
            0x00,
            func::AKM_STEER_ANGLE,
            0x01, // servo ID
            angle as u8,
        ];
        self.send_cmd(&mut cmd)
    }

    // =========================================================================
    // Buzzer & LEDs
    // =========================================================================

    /// Control the buzzer
    ///
    /// # Arguments
    /// * `time_ms` - 0=off, 1=continuous, >=10=beep for N ms (multiple of 10)
    pub fn set_beep(&mut self, time_ms: u16) -> Result<()> {
        let mut cmd = vec![
            HEAD,
            DEVICE_ID,
            0x05,
            func::BEEP,
            (time_ms & 0xFF) as u8,
            (time_ms >> 8) as u8,
        ];
        self.send_cmd(&mut cmd)
    }

    /// Set RGB LED color
    ///
    /// # Arguments
    /// * `led_id` - LED ID [0-13], or 0xFF for all LEDs
    /// * `r, g, b` - Color values [0-255]
    pub fn set_rgb(&mut self, led_id: u8, r: u8, g: u8, b: u8) -> Result<()> {
        let mut cmd = vec![HEAD, DEVICE_ID, 0x00, func::RGB, led_id, r, g, b];
        self.send_cmd(&mut cmd)
    }

    /// Set RGB LED effect
    ///
    /// # Arguments
    /// * `effect` - 0=off, 1=flow, 2=marquee, 3=breathing, 4=gradient, 5=stars, 6=battery
    /// * `speed` - Speed [1-10], lower=faster
    pub fn set_rgb_effect(&mut self, effect: u8, speed: u8) -> Result<()> {
        let mut cmd = vec![HEAD, DEVICE_ID, 0x00, func::RGB_EFFECT, effect, speed, 255];
        self.send_cmd(&mut cmd)
    }

    // =========================================================================
    // Bus Servo Control (for robot arm)
    // =========================================================================

    /// Set bus servo torque enable
    pub fn set_uart_servo_torque(&mut self, enable: bool) -> Result<()> {
        let mut cmd = vec![
            HEAD,
            DEVICE_ID,
            0x04,
            func::UART_SERVO_TORQUE,
            if enable { 1 } else { 0 },
        ];
        self.send_cmd(&mut cmd)
    }

    /// Set bus servo position
    ///
    /// # Arguments
    /// * `servo_id` - Servo ID [1-255], 254=all servos
    /// * `pulse` - Position [96-4000]
    /// * `run_time_ms` - Movement time [0-2000] ms
    pub fn set_uart_servo(&mut self, servo_id: u8, pulse: u16, run_time_ms: u16) -> Result<()> {
        let pulse = pulse.clamp(96, 4000);
        let run_time = run_time_ms.min(2000);

        let mut cmd = vec![
            HEAD,
            DEVICE_ID,
            0x00,
            func::UART_SERVO,
            servo_id,
            (pulse & 0xFF) as u8,
            (pulse >> 8) as u8,
            (run_time & 0xFF) as u8,
            (run_time >> 8) as u8,
        ];
        self.send_cmd(&mut cmd)
    }

    // =========================================================================
    // Configuration
    // =========================================================================

    /// Set car type
    pub fn set_car_type(&mut self, car_type: CarType) -> Result<()> {
        self.car_type = car_type as u8;
        let mut cmd = vec![
            HEAD,
            DEVICE_ID,
            0x00,
            func::SET_CAR_TYPE,
            self.car_type,
            0x5F, // permanent save
        ];
        self.send_cmd(&mut cmd)?;
        thread::sleep(Duration::from_millis(100));
        Ok(())
    }

    /// Enable/disable auto-reporting of sensor data
    pub fn set_auto_report(&mut self, enable: bool, permanent: bool) -> Result<()> {
        let mut cmd = vec![
            HEAD,
            DEVICE_ID,
            0x05,
            func::AUTO_REPORT,
            if enable { 1 } else { 0 },
            if permanent { 0x5F } else { 0 },
        ];
        self.send_cmd(&mut cmd)
    }

    /// Reset car state (stop, lights off, buzzer off)
    pub fn reset_state(&mut self) -> Result<()> {
        let mut cmd = vec![HEAD, DEVICE_ID, 0x04, func::RESET_STATE, 0x5F];
        self.send_cmd(&mut cmd)
    }

    // =========================================================================
    // Data Getters
    // =========================================================================

    /// Get the latest IMU data
    pub fn get_imu_data(&self) -> ImuData {
        self.state.lock().imu
    }

    /// Get the latest motion data (velocity + battery)
    pub fn get_motion_data(&self) -> MotionData {
        self.state.lock().motion
    }

    /// Get the latest encoder data
    pub fn get_encoder_data(&self) -> EncoderData {
        self.state.lock().encoder
    }

    /// Get battery voltage
    pub fn get_battery_voltage(&self) -> f32 {
        self.state.lock().motion.battery_voltage
    }

    // =========================================================================
    // Internal
    // =========================================================================

    fn send_cmd(&mut self, cmd: &mut Vec<u8>) -> Result<()> {
        // Set length field
        cmd[2] = (cmd.len() - 1) as u8;

        // Calculate checksum
        let checksum = cmd[2..].iter().fold(COMPLEMENT, |acc, &b| acc + b as u16) & 0xFF;
        cmd.push(checksum as u8);

        // Send
        self.port.write_all(cmd)?;
        self.port.flush()?;

        tracing::trace!("TX: {:02X?}", cmd);

        thread::sleep(self.delay);
        Ok(())
    }
}

impl Drop for Rosmaster {
    fn drop(&mut self) {
        // Stop the robot before closing
        let _ = self.stop();
        self.stop_receiver();
        tracing::info!("Rosmaster closed");
    }
}

// =============================================================================
// Receiver Thread
// =============================================================================

fn receive_loop(
    mut port: Box<dyn SerialPort>,
    state: Arc<Mutex<SharedState>>,
    running: Arc<AtomicBool>,
) {
    let mut buf = [0u8; 256];

    while running.load(Ordering::SeqCst) {
        // Read one byte at a time looking for header
        if port.read_exact(&mut buf[0..1]).is_err() {
            continue;
        }

        if buf[0] != HEAD {
            continue;
        }

        // Read device ID
        if port.read_exact(&mut buf[1..2]).is_err() {
            continue;
        }

        if buf[1] != DEVICE_ID - 1 {
            continue;
        }

        // Read length
        if port.read_exact(&mut buf[2..3]).is_err() {
            continue;
        }

        let len = buf[2] as usize;
        if len < 2 || len > 200 {
            continue;
        }

        // Read function code and data
        let data_len = len - 1; // excluding checksum
        if port.read_exact(&mut buf[3..3 + data_len]).is_err() {
            continue;
        }

        let func_code = buf[3];
        let data = &buf[4..3 + data_len - 1];
        let rx_checksum = buf[3 + data_len - 1];

        // Verify checksum
        let calc_checksum = buf[2..3 + data_len - 1]
            .iter()
            .fold(0u16, |acc, &b| acc + b as u16)
            & 0xFF;

        if calc_checksum as u8 != rx_checksum {
            tracing::trace!("Checksum error: calc={:02X} rx={:02X}", calc_checksum, rx_checksum);
            continue;
        }

        // Parse data
        parse_frame(func_code, data, &state);
    }
}

fn parse_frame(func: u8, data: &[u8], state: &Arc<Mutex<SharedState>>) {
    match func {
        func::REPORT_SPEED => {
            if data.len() >= 7 {
                let mut s = state.lock();
                s.motion.velocity[0] = i16::from_le_bytes([data[0], data[1]]) as f32 / 1000.0;
                s.motion.velocity[1] = i16::from_le_bytes([data[2], data[3]]) as f32 / 1000.0;
                s.motion.velocity[2] = i16::from_le_bytes([data[4], data[5]]) as f32 / 1000.0;
                s.motion.battery_voltage = data[6] as f32 / 10.0;
            }
        }
        func::REPORT_MPU_RAW | func::REPORT_ICM_RAW => {
            if data.len() >= 18 {
                let gyro_ratio = if func == func::REPORT_MPU_RAW {
                    1.0 / 3754.9
                } else {
                    1.0 / 1000.0
                };
                let accel_ratio = if func == func::REPORT_MPU_RAW {
                    1.0 / 1671.84
                } else {
                    1.0 / 1000.0
                };

                let mut s = state.lock();
                s.imu.gyro[0] = i16::from_le_bytes([data[0], data[1]]) as f32 * gyro_ratio;
                s.imu.gyro[1] = i16::from_le_bytes([data[2], data[3]]) as f32 * -gyro_ratio;
                s.imu.gyro[2] = i16::from_le_bytes([data[4], data[5]]) as f32 * -gyro_ratio;
                s.imu.accel[0] = i16::from_le_bytes([data[6], data[7]]) as f32 * accel_ratio;
                s.imu.accel[1] = i16::from_le_bytes([data[8], data[9]]) as f32 * accel_ratio;
                s.imu.accel[2] = i16::from_le_bytes([data[10], data[11]]) as f32 * accel_ratio;
                s.imu.mag[0] = i16::from_le_bytes([data[12], data[13]]) as f32;
                s.imu.mag[1] = i16::from_le_bytes([data[14], data[15]]) as f32;
                s.imu.mag[2] = i16::from_le_bytes([data[16], data[17]]) as f32;
            }
        }
        func::REPORT_IMU_ATT => {
            if data.len() >= 6 {
                let mut s = state.lock();
                s.imu.attitude[0] = i16::from_le_bytes([data[0], data[1]]) as f32 / 10000.0;
                s.imu.attitude[1] = i16::from_le_bytes([data[2], data[3]]) as f32 / 10000.0;
                s.imu.attitude[2] = i16::from_le_bytes([data[4], data[5]]) as f32 / 10000.0;
            }
        }
        func::REPORT_ENCODER => {
            if data.len() >= 16 {
                let mut s = state.lock();
                s.encoder.m1 = i32::from_le_bytes([data[0], data[1], data[2], data[3]]);
                s.encoder.m2 = i32::from_le_bytes([data[4], data[5], data[6], data[7]]);
                s.encoder.m3 = i32::from_le_bytes([data[8], data[9], data[10], data[11]]);
                s.encoder.m4 = i32::from_le_bytes([data[12], data[13], data[14], data[15]]);
            }
        }
        _ => {
            tracing::trace!("Unknown func: {:02X}", func);
        }
    }
}
