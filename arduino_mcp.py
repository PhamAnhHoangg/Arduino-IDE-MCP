"""
Arduino MCP server
------------------
Lets Claude (Claude Code, Claude Desktop, VS Code) write, compile, upload,
test and debug Arduino sketches on a real board, by wrapping the official
`arduino-cli` tool.

Run it on the computer the Arduino is plugged into (it needs the USB port).

Settings (all optional, set as environment variables in the MCP config):
  ARDUINO_CLI    full path to arduino-cli(.exe). If not set, the server looks
                 on PATH and inside a normal Arduino IDE 2 install.
  ARDUINO_FQBN   default board. Defaults to arduino:samd:nano_33_iot
                 (the Nano 33 IoT from the Engineering Kit R2).
"""

import functools
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _ensure_packages():
    """First run as a plugin: install the two Python packages if missing.
    pip output goes to stderr, because stdout is the MCP channel."""
    try:
        import mcp.server.fastmcp  # noqa: F401
        import serial  # noqa: F401
        return
    except ImportError:
        pass
    req = Path(__file__).with_name("requirements.txt")
    print("arduino-mcp: installing Python packages (first run only)...", file=sys.stderr)
    for extra in ([], ["--user"]):
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", *extra, "-r", str(req)],
                           stdout=sys.stderr, stderr=sys.stderr)
        if r.returncode == 0:
            import importlib, site
            importlib.invalidate_caches()
            if extra:
                sys.path.append(site.getusersitepackages())
            return
    print(f"arduino-mcp: could not install packages. Run this yourself:\n"
          f'  "{sys.executable}" -m pip install -r "{req}"', file=sys.stderr)
    sys.exit(1)


_ensure_packages()

from mcp.server.fastmcp import FastMCP  # noqa: E402

DEFAULT_FQBN = os.environ.get("ARDUINO_FQBN", "arduino:samd:nano_33_iot")
WORK = Path(tempfile.gettempdir()) / "arduino_mcp"
SCRATCH = Path.home() / "Documents" / "arduino-mcp-sketches"

mcp = FastMCP(
    "arduino",
    instructions=("check_sketch until it compiles, then test_on_board with expect=[...]; "
                  "on FAIL fix and retry. Use library_api instead of guessing library functions."),
)


# ================================================================ helpers

_CLI = None


def _find_cli() -> str:
    """Find arduino-cli: env var, PATH, standard installs, Arduino IDE 2."""
    global _CLI
    if _CLI:
        return _CLI
    env = os.environ.get("ARDUINO_CLI")
    if env and Path(env).exists():
        _CLI = env
        return env
    found = shutil.which("arduino-cli")
    candidates = [Path(found)] if found else []
    pf = [os.environ.get("ProgramFiles") or "C:/Program Files",
          os.environ.get("ProgramFiles(x86)") or "C:/Program Files (x86)"]
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
    for base in pf:
        candidates.append(Path(base) / "Arduino CLI/arduino-cli.exe")  # winget / installer
    candidates.append(Path(local) / "Microsoft/WinGet/Links/arduino-cli.exe")
    candidates += [Path(x) for x in glob.glob(str(Path(local) / "Microsoft/WinGet/Packages/ArduinoSA.CLI*/arduino-cli.exe"))]
    for base in pf + [local, str(Path(local) / "Programs")]:
        for ide in ("Arduino IDE", "arduino-ide"):
            candidates.append(Path(base) / ide / "resources/app/lib/backend/resources/arduino-cli.exe")
    candidates += [Path("/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli"),
                   Path("/opt/homebrew/bin/arduino-cli"), Path("/usr/local/bin/arduino-cli"),
                   Path.home() / "bin/arduino-cli", Path.home() / ".local/bin/arduino-cli"]
    for c in candidates:
        if c.exists():
            _CLI = str(c)
            return _CLI
    raise RuntimeError(
        "arduino-cli was not found. Install it (see README) or set ARDUINO_CLI "
        "to the full path of arduino-cli.exe in your MCP config. "
        "If you installed it while Claude was open, restart Claude."
    )


# ---- transparency: every tool reply ends with a list of exactly what was done,
# and the same lines go to a log file you can open any time.
LOG_FILE = SCRATCH / "arduino-mcp.log"
_STEPS: list[str] = []


def _short(text: str) -> str:
    """Shorten absolute paths to their last part, to keep replies small."""
    return re.sub(r"(?:[A-Za-z]:)?(?:[\\/][^\s'\"\\/]+){3,}[\\/]([^\\/\s'\"]+)", r"\1", text)


