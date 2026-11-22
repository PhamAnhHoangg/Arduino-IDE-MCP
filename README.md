# arduino-mcp

**Let Claude write, compile, upload, test and debug Arduino code on your real board.**

An [MCP](https://modelcontextprotocol.io) server that wraps the official
[`arduino-cli`](https://arduino.github.io/arduino-cli/). Plug in your Arduino,
add this server to Claude Code (or Claude Desktop / VS Code), and ask:

> Write a sketch that blinks the LED and prints "TEST ok" every second, then
> test it on my board.

Claude will compile it, read the errors, fix them, upload it, read the Serial
output and tell you PASS or FAIL, looping until it works.

It runs on **your own computer**, because it needs the USB port.

---

## What it does

**The test and debug loop**

1. **`check_sketch`** compiles the code with no board needed. It returns each
   error with the file, the line, the code on that line and a plain-English
   hint. It also runs extra checks the compiler doesn't do: a missing
   `Serial.begin`, long blocking `delay()`s, `while(!Serial)` freezing on
   battery, and a missing `controller.begin()` / `controller.ping()` for the
   Arduino Motor Carrier.
2. **`library_api`** fact-checks a library. It reads the real function names
   and parameters from the installed library's `.h` files, so Claude doesn't
   invent functions that don't exist.
3. **`test_on_board`** does compile, upload, open Serial, send test lines,
   read the output and answer **PASS** / **FAIL**, all in one call. You give it what
   must appear (`expect`), what means failure (`fail_on`) and what to send (`send`).

You can paste code straight in (the `code` input), or point at a `.ino` file
or sketch folder. Sketches whose folder name doesn't match the `.ino` name
work too. Re-compiles are faster because each sketch keeps its build folder.

**All tools**

| Tool | What it does |
|---|---|
| `check_sketch` | Compile + clear errors + extra checks. No board needed. |
| `test_on_board` | Compile, upload, Serial test, PASS/FAIL. |
| `library_api` | Real functions of an installed library. |
| `check_setup` | Which arduino-cli is used, and installed boards. Run first if something breaks. |
| `list_connected_boards` / `list_serial_ports` | Plugged-in boards and their ports (`COM5`, `/dev/ttyACM0`...). |
| `search_boards` | Find a board's ID (FQBN), e.g. "mega" gives `arduino:avr:mega`. |
| `install_core` | Install board support, e.g. `arduino:avr` (Uno/Mega/Nano), `arduino:samd` (Nano 33 IoT, MKR). |
| `search_libraries` / `install_library` / `list_installed_libraries` | Library Manager. |
| `compile_sketch` / `upload_sketch` | Plain compile, or compile and upload. |
| `serial_read` / `serial_send` | Watch Serial output, or send a line. |

The default board is the **Arduino Nano 33 IoT** (`arduino:samd:nano_33_iot`).
For any other board, just tell Claude which one ("use an Uno"), or change the
default (see [Settings](#settings)).

---

## Install (3 steps)

You need **Python 3.10+** and **arduino-cli**.

### 1. Install arduino-cli

| System | Command |
|---|---|
| Windows | `winget install ArduinoSA.CLI` |
| macOS | `brew install arduino-cli` |
| Linux | `curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh \| sh` (puts it in `./bin`; move it onto your PATH) |

Already have **Arduino IDE 2** on Windows or macOS? You can skip this step.
The server looks for the copy of arduino-cli that ships inside the IDE.

No Python yet? Get it from [python.org](https://www.python.org/downloads/).
On Windows, tick **"Add python.exe to PATH"** in the installer.

### 2. Download this repo and install the Python packages

Click **Code > Download ZIP** on GitHub and unzip it (or `git clone` it), then
in a terminal inside the folder:

```
python -m pip install -r requirements.txt
```
(On macOS/Linux use `python3` if `python` isn't found.)

### 3. Add it to Claude

**Claude Code** (terminal, and the Claude Code extension for VS Code / JetBrains):

```
# Windows
claude mcp add arduino --scope user -- python "C:\path\to\arduino-mcp\arduino_mcp.py"

# macOS / Linux
claude mcp add arduino --scope user -- python3 /path/to/arduino-mcp/arduino_mcp.py
```
Restart `claude` and type `/mcp`. You should see **arduino** connected.

<details>
<summary><b>Claude Desktop</b></summary>

Settings > Developer > Edit Config, then add:
```json
{
  "mcpServers": {
    "arduino": {
      "command": "python",
      "args": ["C:\\path\\to\\arduino-mcp\\arduino_mcp.py"]
    }
  }
}
```
On Windows, write paths with double backslashes. On macOS/Linux use `python3`
and a normal path. Restart the app.
</details>

<details>
<summary><b>VS Code (Copilot agent mode)</b></summary>

Copy `vscode-mcp.example.json` to `.vscode/mcp.json` in your project, fix the
path inside, and click **Start** above the server entry.
</details>

Finally, install your board's core once. You can just ask Claude
"install the core for my board", or run it yourself:
```
arduino-cli core update-index
arduino-cli core install arduino:avr      # Uno, Mega, classic Nano
arduino-cli core install arduino:samd     # Nano 33 IoT, MKR boards
```

---

## Example prompts

- "List my connected Arduino boards."
- "Check `blink/blink.ino` and fix any errors."
- "Write a sketch that reads A0 and prints the value. Test it on COM5, expecting a number."
- "Run `examples/serial_selftest` on my board, send `ping`, expect `READY` and `pong`."
- "What functions does the ArduinoMotorCarrier library have for servos?"
- "Read the serial output for 10 seconds."

**Tip for testable sketches:** have the sketch print clear markers like
`READY` or `TEST ok`, and print them repeatedly or in reply to a command.
Output in the first second after reset can be missed while USB reconnects.
See [`examples/serial_selftest`](examples/serial_selftest/serial_selftest.ino).

---

## Settings

Optional environment variables. Add them with `-e` in `claude mcp add`, e.g.
`claude mcp add arduino --scope user -e ARDUINO_FQBN=arduino:avr:uno -- python ...`

| Variable | Meaning | Default |
|---|---|---|
| `ARDUINO_CLI` | Full path to `arduino-cli` if it isn't found automatically | PATH, then Arduino IDE 2 |
| `ARDUINO_FQBN` | Default board | `arduino:samd:nano_33_iot` |

---

## Troubleshooting

- **"arduino-cli was not found"**: set `ARDUINO_CLI` to its full path
  (`where arduino-cli` on Windows, `which arduino-cli` on macOS/Linux).
- **Upload: "access denied" / "port busy"**: only one program can use a serial
  port. Close the Arduino IDE Serial Monitor and any script using the port.
- **No board found**: try another USB cable, because some only charge.
- **Linux: permission denied on `/dev/ttyACM0`**: run
  `sudo usermod -aG dialout $USER`, then log out and back in.
- **Native-USB boards (Nano 33 IoT, MKR, Leonardo) won't upload**: double-tap
  reset (the LED pulses) and try again. The port name can change after this.
- **First compile is slow**: that's normal. Later ones are faster.

---

## Limits

- `test_on_board` only sees what the board prints over Serial. It can't see a
  motor turn or an LED light. Make the sketch print what it did (sensor values,
  encoder counts), or check the hardware yourself.
- Claude can make mistakes. Read the code before running it on hardware that
  can move, heat up, or draw a lot of current.

## Status: what is tested

This is an early version.

**Tested** (Linux, Python `mcp` 1.x, through a real MCP client):
- The server starts and all 15 tools load.
- Compile-error parsing, hints and extra checks, using real `g++` compiler errors.
- `library_api` against the real ArduinoMotorCarrier library source.
- `test_on_board`, `serial_send` and `serial_read` against a **simulated** serial board
  (PASS, FAIL, compile failure, send and reply).

**Not tested yet:**
- Real compiles with the Arduino toolchain, and real uploads to a board.
- Windows and macOS.
- Finding the arduino-cli bundled in Arduino IDE 2.

If you try it, please open an issue with what worked or broke.

## License

[MIT](LICENSE)
