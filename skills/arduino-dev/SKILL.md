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
