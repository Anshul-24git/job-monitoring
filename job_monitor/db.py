import sqlite3
from datetime import datetime, timedelta
from typing import Iterable, List, Optional

from .models import Job


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            source TEXT,
            url TEXT,
            title TEXT,
            location TEXT,
            posted_at TEXT,
            first_seen TEXT,
            notified_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source)")
    _ensure_column(conn, "notify_pending", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "notification_attempts", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "last_notify_error", "TEXT")
    _ensure_column(conn, "next_notify_at", "TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_pending_notifications
        ON jobs(notify_pending, notified_at, next_notify_at)
        """
    )
    conn.commit()
    return conn


def _ensure_column(conn: sqlite3.Connection, column: str, definition: str) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if column not in existing:
        conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")


def job_exists(conn: sqlite3.Connection, job_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,))
    return cur.fetchone() is not None


def has_jobs_for_source(conn: sqlite3.Connection, source: str) -> bool:
    cur = conn.execute("SELECT 1 FROM jobs WHERE source = ? LIMIT 1", (source,))
    return cur.fetchone() is not None


def insert_job(conn: sqlite3.Connection, job: Job, *, first_seen: datetime) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO jobs
        (job_id, source, url, title, location, posted_at, first_seen, notified_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            job.job_id,
            job.source,
            job.url,
            job.title,
            job.location,
            job.posted_at.isoformat() if job.posted_at else None,
            first_seen.isoformat(),
        ),
    )
    conn.commit()


def mark_notified(conn: sqlite3.Connection, job_ids: Iterable[str], *, notified_at: datetime) -> None:
    conn.executemany(
        """
        UPDATE jobs
        SET notified_at = ?,
            notify_pending = 0,
            last_notify_error = NULL,
            next_notify_at = NULL
        WHERE job_id = ?
        """,
        [(notified_at.isoformat(), job_id) for job_id in job_ids],
    )
    conn.commit()


def mark_notification_pending(conn: sqlite3.Connection, job_ids: Iterable[str]) -> None:
    conn.executemany(
        """
        UPDATE jobs
        SET notify_pending = 1,
            next_notify_at = NULL
        WHERE job_id = ? AND notified_at IS NULL
        """,
        [(job_id,) for job_id in job_ids],
    )
    conn.commit()


def mark_notification_failed(
    conn: sqlite3.Connection,
    job_ids: Iterable[str],
    *,
    error: str,
    next_notify_at: datetime,
) -> None:
    conn.executemany(
        """
        UPDATE jobs
        SET notify_pending = 1,
            notification_attempts = notification_attempts + 1,
            last_notify_error = ?,
            next_notify_at = ?
        WHERE job_id = ? AND notified_at IS NULL
        """,
        [(error[:1000], next_notify_at.isoformat(), job_id) for job_id in job_ids],
    )
    conn.commit()


def get_pending_notifications(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    limit: int,
) -> List[Job]:
    rows = conn.execute(
        """
        SELECT job_id, source, title, location, url, posted_at, notification_attempts
        FROM jobs
        WHERE notify_pending = 1
          AND notified_at IS NULL
          AND (next_notify_at IS NULL OR next_notify_at <= ?)
        ORDER BY first_seen ASC
        LIMIT ?
        """,
        (now.isoformat(), int(limit)),
    ).fetchall()
    return [_job_from_row(row) for row in rows]


def count_due_pending_notifications(conn: sqlite3.Connection, *, now: datetime) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM jobs
        WHERE notify_pending = 1
          AND notified_at IS NULL
          AND (next_notify_at IS NULL OR next_notify_at <= ?)
        """,
        (now.isoformat(),),
    ).fetchone()
    return int(row[0] or 0)


def recover_recent_unnotified_notifications(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    lookback_hours: int,
) -> int:
    """Queue recent unnotified jobs that were discovered after the last success.

    This recovers jobs stranded by an SMTP failure before durable pending-notify
    state existed, without re-emailing old first-run seed rows.
    """
    row = conn.execute("SELECT MAX(notified_at) FROM jobs WHERE notified_at IS NOT NULL").fetchone()
    last_success = _parse_datetime(row[0]) if row and row[0] else None
    if not last_success:
        return 0

    cutoff = max(last_success, now - timedelta(hours=max(1, int(lookback_hours))))
    rows = conn.execute(
        """
        SELECT job_id, first_seen
        FROM jobs
        WHERE notified_at IS NULL
          AND notify_pending = 0
        """
    ).fetchall()
    job_ids = [job_id for job_id, first_seen in rows if _is_at_or_after(first_seen, cutoff, now)]
    if not job_ids:
        return 0

    mark_notification_pending(conn, job_ids)
    return len(job_ids)


def _job_from_row(row) -> Job:
    job_id, source, title, location, url, posted_at, attempts = row
    return Job(
        job_id=job_id,
        source=source,
        title=title,
        location=location or "",
        url=url,
        posted_at=_parse_datetime(posted_at),
        raw={"notification_attempts": attempts or 0},
    )


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_at_or_after(value: Optional[str], cutoff: datetime, now: datetime) -> bool:
    parsed = _parse_datetime(value)
    if not parsed:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=now.tzinfo)
    return parsed >= cutoff
