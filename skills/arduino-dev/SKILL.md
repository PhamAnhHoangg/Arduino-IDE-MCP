---
name: arduino-dev
description: This skill should be used when the user wants to write, compile, upload, test, debug or fix Arduino code on a connected board, e.g. "upload this to my Arduino", "why won't my sketch compile", "test it on my board", "read the serial monitor".
---

Use the `arduino` tools. Be brief: don't repeat tool output back, report only the result.

1. Unsure of a library function? `library_api` with a `filter` word. Never guess names.
2. `check_sketch` (pass `code` or `sketch_path`). Fix every ERROR, repeat until COMPILE OK. Keep one sketch file per task so the user's Arduino IDE window shows each change.
3. Port unknown? `list_connected_boards` once. If nothing compiles at all, `check_setup` / `install_core`.
4. `test_on_board` with `expect` (and `send` if the sketch reads commands). Make the sketch print markers repeatedly (e.g. `READY` every second). On FAIL, fix and retry; stop after 5 tries and explain.
5. If the user wants to see compile/upload in the IDE itself: `ide_button` (Windows), then confirm with `check_sketch` or `serial`.
6. Serial can't show motors moving: ask the user to confirm physical behaviour. Warn before uploading code that drives motors.
