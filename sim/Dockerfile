FROM ros:humble-ros-base

ENV DEBIAN_FRONTEND=noninteractive \
    WEBOTS_HOME=/opt/webots \
    WEBOTS_VERSION=R2023b \
    WEBOTS_WORLD=/workspace/worlds/battle_arena.wbt

# Base runtime dependencies for Webots + headless rendering + Python tooling
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget ca-certificates xvfb \
    libglu1-mesa libgl1-mesa-glx libgl1-mesa-dri libegl1-mesa \
    libxkbcommon0 libxrandr2 libxinerama1 libxi6 libxcursor1 \
    libasound2 libpulse0 libjpeg62 libpng16-16 \
    python3-pip \
 && rm -rf /var/lib/apt/lists/*

# Fetch and install Webots into /opt/webots
RUN wget -q https://github.com/cyberbotics/webots/releases/download/${WEBOTS_VERSION}/webots-${WEBOTS_VERSION}-x86-64.tar.xz -O /tmp/webots.tar.xz \
 && mkdir -p ${WEBOTS_HOME} \
 && tar -xf /tmp/webots.tar.xz -C /tmp \
 && mv /tmp/webots-${WEBOTS_VERSION} ${WEBOTS_HOME} \
 && rm /tmp/webots.tar.xz

ENV PATH=${WEBOTS_HOME}:${PATH} \
    LD_LIBRARY_PATH=${WEBOTS_HOME}/lib:${LD_LIBRARY_PATH} \
    WEBOTS_DISABLE_SAVE_WORLD=1 \
    WEBOTS_DISABLE_SAVE_SCREEN=1

# Install the ROS 2 integration packages so controllers can talk to the graph
RUN apt-get update && apt-get install -y --no-install-recommends \
    ros-humble-webots-ros2 \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir roslibpy

WORKDIR /workspace
COPY entrypoint.sh /usr/local/bin/run-webots.sh
RUN chmod +x /usr/local/bin/run-webots.sh
COPY worlds ./worlds
COPY controllers ./controllers

CMD ["/usr/local/bin/run-webots.sh"]
