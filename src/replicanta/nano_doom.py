"""Nano DOOM bridge service for Replicanta organisms.

A Python port of the Arduino "doom-nano" raycast engine by David Ruiz
(https://github.com/daveruiz/doom-nano). It runs as a deterministic,
text-rendered first-person ASCII shooter inside the organism. The Python
module owns all game state; the service only marshals arguments and returns
plain strings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Block type nibbles (matching the original doom-nano legend)
E_FLOOR = 0x0
E_PLAYER = 0x1
E_ENEMY = 0x2
E_DOOR = 0x4
E_LOCKEDDOOR = 0x5
E_EXIT = 0x7
E_MEDIKIT = 0x8
E_KEY = 0x9
E_FIREBALL = 0xA
E_WALL = 0xF

# Entity states
S_STAND = 0
S_ALERT = 1
S_FIRING = 2
S_MELEE = 3
S_HIT = 4
S_DEAD = 5
S_HIDDEN = 6
S_OPEN = 7
S_CLOSE = 8

SCREEN_WIDTH = 80
SCREEN_HEIGHT = 24
RENDER_HEIGHT = 20  # reserve bottom rows for HUD / messages
HALF_WIDTH = SCREEN_WIDTH // 2
HALF_HEIGHT = RENDER_HEIGHT // 2

RES_DIVIDER = 1
Z_RES_DIVIDER = 1
DISTANCE_MULTIPLIER = 20
MAX_RENDER_DEPTH = 14
MAX_SPRITE_DEPTH = 8

ROT_SPEED = 0.12
MOV_SPEED = 0.25
MOV_SPEED_INV = 4.0
JOGGING_SPEED = 0.05
ENEMY_SPEED = 0.06
FIREBALL_SPEED = 0.2
FIREBALL_ANGLES = 45

MAX_ENTITIES = 32
MAX_ENTITY_DISTANCE = 12
MAX_ENEMY_VIEW = 7
ITEM_COLLIDER_DIST = 0.7
ENEMY_COLLIDER_DIST = 0.5
FIREBALL_COLLIDER_DIST = 0.4
ENEMY_MELEE_DIST = 0.9

ENEMY_MELEE_DAMAGE = 8
ENEMY_FIREBALL_DAMAGE = 20
GUN_MAX_DAMAGE = 15
PLAYER_MAX_HEALTH = 100

# Gameplay tuning: fewer enemies makes the GIF readable and keeps the entity alive longer.
MAX_ENEMIES_ON_START = 2

# ASCII wall/floor gradient. Lower index = darker/farther.
GRADIENT = " .-=#@"
GRADIENT_COUNT = len(GRADIENT)
FLOOR_GRADIENT = " .-="
CEILING_CHAR = " "


@dataclass
class Coords:
    x: float = 0.0
    y: float = 0.0


@dataclass
class Player:
    pos: Coords = field(default_factory=Coords)
    dir: Coords = field(default_factory=lambda: Coords(1.0, 0.0))
    plane: Coords = field(default_factory=lambda: Coords(0.0, 0.66))
    velocity: float = 0.0
    health: int = PLAYER_MAX_HEALTH
    keys: int = 0


@dataclass
class Entity:
    uid: int = 0
    pos: Coords = field(default_factory=Coords)
    state: int = S_STAND
    health: int = 0
    distance: float = 0.0
    timer: int = 0
    etype: int = E_FLOOR


@dataclass
class StaticEntity:
    uid: int = 0
    x: int = 0
    y: int = 0
    active: bool = True


_DEFAULT_MAP_GRID = [
    "################################################################",
    "#############################...........########################",
    "######....###################........E..########################",
    "######....########..........#...........#...####################",
    "######.....#######..........L.....E.......M.####################",
    "######.....#######..........#...........#...####################",
    "##################...########...........########################",
    "######.........###...########...........########################",
    "######.........###...#############D#############################",
    "######.........#......E##########...############################",
    "######....E....D...E...##########...############################",
    "######.........#.......##########...############################",
    "######....E....##################...############################",
    "#...##.........##################...############################",
    "#.K.######D######################...############################",
    "#...#####...###############...#E.....K##########################",
    "##D######...###############..####...############################",
    "#...#####...###############..####...############################",
    "#...#...#...###############..####...############################",
    "#...D...#...#####################...############################",
    "#...#...#...#####################...############################",
    "#...######D#######################L#############################",
    "#.E.##.........#################.....#################........##",
    "#...##.........############...............############........##",
    "#...##...E.....############...............############........##",
    "#....#.........############...E.......E....#.........#........##",
    "#....L....K....############................D....E....D....E...##",
    "#....#.........############................#.........#........##",
    "#...##.....E...############...............####....####........##",
    "#...##.........############...............#####..#####.....M..##",
    "#...##.........#################.....##########..#####........##",
    "#...######L#######################D############..###############",
    "#...#####...#####################...###########..###############",
    "#E.E#####...#####################...###########..###############",
    "#...#...#...#####################.E.###########..###############",
    "#...D.M.#...#####################...###########..###############",
    "#...#...#...#####################...###########..###.#.#.#.#####",
    "#...#####...#####################...###########...#.........####",
    "#...#####...#####################...###########...D....E..K.####",
    "#................##......########...###########...#.........####",
    "#....E........E...L...E...X######...################.#.#.#.#####",
    "#................##......########...############################",
    "#################################...############################",
    "#############..#..#..#############L#############################",
    "###########....#..#.########....#...#....#######################",
    "#############.....##########.P..D...D....#######################",
    "############################....#...#....#######################",
    "##############..#################...############################",
    "##############..############....#...#....#######################",
    "############################....D...D....#######################",
    "############################....#...#....#######################",
    "#################################...############################",
    "############################.............#######################",
    "############################..........EK.#######################",
    "############################.............#######################",
    "################################################################",
]

MAPS = {
    "default": _DEFAULT_MAP_GRID,
    "box": [
        "########",
        "#P.....#",
        "#......#",
        "#.....E#",
        "########",
    ],
}


class _DoomGame:
    def __init__(self, name: str, world_map: list[str]):
        self.name = name
        self.world_map = [list(row) for row in world_map]
        self.level_width = len(self.world_map[0])
        self.level_height = len(self.world_map)
        self.player = Player()
        self.entities: list[Entity] = []
        self.static_entities: list[StaticEntity] = []
        self.num_entities = 0
        self.num_static = 0
        self.turns = 0
        self.finished = False
        self.won = False
        self.message = "find the exit"
        self.log: list[str] = []
        self.zbuffer: list[float] = [0.0] * (SCREEN_WIDTH // Z_RES_DIVIDER)
        self.frame_count = 0
        self._initialize_level()

    def _initialize_level(self):
        enemy_spawn_positions = []
        for y, row in enumerate(self.world_map):
            for x, ch in enumerate(row):
                if ch == "P":
                    self.player.pos = Coords(x + 0.5, y + 0.5)
                elif ch == "E":
                    enemy_spawn_positions.append((x, y))
                elif ch == "M":
                    self._spawn_entity(E_MEDIKIT, x, y)
                elif ch == "K":
                    self._spawn_entity(E_KEY, x, y)
                elif ch == "D":
                    self._add_static(E_DOOR, x, y)
                elif ch == "L":
                    self._add_static(E_LOCKEDDOOR, x, y)
        # Limit enemies for clarity in demos; keep the closest ones to the player.
        px, py = self.player.pos.x, self.player.pos.y
        enemy_spawn_positions.sort(key=lambda xy: math.hypot(xy[0] - px, xy[1] - py))
        for x, y in enemy_spawn_positions[:MAX_ENEMIES_ON_START]:
            self._spawn_entity(E_ENEMY, x, y)

    def _create_uid(self, etype: int, x: int, y: int) -> int:
        return (etype << 16) | (x << 8) | y

    def _uid_type(self, uid: int) -> int:
        return uid >> 16

    def _spawn_entity(self, etype: int, x: int, y: int):
        if self.num_entities >= MAX_ENTITIES:
            return
        uid = self._create_uid(etype, x, y)
        for e in self.entities:
            if e.uid == uid:
                return
        if etype == E_ENEMY:
            ent = Entity(uid=uid, pos=Coords(x + 0.5, y + 0.5), state=S_STAND, health=100, etype=E_ENEMY)
        elif etype == E_MEDIKIT:
            ent = Entity(uid=uid, pos=Coords(x + 0.5, y + 0.5), state=S_STAND, health=0, etype=E_MEDIKIT)
        elif etype == E_KEY:
            ent = Entity(uid=uid, pos=Coords(x + 0.5, y + 0.5), state=S_STAND, health=0, etype=E_KEY)
        elif etype == E_FIREBALL:
            angle = math.atan2(y - self.player.pos.y, x - self.player.pos.x)
            dir_idx = int(FIREBALL_ANGLES + angle / math.pi * FIREBALL_ANGLES) % (2 * FIREBALL_ANGLES)
            ent = Entity(uid=uid, pos=Coords(x, y), state=S_STAND, health=dir_idx, etype=E_FIREBALL)
        else:
            ent = Entity(uid=uid, pos=Coords(x + 0.5, y + 0.5), state=S_STAND, health=0, etype=etype)
        self.entities.append(ent)
        self.num_entities += 1

    def _add_static(self, etype: int, x: int, y: int):
        if self.num_static >= MAX_ENTITIES:
            return
        uid = self._create_uid(etype, x, y)
        self.static_entities.append(StaticEntity(uid=uid, x=x, y=y, active=True))
        self.num_static += 1

    def _get_block(self, x: int, y: int) -> int:
        if x < 0 or x >= self.level_width or y < 0 or y >= self.level_height:
            return E_WALL
        ch = self.world_map[y][x]
        return {
            "#": E_WALL,
            "P": E_FLOOR,
            "E": E_FLOOR,
            "M": E_FLOOR,
            "K": E_FLOOR,
            "D": E_DOOR,
            "L": E_LOCKEDDOOR,
            "X": E_EXIT,
            ".": E_FLOOR,
            " ": E_FLOOR,
        }.get(ch, E_FLOOR)

    def _set_block(self, x: int, y: int, ch: str):
        self.world_map[y][x] = ch

    def _coords_distance(self, a: Coords, b: Coords) -> float:
        return math.hypot(a.x - b.x, a.y - b.y)

    def _detect_collision(self, pos: Coords, rel_x: float, rel_y: float, only_walls: bool = False) -> int:
        round_x = int(pos.x + rel_x)
        round_y = int(pos.y + rel_y)
        block = self._get_block(round_x, round_y)
        if block in (E_WALL, E_LOCKEDDOOR):
            return self._create_uid(block, round_x, round_y)
        if only_walls:
            return 0
        for ent in self.entities:
            if ent.pos.x == pos.x and ent.pos.y == pos.y:
                continue
            if ent.etype != E_ENEMY or ent.state in (S_DEAD, S_HIDDEN):
                continue
            new_x = ent.pos.x - rel_x
            new_y = ent.pos.y - rel_y
            dist = self._coords_distance(pos, Coords(new_x, new_y))
            if dist < ENEMY_COLLIDER_DIST and dist < ent.distance:
                return ent.uid
        return 0

    def _update_position(self, pos: Coords, rel_x: float, rel_y: float, only_walls: bool = False) -> bool:
        collide_x = self._detect_collision(pos, rel_x, 0, only_walls)
        collide_y = self._detect_collision(pos, 0, rel_y, only_walls)
        moved = False
        if not collide_x:
            pos.x += rel_x
            moved = True
        if not collide_y:
            pos.y += rel_y
            moved = True
        return moved

    def can_shoot(self) -> bool:
        for ent in self.entities:
            if ent.etype != E_ENEMY or ent.state in (S_DEAD, S_HIDDEN):
                continue
            transform = self._translate_into_view(ent.pos)
            if transform.y > 0 and abs(transform.x) < transform.y * 0.8:
                return True
        return False

    def _fire(self):
        hit = False
        for ent in self.entities:
            if ent.etype != E_ENEMY or ent.state in (S_DEAD, S_HIDDEN):
                continue
            transform = self._translate_into_view(ent.pos)
            if transform.y > 0 and abs(transform.x) < transform.y * 0.8:
                damage = min(GUN_MAX_DAMAGE, int(GUN_MAX_DAMAGE / (abs(transform.x) * ent.distance * 0.2 + 1)))
                if damage > 0:
                    ent.health = max(0, ent.health - damage)
                    ent.state = S_HIT
                    ent.timer = 4
                    hit = True
        if hit:
            self.message = "hit"
            self.log.append("hit")
        else:
            self.message = "miss"
            self.log.append("miss")

    def _spawn_fireball(self, x: float, y: float):
        if self.num_entities >= MAX_ENTITIES:
            return
        uid = self._create_uid(E_FIREBALL, int(x * 10), int(y * 10))
        for ent in self.entities:
            if ent.uid == uid:
                return
        angle = math.atan2(y - self.player.pos.y, x - self.player.pos.x)
        dir_idx = int(FIREBALL_ANGLES + angle / math.pi * FIREBALL_ANGLES) % (2 * FIREBALL_ANGLES)
        ent = Entity(uid=uid, pos=Coords(x, y), state=S_STAND, health=dir_idx, etype=E_FIREBALL)
        self.entities.append(ent)
        self.num_entities += 1

    def _remove_entity(self, uid: int):
        self.entities = [e for e in self.entities if e.uid != uid]
        self.num_entities = len(self.entities)

    def _translate_into_view(self, pos: Coords) -> Coords:
        sprite_x = pos.x - self.player.pos.x
        sprite_y = pos.y - self.player.pos.y
        inv_det = 1.0 / (self.player.plane.x * self.player.dir.y - self.player.dir.x * self.player.plane.y)
        transform_x = inv_det * (self.player.dir.y * sprite_x - self.player.dir.x * sprite_y)
        transform_y = inv_det * (-self.player.plane.y * sprite_x + self.player.plane.x * sprite_y)
        return Coords(transform_x, transform_y)

    def _update_entities(self):
        i = 0
        while i < self.num_entities:
            ent = self.entities[i]
            ent.distance = self._coords_distance(self.player.pos, ent.pos)
            if ent.timer > 0:
                ent.timer -= 1
            if ent.distance > MAX_ENTITY_DISTANCE:
                self._remove_entity(ent.uid)
                continue
            if ent.state == S_HIDDEN:
                i += 1
                continue

            if ent.etype == E_ENEMY:
                self._update_enemy(ent)
            elif ent.etype == E_FIREBALL:
                if ent.distance < FIREBALL_COLLIDER_DIST:
                    self.player.health = max(0, self.player.health - ENEMY_FIREBALL_DAMAGE)
                    self.log.append("fireballed")
                    self._remove_entity(ent.uid)
                    continue
                angle = ent.health / FIREBALL_ANGLES * math.pi
                moved = self._update_position(
                    ent.pos,
                    math.cos(angle) * FIREBALL_SPEED,
                    math.sin(angle) * FIREBALL_SPEED,
                    only_walls=True,
                )
                if not moved:
                    self._remove_entity(ent.uid)
                    continue
            elif ent.etype == E_MEDIKIT:
                if ent.distance < ITEM_COLLIDER_DIST:
                    ent.state = S_HIDDEN
                    self.player.health = min(PLAYER_MAX_HEALTH, self.player.health + 50)
                    self.message = "medikit"
                    self.log.append("medikit")
            elif ent.etype == E_KEY and ent.distance < ITEM_COLLIDER_DIST:
                ent.state = S_HIDDEN
                self.player.keys += 1
                self.message = "key"
                self.log.append("key")

            i += 1

    def _update_enemy(self, ent: Entity):
        if ent.health <= 0:
            if ent.state != S_DEAD:
                ent.state = S_DEAD
                ent.timer = 6
            return
        if ent.state == S_HIT:
            if ent.timer == 0:
                ent.state = S_ALERT
                ent.timer = 30
            return
        if ent.state == S_FIRING:
            if ent.timer == 0:
                ent.state = S_ALERT
                ent.timer = 30
            return

        if ENEMY_MELEE_DIST < ent.distance < MAX_ENEMY_VIEW:
            if ent.state != S_ALERT:
                ent.state = S_ALERT
                ent.timer = 20
            else:
                if ent.timer == 0:
                    self._spawn_fireball(ent.pos.x, ent.pos.y)
                    ent.state = S_FIRING
                    ent.timer = 6
                else:
                    dx = self.player.pos.x - ent.pos.x
                    dy = self.player.pos.y - ent.pos.y
                    self._update_position(
                        ent.pos,
                        (1 if dx > 0 else -1 if dx < 0 else 0) * ENEMY_SPEED,
                        (1 if dy > 0 else -1 if dy < 0 else 0) * ENEMY_SPEED,
                        only_walls=True,
                    )
        elif ent.distance <= ENEMY_MELEE_DIST:
            if ent.state != S_MELEE:
                ent.state = S_MELEE
                ent.timer = 10
            elif ent.timer == 0:
                self.player.health = max(0, self.player.health - ENEMY_MELEE_DAMAGE)
                ent.timer = 14
                self.message = "bitten"
                self.log.append("bitten")
        else:
            ent.state = S_STAND

    def _use(self):
        dx = self.player.dir.x
        dy = self.player.dir.y
        for dist in (0.7, 1.4, 2.1):
            x = int(self.player.pos.x + dx * dist)
            y = int(self.player.pos.y + dy * dist)
            block = self._get_block(x, y)
            if block == E_DOOR:
                self._set_block(x, y, ".")
                self.message = "door opened"
                self.log.append("door opened")
                return True
            if block == E_LOCKEDDOOR:
                if self.player.keys > 0:
                    self.player.keys -= 1
                    self._set_block(x, y, ".")
                    self.message = "locked door opened"
                    self.log.append("locked door opened")
                    return True
                self.message = "locked"
                self.log.append("locked")
                return False
        self.message = "no door"
        return False

    def tick(self, raw_command: str) -> str:
        if self.finished:
            return self.render() + "\nGAME OVER — press F10 to restart"
        cmd = raw_command.strip().lower()
        if not cmd or cmd == "look":
            return self.render()

        if cmd in ("w", "forward"):
            self.player.velocity += (MOV_SPEED - self.player.velocity) * 0.4
        elif cmd in ("s", "back"):
            self.player.velocity += (-MOV_SPEED - self.player.velocity) * 0.4
        elif cmd in ("a", "turn-left", "turn_left"):
            self._rotate(-ROT_SPEED)
            self.player.velocity *= 0.5
        elif cmd in ("d", "turn-right", "turn_right"):
            self._rotate(ROT_SPEED)
            self.player.velocity *= 0.5
        elif cmd in ("q", "strafe-left"):
            self._strafe(-MOV_SPEED)
            self.player.velocity *= 0.5
        elif cmd in ("e", "strafe-right"):
            self._strafe(MOV_SPEED)
            self.player.velocity *= 0.5
        elif cmd in ("shoot", "fire"):
            self._fire()
            self.player.velocity *= 0.5
        elif cmd in ("use", "u", "open"):
            self._use()
            self.player.velocity *= 0.5
        else:
            return f"unknown command '{raw_command}' — try w/s/a/d/q/e/shoot/use"

        if cmd not in ("w", "forward", "s", "back"):
            self.player.velocity *= 0.5
            if abs(self.player.velocity) < 0.003:
                self.player.velocity = 0.0

        if abs(self.player.velocity) > 0.003:
            self._update_position(
                self.player.pos,
                self.player.dir.x * self.player.velocity,
                self.player.dir.y * self.player.velocity,
            )

        self._update_entities()
        self.turns += 1
        self.frame_count += 1
        self._check_end()
        return self.render()

    def _rotate(self, angle: float):
        old_dir_x = self.player.dir.x
        self.player.dir.x = self.player.dir.x * math.cos(angle) - self.player.dir.y * math.sin(angle)
        self.player.dir.y = old_dir_x * math.sin(angle) + self.player.dir.y * math.cos(angle)
        old_plane_x = self.player.plane.x
        self.player.plane.x = self.player.plane.x * math.cos(angle) - self.player.plane.y * math.sin(angle)
        self.player.plane.y = old_plane_x * math.sin(angle) + self.player.plane.y * math.cos(angle)

    def _strafe(self, amount: float):
        angle = math.atan2(self.player.dir.y, self.player.dir.x) + math.pi / 2
        self._update_position(
            self.player.pos,
            math.cos(angle) * amount,
            math.sin(angle) * amount,
        )

    def _check_end(self):
        px = int(self.player.pos.x)
        py = int(self.player.pos.y)
        if self.player.health <= 0:
            self.finished = True
            self.won = False
            self.message = "game over"
            self.log.append("you died")
        elif self._get_block(px, py) == E_EXIT:
            self.finished = True
            self.won = True
            self.message = "escaped"
            self.log.append("escaped")

    def _render_map(self) -> list[list[str]]:
        frame = [[CEILING_CHAR for _ in range(SCREEN_WIDTH)] for _ in range(RENDER_HEIGHT)]
        last_uid = 0
        for x in range(0, SCREEN_WIDTH, RES_DIVIDER):
            camera_x = 2 * x / SCREEN_WIDTH - 1
            ray_x = self.player.dir.x + self.player.plane.x * camera_x
            ray_y = self.player.dir.y + self.player.plane.y * camera_x
            map_x = int(self.player.pos.x)
            map_y = int(self.player.pos.y)
            map_coords = Coords(self.player.pos.x, self.player.pos.y)
            delta_x = abs(1 / (ray_x or 1e-9))
            delta_y = abs(1 / (ray_y or 1e-9))
            step_x = 1 if ray_x > 0 else -1
            step_y = 1 if ray_y > 0 else -1
            side_x = (map_x + 1.0 - self.player.pos.x) * delta_x if ray_x > 0 else (self.player.pos.x - map_x) * delta_x
            side_y = (map_y + 1.0 - self.player.pos.y) * delta_y if ray_y > 0 else (self.player.pos.y - map_y) * delta_y
            hit = False
            side = 0
            depth = 0
            while not hit and depth < MAX_RENDER_DEPTH:
                if side_x < side_y:
                    side_x += delta_x
                    map_x += step_x
                    side = 0
                else:
                    side_y += delta_y
                    map_y += step_y
                    side = 1
                block = self._get_block(map_x, map_y)
                if block in (E_WALL, E_LOCKEDDOOR):
                    hit = True
                else:
                    if (block == E_ENEMY or block >= 0x8) and self._coords_distance(
                        self.player.pos, map_coords
                    ) < MAX_ENTITY_DISTANCE:
                        uid = self._create_uid(block, map_x, map_y)
                        if last_uid != uid:
                            spawned = False
                            for ent in self.entities:
                                if ent.uid == uid:
                                    spawned = True
                                    break
                            if not spawned:
                                self._spawn_entity(block, map_x, map_y)
                            last_uid = uid
                depth += 1

            if hit:
                if side == 0:
                    distance = (map_x - self.player.pos.x + (1 - step_x) / 2) / (ray_x or 1e-9)
                else:
                    distance = (map_y - self.player.pos.y + (1 - step_y) / 2) / (ray_y or 1e-9)
                distance = max(0.1, distance)
                z_index = min(int(distance * DISTANCE_MULTIPLIER), 255)
                for z in range(x, min(x + RES_DIVIDER, len(self.zbuffer))):
                    self.zbuffer[z] = z_index
                line_height = int(RENDER_HEIGHT / distance)
                line_height = max(1, line_height)
                view_height = (
                    abs(math.sin(self.frame_count * JOGGING_SPEED)) * 2 * (1 if abs(self.player.velocity) > 0.1 else 0)
                )
                center = HALF_HEIGHT + int(view_height / distance)
                top = max(0, center - line_height // 2)
                bottom = min(RENDER_HEIGHT - 1, center + line_height // 2)
                intensity = max(0, GRADIENT_COUNT - 1 - int(distance / MAX_RENDER_DEPTH * GRADIENT_COUNT) - side * 2)
                for y in range(top, bottom + 1):
                    for col in range(x, min(x + RES_DIVIDER, SCREEN_WIDTH)):
                        frame[y][col] = GRADIENT[min(intensity, GRADIENT_COUNT - 1)]
            else:
                for z in range(x, min(x + RES_DIVIDER, len(self.zbuffer))):
                    self.zbuffer[z] = 255
        return frame

    def _render_entities(self, frame: list[list[str]]):
        self.entities.sort(key=lambda e: -e.distance)
        for ent in self.entities:
            if ent.state == S_HIDDEN:
                continue
            transform = self._translate_into_view(ent.pos)
            if transform.y <= 0.1 or transform.y > MAX_SPRITE_DEPTH:
                continue
            sprite_x = int(HALF_WIDTH * (1.0 + transform.x / transform.y))
            sprite_y = int(HALF_HEIGHT + 2 / transform.y)
            size = max(1, int(4 / transform.y))
            z_index = int(transform.y * DISTANCE_MULTIPLIER)
            left = max(0, sprite_x - size // 2)
            right = min(SCREEN_WIDTH - 1, sprite_x + size // 2)
            top = max(0, sprite_y - size // 2)
            bottom = min(RENDER_HEIGHT - 1, sprite_y + size // 2)
            char = "?"
            if ent.etype == E_ENEMY:
                char = "&" if ent.distance < 2 else "M"
            elif ent.etype == E_FIREBALL:
                char = "*"
            elif ent.etype == E_MEDIKIT:
                char = "+"
            elif ent.etype == E_KEY:
                char = "K"
            for col in range(left, right + 1):
                if col < 0 or col >= len(self.zbuffer):
                    continue
                if self.zbuffer[col // Z_RES_DIVIDER] < z_index:
                    continue
                for row in range(top, bottom + 1):
                    if 0 <= row < RENDER_HEIGHT:
                        frame[row][col] = char

    def render(self) -> str:
        frame = self._render_map()
        self._render_entities(frame)
        cross_x = HALF_WIDTH
        cross_y = HALF_HEIGHT
        if 0 <= cross_x < SCREEN_WIDTH and 0 <= cross_y < RENDER_HEIGHT:
            frame[cross_y][cross_x] = "+"

        alive = [e for e in self.entities if e.etype == E_ENEMY and e.state not in (S_DEAD, S_HIDDEN)]
        enemy_status = "down" if not alive else f"{len(alive)}"
        hud = (
            f" doom  enemies={enemy_status}  hp={self.player.health}  keys={self.player.keys}  "
            f"turns={self.turns}  pos=({self.player.pos.x:.1f},{self.player.pos.y:.1f})"
        )
        footer = f" {self.message[: SCREEN_WIDTH - 2]}"
        lines = [hud[:SCREEN_WIDTH], footer[:SCREEN_WIDTH]]
        lines.extend("".join(row) for row in frame)
        if self.finished:
            lines.append("YOU ESCAPED" if self.won else "GAME OVER")
        return "\n".join(lines)

    def tactical_summary(self) -> str:
        alive = [e for e in self.entities if e.etype == E_ENEMY and e.state not in (S_DEAD, S_HIDDEN)]
        if not alive:
            return "no enemies visible."
        lines = []
        for ent in alive:
            dx = ent.pos.x - self.player.pos.x
            dy = ent.pos.y - self.player.pos.y
            distance = math.hypot(dx, dy)
            angle = math.atan2(dy, dx) - math.atan2(self.player.dir.y, self.player.dir.x)
            while angle <= -math.pi:
                angle += math.tau
            while angle > math.pi:
                angle -= math.tau
            direction = "ahead" if abs(angle) < 0.3 else "left" if angle < 0 else "right"
            behind = abs(angle) > math.pi / 2
            lines.append(
                f"enemy at distance {distance:.1f}, {direction}{' (behind you)' if behind else ''}, hp={ent.health}"
            )
        return "\n".join(lines)


class DoomService:
    """Thin bridge between the nano-doom Lua module and the Python game model."""

    def __init__(self, organism=None, lua_lock=None):
        self.organism = organism
        self._lua_lock = lua_lock
        self._game: _DoomGame | None = None

    def available(self):
        return True

    def running(self):
        return self._game is not None and not self._game.finished

    def status(self):
        if self._game is None:
            return "no game running — press F10 in the DOOM pane"
        return self._game.render()

    def tactical(self):
        if self._game is None:
            return "no game running"
        return self._game.tactical_summary()

    def can_shoot(self):
        if self._game is None:
            return False
        return self._game.can_shoot()

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
        if cmd == "start":
            return self.start()
        if self._game is None:
            raise RuntimeError("no game running")
        return self._game.tick(cmd)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False

