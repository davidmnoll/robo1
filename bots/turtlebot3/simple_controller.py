#!/usr/bin/env python3
"""
Simple TurtleBot3 controller example.
Demonstrates reading sensors and sending motor commands.
"""
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from sensor_msgs.msg import LaserScan


class SimpleController(Node):
    def __init__(self):
        super().__init__('simple_controller')

        # Publisher: send velocity commands to the robot
        self.cmd_pub = self.create_publisher(Twist, '/cmd_vel', 10)

        # Subscriber: receive laser scan data
        self.scan_sub = self.create_subscription(
            LaserScan, '/scan', self.scan_callback, 10)

        # Timer: control loop at 10Hz
        self.timer = self.create_timer(0.1, self.control_loop)

        self.front_distance = float('inf')
        self.get_logger().info('Simple controller started!')

    def scan_callback(self, msg):
        # Get distance directly in front (middle of scan array)
        if len(msg.ranges) > 0:
            mid = len(msg.ranges) // 2
            self.front_distance = msg.ranges[mid]

    def control_loop(self):
        cmd = Twist()

        if self.front_distance < 0.5:
            # Obstacle ahead: turn
            cmd.linear.x = 0.0
            cmd.angular.z = 0.5  # rotate left
            self.get_logger().info(f'Obstacle at {self.front_distance:.2f}m - turning')
        else:
            # Clear ahead: drive forward
            cmd.linear.x = 0.2  # forward speed
            cmd.angular.z = 0.0

        self.cmd_pub.publish(cmd)


def main():
    rclpy.init()
    node = SimpleController()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        # Stop the robot
        stop_cmd = Twist()
        node.cmd_pub.publish(stop_cmd)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
