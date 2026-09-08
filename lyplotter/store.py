"""SQLite persistence for home coordinates and drawing progress."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from lyplotter.log import get_logger

_log = get_logger("lyplotter.store")

APP_DIR_NAME = ".lyplotter"
DB_NAME = "plotter.db"


def default_db_path() -> Path:
    """Return the default database path under the user home directory.

    Returns:
        ``~/.lyplotter/plotter.db``.
    """
    return Path.home() / APP_DIR_NAME / DB_NAME


@dataclass
class Settings:
    """Persisted machine and sender preferences.

    Attributes:
        port: Last serial device path.
        baud: Last baud rate.
        home_x: Saved work origin X (mm), informational.
        home_y: Saved work origin Y (mm), informational.
        travel_feed: Pen-up feed mm/min.
        draw_feed: Pen-down feed mm/min.
        pen_down: Pen-down G-code.
        pen_up: Pen-up G-code.
    """

    port: str = ""
    baud: int = 115200
    home_x: float = 0.0
    home_y: float = 0.0
    travel_feed: float = 3000.0
    draw_feed: float = 750.0
    pen_down: str = "M3 S1000"
    pen_up: str = "M5"


@dataclass
class Drawing:
    """One converted job.

    Attributes:
        id: Database primary key.
        svg_path: Source SVG.
        gcode_path: Generated G-code file.
        status: ``converted``, ``running``, ``paused``, ``done``, or ``failed``.
        created_at: ISO timestamp.
    """

    id: int
    svg_path: str
    gcode_path: str
    status: str
    created_at: str


@dataclass
class Progress:
    """Resume cursor for a drawing.

    Attributes:
        drawing_id: Parent drawing.
        last_ok_line: Last G-code line index that received ``ok`` (0-based).
        last_x: Last known work X.
        last_y: Last known work Y.
        pen_down: Whether the pen was down after that line.
        updated_at: ISO timestamp.
    """

    drawing_id: int
    last_ok_line: int
    last_x: float
    last_y: float
    pen_down: bool
    updated_at: str


class Store:
    """SQLite store for settings, drawings, and stream progress."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        """Open (or create) the database.

        Args:
            db_path: Optional override; defaults to :func:`default_db_path`.
        """
        self.path = Path(db_path) if db_path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        _log.info("SQLite database opened at %s", self.path)

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()
        _log.debug("SQLite database closed")

    def _init_schema(self) -> None:
        """Create tables if they do not exist."""
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    port TEXT NOT NULL DEFAULT '',
                    baud INTEGER NOT NULL DEFAULT 115200,
                    home_x REAL NOT NULL DEFAULT 0,
                    home_y REAL NOT NULL DEFAULT 0,
                    travel_feed REAL NOT NULL DEFAULT 3000,
                    draw_feed REAL NOT NULL DEFAULT 750,
                    pen_down TEXT NOT NULL DEFAULT 'M3 S1000',
                    pen_up TEXT NOT NULL DEFAULT 'M5'
                );
                INSERT OR IGNORE INTO settings (id) VALUES (1);

                CREATE TABLE IF NOT EXISTS drawings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    svg_path TEXT NOT NULL,
                    gcode_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS progress (
                    drawing_id INTEGER PRIMARY KEY,
                    last_ok_line INTEGER NOT NULL DEFAULT -1,
                    last_x REAL NOT NULL DEFAULT 0,
                    last_y REAL NOT NULL DEFAULT 0,
                    pen_down INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (drawing_id) REFERENCES drawings(id)
                );
                """
            )
            self._conn.commit()

    def get_settings(self) -> Settings:
        """Load the singleton settings row.

        Returns:
            Current :class:`Settings`.
        """
        with self._lock:
            row = self._conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        return Settings(
            port=row["port"],
            baud=row["baud"],
            home_x=row["home_x"],
            home_y=row["home_y"],
            travel_feed=row["travel_feed"],
            draw_feed=row["draw_feed"],
            pen_down=row["pen_down"],
            pen_up=row["pen_up"],
        )

    def save_settings(self, settings: Settings) -> None:
        """Persist settings.

        Args:
            settings: Values to store.
        """
        with self._lock:
            self._conn.execute(
                """
                UPDATE settings SET
                    port = ?, baud = ?, home_x = ?, home_y = ?,
                    travel_feed = ?, draw_feed = ?, pen_down = ?, pen_up = ?
                WHERE id = 1
                """,
                (
                    settings.port,
                    settings.baud,
                    settings.home_x,
                    settings.home_y,
                    settings.travel_feed,
                    settings.draw_feed,
                    settings.pen_down,
                    settings.pen_up,
                ),
            )
            self._conn.commit()
        _log.debug("Settings saved (port=%s baud=%s)", settings.port, settings.baud)

    def save_home(self, x: float, y: float) -> None:
        """Record work-origin coordinates used as home.

        Args:
            x: Work X in millimetres.
            y: Work Y in millimetres.
        """
        settings = self.get_settings()
        settings.home_x = x
        settings.home_y = y
        self.save_settings(settings)

    def add_drawing(self, svg_path: str, gcode_path: str, status: str = "converted") -> Drawing:
        """Insert a drawing row.

        Args:
            svg_path: Source file.
            gcode_path: Generated G-code file.
            status: Initial status.

        Returns:
            The new :class:`Drawing`.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO drawings (svg_path, gcode_path, status, created_at) VALUES (?, ?, ?, ?)",
                (svg_path, gcode_path, status, now),
            )
            self._conn.commit()
            drawing_id = int(cur.lastrowid)
        _log.info("Drawing #%d added: %s -> %s", drawing_id, svg_path, gcode_path)
        return Drawing(drawing_id, svg_path, gcode_path, status, now)

    def set_status(self, drawing_id: int, status: str) -> None:
        """Update a drawing's status.

        Args:
            drawing_id: Row id.
            status: New status string.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE drawings SET status = ? WHERE id = ?",
                (status, drawing_id),
            )
            self._conn.commit()
        _log.debug("Drawing #%d status -> %s", drawing_id, status)

    def get_drawing(self, drawing_id: int) -> Drawing | None:
        """Fetch one drawing.

        Args:
            drawing_id: Row id.

        Returns:
            The drawing, or ``None``.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM drawings WHERE id = ?", (drawing_id,)
            ).fetchone()
        if row is None:
            return None
        return Drawing(
            id=row["id"],
            svg_path=row["svg_path"],
            gcode_path=row["gcode_path"],
            status=row["status"],
            created_at=row["created_at"],
        )

    def latest_unfinished(self) -> Drawing | None:
        """Return the newest drawing that is running or paused.

        Returns:
            A :class:`Drawing` if a job can be resumed, otherwise ``None``.
        """
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM drawings
                WHERE status IN ('running', 'paused')
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return Drawing(
            id=row["id"],
            svg_path=row["svg_path"],
            gcode_path=row["gcode_path"],
            status=row["status"],
            created_at=row["created_at"],
        )

    def list_drawings(self, limit: int = 20) -> list[Drawing]:
        """List recent drawings, newest first.

        Args:
            limit: Maximum rows.

        Returns:
            Drawing records.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM drawings ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            Drawing(
                id=r["id"],
                svg_path=r["svg_path"],
                gcode_path=r["gcode_path"],
                status=r["status"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def save_progress(
        self,
        drawing_id: int,
        last_ok_line: int,
        last_x: float,
        last_y: float,
        pen_down: bool,
    ) -> None:
        """Upsert stream progress for crash recovery.

        Args:
            drawing_id: Parent drawing.
            last_ok_line: Last acknowledged line index.
            last_x: Last X (mm).
            last_y: Last Y (mm).
            pen_down: Pen state after that line.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO progress (
                    drawing_id, last_ok_line, last_x, last_y, pen_down, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(drawing_id) DO UPDATE SET
                    last_ok_line = excluded.last_ok_line,
                    last_x = excluded.last_x,
                    last_y = excluded.last_y,
                    pen_down = excluded.pen_down,
                    updated_at = excluded.updated_at
                """,
                (drawing_id, last_ok_line, last_x, last_y, 1 if pen_down else 0, now),
            )
            self._conn.commit()
        _log.debug(
            "Progress saved drawing #%d line %d (%.2f, %.2f pen=%s)",
            drawing_id,
            last_ok_line,
            last_x,
            last_y,
            pen_down,
        )

    def get_progress(self, drawing_id: int) -> Progress | None:
        """Load progress for a drawing.

        Args:
            drawing_id: Parent drawing.

        Returns:
            Progress, or ``None`` if never started.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM progress WHERE drawing_id = ?", (drawing_id,)
            ).fetchone()
        if row is None:
            return None
        return Progress(
            drawing_id=row["drawing_id"],
            last_ok_line=row["last_ok_line"],
            last_x=row["last_x"],
            last_y=row["last_y"],
            pen_down=bool(row["pen_down"]),
            updated_at=row["updated_at"],
        )
