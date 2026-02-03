import sqlite3
from datetime import datetime
from typing import Iterable, Optional

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
    conn.commit()
    return conn


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
        "UPDATE jobs SET notified_at = ? WHERE job_id = ?",
        [(notified_at.isoformat(), job_id) for job_id in job_ids],
    )
    conn.commit()
