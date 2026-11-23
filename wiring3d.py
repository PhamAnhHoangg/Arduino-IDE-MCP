"""
3D step-by-step breadboard wiring page.

build_html(title, board, parts, steps) -> one self-contained HTML page:
a tilted 3D breadboard scene, one step at a time (Prev / Next, arrow keys),
new wires bright, earlier wires faded. No text on the scene: colours only.

Wire ends:
  "j10"           breadboard hole (rows a-j, columns 1-30)
  "+top" "-bot"   power rails, optional column: "-bot:13"
  "pin:10"        board header pin (10, GND, 5V, A0, ...)
  "servo.SIG"     terminal of an off-board part
  "U1.3"          pin 3 of a dip chip -> a free hole in that chip pin's column
"""

import html
import json
import re

P = 30
ROW = {'a': 260, 'b': 290, 'c': 320, 'd': 350, 'e': 380, 'f': 440, 'g': 470, 'h': 500, 'i': 530, 'j': 560}
RAIL = {'+top': 180, '-top': 210, '+bot': 610, '-bot': 640}
COLS = 30
COLORS = {"red": "#e53935", "black": "#263238", "blue": "#1e88e5", "orange": "#fb8c00", "green": "#43a047",
          "yellow": "#fdd835", "purple": "#8e24aa", "teal": "#00acc1", "brown": "#795548", "white": "#eceff1",
          "grey": "#90a4ae", "gray": "#90a4ae", "pink": "#ec407a"}
CYCLE = ["#8e24aa", "#1e88e5", "#00acc1", "#43a047", "#fb8c00", "#ec407a", "#fdd835"]

BOARDS = {
    "mega": dict(name="ARDUINO MEGA 2560", w=1060, top=[["SCL", "SDA", "AREF", "GND", "13", "12", "11", "10", "9", "8"],
                 ["7", "6", "5", "4", "3", "2", "1", "0"]],
                 bot=[["IOREF", "RESET", "3.3V", "5V", "GND", "GND2", "VIN"], [f"A{i}" for i in range(8)],
                      [f"A{i}" for i in range(8, 16)]], chip="ATmega2560", side=True),
    "uno": dict(name="ARDUINO UNO", w=760, top=[["SCL", "SDA", "AREF", "GND", "13", "12", "11", "10", "9", "8"],
                ["7", "6", "5", "4", "3", "2", "1", "0"]],
                bot=[["IOREF", "RESET", "3.3V", "5V", "GND", "GND2", "VIN"], [f"A{i}" for i in range(6)]],
                chip="ATmega328P", side=False),
}


def _col(c):
    return 360 + (c - 1) * P


def _e(s):
    return html.escape(str(s))


