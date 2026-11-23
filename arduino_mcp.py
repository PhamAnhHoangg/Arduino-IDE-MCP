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
    instructions="check_sketch until it compiles, then test_on_board; on FAIL fix and retry.",
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
    drives = [f"{d}:/" for d in "CDEFG"] + [f"{d}:/arduino" for d in "CDEFG"]
    for base in pf + [local, str(Path(local) / "Programs")] + drives:
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
        _attach(ino.parent, fqbn or DEFAULT_FQBN)  # for arduino-cli; Arduino IDE 2.3 ignores it
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

def check_setup() -> str:
    """arduino-cli path/version and installed cores. Run first if anything fails."""
    try:
        cli = _find_cli()
    except RuntimeError as e:
        return str(e)
    _, ver = _exec(["version"], 30)
    _, cores = _exec(["core", "list"], 60)
    return f"{cli}\n{ver}\nDefault board: {DEFAULT_FQBN}\n{cores}"


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


def install_core(core: str = "arduino:samd") -> str:
    """Install board support. arduino:samd = Nano 33 IoT/MKR, arduino:avr = Uno/Mega."""
    _, out = _exec(["core", "list"], 60)
    if re.search(rf"^{re.escape(core)}\s", out, re.M):
        return f"{core} already installed."
    _update_index()
    c, out = _exec(["core", "install", core], 900)
    return f"{core} installed." if c == 0 else "FAILED:\n" + out[-1500:]


def search_libraries(query: str) -> str:
    """Search Library Manager (names only)."""
    _, out = _exec(["lib", "search", query, "--names"], 120)
    return out[:1500]


def install_library(name: str) -> str:
    """Install a library by exact name, e.g. 'ArduinoMotorCarrier'."""
    _, out = _exec(["lib", "list"], 60)
    if re.search(rf"^{re.escape(name)}\s", out, re.M):
        return f"{name} already installed."
    c, out = _exec(["lib", "install", name], 600)
    return f"{name} installed." if c == 0 else "FAILED:\n" + out[-1500:]


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


def _check_sketch(sketch_path: str = "", code: str = "", fqbn: str = "",
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


def _test_on_board(port: str = "", sketch_path: str = "", code: str = "", fqbn: str = "",
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


def _serial(port: str = "", text: str = "", baud: int = 115200, seconds: float = 3) -> str:
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


def open_in_ide(sketch_path: str = "", code: str = "", fqbn: str = "") -> str:
    """Show a sketch in the Arduino IDE window so the user can watch. Saves the
    board in sketch.yaml first so the IDE has it selected (if the sketch was
    already open, the user must close and reopen that window)."""
    sk = Sketch(sketch_path, code)
    try:
        return _open_in_ide(sk.ino, force=True, fqbn=fqbn)
    finally:
        sk.cleanup()


def _win():
    import ctypes
    from ctypes import wintypes
    return ctypes, wintypes, ctypes.windll.user32


def _ide_windows() -> list[tuple[int, str]]:
    ctypes, wintypes, user32 = _win()
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if "Arduino IDE" in buf.value:
                found.append((hwnd, buf.value))
        return True
    user32.EnumWindows(each, 0)
    return found


def _find_ide(title_hint: str = "", wait: float = 0) -> tuple[int, str] | None:
    end = time.time() + wait
    while True:
        wins = _ide_windows()
        if title_hint:  # IDE 2 titles look like "face_fan | Arduino IDE 2.3.11"
            wins = [w for w in wins if w[1].lower().startswith(title_hint.lower() + " |")]
        if wins or time.time() >= end:
            return wins[0] if wins else None
        time.sleep(0.5)


def _focus(hwnd: int) -> bool:
    """Bring one window to the front. Returns False if Windows refused."""
    ctypes, wintypes, user32 = _win()
    if user32.GetForegroundWindow() == hwnd:
        return True
    user32.ShowWindow(hwnd, 9)  # restore if minimised
    user32.keybd_event(0x12, 0, 0, 0)  # Alt tap: Windows only allows focus change after input
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(0x12, 0, 0x0002, 0)
    time.sleep(0.4)
    return user32.GetForegroundWindow() == hwnd


def _chord(*vks: int):
    _, _, user32 = _win()
    for vk in vks:
        user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, 0x0002, 0)


def _set_clipboard(text: str) -> bool:
    ctypes, wintypes, user32 = _win()
    k32 = ctypes.windll.kernel32
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\0\0"
        h = k32.GlobalAlloc(0x0002, len(data))
        ctypes.memmove(k32.GlobalLock(h), data, len(data))
        k32.GlobalUnlock(h)
        return bool(user32.SetClipboardData(13, h))  # CF_UNICODETEXT
    finally:
        user32.CloseClipboard()


def _get_clipboard() -> str | None:
    ctypes, wintypes, user32 = _win()
    k32 = ctypes.windll.kernel32
    user32.GetClipboardData.restype = wintypes.HANDLE
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    if not user32.OpenClipboard(None):
        return None
    try:
        h = user32.GetClipboardData(13)
        if not h:
            return None
        ptr = k32.GlobalLock(h)
        try:
            return ctypes.wstring_at(ptr)
        finally:
            k32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def _click_editor(hwnd: int):
    """Click once inside the code editor area of THAT window (top-middle of
    the window, below the toolbar), so keystrokes go to the editor."""
    ctypes, wintypes, user32 = _win()
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    x = r.left + int((r.right - r.left) * 0.55)
    y = r.top + int((r.bottom - r.top) * 0.30)
    old = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(old))
    user32.SetCursorPos(x, y)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # left up
    time.sleep(0.15)
    user32.SetCursorPos(old.x, old.y)


def _norm(t: str) -> str:
    return "\n".join(l.rstrip() for l in t.replace("\r\n", "\n").strip().split("\n"))


