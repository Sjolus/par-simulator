import json
import math
import os
import sys
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

import pygame
from SimConnect import AircraftRequests, SimConnect


# ---------------------------
# Configuration
# ---------------------------
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "par_config.json")

TARGET_CALLSIGN = None  # e.g. "A1234" to lock to a target

RUNWAY_LAT = 49.0128
RUNWAY_LON = 2.5500
RUNWAY_ELEV_FT = 400.0
RUNWAY_HEADING_DEG = 160.0

GLIDESLOPE_DEG = 3.0
MAX_RANGE_NM = 10.0

WINDOW_SIZE = (900, 800)
FPS = 30
POLL_HZ = 2.0
HISTORY_SECONDS = 15
LOG_HISTORY = 200
LOG_VISIBLE_LINES = 8

AIRPORT_CONFIGS: Dict[str, Dict] = {}
RUNWAY_CONFIGS: Dict[str, Dict] = {}
ACTIVE_AIRPORT_KEY: Optional[str] = None
ACTIVE_RUNWAY_KEY: Optional[str] = None
LOG_LINES: Deque[str] = deque(maxlen=LOG_HISTORY)
LOG_SELECTED_INDEX: Optional[int] = None

# Colors
BG = (52, 52, 52)
FRAME = (43, 83, 146)
CYAN = (30, 190, 255)
YELLOW = (255, 235, 60)
GREEN = (60, 220, 60)
ORANGE = (255, 180, 60)
WHITE = (240, 240, 240)
DOT = (235, 235, 235)


# ---------------------------
# Helpers
# ---------------------------
FEET_PER_NM = 6076.12


def _nm_to_ft(nm: float) -> float:
    return nm * FEET_PER_NM


def _geodetic_to_local_m(lat: float, lon: float, ref_lat: float, ref_lon: float) -> Tuple[float, float]:
    dlat = math.radians(lat - ref_lat)
    dlon = math.radians(lon - ref_lon)
    x = dlon * math.cos(math.radians(ref_lat)) * 6371000.0
    y = dlat * 6371000.0
    return x, y


def _rotate(x: float, y: float, heading_deg: float) -> Tuple[float, float]:
    theta = math.radians(heading_deg)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    return x * cos_t + y * sin_t, -x * sin_t + y * cos_t


def _pick_target(targets: List[Dict]) -> Optional[Dict]:
    if not targets:
        return None
    if TARGET_CALLSIGN:
        for t in targets:
            if str(t.get("callsign") or "").strip().upper() == TARGET_CALLSIGN.upper():
                return t
    return targets[0]


def _compute_track(t: Dict) -> Optional[Dict]:
    if t is None:
        return None
    lat = t.get("lat")
    lon = t.get("lon")
    alt = t.get("alt")
    if lat is None or lon is None or alt is None:
        return None

    x_m, y_m = _geodetic_to_local_m(lat, lon, RUNWAY_LAT, RUNWAY_LON)
    along_m, cross_m = _rotate(x_m, y_m, RUNWAY_HEADING_DEG)

    range_nm = max(0.0, (along_m / 1852.0))
    height_ft = float(alt) - RUNWAY_ELEV_FT

    return {
        "callsign": (t.get("callsign") or "").strip(),
        "range_nm": range_nm,
        "cross_m": cross_m,
        "height_ft": height_ft,
        "gs": t.get("gs"),
        "vs": t.get("vs"),
    }


def _glidepath_height_ft(range_nm: float) -> float:
    return math.tan(math.radians(GLIDESLOPE_DEG)) * _nm_to_ft(range_nm)


def _apply_runway(active: Dict) -> None:
    global RUNWAY_LAT, RUNWAY_LON, RUNWAY_ELEV_FT, RUNWAY_HEADING_DEG
    global GLIDESLOPE_DEG, MAX_RANGE_NM

    RUNWAY_LAT = float(active.get("lat", RUNWAY_LAT))
    RUNWAY_LON = float(active.get("lon", RUNWAY_LON))
    RUNWAY_ELEV_FT = float(active.get("elev_ft", RUNWAY_ELEV_FT))
    RUNWAY_HEADING_DEG = float(active.get("heading_deg", RUNWAY_HEADING_DEG))
    GLIDESLOPE_DEG = float(active.get("glideslope_deg", GLIDESLOPE_DEG))
    MAX_RANGE_NM = float(active.get("max_range_nm", MAX_RANGE_NM))


def _log(message: str) -> None:
    timestamp = time.strftime("%H:%M:%S")
    LOG_LINES.appendleft(f"[{timestamp}] {message}")


