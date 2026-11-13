"""Server-side "internal lobby" virtual game.

When a lobby is offline (no real robot/forwarder producing a WebRTC track), the
API server itself becomes the producer: it simulates a small 2D world and
renders a first-person raycast view per virtual player, streamed to the browser
over the existing Hop 2 path. Joystick commands from the browser's `control`
datachannel drive each player's pose in the shared world.

This module is deliberately self-contained: it holds only in-memory state and
rendering. All DB load/persist and WebRTC wiring live in main.py, which calls
into these classes. Coordinates match the Tauri `Editor2D` convention: world
units on the XY ground plane, walls are `width` x `height` rectangles centered
at `(x, y)` rotated by `rotation` radians; player pose is `(x, y, yaw)`.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
from aiortc import VideoStreamTrack
from av import VideoFrame

logger = logging.getLogger("gateway.virtual_world")

# --- Rendering / simulation tuning -----------------------------------------
FRAME_W = 320
FRAME_H = 240
FOV = math.radians(70.0)
MAX_DEPTH = 40.0            # world units; rays beyond this hit nothing
TICK_HZ = 30.0             # simulation integration rate
RENDER_FPS = 15.0          # camera track frame rate
MOVE_SPEED = 3.0           # world units / second at full stick
TURN_SPEED = 2.4           # radians / second at full stick
PLAYER_RADIUS = 0.3        # collision radius against walls
WALL_SCREEN_SCALE = 1.0    # projected wall height multiplier

# Joystick axis convention (gamepad-style `axes` array from the browser):
#   left stick  = [0]=X, [1]=Y ("Move");  right stick = [2]=X, [3]=Y ("Look").
# Forward = left stick pushed up (negative Y); turn = right stick X.
AXIS_LINEAR = 1
AXIS_ANGULAR = 2
INVERT_LINEAR = True   # stick up is negative -> forward is positive
INVERT_ANGULAR = True  # stick right is positive -> turn right (yaw decreases)


@dataclass
class Wall:
    """An oriented rectangle on the XY ground plane."""

    cx: float
    cy: float
    hw: float  # half-width  (x extent in local frame)
    hh: float  # half-height (y extent in local frame)
    rot: float  # radians

    def __post_init__(self) -> None:
        self._cos = math.cos(self.rot)
        self._sin = math.sin(self.rot)

    def to_local(self, px: float, py: float) -> Tuple[float, float]:
        """Transform a world point into this wall's local (axis-aligned) frame."""
        dx = px - self.cx
        dy = py - self.cy
        # rotate by -rot
        lx = dx * self._cos + dy * self._sin
        ly = -dx * self._sin + dy * self._cos
        return lx, ly

    def contains(self, px: float, py: float, pad: float = 0.0) -> bool:
        lx, ly = self.to_local(px, py)
        return abs(lx) <= self.hw + pad and abs(ly) <= self.hh + pad

    def ray_distance(self, ox: float, oy: float, dx: float, dy: float) -> Optional[float]:
        """Nearest positive intersection distance of ray (o, d) with this box.

        Ray is transformed into local frame and tested against the axis-aligned
        slab [-hw, hw] x [-hh, hh]. Returns None if no hit in front of origin.
        """
        # origin & direction in local frame
        ldx = dx * self._cos + dy * self._sin
        ldy = -dx * self._sin + dy * self._cos
        lox, loy = self.to_local(ox, oy)

        tmin = -math.inf
        tmax = math.inf
        for lo, ld, half in ((lox, ldx, self.hw), (loy, ldy, self.hh)):
            if abs(ld) < 1e-9:
                # ray parallel to this axis; miss if origin outside the slab
                if lo < -half or lo > half:
                    return None
                continue
            t1 = (-half - lo) / ld
            t2 = (half - lo) / ld
            if t1 > t2:
                t1, t2 = t2, t1
            tmin = max(tmin, t1)
            tmax = min(tmax, t2)
            if tmin > tmax:
                return None
        if tmax < 0:
            return None
        return tmin if tmin > 0 else tmax


