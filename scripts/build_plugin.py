"""Build dist/arduino.plugin (the zip you upload in the Claude app's plugin settings).

    python scripts/build_plugin.py
"""
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["arduino_mcp.py", "wiring3d.py", "requirements.txt", "README.md", "LICENSE",
         ".mcp.json", ".gitignore", "vscode-mcp.example.json"]
DIRS = [".claude-plugin", "skills", "examples"]


def main() -> int:
    version = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    out = ROOT / "dist" / "arduino.plugin"
    out.parent.mkdir(exist_ok=True)
    paths = [ROOT / f for f in FILES]
    for d in DIRS:
        paths += sorted(p for p in (ROOT / d).rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            z.write(p, p.relative_to(ROOT).as_posix())
    print(f"built {out.relative_to(ROOT)} v{version} ({len(paths)} files, {out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
