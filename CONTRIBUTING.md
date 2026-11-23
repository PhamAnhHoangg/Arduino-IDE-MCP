# Contributing

Thanks for helping! Bug reports from real boards are the most useful thing,
because most of this plugin was written on a machine without one.

## Report a bug

Open an issue with:
- your OS, board (for example Mega 2560 on COM7) and `arduino-cli version`
- what you asked Claude, and what the tool replied (the `[did: ...]` line helps)
- the log file `Documents/arduino-mcp-sketches/arduino-mcp.log` if you can

## Change the code

```
pip install -r requirements.txt pytest
python -m pytest -q
python scripts/build_plugin.py      # rebuilds dist/arduino.plugin
```

- Keep tool descriptions short: every word costs tokens in every chat.
- Wiring pictures: colours only, no text labels on wires (they hide the hole).
- Bump the version in `.claude-plugin/plugin.json` and add a line to
  `CHANGELOG.md`.
- To publish a release, just bump the version. When it reaches `main`, GitHub
  Actions runs the tests and publishes a Release with `arduino.plugin` attached.
