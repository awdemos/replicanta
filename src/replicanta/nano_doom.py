"""Nano DOOM bridge service for Replicanta organisms.

A tiny deterministic first-person ASCII shooter using a 3D raycasting
engine (adapted from the public-domain-style doomcli reference). The
module owns all game state; the Python service only marshals arguments
and returns plain strings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from replicanta import lua_sandbox


MAPS = {
    "default": [
        "################",
        "#..............#",
        "#..##.....##...#",
        "#..............#",
        "#..#..##..#....#",
        "#..............#",
        "#....###.......#",
        "#..............#",
        "#..##......##..#",
        "#..............#",
        "################",
    ],
    "box": [
        "########",
        "#^.....#",
        "#......#",
        "#.....E#",
        "########",
    ],
}

FOV = math.pi / 3
MAX_DEPTH = 18.0
MOVE_SPEED = 0.7
TURN_SPEED = 0.35
SHOT_RANGE = 9.0
SHOT_CONE = 0.12


@dataclass
class _Player:
    x: float = 3.5
    y: float = 3.5
    angle: float = 0.0
    hp: int = 15
    ammo: int = 20


@dataclass
class _Enemy:
    x: float = 10.5
    y: float = 5.5
    hp: int = 6


@dataclass
class _DoomGame:
    name: str
    world_map: list[str]
    player: _Player = field(default_factory=_Player)
    enemies: list[_Enemy] = field(default_factory=list)
    turns: int = 0
    finished: bool = False
    won: bool = False
    message: str = "find the target"
    log: list[str] = field(default_factory=list)

    def __post_init__(self):
        self._spawn_enemies()

    def _spawn_enemies(self):
        self.enemies = []
        for y, row in enumerate(self.world_map):
            for x, ch in enumerate(row):
                if ch == "E":
                    self.enemies.append(_Enemy(x=x + 0.5, y=y + 0.5))
        if not self.enemies:
            # Default fallback enemy so the game has a target.
            self.enemies.append(_Enemy(x=self.player.x + 4.0, y=self.player.y + 2.0))

    def tick(self, raw_command: str) -> str:
        """Run one command and return a text report."""
        if self.finished:
            return "game over — /doom stop to reset"
        cmd = raw_command.strip().lower()
        if not cmd or cmd == "look":
            return self.render()

        if cmd in ("w", "forward"):
            self._move(1)
        elif cmd in ("s", "back"):
            self._move(-1)
        elif cmd in ("a", "left"):
            self._strafe(-1)
        elif cmd in ("d", "right"):
            self._strafe(1)
        elif cmd in ("q", "turn-left", "turn_left"):
            self.player.angle -= TURN_SPEED
        elif cmd in ("e", "turn-right", "turn_right"):
            self.player.angle += TURN_SPEED
        elif cmd in ("shoot", "fire", "f"):
            self._shoot()
        else:
            return f"unknown command '{raw_command}' — try w/a/s/d/q/e/shoot"

        self.player.angle %= math.tau
        self._enemy_turn()
        self.turns += 1
        self._check_end()
        return self.render()

    def _move(self, direction: int):
        """Move forward (direction=1) or back (direction=-1)."""
        dx = math.cos(self.player.angle) * MOVE_SPEED * direction
        dy = math.sin(self.player.angle) * MOVE_SPEED * direction
        self._try_move(dx, dy)

    def _strafe(self, direction: int):
        """Strafe left (direction=-1) or right (direction=1)."""
        angle = self.player.angle + direction * math.pi / 2
        dx = math.cos(angle) * MOVE_SPEED
        dy = math.sin(angle) * MOVE_SPEED
        self._try_move(dx, dy)

    def _try_move(self, dx: float, dy: float):
        next_x = self.player.x + dx
        next_y = self.player.y + dy
        bumped = False
        if not _is_wall(self.world_map, next_x, self.player.y):
            self.player.x = next_x
        else:
            bumped = True
        if not _is_wall(self.world_map, self.player.x, next_y):
            self.player.y = next_y
        elif not bumped:
            bumped = True
        self.log.append("moved" if not bumped else "bump")

    def _shoot(self):
        if self.player.ammo <= 0:
            self.message = "click (no ammo)"
            self.log.append("click (no ammo)")
            return
        self.player.ammo -= 1
        hit = self._find_hit()
        if hit is not None:
            hit.hp -= 3
            if hit.hp <= 0:
                self.message = "target down"
                self.log.append("enemy down")
            else:
                self.message = "hit"
                self.log.append("hit")
        else:
            self.message = "miss"
            self.log.append("miss")

    def _find_hit(self) -> _Enemy | None:
        best: _Enemy | None = None
        best_dist = float("inf")
        for enemy in self.enemies:
            if enemy.hp <= 0:
                continue
            dx = enemy.x - self.player.x
            dy = enemy.y - self.player.y
            distance = math.hypot(dx, dy)
            if distance > SHOT_RANGE:
                continue
            angle_to_enemy = math.atan2(dy, dx)
            relative = abs(_normalize_angle(angle_to_enemy - self.player.angle))
            if relative > SHOT_CONE:
                continue
            if not _has_line_of_sight(self.world_map, self.player.x, self.player.y, enemy.x, enemy.y):
                continue
            if distance < best_dist:
                best_dist = distance
                best = enemy
        return best

    def _enemy_turn(self):
        for enemy in self.enemies:
            if enemy.hp <= 0:
                continue
            distance = math.hypot(enemy.x - self.player.x, enemy.y - self.player.y)
            if distance <= 1.2:
                self.player.hp -= 1
                self.log.append("bitten")
                continue
            dx = 0.0
            dy = 0.0
            if enemy.y < self.player.y:
                dy = 0.5
            elif enemy.y > self.player.y:
                dy = -0.5
            if enemy.x < self.player.x:
                dx = 0.5
            elif enemy.x > self.player.x:
                dx = -0.5
            if dx == 0 and dy == 0:
                continue
            next_x = enemy.x + dx
            next_y = enemy.y + dy
            if not _is_wall(self.world_map, next_x, next_y):
                enemy.x = next_x
                enemy.y = next_y

    def _check_end(self):
        alive = [e for e in self.enemies if e.hp > 0]
        if self.player.hp <= 0:
            self.finished = True
            self.won = False
            self.message = "game over"
            self.log.append("you died")
        elif not alive:
            self.finished = True
            self.won = True
            self.message = "all clear"
            self.log.append("all clear")

    def render(self, width: int = 60, height: int = 20) -> str:
        """Render a first-person ASCII frame plus HUD."""
        frame = _build_frame(width, max(8, height - 2), self)
        enemy_status = "down" if all(e.hp <= 0 for e in self.enemies) else "alive"
        hud = (
            f" doom  target={enemy_status}  hp={self.player.hp}  ammo={self.player.ammo}  "
            f"turns={self.turns}  pos=({self.player.x:.1f},{self.player.y:.1f})"
        )
        footer = f" {self.message[: width - 2]}"
        lines = [hud[:width], footer[:width]]
        lines.extend(frame)
        if self.finished:
            lines.append("YOU WON" if self.won else "GAME OVER")
        return "\n".join(lines)


class DoomService:
    """Thin bridge between the nano-doom Lua module and the Python game model."""

    def __init__(self, organism=None, lua_lock=None):
        self.organism = organism
        self._lua_lock = lua_lock
        self._game = None

    def available(self):
        return True

    def running(self):
        return self._game is not None and not self._game.finished

    def status(self):
        if self._game is None:
            return "no game running — press F10 in the DOOM pane"
        return self._game.render()

    @staticmethod
    def maps():
        return sorted(MAPS)

    def start(self, name="default"):
        name = str(name or "default")
        if name not in MAPS:
            raise ValueError(f"unknown map {name!r}")
        self._game = _DoomGame(name=name, world_map=MAPS[name])
        return self._game.render()

    def stop(self):
        self._game = None
        return "stopped"

    def command(self, cmd):
        if cmd == "__maps":
            return ", ".join(sorted(MAPS))
        if cmd == "__tactical":
            return self.tactical()
        if cmd == "__can_shoot":
            return "yes" if self.can_shoot() else "no"
        if self._game is None:
            raise RuntimeError("no game running")
        return self._game.tick(cmd)

    def tactical(self):
        """Plain-english target hints for the entity's prompt."""
        if self._game is None:
            return "no game running"
        alive = [e for e in self._game.enemies if e.hp > 0]
        if not alive:
            return "no targets."
        lines = []
        for enemy in alive:
            dx = enemy.x - self._game.player.x
            dy = enemy.y - self._game.player.y
            distance = math.hypot(dx, dy)
            angle = _normalize_angle(math.atan2(dy, dx) - self._game.player.angle)
            direction = "ahead" if abs(angle) < 0.3 else "left" if angle < 0 else "right"
            behind = abs(angle) > math.pi / 2
            lines.append(
                f"target at distance {distance:.1f}, {direction}{' (behind you)' if behind else ''}, hp={enemy.hp}"
            )
        return "\n".join(lines)

    def can_shoot(self):
        if self._game is None:
            return False
        best = self._game._find_hit()
        return best is not None and best.hp > 0


