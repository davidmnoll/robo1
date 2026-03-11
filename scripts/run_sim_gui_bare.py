#!/usr/bin/env python3
"""Launch ros-core (rosbridge + camera_forwarder) and Webots sim with GUI
directly on this machine — no Docker, no xvfb, no headless mode.

Works on both Linux and Windows.
"""

import os
import signal
import subprocess
import sys
import shutil
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
IS_WINDOWS = sys.platform == "win32"


def find_ros_setup() -> str | None:
    """Locate a ROS 2 setup script."""
    explicit = os.environ.get("ROS_SETUP_SCRIPT")
    if explicit and Path(explicit).is_file():
        return explicit

    if IS_WINDOWS:
        ros_root = os.environ.get("ROS_WIN_ROOT", r"C:\dev\ros2_jazzy")
        candidates = [
            Path(ros_root) / "local_setup.bash",
            Path(ros_root) / "setup.bash",
            Path(r"C:\opt\ros\jazzy\local_setup.bash"),
            Path(r"C:\opt\ros\humble\local_setup.bash"),
        ]
    else:
        distro = os.environ.get("ROS_DISTRO", "")
        candidates = []
        if distro:
            candidates.append(Path(f"/opt/ros/{distro}/setup.bash"))
        for d in ("humble", "jazzy", "rolling", "galactic", "foxy"):
            candidates.append(Path(f"/opt/ros/{d}/setup.bash"))

    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def find_webots() -> str | None:
    """Locate the webots binary."""
    found = shutil.which("webots")
    if found:
        return found
    if IS_WINDOWS:
        for wpath in [
            Path(r"C:\Program Files\Webots\msys64\mingw64\bin\webots.exe"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Webots" / "msys64" / "mingw64" / "bin" / "webots.exe",
        ]:
            if wpath.is_file():
                return str(wpath)
    return None


def find_python() -> str:
    """Find python from the ros venv or system."""
    venv = Path(os.environ.get("ROS_VENV", ROOT_DIR / "ros" / ".venv"))
    if IS_WINDOWS:
        venv_py = venv / "Scripts" / "python.exe"
    else:
        venv_py = venv / "bin" / "python"
    if venv_py.is_file():
        return str(venv_py)
    return sys.executable


def main():
    # ── Settings ──
    rosbridge_port = os.environ.get("ROS_BRIDGE_PORT", "9090")
    sim_root = Path(os.environ.get("SIM_WORKSPACE_ROOT", ROOT_DIR / "sim"))
    world_path = Path(os.environ.get("WEBOTS_WORLD", sim_root / "worlds" / "turtlebot_apartment.wbt"))
    bot_log_dir = Path(os.environ.get("BOT_LOG_DIR", Path(os.environ.get("TEMP", "/tmp")) / "bot_logs"))
    bot_log_dir.mkdir(parents=True, exist_ok=True)

    # ── Validate ──
    ros_setup = find_ros_setup()
    if not ros_setup:
        hint = "Set ROS_SETUP_SCRIPT or ROS_WIN_ROOT." if IS_WINDOWS else "Set ROS_SETUP_SCRIPT or ROS_DISTRO."
        print(f"Could not locate ROS 2 setup.bash. {hint}", file=sys.stderr)
        sys.exit(1)

    if not world_path.is_file():
        print(f"Webots world not found: {world_path}", file=sys.stderr)
        sys.exit(1)

    webots_bin = find_webots()
    if not webots_bin:
        print("webots not found in PATH. Install Webots or add it to PATH.", file=sys.stderr)
        sys.exit(1)

    python_bin = find_python()

    # ── Environment for subprocesses ──
    env = os.environ.copy()
    env["BOT_LOG_DIR"] = str(bot_log_dir)
    env["WEBOTS_DISABLE_SAVE_WORLD"] = "1"
    env["WEBOTS_DISABLE_SAVE_SCREEN"] = "1"
    env.setdefault("WEBOTS_PROJECT_PATH", str(sim_root))

    procs: list[subprocess.Popen] = []

    def cleanup(*_args):
        print("\n[sim-gui-bare] Shutting down...")
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        print("[sim-gui-bare] Done.")
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # On Linux, source ROS setup and inherit the env into subprocesses
    # by running commands through a shell wrapper.
    if IS_WINDOWS:
        shell_prefix = []
    else:
        shell_prefix = []  # ros2 should be on PATH after user sources setup

    # ── 1. rosbridge ──
    print(f"[sim-gui-bare] Starting rosbridge on port {rosbridge_port}...")
    rosbridge_cmd = [
        "ros2", "run", "rosbridge_server", "rosbridge_websocket",
        "--port", rosbridge_port,
        "--ros-args", "--log-level", "warn",
        "-p", "default_call_service_timeout:=5.0",
        "-p", "call_services_in_new_thread:=true",
        "-p", "send_action_goals_in_new_thread:=true",
    ]
    procs.append(subprocess.Popen(shell_prefix + rosbridge_cmd, env=env))

    # ── 2. camera_forwarder ──
    print("[sim-gui-bare] Starting camera_forwarder...")
    cam_script = str(ROOT_DIR / "ros" / "camera_forwarder.py")
    procs.append(subprocess.Popen([python_bin, cam_script], env=env))

    # ── 3. Webots with GUI ──
    print(f"[sim-gui-bare] Launching Webots GUI: {world_path}")
    webots_cmd = [webots_bin, "--stdout", "--stderr", "--batch", str(world_path)]
    procs.append(subprocess.Popen(webots_cmd, env=env))

    print("[sim-gui-bare] All processes running. Press Ctrl+C to stop.")

    # Wait for any process to exit, then tear everything down
    while True:
        for p in procs:
            try:
                p.wait(timeout=1)
                # A process exited
                print(f"[sim-gui-bare] Process (pid {p.pid}) exited with code {p.returncode}")
                cleanup()
            except subprocess.TimeoutExpired:
                continue


if __name__ == "__main__":
    main()