def _press_in_ide(keys: str, title_hint: str) -> str:
    """Windows only: bring the Arduino IDE window to the front and press a
    shortcut (Ctrl+R = Verify, Ctrl+U = Upload), as a person would.
    Refuses to type if it can't confirm the IDE window is in front."""
    if not sys.platform.startswith("win"):
        return "Pressing IDE buttons is only supported on Windows for now."
    w = _find_ide(title_hint) or _find_ide()
    if not w:
        return "No Arduino IDE window found. Open the sketch first (open_in_ide)."
    hwnd, title = w
    if not _focus(hwnd):
        _step(f"REFUSED to press keys: could not bring '{title}' to the front")
        return f"Could not bring '{title}' to the front, so I didn't press anything. Click the IDE window and try again."
    _chord(0x11, ord(keys.upper()))
    _step(f"pressed Ctrl+{keys.upper()} in '{title}'")
    return f"Pressed Ctrl+{keys.upper()} in '{title}'."


def type_in_ide(code: str, sketch_path: str = "", lines_per_step: int = 1,
                delay: float = 0.05) -> str:
    """Windows: put `code` into the sketch's open Arduino IDE editor where the
    user can watch it appear line by line, then save (Ctrl+S) and check the
    saved file matches. Keys go ONLY to that IDE window; it stops if the user
    clicks away. sketch_path: the sketch (default: the mcp_sketch scratch)."""
    if not sys.platform.startswith("win"):
        return "type_in_ide only works on Windows. Use check_sketch(code=...) instead."
    if not sketch_path:
        scratch = SCRATCH / "mcp_sketch" / "mcp_sketch.ino"
        if not scratch.exists():
            scratch.parent.mkdir(parents=True, exist_ok=True)
            scratch.write_text("", encoding="utf-8")
        sketch_path = str(scratch)
    sk = Sketch(sketch_path)  # does NOT write the code: the IDE will
    try:
        before = sk.ino.stat().st_mtime
        name = sk.ino.stem
        w = _find_ide(name)
        if not w:
            _open_in_ide(sk.ino, force=True)
            w = _find_ide(name, wait=20)
        if not w:
            return f"No Arduino IDE window for '{name}' appeared. Open {sk.ino} in the IDE and retry."
        hwnd, title = w
        if not _focus(hwnd):
            return f"Couldn't bring '{title}' to the front, so nothing was typed. Click it and retry."
        saved_clip = _get_clipboard()
        _click_editor(hwnd)
        _chord(0x11, ord("A"))  # select all
        time.sleep(0.1)
        _chord(0x2E)  # Delete
        _step(f"cleared editor in '{title}'")
        lines = code.replace("\r\n", "\n").split("\n")
        step = max(1, lines_per_step)
        done = 0
        _, _, user32 = _win()
        for i in range(0, len(lines), step):
            if user32.GetForegroundWindow() != hwnd:
                _step(f"STOPPED after {done} lines: IDE window lost focus")
                if saved_clip is not None:
                    _set_clipboard(saved_clip)
                return (f"STOPPED after {done}/{len(lines)} lines because the IDE window lost focus "
                        f"(nothing was typed anywhere else). Ask the user not to click away, then retry.")
            chunk = "\n".join(lines[i:i + step]) + ("\n" if i + step < len(lines) else "")
            _set_clipboard(chunk)
            _chord(0x11, ord("V"))  # paste is literal: no auto-indent / auto-close brackets
            done += len(lines[i:i + step])
            time.sleep(max(0.0, delay))
        _step(f"typed {done} lines into '{title}'")
        time.sleep(0.2)
        _chord(0x11, ord("S"))
        _step("pressed Ctrl+S")
        if saved_clip is not None:
            _set_clipboard(saved_clip)
        time.sleep(1.5)
        on_disk = sk.ino.read_text(encoding="utf-8", errors="replace")
        if sk.ino.stat().st_mtime == before:
            return ("NOT SAVED: the IDE didn't save the file, so the typing probably went to another "
                    "part of the IDE. Nothing was typed outside the IDE. Retry, or use check_sketch(code=...).")
        if _norm(on_disk) == _norm(code):
            return f"TYPED OK: {done} lines in '{title}', saved, file matches."
        _step("saved file did NOT match the code")
        diff = next((f"line {n + 1}: IDE has {a!r}, expected {b!r}"
                     for n, (a, b) in enumerate(zip(_norm(on_disk).split("\n"), _norm(code).split("\n")))
                     if a != b), "different length")
        return ("TYPED BUT MISMATCH: the saved file differs from the code (" + diff + "). "
                "The click may have missed the editor. Fix: check_sketch(code=..., sketch_path=...) "
                "writes the file directly and the IDE reloads it.")
    finally:
        sk.cleanup()


def ide_button(action: str = "verify", sketch_name: str = "") -> str:
    """Windows: press Verify (Ctrl+R) or Upload (Ctrl+U) in the open Arduino
    IDE so the user sees it run there. Uses the IDE's selected board/port.
    Can't read the IDE result: confirm with check_sketch or serial."""
    a = action.lower().strip()
    if a not in ("verify", "upload"):
        return "action must be 'verify' or 'upload'."
    return _press_in_ide("r" if a == "verify" else "u", sketch_name) + (
        "\nArduino IDE ignores sketch.yaml: if it says 'No board selected', the user must pick "
        "the board once in the IDE's board dropdown (it remembers it). Or use test_on_board.")


# ---------------------------------------------------------------- parts + wiring page

BOARD_NAMES = {"arduino:samd:nano_33_iot": "Arduino Nano 33 IoT", "arduino:avr:uno": "Arduino Uno",
               "arduino:avr:mega": "Arduino Mega 2560", "arduino:avr:nano": "Arduino Nano"}
BUILTIN_LIBS = {"Wire", "SPI", "EEPROM", "SoftwareSerial", "HID", "Keyboard", "Mouse", "Arduino"}


