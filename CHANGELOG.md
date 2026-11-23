# Changelog

## 0.9.4
- Publishing rule in the skill: when Claude commits, pushes or publishes a project
  for you, you are the only author. Claude is credited only as an assist tool.

## 0.9.3
- Wiring steps: USB cable from the board to a laptop (with webcam), and a 9V
  battery into the power module socket. Every project ends with this step.

## 0.9.2
- `ide open` saves the board that is actually plugged in, never a guessed default.

## 0.9.1
- The 3D wiring steps open automatically after the first clean compile of a new
  sketch that uses wired parts.

## 0.9.0
- New tool `wiring_steps`: a 3D step-by-step breadboard wiring page (Back / Next,
  Show all, tilt slider). Colours only, no text on the wires.

## 0.8.0
- Detailed wiring drawing: real Nano header pins and Motor Carrier ports.

## 0.7.x
- Blueprint-style schematic in the parts page, PNG for chat.

## 0.6.x
- `check_sketch guide=True`: parts page with wiring, parts and software
  checklists, where-to-buy search links, similar projects and CAD links.

## 0.5.0
- Fewer, smaller tools (7) to save tokens.

## 0.4.0
- `ide type`: types the code into the Arduino IDE editor so you can watch.

## 0.3.0
- Board and port auto-detection, NEED INPUT / PORT BUSY / MISSING LIBRARY replies.

## 0.2.x
- Opens sketches in Arduino IDE, presses Verify / Upload, log of every step.

## 0.1.0
- First version: compile, upload, Serial test, library fact-check.