def _step(text: str):
    line = f"[{time.strftime('%H:%M:%S')}] {text}"
    _STEPS.append(_short(text))
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def tool(fn):
    """Register an MCP tool whose reply ends with a 'What I did' step list."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        _STEPS.clear()
        with open(LOG_FILE, "a", encoding="utf-8") if LOG_FILE.parent.exists() else open(os.devnull, "w") as f:
            f.write(f"\n[{time.strftime('%H:%M:%S')}] TOOL {fn.__name__} "
                    f"{ {k: (f'<{len(v)} chars>' if k == 'code' and v else v) for k, v in kwargs.items()} }\n")
        t0 = time.time()
        try:
            out = fn(*args, **kwargs)
        except Exception as e:
            _step(f"ERROR {type(e).__name__}: {e}")
            out = f"ERROR: {e}"
        steps = "; ".join(_STEPS)
        return f"{out}\n[did: {steps}{'; ' if steps else ''}{time.time() - t0:.1f}s total]"
    return mcp.tool()(wrapper)


def _exec(args: list[str], timeout: int = 600) -> tuple[int, str]:
    cmd = [_find_cli(), *args]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        _step(f"RAN arduino-cli {' '.join(args)}  -> TIMED OUT after {timeout}s")
        return -1, f"Timed out after {timeout}s"
    _step(f"arduino-cli {' '.join(a for a in args if not a.startswith('--build-path') and a != '--warnings' and a != 'all')} -> {'ok' if p.returncode == 0 else 'exit ' + str(p.returncode)} {time.time() - t0:.1f}s")
    return p.returncode, ((p.stdout or "") + "\n" + (p.stderr or "")).strip()


def _run(args: list[str], timeout: int = 600) -> str:
    code, out = _exec(args, timeout)
    status = "OK" if code == 0 else f"FAILED (exit code {code})"
    return f"{status}: arduino-cli {' '.join(args)}\n\n{out}"


class Sketch:
    """A sketch ready to build: handles pasted code, and .ino files whose
    folder name doesn't match (arduino-cli needs Blink/Blink.ino)."""

    def __init__(self, sketch_path: str = "", code: str = ""):
        self.tmp = None
        if code:
            if sketch_path:
                p = Path(sketch_path).expanduser().resolve()
                if p.suffix != ".ino":
                    p = p / f"{p.name}.ino"
            else:
                p = SCRATCH / "mcp_sketch" / "mcp_sketch.ino"
            p.parent.mkdir(parents=True, exist_ok=True)
            old = p.read_text(encoding="utf-8", errors="replace") if p.exists() else None
            if old != code:
                p.write_text(code, encoding="utf-8")
                _step(f"wrote {len(code.splitlines())} lines to {p}")
            sketch_path = str(p)
        if not sketch_path:
            raise ValueError("Give sketch_path (a .ino file or folder) or code.")
        p = Path(sketch_path).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"Sketch not found: {p}")
        if p.is_dir():
            inos = sorted(p.glob("*.ino"))
            if not inos:
                raise ValueError(f"No .ino file in {p}")
            main = p / f"{p.name}.ino"
            if main.exists():
                p = main
            elif len(inos) == 1:
                p = inos[0]
            else:
                raise ValueError(f"{p} has several .ino files; pass the main one.")
        if p.suffix != ".ino":
            raise ValueError("sketch_path must be a .ino file or a sketch folder.")
        self.ino = p
        self.src_dir = p.parent
        if p.parent.name == p.stem:
            self.dir = p.parent
        else:
            self.tmp = Path(tempfile.mkdtemp(prefix="arduino_mcp_"))
            self.dir = self.tmp / p.stem
            self.dir.mkdir()
            for f in p.parent.iterdir():
                if f.is_file() and f.suffix in (".h", ".hpp", ".c", ".cpp", ".S"):
                    shutil.copy2(f, self.dir / f.name)
            shutil.copy2(p, self.dir / p.name)
        # persistent build folder per sketch = much faster re-compiles
        key = hashlib.md5(str(self.ino).encode()).hexdigest()[:10]
        self.build = WORK / "build" / f"{p.stem}_{key}"

    def fix_paths(self, text: str) -> str:
        """Show the user's real file path in messages, not the temp copy."""
        if self.tmp:
            text = text.replace(str(self.dir), str(self.src_dir))
        return text

    def cleanup(self):
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)


DIAG = re.compile(r"^(?P<file>.+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
                  r"(?P<kind>fatal error|error|warning|note):\s*(?P<msg>.*)$")

