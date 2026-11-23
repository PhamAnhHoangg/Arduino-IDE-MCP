---
name: arduino-dev
description: This skill should be used when the user wants to write, compile, upload, test, debug or fix Arduino code on a connected board, e.g. "upload this to my Arduino", "why won't my sketch compile", "test it on my board", "read the serial monitor".
---

Use the `arduino` tools. Be brief: report results, don't repeat tool output.

1. Unsure of a library function? `library` with `name` + `filter`. Never guess.
2. `check_sketch` until COMPILE OK. One sketch file per task.
3. `test_on_board` with `expect` (and `send`). Leave port/board empty (auto). Sketch prints markers repeatedly. On FAIL fix and retry; stop after 5 tries.
4. Replies starting `NEED INPUT`, `PORT BUSY`, `MISSING LIBRARY`: ask the user that one thing.
5. User wants to watch in Arduino IDE: `ide` type/verify/upload. Never use screenshots/computer use.
6. Serial can't show motors moving: ask the user. Warn before uploading code that drives motors.
7. User asks what parts, wiring, where to buy or CAD: `check_sketch` with guide=True.
8. ALWAYS after writing a new sketch/project that uses wired parts (and when the user is stuck wiring or asks how to wire): call `wiring_steps` without being asked, right after the first COMPILE OK. Small steps (2-6 wires), wire colours that match real wires, notes say where each wire goes. Parts list = electronic components only.