def _plan(src: str, fqbn: str) -> dict:
    """Work out parts, libraries and connections from the code. Lines like
    `// PART: SG90 servo -> SERVO1` in the sketch add or rename parts."""
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    nocomment = re.sub(r"//.*", "", code)
    consts = dict(re.findall(r"(?:const\s+)?(?:int|byte|uint8_t|short)\s+(\w+)\s*=\s*(\w+)\s*;", nocomment))
    consts.update(re.findall(r"#define\s+(\w+)\s+(\w+)", nocomment))
    pin = lambda x: consts.get(x, x)
    board = BOARD_NAMES.get(fqbn, fqbn)
    libs = sorted(set(re.findall(r'#include\s*[<"]([\w.-]+?)\.h[>"]', nocomment)))
    parts, links = [(board, "the main board")], []
    carrier = "ArduinoMotorCarrier" in libs
    if carrier:
        parts += [("Nano Motor Carrier", "board sits on top of it"),
                  ("18650 Li-ion battery (charged)", "in the carrier's holder, carrier switch ON")]
        links.append(("BATTERY", "18650 battery"))
        for n in sorted(set(re.findall(r"\bservo([1-4])\.", nocomment))):
            parts.append((f"Servo motor", f"SERVO {n} header (brown/black = GND side)"))
            links.append((f"SERVO {n}", "Servo motor"))
        for n in sorted(set(re.findall(r"\bM([1-4])\.", nocomment))):
            parts.append((f"DC motor", f"screw terminal M{n}"))
            links.append((f"M{n}", "DC motor"))
        for n in sorted(set(re.findall(r"\bencoder([12])\.", nocomment))):
            parts.append(("Motor encoder cable", f"ENCODER {n} header"))
            links.append((f"ENC {n}", "Encoder"))
    for obj, p in re.findall(r"(\w+)\.attach\(\s*(\w+)", nocomment):
        parts.append(("Servo motor", f"signal to pin {pin(p)}, red to 5V, brown to GND"))
        links.append((f"D{pin(p)}".replace("DA", "A"), "Servo"))
    used = set()
    for fn, p in re.findall(r"\b(pinMode|digitalWrite|analogWrite|digitalRead|analogRead)\s*\(\s*(\w+)", nocomment):
        p = pin(p)
        if p in ("LED_BUILTIN", "13") or p in used:
            continue
        used.add(p)
        kind = {"analogRead": "analog sensor (e.g. potentiometer)", "digitalRead": "button / digital sensor",
                "analogWrite": "LED or motor driver (PWM)"}.get(fn, "LED / output device")
        label = p if str(p).startswith("A") else f"D{p}"
        parts.append((kind[0].upper() + kind[1:], f"on pin {label} (check what you connected)"))
        links.append((label, kind.split(" (")[0]))
    if "LED_BUILTIN" in nocomment:
        parts.append(("Built-in LED", "on the board, nothing to wire"))
    if re.search(r"Serial\.begin", nocomment):
        baud = (re.findall(r"Serial\.begin\s*\(\s*(\d+)", nocomment) or ["?"])[0]
        parts.append(("USB data cable to the computer", f"Serial at {baud} baud"))
        links.append(("USB", "Computer"))
    for name, where in re.findall(r"//\s*PART:\s*(.+?)\s*->\s*(.+)", src):  # user-declared parts win
        name, where = name.strip(), where.strip()
        key = where.upper().replace(" ", "")
        links = [l for l in links if l[0].upper().replace(" ", "") != key]
        parts = [pt for pt in parts if not re.search(rf"\b{re.escape(where)}\b|pin {re.escape(where.lstrip('Dd'))}\b", pt[1])]
        parts.append((name, where))
        links.append((where, name))
    core = ":".join(fqbn.split(":")[:2])
    software = [f"Board package {core}", "Arduino IDE 2 or arduino-cli"] + \
               [f"Library: {l}" for l in libs if l not in BUILTIN_LIBS]
    names = " ".join(n for n, _ in parts)
    tools = []
    if carrier and "DC motor" in names:
        tools += [("Small flat screwdriver (2-2.5 mm)", "tightens the motor wires in screw terminals"),
                  ("Wire stripper", "bare ~5 mm of each motor wire")]
    if carrier:
        tools.append(("18650 charger", "charge the battery before you start"))
    if any(l[0][:1] in "DA" and l[0][1:2].isdigit() for l in links):
        tools += [("Breadboard", "holds the parts while testing"),
                  ("Jumper wires (male-male)", "board pin to part, and GND to GND")]
    if "LED / output" in names or "LED or motor" in names:
        tools.append(("220 ohm resistor per LED", "stops the LED burning out"))
    tools.append(("Multimeter (optional)", "check battery voltage and wiring"))
    return {"board": board, "parts": parts, "links": links, "software": software, "libs": libs, "tools": tools}