class Scene:
    def __init__(self, board, parts):
        self.board_key = board.lower()
        if "mega" in self.board_key:
            self.bd = dict(BOARDS["mega"])
        elif "uno" in self.board_key:
            self.bd = dict(BOARDS["uno"])
        else:
            self.bd = dict(BOARDS["uno"], name=board.upper(), chip="MCU")
        if "elegoo" in self.board_key:
            self.bd["name"] = self.bd["name"].replace("ARDUINO", "ELEGOO")
        self.parts = {p["id"]: p for p in parts}
        self.used_rail_cols = set()
        self.pin = {}
        x = 440
        for grp in self.bd["top"]:
            for i, l in enumerate(grp):
                self.pin[l] = (x + 28 * i, 880)
            x += 28 * len(grp) + 24
        self.bpin = {}
        x = 440
        for grp in self.bd["bot"]:
            for i, l in enumerate(grp):
                self.bpin[l] = (x + 28 * i, 1188)
                self.pin.setdefault(l, self.bpin[l])
            x += 28 * len(grp) + 24
        # off-board slots
        slots = [(1000, 720), (1440, 800), (1440, 560), (1440, 340), (120, 700), (120, 450)]
        self.term = {}
        si = 0
        for p in parts:
            t = p.get("type", "generic")
            if t in ("servo", "motor", "battery9v", "generic", "sensor"):
                p["_xy"] = p.get("xy") or slots[min(si, len(slots) - 1)]
                si += 1
                x0, y0 = p["_xy"]
                if t == "servo":
                    names = ["GND", "V+", "SIG"]
                    for i, n in enumerate(names):
                        self.term[f'{p["id"]}.{n}'] = (x0 - 12 + 18 * i, y0)
                    for a, b in (("BROWN", "GND"), ("RED", "V+"), ("ORANGE", "SIG"), ("-", "GND"), ("+", "V+"), ("S", "SIG")):
                        self.term[f'{p["id"]}.{a}'] = self.term[f'{p["id"]}.{b}']
                elif t == "motor":
                    self.term[f'{p["id"]}.1'] = (x0 + 30, y0)
                    self.term[f'{p["id"]}.2'] = (x0 + 60, y0)
                elif t == "battery9v":
                    self.term[f'{p["id"]}.+'] = (x0 + 25, y0)
                    self.term[f'{p["id"]}.-'] = (x0 + 55, y0)
                else:
                    terms = p.get("terms", ["VCC", "OUT", "GND"])
                    for i, n in enumerate(terms):
                        self.term[f'{p["id"]}.{n}'] = (x0 + 24 + 44 * i, y0)
        for p in parts:
            if p.get("type") == "dip":
                n, c = int(p.get("pins", 8)), int(p.get("col", 10))
                for k in range(1, n // 2 + 1):
                    p.setdefault("_pins", {})[k] = (c + k - 1, "bot")
                    p["_pins"][n + 1 - k] = (c + k - 1, "top")
        self.dip_used = set()

    # ---- resolve an end name to (x, y, hole_label_or_None)
    def end(self, name, other_x=None):
        s = str(name).strip()
        m = re.fullmatch(r"([a-jA-J])(\d+)", s)
        if m:
            return _col(int(m.group(2))), ROW[m.group(1).lower()], m.group(1).lower() + m.group(2)
        m = re.fullmatch(r"([+-])\s*(top|bot|bottom)(?::(\d+))?", s, re.I)
        if m:
            key = m.group(1) + ("top" if m.group(2).lower() == "top" else "bot")
            c = int(m.group(3)) if m.group(3) else self._free_rail_col(key, other_x)
            self.used_rail_cols.add((key, c))
            return _col(c), RAIL[key], None
        m = re.fullmatch(r"(?:pin|board):(.+)", s, re.I)
        if m:
            p = m.group(1).strip().upper().lstrip("D") if m.group(1).strip().upper() not in self.pin else m.group(1).strip().upper()
            if p not in self.pin:
                raise ValueError(f"unknown board pin {s}")
            x, y = self.pin[p]
            return x, y, None
        if "." in s:
            pid, t = s.split(".", 1)
            part = self.parts.get(pid)
            if part and part.get("type") == "dip":
                c, side = part["_pins"][int(t)]
                for r in ("jihgf" if side == "bot" else "abcde"):
                    if (r, c) not in self.dip_used:
                        self.dip_used.add((r, c))
                        return _col(c), ROW[r], f"{r}{c}"
            for key in (s, f"{pid}.{t.upper()}"):
                if key in self.term:
                    return (*self.term[key], None)
        raise ValueError(f"unknown wire end '{s}'")

    def _free_rail_col(self, key, x):
        start = 1 if x is None else max(1, min(COLS, round((x - 360) / P) + 1))
        lo = 5 if "power" in {p.get("type") for p in self.parts.values()} else 1
        for d in range(COLS):
            for c in (start + d, start - d):
                if lo <= c <= COLS and c % 6 and (key, c) not in self.used_rail_cols:
                    return c
        return start


DEFS = '''<defs>
<linearGradient id="bb" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fbfbf7"/><stop offset="1" stop-color="#e4e4dc"/></linearGradient>
<linearGradient id="pcb" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1f7fb5"/><stop offset="1" stop-color="#155e88"/></linearGradient>
<linearGradient id="chip" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3a3a3a"/><stop offset="1" stop-color="#151515"/></linearGradient>
<filter id="sh" x="-20%" y="-20%" width="140%" height="160%"><feDropShadow dx="0" dy="14" stdDeviation="10" flood-color="#000" flood-opacity=".45"/></filter>
</defs>'''


def _breadboard(o, has_power):
    a = o.append
    a('<rect x="290" y="168" width="1010" height="520" rx="12" fill="#b9b9b0"/>')
    a('<rect x="290" y="150" width="1010" height="520" rx="12" fill="url(#bb)" filter="url(#sh)"/>')
    for k, yy in RAIL.items():
        col = COLORS["red"] if "+" in k else COLORS["blue"]
        ly = yy - 15 if "+" in k else yy + 15
        a(f'<line x1="330" y1="{ly}" x2="1270" y2="{ly}" stroke="{col}" stroke-width="3"/>')
        for xx in (312, 1284):
            a(f'<text x="{xx}" y="{yy + 7}" font-size="22" font-weight="800" fill="{col}" text-anchor="middle">{"+" if "+" in k else "−"}</text>')
        for c in range(1, COLS + 1):
            if c % 6:
                a(f'<rect x="{_col(c) - 5}" y="{yy - 5}" width="10" height="10" rx="2" fill="#3b3b36"/>')
    a('<rect x="330" y="400" width="940" height="20" fill="#d6d6cc"/>')
    for r, yy in ROW.items():
        for xx in (335, 1262):
            a(f'<text x="{xx}" y="{yy + 6}" font-size="16" fill="#777" text-anchor="middle">{r}</text>')
        for c in range(1, COLS + 1):
            a(f'<rect x="{_col(c) - 5}" y="{yy - 5}" width="10" height="10" rx="2" fill="#3b3b36"/>')
    for c in range(1, COLS + 1):
        for yy in (242, 590):
            a(f'<text x="{_col(c)}" y="{yy}" font-size="13" fill="#888" text-anchor="middle">{c}</text>')


def _power(o):
    a = o.append
    a('<g filter="url(#sh)"><rect x="335" y="178" width="96" height="482" rx="6" fill="#15161a"/>'
      '<rect x="335" y="160" width="96" height="482" rx="6" fill="#26282e" stroke="#555" stroke-width="2"/>'
      '<rect x="347" y="250" width="72" height="48" rx="4" fill="#0e0e0e"/><circle cx="383" cy="274" r="13" fill="#444"/>'
      '<rect x="355" y="330" width="56" height="34" rx="4" fill="#c0c0c0"/>')
    for yy in (185, 612):
        a(f'<rect x="362" y="{yy}" width="18" height="30" fill="#ffd54f"/><rect x="386" y="{yy}" width="18" height="30" fill="#222" stroke="#999"/>')
    a('<text x="383" y="420" font-size="16" fill="#fff" text-anchor="middle" font-weight="700">POWER</text>'
      '<text x="383" y="440" font-size="16" fill="#fff" text-anchor="middle" font-weight="700">MODULE</text>'
      '<text x="383" y="470" font-size="13" fill="#ffd54f" text-anchor="middle">5V</text></g>')


def _dip(o, p):
    a = o.append
    n, c = int(p.get("pins", 8)), int(p.get("col", 10))
    half = n // 2
    x0, x1 = _col(c) - 16, _col(c + half - 1) + 16
    for k in range(half):
        x = _col(c + k)
        a(f'<rect x="{x - 4}" y="378" width="8" height="22" fill="#cfd8dc"/><rect x="{x - 4}" y="420" width="8" height="22" fill="#cfd8dc"/>')
    a(f'<g filter="url(#sh)"><rect x="{x0}" y="392" width="{x1 - x0}" height="40" rx="4" fill="#0a0a0a"/>'
      f'<rect x="{x0}" y="378" width="{x1 - x0}" height="44" rx="4" fill="url(#chip)"/>'
      f'<circle cx="{x0}" cy="400" r="10" fill="#0a0a0a"/>'
      f'<text x="{(x0 + x1) / 2}" y="407" font-size="{20 if half > 4 else 15}" fill="#eee" text-anchor="middle" font-weight="700">{_e(p.get("label", p["id"]))}</text></g>')
    a(f'<circle cx="{x0 + 14}" cy="412" r="3.5" fill="#999"/>')
    for k in range(half):
        a(f'<text x="{_col(c + k)}" y="461" font-size="13" fill="#c62828" text-anchor="middle" font-weight="700">{k + 1}</text>')
        a(f'<text x="{_col(c + k)}" y="368" font-size="13" fill="#c62828" text-anchor="middle" font-weight="700">{n - k}</text>')
    return (x0 - 30, 350, x1 - x0 + 60, 125)


def _inboard(o, sc, p):
    a = o.append
    t = p["type"]
    xa, ya, _ = sc.end(p["a"])
    xb, yb, _ = sc.end(p.get("b") or p.get("k"))
    if t == "resistor":
        mx, my = (xa + xb) / 2, (ya + yb) / 2
        a(f'<path d="M{xa},{ya} Q{mx},{my - 30} {xb},{yb}" fill="none" stroke="#b0bec5" stroke-width="4"/>'
          f'<rect x="{mx - 30}" y="{my - 30}" width="60" height="20" rx="9" fill="#d7b98e" stroke="#8d6e63" filter="url(#sh)"/>'
          f'<rect x="{mx - 18}" y="{my - 30}" width="5" height="20" fill="#c62828"/><rect x="{mx - 6}" y="{my - 30}" width="5" height="20" fill="#c62828"/>'
          f'<rect x="{mx + 6}" y="{my - 30}" width="5" height="20" fill="#5d4037"/>')
        return (min(xa, xb) - 20, min(ya, yb) - 45, abs(xb - xa) + 40, abs(yb - ya) + 65)
    col = COLORS.get(p.get("color", "red"), p.get("color", "#e53935"))
    mx, my = (xa + xb) / 2, (ya + yb) / 2 - 40
    a(f'<line x1="{xa}" y1="{ya}" x2="{mx - 6}" y2="{my}" stroke="#b0bec5" stroke-width="4"/>'
      f'<line x1="{xb}" y1="{yb}" x2="{mx + 6}" y2="{my}" stroke="#b0bec5" stroke-width="4"/>'
      f'<g filter="url(#sh)"><rect x="{mx - 18}" y="{my - 8}" width="36" height="10" fill="{col}"/>'
      f'<path d="M{mx - 15},{my - 6} v-22 a15,15 0 0 1 30,0 v22 z" fill="{col}" fill-opacity=".85"/>'
      f'<path d="M{mx - 8},{my - 26} a8,8 0 0 1 8,-8" stroke="#fff" stroke-width="3" fill="none" opacity=".6"/></g>'
      f'<text x="{xa}" y="{ya + 24}" font-size="13" fill="#555" text-anchor="middle">+</text>')
    return (min(xa, xb) - 30, my - 50, abs(xb - xa) + 60, 110)


def _offboard(o, p):
    a = o.append
    t = p["type"]
    x0, y0 = p["_xy"]
    lab = _e(p.get("label", ""))
    if t == "servo":
        a(f'<g filter="url(#sh)"><rect x="{x0 - 30}" y="{y0 + 30}" width="190" height="110" rx="8" fill="#0d47a1"/>'
          f'<rect x="{x0 - 30}" y="{y0 + 10}" width="190" height="110" rx="8" fill="#1565c0"/>'
          f'<circle cx="{x0 + 110}" cy="{y0 + 65}" r="22" fill="#fff"/><rect x="{x0 + 90}" y="{y0 + 55}" width="70" height="20" rx="10" fill="#fff"/>'
          f'<text x="{x0 + 30}" y="{y0 + 110}" font-size="20" fill="#fff" text-anchor="middle" font-weight="800">{lab or "SERVO"}</text>'
          f'<rect x="{x0 - 22}" y="{y0}" width="56" height="18" fill="#111"/></g>')
        return (x0 - 40, y0 - 10, 210, 160)
    if t == "motor":
        a(f'<g filter="url(#sh)"><rect x="{x0}" y="{y0 + 15}" width="90" height="70" rx="10" fill="#9e9e9e"/>'
          f'<rect x="{x0}" y="{y0}" width="90" height="70" rx="10" fill="#cfd8dc" stroke="#888"/>'
          f'<ellipse cx="{x0 + 45}" cy="{y0 + 35}" rx="70" ry="22" fill="#ffca28" opacity=".85"/>'
          f'<circle cx="{x0 + 45}" cy="{y0 + 35}" r="9" fill="#555"/>'
          f'<text x="{x0 + 45}" y="{y0 + 105}" font-size="18" fill="#fff" text-anchor="middle" font-weight="700">{lab or "DC motor"}</text></g>')
        return (x0 - 40, y0 - 15, 170, 135)
    if t == "battery9v":
        a(f'<g filter="url(#sh)"><rect x="{x0}" y="{y0 + 15}" width="80" height="150" rx="8" fill="#333"/>'
          f'<rect x="{x0}" y="{y0}" width="80" height="150" rx="8" fill="#555"/>'
          f'<text x="{x0 + 40}" y="{y0 + 85}" font-size="28" fill="#fff" font-weight="800" text-anchor="middle">9V</text></g>')
        return (x0 - 15, y0 - 15, 110, 190)
    terms = p.get("terms", ["VCC", "OUT", "GND"])
    w = 48 + 44 * len(terms)
    a(f'<g filter="url(#sh)"><rect x="{x0}" y="{y0 + 14}" width="{w}" height="80" rx="8" fill="#1b5e20"/>'
      f'<rect x="{x0}" y="{y0}" width="{w}" height="80" rx="8" fill="#2e7d32"/>'
      f'<text x="{x0 + w / 2}" y="{y0 + 60}" font-size="16" fill="#fff" text-anchor="middle" font-weight="800">{lab or _e(p["id"])}</text></g>')
    for i, n in enumerate(terms):
        a(f'<text x="{x0 + 24 + 44 * i}" y="{y0 + 30}" font-size="12" fill="#fff" text-anchor="middle">{_e(n)}</text>')
    return (x0 - 10, y0 - 15, w + 20, 120)


def _board(o, sc):
    a = o.append
    bd = sc.bd
    w = bd["w"]
    cxm = 675 if w > 900 else 440
    a(f'<g filter="url(#sh)"><rect x="220" y="868" width="{w}" height="360" rx="16" fill="#0f4566"/>'
      f'<rect x="220" y="850" width="{w}" height="360" rx="16" fill="url(#pcb)"/>'
      '<rect x="170" y="900" width="110" height="90" rx="4" fill="#b0b5b9" stroke="#777"/>'
      '<text x="225" y="952" font-size="18" fill="#333" text-anchor="middle" font-weight="700">USB</text>'
      '<rect x="180" y="1090" width="90" height="70" rx="4" fill="#111"/>'
      f'<rect x="{cxm - 55}" y="1000" width="110" height="110" rx="4" fill="#111"/><text x="{cxm}" y="1060" font-size="12" fill="#aaa" text-anchor="middle">{_e(bd["chip"])}</text>'
      f'<text x="{220 + w * 0.65}" y="1110" font-size="30" fill="#fff" font-weight="800" text-anchor="middle">{_e(bd["name"])}</text></g>')
    if bd.get("side"):
        a('<rect x="1180" y="900" width="60" height="250" fill="#111"/>')
    for grp in bd["top"]:
        xs = [sc.pin[l][0] for l in grp]
        a(f'<rect x="{xs[0] - 14}" y="866" width="{xs[-1] - xs[0] + 28}" height="28" fill="#111"/>')
        for l in grp:
            x, y = sc.pin[l]
            a(f'<rect x="{x - 4}" y="{y - 4}" width="8" height="8" fill="#888"/>'
              f'<text class="pl" data-pin="{_e(l)}" x="{x + 6}" y="{y + 22}" font-size="15" fill="#ddd" text-anchor="end" transform="rotate(-90 {x + 6} {y + 22})">{_e(l)}</text>')
    tops = {l for g in bd["top"] for l in g}
    for grp in bd["bot"]:
        xs = [sc.bpin[l][0] for l in grp]
        a(f'<rect x="{xs[0] - 14}" y="1174" width="{xs[-1] - xs[0] + 28}" height="28" fill="#111"/>')
        for l in grp:
            x, y = sc.bpin[l]
            a(f'<rect x="{x - 4}" y="{y - 4}" width="8" height="8" fill="#888"/>'
              f'<text class="pl" data-pin="{_e(l if l not in tops else "b:" + l)}" x="{x + 6}" y="{y - 14}" font-size="15" fill="#ddd" transform="rotate(-90 {x + 6} {y - 14})">{_e(l.replace("GND2", "GND"))}</text>')


def _wire_path(x1, y1, x2, y2, both_bb):
    dist = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** .5
    lift = min(30 + dist * 0.25, 140)
    if both_bb:
        if abs(y2 - y1) < 5:
            return f"M{x1},{y1} C{x1},{y1 - lift} {x2},{y2 - lift} {x2},{y2}"
        return f"M{x1},{y1} C{x1 + 12},{(y1 + y2) / 2 - lift * 0.5} {x2 + 12},{(y1 + y2) / 2 - lift * 0.5} {x2},{y2}"
    return f"M{x1},{y1} C{x1},{y1 - lift} {x2},{y2 + lift * 0.6} {x2},{y2}"


def build_html(title: str, board: str, parts: list, steps: list) -> str:
    parts = [dict(p) for p in parts]
    sc = Scene(board, parts)
    o = []
    a = o.append
    a(DEFS)
    has_power = any(p.get("type") == "power" for p in parts)
    _breadboard(o, has_power)
    place_step = {}
    for i, s in enumerate(steps):
        for pid in s.get("place", []):
            place_step.setdefault(pid, i)
    boxes = []

    def grp(pid, fn, *args):
        st = place_step.get(pid, -1)
        o.append(f'<g class="part" data-step="{st}">')
        box = fn(*args)
        o.append('</g>')
        if st >= 0 and box:
            x, y, w, h = box
            boxes.append(f'<rect class="hl" data-step="{st}" x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="none" stroke="#ffeb3b" stroke-width="5" stroke-dasharray="12 8"/>')

    for p in parts:
        if p.get("type") == "power":
            grp(p["id"], lambda: (_power(o), (322, 146, 122, 510))[1])
    for p in parts:
        if p.get("type") == "dip":
            grp(p["id"], _dip, o, p)
        elif p.get("type") in ("resistor", "led"):
            grp(p["id"], _inboard, o, sc, p)
    _board(o, sc)
    for p in parts:
        if "_xy" in p:
            grp(p["id"], _offboard, o, p)
    o.extend(boxes)
    # wires
    tags = []
    used_pins = {}
    ci = 0
    for i, s in enumerate(steps):
        for w in s.get("wires", []):
            f, t = w[0], w[1]
            col = w[2] if len(w) > 2 else ""
            fx, fy, fl = sc.end(f)
            tx, ty, tl = sc.end(t, other_x=fx)
            if not col:
                txt = f"{f} {t}".upper()
                col = "black" if re.search(r"GND|-TOP|-BOT|\.-|BROWN", txt) else "red" if re.search(r"5V|3\.3V|\+TOP|\+BOT|VIN|V\+", txt) else CYCLE[ci % len(CYCLE)]
                if col not in ("black", "red"):
                    ci += 1
            col = COLORS.get(col.lower(), col)
            for e_ in (f, t):
                m = re.fullmatch(r"(?:pin|board):(.+)", str(e_).strip(), re.I)
                if m:
                    used_pins.setdefault(m.group(1).strip().upper().lstrip("D") or m.group(1), i)
            d = _wire_path(fx, fy, tx, ty, fy < 700 and ty < 700 and fx < 1300 and tx < 1300)
            a(f'<g class="wire" data-step="{i}"><path d="{d}" fill="none" stroke="#000" stroke-opacity=".35" stroke-width="13" transform="translate(0,9)" stroke-linecap="round"/>'
              f'<path d="{d}" fill="none" stroke="{col}" stroke-width="9" stroke-linecap="round"/>'
              f'<path d="{d}" fill="none" stroke="#fff" stroke-opacity=".35" stroke-width="2" transform="translate(-1,-2)" stroke-linecap="round"/>'
              f'<circle cx="{fx}" cy="{fy}" r="8" fill="#ddd" stroke="{col}" stroke-width="3"/><circle cx="{tx}" cy="{ty}" r="8" fill="#ddd" stroke="{col}" stroke-width="3"/></g>')
            # no text tags on the scene: they hide the hole (user feedback). Colour is enough.
    placed = []
    for i, x, y, lab, col in tags:
        tw = len(lab) * 9 + 12
        ty = y - 30 if y > 410 else y + 32
        for (pi, px, py) in placed:
            if pi == i and abs(px - x) < tw and abs(py - ty) < 24:
                ty += 26 if ty > y else -26
        placed.append((i, x, ty))
        a(f'<g class="tag" data-step="{i}"><rect x="{x - tw / 2}" y="{ty - 13}" width="{tw}" height="24" rx="12" fill="#fff" fill-opacity=".92" stroke="{col}" stroke-width="3"/>'
          f'<text x="{x}" y="{ty + 5}" font-size="16" font-weight="800" fill="#222" text-anchor="middle">{_e(lab)}</text></g>')
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="60 100 1700 1150" '
           'font-family="Inter,Segoe UI,Arial,sans-serif">' + "\n".join(o) + '</svg>')
    data = json.dumps([{"title": s.get("title", f"Step {i + 1}"), "notes": s.get("notes", [])} for i, s in enumerate(steps)])
    pins = json.dumps(used_pins)
    return PAGE.replace("%TITLE%", _e(title)).replace("%SVG%", svg).replace("%DATA%", data).replace("%PINS%", pins)


PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>%TITLE% wiring</title><style>
html,body{margin:0;height:100%;background:radial-gradient(ellipse at 50% 30%,#4a5788,#2b3358);color:#fff;
font-family:Inter,Segoe UI,Arial,sans-serif;overflow:hidden}
.hd{position:absolute;left:24px;right:24px;top:16px;z-index:2;pointer-events:none}
.k{display:inline-block;background:#ffeb3b;color:#222;font-weight:800;padding:4px 14px;border-radius:16px;font-size:18px}
h1{font-size:30px;margin:8px 0 4px} ul{margin:0;padding-left:22px;font-size:19px;line-height:1.4;color:#e3e8ff}
.sc{position:absolute;left:0;right:0;top:150px;bottom:64px;display:flex;align-items:center;justify-content:center}
#v{perspective:2400px;width:min(100vw,calc((100vh - 214px)*1.48))}
.sc svg{width:100%;height:auto;transform-origin:50% 55%;transition:transform .3s}
.bar{position:absolute;left:0;right:0;bottom:14px;display:flex;gap:10px;justify-content:center;align-items:center;z-index:3}
button{font:700 17px Inter,Segoe UI,Arial;background:#fff;color:#222;border:0;border-radius:20px;padding:9px 20px;cursor:pointer}
button.on{background:#ffeb3b} .dots span{display:inline-block;width:12px;height:12px;border-radius:6px;background:#ffffff55;margin:0 3px;cursor:pointer}
.dots span.on{background:#ffeb3b} label{font-size:14px;color:#cfd6ff}
.wire,.tag,.part,.hl{transition:opacity .25s}
</style></head><body>
<div class="hd"><span class="k" id="k"></span><h1 id="t"></h1><ul id="n"></ul></div>
<div class="sc"><div id="v">%SVG%</div></div>
<div class="bar"><button id="p">&larr; Back</button><span class="dots" id="d"></span><button id="x">Next &rarr;</button>
<button id="all">Show all</button><label>tilt <input id="tilt" type="range" min="0" max="55" value="30"></label></div>
<script>
const S=%DATA%, PINS=%PINS%, N=S.length; let cur=0, all=false;
const q=s=>[...document.querySelectorAll(s)];
const d=document.getElementById('d'); S.forEach((_,i)=>{const e=document.createElement('span');e.onclick=()=>go(i);d.appendChild(e)});
function go(i){cur=Math.max(0,Math.min(N-1,i));all=false;draw()}
function draw(){
 const c=all?N-1:cur;
 document.getElementById('k').textContent=all?'Full picture':`Step ${c+1} of ${N}`;
 document.getElementById('t').textContent=all?'Everything together':S[c].title;
 document.getElementById('n').innerHTML=all?'':S[c].notes.map(x=>'<li>'+x.replace(/</g,'&lt;')+'</li>').join('');
 q('.wire').forEach(e=>{const s=+e.dataset.step;e.style.display=s>c?'none':'';e.style.opacity=(all||s==c)?1:.3});
 q('.tag').forEach(e=>{e.style.display=(!all&&+e.dataset.step==c)?'':'none'});
 q('.part').forEach(e=>{const s=+e.dataset.step;e.style.opacity=s>c?.15:1});
 q('.hl').forEach(e=>{e.style.display=(!all&&+e.dataset.step==c)?'':'none'});
 q('.pl').forEach(e=>{const s=PINS[e.dataset.pin];const on=s!==undefined&&s<=c;e.setAttribute('fill',on?'#ffeb3b':'#ddd');e.setAttribute('font-weight',on?800:400)});
 q('#d span').forEach((e,i)=>e.className=(!all&&i==c)?'on':'');
 document.getElementById('all').className=all?'on':'';
}
document.getElementById('p').onclick=()=>go(cur-1);
document.getElementById('x').onclick=()=>go(cur+1);
document.getElementById('all').onclick=()=>{all=!all;draw()};
const tilt=document.getElementById('tilt'), svg=document.querySelector('#v svg');
function t(){svg.style.transform=`rotateX(${tilt.value}deg)`} tilt.oninput=t; t();
document.onkeydown=e=>{if(e.key=='ArrowRight'||e.key==' ')go(cur+1);if(e.key=='ArrowLeft')go(cur-1);if(e.key=='a')document.getElementById('all').click()};
draw();
</script></body></html>'''
