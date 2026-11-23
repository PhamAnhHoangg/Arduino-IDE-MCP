# Security

This plugin runs on your own computer. It calls `arduino-cli`, opens your
serial ports and, with `ide type`, sends key presses to the Arduino IDE window.
It does not send your code or data anywhere except to the tools you installed
(Arduino CLI and its board/library downloads).

Please read code before uploading it to hardware that can move, heat up or
draw a lot of current.

## Reporting a problem

If you find a security problem (for example a way for a sketch or a tool
argument to run other programs on your PC), please do **not** open a public
issue. Use GitHub's private
[**Report a vulnerability**](https://github.com/PhamAnhHoangg/Arduino-IDE-MCP/security/advisories/new)
form instead. You will get an answer as soon as possible.