def _load_config() -> None:
    global TARGET_CALLSIGN
    global WINDOW_SIZE, POLL_HZ, AIRPORT_CONFIGS, RUNWAY_CONFIGS
    global ACTIVE_AIRPORT_KEY, ACTIVE_RUNWAY_KEY

    if not os.path.exists(CONFIG_PATH):
        _log("Config: par_config.json not found, using defaults")
        return

    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        cfg = json.load(handle)

    TARGET_CALLSIGN = cfg.get("target_callsign", TARGET_CALLSIGN)
    POLL_HZ = float(cfg.get("poll_hz", POLL_HZ))
    window = cfg.get("window_size")
    if isinstance(window, list) and len(window) == 2:
        WINDOW_SIZE = (int(window[0]), int(window[1]))

    AIRPORT_CONFIGS = cfg.get("airports", {})
    ACTIVE_AIRPORT_KEY = cfg.get("active_airport")
    ACTIVE_RUNWAY_KEY = cfg.get("active_runway")

    _log(f"Config: loaded {len(AIRPORT_CONFIGS)} airports")
    if ACTIVE_AIRPORT_KEY:
        _log(f"Config: active airport {ACTIVE_AIRPORT_KEY}")
    if ACTIVE_RUNWAY_KEY:
        _log(f"Config: active runway {ACTIVE_RUNWAY_KEY}")

    airport = AIRPORT_CONFIGS.get(ACTIVE_AIRPORT_KEY) if ACTIVE_AIRPORT_KEY else None
    RUNWAY_CONFIGS = airport.get("runways", {}) if airport else {}
    active = RUNWAY_CONFIGS.get(ACTIVE_RUNWAY_KEY) if ACTIVE_RUNWAY_KEY else None
    if active:
        _apply_runway(active)
        _log("Config: runway applied")


# ---------------------------
# SimConnect data source
# ---------------------------
class SimConnectSource:
    def __init__(self) -> None:
        self.sm = None
        self.aq = None
        self.connected = False
        self.last_error: Optional[str] = None
        self.last_attempt = 0.0
        self.last_poll = 0.0
        self.cache: List[Dict] = []

    def connect(self) -> bool:
        self.last_attempt = time.time()
        try:
            self.sm = SimConnect()
            self.aq = AircraftRequests(self.sm, _time=2000)
            self.connected = True
            self.last_error = None
            _log("SimConnect: connected")
            return True
        except Exception as exc:
            self.connected = False
            self.last_error = str(exc)
            self.sm = None
            self.aq = None
            _log(f"SimConnect: connect failed ({self.last_error})")
            return False

    def _get_ai_object_ids(self) -> List[int]:
        if not self.sm:
            return []
        if hasattr(self.sm, "get_aircraft_list"):
            aircraft = self.sm.get_aircraft_list()
            return [a["object_id"] for a in aircraft if a.get("is_user") is False]

        req = self.sm.RequestDataOnSimObjectType
        data = req(0, 0, self.sm.SIMCONNECT_SIMOBJECT_TYPE_AIRCRAFT)
        return [d["ObjectID"] for d in data if d.get("IsUser") is False]

    def poll(self) -> List[Dict]:
        if not self.connected or not self.aq:
            return self.cache
        now = time.time()
        if now - self.last_poll < 1.0 / POLL_HZ:
            return self.cache

        self.last_poll = now
        targets: List[Dict] = []
        try:
            for obj_id in self._get_ai_object_ids():
                targets.append(
                    {
                        "id": obj_id,
                        "lat": self.aq.get("PLANE LATITUDE", _simconnect_id=obj_id),
                        "lon": self.aq.get("PLANE LONGITUDE", _simconnect_id=obj_id),
                        "alt": self.aq.get("PLANE ALTITUDE", _simconnect_id=obj_id),
                        "hdg": self.aq.get("PLANE HEADING DEGREES TRUE", _simconnect_id=obj_id),
                        "pitch": self.aq.get("PLANE PITCH DEGREES", _simconnect_id=obj_id),
                        "bank": self.aq.get("PLANE BANK DEGREES", _simconnect_id=obj_id),
                        "gs": self.aq.get("GROUND VELOCITY", _simconnect_id=obj_id),
                        "vs": self.aq.get("VERTICAL SPEED", _simconnect_id=obj_id),
                        "callsign": self.aq.get("ATC ID", _simconnect_id=obj_id),
                    }
                )
        except Exception as exc:
            self.connected = False
            self.last_error = str(exc)
            self.sm = None
            self.aq = None
            _log(f"SimConnect: lost connection ({self.last_error})")
            return self.cache

        if not targets:
            _log("SimConnect: no AI targets in range")

        self.cache = targets
        return targets