def _is_wall(world_map: list[str], x: float, y: float) -> bool:
    grid_x = int(x)
    grid_y = int(y)
    if grid_y < 0 or grid_y >= len(world_map):
        return True
    if grid_x < 0 or grid_x >= len(world_map[grid_y]):
        return True
    return world_map[grid_y][grid_x] == "#"


def _cast_ray(world_map: list[str], x: float, y: float, angle: float) -> float:
    distance = 0.0
    step = 0.03
    while distance < MAX_DEPTH:
        test_x = x + math.cos(angle) * distance
        test_y = y + math.sin(angle) * distance
        if _is_wall(world_map, test_x, test_y):
            return distance
        distance += step
    return MAX_DEPTH


def _normalize_angle(angle: float) -> float:
    while angle <= -math.pi:
        angle += math.tau
    while angle > math.pi:
        angle -= math.tau
    return angle


def _has_line_of_sight(world_map: list[str], x1: float, y1: float, x2: float, y2: float) -> bool:
    dx = x2 - x1
    dy = y2 - y1
    distance = math.hypot(dx, dy)
    if distance == 0:
        return True
    steps = max(1, int(distance / 0.05))
    for step in range(1, steps):
        ratio = step / steps
        if _is_wall(world_map, x1 + dx * ratio, y1 + dy * ratio):
            return False
    return True