HINTS = [
    (r"No such file or directory", "Library missing: search_libraries, then install_library."),
    (r"was not declared in this scope", "Unknown name: typo, missing #include, or declared in another function."),
    (r"has no member named", "No such function: check library_api."),
    (r"no matching function for call", "Wrong arguments: check library_api."),
    (r"expected .;.", "Missing semicolon, usually at the end of the line before."),
    (r"expected .\}.|expected declaration", "Braces { } don't match up."),
    (r"redefinition of", "Same name defined twice (maybe two .ino files in the folder)."),
    (r"invalid conversion|cannot convert", "Wrong type passed (e.g. String vs char*, int vs pointer)."),
    (r"overflow|text.* will not fit|region .* overflowed", "Sketch is too big for the board's memory."),
    (r"unused variable", "Declared but never used: harmless, but maybe a bug."),
    (r"comparison of integer expressions of different signedness", "int vs unsigned comparison: usually harmless with millis()."),
]


def _hint(msg: str) -> str:
    for pat, h in HINTS:
        if re.search(pat, msg):
            return h
    return ""


def _parse_compile(out: str, sk: "Sketch") -> tuple[list[dict], list[dict], str]:
    errors, warnings = [], []
    seen = set()
    for line in out.splitlines():
        m = DIAG.match(line.strip())
        if not m or m["kind"] == "note":
            continue
        f = sk.fix_paths(m["file"])
        k = (f, m["line"], m["msg"])
        if k in seen:
            continue
        seen.add(k)
        item = {"file": f, "line": int(m["line"]), "msg": m["msg"]}
        # show the actual code line when it's in the user's sketch
        try:
            src = Path(f)
            if src.exists() and src.parent == sk.src_dir:
                item["code"] = src.read_text(encoding="utf-8", errors="replace").splitlines()[item["line"] - 1].strip()
        except Exception:
            pass
        item["hint"] = _hint(m["msg"])
        in_sketch = Path(f).parent == sk.src_dir
        if "error" in m["kind"]:
            errors.append(item)
        elif in_sketch:  # skip warnings from inside libraries/core
            warnings.append(item)
    usage = "\n".join(l for l in out.splitlines()
                      if l.startswith(("Sketch uses", "Global variables use")))
    return errors, warnings, usage


def _fmt(items: list[dict], label: str) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(f"{label} {i}: {Path(it['file']).name} line {it['line']}: {it['msg']}")
        if it.get("code"):
            lines.append(f"    code: {it['code']}")
        if it.get("hint"):
            lines.append(f"    hint: {it['hint']}")
    return "\n".join(lines)


_DETECT = {"t": 0.0, "boards": []}


def _detect_boards(max_age: float = 20.0) -> list[tuple[str, str, str]]:
    """Recognised boards plugged in now: [(port, fqbn, name)]. Cached briefly."""
    if time.time() - _DETECT["t"] < max_age:
        return _DETECT["boards"]
    _, out = _exec(["board", "list", "--format", "json"], 60)
    found = []
    try:
        data = json.loads(out[out.index("{") if out.lstrip().startswith("{") else out.index("["):])
        ports = data.get("detected_ports", []) if isinstance(data, dict) else data
        for item in ports or []:
            port = item.get("port", {})
            for b in item.get("matching_boards") or []:
                if port.get("protocol") == "serial" and b.get("fqbn"):
                    found.append((port.get("address"), b["fqbn"], b.get("name", "")))
                    break
    except Exception:
        pass
    _DETECT.update(t=time.time(), boards=found)
    return found


def _sketch_yaml(sketch_dir: Path) -> dict:
    yml = sketch_dir / "sketch.yaml"
    if not yml.exists():
        return {}
    txt = yml.read_text(encoding="utf-8", errors="replace")
    return {k: v.strip() for k, v in re.findall(r"^(default_fqbn|default_port):\s*(\S+)", txt, re.M)}


class NeedInput(Exception):
    """Auto-detect couldn't decide: Claude should ask the user one question."""


def _resolve(port: str, fqbn: str, sketch_dir: Path | None, need_port: bool) -> tuple[str, str]:
    """Fill in port/board automatically: given value > connected board >
    sketch.yaml > default. Raises NeedInput when it can't decide."""
    saved = _sketch_yaml(sketch_dir) if sketch_dir else {}
    if fqbn and (port or not need_port):
        return port, fqbn
    boards = _detect_boards() if (need_port and not port) or not fqbn else []
    if port and not fqbn:
        match = [b for b in boards if b[0] == port]
        return port, (match[0][1] if match else saved.get("default_fqbn", DEFAULT_FQBN))
    if need_port and not port:
        if fqbn:
            boards = [b for b in boards if b[1] == fqbn] or boards
        if len(boards) == 1:
            _step(f"auto-picked {boards[0][2]} on {boards[0][0]}")
            return boards[0][0], fqbn or boards[0][1]
        if not boards:
            raise NeedInput("NEED INPUT: no Arduino detected. Ask the user to plug it in with a "
                            "data USB cable (or double-tap reset), then retry.")
        raise NeedInput("NEED INPUT: several boards connected, ask the user which one: "
                        + "; ".join(f"{n} on {p} ({f})" for p, f, n in boards))
    # board only (no port needed): sketch.yaml, else a single connected board, else default
    if saved.get("default_fqbn"):
        return port, saved["default_fqbn"]
    fq = {b[1] for b in boards}
    return port, (fq.pop() if len(fq) == 1 else DEFAULT_FQBN)