ICONS = {  # tiny line drawings, 24x24, drawn with currentColor
    "board": '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M4 7H2M4 11H2M4 15H2M20 7h2M20 11h2M20 15h2"/><rect x="9" y="8" width="6" height="6"/>',
    "carrier": '<rect x="2" y="5" width="20" height="14" rx="2"/><rect x="7" y="8" width="10" height="5"/><path d="M5 19v2M9 19v2M15 19v2M19 19v2"/>',
    "battery": '<rect x="2" y="8" width="18" height="8" rx="2"/><path d="M20 11h2v2h-2M6 12h4M8 10v4"/>',
    "servo": '<rect x="3" y="9" width="14" height="10" rx="1"/><circle cx="8" cy="9" r="2.5"/><path d="M8 9l9-5M17 14h4"/>',
    "motor": '<circle cx="11" cy="12" r="7"/><path d="M8 15V9l3 3 3-3v6M18 12h4"/>',
    "led": '<path d="M7 14V9a5 5 0 0 1 10 0v5zM5 14h14M10 14v6M14 14v4"/>',
    "sensor": '<circle cx="12" cy="11" r="7"/><path d="M12 11l4-4M8 18v3M12 18v3M16 18v3"/>',
    "button": '<rect x="4" y="6" width="16" height="12" rx="2"/><circle cx="12" cy="12" r="3"/>',
    "usb": '<rect x="7" y="2" width="10" height="9" rx="1"/><path d="M10 5h1M13 5h1M12 11v5M9 16h6v5H9z"/>',
    "pc": '<rect x="3" y="4" width="18" height="12" rx="1"/><path d="M8 20h8M12 16v4"/>',
    "encoder": '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2"/><path d="M12 4v3M12 17v3M4 12h3M17 12h3"/>',
    "screwdriver": '<path d="M14 3l7 7-3 3-7-7zM11 6l-8 8 2 2 3 3 8-8"/>',
    "strip": '<path d="M4 4l8 8M4 20l8-8M12 12l8-4M12 12l8 4"/>',
    "breadboard": '<rect x="2" y="5" width="20" height="14" rx="1"/><path d="M2 12h20M5 8h.1M8 8h.1M11 8h.1M14 8h.1M5 16h.1M8 16h.1M11 16h.1M14 16h.1" stroke-width="2.5"/>',
    "wire": '<path d="M3 18c6 0 4-12 10-12s5 6 8 6"/><rect x="1" y="16" width="4" height="4"/><rect x="19" y="10" width="4" height="4"/>',
    "resistor": '<path d="M2 12h4l2-4 3 8 3-8 3 8 2-4h3"/>',
    "meter": '<rect x="5" y="2" width="14" height="20" rx="2"/><rect x="8" y="5" width="8" height="5"/><circle cx="12" cy="16" r="3"/>',
    "charger": '<rect x="3" y="7" width="18" height="12" rx="2"/><path d="M13 9l-3 4h4l-3 4"/>',
    "part": '<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3"/>',
}
ICON_WORDS = [("charger", "charger"), ("resistor", "resistor"), ("carrier", "carrier"), ("battery", "battery"), ("18650", "battery"),
              ("servo", "servo"), ("motor", "motor"), ("led", "led"), ("sensor", "sensor"), ("potentiometer", "sensor"),
              ("button", "button"), ("usb", "usb"), ("computer", "pc"), ("encoder", "encoder"),
              ("screwdriver", "screwdriver"), ("stripper", "strip"), ("breadboard", "breadboard"),
              ("jumper", "wire"), ("multimeter", "meter"), ("arduino", "board")]


def _icon(name: str, size: int = 28) -> str:
    n = name.lower()
    key = next((v for k, v in ICON_WORDS if k in n), "part")
    return (f'<svg class="ic" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS[key]}</svg>')


def _wire_hint(port: str) -> tuple:
    """(wire class, short how-to) for a connection label."""
    p = port.upper().replace(" ", "")
    if p == "BATTERY":
        return "pw", "+ end to the + mark"
    if p == "USB":
        return "us", "data cable, not charge-only"
    if p.startswith("SERVO"):
        return "sg", "3-pin plug, brown wire = GND"
    if re.match(r"M\d", p):
        return "pw", "2 wires, swap them to reverse"
    if p.startswith("ENC"):
        return "sg", "encoder cable plug"
    return "sg", "signal wire; also GND to GND"


SKIP_BUY = ("Built-in LED", "USB data cable to the computer")


def _shop_html(plan: dict) -> str:
    """Search links only (no scraping): where to buy, example projects, CAD models."""
    import html
    from urllib.parse import quote_plus as q
    e = html.escape
    a = lambda url, t: f'<a href="{e(url)}" target="_blank" rel="noopener">{t}</a>'
    buy = []
    for name, _ in dict.fromkeys((n, 0) for n, _ in plan["parts"]):
        if name in SKIP_BUY:
            continue
        k = q(name.split(" (")[0] + ("" if "arduino" in name.lower() else " arduino"))
        buy.append(f'<li><b>{e(name)}</b> <span>'
                   + " · ".join([a(f"https://www.amazon.com/s?k={k}", "Amazon").replace('href="', 'class="amz" href="'),
                                 a(f"https://www.aliexpress.com/wholesale?SearchText={k}", "AliExpress"),
                                 a(f"https://www.ebay.com/sch/i.html?_nkw={k}", "eBay"),
                                 a(f"https://www.google.com/search?q={k}+site%3Astore.arduino.cc", "Arduino Store"),
                                 a(f"https://www.google.com/search?tbm=shop&q={k}", "compare prices")])
                   + "</span></li>")
    topic = " ".join(dict.fromkeys([plan["board"]] + [n.split(" (")[0] for n, _ in plan["parts"][1:4]
                                                     if n not in SKIP_BUY]))
    t = q(topic)
    proj = " · ".join([a(f"https://projecthub.arduino.cc/search?q={t}", "Arduino Project Hub"),
                       a(f"https://www.hackster.io/search?q={t}", "Hackster"),
                       a(f"https://www.instructables.com/search/?q={t}", "Instructables")])
    cad = " · ".join([a(f"https://www.printables.com/search/models?q={t}", "Printables"),
                      a(f"https://www.thingiverse.com/search?q={t}", "Thingiverse"),
                      a(f"https://grabcad.com/library?query={q(plan['board'])}", "GrabCAD (board 3D model)")])
    regions = "".join(f'<option value="{d}">{d}</option>' for d in
                      ("amazon.com", "amazon.de", "amazon.co.uk", "amazon.fr", "amazon.it", "amazon.es",
                       "amazon.nl", "amazon.ca", "amazon.com.au", "amazon.co.jp", "amazon.in", "amazon.sg"))
    return f"""<section><h2>Where to buy</h2><p class="sub">Amazon site: <select id="amz">{regions}</select>
 · {a("https://www.google.com/maps/search/electronics+components+store", "electronics shops near me")}</p>
<ul>{"".join(buy)}</ul></section>
<section><h2>Ideas and 3D models</h2><ul><li><b>Similar projects</b> <span>{proj}</span></li>
<li><b>CAD / 3D-print models</b> <span>{cad}</span></li></ul></section>
<script>(function(){{var s=document.getElementById('amz'),k='arduino-mcp-amz';
function set(d){{document.querySelectorAll('a.amz').forEach(function(x){{x.href=x.href.replace(/amazon\\.[a-z.]+/,d)}})}}
try{{var v=localStorage.getItem(k);if(v){{s.value=v;set(v)}}}}catch(_){{}}
s.onchange=function(){{set(s.value);try{{localStorage.setItem(k,s.value)}}catch(_){{}}}}}})()</script>"""


