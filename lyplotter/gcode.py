"""Generate LY Drawbot GRBL G-code from laid-out polylines.

Pen lift is a servo driven as spindle PWM, not a Z axis:

* Pen down: ``M3 S1000``
* Pen up: ``M5``
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lyplotter.log import get_logger
from lyplotter.svg_layout import (
    A4_HEIGHT_MM,
    A4_WIDTH_MM,
    DEFAULT_MARGIN_MM,
    LayoutResult,
    Polyline,
    layout_svg,
)

_log = get_logger("lyplotter.gcode")

PEN_DOWN_CMD = "M3 S1000"
PEN_UP_CMD = "M5"
TRAVEL_FEED = 3000.0
DRAW_FEED = 750.0
PEN_DOWN_DWELL_S = 0.2
PEN_UP_DWELL_S = 0.25


@dataclass(frozen=True)
class GcodeSettings:
    """Numeric and command defaults for plotter G-code.

    Attributes:
        pen_down: Command that lowers the pen.
        pen_up: Command that raises the pen.
        travel_feed: Pen-up feed in mm/min.
        draw_feed: Pen-down feed in mm/min.
        pen_down_dwell: Seconds to wait after lowering the pen.
        pen_up_dwell: Seconds to wait after raising the pen.
        decimals: Coordinate decimal places.
    """

    pen_down: str = PEN_DOWN_CMD
    pen_up: str = PEN_UP_CMD
    travel_feed: float = TRAVEL_FEED
    draw_feed: float = DRAW_FEED
    pen_down_dwell: float = PEN_DOWN_DWELL_S
    pen_up_dwell: float = PEN_UP_DWELL_S
    decimals: int = 3


def _fmt(value: float, decimals: int) -> str:
    """Format a millimetre coordinate without trailing zeros noise.

    Args:
        value: Number to format.
        decimals: Maximum decimal places.

    Returns:
        A compact decimal string.
    """
    return f"{value:.{decimals}f}"


def polylines_to_gcode(layout: LayoutResult, settings: GcodeSettings | None = None) -> str:
    """Convert laid-out polylines to a GRBL program for the Drawbot.

    Args:
        layout: Result of :func:`lyplotter.svg_layout.layout_svg`.
        settings: Optional overrides for feeds and pen commands.

    Returns:
        A complete G-code program as a single string (newline-separated).
    """
    cfg = settings or GcodeSettings()
    d = cfg.decimals
    lines: list[str] = [
        "; lyplotter A4 pen plot",
        f"; bbox {_fmt(layout.bbox_min[0], d)},{_fmt(layout.bbox_min[1], d)}"
        f" -> {_fmt(layout.bbox_max[0], d)},{_fmt(layout.bbox_max[1], d)}",
        "G21",
        "G90",
        cfg.pen_up,
        f"G4 P{cfg.pen_up_dwell}",
        f"G0 F{cfg.travel_feed:.0f}",
    ]

    join_eps = 10.0 ** (-d)
    pen_down = False
    last_x: float | None = None
    last_y: float | None = None

    for stroke in layout.polylines:
        if len(stroke) < 2:
            continue
        x0, y0 = stroke[0]
        connected = (
            pen_down
            and last_x is not None
            and last_y is not None
            and abs(last_x - x0) <= join_eps
            and abs(last_y - y0) <= join_eps
        )
        if not connected:
            if pen_down:
                lines.append(cfg.pen_up)
                lines.append(f"G4 P{cfg.pen_up_dwell}")
                lines.append(f"G0 F{cfg.travel_feed:.0f}")
                pen_down = False
            lines.append(f"G0 X{_fmt(x0, d)} Y{_fmt(y0, d)}")
            lines.append(cfg.pen_down)
            lines.append(f"G4 P{cfg.pen_down_dwell}")
            lines.append(f"G1 F{cfg.draw_feed:.0f}")
            pen_down = True
        for x, y in stroke[1:]:
            lines.append(f"G1 X{_fmt(x, d)} Y{_fmt(y, d)}")
        last_x, last_y = stroke[-1]

    if pen_down:
        lines.append(cfg.pen_up)
        lines.append(f"G4 P{cfg.pen_up_dwell}")
        lines.append(f"G0 F{cfg.travel_feed:.0f}")
    lines.append("G0 X0 Y0")
    lines.append(cfg.pen_up)
    lines.append("M2")
    return "\n".join(lines) + "\n"


def svg_to_gcode(
    svg_path: str | Path,
    settings: GcodeSettings | None = None,
    page_width: float = A4_WIDTH_MM,
    page_height: float = A4_HEIGHT_MM,
    margin: float | None = None,
) -> tuple[str, LayoutResult]:
    """Layout an SVG on A4 and return G-code plus geometry.

    Args:
        svg_path: Source SVG file.
        settings: Optional G-code settings.
        page_width: Page width in millimetres.
        page_height: Page height in millimetres.
        margin: Page margin; default is :data:`DEFAULT_MARGIN_MM`.

    Returns:
        ``(gcode_text, layout)``.
    """
    from lyplotter.svg_layout import DEFAULT_MARGIN_MM

    used_margin = DEFAULT_MARGIN_MM if margin is None else margin
    layout = layout_svg(svg_path, page_width, page_height, used_margin)
    return polylines_to_gcode(layout, settings), layout


def write_gcode(gcode: str, dest: str | Path) -> Path:
    """Write G-code text to disk, creating parent directories.

    Args:
        gcode: Program text.
        dest: Output file path.

    Returns:
        The resolved output path.
    """
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(gcode, encoding="utf-8")
    _log.info("Wrote %d bytes G-code to %s", len(gcode), path)
    return path


def square_test_gcode(
    settings: GcodeSettings | None = None,
    page_width: float = A4_WIDTH_MM,
    page_height: float = A4_HEIGHT_MM,
    margin: float = DEFAULT_MARGIN_MM,
) -> tuple[str, LayoutResult]:
    """Generate a test A4 square outline for verifying the plotter.

    The single closed polyline outlines the printable area (the A4 page
    inset by ``margin`` on every side) in GRBL millimetres, so it doubles
    as a travel-range check.

    Args:
        settings: Optional G-code settings.
        page_width: Page width in millimetres.
        page_height: Page height in millimetres.
        margin: Inset from the page edge, millimetres.

    Returns:
        ``(gcode_text, layout)`` ready to plot.
    """
    x1 = page_width - margin
    y1 = page_height - margin
    square: Polyline = [
        (margin, margin),
        (x1, margin),
        (x1, y1),
        (margin, y1),
        (margin, margin),
    ]
    layout = LayoutResult(
        polylines=[square],
        bbox_min=(margin, margin),
        bbox_max=(x1, y1),
        scale=1.0,
    )
    _log.info(
        "Test square prepared: %.1f x %.1f mm inside margins",
        x1 - margin,
        y1 - margin,
    )
    return polylines_to_gcode(layout, settings), layout
