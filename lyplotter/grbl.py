"""GRBL serial client for the LY Drawbot (character-counting streamer)."""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Protocol

from lyplotter.log import get_logger

_log = get_logger("lyplotter.grbl")

GRBL_RX_BUFFER = 128
DEFAULT_BAUD = 115200
SOFT_RESET = b"\x18"
FEED_HOLD = b"!"
CYCLE_START = b"~"
STATUS_QUERY = b"?"

POS_RE = re.compile(r"(WPos|MPos):(-?[0-9.]+),(-?[0-9.]+)(?:,(-?[0-9.]+))?")
STATE_RE = re.compile(r"<([^|>]+)")


class SerialLike(Protocol):
    """Minimal serial port interface used by :class:`GrblClient`."""

    def write(self, data: bytes) -> int: ...
    def readline(self) -> bytes: ...
    def close(self) -> None: ...
    @property
    def in_waiting(self) -> int: ...
    def reset_input_buffer(self) -> None: ...
    def reset_output_buffer(self) -> None: ...


@dataclass
class MachineStatus:
    """Parsed GRBL status snapshot.

    Attributes:
        connected: Whether a serial session is open.
        state: GRBL state word (Idle, Run, Hold, Alarm, …) or ``Disconnected``.
        work_x: Work X millimetres.
        work_y: Work Y millimetres.
        raw: Last status line, if any.
    """

    connected: bool = False
    state: str = "Disconnected"
    work_x: float = 0.0
    work_y: float = 0.0
    raw: str = ""


@dataclass
class StreamEvent:
    """Progress callback payload while sending a file.

    Attributes:
        line_index: Index of the last line that received ``ok``.
        total_lines: Number of G-code lines being sent.
        last_x: Parsed X from that line, if any.
        last_y: Parsed Y from that line, if any.
        pen_down: Inferred pen state.
        done: True when the file finished or failed.
        error: Error text, empty if ok.
    """

    line_index: int
    total_lines: int
    last_x: float
    last_y: float
    pen_down: bool
    done: bool = False
    error: str = ""


XY_RE = re.compile(r"X(-?[0-9.]+)", re.IGNORECASE)
YY_RE = re.compile(r"Y(-?[0-9.]+)", re.IGNORECASE)


def parse_xy(line: str, last_x: float, last_y: float) -> tuple[float, float]:
    """Update X/Y from a G-code line, keeping previous values if omitted.

    Args:
        line: One G-code command.
        last_x: Previous X.
        last_y: Previous Y.

    Returns:
        Updated ``(x, y)``.
    """
    x, y = last_x, last_y
    mx = XY_RE.search(line)
    my = YY_RE.search(line)
    if mx:
        x = float(mx.group(1))
    if my:
        y = float(my.group(1))
    return x, y


def infer_pen_down(line: str, pen_down: bool, pen_down_cmd: str, pen_up_cmd: str) -> bool:
    """Infer pen state after a G-code line.

    Args:
        line: Command text.
        pen_down: Previous state.
        pen_down_cmd: Command that lowers the pen.
        pen_up_cmd: Command that raises the pen.

    Returns:
        Updated pen-down flag.
    """
    stripped = line.strip().upper()
    if stripped.startswith(pen_down_cmd.strip().upper()):
        return True
    if stripped.startswith(pen_up_cmd.strip().upper()):
        return False
    if stripped.startswith("M5"):
        return False
    if stripped.startswith("M3"):
        return True
    return pen_down


