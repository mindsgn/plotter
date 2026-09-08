"""Textual TUI: connect, convert SVG, visualize A4, and stream to the Drawbot."""

from __future__ import annotations

import threading
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Button, DirectoryTree, Footer, Header, Input, Log, Static
from textual.widget import Widget

from lyplotter.gcode import GcodeSettings, svg_to_gcode, write_gcode
from lyplotter.grbl import (
    DEFAULT_BAUD,
    GrblClient,
    MachineStatus,
    StreamEvent,
    gcode_lines,
)
from lyplotter.log import get_logger
from lyplotter.store import Drawing, Store
from lyplotter.svg_layout import A4_HEIGHT_MM, A4_WIDTH_MM, LayoutResult, Polyline

_log = get_logger("lyplotter.tui")


class A4Visualizer(Widget):
    """Top-down A4 preview with draw strokes and a live pen marker."""

    polylines: reactive[list[Polyline]] = reactive(list)
    pen_x: reactive[float] = reactive(0.0)
    pen_y: reactive[float] = reactive(0.0)
    done_line: reactive[int] = reactive(-1)

    def render(self) -> Text:
        """Draw the page, paths, and pen into a character grid.

        Returns:
            A Rich ``Text`` filling the widget.
        """
        width = max(self.size.width, 8)
        height = max(self.size.height, 8)
        grid = [[" " for _ in range(width)] for _ in range(height)]

        def plot(x_mm: float, y_mm: float, ch: str) -> None:
            col = int(round((x_mm / A4_WIDTH_MM) * (width - 1)))
            row = int(round((1.0 - y_mm / A4_HEIGHT_MM) * (height - 1)))
            if 0 <= row < height and 0 <= col < width:
                grid[row][col] = ch

        for c in range(width):
            grid[0][c] = "-"
            grid[height - 1][c] = "-"
        for r in range(height):
            grid[r][0] = "|"
            grid[r][width - 1] = "|"
        grid[0][0] = "+"
        grid[0][width - 1] = "+"
        grid[height - 1][0] = "+"
        grid[height - 1][width - 1] = "+"

        for stroke in self.polylines:
            for x, y in stroke:
                plot(x, y, "·")
        plot(self.pen_x, self.pen_y, "@")

        out = Text()
        for i, row in enumerate(grid):
            style = "green" if i == 0 or i == height - 1 else ""
            out.append("".join(row), style=style or None)
            if i < height - 1:
                out.append("\n")
        return out


class StatusBar(Static):
    """Connection, coordinates, and job progress."""

    def update_from(
        self,
        status: MachineStatus,
        port: str,
        pen_down: bool,
        progress: str,
    ) -> None:
        """Refresh the status text.

        Args:
            status: GRBL snapshot.
            port: Port name when connected.
            pen_down: App-tracked pen state.
            progress: Job progress label.
        """
        link = "CONNECTED" if status.connected else "DISCONNECTED"
        pen = "DOWN" if pen_down else "UP"
        port_s = port or "-"
        self.update(
            f"{link}  port={port_s}  baud={DEFAULT_BAUD}  "
            f"state={status.state}  X={status.work_x:.2f} Y={status.work_y:.2f}  "
            f"pen={pen}  {progress}"
        )


class SVGPickerScreen(ModalScreen[str]):
    """Full-screen modal to browse and select an SVG file."""

    CSS = """
    SVGPickerScreen > Vertical {
        width: 100%;
        height: 100%;
    }
    #svg-picker-label {
        height: 1;
        padding: 0 1;
        background: $accent;
        color: $text;
    }
    #svg-tree {
        height: 1fr;
    }
    """
    BINDINGS = [
        Binding("escape", "cancel", show=False),
    ]

    def __init__(self, start: str | None = None) -> None:
        super().__init__()
        self._start = start

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                "Browse for SVG  |  Enter: select  |  Esc: cancel",
                id="svg-picker-label",
            )
            yield DirectoryTree(self._start or str(Path.home()), id="svg-tree")

    def on_mount(self) -> None:
        self.query_one("#svg-tree", DirectoryTree).focus()

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        path = Path(str(event.path))
        if path.suffix.lower() == ".svg":
            self.dismiss(str(path))

    def action_cancel(self) -> None:
        self.dismiss(None)


