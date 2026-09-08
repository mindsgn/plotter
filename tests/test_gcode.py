"""Tests for A4 layout and LY Drawbot G-code generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from lyplotter.gcode import (
    PEN_DOWN_CMD,
    PEN_UP_CMD,
    polylines_to_gcode,
    square_test_gcode,
    svg_to_gcode,
)
from lyplotter.svg_layout import (
    A4_HEIGHT_MM,
    A4_WIDTH_MM,
    DEFAULT_MARGIN_MM,
    LayoutResult,
    bounding_box,
    extract_polylines,
    fit_center_flip,
    layout_svg,
    sort_nearest,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_fit_center_places_square_inside_margins() -> None:
    """A large square is scaled into the printable A4 area and centered."""
    square = [[(0.0, 0.0), (1000.0, 0.0), (1000.0, 1000.0), (0.0, 1000.0), (0.0, 0.0)]]
    result = fit_center_flip(square)
    (min_x, min_y), (max_x, max_y) = result.bbox_min, result.bbox_max
    assert min_x >= DEFAULT_MARGIN_MM - 1e-6
    assert min_y >= DEFAULT_MARGIN_MM - 1e-6
    assert max_x <= A4_WIDTH_MM - DEFAULT_MARGIN_MM + 1e-6
    assert max_y <= A4_HEIGHT_MM - DEFAULT_MARGIN_MM + 1e-6
    mid_x = (min_x + max_x) / 2
    mid_y = (min_y + max_y) / 2
    assert mid_x == pytest.approx(A4_WIDTH_MM / 2, abs=0.05)
    assert mid_y == pytest.approx(A4_HEIGHT_MM / 2, abs=0.05)


def test_y_flip_puts_svg_top_near_page_top() -> None:
    """A point at SVG y=0 (top) lands near Y=A4 after flip when bbox is that point."""
    # Single horizontal line at SVG y=0 spanning width; after center/flip it stays mid-page.
    line = [[(0.0, 0.0), (10.0, 0.0)]]
    result = fit_center_flip(line)
    ys = [y for x, y in result.polylines[0]]
    assert min(ys) == pytest.approx(max(ys), abs=1e-6)
    assert result.polylines[0][0][1] == pytest.approx(A4_HEIGHT_MM / 2, abs=0.05)


def test_sort_nearest_starts_with_first_stroke() -> None:
    """Greedy sort keeps the first polyline and then picks the closest start."""
    a = [(0.0, 0.0), (1.0, 0.0)]
    b = [(10.0, 0.0), (11.0, 0.0)]
    c = [(1.1, 0.0), (2.0, 0.0)]
    ordered = sort_nearest([a, b, c])
    assert ordered[0] == a
    assert ordered[1] == c
    assert ordered[2] == b


def test_sort_nearest_reverses_when_end_is_closer() -> None:
    """A stroke is reversed when its last point is nearer than its first."""
    a = [(0.0, 0.0), (1.0, 0.0)]
    b = [(11.0, 0.0), (1.1, 0.0)]
    ordered = sort_nearest([a, b])
    assert ordered[0] == a
    assert ordered[1] == [(1.1, 0.0), (11.0, 0.0)]


def test_svg_square_converts_with_pen_commands(tmp_path: Path) -> None:
    """Fixture square SVG produces A4-centered G-code with M3/M5."""
    svg = FIXTURES / "square.svg"
    gcode, layout = svg_to_gcode(svg)
    (min_x, min_y), (max_x, max_y) = layout.bbox_min, layout.bbox_max
    assert min_x >= DEFAULT_MARGIN_MM - 0.5
    assert min_y >= DEFAULT_MARGIN_MM - 0.5
    assert max_x <= A4_WIDTH_MM - DEFAULT_MARGIN_MM + 0.5
    assert max_y <= A4_HEIGHT_MM - DEFAULT_MARGIN_MM + 0.5
    assert PEN_DOWN_CMD in gcode
    assert PEN_UP_CMD in gcode
    assert "G21" in gcode
    assert "G90" in gcode
    assert "G0 X0 Y0" in gcode
    out = tmp_path / "square.gcode"
    out.write_text(gcode)
    assert out.read_text().endswith("\n")


def test_polylines_to_gcode_header() -> None:
    """Generated programs start in mm absolute mode with the pen up."""
    layout = fit_center_flip([[(0.0, 0.0), (10.0, 0.0)]])
    text = polylines_to_gcode(layout)
    lines = [ln for ln in text.splitlines() if not ln.startswith(";")]
    assert lines[0] == "G21"
    assert lines[1] == "G90"
    assert lines[2] == PEN_UP_CMD


def test_layout_svg_missing_file() -> None:
    """Missing SVG paths raise FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        layout_svg("/no/such/file.svg")


def test_bounding_box_empty() -> None:
    """Empty geometry has no bounding box."""
    with pytest.raises(ValueError):
        bounding_box([])


def test_connected_strokes_share_one_pen_down() -> None:
    """Strokes that meet at an endpoint do not lift the pen between them."""
    layout = LayoutResult(
        polylines=[
            [(0.0, 0.0), (10.0, 0.0)],
            [(10.0, 0.0), (10.0, 5.0)],
        ],
        bbox_min=(0.0, 0.0),
        bbox_max=(10.0, 5.0),
        scale=1.0,
    )
    text = polylines_to_gcode(layout)
    assert text.count(PEN_DOWN_CMD) == 1
    assert "G4 P0.25" in text


def test_occluded_duplicate_rects_keep_front_outline() -> None:
    """Coincident back-face edges collapse; covered midpoints are dropped."""
    strokes = extract_polylines(FIXTURES / "occluded_rects.svg")
    assert len(strokes) < 3
    pts = {(round(x, 3), round(y, 3)) for stroke in strokes for x, y in stroke}
    assert (10.0, 10.0) in pts
    assert (70.0, 10.0) in pts or (70.0, 70.0) in pts
    assert (40.0, 20.0) not in pts
    assert (20.0, 20.0) not in pts


def test_iso_cubes_chain_fewer_strokes_than_polygons() -> None:
    """Two filled isometric cubes become a handful of chained strokes."""
    strokes = extract_polylines(FIXTURES / "iso_cubes.svg")
    polygon_count = 12
    assert 1 <= len(strokes) < polygon_count
    gcode, layout = svg_to_gcode(FIXTURES / "iso_cubes.svg")
    assert PEN_DOWN_CMD in gcode
    assert layout.bbox_min[0] >= DEFAULT_MARGIN_MM - 0.5
    assert layout.bbox_max[0] <= A4_WIDTH_MM - DEFAULT_MARGIN_MM + 0.5


def test_square_test_gcode_outlines_printable_area() -> None:
    """Test square spans the printable A4 area with pen commands."""
    gcode, layout = square_test_gcode()
    (min_x, min_y), (max_x, max_y) = layout.bbox_min, layout.bbox_max
    assert min_x == pytest.approx(DEFAULT_MARGIN_MM)
    assert min_y == pytest.approx(DEFAULT_MARGIN_MM)
    assert max_x == pytest.approx(A4_WIDTH_MM - DEFAULT_MARGIN_MM)
    assert max_y == pytest.approx(A4_HEIGHT_MM - DEFAULT_MARGIN_MM)
    assert len(layout.polylines) == 1
    assert len(layout.polylines[0]) == 5  # closed square
    assert layout.polylines[0][0] == layout.polylines[0][-1]  # closed loop
    assert PEN_DOWN_CMD in gcode
    assert PEN_UP_CMD in gcode
