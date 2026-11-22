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

from mcp.server.fastmcp import FastMCP

DEFAULT_FQBN = os.environ.get("ARDUINO_FQBN", "arduino:samd:nano_33_iot")
WORK = Path(tempfile.gettempdir()) / "arduino_mcp"
SCRATCH = Path.home() / "Documents" / "arduino-mcp-sketches"

mcp = FastMCP(
    "arduino",
    instructions=(
        "Workflow for Arduino code: 1) check_sketch (fast, no board), "
        "2) fix errors it reports and repeat until it says COMPILE OK, "
        "3) test_on_board with expect=[...] to upload and verify Serial "
        "output, 4) if FAIL, read the log, fix, and call test_on_board again. "
        "Before using an unfamiliar library function, call library_api to "
        "read the real installed header instead of guessing."
    ),
)


# ================================================================ helpers

def _find_cli() -> str:
    """Find arduino-cli: env var, then PATH, then inside Arduino IDE 2."""
    env = os.environ.get("ARDUINO_CLI")
    if env and Path(env).exists():
        return env
    found = shutil.which("arduino-cli")
    if found:
        return found
    candidates = []
    for base in (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            for ide in ("Arduino IDE", "Programs/Arduino IDE", "Programs/arduino-ide"):
                candidates.append(Path(base) / ide / "resources/app/lib/backend/resources/arduino-cli.exe")
    candidates.append(Path("/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli"))
    for c in candidates:
        if c.exists():
            return str(c)
    raise RuntimeError(
        "arduino-cli was not found. Install it (see README) or set ARDUINO_CLI "
        "to the full path of arduino-cli.exe in your MCP config."
    )


def _exec(args: list[str], timeout: int = 600) -> tuple[int, str]:
    cmd = [_find_cli(), *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return -1, f"Timed out after {timeout}s"
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
            p.write_text(code, encoding="utf-8")
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
    (r"No such file or directory", "A library is missing. Run search_libraries with the header name, then install_library."),
    (r"was not declared in this scope", "Name is unknown: typo, missing #include, or the variable is declared inside another function. Check spelling/case."),
    (r"has no member named", "That function doesn't exist in this library/class. Call library_api to see the real function names."),
    (r"no matching function for call", "Function exists but the arguments are wrong. Call library_api to see the real parameters."),
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


def _compile(sk: "Sketch", fqbn: str) -> tuple[bool, str]:
    sk.build.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    code, out = _exec(["compile", "--fqbn", fqbn, "--warnings", "all",
                       "--build-path", str(sk.build), str(sk.dir)], 900)
    secs = time.time() - t0
    errors, warnings, usage = _parse_compile(out, sk)
    ok = code == 0
    parts = [f"{'COMPILE OK' if ok else 'COMPILE FAILED'} ({secs:.0f}s, board {fqbn}, sketch {sk.ino})"]
    if usage:
        parts.append(usage)
    if errors:
        parts.append(_fmt(errors, "ERROR"))
    if warnings:
        parts.append(_fmt(warnings[:15], "WARNING"))
    if not ok and not errors:  # a failure we couldn't parse: show raw tail
        parts.append("Raw output:\n" + sk.fix_paths(out[-3000:]))
    return ok, "\n\n".join(parts)


# ---- static checks (no board, no compiler): things the compiler won't catch

def _static_checks(src: str, fqbn: str) -> list[str]:
    notes = []
    code = re.sub(r"//.*", "", src)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    begins = re.findall(r"Serial\.begin\s*\(\s*(\d+)", code)
    if re.search(r"Serial\.(print|println|write|read|available|parseInt)", code) and not begins:
        notes.append("Serial is used but Serial.begin(...) is never called in setup().")
    if begins:
        notes.append(f"Serial baud rate is {begins[0]}: use the same baud in serial_read / test_on_board.")
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


def _find_port(port: str, wait: float = 10.0) -> str:
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


# ================================================================ tools

@mcp.tool()
def check_setup() -> str:
    """Show which arduino-cli is used, its version, and installed board cores.
    Run this first if anything fails."""
    try:
        cli = _find_cli()
    except RuntimeError as e:
        return str(e)
    return (f"arduino-cli: {cli}\nDefault board (FQBN): {DEFAULT_FQBN}\n\n"
            + _run(["version"], 30) + "\n\n" + _run(["core", "list"], 60))


@mcp.tool()
def list_connected_boards() -> str:
    """List boards/serial ports plugged in right now (port name like COM5,
    and the board's FQBN when arduino-cli recognises it)."""
    code, out = _exec(["board", "list", "--format", "json"], 60)
    try:
        data = json.loads(out[out.index("{") if out.lstrip().startswith("{") else out.index("["):])
    except Exception:
        return out
    ports = data.get("detected_ports", []) if isinstance(data, dict) else data
    lines = []
    for item in ports or []:
        port = item.get("port", {})
        boards = item.get("matching_boards") or []
        name = ", ".join(f"{b.get('name')} ({b.get('fqbn')})" for b in boards) or "unknown board"
        lines.append(f"{port.get('address')}  [{port.get('protocol_label', port.get('protocol'))}]  {name}")
    return "\n".join(lines) or "No ports found. Is the board plugged in with a data USB cable?"


@mcp.tool()
def list_serial_ports() -> str:
    """List every serial port the computer sees (backup for when
    list_connected_boards shows nothing)."""
    from serial.tools import list_ports
    ports = [f"{p.device}  {p.description}" for p in list_ports.comports()]
    return "\n".join(ports) or "No serial ports found."


@mcp.tool()
def search_boards(query: str) -> str:
    """Find a board's FQBN by name, e.g. 'nano 33 iot' or 'mega'.
    Only boards whose core is installed are listed."""
    return _run(["board", "listall", query], 60)


@mcp.tool()
def install_core(core: str = "arduino:samd") -> str:
    """Install a board package (core). arduino:samd = Nano 33 IoT,
    arduino:avr = Uno/Mega/Nano. Can take a few minutes the first time."""
    _exec(["core", "update-index"], 300)
    return _run(["core", "install", core], 900)


@mcp.tool()
def search_libraries(query: str) -> str:
    """Search the Arduino Library Manager, e.g. 'motor carrier' or a header
    name from a 'No such file' error."""
    out = _run(["lib", "search", query, "--omit-releases-details"], 120)
    return out[:6000] + ("\n...(cut)" if len(out) > 6000 else "")


@mcp.tool()
def install_library(name: str) -> str:
    """Install a library by its Library Manager name, e.g. 'ArduinoMotorCarrier'.
    Dependencies are installed too."""
    return _run(["lib", "install", name], 600)


@mcp.tool()
def list_installed_libraries() -> str:
    """List libraries that are already installed."""
    return _run(["lib", "list"], 60)


@mcp.tool()
def library_api(library: str, filter: str = "") -> str:
    """Fact-check a library: read the REAL function names and parameters from
    the installed library's header (.h) files, instead of guessing.
    library = name as in list_installed_libraries (e.g. 'ArduinoMotorCarrier').
    filter = optional word to narrow the output (e.g. 'setDuty')."""
    code, out = _exec(["lib", "list", "--format", "json"], 60)
    try:
        data = json.loads(out[out.index("{") if out.lstrip().startswith("{") else out.index("["):])
    except Exception:
        return "Could not read the library list:\n" + out[-2000:]
    libs = data.get("installed_libraries", []) if isinstance(data, dict) else data
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
            lines.extend(found[:120])
    text = "\n".join(lines)
    return text[:12000] + ("\n...(cut, use filter)" if len(text) > 12000 else "")


@mcp.tool()
def check_sketch(sketch_path: str = "", code: str = "", fqbn: str = "") -> str:
    """FAST check with no board needed: compiles the sketch and fact-checks it.
    Returns each error/warning with file, line, the code on that line and a
    plain-English hint, plus extra checks the compiler doesn't do (missing
    Serial.begin, Motor Carrier ping, long delays...).
    Give either sketch_path (.ino or folder) or code (full sketch text; it is
    saved to sketch_path if given, else to Documents/arduino-mcp-sketches).
    Fix every ERROR and call again until it says COMPILE OK."""
    board = fqbn or DEFAULT_FQBN
    sk = Sketch(sketch_path, code)
    try:
        ok, report = _compile(sk, board)
        notes = _static_checks(sk.ino.read_text(encoding="utf-8", errors="replace"), board)
        if notes:
            report += "\n\nEXTRA CHECKS (not compiler errors, but likely problems):\n" + \
                      "\n".join(f"- {n}" for n in notes)
        report += ("\n\nNext: test_on_board to run it on the Arduino." if ok
                   else "\n\nNext: fix the errors above and call check_sketch again.")
        return report
    finally:
        sk.cleanup()


@mcp.tool()
def compile_sketch(sketch_path: str, fqbn: str = "") -> str:
    """Compile (verify) a sketch. Same as check_sketch without the extra checks."""
    sk = Sketch(sketch_path)
    try:
        return _compile(sk, fqbn or DEFAULT_FQBN)[1]
    finally:
        sk.cleanup()


@mcp.tool()
def upload_sketch(sketch_path: str, port: str, fqbn: str = "") -> str:
    """Compile and upload a sketch to the board on `port` (e.g. COM5; see
    list_connected_boards). Close the Serial Monitor and any Python script
    using the port first, or the upload fails with 'access denied'."""
    sk = Sketch(sketch_path)
    board = fqbn or DEFAULT_FQBN
    try:
        ok, report = _compile(sk, board)
        if not ok:
            return report
        return report + "\n\n" + _run(["upload", "-p", port, "--fqbn", board,
                                       "--input-dir", str(sk.build), str(sk.dir)], 300)
    finally:
        sk.cleanup()


@mcp.tool()
def test_on_board(port: str, sketch_path: str = "", code: str = "", fqbn: str = "",
                  expect: list[str] | None = None, fail_on: list[str] | None = None,
                  send: list[str] | None = None, baud: int = 115200,
                  seconds: float = 8, upload: bool = True) -> str:
    """The full auto test in one call: compile -> upload -> open Serial ->
    optionally send lines -> read output -> PASS/FAIL.
      expect:  strings that must appear in Serial output (e.g. ["READY", "ok"])
      fail_on: strings that mean failure (e.g. ["ERROR", "not found"])
      send:    lines to send after connecting (e.g. ["50,80"]), one per 0.3 s
      seconds: max time to listen (stops early when all expect strings seen)
      upload=False tests the code already on the board.
    Tip: make the sketch print a line like "READY" at the end of setup() and
    results like "TEST ok" so they can be checked. Output printed in the first
    ~second after reset can be missed while the USB port reconnects; printing
    repeatedly, or in reply to a sent line, avoids that.
    If it FAILS, read the log, fix the code and call this again."""
    expect, fail_on, send = expect or [], fail_on or [], send or []
    board = fqbn or DEFAULT_FQBN
    report = []
    if upload:
        sk = Sketch(sketch_path, code)
        try:
            ok, rep = _compile(sk, board)
            report.append(rep)
            if not ok:
                return "RESULT: FAIL at compile\n\n" + "\n\n".join(report)
            t0 = time.time()
            c, out = _exec(["upload", "-p", port, "--fqbn", board,
                            "--input-dir", str(sk.build), str(sk.dir)], 300)
            if c != 0:
                report.append("UPLOAD FAILED:\n" + out[-2500:] +
                              "\n\nhint: close Serial Monitor / Python scripts on this port; "
                              "or double-tap the Nano's reset button and retry; check the COM port.")
                return "RESULT: FAIL at upload\n\n" + "\n\n".join(report)
            report.append(f"UPLOAD OK ({time.time() - t0:.0f}s)")
        finally:
            sk.cleanup()
        time.sleep(1.0)
        new_port = _find_port(port)
        if new_port != port:
            report.append(f"Board came back on {new_port} (was {port}).")
            port = new_port
    try:
        s = _open_serial(port, baud, retries=8 if upload else 0)
    except Exception as e:
        report.append(f"Could not open {port}: {e}")
        return "RESULT: FAIL at serial\n\n" + "\n\n".join(report)
    with s:
        for line in send:
            s.write((line + "\n").encode())
            s.flush()
            time.sleep(0.3)
        log = _read_until(s, min(max(seconds, 0.5), 120), expect, fail_on)
    missing = [e for e in expect if e not in log]
    bad = [f for f in fail_on if f in log]
    if bad:
        verdict = f"FAIL: saw {bad}"
    elif missing:
        verdict = f"FAIL: never saw {missing}"
    elif not expect and not log.strip():
        verdict = "UNKNOWN: board printed nothing (no expect given)"
    else:
        verdict = "PASS" if expect else "RAN (no expect given, read the log yourself)"
    report.append(f"SERIAL LOG ({port} @ {baud}, sent {send}):\n" + (log.strip() or "(nothing received)"))
    return f"RESULT: {verdict}\n\n" + "\n\n".join(report)


@mcp.tool()
def serial_read(port: str, baud: int = 115200, seconds: float = 5) -> str:
    """Listen to the board's Serial output for a few seconds (max 60) and
    return what it printed. Same as watching the Serial Monitor."""
    try:
        with _open_serial(port, baud) as s:
            return _read_until(s, min(max(seconds, 0.5), 60), [], []) or "(nothing received)"
    except Exception as e:
        return f"Could not open {port}: {e} (is another program using it?)"


@mcp.tool()
def serial_send(port: str, text: str, baud: int = 115200, read_seconds: float = 2,
                add_newline: bool = True) -> str:
    """Send text to the board over Serial (e.g. '50,80'),
    then return whatever the board replies during read_seconds."""
    try:
        with _open_serial(port, baud) as s:
            s.write((text + ("\n" if add_newline else "")).encode())
            s.flush()
            return _read_until(s, min(max(read_seconds, 0), 60), [], []) or "(nothing received)"
    except Exception as e:
        return f"Could not open {port}: {e} (is another program using it?)"


if __name__ == "__main__":
    print("Arduino MCP server running (stdio). Claude starts this for you; "
          "you don't need to keep this window open.", file=sys.stderr)
    mcp.run()