def gcode_lines(text: str) -> list[str]:
    """Split a program into sendable lines (no comments, no blanks).

    Args:
        text: Full G-code file contents.

    Returns:
        Stripped command lines.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        without = raw.split(";", 1)[0].strip()
        if without:
            lines.append(without)
    return lines


class GrblClient:
    """Talk to GRBL over a serial-like object.

    Streaming uses the 128-byte RX buffer character-counting protocol.
    """

    def __init__(
        self,
        port_factory: Callable[[str, int], SerialLike] | None = None,
        buffer_size: int = GRBL_RX_BUFFER,
    ) -> None:
        """Create a disconnected client.

        Args:
            port_factory: Optional factory ``(port, baud) -> serial``. Defaults
                to pyserial ``Serial``.
            buffer_size: GRBL RX buffer size in bytes.
        """
        self._port_factory = port_factory
        self.buffer_size = buffer_size
        self._serial: SerialLike | None = None
        self.status = MachineStatus()
        self._lock = threading.Lock()
        self._reader_stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._ok_error: deque[str] = deque()
        self._console: deque[str] = deque(maxlen=200)
        self._streaming = False
        self._pause = threading.Event()
        self._abort = threading.Event()
        self.port_name = ""
        self.baud = DEFAULT_BAUD

    def list_ports(self) -> list[str]:
        """List available serial device names.

        Returns:
            Device paths or COM names.
        """
        try:
            from serial.tools import list_ports
        except ImportError:
            _log.warning("pyserial not installed; cannot list serial ports")
            return []
        ports = [p.device for p in list_ports.comports()]
        _log.debug("Serial ports: %s", ", ".join(ports) or "(none)")
        return ports

    def connect(self, port: str, baud: int = DEFAULT_BAUD, timeout: float = 2.0) -> None:
        """Open serial, soft-reset, and wait for the GRBL banner.

        Args:
            port: Device path.
            baud: Baud rate (115200 for GRBL 0.9+).
            timeout: Read timeout seconds for the serial constructor.

        Raises:
            RuntimeError: If already connected or the port cannot be opened.
        """
        if self._serial is not None:
            raise RuntimeError("Already connected")
        factory = self._port_factory
        if factory is None:
            import serial

            def factory(p: str, b: int) -> SerialLike:
                return serial.Serial(p, b, timeout=timeout)

        ser = factory(port, baud)
        self._serial = ser
        self.port_name = port
        self.baud = baud
        try:
            if hasattr(ser, "reset_input_buffer"):
                ser.reset_input_buffer()
            if hasattr(ser, "reset_output_buffer"):
                ser.reset_output_buffer()
            ser.write(SOFT_RESET)
            time.sleep(0.2)
            deadline = time.time() + timeout
            banner = ""
            while time.time() < deadline:
                line = self._readline()
                if line:
                    banner += line + "\n"
                    if "Grbl" in line or "ok" in line.lower():
                        break
            self.status.connected = True
            self.status.state = "Idle"
            self._console.append(banner.strip() or "connected")
            _log.info("Connected to %s @ %d  banner: %s", port, baud, banner.strip() or "(empty)")
        except Exception as exc:
            _log.warning("Connect to %s failed: %s", port, exc)
            try:
                ser.close()
            except Exception:
                pass
            self._serial = None
            raise
        self._reader_stop.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def disconnect(self) -> None:
        """Close the serial port and mark the machine disconnected."""
        self._abort.set()
        self._reader_stop.set()
        if self._reader and self._reader is not threading.current_thread():
            self._reader.join(timeout=1.0)
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception as exc:
                _log.warning("Error closing serial on disconnect: %s", exc)
        was_port = self.port_name
        self._serial = None
        self.status = MachineStatus()
        self.port_name = ""
        if was_port:
            _log.info("Disconnected from %s", was_port)

    def is_connected(self) -> bool:
        """Return whether a port is open.

        Returns:
            True if connected.
        """
        return self._serial is not None and self.status.connected

    def _readline(self) -> str:
        """Read one line from serial if a port is open.

        Returns:
            Decoded stripped line, or empty string.
        """
        ser = self._serial
        if ser is None:
            return ""
        try:
            raw = ser.readline()
        except Exception as exc:
            _log.warning("Serial read error: %s", exc)
            return ""
        if not raw:
            return ""
        return raw.decode("utf-8", errors="replace").strip()

    def _read_loop(self) -> None:
        """Background reader: classify ok/error/status/console lines."""
        while not self._reader_stop.is_set():
            line = self._readline()
            if not line:
                continue
            self._console.append(line)
            if line.startswith("ok") or line.startswith("error"):
                with self._lock:
                    self._ok_error.append(line)
            elif line.startswith("<"):
                self._apply_status(line)

    def _apply_status(self, line: str) -> None:
        """Update :attr:`status` from a GRBL ``<...>`` report.

        Args:
            line: Status report line.
        """
        state_m = STATE_RE.match(line)
        if state_m:
            self.status.state = state_m.group(1)
        self.status.raw = line
        for kind, xs, ys, _zs in POS_RE.findall(line):
            if kind == "WPos":
                self.status.work_x = float(xs)
                self.status.work_y = float(ys)
                break
        else:
            mpos = POS_RE.search(line)
            if mpos:
                self.status.work_x = float(mpos.group(2))
                self.status.work_y = float(mpos.group(3))

    def send_realtime(self, data: bytes) -> None:
        """Write a realtime byte (hold, resume, reset, ``?``).

        Args:
            data: Raw bytes such as :data:`FEED_HOLD`.

        Raises:
            RuntimeError: If disconnected.
        """
        ser = self._serial
        if ser is None:
            _log.warning("Realtime command attempted while disconnected")
            raise RuntimeError("Disconnected")
        try:
            ser.write(data)
        except Exception as exc:
            _log.warning("Realtime serial write failed: %s", exc)
            raise

    def poll_status(self) -> None:
        """Request a status report with ``?``."""
        if self._serial is not None:
            self.send_realtime(STATUS_QUERY)

    def send_line(self, line: str, wait: bool = True, timeout: float = 5.0) -> str:
        """Send one G-code line and optionally wait for ``ok``/``error``.

        Args:
            line: Command without newline.
            wait: Block until acknowledgement.
            timeout: Seconds to wait for ack.

        Returns:
            The ack line, or empty if ``wait`` is False.

        Raises:
            RuntimeError: On disconnect, timeout, or ``error``.
        """
        ser = self._serial
        if ser is None:
            raise RuntimeError("Disconnected")
        payload = (line.strip() + "\n").encode("ascii", errors="ignore")
        _log.debug("SEND: %s", line.strip())
        ser.write(payload)
        if not wait:
            return ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._ok_error:
                    ack = self._ok_error.popleft()
                    if ack.startswith("error"):
                        _log.error("GRBL error: %s (command: %s)", ack, line)
                        raise RuntimeError(ack)
                    return ack
            time.sleep(0.01)
        _log.error("Timeout waiting for ok after: %s", line)
        raise RuntimeError(f"Timeout waiting for ok after: {line}")

    def jog(self, dx: float = 0.0, dy: float = 0.0, feed: float = 2000.0) -> None:
        """Issue a GRBL ``$J`` incremental jog in millimetres.

        Args:
            dx: X delta.
            dy: Y delta.
            feed: Jog feed mm/min.
        """
        parts = ["$J=G91 G21"]
        if dx:
            parts.append(f"X{dx:.3f}")
        if dy:
            parts.append(f"Y{dy:.3f}")
        parts.append(f"F{feed:.0f}")
        self.send_line(" ".join(parts))

    def set_work_zero(self) -> None:
        """Set the current position as work origin (G92).

        Raises:
            RuntimeError: If disconnected.
        """
        self.send_line("G92 X0 Y0")

    def return_to_zero(self) -> None:
        """Raise the pen conceptually then rapid to work origin."""
        self.send_line("M5")
        self.send_line("G90")
        self.send_line("G0 X0 Y0")

    def pen_down(self, command: str = "M3 S1000") -> None:
        """Lower the pen.

        Args:
            command: Firmware pen-down command.
        """
        self.send_line(command)

    def pen_up(self, command: str = "M5") -> None:
        """Raise the pen.

        Args:
            command: Firmware pen-up command.
        """
        self.send_line(command)

    def pause_stream(self) -> None:
        """Feed-hold the machine and pause the Python sender."""
        self._pause.set()
        if self._serial is not None:
            self.send_realtime(FEED_HOLD)

    def resume_stream(self) -> None:
        """Clear pause and send cycle-start."""
        self._pause.clear()
        if self._serial is not None:
            self.send_realtime(CYCLE_START)

    def abort_stream(self) -> None:
        """Stop streaming, feed-hold, soft-reset, and try to lift the pen."""
        self._abort.set()
        if self._serial is None:
            return
        try:
            self.send_realtime(FEED_HOLD)
            time.sleep(0.05)
            self.send_realtime(SOFT_RESET)
            time.sleep(0.2)
            self.send_line("M5", wait=False)
            _log.info("Abort: feed hold + soft reset + pen up sent")
        except Exception as exc:
            _log.warning("Abort sequence failed: %s", exc)

    def stream(
        self,
        lines: list[str],
        start_index: int = 0,
        on_progress: Callable[[StreamEvent], None] | None = None,
        pen_down_cmd: str = "M3 S1000",
        pen_up_cmd: str = "M5",
        start_x: float = 0.0,
        start_y: float = 0.0,
        start_pen_down: bool = False,
    ) -> StreamEvent:
        """Send G-code using character counting.

        Args:
            lines: Sendable G-code lines.
            start_index: First line to send (resume).
            on_progress: Optional callback after each ``ok``.
            pen_down_cmd: Used to infer pen state.
            pen_up_cmd: Used to infer pen state.
            start_x: Position before the first sent line.
            start_y: Position before the first sent line.
            start_pen_down: Pen state before the first sent line.

        Returns:
            Final :class:`StreamEvent`.

        Raises:
            RuntimeError: If disconnected or GRBL returns ``error``.
        """
        ser = self._serial
        if ser is None:
            raise RuntimeError("Disconnected")
        self._streaming = True
        self._abort.clear()
        self._pause.clear()
        _log.info(
            "Streaming %d lines from index %d (%.0f, %.0f, pen=%s)",
            len(lines),
            start_index,
            start_x,
            start_y,
            start_pen_down,
        )
        sent: deque[int] = deque()
        in_buffer = 0
        next_send = start_index
        last_acked = start_index - 1
        x, y = start_x, start_y
        pen = start_pen_down
        total = len(lines)
        final = StreamEvent(last_acked, total, x, y, pen, done=True)

        try:
            while last_acked < total - 1:
                if self._abort.is_set():
                    final = StreamEvent(last_acked, total, x, y, pen, done=True, error="aborted")
                    break
                while self._pause.is_set() and not self._abort.is_set():
                    time.sleep(0.05)
                while next_send < total:
                    payload = lines[next_send] + "\n"
                    n = len(payload.encode("ascii", errors="ignore"))
                    if in_buffer + n > self.buffer_size:
                        break
                    ser.write(payload.encode("ascii", errors="ignore"))
                    sent.append(n)
                    in_buffer += n
                    next_send += 1
                ack = None
                with self._lock:
                    if self._ok_error:
                        ack = self._ok_error.popleft()
                if ack is None:
                    time.sleep(0.005)
                    continue
                if ack.startswith("error"):
                    _log.error("GRBL stream error: %s (after line %d)", ack, last_acked)
                    raise RuntimeError(ack)
                if not sent:
                    continue
                used = sent.popleft()
                in_buffer -= used
                last_acked += 1
                line = lines[last_acked]
                x, y = parse_xy(line, x, y)
                pen = infer_pen_down(line, pen, pen_down_cmd, pen_up_cmd)
                event = StreamEvent(last_acked, total, x, y, pen)
                if on_progress:
                    on_progress(event)
                final = event
            final.done = True
            if final.error:
                _log.info("Stream aborted after line %d/%d", last_acked + 1, total)
            else:
                _log.info("Stream complete: %d/%d lines (%.2f, %.2f)", total, total, x, y)
            return final
        finally:
            self._streaming = False


def stream_file(
    client: GrblClient,
    gcode_text: str,
    start_index: int = 0,
    on_progress: Callable[[StreamEvent], None] | None = None,
    **kwargs,
) -> StreamEvent:
    """Stream a G-code program string.

    Args:
        client: Connected GRBL client.
        gcode_text: File contents.
        start_index: Resume line.
        on_progress: Progress callback.
        **kwargs: Extra :meth:`GrblClient.stream` arguments.

    Returns:
        Final stream event.
    """
    return client.stream(gcode_lines(gcode_text), start_index=start_index, on_progress=on_progress, **kwargs)
