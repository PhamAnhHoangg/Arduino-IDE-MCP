---
name: arduino-dev
description: This skill should be used when the user wants to write, compile, upload, test, debug or fix Arduino code on a connected board, e.g. "upload this to my Arduino", "why won't my sketch compile", "test it on my board", "read the serial monitor", "which port is my Arduino on".
---

# Arduino development loop

Use the `arduino` MCP tools. Work in this order and keep going until the test passes or a real blocker appears.

1. **Setup (once per session, or when something fails):** call `check_setup`. If no core is installed for the board, call `install_core` (`arduino:samd` for Nano 33 IoT / MKR, `arduino:avr` for Uno / Mega / classic Nano). Find the board and port with `list_connected_boards`. If it lists nothing, try `list_serial_ports` and suggest a data USB cable.
2. **Fact-check before writing library code:** for any library function not certainly known, call `library_api` with the library name (and a `filter` word) and use only the names and parameters it shows. If the library is missing, use `search_libraries` then `install_library`.
3. **Compile with `check_sketch`:** pass `sketch_path`, or `code` for new code. Fix every ERROR it reports, using its line, code and hint, and call it again until it says COMPILE OK. Treat EXTRA CHECKS as likely bugs to fix or mention.
4. **Make the sketch testable:** call `Serial.begin` with the same baud as the test, print a clear marker repeatedly (e.g. `READY` every second), and print results in reply to commands (e.g. reply `pong` to `ping`). Avoid relying on text printed only once right after reset.
5. **Run `test_on_board`:** pass `port`, `expect`, and optionally `fail_on` and `send`. On FAIL, read the serial log, find the cause, fix the code, and run it again. Stop after about 5 failed attempts and explain what is happening instead of looping.
6. **Report honestly:** say what was verified (compile result, Serial PASS) and what was not. Serial output cannot prove a motor turned or an LED lit, so ask the user to confirm physical behaviour.

Safety: before uploading code that drives motors, heaters or high current, tell the user what it will do. If an upload fails with "access denied" or "port busy", ask the user to close the Serial Monitor or any script using the port. For native-USB boards (Nano 33 IoT, MKR, Leonardo), suggest double-tapping reset and listing ports again.
