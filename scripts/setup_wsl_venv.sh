#!/bin/bash
set -e

VENV_DIR="/home/dmn/robo1-ros-venv"
PROJECT_DIR="/mnt/c/Users/dmn32/main/code/project/robo1"

echo "Creating venv at $VENV_DIR..."
rm -rf "$VENV_DIR"
python3 -m venv "$VENV_DIR"

echo "Installing packages..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install pyyaml requests websocket-client numpy

# Install aiortc and av (needs system libav* packages)
"$VENV_DIR/bin/python" -m pip install aiortc av

echo "---"
echo "Venv ready at: $VENV_DIR"
echo "Python: $("$VENV_DIR/bin/python" --version)"
echo "Packages:"
"$VENV_DIR/bin/python" -m pip list 2>/dev/null | grep -iE 'pyyaml|requests|websocket|numpy|aiortc|av'
