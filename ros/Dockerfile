FROM ros:humble-ros-base

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    ros-humble-rosbridge-server \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY camera_forwarder.py /workspace/camera_forwarder.py
COPY entrypoint.sh /usr/local/bin/start-ros.sh
RUN chmod +x /usr/local/bin/start-ros.sh

EXPOSE 9090

CMD ["/usr/local/bin/start-ros.sh"]
