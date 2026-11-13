# Rosmaster A1 Robot Image
# Extends base image with required ROS packages

ARG BASE_IMAGE=192.168.2.51:5000/ros-humble-a1:2.1.3
FROM ${BASE_IMAGE}

# Install required ROS packages and build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    ros-humble-slam-toolbox \
    cmake \
    git \
    python3-serial \
    && rm -rf /var/lib/apt/lists/*

# Build YDLidar SDK
RUN cd /root && \
    git clone https://github.com/YDLIDAR/YDLidar-SDK.git && \
    cd YDLidar-SDK && \
    mkdir build && cd build && \
    cmake .. && \
    make -j$(nproc) && \
    make install && \
    ldconfig

# Build YDLIDAR ROS2 driver (humble branch)
RUN cd /root && \
    git clone --branch humble https://github.com/YDLIDAR/ydlidar_ros2_driver.git ydlidar_humble && \
    cd ydlidar_humble && \
    . /opt/ros/humble/setup.sh && \
    colcon build --symlink-install