def _shade_for_distance(distance: float) -> str:
    ratio = distance / MAX_DEPTH
    if ratio < 0.16:
        return "@"
    if ratio < 0.28:
        return "#"
    if ratio < 0.4:
        return "O"
    if ratio < 0.56:
        return "="
    if ratio < 0.72:
        return "-"
    if ratio < 0.88:
        return "."
    return " "


def _floor_shade(row: int, height: int) -> str:
    ratio = row / max(height, 1)
    if ratio < 0.78:
        return " "
    if ratio < 0.84:
        return "."
    if ratio < 0.9:
        return "-"
    if ratio < 0.96:
        return "="
    return "#"


def _draw_enemy(
    frame: list[list[str]], depth_buffer: list[float], width: int, height: int, player: _Player, enemy: _Enemy
):
    if enemy.hp <= 0:
        return
    dx = enemy.x - player.x
    dy = enemy.y - player.y
    distance = math.hypot(dx, dy)
    if distance <= 0.1 or distance >= MAX_DEPTH:
        return
    angle_to_enemy = math.atan2(dy, dx)
    relative_angle = _normalize_angle(angle_to_enemy - player.angle)
    if abs(relative_angle) > FOV / 2:
        return
    screen_x = int((relative_angle + FOV / 2) / FOV * width)
    sprite_height = max(1, int(height / distance))
    sprite_width = max(1, sprite_height // 2)
    top = max(1, height // 2 - sprite_height // 2)
    bottom = min(height, top + sprite_height)
    left = max(0, screen_x - sprite_width // 2)
    right = min(width, left + sprite_width)
    for column in range(left, right):
        if 0 <= column < width and distance >= depth_buffer[column]:
            continue
        for row in range(top, bottom):
            if 0 <= row < height:
                frame[row][column] = "&" if distance < SHOT_RANGE else "M"


def _build_frame(width: int, height: int, game: _DoomGame) -> list[str]:
    frame = [[" " for _ in range(width)] for _ in range(height)]
    depth_buffer: list[float] = []
    player = game.player
    for column in range(width):
        ray_angle = player.angle - FOV / 2 + (column / max(width - 1, 1)) * FOV
        distance = _cast_ray(game.world_map, player.x, player.y, ray_angle)
        corrected = distance * math.cos(ray_angle - player.angle)
        wall_height = int(height / max(corrected, 0.1))
        ceiling = max(0, height // 2 - wall_height // 2)
        floor = min(height, height // 2 + wall_height // 2)
        wall_char = _shade_for_distance(distance)
        depth_buffer.append(distance)
        for row in range(height):
            if row < ceiling:
                frame[row][column] = " "
            elif row < floor:
                frame[row][column] = wall_char
            else:
                frame[row][column] = _floor_shade(row, height)
    for enemy in game.enemies:
        _draw_enemy(frame, depth_buffer, width, height, player, enemy)
    center = width // 2
    if 0 <= center < width and height > 2:
        frame[height // 2][center] = "+"
    return ["".join(row) for row in frame]


def _build_proxy(obj):
    """Wrap non-string results so Lua can read dict keys."""
    if isinstance(obj, dict):
        return lua_sandbox.DictProxy(obj)
    return obj
