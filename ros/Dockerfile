FROM ros:humble-ros-base

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    ros-humble-rosbridge-server \
    ros-humble-rmw-fastrtps-cpp \
    wireguard-tools \
    iptables \
    iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY camera_forwarder.py /workspace/camera_forwarder.py
COPY entrypoint.sh /usr/local/bin/start-ros.sh
COPY discovery_server.xml /workspace/discovery_server.xml
RUN chmod +x /usr/local/bin/start-ros.sh

# Rosbridge WebSocket
EXPOSE 9090
# ROS2 Discovery Server
EXPOSE 11811/udp
# WireGuard VPN
EXPOSE 51820/udp

CMD ["/usr/local/bin/start-ros.sh"]