def _dots(pts, r: float = 4.5, cls: str = "dot") -> str:
    return "".join(f'<circle cx="{x}" cy="{y}" r="{r}" class="{cls}"/>' for x, y in pts)


NANO_LEFT = ["D13", "3V3", "AREF", "A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7", "5V", "RST", "GND", "VIN"]
NANO_RIGHT = ["D12", "D11", "D10", "D9", "D8", "D7", "D6", "D5", "D4", "D3", "D2", "GND", "RST", "RX0", "TX1"]
SERVO_WIRES = [("signal", "#ffa53a"), ("+5V", "#ff5a5a"), ("GND", "#b07a50")]


def _footprint(kind: str, x: int, y: int) -> str:
    """Top-down outline of a part; its wire pins sit at x, offsets -12/0/+12 around y."""
    b = x + 40
    if kind == "servo":
        return (f'<rect x="{b+12}" y="{y-28}" width="120" height="56" rx="4"/><rect x="{b}" y="{y-8}" width="12" height="16"/>'
                f'<rect x="{b+132}" y="{y-8}" width="12" height="16"/><circle cx="{b+47}" cy="{y}" r="18"/>'
                f'<path d="M{b+47} {y}h60"/><circle cx="{b+47}" cy="{y}" r="5"/>')
    if kind == "motor":
        return (f'<rect x="{b}" y="{y-20}" width="16" height="40" rx="3"/><circle cx="{b+60}" cy="{y}" r="44"/>'
                f'<circle cx="{b+60}" cy="{y}" r="10"/><path d="M{b+104} {y}h26"/>'
                f'<text x="{b+4}" y="{y-24}" class="pin">+</text><text x="{b+4}" y="{y+38}" class="pin">-</text>')
    if kind == "led":
        return f'<path d="M{b} {y-22}h10a22 22 0 1 1 0 44h-10z"/>'
    if kind == "sensor":
        return f'<circle cx="{b+34}" cy="{y}" r="32"/><path d="M{b+34} {y}l18 -18"/>'
    if kind == "button":
        return f'<rect x="{b}" y="{y-28}" width="56" height="56" rx="4"/><circle cx="{b+28}" cy="{y}" r="16"/>'
    if kind in ("pc", "usb"):
        return (f'<rect x="{b}" y="{y-44}" width="150" height="88" rx="6"/><rect x="{b+10}" y="{y-34}" width="130" height="62"/>'
                f'<path d="M{b-15} {y+54}h180"/>')
    return f'<rect x="{b}" y="{y-30}" width="100" height="60" rx="3"/>'


