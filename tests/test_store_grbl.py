"""Tests for SQLite store and GRBL character-counting streamer."""

from __future__ import annotations

import time
from collections import deque

from lyplotter.grbl import (
    SOFT_RESET,
    STATUS_QUERY,
    GrblClient,
    gcode_lines,
    infer_pen_down,
    parse_xy,
)
from lyplotter.store import Store


def test_store_home_and_progress(tmp_path) -> None:
    """Home coordinates and resume line survive a new Store instance."""
    db = tmp_path / "plotter.db"
    store = Store(db)
    store.save_home(12.5, 40.0)
    drawing = store.add_drawing("/tmp/a.svg", "/tmp/a.gcode")
    store.set_status(drawing.id, "running")
    store.save_progress(drawing.id, 10, 1.0, 2.0, True)
    store.close()

    store2 = Store(db)
    settings = store2.get_settings()
    assert settings.home_x == 12.5
    assert settings.home_y == 40.0
    unfinished = store2.latest_unfinished()
    assert unfinished is not None
    assert unfinished.id == drawing.id
    progress = store2.get_progress(drawing.id)
    assert progress is not None
    assert progress.last_ok_line == 10
    assert progress.pen_down is True
    store2.close()


def test_gcode_lines_strips_comments() -> None:
    """Blank lines and comments are not streamed."""
    text = "; header\nG21\n\nG90 ; abs\n"
    assert gcode_lines(text) == ["G21", "G90"]


def test_parse_xy_and_pen() -> None:
    """X/Y and pen flags update from typical plotter lines."""
    x, y = parse_xy("G1 X10.5 Y20", 0.0, 0.0)
    assert (x, y) == (10.5, 20.0)
    x, y = parse_xy("G1 X11", x, y)
    assert (x, y) == (11.0, 20.0)
    assert infer_pen_down("M3 S1000", False, "M3 S1000", "M5") is True
    assert infer_pen_down("M5", True, "M3 S1000", "M5") is False


class FakeSerial:
    """In-memory serial port that ACKs G-code with ``ok``."""

    def __init__(self, port: str, baud: int, timeout: float = 1.0) -> None:
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.written: list[bytes] = []
        self._out = deque([b"Grbl 1.1f ['$' for help]\r\n"])
        self.in_waiting = 0

    def write(self, data: bytes) -> int:
        self.written.append(data)
        if data == SOFT_RESET:
            return len(data)
        if data == STATUS_QUERY:
            self._out.append(b"<Idle|WPos:1.000,2.000,0.000>\n")
            return len(data)
        if data.endswith(b"\n"):
            self._out.append(b"ok\n")
        return len(data)

    def readline(self) -> bytes:
        if self._out:
            return self._out.popleft()
        time.sleep(0.002)
        return b""

    def close(self) -> None:
        return None

    def reset_input_buffer(self) -> None:
        return None

    def reset_output_buffer(self) -> None:
        return None


def test_grbl_stream_counts_ok(tmp_path) -> None:
    """Streamer waits for ok per line and reports the last index."""
    fake_holder: dict[str, FakeSerial] = {}

    def factory(port: str, baud: int) -> FakeSerial:
        fake_holder["s"] = FakeSerial(port, baud)
        return fake_holder["s"]

    client = GrblClient(port_factory=factory, buffer_size=128)
    client.connect("/dev/fake", 115200)
    events = []
    result = client.stream(
        ["G21", "G90", "G0 X1 Y1"],
        on_progress=events.append,
    )
    client.disconnect()
    assert result.done
    assert result.line_index == 2
    assert result.last_x == 1.0
    assert result.last_y == 1.0
    assert len(events) == 3
