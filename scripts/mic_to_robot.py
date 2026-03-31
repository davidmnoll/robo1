#!/usr/bin/env python3
import rclpy
from std_msgs.msg import UInt8MultiArray
import pyaudio

rclpy.init()
node = rclpy.create_node("mic_streamer")
pub = node.create_publisher(UInt8MultiArray, "/audio_play", 10)

p = pyaudio.PyAudio()
stream = p.open(
    format=pyaudio.paInt16, channels=1, rate=44100, input=True, frames_per_buffer=1024
)

print("Streaming mic to robot... Ctrl+C to stop")
try:
    while True:
        data = stream.read(1024, exception_on_overflow=False)
        msg = UInt8MultiArray()
        msg.data = list(data)
        pub.publish(msg)
except KeyboardInterrupt:
    pass

stream.stop_stream()
stream.close()
p.terminate()
node.destroy_node()
rclpy.shutdown()