def _board_geometry(plan: dict, T: int) -> dict:
    """Outline SVG + {port: [(x, y, edge, wire_label, colour)]} for the board seen from above.
    Nano pin order is the standard Nano header. Motor Carrier port positions are approximate."""
    nano = "nano" in plan["board"].lower()
    carrier = "Nano Motor Carrier" in " ".join(p for p, _ in plan["parts"])
    ports, svg, notes = {}, [], []
    used = {l[0].upper().replace(" ", "") for l in plan["links"]}
    if nano:
        nx = 260 if carrier else 150
        ny = T + (90 if carrier else 50)
        rx = nx + 180
        svg.append(f'<g class="o"><rect x="{nx-22}" y="{ny-40}" width="224" height="{14*30+70}" rx="8"/>'
                   f'<rect x="{nx+68}" y="{ny-56}" width="44" height="30" rx="4"/></g>'
                   f'<text x="{nx+90}" y="{ny+14*30/2+5}" text-anchor="middle" class="hint" '
                   f'transform="rotate(-90 {nx+90} {ny+14*30/2})">{plan["board"]}</text>')
        for col, names, edge, tx, anchor in ((nx, NANO_LEFT, "l", nx + 14, "start"),
                                              (rx, NANO_RIGHT, "r", rx - 14, "end")):
            for i, nm in enumerate(names):
                y = ny + i * 30
                key = {"RX0": "D0", "TX1": "D1"}.get(nm, nm)
                hot = key in used
                svg.append(_dots([(col, y)], 6.5 if hot else 3.5, "dot hot" if hot else "dot"))
                svg.append(f'<text x="{tx}" y="{y+5}" text-anchor="{anchor}" class="{"pinhot" if hot else "pinsm"}">{nm}</text>')
                ports.setdefault(key, [(col, y, edge, "signal", "#ffffff")])
        ports["GND_"] = [(rx, ny + 11 * 30, "r", "GND", "#9fb3d9")]
        ports["5V_"] = [(nx, ny + 11 * 30, "l", "+5V", "#ff5a5a")]
        ports["USB"] = [(nx + 90, ny - 56, "t", "USB cable", "#ffffff")]
        if carrier:
            cx0, cy0, cw, ch = 80, T, 540, 14 * 30 + 200
            svg.append(f'<g class="o"><rect x="{cx0}" y="{cy0}" width="{cw}" height="{ch}" rx="12" class="ap"/>'
                       f'<rect x="{cx0+22}" y="{cy0+120}" width="70" height="300" rx="30"/>'
                       f'<rect x="{cx0+30}" y="{cy0+440}" width="54" height="26" rx="4"/></g>'
                       f'<text x="{cx0+57}" y="{cy0+145}" text-anchor="middle" class="pin">+</text>'
                       f'<text x="{cx0+57}" y="{cy0+410}" text-anchor="middle" class="pin">-</text>'
                       f'<text x="{cx0+57}" y="{cy0+275}" text-anchor="middle" class="hint" '
                       f'transform="rotate(-90 {cx0+57} {cy0+270})">18650 battery</text>'
                       f'<text x="{cx0+57}" y="{cy0+485}" text-anchor="middle" class="pinsm">ON/OFF</text>'
                       f'<text x="{cx0+cw-14}" y="{cy0+26}" text-anchor="end" class="hint">Nano Motor Carrier</text>')
            ex = cx0 + cw
            for n in range(1, 5):
                y = cy0 + 70 + (n - 1) * 80
                key, hot = f"SERVO{n}", f"SERVO{n}" in used
                svg.append(f'<rect x="{ex-30}" y="{y-20}" width="18" height="40" class="o"/>'
                           + _dots([(ex - 21, y - 12 + 12 * k) for k in range(3)], 5 if hot else 3.5, "dot hot" if hot else "dot")
                           + f'<text x="{ex-38}" y="{y+5}" text-anchor="end" class="{"pinhot" if hot else "pinsm"}">SERVO {n}</text>')
                ports[key] = [(ex - 21, y - 12 + 12 * k, "r", w, c) for k, (w, c) in enumerate(SERVO_WIRES)]
            by = cy0 + ch
            for n in range(1, 5):
                x = cx0 + 150 + (n - 1) * 95
                key, hot = f"M{n}", f"M{n}" in used
                svg.append(f'<rect x="{x-24}" y="{by-34}" width="48" height="22" class="o"/>'
                           + _dots([(x - 12, by - 23), (x + 12, by - 23)], 5 if hot else 3.5, "dot hot" if hot else "dot")
                           + f'<text x="{x}" y="{by-42}" text-anchor="middle" class="{"pinhot" if hot else "pinsm"}">M{n} + -</text>')
                ports[key] = [(x - 12, by - 23, "b", f"M{n}+", "#ff5a5a"), (x + 12, by - 23, "b", f"M{n}-", "#9fb3d9")]
            notes.append("Motor Carrier port positions are approximate (Arduino's docs don't give them). "
                         "Match the labels printed on your board.")
            notes.append("Servo plug: brown/black = GND (-), red = +5V (middle), orange/yellow = signal. "
                         "Line brown up with the - or GND mark on the header.")
        top, bottom, right = (T, T + 14 * 30 + 200, 620) if carrier else (ny - 60, ny + 14 * 30 + 40, rx + 30)
    else:  # generic board: used ports in a column, not to scale
        hs = [l[0] for l in plan["links"]]
        h = 60 * (len(hs) + 2) + 50
        svg.append(f'<g class="o"><rect x="80" y="{T}" width="300" height="{h}" rx="10"/></g>'
                   f'<text x="100" y="{T+30}" class="hint">{plan["board"]}</text>')
        for i, p in enumerate(hs + ["5V", "GND"]):
            y = T + 70 + i * 60
            hot = i < len(hs)
            svg.append(_dots([(380, y)], 6.5 if hot else 4.5, "dot hot" if hot else "dot")
                       + f'<text x="364" y="{y+5}" text-anchor="end" class="{"pinhot" if hot else "pin"}">{p}</text>')
            key = p.upper().replace(" ", "")
            ports[{"5V": "5V_", "GND": "GND_"}.get(key, key) if not hot else key] = [
                (380, y, "r", {"USB": "USB cable", "5V": "+5V", "GND": "GND"}.get(key, "signal"),
                 {"5V": "#ff5a5a", "GND": "#9fb3d9"}.get(key, "#ffffff"))]
        notes.append(f"Pin positions for {plan['board']} are not drawn to scale: use the labels on your board.")
        top, bottom, right = T, T + h, 400
    return {"svg": "".join(svg), "ports": ports, "notes": notes, "top": top, "bottom": bottom, "right": right}