def _port_hint(out: str, port: str) -> str:
    if re.search(r"cannot find the file|No such file or directory|FileNotFoundError", out):
        return f"PORT NOT FOUND: {port} doesn't exist now. Unplugged? Run list_connected_boards."
    if re.search(r"Access is denied|PermissionError|[Bb]usy|Resource busy", out):
        return (f"PORT BUSY: something else has {port} open. Ask the user to close the Arduino IDE "
                f"Serial Monitor/Plotter and any script (e.g. Python) using {port}, then retry.")
    if "No device found" in out:
        return (f"Board not found on {port} during upload. If another program has the port open, "
                f"close it; else double-tap the board's reset button and retry.")
    return "hint: check the port, close other programs using it, or double-tap reset."


def _compile(sk: "Sketch", fqbn: str) -> tuple[bool, str]:
    sk.build.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    cmd = ["compile", "--fqbn", fqbn, "--warnings", "all", "--build-path", str(sk.build), str(sk.dir)]
    code, out = _exec(cmd, 900)
    if code != 0 and re.search(r"[Pp]latform '?[\w-]+:[\w-]+'? (is )?not (found|installed)", out):
        core = ":".join(fqbn.split(":")[:2])
        _step(f"board package {core} missing, installing it")
        _update_index()
        if _exec(["core", "install", core], 900)[0] == 0:
            code, out = _exec(cmd, 900)
    secs = time.time() - t0
    errors, warnings, usage = _parse_compile(out, sk)
    ok = code == 0
    mem = " ".join(re.findall(r"\((\d+%)\)", usage))
    parts = [f"{'COMPILE OK' if ok else 'COMPILE FAILED'} {sk.ino.name} for {fqbn} in {secs:.0f}s"
             + (f" (flash/RAM {mem})" if mem else "")]
    if errors:
        parts.append(_fmt(errors[:8], "ERROR"))
    if warnings:
        parts.append(_fmt(warnings[:5], "WARNING"))
    missing = re.findall(r"([\w.-]+)\.h: No such file or directory", out)
    for h in sorted(set(missing))[:2]:
        _, names = _exec(["lib", "search", h, "--names"], 120)
        cands = re.findall(r'^Name: "([^"]+)"', names, re.M)[:5]
        exact = [c for c in cands if c.replace(" ", "").lower() == h.lower()]
        parts.append(f"MISSING LIBRARY for {h}.h: " + (f"install_library('{exact[0]}')" if exact
                     else ("NEED INPUT: ask the user which to install: " + ", ".join(cands)) if cands
                     else "no match in Library Manager; ask the user where this library comes from."))
    if not ok and not errors:  # a failure we couldn't parse: show raw tail
        parts.append(sk.fix_paths(out[-1200:]))
    return ok, "\n".join(parts)


# ---- static checks (no board, no compiler): things the compiler won't catch

def _static_checks(src: str, fqbn: str) -> list[str]:
    notes = []
    code = re.sub(r"//.*", "", src)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    begins = re.findall(r"Serial\.begin\s*\(\s*(\d+)", code)
    if re.search(r"Serial\.(print|println|write|read|available|parseInt)", code) and not begins:
        notes.append("Serial is used but Serial.begin(...) is never called in setup().")
    if begins and begins[0] != "115200":
        notes.append(f"Serial baud is {begins[0]}: pass baud={begins[0]} to test_on_board/serial.")
    if re.search(r"while\s*\(\s*!\s*Serial\s*\)", code):
        notes.append("while(!Serial) waits for the computer: fine on USB, but the sketch freezes "
                     "on battery power with no USB connected.")
    if "ArduinoMotorCarrier" in code:
        if "controller.begin" not in code:
            notes.append("ArduinoMotorCarrier: controller.begin() is never called, so motors/servos won't work.")
        loop = re.search(r"void\s+loop\s*\([^)]*\)\s*\{(.*)", code, re.S)
        if loop and "controller.ping" not in loop.group(1):
            notes.append("ArduinoMotorCarrier: controller.ping() is not called in loop(). The carrier firmware "
                         "resets if it isn't pinged at least every ~3 s (per the library examples).")
    if re.search(r"\bdelay\s*\(\s*\d{4,}\s*\)", code):
        notes.append("A delay of 1 second or more blocks everything (Serial reading, motors) while it waits.")
    for m in re.finditer(r"analogWrite\s*\(\s*\w+\s*,\s*(\d+)", code):
        if int(m.group(1)) > 255:
            notes.append(f"analogWrite value {m.group(1)} is above 255 (default 8-bit range).")
    if "setup" not in code or not re.search(r"void\s+loop\s*\(", code):
        notes.append("Sketch needs both void setup() and void loop().")
    return notes


