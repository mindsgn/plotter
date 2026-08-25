# Universal G-Code Sender — feature audit

**Repository:** [winder/Universal-G-Code-Sender](https://github.com/winder/Universal-G-Code-Sender)  
**Site:** [universalgcodesender.com](https://universalgcodesender.com/)  
**Wiki:** [Features](https://github.com/winder/Universal-G-Code-Sender/wiki/Features), [Usage](https://github.com/winder/Universal-G-Code-Sender/wiki/Usage)  
**Latest release reviewed:** v2.1.25 / v2.1.26 (Platform)  
**License:** GPL (do not copy UGS Java into this project)

This document lists UGS capabilities for the LY Drawbot terminal app. The last section maps each area to **implemented**, **partial**, or **out of scope**.

---

## 1. Product overview

Universal G-Code Sender (UGS) is a Java desktop application for driving CNC controllers. It is **not** a CAM package first; it is a **sender**: open G-code, visualize the toolpath, connect over serial (or TCP for some firmwares), stream commands, and show machine state.

There are two UIs that share `ugs-core`:

| Product | What it is |
| --- | --- |
| **UGS Classic** | Older Swing window (`com.willwinder.universalgcodesender.MainWindow`). Still buildable; used as a library. |
| **UGS Platform** | Current product. NetBeans Platform modules, dockable windows, plugins, bundled JRE. Launch `bin/ugsplatform`. |

Technical stack (from the project README):

- Serial: JSSC or JSerialComm
- 3D visualizer: JogAmp / OpenGL
- Geometry: JTS
- SVG import (designer): Apache Batik
- UI framework: NetBeans Platform + Swing

---

## 2. Architecture

### 2.1 Core (`ugs-core`)

- **Communication:** serial (and TCP for some controllers). Translates G-code lines to firmware protocol and parses replies.
- **G-code parser / streamer:** splits files into commands, manages send order and buffer.
- **Machine state (DRO data):** work/machine position, feed, spindle, controller state (`Idle`, `Run`, `Jog`, `Hold`, `Alarm`, …).
- **Toolpath visualization logic:** interpret G-code into a displayable path (not CAM generation).
- **Settings:** ports, baud, firmware type, sender options, keybindings, processor chain.

### 2.2 Platform (`ugs-platform`)

Each window is a NetBeans `TopComponent` / plugin. Modules listed in `ugs-platform/pom.xml`:

| Module | Role |
| --- | --- |
| `branding` / `application` | App identity, launcher, bundled Java |
| `ugs-platform-ugslib` | Shared platform library |
| `ugs-platform-ugscore` | Glue to `ugs-core`, CentralLookup, actions |
| `ugs-platform-visualizer` | 3D G-code view |
| `ugs-platform-gcode-editor` | Edit G-code with highlighting |
| `ugs-platform-plugin-designer` | Simple CAD/CAM |
| `ugs-platform-plugin-jog` | Jog pad |
| `ugs-platform-plugin-dro` | Controller state / DRO |
| `ugs-platform-plugin-console` | Serial console |
| `ugs-platform-plugin-toolbox` | Customizable action buttons |
| `ugs-platform-plugin-filebrowser` | Browse/load G-code |
| `ugs-platform-plugin-workflow` | Multi-file workflow |
| `ugs-platform-plugin-setup-wizard` | First-run / machine setup |
| `ugs-platform-plugin-joystick` | Gamepad / joystick (SDL) |
| `ugs-platform-plugin-cloud-storage` | Remote file storage |
| `ugs-platform-plugin-interceptor` | Command interceptor UI |
| `ugs-platform-welcome-page` | Welcome / getting started |
| `ProbeModule` | Probe routines |
| `GcodeTools` | Extra generators (e.g. dowel maker) |
| `ugs-platform-surfacescanner` / `Surfacer` | Surface scan and auto-level |

Services used across plugins: `CentralLookup` (controller, settings, file), `ActionRegistrationService`, `MacroService`, `JogActionService`.

---

## 3. Firmware and connection

### 3.1 Supported controllers

- GRBL (including 0.9 / 1.1; baud 9600 vs **115200**)
- FluidNC
- Smoothieware
- TinyG
- G2core

LY Drawbot uses **GRBL on Arduino Uno** (CNC V3 shield). Typical port names:

- macOS: `/dev/tty.usbserial*` or `/dev/cu.wchusbserial*` (CH340)
- Linux: `/dev/ttyUSB*` / `/dev/ttyACM*`
- Windows: `COMn`

CH340 USB-serial drivers are required on many Macs/Windows PCs.

### 3.2 Connecting (Platform)

Toolbar: firmware combo, serial port, baud, connect. Older versions required a manual port refresh.

### 3.3 GRBL streaming model (core behavior)

UGS does **not** dump the whole file at once. For GRBL it uses **character-counting** against the planner buffer (typically **128 bytes**):

1. Send a line ending with `\n` if it still fits in the remaining buffer.
2. Count those bytes as in-flight.
3. On `ok` or `error:N`, subtract that line’s length and send more.
4. Status reports: send `?` periodically; parse `<Idle|MPos:…|WPos:…|FS:…>` (format depends on GRBL `$10`).
5. Realtime bytes (not G-code lines): `!` feed hold, `~` cycle start/resume, `Ctrl-X` (`0x18`) soft reset, feed/spindle override bytes (GRBL 1.1).

This protocol is what a LY Drawbot sender must implement. UGS also supports other streaming strategies per firmware (TinyG JSON, Smoothie, etc.).

---

## 4. Machine actions

Enabled/disabled from controller state (disabled while running, limited in Alarm).

| Action | Behavior |
| --- | --- |
| **Reset zero** | Set current work position to X0 Y0 Z0 (`G10 L20` / equivalent). |
| **Return to zero** | Move to work origin; if Z is below a configured safe height, raise Z first. |
| **Soft reset** | `0x18` — reset controller without power cycle. |
| **Unlock** | Clear some alarm states (`$X` on GRBL). |
| **Home** | Homing cycle (`$H`) using limit switches. |
| **Outline** | Jog XY around the loaded model bounding box at current Z (fixture check). |

LY Drawbot kits often **lack usable homing switches**. Soft home = jog to paper origin, then reset work zero. Z “safe height” does not apply; pen is a **servo** via spindle PWM (`M3`/`M5`).

---

## 5. Program / file actions

- Open / close G-code
- Send / pause / cancel a job
- **Outline** the loaded model
- Progress: current line, estimated remaining time (when implemented)

---

## 6. Visualizer (3D)

- Show loaded G-code vs machine
- Rotate (left drag), pan (Shift + left drag), zoom (wheel)
- Real-time tool marker (cone) in work coordinates
- Color by motion: G0 / G1 / G2 / G3
- Customizable colors
- Outline overlay
- Up to **six axes** (XYZABC)
- Right-click: **jog to XY**, **set work position**
- Metric/imperial display toggle for the whole app

---

## 7. Digital read-out (DRO)

Module: `ugs-platform-plugin-dro` (`MachineStatusPanel`).

- Connection implied by live state vs disconnected
- Work coordinates; optional machine coordinates
- Per-axis zero buttons
- Work coordinate math (`# / 2`, leading `*` `/`)
- State: Idle, Run, Jog, Alarm, Hold, …
- Feed and spindle
- Modal G-code (G20/G21, etc.)
- Alarm / which limit switch

---

## 8. Jog controller

Module: `ugs-platform-plugin-jog`.

- XY (and Z) step buttons
- Step size and feed
- Keyboard jogging
- Continuous / analog jog when joystick is enabled
- GRBL 1.1 `$J=` jogging where supported

---

## 9. Overrides

Window: **Window → Overrides**.

- Feed rate override while running
- Rapid override
- Spindle override  
Uses GRBL 1.1 realtime override bytes when the firmware supports them.

---

## 10. Serial console

Live TX/RX log. Manual command box (e.g. `M3 S1000`, `M5`, `$$` settings). Essential for LY Drawbot pen tests.

---

## 11. Toolbox, toolbar, keybindings

- Toolbox: user-picked actions; greyed out when invalid
- Toolbar: right-click customize, extra toolbars
- Actions assignable to shortcuts
- **Window → Reset windows** restores layout
- Editor container cannot be removed (G-code / designer / welcome)

---

## 12. G-code editor

Module: `ugs-platform-gcode-editor`.

- Syntax highlighting
- Selected lines highlighted in the visualizer
- Follow currently sent line
- **Run from selected line** (rebuild remaining path)
- Error / unsupported-command highlighting when connected
- Editor actions: **mirror**, **rotate ±90°**, **translate to zero**, **insert current position**
- Arcs become line segments after those transforms
- Option: do not auto-show editor on open

---

## 13. Configurable G-code processors (optimization)

Chainable processors before send:

- Remove comments
- Truncate decimal precision
- Convert arcs **G2/G3** to line segments
- Remove whitespace
- Line splitter
- **MeshLeveler** (auto-level): warp Z from a probed mesh
- Command interceptor plugin for custom rewrite/filter/append

---

## 14. Designer (CAD/CAM)

Module: `ugs-platform-plugin-designer`. Intended as a quick path to G-code, not a full CAM suite.

- Import **SVG**, **DXF**, **C2D** (Carbide Create)
- Draw shapes and text
- Toolpath ops: pocket, outline, inside, on-line, with depth
- Move, scale, rotate, mirror
- Boolean: intersect, union, subtract
- Undo/redo
- Numeric position/size/rotation
- Grid multiply
- Clipart library
- Bitmap trace
- Laser-oriented workflows on the website (“laser engraver support”)

---

## 15. Probe module

Workpiece / tool probing: probe service, parameter panels, path preview. Used to set work coordinates and measure stock. **Not relevant** to a servo pen plotter without a probe.

---

## 16. Surface scanner / auto-level (Surfacer)

Grid probe a surface, build a mesh, MeshLeveler adjusts Z of milling paths. **Not relevant** to 2D pen plotting.

---

## 17. GcodeTools (e.g. dowel maker)

Generators for specific shop G-code (dowels, etc.). CNC mill oriented.

---

## 18. Setup wizard

Guided firmware/machine setup (steps, motors, limits). Useful for mills; LY Drawbot is usually already flashed with seller GRBL.

---

## 19. Workflow plugin

Queue / step through multiple G-code files (multi-tool or multi-op jobs).

---

## 20. File browser

Browse a folder of G-code and load files without a system open dialog.

---

## 21. Macros

- Named G-code snippets
- Substitutions: `{machine_x}`, `{work_z}`, …
- Prompts: `{prompt|name}`
- Machine → Macros menu, settings panel, JSON import/export
- Registered as actions (shortcuts)

Typical LY Drawbot macros: pen down `M3 S1000`, pen up `M5`.

---

## 22. Gamepad / joystick

SDL-backed devices. Custom button → action map. Analog jog with deadzone and reverse. Default layout maps D-pad to XY, sticks to analog jog, A/Y to Z, Start/Back to start/stop. macOS often needs a 360-controller driver.

---

## 23. Web pendant

Optional HTTP UI (default `http://localhost:8080/`). Jog, start/stop, file list from a workspace folder. Off by default; can auto-start.

---

## 24. Cloud storage

Plugin to open/save jobs from cloud backends (not required for local plotting).

---

## 25. Welcome page

Onboarding, links, empty editor state.

---

## 26. i18n and packaging

- Crowdin translations
- Builds: Windows x64, macOS x64 and ARM64, Linux x64/ARM/ARM64, all-platforms zip
- Nightly snapshots
- Bundled Temurin 17 JRE per OS
- Discord / GitHub Discussions

---

## 27. UGS Classic (subset)

Classic exposes connect, send file, basic visualizer, console, and jog without the NetBeans dock/plugin system. Platform is the documented feature-complete product.

---

## 28. What LY Drawbot users actually use in UGS

From [Generative Hut](https://www.generativehut.com/post/ly-drawbot-a-70-pen-plotter) and [LY Drawbot Tool by LOD](https://github.com/love-open-design/LY-Drawbot-Tool-by-LOD):

1. Install CH340 drivers.
2. Open UGS, connect GRBL @ 115200.
3. Jog to the paper corner; treat that as origin.
4. Test pen: `M5` up, `M3 S1000` down.
5. Generate G-code in Inkscape (J-Tech Laser Tool or LOD XY Plotter tool).
6. Load file in UGS, visualize, send.

LOD defaults: pen-up travel **F3000**, pen-down **F750–1500**, dwells **G4 P0.1 / P0.2**, header `G90`, footer `G1 X0 Y0`. J-Tech 2.x uses `svg_to_gcode` and only converts **paths** (Object to Path first).

---

## 29. Mapping: UGS → this terminal app (`lyplotter`)

| UGS feature | This app |
| --- | --- |
| Cross-platform Java UI | Python 3 Textual TUI |
| GRBL serial connect / disconnect | **Implemented** — status **CONNECTED / DISCONNECTED** |
| Firmware picker (TinyG, Smoothie, …) | Out of scope — GRBL only |
| Baud / port list | **Implemented** |
| DRO (WPos, state, feed) | **Partial** — XY, state, pen, progress |
| Jog pad / keyboard jog | **Implemented** — XY steps |
| Set work zero / return to zero | **Implemented** — Set Home + saved SQLite origin |
| `$H` homing | Out of scope (typical kit has no switches) |
| Soft reset / unlock / feed hold | **Implemented** — abort uses `!` + `0x18` + pen up |
| Character-count G-code stream | **Implemented** |
| Pause / resume / run from line | **Implemented** — SQLite `last_ok_line` |
| 3D OpenGL visualizer | **Partial** — 2D A4 terminal visualizer |
| Color by G0/G1 | **Partial** — travel vs draw strokes |
| G-code editor / mirror / rotate | Out of scope |
| Designer, SVG/DXF CAM | **Partial** — SVG → A4-centered G-code only |
| Arc expander / processors | **Partial** — curves sampled to lines at convert time |
| Probe / auto-level / dowel / surfacer | Out of scope |
| Joystick, pendant, cloud, workflow | Out of scope |
| Macros | **Partial** — built-in pen up/down, not user macro editor |
| Overrides | Out of scope |
| Setup wizard | **Partial** — `setup.sh` + README (CH340) |
| Job persistence | **Implemented** — SQLite drawings + home + progress |

UGS remains the right tool if you need mill probing, 3D visualization, or non-GRBL firmware. This app is a **small GRBL pen-plotter sender + SVG layout**, not a UGS clone.