def _schematic_svg(sketch: str, plan: dict) -> str:
    """Wiring drawing, blueprint style: the board from above with its real header pins, each wire from
    the exact pin/port to the part. Approximate positions are said so on the drawing."""
    import html
    e = html.escape
    T = 170
    g = _board_geometry(plan, T)
    P = g["ports"]
    items = []  # (part, kind, [(x, y, edge, label, colour)])
    for port, part in plan["links"]:
        key = port.upper().replace(" ", "")
        kind = next((v for k, v in ICON_WORDS if k in part.lower()), "part")
        if key == "BATTERY" and "SERVO1" in P:
            continue  # sits in the carrier's own holder, drawn on the board
        src = list(P.get(key, []))
        if not src:
            continue
        if len(src) == 1 and key != "USB":  # a single board pin: add the power/ground wires it needs
            if kind == "servo":
                src = [src[0]] + P["5V_"] + P["GND_"]
                src = [(x, y, ed, w, c) for (x, y, ed, _, _), (w, c) in zip(src, SERVO_WIRES)]
            else:
                src = src + P["GND_"]
        items.append((part, kind, port, src))
        if kind == "servo" and "33 IoT" in plan["board"] and src[1][3] == "+5V" and "SERVO1" not in P:
            g["notes"].append("Nano 33 IoT: its 5V pin stays off until the VUSB jumper is soldered. "
                              "Power the servo from a separate 5V supply (GND shared) if it doesn't move.")
    order = {"t": 0, "r": 1, "l": 2, "b": 3}
    items.sort(key=lambda it: (order[it[3][0][2]], it[3][0][1], it[3][0][0]))
    W, PX = 1320, 980
    step = 200
    rows_top = T + 20
    n = max(1, len(items))
    span = max(g["bottom"] - T, step * n)
    ys = [rows_top + int(span * (i + 0.5) / n) for i in range(n)]
    lane_b = g["bottom"] + 30
    o, labels = [], []
    ch, bl, tl = g["right"] + 40, 0, 0
    for (part, kind, port, src), yr in zip(items, ys):
        offs = [-20, 0, 20] if len(src) == 3 else ([-14, 14] if len(src) == 2 else [0])
        for (x, y, edge, wl, col), off in zip(src, offs):
            ty = yr + off
            if edge == "r":
                pts = [(x, y), (ch, y), (ch, ty)]
            elif edge == "t":
                ly = g["top"] - 40 - 14 * tl; tl += 1
                pts = [(x, y), (x, ly), (ch, ly), (ch, ty)]
            else:  # left or bottom: run under the board
                ly = lane_b + 14 * bl; bl += 1
                pts = [(x, y), ((x - 30) if edge == "l" else x, y), ((x - 30) if edge == "l" else x, ly), (ch, ly), (ch, ty)]
            pts.append((PX, ty))
            d = "M" + " L".join(f"{a} {b}" for a, b in pts)
            dash = ' stroke-dasharray="7 5"' if wl in ("GND",) or wl.endswith("-") else ""
            o.append(f'<path d="{d}" class="w" stroke="{col}"{dash}/>')
            labels.append(f'<text x="{PX-12}" y="{ty-5}" text-anchor="end" class="wl" fill="{col}">{e(wl)}</text>')
            ch += 14
        o.append(f'<g class="o">{_footprint(kind, PX, yr)}</g>' + _dots([(PX, yr + k) for k in offs]))
        hint = _wire_hint(port)[1]
        o.append(f'<text x="{PX+44}" y="{yr-62}" class="lbl">{e(part)}</text>'
                 f'<text x="{PX+44}" y="{yr+72}" class="hint">{e(hint)}</text>')
    H = max(lane_b + 14 * bl, ys[-1] + 90 if items else 0) + 40 + 26 * len(g["notes"])
    W = max(W, PX + 330)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
           f'font-family="Segoe UI,Arial,sans-serif" role="img" aria-label="wiring drawing of {e(sketch)}">',
           '<style>.o{fill:none;stroke:#fff;stroke-width:2}.ap{stroke-dasharray:10 6}'
           '.dot{fill:#fff;stroke:none;filter:url(#gl)}.hot{fill:#ffe066}text{fill:#fff;stroke:none}'
           '.ttl{font-size:30px;font-weight:700}.lbl{font-size:22px;font-weight:700}.hint{font-size:16px;fill:#cfe0ff}'
           '.pin{font-size:18px;font-weight:700}.pinsm{font-size:12px;fill:#b9c6e6}'
           '.pinhot{font-size:16px;font-weight:700;fill:#ffe066}.wl{font-size:14px;font-weight:700}'
           '.w{fill:none;stroke-width:3;stroke-linejoin:round}</style>',
           '<defs><filter id="gl" x="-2" y="-2" width="5" height="5"><feGaussianBlur stdDeviation="3" result="b"/>'
           '<feFlood flood-color="#7fd0ff"/><feComposite in2="b" operator="in"/>'
           '<feMerge><feMergeNode/><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>',
           f'<rect width="{W}" height="{H}" fill="#3d4b78"/>',
           f'<text x="40" y="52" class="ttl">{e(sketch)}: how to wire it</text>',
           '<text x="40" y="80" class="hint">Yellow = pins/ports this sketch uses. Dashed = GND or minus.</text>',
           g["svg"], "".join(o), "".join(labels)]
    for i, nt in enumerate(g["notes"]):
        out.append(f'<text x="40" y="{H-24-26*(len(g["notes"])-1-i)}" class="hint">{e(nt)}</text>')
    out.append("</svg>")
    return "".join(out)