# ---------------------------
# Rendering
# ---------------------------
class ParDisplay:
    def __init__(self) -> None:
        pygame.init()
        pygame.scrap.init()
        self.screen = pygame.display.set_mode(WINDOW_SIZE)
        pygame.display.set_caption("PAR Display")
        self.clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("Arial", 18)
        self.font_small = pygame.font.SysFont("Arial", 14)
        self.history: Deque[Dict] = deque(maxlen=int(HISTORY_SECONDS * FPS))
        self.dropdown_open = None

    def _dropdown_rect(self) -> pygame.Rect:
        return pygame.Rect(WINDOW_SIZE[0] - 220, 16, 200, 28)

    def _runway_dropdown_rect(self) -> pygame.Rect:
        return pygame.Rect(WINDOW_SIZE[0] - 220, 50, 200, 28)

    def _connect_rect(self) -> pygame.Rect:
        return pygame.Rect(WINDOW_SIZE[0] - 220, 84, 200, 28)

    def _log_rect(self) -> pygame.Rect:
        return pygame.Rect(20, 120, WINDOW_SIZE[0] - 40, 18 + LOG_VISIBLE_LINES * 18)

    def _copy_rect(self) -> pygame.Rect:
        rect = self._log_rect()
        return pygame.Rect(rect.right - 90, rect.top + 4, 80, 22)

    def _dropdown_item_rect(self, index: int) -> pygame.Rect:
        base = self._dropdown_rect()
        return pygame.Rect(base.left, base.bottom + 4 + index * 24, base.width, 22)

    def _runway_item_rect(self, index: int) -> pygame.Rect:
        base = self._runway_dropdown_rect()
        return pygame.Rect(base.left, base.bottom + 4 + index * 24, base.width, 22)

    def _draw_dropdown(self) -> None:
        airport_rect = self._dropdown_rect()
        runway_rect = self._runway_dropdown_rect()

        pygame.draw.rect(self.screen, FRAME, airport_rect, 2)
        airport_label = ACTIVE_AIRPORT_KEY or "Select airport"
        airport_text = self.font_small.render(airport_label, True, WHITE)
        self.screen.blit(airport_text, (airport_rect.left + 8, airport_rect.top + 5))

        pygame.draw.rect(self.screen, FRAME, runway_rect, 2)
        runway_label = ACTIVE_RUNWAY_KEY or "Select runway"
        runway_text = self.font_small.render(runway_label, True, WHITE)
        self.screen.blit(runway_text, (runway_rect.left + 8, runway_rect.top + 5))

        if self.dropdown_open == "airport":
            for idx, key in enumerate(sorted(AIRPORT_CONFIGS.keys())):
                item_rect = self._dropdown_item_rect(idx)
                pygame.draw.rect(self.screen, BG, item_rect)
                pygame.draw.rect(self.screen, FRAME, item_rect, 1)
                item_text = self.font_small.render(key, True, WHITE)
                self.screen.blit(item_text, (item_rect.left + 8, item_rect.top + 3))

        if self.dropdown_open == "runway":
            for idx, key in enumerate(sorted(RUNWAY_CONFIGS.keys())):
                item_rect = self._runway_item_rect(idx)
                pygame.draw.rect(self.screen, BG, item_rect)
                pygame.draw.rect(self.screen, FRAME, item_rect, 1)
                item_text = self.font_small.render(key, True, WHITE)
                self.screen.blit(item_text, (item_rect.left + 8, item_rect.top + 3))

    def handle_click(self, pos: Tuple[int, int]) -> Optional[str]:
        if self._copy_rect().collidepoint(pos):
            return "copy-log"

        if self._log_rect().collidepoint(pos):
            rect = self._log_rect()
            line_y = pos[1] - (rect.top + 28)
            if line_y >= 0:
                idx = line_y // 18
                if 0 <= idx < min(LOG_VISIBLE_LINES, len(LOG_LINES)):
                    global LOG_SELECTED_INDEX
                    LOG_SELECTED_INDEX = int(idx)
            return None

        if self._connect_rect().collidepoint(pos):
            return "connect"

        if self._dropdown_rect().collidepoint(pos):
            self.dropdown_open = None if self.dropdown_open == "airport" else "airport"
            return None

        if self._runway_dropdown_rect().collidepoint(pos):
            self.dropdown_open = None if self.dropdown_open == "runway" else "runway"
            return None

        if self.dropdown_open == "airport":
            for idx, key in enumerate(sorted(AIRPORT_CONFIGS.keys())):
                if self._dropdown_item_rect(idx).collidepoint(pos):
                    self.dropdown_open = None
                    return f"airport:{key}"

        if self.dropdown_open == "runway":
            for idx, key in enumerate(sorted(RUNWAY_CONFIGS.keys())):
                if self._runway_item_rect(idx).collidepoint(pos):
                    self.dropdown_open = None
                    return f"runway:{key}"

        self.dropdown_open = None
        return None

    def _draw_frame(self, rect: pygame.Rect) -> None:
        pygame.draw.rect(self.screen, FRAME, rect, 2)

    def _draw_labels(self) -> None:
        lines = [
            f"RWY {int(round(RUNWAY_HEADING_DEG))}",
            "QNH 1010",
            "STS OK",
        ]
        y = 20
        for line in lines:
            text = self.font.render(line, True, WHITE)
            self.screen.blit(text, (20, y))
            y += 22

    def _draw_log(self) -> None:
        rect = self._log_rect()
        pygame.draw.rect(self.screen, BG, rect)
        pygame.draw.rect(self.screen, FRAME, rect, 2)

        header = self.font_small.render("Log", True, WHITE)
        self.screen.blit(header, (rect.left + 6, rect.top + 4))

        copy_rect = self._copy_rect()
        pygame.draw.rect(self.screen, FRAME, copy_rect, 1)
        copy_text = self.font_small.render("Copy", True, WHITE)
        self.screen.blit(copy_text, (copy_rect.left + 10, copy_rect.top + 4))

        base_y = rect.top + 28
        visible = list(LOG_LINES)[:LOG_VISIBLE_LINES]
        for idx, line in enumerate(visible):
            y = base_y + idx * 18
            if LOG_SELECTED_INDEX is not None and LOG_SELECTED_INDEX == idx:
                pygame.draw.rect(self.screen, (70, 70, 70), (rect.left + 4, y - 2, rect.width - 8, 18))
            text = self.font_small.render(line, True, WHITE)
            self.screen.blit(text, (rect.left + 6, y))

    def _draw_status(self, connected: bool, error: Optional[str]) -> None:
        status = "SIM: CONNECTED" if connected else "SIM: DISCONNECTED"
        color = GREEN if connected else ORANGE
        text = self.font_small.render(status, True, color)
        self.screen.blit(text, (20, 88))
        if error and not connected:
            err = self.font_small.render("Click Connect when sim is running", True, WHITE)
            self.screen.blit(err, (20, 108))

    def _draw_connect_button(self, connected: bool) -> None:
        rect = self._connect_rect()
        pygame.draw.rect(self.screen, FRAME, rect, 2)
        label = "Connect" if not connected else "Reconnect"
        text = self.font_small.render(label, True, WHITE)
        self.screen.blit(text, (rect.left + 8, rect.top + 5))

    def _draw_elevation(self, rect: pygame.Rect, track: Optional[Dict]) -> None:
        pygame.draw.rect(self.screen, BG, rect)
        self._draw_frame(rect)

        left = rect.left + 20
        right = rect.right - 20
        bottom = rect.bottom - 30
        top = rect.top + 20

        pygame.draw.line(self.screen, CYAN, (left, bottom), (right, top), 2)
        pygame.draw.line(self.screen, YELLOW, (left, bottom - 5), (right, bottom - 120), 2)

        for i in range(1, 11):
            x = left + (right - left) * (i / 10.0)
            color = GREEN if i % 2 == 0 else ORANGE
            pygame.draw.line(self.screen, color, (x, bottom), (x, top), 1)

        if not track:
            return

        range_nm = track["range_nm"]
        if range_nm > MAX_RANGE_NM:
            return

        gx = left + (right - left) * (range_nm / MAX_RANGE_NM)
        glide_ft = _glidepath_height_ft(range_nm)
        max_alt_ft = _glidepath_height_ft(MAX_RANGE_NM) * 1.2
        alt_ft = track["height_ft"]

        def y_from_alt(a_ft: float) -> float:
            return bottom - (a_ft / max_alt_ft) * (bottom - top)

        pygame.draw.circle(self.screen, WHITE, (int(gx), int(y_from_alt(glide_ft))), 5, 1)
        pygame.draw.circle(self.screen, DOT, (int(gx), int(y_from_alt(alt_ft))), 5)

        label = f"{track['callsign']}"
        text = self.font.render(label, True, WHITE)
        self.screen.blit(text, (gx + 10, y_from_alt(alt_ft) - 20))

    def _draw_azimuth(self, rect: pygame.Rect, track: Optional[Dict]) -> None:
        pygame.draw.rect(self.screen, BG, rect)
        self._draw_frame(rect)

        left = rect.left + 20
        right = rect.right - 20
        top = rect.top + 20
        bottom = rect.bottom - 20
        center_y = (top + bottom) // 2

        pygame.draw.line(self.screen, CYAN, (left, center_y), (right, top), 2)
        pygame.draw.line(self.screen, CYAN, (left, center_y), (right, bottom), 2)
        pygame.draw.line(self.screen, YELLOW, (left, center_y), (right, center_y), 2)

        for i in range(1, 11):
            x = left + (right - left) * (i / 10.0)
            color = GREEN if i % 2 == 0 else ORANGE
            pygame.draw.line(self.screen, color, (x, bottom), (x, top), 1)

        if not track:
            return

        range_nm = track["range_nm"]
        if range_nm > MAX_RANGE_NM:
            return

        gx = left + (right - left) * (range_nm / MAX_RANGE_NM)
        max_cross_m = 600.0
        cross_m = track["cross_m"]
        cy = center_y - (cross_m / max_cross_m) * (bottom - top) / 2

        pygame.draw.circle(self.screen, DOT, (int(gx), int(cy)), 5)

        label = f"{track['callsign']}"
        text = self.font.render(label, True, WHITE)
        self.screen.blit(text, (gx + 10, cy - 20))

    def render(self, track: Optional[Dict], connected: bool, error: Optional[str]) -> None:
        self.screen.fill(BG)
        header_height = 140
        available_height = WINDOW_SIZE[1] - header_height - 40
        panel_height = available_height // 2
        top_rect = pygame.Rect(20, header_height, WINDOW_SIZE[0] - 40, panel_height)
        bottom_rect = pygame.Rect(20, top_rect.bottom + 20, WINDOW_SIZE[0] - 40, panel_height)

        self._draw_elevation(top_rect, track)
        self._draw_azimuth(bottom_rect, track)

        self._draw_labels()
        self._draw_dropdown()
        self._draw_status(connected, error)
        self._draw_connect_button(connected)
        self._draw_log()

        pygame.display.flip()


