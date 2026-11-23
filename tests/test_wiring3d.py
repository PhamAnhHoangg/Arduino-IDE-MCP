import json
import re
from pathlib import Path

import pytest

import wiring3d

ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / "examples" / "face_fan_wiring" / "spec.json").read_text(encoding="utf-8"))


def build(spec=SPEC):
    return wiring3d.build_html(spec["title"], spec["board"], spec["parts"], spec["steps"])


def test_example_builds_one_page_with_every_step_and_wire():
    page = build()
    assert page.startswith("<!doctype html>")
    data = json.loads(re.search(r"const S=(\[.*?\]), PINS=", page).group(1))
    assert len(data) == len(SPEC["steps"])
    wires = sum(len(s.get("wires", [])) for s in SPEC["steps"])
    assert page.count('class="wire"') == wires


def test_no_text_tags_on_wires():
    # user rule: colours only, labels hide the hole a wire goes into
    assert 'class="tag"' not in build()


@pytest.mark.parametrize("end", ["j10", "+top", "-bot:13", "pin:10", "pin:GND", "pin:5V", "usb"])
def test_wire_ends_resolve(end):
    sc = wiring3d.Scene("mega", [])
    x, y, _ = sc.end(end)
    assert 0 < x < 1800 and 0 < y < 1300


def test_dip_pin_picks_a_free_hole_in_its_column():
    sc = wiring3d.Scene("uno", [{"id": "U1", "type": "dip", "pins": 16, "col": 10}])
    first = sc.end("U1.1")[2]
    second = sc.end("U1.1")[2]
    assert first != second and first[1:] == second[1:] == "10"


def test_unknown_ends_raise():
    sc = wiring3d.Scene("mega", [])
    for bad in ("pin:99", "nothing.here", "z5"):
        with pytest.raises(ValueError):
            sc.end(bad)


def test_uno_and_generic_parts_build():
    page = wiring3d.build_html("t", "uno", [
        {"id": "R1", "type": "resistor", "a": "c20", "b": "c24"},
        {"id": "L1", "type": "led", "a": "e24", "k": "e25"},
        {"id": "us", "type": "generic", "terms": ["VCC", "TRIG", "ECHO", "GND"]},
        {"id": "pc", "type": "laptop"},
    ], [{"title": "x", "wires": [["pin:13", "a20"], ["us.VCC", "pin:5V"], ["usb", "pc.USB"]]}])
    assert "ARDUINO UNO" in page