def _find_port(port: str, wait: float = 6.0) -> str:
    """After an upload the Nano 33 IoT resets and its USB port disappears
    for a moment (and on Windows can come back as a different COM number).
    Wait for it to come back."""
    from serial.tools import list_ports
    end = time.time() + wait
    while time.time() < end:
        names = [p.device for p in list_ports.comports()]
        if port in names:
            return port
        arduinos = [p.device for p in list_ports.comports() if (p.vid or 0) == 0x2341]
        if len(arduinos) == 1:
            return arduinos[0]
        time.sleep(0.3)
    return port


def _open_serial(port: str, baud: int, retries: float = 0):
    import serial  # pyserial
    end = time.time() + retries
    while True:
        try:
            s = serial.Serial(port, baud, timeout=0.1)
            time.sleep(0.2)
            return s
        except Exception:
            if time.time() >= end:
                raise
            time.sleep(0.3)


def _read_until(s, seconds: float, expect: list[str], fail_on: list[str], max_chars: int = 12000) -> str:
    """Read Serial for up to `seconds`; stop early once every expected string
    has appeared, or as soon as a fail_on string appears."""
    end = time.time() + seconds
    buf = b""
    while time.time() < end and len(buf) < max_chars:
        buf += s.read(512)
        text = buf.decode("utf-8", errors="replace")
        if fail_on and any(f in text for f in fail_on):
            time.sleep(0.3)
            buf += s.read(2048)
            break
        if expect and all(e in text for e in expect):
            break
    return buf.decode("utf-8", errors="replace")


_OPENED: set[str] = set()


def _attach(sketch_dir: Path, fqbn: str = "", port: str = "") -> None:
    """Save board (and port) in the sketch's sketch.yaml via `arduino-cli board
    attach`, so Arduino IDE and arduino-cli know the board without the user
    picking it. Only writes when something is missing or changed."""
    yml = sketch_dir / "sketch.yaml"
    cur = yml.read_text(encoding="utf-8", errors="replace") if yml.exists() else ""
    args = []
    if fqbn and f"default_fqbn: {fqbn}" not in cur:
        args += ["-b", fqbn]
    if port and f"default_port: {port}" not in cur:
        args += ["-p", port]
    if args:
        _exec(["board", "attach", *args, str(sketch_dir)], 60)


