"""Fit SVG geometry onto an A4 page in millimetres for the LY Drawbot.

SVG uses a top-left origin with Y increasing downward. GRBL uses a
bottom-left origin with Y increasing upward. All public functions return
coordinates in GRBL millimetres after scale, center, and Y-flip.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from svgelements import SVG, Path as SvgPath, Shape

from lyplotter.log import get_logger

_log = get_logger("lyplotter.svg_layout")

A4_WIDTH_MM = 210.0
A4_HEIGHT_MM = 297.0
DEFAULT_MARGIN_MM = 5.0
CURVE_SAMPLES = 24

Point = tuple[float, float]
Polyline = list[Point]


@dataclass(frozen=True)
class LayoutResult:
    """Polylines placed on A4 plus the bounding box after layout.

    Attributes:
        polylines: Draw strokes in GRBL millimetres (origin bottom-left).
        bbox_min: Lower-left of the artwork bounding box (mm).
        bbox_max: Upper-right of the artwork bounding box (mm).
        scale: Uniform scale applied from SVG user units / parsed mm to page.
    """

    polylines: list[Polyline]
    bbox_min: Point
    bbox_max: Point
    scale: float


def _segment_points(segment, samples: int) -> list[Point]:
    """Sample a path segment into points, including the end but not the start.

    Args:
        segment: An svgelements path segment with ``point(t)``.
        samples: Number of interior steps for curved segments.

    Returns:
        Points from just after the start through the segment end.
    """
    name = type(segment).__name__
    if name in {"Move", "Close", "Line"}:
        end = segment.end
        return [(float(end.x), float(end.y))]
    points: list[Point] = []
    steps = max(2, samples)
    for i in range(1, steps + 1):
        p = segment.point(i / steps)
        points.append((float(p.x), float(p.y)))
    return points


def extract_polylines(svg_path: str | Path, samples: int = CURVE_SAMPLES) -> list[Polyline]:
    """Parse an SVG file into polylines in the document's coordinate space.

    Shapes (rect, circle, polyline, path, …) are converted to paths.
    Images and unoutlined text are skipped.

    Args:
        svg_path: Path to an SVG file.
        samples: Samples per curved segment.

    Returns:
        A list of polylines, each a sequence of (x, y) points.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If no drawable geometry is found.
    """
    path = Path(svg_path)
    if not path.is_file():
        _log.warning("SVG not found: %s", path)
        raise FileNotFoundError(f"SVG not found: {path}")

    svg = SVG.parse(str(path))
    polylines: list[Polyline] = []

    for element in svg.elements():
        if not isinstance(element, Shape):
            continue
        try:
            parsed = SvgPath(element)
        except (TypeError, ValueError, AttributeError) as exc:
            _log.warning(
                "Skipped unparseable SVG element %s: %s",
                getattr(element, "id", "?"),
                exc,
            )
            continue
        parsed.reify()
        if not parsed:
            _log.debug("Skipped empty SVG path element id=%s", getattr(element, "id", "?"))
            continue

        current: list[Point] = []
        for segment in parsed.segments():
            seg_name = type(segment).__name__
            if seg_name == "Move":
                if len(current) >= 2:
                    polylines.append(current)
                start = segment.end
                current = [(float(start.x), float(start.y))]
                continue
            current.extend(_segment_points(segment, samples))
            if seg_name == "Close" and len(current) >= 2:
                polylines.append(current)
                current = []
        if len(current) >= 2:
            polylines.append(current)

    cleaned = [_dedupe_points(p) for p in polylines]
    cleaned = [p for p in cleaned if len(p) >= 2]
    if not cleaned:
        _log.warning("No drawable paths in %s", path)
        raise ValueError(f"No drawable paths in {path}")
    _log.info("Extracted %d strokes from %s", len(cleaned), path)
    return cleaned


def _dedupe_points(points: Polyline, epsilon: float = 1e-9) -> Polyline:
    """Drop consecutive duplicate points.

    Args:
        points: Input polyline.
        epsilon: Distance under which points are treated as equal.

    Returns:
        A polyline without consecutive duplicates.
    """
    if not points:
        return []
    out = [points[0]]
    for x, y in points[1:]:
        px, py = out[-1]
        if abs(x - px) > epsilon or abs(y - py) > epsilon:
            out.append((x, y))
    return out


def bounding_box(polylines: list[Polyline]) -> tuple[Point, Point]:
    """Return axis-aligned min and max corners of polylines.

    Args:
        polylines: One or more strokes.

    Returns:
        ``((min_x, min_y), (max_x, max_y))``.

    Raises:
        ValueError: If ``polylines`` is empty.
    """
    if not polylines:
        raise ValueError("Cannot compute bounding box of empty geometry")
    xs = [x for stroke in polylines for x, _ in stroke]
    ys = [y for stroke in polylines for _, y in stroke]
    return (min(xs), min(ys)), (max(xs), max(ys))


def fit_center_flip(
    polylines: list[Polyline],
    page_width: float = A4_WIDTH_MM,
    page_height: float = A4_HEIGHT_MM,
    margin: float = DEFAULT_MARGIN_MM,
) -> LayoutResult:
    """Scale uniformly to fit the printable area, center, and flip Y for GRBL.

    Artwork larger than A4 is scaled down. Smaller artwork is scaled up to
    fill the page minus margins, then centered.

    Args:
        polylines: Geometry in SVG-style coordinates (Y down).
        page_width: Page width in millimetres.
        page_height: Page height in millimetres.
        margin: Keep-out margin on all sides, millimetres.

    Returns:
        Laid-out polylines in GRBL millimetres (origin bottom-left of the page).
    """
    (min_x, min_y), (max_x, max_y) = bounding_box(polylines)
    width = max(max_x - min_x, 1e-9)
    height = max(max_y - min_y, 1e-9)
    printable_w = max(page_width - 2 * margin, 1e-9)
    printable_h = max(page_height - 2 * margin, 1e-9)
    scale = min(printable_w / width, printable_h / height)

    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    page_cx = page_width / 2.0
    page_cy = page_height / 2.0

    laid_out: list[Polyline] = []
    for stroke in polylines:
        new_stroke: Polyline = []
        for x, y in stroke:
            gx = (x - cx) * scale + page_cx
            # SVG Y down → page Y down, then flip to GRBL Y up.
            y_down = (y - cy) * scale + page_cy
            gy = page_height - y_down
            new_stroke.append((gx, gy))
        laid_out.append(new_stroke)

    bbox_min, bbox_max = bounding_box(laid_out)
    return LayoutResult(
        polylines=laid_out,
        bbox_min=bbox_min,
        bbox_max=bbox_max,
        scale=scale,
    )


def sort_nearest(polylines: list[Polyline]) -> list[Polyline]:
    """Order strokes by nearest unused start to reduce pen-up travel.

    Args:
        polylines: Unordered draw strokes.

    Returns:
        The same strokes in a greedy nearest-neighbour order.
    """
    if len(polylines) <= 1:
        return list(polylines)
    remaining = list(polylines)
    ordered = [remaining.pop(0)]
    while remaining:
        ex, ey = ordered[-1][-1]
        best_i = 0
        best_d = float("inf")
        for i, stroke in enumerate(remaining):
            sx, sy = stroke[0]
            d = (sx - ex) ** 2 + (sy - ey) ** 2
            if d < best_d:
                best_d = d
                best_i = i
        ordered.append(remaining.pop(best_i))
    return ordered


def layout_svg(
    svg_path: str | Path,
    page_width: float = A4_WIDTH_MM,
    page_height: float = A4_HEIGHT_MM,
    margin: float = DEFAULT_MARGIN_MM,
) -> LayoutResult:
    """Load an SVG, fit it on A4, center it, flip Y, and sort travel.

    Args:
        svg_path: Path to the source SVG.
        page_width: Page width in millimetres.
        page_height: Page height in millimetres.
        margin: Keep-out margin in millimetres.

    Returns:
        A :class:`LayoutResult` ready for G-code generation.
    """
    raw = extract_polylines(svg_path)
    fitted = fit_center_flip(raw, page_width, page_height, margin)
    return LayoutResult(
        polylines=sort_nearest(fitted.polylines),
        bbox_min=fitted.bbox_min,
        bbox_max=fitted.bbox_max,
        scale=fitted.scale,
    )
