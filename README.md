# LY Drawbot terminal plotter

Python TUI that fits an SVG onto **A4** (210×297 mm), writes GRBL G-code with servo pen commands, and streams it to an [LY Drawbot](https://www.generativehut.com/post/ly-drawbot-a-70-pen-plotter).

UGS feature audit (what we copied in spirit, what we skipped): [features.md](features.md).

## Hardware notes

- Arduino Uno + CNC V3, **GRBL**, USB **CH340** chip.
- Typical baud: **115200**.
- Pen down: `M3 S1000`. Pen up: `M5`.
- Paper origin is the **bottom-left** of the sheet after you jog there and press **Set home**. The kit usually has no homing switches.

If the serial port does not appear, install CH340 drivers for your OS, then unplug/replug the USB cable.

## Setup, install, run

```bash
chmod +x setup.sh
./setup.sh          # create .venv and install deps
./setup.sh run      # start the TUI
./setup.sh test     # unit tests (no plotter required)
```

Or:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m lyplotter
```

macOS/Linux may need your user in the `dialout`/`uucp` group (Linux) to open `/dev/ttyUSB*`.

## Workflow

1. Tape A4 paper. Place the machine so the pen can reach the sheet.
2. Start the app. Status shows **DISCONNECTED** until you press **o**.
3. Jog with arrows (Shift = 10 mm) to the bottom-left corner of the page.
4. Press **h** (set home). That point is work `X0 Y0`. Coordinates are saved in SQLite (`~/.lyplotter/plotter.db`) if the app dies mid-plot.
5. Type an SVG path in the second field, press **Enter**. Artwork larger than A4 is **scaled down** to fit inside a 5 mm margin. Smaller artwork keeps its size and is placed at the **work origin** (not centered on the page).
6. Check the visualizer (page frame, strokes `·`, pen `@`).
7. Press **p** to plot. **Space** pauses, **a** aborts (feed hold + reset + pen up). **r** resumes from the last acknowledged line.

Shapes in the SVG are converted when `svgelements` can turn them into paths. Convert text to outlines and prefer real paths; bitmaps are ignored.

## Keys

| Key | Action |
| --- | --- |
| o | Connect |
| d | Disconnect |
| arrows / Shift+arrows | Jog 1 mm / 10 mm |
| h | Set home (G92 zero) |
| z | Pen up, go to X0 Y0 |
| u / n | Pen up / pen down |
| Enter | Convert SVG in the path field |
| ctrl+b | Browse for an SVG file |
| t | Test 20 mm square at origin (generate + plot if connected) |
| p | Plot from start |
| r | Resume unfinished job |
| space | Pause / continue stream |
| a | Abort |
| q | Quit |

## Data

- Database: `~/.lyplotter/plotter.db` (settings, drawings, progress).
- Generated G-code: `~/.lyplotter/jobs/`.
- Log file: `~/.lyplotter/lyplotter.log` (rotates at 1 MB).