def _guide_html(sketch: str, plan: dict) -> str:
    import html
    e = html.escape
    svg = _schematic_svg(sketch, plan)
    li = lambda items: "".join(f'<li><label><input type="checkbox"> {_icon(a)} <span class="t"><b>{e(a)}</b> '
                               f'<span>{e(b)}</span></span></label></li>' for a, b in items)
    sw = "".join(f'<li><label><input type="checkbox"> {e(x)}</label></li>' for x in plan["software"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(sketch)} parts</title><style>
:root{{--bg:#fff;--fg:#1b1f24;--mut:#5b6470;--card:#f3f5f7;--acc:#00878f;--line:#9aa4ae}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16191d;--fg:#e8eaed;--mut:#a0a8b2;--card:#22272d;--acc:#2bb7bf;--line:#5d6670}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}}
main{{max-width:820px;margin:auto;padding:24px 16px}} h1{{font-size:22px;margin:0 0 4px}}
.sub{{color:var(--mut);margin:0 0 20px}} section{{background:var(--card);border-radius:12px;padding:16px;margin:14px 0}}
h2{{font-size:16px;margin:0 0 8px}} ul{{list-style:none;padding:0;margin:0}} li{{padding:4px 0}}
li span span{{color:var(--mut)}} li label{{display:flex;gap:10px;align-items:center}}
.ic{{flex:none;color:var(--acc)}} .pi .ic{{color:var(--acc)}} .bi .ic{{color:#fff}} .h{{fill:var(--mut);font-size:11px}}
.pw{{stroke:#d9534f}} .sg{{stroke:var(--acc)}} .us{{stroke:var(--line)}} .lg{{color:var(--mut);font-size:13px;margin:6px 0 0}}
.k{{display:inline-block;width:18px;height:3px;margin:0 6px 3px 12px;background:var(--line)}} .k.pw{{background:#d9534f}}
.k.sg{{background:var(--acc)}} .sc svg{{width:100%;height:auto;border-radius:8px}} .w{{stroke-width:3}}
.b{{fill:var(--acc)}} .bt{{fill:#fff;font-weight:600;font-size:15px}} .p{{fill:var(--bg);stroke:var(--line)}}
text{{fill:var(--fg);font-size:13px}} .l{{fill:var(--acc);font-weight:600;font-size:12px}}
.d{{fill:var(--acc)}} a{{color:var(--acc)}} select{{font:inherit}} .note{{color:var(--mut);font-size:13px}}</style></head><body><main>
<h1>{e(sketch)}: what you need</h1><p class="sub">Generated from the sketch's code. Tick things off as you go.</p>
<section><h2>Wiring schematic</h2><div class="sc">{svg}</div></section>
<section><h2>Parts</h2><ul>{li(plan["parts"])}</ul></section>
<section><h2>Tools you need</h2><ul>{li(plan["tools"])}</ul></section>
<section><h2>Software</h2><ul>{sw}</ul></section>
{_shop_html(plan)}
<p class="note">Worked out from the code, so it knows which pins and ports are used, not which exact part you
plugged in. Add a line like <code>// PART: SG90 servo -> SERVO1</code> to the sketch to name a part.
Shop, project and CAD links are searches, not checked prices or stock.</p>
</main></body></html>"""


def _write_guide(sk: "Sketch", fqbn: str) -> str:
    plan = _plan(sk.ino.read_text(encoding="utf-8", errors="replace"), fqbn)
    out = sk.src_dir / f"{sk.ino.stem}_parts.html"
    out.write_text(_guide_html(sk.ino.stem, plan), encoding="utf-8")
    (sk.src_dir / f"{sk.ino.stem}_schematic.svg").write_text(_schematic_svg(sk.ino.stem, plan), encoding="utf-8")
    _step(f"wrote {out}")
    try:
        import webbrowser
        webbrowser.open(out.as_uri())
        _step("opened it in the browser")
    except Exception:
        pass
    return f"PARTS PAGE: {out} + {sk.ino.stem}_schematic.svg ({len(plan['parts'])} parts, {len(plan['tools'])} tools, {len(plan['links'])} connections)"


# ---------------------------------------------------------------- public tools
# 8 small tools. Descriptions are kept short on purpose: they cost tokens.

@tool
def check_sketch(code: str = "", sketch_path: str = "", fqbn: str = "", guide: bool = False) -> str:
    """Compile + check (no board). Pass code or sketch_path. Errors come with line, code, hint.
    guide=True: also open a parts + wiring page for the user."""
    out = _check_sketch(sketch_path=sketch_path, code=code, fqbn=fqbn)
    if guide:
        sk = Sketch(sketch_path) if sketch_path else Sketch(str(SCRATCH / "mcp_sketch" / "mcp_sketch.ino"))
        try:
            _, board = _resolve("", fqbn, sk.dir if not sk.tmp else None, need_port=False)
            out += "\n" + _write_guide(sk, board)
        finally:
            sk.cleanup()
    return out


@tool
def wiring_steps(steps: list[dict], parts: list[dict] = [], board: str = "mega",
                 title: str = "Wiring", sketch_path: str = "") -> str:
    """Open a 3D step-by-step breadboard wiring page (Back/Next). steps: [{title, notes[],
    place[part ids], wires[[from,to,color]]}]. Ends: hole 'j10', rail '+top' '-bot' ('-bot:13'),
    board 'pin:10' 'pin:GND', part 'servo.SIG' 'motor.1', chip 'U1.3'. parts: [{id,type:
    dip(pins,col)|power|resistor(a,b)|led(a,k,color)|servo|motor|battery9v|generic(terms,label)}]"""
    import wiring3d
    page = wiring3d.build_html(title, board, list(parts), list(steps))
    folder = Path(sketch_path).parent if sketch_path else SCRATCH
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / (re.sub(r"\W+", "_", title).strip("_").lower() + "_wiring_steps.html")
    out.write_text(page, encoding="utf-8")
    _step(f"wrote {out}")
    try:
        import webbrowser
        webbrowser.open(out.as_uri())
        _step("opened it in the browser")
    except Exception:
        pass
    return f"WIRING STEPS: {out} ({len(steps)} steps, {sum(len(s.get('wires', [])) for s in steps)} wires)"


@tool
def test_on_board(code: str = "", sketch_path: str = "", expect: list[str] = [],
                  send: list[str] = [], fail_on: list[str] = [], upload: bool = True,
                  port: str = "", fqbn: str = "", baud: int = 115200, seconds: float = 8) -> str:
    """Compile, upload, send lines, read Serial -> PASS if all `expect` text seen.
    Port/board auto-detected. Sketch should print markers (e.g. READY) repeatedly."""
    return _test_on_board(port=port, sketch_path=sketch_path, code=code, fqbn=fqbn,
                          expect=list(expect), fail_on=list(fail_on), send=list(send),
                          baud=baud, seconds=seconds, upload=upload)


@tool
def serial(text: str = "", port: str = "", baud: int = 115200, seconds: float = 3) -> str:
    """Send a line (optional) and read Serial output."""
    return _serial(port=port, text=text, baud=baud, seconds=seconds)


@tool
def boards(search: str = "") -> str:
    """No args: arduino-cli status + connected boards. search: find a board's FQBN."""
    if search:
        return search_boards(search)
    return check_setup() + "\nConnected:\n" + list_connected_boards()


@tool
def install(name: str) -> str:
    """Install a board core ('arduino:samd') or a library ('Servo')."""
    return install_core(name) if re.fullmatch(r"[\w.-]+:[\w.-]+", name) else install_library(name)


@tool
def library(name: str = "", filter: str = "", search: str = "") -> str:
    """Real functions of installed library `name` (filter narrows). search: find libraries.
    No args: list installed. Use before calling unfamiliar library functions."""
    return search_libraries(search) if search else library_api(name, filter)


@tool
def ide(action: str, code: str = "", sketch_path: str = "") -> str:
    """Arduino IDE window (Windows), for the user to watch. action: open | type (put code
    in the editor line by line, save, verify file) | verify | upload (press the IDE button;
    needs board picked in IDE; can't read result)."""
    a = action.lower().strip()
    if a == "open":
        return open_in_ide(sketch_path=sketch_path, code=code)
    if a == "type":
        return type_in_ide(code=code, sketch_path=sketch_path)
    if a in ("verify", "upload"):
        name = Path(sketch_path).stem if sketch_path else ""
        return ide_button(action=a, sketch_name=name)
    return "action must be open, type, verify or upload."


def _strip_titles(o):
    if isinstance(o, dict):
        o.pop("title", None)
        for v in o.values():
            _strip_titles(v)
    elif isinstance(o, list):
        for v in o:
            _strip_titles(v)


for _t in mcp._tool_manager.list_tools():  # drop auto "title" fields: pure token waste
    _strip_titles(_t.parameters)


if __name__ == "__main__":
    print("Arduino MCP server running (stdio). Claude starts this for you; "
          "you don't need to keep this window open.", file=sys.stderr)
    mcp.run()
