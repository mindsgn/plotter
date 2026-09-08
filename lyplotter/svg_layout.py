"""Fit SVG geometry onto an A4 page in millimetres for the LY Drawbot.

SVG uses a top-left origin with Y increasing downward. GRBL uses a
bottom-left origin with Y increasing upward. All public functions return
coordinates in GRBL millimetres after scale-down (if needed), origin
placement, and Y-flip.

Filled shapes are converted to visible outlines: duplicate edges collapse,
and segments whose midpoint lies strictly inside a later fill (painter's
algorithm) are dropped so hidden isometric faces are not plotted.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from svgelements import SVG, Path as SvgPath, Shape

from lyplotter.log import get_logger

_log = get_logger("lyplotter.svg_layout")

A4_WIDTH_MM = 210.0
A4_HEIGHT_MM = 297.0
DEFAULT_MARGIN_MM = 5.0
CURVE_SAMPLES = 24
_EDGE_DIGITS = 4
_INSIDE_EPS = 1e-7

Point = tuple[float, float]
Polyline = list[Point]
EdgeKey = tuple[Point, Point]


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


def _path_to_polylines(parsed: SvgPath, samples: int) -> list[Polyline]:
    """Turn a reified SVG path into Move-separated polylines.

    Args:
        parsed: A path with transforms already applied.
        samples: Samples per curved segment.

    Returns:
        Polylines with consecutive duplicates removed.
    """
    polylines: list[Polyline] = []
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
    return [p for p in cleaned if len(p) >= 2]


def _paint_visible(paint) -> bool:
    """Return True if an SVG fill or stroke should produce geometry.

    Args:
        paint: A svgelements Color, string, or None.

    Returns:
        False for missing, ``none``, or fully transparent paint.
    """
    if paint is None:
        return False
    value = getattr(paint, "value", "present")
    if value is None:
        return False
    text = str(paint).strip().lower()
    if text in {"none", "transparent", ""}:
        return False
    opacity = getattr(paint, "opacity", None)
    if opacity is not None and float(opacity) <= 0.0:
        return False
    alpha = getattr(paint, "alpha", None)
    if alpha is not None and float(alpha) <= 0.0:
        return False
    return True


def _round_pt(point: Point, digits: int = _EDGE_DIGITS) -> Point:
    """Round a point so coincident vertices share a key.

    Args:
        point: ``(x, y)``.
        digits: Decimal places.

    Returns:
        Rounded coordinates.
    """
    return (round(point[0], digits), round(point[1], digits))


def _edge_key(start: Point, end: Point) -> EdgeKey | None:
    """Undirected key for a non-degenerate segment.

    Args:
        start: First endpoint.
        end: Second endpoint.

    Returns:
        Sorted rounded endpoints, or None if the segment has no length.
    """
    a = _round_pt(start)
    b = _round_pt(end)
    if a == b:
        return None
    return (a, b) if a <= b else (b, a)


def _on_segment(point: Point, start: Point, end: Point, eps: float = _INSIDE_EPS) -> bool:
    """Return True if ``point`` lies on the closed segment ``start``–``end``.

    Args:
        point: Test point.
        start: Segment start.
        end: Segment end.
        eps: Distance tolerance.

    Returns:
        Whether the point is on the segment, including endpoints.
    """
    ax, ay = start
    bx, by = end
    px, py = point
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    length_sq = vx * vx + vy * vy
    if length_sq < eps * eps:
        return abs(wx) <= eps and abs(wy) <= eps
    cross = abs(vx * wy - vy * wx)
    if cross * cross > eps * eps * length_sq:
        return False
    dot = vx * wx + vy * wy
    if dot < -eps:
        return False
    if dot > length_sq + eps:
        return False
    return True


def _strictly_inside(point: Point, ring: Polyline, eps: float = _INSIDE_EPS) -> bool:
    """Even-odd point-in-polygon test that treats the boundary as outside.

    Args:
        point: Test point.
        ring: Closed or open polygon vertices.
        eps: On-edge tolerance.

    Returns:
        True only if the point is in the interior, not on an edge.
    """
    if len(ring) < 3:
        return False
    pts = ring
    if pts[0] != pts[-1]:
        pts = pts + [pts[0]]
    for a, b in zip(pts, pts[1:]):
        if _on_segment(point, a, b, eps):
            return False
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        intersect = (y1 > y) != (y2 > y)
        if intersect:
            denom = y2 - y1
            if abs(denom) < eps:
                continue
            at_x = (x2 - x1) * (y - y1) / denom + x1
            if x < at_x:
                inside = not inside
    return inside


def _add_polyline_edges(
    stroke: Polyline,
    fill_index: int,
    edges: dict[EdgeKey, tuple[Point, Point, int]],
) -> None:
    """Register unique undirected edges, keeping the earliest fill index.

    Args:
        stroke: Connected points.
        fill_index: Source filled-polygon index, or -1 for stroke-only.
        edges: Map of edge key to ``(p1, p2, min_fill_index)``.
    """
    for start, end in zip(stroke, stroke[1:]):
        key = _edge_key(start, end)
        if key is None:
            continue
        prev = edges.get(key)
        if prev is None:
            edges[key] = (start, end, fill_index)
            continue
        old_idx = prev[2]
        if fill_index >= 0 and (old_idx < 0 or fill_index < old_idx):
            edges[key] = (start, end, fill_index)


def _visible_segments(
    edges: dict[EdgeKey, tuple[Point, Point, int]],
    fills: list[Polyline],
) -> list[tuple[Point, Point]]:
    """Drop edges whose midpoint is strictly inside a later fill.

    Args:
        edges: Unique edges with earliest fill index.
        fills: Filled rings in document order.

    Returns:
        Surviving segments with original coordinates.
    """
    visible: list[tuple[Point, Point]] = []
    for start, end, fill_index in edges.values():
        mid = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
        hidden = False
        for ring in fills[fill_index + 1 :]:
            if _strictly_inside(mid, ring):
                hidden = True
                break
        if not hidden:
            visible.append((start, end))
    return visible


def _chain_edges(segments: list[tuple[Point, Point]]) -> list[Polyline]:
    """Join unused segments into polylines, preferring collinear continuations.

    Args:
        segments: Undirected visible edges.

    Returns:
        Chained strokes of at least two points.
    """
    if not segments:
        return []

    def node(point: Point) -> Point:
        return _round_pt(point)

    canon: dict[Point, Point] = {}
    adj: dict[Point, list[tuple[int, Point]]] = defaultdict(list)
    unused: set[int] = set()
    for i, (start, end) in enumerate(segments):
        ka, kb = node(start), node(end)
        if ka == kb:
            continue
        canon.setdefault(ka, start)
        canon.setdefault(kb, end)
        adj[ka].append((i, kb))
        adj[kb].append((i, ka))
        unused.add(i)

    def unused_degree(key: Point) -> int:
        return sum(1 for eid, _ in adj[key] if eid in unused)

    def pick_start() -> Point | None:
        odd = [key for key in adj if unused_degree(key) % 2 == 1]
        if odd:
            return odd[0]
        for key in adj:
            if unused_degree(key) > 0:
                return key
        return None

    def next_step(curr: Point, prev: Point | None) -> tuple[int, Point] | None:
        candidates = [(eid, other) for eid, other in adj[curr] if eid in unused]
        if not candidates:
            return None
        if prev is None:
            return candidates[0]
        vx, vy = curr[0] - prev[0], curr[1] - prev[1]
        vn = (vx * vx + vy * vy) ** 0.5
        best: tuple[int, Point] | None = None
        best_score = 0.0
        for eid, other in candidates:
            wx, wy = other[0] - curr[0], other[1] - curr[1]
            wn = (wx * wx + wy * wy) ** 0.5
            if vn < 1e-12 or wn < 1e-12:
                score = -1.0
            else:
                score = (vx * wx + vy * wy) / (vn * wn)
            if best is None or score > best_score:
                best = (eid, other)
                best_score = score
        return best

    polylines: list[Polyline] = []
    while unused:
        start = pick_start()
        if start is None:
            break
        path = [start]
        prev: Point | None = None
        curr = start
        while True:
            nxt = next_step(curr, prev)
            if nxt is None:
                break
            eid, other = nxt
            unused.discard(eid)
            path.append(other)
            prev, curr = curr, other
        if len(path) >= 2:
            polylines.append([canon[key] for key in path])
    return polylines


def extract_polylines(svg_path: str | Path, samples: int = CURVE_SAMPLES) -> list[Polyline]:
    """Parse an SVG file into visible polylines in document coordinates.

    Shapes (rect, circle, polyline, path, …) are converted to paths.
    Images and unoutlined text are skipped. Filled faces are outlined;
    coincident edges are merged and hidden segments are removed.

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
    fills: list[Polyline] = []
    edges: dict[EdgeKey, tuple[Point, Point, int]] = {}

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

        subpaths = _path_to_polylines(parsed, samples)
        if not subpaths:
            continue
        filled = _paint_visible(getattr(element, "fill", None))
        stroked = _paint_visible(getattr(element, "stroke", None))
        if not filled and not stroked:
            # SVG default fill is black; keep geometry if paint is unspecified.
            filled = getattr(element, "fill", None) is None

        if filled:
            for sub in subpaths:
                ring = list(sub)
                if ring[0] != ring[-1]:
                    ring.append(ring[0])
                if len(ring) < 4:
                    continue
                index = len(fills)
                fills.append(ring)
                _add_polyline_edges(ring, index, edges)
        if stroked:
            for sub in subpaths:
                _add_polyline_edges(sub, -1, edges)

    visible = _visible_segments(edges, fills)
    chained = _chain_edges(visible)
    chained = [_dedupe_points(p) for p in chained]
    chained = [p for p in chained if len(p) >= 2]
    if not chained:
        _log.warning("No drawable paths in %s", path)
        raise ValueError(f"No drawable paths in {path}")
    _log.info("Extracted %d strokes from %s", len(chained), path)
    return chained


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


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` into ``[low, high]``.

    Args:
        value: Number to clamp.
        low: Inclusive minimum.
        high: Inclusive maximum.

    Returns:
        The bounded value. If ``high < low``, returns ``low``.
    """
    if high < low:
        return low
    return max(low, min(high, value))


def fit_origin_flip(
    polylines: list[Polyline],
    page_width: float = A4_WIDTH_MM,
    page_height: float = A4_HEIGHT_MM,
    margin: float = DEFAULT_MARGIN_MM,
) -> LayoutResult:
    """Scale down to the printable envelope, pin to origin, and flip Y for GRBL.

    Artwork larger than the page minus margins is scaled down uniformly.
    Smaller artwork keeps its parsed millimetre size (never scaled up).
    Geometry is placed at the work origin plus ``margin`` so travel stays
    near home. SVG Y-down becomes GRBL Y-up relative to the artwork bbox.

    Args:
        polylines: Geometry in SVG-style coordinates (Y down).
        page_width: Maximum page width in millimetres.
        page_height: Maximum page height in millimetres.
        margin: Keep-out margin on all sides, millimetres.

    Returns:
        Laid-out polylines in GRBL millimetres (origin bottom-left).
    """
    (min_x, min_y), (max_x, max_y) = bounding_box(polylines)
    width = max(max_x - min_x, 1e-9)
    height = max(max_y - min_y, 1e-9)
    printable_w = max(page_width - 2 * margin, 1e-9)
    printable_h = max(page_height - 2 * margin, 1e-9)
    scale = min(1.0, printable_w / width, printable_h / height)
    x_lo, x_hi = margin, page_width - margin
    y_lo, y_hi = margin, page_height - margin

    laid_out: list[Polyline] = []
    for stroke in polylines:
        new_stroke: Polyline = []
        for x, y in stroke:
            gx = (x - min_x) * scale + margin
            gy = (max_y - y) * scale + margin
            new_stroke.append((_clamp(gx, x_lo, x_hi), _clamp(gy, y_lo, y_hi)))
        laid_out.append(new_stroke)

    bbox_min, bbox_max = bounding_box(laid_out)
    return LayoutResult(
        polylines=laid_out,
        bbox_min=bbox_min,
        bbox_max=bbox_max,
        scale=scale,
    )


def sort_nearest(polylines: list[Polyline]) -> list[Polyline]:
    """Order strokes by nearest unused endpoint, reversing when that is closer.

    Args:
        polylines: Unordered draw strokes.

    Returns:
        The same strokes in a greedy nearest-neighbour order, some reversed.
    """
    if len(polylines) <= 1:
        return list(polylines)
    remaining = list(polylines)
    ordered = [remaining.pop(0)]
    while remaining:
        ex, ey = ordered[-1][-1]
        best_i = 0
        best_d = float("inf")
        best_rev = False
        for i, stroke in enumerate(remaining):
            sx, sy = stroke[0]
            d_start = (sx - ex) ** 2 + (sy - ey) ** 2
            exx, eyy = stroke[-1]
            d_end = (exx - ex) ** 2 + (eyy - ey) ** 2
            if d_end < d_start:
                d, rev = d_end, True
            else:
                d, rev = d_start, False
            if d < best_d:
                best_d = d
                best_i = i
                best_rev = rev
        chosen = remaining.pop(best_i)
        if best_rev:
            chosen = list(reversed(chosen))
        ordered.append(chosen)
    return ordered


def layout_svg(
    svg_path: str | Path,
    page_width: float = A4_WIDTH_MM,
    page_height: float = A4_HEIGHT_MM,
    margin: float = DEFAULT_MARGIN_MM,
) -> LayoutResult:
    """Load an SVG, scale it into the page envelope, flip Y, and sort travel.

    Args:
        svg_path: Path to the source SVG.
        page_width: Page width in millimetres.
        page_height: Page height in millimetres.
        margin: Keep-out margin in millimetres.

    Returns:
        A :class:`LayoutResult` ready for G-code generation.
    """
    raw = extract_polylines(svg_path)
    fitted = fit_origin_flip(raw, page_width, page_height, margin)
    return LayoutResult(
        polylines=sort_nearest(fitted.polylines),
        bbox_min=fitted.bbox_min,
        bbox_max=fitted.bbox_max,
        scale=fitted.scale,
    )
