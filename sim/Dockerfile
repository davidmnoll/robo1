FROM cyberbotics/webots:R2025a-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    WEBOTS_WORLD=/workspace/worlds/battle_arena.wbt

RUN apt-get update && apt-get install -y --no-install-recommends \
    software-properties-common \
    curl \
    gnupg \
    lsb-release \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN add-apt-repository universe \
    && add-apt-repository restricted \
    && add-apt-repository multiverse

RUN curl -s https://raw.githubusercontent.com/ros/rosdistro/master/ros.key -o /tmp/ros.key \
    && gpg --dearmor -o /usr/share/keyrings/ros-archive-keyring.gpg /tmp/ros.key \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu $(lsb_release -cs) main" > /etc/apt/sources.list.d/ros2.list \
    && rm /tmp/ros.key

RUN apt-get update && apt-get install -y --no-install-recommends \
    ros-humble-ros-base \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir roslibpy

WORKDIR /workspace
COPY entrypoint.sh /usr/local/bin/run-webots.sh
RUN chmod +x /usr/local/bin/run-webots.sh
COPY worlds ./worlds
COPY controllers ./controllers

CMD ["/usr/local/bin/run-webots.sh"]