class PlotterApp(App):
    """LY Drawbot terminal plotter."""

    TITLE = "LY Drawbot"
    CSS = """
    Screen {
        layout: vertical;
    }
    #status {
        height: 3;
        border: solid $accent;
        padding: 0 1;
    }
    #viz {
        height: 1fr;
        border: solid $primary;
    }
    #path-row {
        height: 3;
    }
    #browse-btn {
        width: auto;
        min-width: 10;
    }
    #log {
        height: 10;
        border: solid $surface;
    }
    """
    BINDINGS = [
        Binding("o", "connect", "Connect"),
        Binding("d", "disconnect", "Disconnect"),
        Binding("h", "set_home", "Set home"),
        Binding("z", "return_zero", "Go XY0"),
        Binding("u", "pen_up", "Pen up"),
        Binding("n", "pen_down", "Pen down"),
        Binding("p", "plot", "Plot"),
        Binding("r", "resume", "Resume"),
        Binding("space", "pause", "Pause/Resume stream"),
        Binding("a", "abort", "Abort"),
        Binding("left", "jog(-1,0)", "Jog -X", show=False),
        Binding("right", "jog(1,0)", "Jog +X", show=False),
        Binding("down", "jog(0,-1)", "Jog -Y", show=False),
        Binding("up", "jog(0,1)", "Jog +Y", show=False),
        Binding("shift+left", "jog(-10,0)", "Jog -X 10", show=False),
        Binding("shift+right", "jog(10,0)", "Jog +X 10", show=False),
        Binding("shift+down", "jog(0,-10)", "Jog -Y 10", show=False),
        Binding("shift+up", "jog(0,10)", "Jog +Y 10", show=False),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+b", "browse", "Browse SVG", show=True),
    ]

    def __init__(self, store: Store | None = None, client: GrblClient | None = None) -> None:
        """Build the app with optional injected store and serial client.

        Args:
            store: Persistence; default ``~/.lyplotter/plotter.db``.
            client: GRBL client; default real serial.
        """
        super().__init__()
        self.store = store or Store()
        self.client = client or GrblClient()
        self.layout: LayoutResult | None = None
        self.current_drawing: Drawing | None = None
        self.gcode_text = ""
        self.pen_down = False
        self.paused = False
        self._stream_thread: threading.Thread | None = None
        self.jobs_dir = Path.home() / ".lyplotter" / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def compose(self) -> ComposeResult:
        """Create child widgets.

        Returns:
            Compose yield of the screen layout.
        """
        yield Header()
        yield StatusBar(id="status")
        yield A4Visualizer(id="viz")
        with Horizontal(id="path-row"):
            yield Input(placeholder="Serial port (empty = first port)", id="port")
            yield Input(placeholder="Path to SVG, then Enter to convert", id="svg")
            yield Button("Browse", id="browse-btn")
        yield Log(id="log", highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        """Load settings, ports, and unfinished jobs."""
        _log.info("App started")
        settings = self.store.get_settings()
        port_input = self.query_one("#port", Input)
        if settings.port:
            port_input.value = settings.port
        self._log("Arrows jog 1 mm, Shift+arrows 10 mm. Enter converts the SVG path.")
        ports = self.client.list_ports()
        if ports:
            self._log("Ports: " + ", ".join(ports))
        unfinished = self.store.latest_unfinished()
        if unfinished:
            self.current_drawing = unfinished
            gpath = Path(unfinished.gcode_path)
            if gpath.is_file():
                self.gcode_text = gpath.read_text(encoding="utf-8")
            self._log(
                f"Unfinished drawing #{unfinished.id} ({unfinished.status}). Press r to resume."
            )
        self.set_interval(0.2, self._tick)
        self._refresh_status()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Convert when Enter is pressed in the SVG field; connect from the port field.

        Args:
            event: Input submission event.
        """
        if event.input.id == "svg":
            self.action_convert()
        elif event.input.id == "port":
            self.action_connect()

    def on_unmount(self) -> None:
        """Disconnect serial and close SQLite when the TUI exits."""
        _log.info("App shutting down")
        try:
            self.client.disconnect()
        except Exception:
            pass
        self.store.close()

    def _log(self, message: str) -> None:
        """Append a line to the log widget and the file logger.

        Args:
            message: Text to show.
        """
        _log.info(message)
        self.query_one("#log", Log).write_line(message)

    def _viz(self) -> A4Visualizer:
        """Return the A4 widget.

        Returns:
            Visualizer instance.
        """
        return self.query_one("#viz", A4Visualizer)

    def _refresh_status(self, extra: str = "") -> None:
        """Push DRO-like fields into the status bar.

        Args:
            extra: Optional progress suffix.
        """
        progress = extra
        if self.current_drawing:
            progress = progress or f"job=#{self.current_drawing.id} {self.current_drawing.status}"
        self.query_one("#status", StatusBar).update_from(
            self.client.status,
            self.client.port_name,
            self.pen_down,
            progress or "no job",
        )
        viz = self._viz()
        viz.pen_x = self.client.status.work_x
        viz.pen_y = self.client.status.work_y

    def _tick(self) -> None:
        """Poll GRBL status while connected."""
        if self.client.is_connected():
            try:
                self.client.poll_status()
            except Exception as exc:
                _log.warning("Status poll failed: %s", exc)
                self.client.status.connected = False
                self.client.status.state = "Disconnected"
        self._refresh_status()

    def _require_idle_connection(self) -> bool:
        """Guard actions that need an idle connected machine.

        Returns:
            True if it is safe to send motion commands.
        """
        if not self.client.is_connected():
            self._log("Plotter is DISCONNECTED.")
            return False
        return True

    def action_connect(self) -> None:
        """Open serial using the port field or the first listed device."""
        if self.client.is_connected():
            self._log("Already connected.")
            return
        typed = self.query_one("#port", Input).value.strip()
        ports = self.client.list_ports()
        port = typed or (ports[0] if ports else "")
        if not port:
            self._log("No serial port. Plug in the Drawbot (CH340) and try again.")
            return
        try:
            self.client.connect(port, DEFAULT_BAUD)
        except Exception as exc:
            self._log(f"Connect failed: {exc}")
            return
        settings = self.store.get_settings()
        settings.port = port
        settings.baud = DEFAULT_BAUD
        self.store.save_settings(settings)
        self._log(f"CONNECTED on {port}")
        self._refresh_status()

    def action_disconnect(self) -> None:
        """Close serial."""
        self.client.disconnect()
        self._log("DISCONNECTED")
        self._refresh_status()

    def action_jog(self, dx: float, dy: float) -> None:
        """Jog in work millimetres.

        Args:
            dx: X step.
            dy: Y step.
        """
        if not self._require_idle_connection():
            return
        dx_f, dy_f = float(dx), float(dy)
        try:
            self.client.jog(dx_f, dy_f)
            self._log(f"Jog X{dx_f:+} Y{dy_f:+}")
        except Exception as exc:
            self._log(f"Jog failed: {exc}")

    def action_set_home(self) -> None:
        """Zero work coordinates at the current pen position and save them."""
        if not self._require_idle_connection():
            return
        try:
            self.client.set_work_zero()
        except Exception as exc:
            self._log(f"Set home failed: {exc}")
            return
        self.store.save_home(0.0, 0.0)
        self._log("Home set: current position is work X0 Y0 (saved).")

    def action_return_zero(self) -> None:
        """Pen up and rapid to work origin."""
        if not self._require_idle_connection():
            return
        try:
            self.client.return_to_zero()
            self.pen_down = False
            self._log("Returning to X0 Y0.")
        except Exception as exc:
            self._log(f"Return failed: {exc}")

    def action_pen_up(self) -> None:
        """Raise the pen."""
        if not self._require_idle_connection():
            return
        settings = self.store.get_settings()
        try:
            self.client.pen_up(settings.pen_up)
            self.pen_down = False
            self._log("Pen up (M5).")
        except Exception as exc:
            self._log(f"Pen up failed: {exc}")

    def action_pen_down(self) -> None:
        """Lower the pen."""
        if not self._require_idle_connection():
            return
        settings = self.store.get_settings()
        try:
            self.client.pen_down(settings.pen_down)
            self.pen_down = True
            self._log("Pen down (M3 S1000).")
        except Exception as ext:
            self._log(f"Pen down failed: {ext}")

    def action_convert(self) -> None:
        """Fit the SVG on A4, write G-code, and show it in the visualizer."""
        svg = self.query_one("#svg", Input).value.strip()
        if not svg:
            self._log("Type an SVG path in the second field, then press Enter.")
            return
        path = Path(svg).expanduser()
        settings = self.store.get_settings()
        gset = GcodeSettings(
            pen_down=settings.pen_down,
            pen_up=settings.pen_up,
            travel_feed=settings.travel_feed,
            draw_feed=settings.draw_feed,
        )
        try:
            gcode, layout = svg_to_gcode(path, gset)
        except Exception as exc:
            self._log(f"Convert failed: {exc}")
            return
        dest = self.jobs_dir / f"{path.stem}.gcode"
        write_gcode(gcode, dest)
        drawing = self.store.add_drawing(str(path.resolve()), str(dest))
        self.current_drawing = drawing
        self.gcode_text = gcode
        self.layout = layout
        viz = self._viz()
        viz.polylines = layout.polylines
        self._log(
            f"Converted {path.name} → {dest}  "
            f"bbox {layout.bbox_min[0]:.1f},{layout.bbox_min[1]:.1f} "
            f"→ {layout.bbox_max[0]:.1f},{layout.bbox_max[1]:.1f} mm (centered on A4)."
        )
        self._refresh_status()

    def _on_stream_progress(self, event: StreamEvent) -> None:
        """Persist progress from the background streamer.

        Args:
            event: Line acknowledgement.
        """
        drawing = self.current_drawing
        if drawing is None:
            return
        self.store.save_progress(
            drawing.id, event.line_index, event.last_x, event.last_y, event.pen_down
        )
        self.pen_down = event.pen_down
        self.client.status.work_x = event.last_x
        self.client.status.work_y = event.last_y

        def ui() -> None:
            viz = self._viz()
            viz.pen_x = event.last_x
            viz.pen_y = event.last_y
            viz.done_line = event.line_index
            self._refresh_status(
                f"job=#{drawing.id} line {event.line_index + 1}/{event.total_lines}"
            )

        self.call_from_thread(ui)

    def _run_stream(self, start_index: int, start_x: float, start_y: float, start_pen: bool) -> None:
        """Background worker that streams G-code.

        Args:
            start_index: First line index.
            start_x: Resume X.
            start_y: Resume Y.
            start_pen: Resume pen state.
        """
        drawing = self.current_drawing
        settings = self.store.get_settings()
        try:
            result = self.client.stream(
                gcode_lines(self.gcode_text),
                start_index=start_index,
                on_progress=self._on_stream_progress,
                pen_down_cmd=settings.pen_down,
                pen_up_cmd=settings.pen_up,
                start_x=start_x,
                start_y=start_y,
                start_pen_down=start_pen,
            )
            if drawing:
                if result.error == "aborted":
                    self.store.set_status(drawing.id, "paused")
                    drawing.status = "paused"
                    msg = "Stream aborted (paused in database)."
                elif result.error:
                    self.store.set_status(drawing.id, "failed")
                    drawing.status = "failed"
                    msg = f"Stream error: {result.error}"
                else:
                    self.store.set_status(drawing.id, "done")
                    drawing.status = "done"
                    msg = "Plot finished."
                self.call_from_thread(lambda: self._log(msg))
        except Exception as exc:
            if drawing:
                self.store.set_status(drawing.id, "failed")
                drawing.status = "failed"
            self.call_from_thread(lambda: self._log(f"Stream failed: {exc}"))

    def action_plot(self) -> None:
        """Send the current G-code from the start."""
        if not self._require_idle_connection():
            return
        if not self.gcode_text or self.current_drawing is None:
            self._log("Convert an SVG first.")
            return
        self.store.set_status(self.current_drawing.id, "running")
        self.current_drawing.status = "running"
        self.paused = False
        self._stream_thread = threading.Thread(
            target=self._run_stream,
            args=(0, 0.0, 0.0, False),
            daemon=True,
        )
        self._stream_thread.start()
        self._log("Plotting…")

    def action_resume(self) -> None:
        """Continue an unfinished job after reconnect."""
        if not self._require_idle_connection():
            return
        drawing = self.current_drawing or self.store.latest_unfinished()
        if drawing is None:
            self._log("Nothing to resume.")
            return
        self.current_drawing = drawing
        self.gcode_text = Path(drawing.gcode_path).read_text(encoding="utf-8")
        progress = self.store.get_progress(drawing.id)
        start = 0 if progress is None else progress.last_ok_line + 1
        x = 0.0 if progress is None else progress.last_x
        y = 0.0 if progress is None else progress.last_y
        pen = False if progress is None else progress.pen_down
        settings = self.store.get_settings()
        try:
            self.client.pen_up(settings.pen_up)
            self.client.send_line("G90")
            self.client.send_line(f"G0 X{x:.3f} Y{y:.3f}")
        except Exception as exc:
            self._log(f"Resume positioning failed: {exc}")
            return
        self.store.set_status(drawing.id, "running")
        drawing.status = "running"
        self.paused = False
        self._stream_thread = threading.Thread(
            target=self._run_stream,
            args=(start, x, y, pen),
            daemon=True,
        )
        self._stream_thread.start()
        self._log(f"Resuming from line {start}.")

    def action_pause(self) -> None:
        """Toggle feed hold / cycle start during a stream."""
        if not self.client.is_connected():
            return
        if not self.paused:
            self.client.pause_stream()
            self.paused = True
            if self.current_drawing:
                self.store.set_status(self.current_drawing.id, "paused")
                self.current_drawing.status = "paused"
            self._log("Paused (feed hold). Space to resume motion.")
        else:
            self.client.resume_stream()
            self.paused = False
            if self.current_drawing:
                self.store.set_status(self.current_drawing.id, "running")
                self.current_drawing.status = "running"
            self._log("Stream resumed.")

    def action_abort(self) -> None:
        """Feed hold, reset GRBL, and lift the pen."""
        if not self.client.is_connected():
            return
        self.client.abort_stream()
        self.pen_down = False
        if self.current_drawing:
            self.store.set_status(self.current_drawing.id, "paused")
            self.current_drawing.status = "paused"
        self._log("Abort: hold + soft reset. Job marked paused; r to resume.")

    def action_browse(self) -> None:
        """Open a modal directory tree to pick an SVG file."""

        def _on_pick(path_str: str | None) -> None:
            if path_str is not None:
                self.query_one("#svg", Input).value = path_str
                self.action_convert()

        self.push_screen(SVGPickerScreen(), _on_pick)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button clicks in the path row."""
        if event.button.id == "browse-btn":
            self.action_browse()


def run() -> None:
    """Start the Textual application.

    Returns:
        None. Blocks until the user quits.
    """
    PlotterApp().run()
