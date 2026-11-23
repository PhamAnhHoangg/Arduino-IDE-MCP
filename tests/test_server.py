import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {"check_sketch", "wiring_steps", "test_on_board", "serial", "boards", "install", "library", "ide"}


async def _session(fn):
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "arduino_mcp.py")])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await fn(s)


def test_server_lists_all_tools():
    async def fn(s):
        return {t.name for t in (await s.list_tools()).tools}
    assert asyncio.run(_session(fn)) == TOOLS


def test_wiring_steps_tool_writes_page():
    spec = json.loads((ROOT / "examples" / "face_fan_wiring" / "spec.json").read_text(encoding="utf-8"))

    async def fn(s):
        return (await s.call_tool("wiring_steps", spec)).content[0].text
    out = asyncio.run(_session(fn))
    assert out.startswith("WIRING STEPS:") and f"{len(spec['steps'])} steps" in out


def test_wiring_nudge_only_for_wired_sketches():
    sys.path.insert(0, str(ROOT))
    import arduino_mcp as m
    wired = (ROOT / "examples" / "serial_selftest" / "serial_selftest.ino").read_text(encoding="utf-8")
    assert m._wiring_nudge("COMPILE OK", "void setup(){Serial.begin(9600);} void loop(){}", "", "arduino:avr:uno") == ""
    servo = "#include <Servo.h>\nServo s; void setup(){s.attach(3);} void loop(){}"
    assert "wiring_steps" in m._wiring_nudge("COMPILE OK", servo, "", "arduino:avr:uno")
    assert m._wiring_nudge("COMPILE OK", servo, "", "arduino:avr:uno") == ""  # only once
    assert m._wiring_nudge("COMPILE FAILED", wired + " ", "", "arduino:avr:uno") == ""