@dataclass
class PlayerState:
    ns: str
    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0
    color: str = "#3b82f6"
    # commanded velocities, normalized to [-1, 1], set from joystick
    lin: float = 0.0
    ang: float = 0.0


def _hex_to_rgb(color: str) -> Tuple[int, int, int]:
    c = (color or "#3b82f6").lstrip("#")
    if len(c) != 6:
        return (59, 130, 246)
    try:
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    except ValueError:
        return (59, 130, 246)


class VirtualWorld:
    """In-memory authoritative world for one lobby: walls + player poses.

    Lifecycle is owned by main.py: create, `set_walls`, `add_player`, run the
    integration loop via `run()`, and `stop()` when the last viewer leaves.
    Poses are read back out (via `snapshot_player`) so main.py can persist them.
    """

    def __init__(self, lobby_id: int) -> None:
        self.lobby_id = lobby_id
        self.walls: List[Wall] = []
        self.players: Dict[str, PlayerState] = {}
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    # -- world / player mutation --------------------------------------------
    def set_walls(self, walls: List[Wall]) -> None:
        self.walls = walls

    def set_walls_from_elements(self, elements: List[dict]) -> None:
        """Build wall list from VirtualWorldElement-shaped dicts (walls only)."""
        walls: List[Wall] = []
        for el in elements:
            if (el.get("element_type") or "wall") != "wall":
                continue
            w = float(el.get("width") or 1.0)
            h = float(el.get("height") or 1.0)
            walls.append(
                Wall(
                    cx=float(el.get("x", 0.0)),
                    cy=float(el.get("y", 0.0)),
                    hw=max(w, 0.05) / 2.0,
                    hh=max(h, 0.05) / 2.0,
                    rot=float(el.get("rotation", 0.0)),
                )
            )
        self.walls = walls

    def add_player(self, ns: str, x: float, y: float, yaw: float, color: str) -> PlayerState:
        p = PlayerState(ns=ns, x=x, y=y, yaw=yaw, color=color)
        self.players[ns] = p
        return p

    def remove_player(self, ns: str) -> None:
        self.players.pop(ns, None)

    def set_command(self, ns: str, lin: float, ang: float) -> None:
        p = self.players.get(ns)
        if p is None:
            return
        p.lin = max(-1.0, min(1.0, float(lin)))
        p.ang = max(-1.0, min(1.0, float(ang)))

    def set_command_from_joy(self, ns: str, axes: List[float]) -> None:
        """Map a browser gamepad `axes` array to this player's velocities."""
        def ax(i: int) -> float:
            return float(axes[i]) if axes and len(axes) > i else 0.0

        lin = ax(AXIS_LINEAR) * (-1.0 if INVERT_LINEAR else 1.0)
        ang = ax(AXIS_ANGULAR) * (-1.0 if INVERT_ANGULAR else 1.0)
        self.set_command(ns, lin, ang)

    def snapshot_player(self, ns: str) -> Optional[Tuple[float, float, float]]:
        p = self.players.get(ns)
        return (p.x, p.y, p.yaw) if p else None

    def _blocked(self, x: float, y: float) -> bool:
        return any(w.contains(x, y, pad=PLAYER_RADIUS) for w in self.walls)

    def tick(self, dt: float) -> None:
        for p in self.players.values():
            if p.ang:
                p.yaw += p.ang * TURN_SPEED * dt
                p.yaw = (p.yaw + math.pi) % (2 * math.pi) - math.pi
            if p.lin:
                step = p.lin * MOVE_SPEED * dt
                nx = p.x + math.cos(p.yaw) * step
                ny = p.y + math.sin(p.yaw) * step
                # simple slide: try full move, then axis-only fallbacks
                if not self._blocked(nx, ny):
                    p.x, p.y = nx, ny
                elif not self._blocked(nx, p.y):
                    p.x = nx
                elif not self._blocked(p.x, ny):
                    p.y = ny

    # -- integration loop ----------------------------------------------------
    async def run(self) -> None:
        self._stop.clear()
        dt = 1.0 / TICK_HZ
        loop = asyncio.get_event_loop()
        next_t = loop.time()
        try:
            while not self._stop.is_set():
                self.tick(dt)
                next_t += dt
                delay = next_t - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    next_t = loop.time()
        except asyncio.CancelledError:
            pass

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self.run())

    def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()