# ---------------------------
# Main loop
# ---------------------------
def main() -> None:
    global ACTIVE_RUNWAY_KEY, ACTIVE_AIRPORT_KEY, RUNWAY_CONFIGS, LOG_SELECTED_INDEX
    _log("App: starting")
    _load_config()
    display = ParDisplay()
    source = SimConnectSource()
    source.connect()
    running = True

    last_track = None
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                selected = display.handle_click(event.pos)
                if selected:
                    if selected == "copy-log":
                        if LOG_SELECTED_INDEX is None:
                            text = "\n".join(reversed(list(LOG_LINES)[:LOG_VISIBLE_LINES]))
                        else:
                            text = list(LOG_LINES)[LOG_SELECTED_INDEX]
                        try:
                            pygame.scrap.put(pygame.SCRAP_TEXT, text.encode("utf-8"))
                            _log("Log copied to clipboard")
                        except Exception:
                            _log("Log copy failed")
                    if selected == "connect":
                        source.connect()
                    if selected.startswith("airport:"):
                        key = selected.split(":", 1)[1]
                        if key in AIRPORT_CONFIGS:
                            ACTIVE_AIRPORT_KEY = key
                            RUNWAY_CONFIGS = AIRPORT_CONFIGS[key].get("runways", {})
                            ACTIVE_RUNWAY_KEY = next(iter(sorted(RUNWAY_CONFIGS.keys())), None)
                            if ACTIVE_RUNWAY_KEY:
                                _apply_runway(RUNWAY_CONFIGS[ACTIVE_RUNWAY_KEY])
                    if selected.startswith("runway:"):
                        key = selected.split(":", 1)[1]
                        if key in RUNWAY_CONFIGS:
                            ACTIVE_RUNWAY_KEY = key
                            _apply_runway(RUNWAY_CONFIGS[key])

        targets = source.poll()
        target = _pick_target(targets)
        last_track = _compute_track(target)

        display.render(last_track, source.connected, source.last_error)
        display.clock.tick(FPS)

    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()