def _open_in_ide(ino: Path, force: bool = False, fqbn: str = "") -> str:
    """Show the sketch in the Arduino IDE, once per sketch (later changes are
    written to the same file, and the IDE reloads it). Returns a status line."""
    if ino.parent.name != ino.stem:
        msg = (f"Not opened in Arduino IDE: the IDE would ask to MOVE {ino.name} into a new "
               f"folder named '{ino.stem}'. Open it yourself if you want that, or use a "
               f"sketch whose folder name matches the .ino name.")
        _step("SKIPPED opening in Arduino IDE (folder name doesn't match .ino name)")
        return msg
    if str(ino) in _OPENED and not force:
        return ""
    try:
        _attach(ino.parent, fqbn or DEFAULT_FQBN)  # so the IDE opens with the board selected
    except Exception:
        pass
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(ino))  # opens with the app .ino files are linked to (Arduino IDE)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", "Arduino IDE", str(ino)])
        else:
            subprocess.Popen(["xdg-open", str(ino)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        _OPENED.add(str(ino))  # don't retry on every call
        _step(f"FAILED to open in Arduino IDE: {e}")
        return f"Could not open Arduino IDE automatically ({e}). Open this file yourself: {ino}"
    _OPENED.add(str(ino))
    _step(f"opened {ino} in Arduino IDE")
    return f"Opened in Arduino IDE: {ino}"


# ================================================================ tools
# Kept few and with short descriptions: every tool description costs tokens.

@tool
def check_setup() -> str:
    """arduino-cli path/version and installed cores. Run first if anything fails."""
    try:
        cli = _find_cli()
    except RuntimeError as e:
        return str(e)
    _, ver = _exec(["version"], 30)
    _, cores = _exec(["core", "list"], 60)
    return f"{cli}\n{ver}\nDefault board: {DEFAULT_FQBN}\n{cores}"


@tool
def list_connected_boards() -> str:
    """Plugged-in boards: port (COM5...) and FQBN."""
    code, out = _exec(["board", "list", "--format", "json"], 60)
    lines = []
    try:
        data = json.loads(out[out.index("{") if out.lstrip().startswith("{") else out.index("["):])
        ports = data.get("detected_ports", []) if isinstance(data, dict) else data
        for item in ports or []:
            port = item.get("port", {})
            if port.get("protocol") != "serial":
                continue
            boards = item.get("matching_boards") or []
            name = ", ".join(f"{b.get('name')} ({b.get('fqbn')})" for b in boards) or "unknown board"
            lines.append(f"{port.get('address')}  {name}")
    except Exception:
        pass
    if not lines:  # fall back to every serial port
        from serial.tools import list_ports
        lines = [f"{p.device}  {p.description}" for p in list_ports.comports()]
    return "\n".join(lines) or "No ports. Is it plugged in with a data USB cable?"


@tool
def search_boards(query: str) -> str:
    """Find a board's FQBN by name, e.g. 'mega'."""
    _, out = _exec(["board", "listall", query], 60)
    return out[:2000]


def _update_index():
    """Refresh board/library lists at most once a day (it's slow)."""
    stamp = WORK / "index-updated"
    if stamp.exists() and time.time() - stamp.stat().st_mtime < 86400:
        _step("skipped index update (done in last 24 h)")
        return
    _exec(["core", "update-index"], 300)
    _exec(["lib", "update-index"], 300)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()


@tool
def install_core(core: str = "arduino:samd") -> str:
    """Install board support. arduino:samd = Nano 33 IoT/MKR, arduino:avr = Uno/Mega."""
    _, out = _exec(["core", "list"], 60)
    if re.search(rf"^{re.escape(core)}\s", out, re.M):
        return f"{core} already installed."
    _update_index()
    c, out = _exec(["core", "install", core], 900)
    return f"{core} installed." if c == 0 else "FAILED:\n" + out[-1500:]


@tool
def search_libraries(query: str) -> str:
    """Search Library Manager (names only)."""
    _, out = _exec(["lib", "search", query, "--names"], 120)
    return out[:1500]


@tool
def install_library(name: str) -> str:
    """Install a library by exact name, e.g. 'ArduinoMotorCarrier'."""
    _, out = _exec(["lib", "list"], 60)
    if re.search(rf"^{re.escape(name)}\s", out, re.M):
        return f"{name} already installed."
    c, out = _exec(["lib", "install", name], 600)
    return f"{name} installed." if c == 0 else "FAILED:\n" + out[-1500:]


@tool
def library_api(library: str = "", filter: str = "") -> str:
    """Real function names/params from an installed library's .h files.
    Use before calling unfamiliar library functions. filter narrows output
    (e.g. 'setDuty'). library='' lists installed libraries."""
    code, out = _exec(["lib", "list", "--format", "json"], 60)
    try:
        data = json.loads(out[out.index("{") if out.lstrip().startswith("{") else out.index("["):])
    except Exception:
        return "Could not read the library list:\n" + out[-2000:]
    libs = data.get("installed_libraries", []) if isinstance(data, dict) else data
    if not library:
        return "Installed: " + ", ".join(sorted(f"{e.get('library', e).get('name', '')} {e.get('library', e).get('version', '')}" for e in libs))
    want = library.lower().replace(" ", "")
    match = None
    for entry in libs:
        lib = entry.get("library", entry)
        if lib.get("name", "").lower().replace(" ", "") == want:
            match = lib
            break
    if not match:
        names = sorted(e.get("library", e).get("name", "") for e in libs)
        return f"'{library}' is not installed. Installed: {', '.join(names) or 'none'}"
    root = Path(match.get("install_dir") or match.get("source_dir", ""))
    src = root / "src" if (root / "src").exists() else root
    lines = [f"{match.get('name')} {match.get('version', '')} at {root}"]
    decl = re.compile(r"^\s*(?!//|\*|/\*|#|return\b)[\w:<>,\s\*&~]+\(.*\)\s*(const)?\s*(override)?\s*(=\s*0)?\s*[;{]?\s*$")
    for h in sorted(src.rglob("*.h"))[:40]:
        found, cls = [], ""
        for ln in h.read_text(encoding="utf-8", errors="replace").splitlines():
            s = ln.strip()
            m = re.match(r"^(class|struct)\s+(\w+)", s)
            if m and not s.endswith(";"):
                cls = m.group(2)
            keep = m or re.match(r"^extern\s+\w+\s+\w+", s) or decl.match(ln)
            if keep and (not filter or filter.lower() in s.lower()):
                where = f"[{cls}] " if cls and not m else ""
                found.append("  " + where + s.rstrip("{").strip())
        if found:
            lines.append(f"\n{h.relative_to(root)}:")
            lines.extend(found[:60])
    text = "\n".join(lines)
    return text[:5000] + ("\n...(cut, use filter)" if len(text) > 5000 else "")


@tool
def check_sketch(sketch_path: str = "", code: str = "", fqbn: str = "",
                 show_in_ide: bool = True) -> str:
    """Compile + fact-check, no board needed. Errors come with line, code and
    hint. Pass sketch_path (.ino/folder) or code (full sketch; saved to
    sketch_path, else Documents/arduino-mcp-sketches/mcp_sketch). Opens it in
    Arduino IDE the first time so the user sees the code."""
    sk = Sketch(sketch_path, code)
    try:
        _, fqbn = _resolve("", fqbn, sk.dir if not sk.tmp else None, need_port=False)
        ide = _open_in_ide(sk.ino, fqbn=fqbn) if show_in_ide else ""
        ok, report = _compile(sk, fqbn)
        notes = _static_checks(sk.ino.read_text(encoding="utf-8", errors="replace"), fqbn)
        if notes:
            report += "\nLIKELY PROBLEMS:\n" + "\n".join(f"- {n}" for n in notes)
        return (ide + "\n" if ide else "") + report
    finally:
        sk.cleanup()


def _dedupe(log: str, limit: int = 1500) -> str:
    """Collapse repeated lines ("READY x12") and keep the end, to save tokens."""
    out, prev, n = [], None, 0
    for line in log.strip().splitlines() + [None]:
        if line == prev:
            n += 1
            continue
        if prev is not None:
            out.append(prev + (f"  (x{n})" if n > 1 else ""))
        prev, n = line, 1
    text = "\n".join(out)
    return ("...\n" + text[-limit:]) if len(text) > limit else text


@tool
def test_on_board(port: str = "", sketch_path: str = "", code: str = "", fqbn: str = "",
                  expect: list[str] | None = None, fail_on: list[str] | None = None,
                  send: list[str] | None = None, baud: int = 115200,
                  seconds: float = 8, upload: bool = True, show_in_ide: bool = True) -> str:
    """One call: write code -> compile -> upload -> Serial (send lines) ->
    PASS/FAIL. expect: text that must appear; fail_on: text meaning failure;
    send: lines to send; stops early once all expect seen. upload=False tests
    what's on the board. port/fqbn: leave empty to auto-detect. Sketch should
    print markers (e.g. READY) repeatedly."""
    expect, fail_on, send = expect or [], fail_on or [], send or []
    head = []
    sk = Sketch(sketch_path, code) if upload else None
    try:
        port, board = _resolve(port, fqbn, sk.dir if sk and not sk.tmp else None, need_port=True)
    except NeedInput as e:
        if sk:
            sk.cleanup()
        return str(e)
    if upload:
        try:
            if show_in_ide:
                ide = _open_in_ide(sk.ino, fqbn=board)
                if ide:
                    head.append(ide)
            ok, rep = _compile(sk, board)
            if not ok:
                return "RESULT: FAIL at compile\n" + rep
            c, out = _exec(["upload", "-p", port, "--fqbn", board,
                            "--input-dir", str(sk.build), str(sk.dir)], 300)
            if c != 0:
                return "RESULT: FAIL at upload\n" + _port_hint(out, port) + "\n" + out[-400:]
            head.append(rep.splitlines()[0])
            if not sk.tmp:
                _attach(sk.dir, board, port)  # IDE's Upload button then uses this port
        finally:
            sk.cleanup()
        time.sleep(1.0)
        new_port = _find_port(port)
        if new_port != port:
            head.append(f"Board came back on {new_port} (was {port}).")
            _step(f"port changed {port} -> {new_port}")
            port = new_port
    try:
        s = _open_serial(port, baud, retries=8 if upload else 0)
    except Exception as e:
        return f"RESULT: FAIL at serial\n{_port_hint(str(e), port)}"
    _step(f"serial {port} @ {baud} opened")
    t0 = time.time()
    with s:
        for line in send:
            s.write((line + "\n").encode())
            s.flush()
            _step(f"sent {line!r}")
            time.sleep(0.3)
        log = _read_until(s, min(max(seconds, 0.5), 120), expect, fail_on)
    _step(f"read {len(log)} chars in {time.time() - t0:.1f}s, serial closed")
    missing = [e for e in expect if e not in log]
    bad = [f for f in fail_on if f in log]
    if bad:
        verdict = f"FAIL: saw {bad}"
    elif missing:
        verdict = f"FAIL: never saw {missing}"
    elif not log.strip():
        verdict = "FAIL: board printed nothing" if expect else "UNKNOWN: board printed nothing"
    else:
        verdict = "PASS" if expect else "RAN (no expect given)"
    head.insert(0, f"RESULT: {verdict}")
    return "\n".join(head) + "\nSERIAL:\n" + (_dedupe(log) or "(nothing)")


@tool
def serial(port: str = "", text: str = "", baud: int = 115200, seconds: float = 3) -> str:
    """Send a line (optional) and read Serial for a few seconds. port: auto if empty."""
    try:
        port, _ = _resolve(port, "x", None, need_port=True)
    except NeedInput as e:
        return str(e)
    try:
        with _open_serial(port, baud) as s:
            _step(f"serial {port} @ {baud} opened")
            if text:
                s.write((text + "\n").encode())
                s.flush()
                _step(f"sent {text!r}")
            out = _read_until(s, min(max(seconds, 0.5), 60), [], [])
            _step(f"read {len(out)} chars, serial closed")
            return _dedupe(out) or "(nothing received)"
    except Exception as e:
        return _port_hint(str(e), port)


@tool
def open_in_ide(sketch_path: str = "", code: str = "", fqbn: str = "") -> str:
    """Show a sketch in the Arduino IDE window so the user can watch. Saves the
    board in sketch.yaml first so the IDE has it selected (if the sketch was
    already open, the user must close and reopen that window)."""
    sk = Sketch(sketch_path, code)
    try:
        return _open_in_ide(sk.ino, force=True, fqbn=fqbn)
    finally:
        sk.cleanup()


def _press_in_ide(keys: str, title_hint: str) -> str:
    """Windows only: bring the Arduino IDE window to the front and press a
    shortcut (Ctrl+R = Verify, Ctrl+U = Upload), as a person would.
    Refuses to type if it can't confirm the IDE window is in front."""
    if not sys.platform.startswith("win"):
        return "Pressing IDE buttons is only supported on Windows for now."
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            t = buf.value
            if "Arduino IDE" in t:
                found.append((hwnd, t))
        return True
    user32.EnumWindows(each, 0)
    if not found:
        return "No Arduino IDE window found. Open the sketch first (open_in_ide)."
    match = [w for w in found if title_hint and title_hint.lower() in w[1].lower()] or found
    hwnd, title = match[0]
    VK_MENU, VK_CONTROL, UP = 0x12, 0x11, 0x0002
    user32.ShowWindow(hwnd, 9)  # restore if minimised
    user32.keybd_event(VK_MENU, 0, 0, 0)  # Windows only lets us focus a window after a key event
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(VK_MENU, 0, UP, 0)
    time.sleep(0.4)
    if user32.GetForegroundWindow() != hwnd:
        _step(f"REFUSED to press keys: could not bring '{title}' to the front")
        return f"Could not bring '{title}' to the front, so I didn't press anything. Click the IDE window and try again."
    key = ord(keys.upper())
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(key, 0, 0, 0)
    user32.keybd_event(key, 0, UP, 0)
    user32.keybd_event(VK_CONTROL, 0, UP, 0)
    _step(f"pressed Ctrl+{keys.upper()} in '{title}'")
    return f"Pressed Ctrl+{keys.upper()} in '{title}'."


@tool
def ide_button(action: str = "verify", sketch_name: str = "") -> str:
    """Windows: press Verify (Ctrl+R) or Upload (Ctrl+U) in the open Arduino
    IDE so the user sees it run there. Uses the IDE's selected board/port.
    Can't read the IDE result: confirm with check_sketch or serial."""
    a = action.lower().strip()
    if a not in ("verify", "upload"):
        return "action must be 'verify' or 'upload'."
    return _press_in_ide("r" if a == "verify" else "u", sketch_name) + (
        "\nIf the IDE says 'No board selected': close that IDE window and run open_in_ide "
        "again (board is saved in sketch.yaml), or pick the board once in the IDE's board dropdown.")


if __name__ == "__main__":
    print("Arduino MCP server running (stdio). Claude starts this for you; "
          "you don't need to keep this window open.", file=sys.stderr)
    mcp.run()