def _cast(world: VirtualWorld, ox: float, oy: float, angle: float) -> float:
    dx = math.cos(angle)
    dy = math.sin(angle)
    best = MAX_DEPTH
    for w in world.walls:
        d = w.ray_distance(ox, oy, dx, dy)
        if d is not None and 0 < d < best:
            best = d
    return best


def render_view(world: VirtualWorld, ns: str) -> np.ndarray:
    """Render a first-person raycast RGB frame (H, W, 3) uint8 for player `ns`."""
    buf = np.empty((FRAME_H, FRAME_W, 3), dtype=np.uint8)
    # sky / ceiling gradient (dark) and floor (slightly lighter)
    buf[: FRAME_H // 2, :, :] = (25, 28, 38)
    buf[FRAME_H // 2 :, :, :] = (40, 44, 52)

    p = world.players.get(ns)
    if p is None:
        return buf

    half_h = FRAME_H / 2.0
    for col in range(FRAME_W):
        cam_x = (2.0 * col / FRAME_W) - 1.0  # -1..1 across the screen
        ray_angle = p.yaw + math.atan(cam_x * math.tan(FOV / 2.0))
        dist = _cast(world, p.x, p.y, ray_angle)
        # fisheye correction so flat walls look flat
        corrected = dist * math.cos(ray_angle - p.yaw)
        corrected = max(corrected, 0.05)
        line_h = min(FRAME_H, (FRAME_H / corrected) * WALL_SCREEN_SCALE)
        top = int(max(0, half_h - line_h / 2.0))
        bottom = int(min(FRAME_H, half_h + line_h / 2.0))
        if dist >= MAX_DEPTH:
            continue
        shade = max(0.15, 1.0 - corrected / MAX_DEPTH)
        base = np.array((120, 140, 170), dtype=np.float32) * shade
        buf[top:bottom, col, :] = base.astype(np.uint8)

    _draw_player_billboards(world, ns, p, buf)
    return buf


def _draw_player_billboards(world: VirtualWorld, ns: str, me: PlayerState, buf: np.ndarray) -> None:
    """Draw other players as simple colored squares projected into the view."""
    half_h = FRAME_H / 2.0
    tan_half_fov = math.tan(FOV / 2.0)
    for other in world.players.values():
        if other.ns == ns:
            continue
        dx = other.x - me.x
        dy = other.y - me.y
        dist = math.hypot(dx, dy)
        if dist < 0.2 or dist >= MAX_DEPTH:
            continue
        rel = math.atan2(dy, dx) - me.yaw
        rel = (rel + math.pi) % (2 * math.pi) - math.pi
        if abs(rel) > FOV / 2.0:
            continue
        cam_x = math.tan(rel) / tan_half_fov  # -1..1
        col = int((cam_x + 1.0) / 2.0 * FRAME_W)
        size = int(min(FRAME_H, (FRAME_H / dist) * 0.5))
        top = int(max(0, half_h - size / 2.0))
        bottom = int(min(FRAME_H, half_h + size / 2.0))
        left = max(0, col - size // 2)
        right = min(FRAME_W, col + size // 2)
        if right <= left or bottom <= top:
            continue
        buf[top:bottom, left:right, :] = _hex_to_rgb(other.color)


class RaycastCameraTrack(VideoStreamTrack):
    """A server-generated video track rendering player `ns`'s first-person view."""

    kind = "video"

    def __init__(self, world: VirtualWorld, ns: str) -> None:
        super().__init__()
        self.world = world
        self.ns = ns

    async def recv(self) -> VideoFrame:
        # next_timestamp() (from aiortc's VideoStreamTrack) paces frames at ~30fps.
        pts, time_base = await self.next_timestamp()
        arr = render_view(self.world, self.ns)
        frame = VideoFrame.from_ndarray(arr, format="rgb24")
        frame.pts = pts
        frame.time_base = time_base
        return frame
