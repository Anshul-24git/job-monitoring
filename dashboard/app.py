from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from job_monitor.config import ConfigError, load_config

BASE_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
STATIC_DIR = DASHBOARD_DIR / "static"

DEFAULT_DB = BASE_DIR / "job_monitor.db"
DEFAULT_LOG = BASE_DIR / "logs" / "job-monitor.log"
DEFAULT_DIAGNOSTICS = BASE_DIR / "logs" / "diagnostics.json"
DEFAULT_CONFIG = BASE_DIR / "config.yaml"

app = FastAPI(title="Job Monitor Dashboard")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _safe_fromiso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _load_config_summary() -> Dict[str, Any]:
    config_path = Path(os.getenv("JOB_MONITOR_CONFIG", str(DEFAULT_CONFIG)))
    if not config_path.exists():
        return {}
    try:
        cfg = load_config(str(config_path))
    except (ConfigError, OSError):
        return {}
    schedule = cfg.get("schedule", {})
    active_hours = schedule.get("active_hours", {})

    source_display_map: Dict[str, str] = {}
    for source in cfg.get("sources", []):
        display = source.get("display_name", source["name"])
        if source.get("auto_source"):
            kind = source.get("kind", "source")
            display = f"{display} ({kind})"
        source_display_map[source["name"]] = display
    return {
        "timezone": schedule.get("timezone"),
        "active_start": active_hours.get("start"),
        "active_end": active_hours.get("end"),
        "default_interval": schedule.get("default_interval_minutes"),
        "sources_count": len(cfg.get("sources", [])),
        "source_display_map": source_display_map,
    }


def _load_diagnostics(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def _load_log_tail(path: Path, limit: int = 200) -> List[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
        return [line.rstrip("\n") for line in lines[-limit:]]
    except OSError:
        return []


def _fetch_db_stats(db_path: Path, *, source_display_map: Dict[str, str]) -> Dict[str, Any]:
    if not db_path.exists():
        return {
            "total_jobs": 0,
            "jobs_last_24h": 0,
            "unnotified": 0,
            "latest_jobs": [],
            "by_source": [],
            "last_seen": None,
        }

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    total_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    unnotified = conn.execute("SELECT COUNT(*) FROM jobs WHERE notified_at IS NULL").fetchone()[0]

    latest_rows = conn.execute(
        """
        SELECT source, title, location, url, posted_at, first_seen, notified_at
        FROM jobs
        ORDER BY
          CASE
            WHEN posted_at IS NULL OR posted_at = '' THEN first_seen
            ELSE posted_at
          END DESC
        LIMIT 50
        """
    ).fetchall()

    latest_jobs = []
    last_seen = None
    for row in latest_rows:
        first_seen = _safe_fromiso(row["first_seen"])
        posted_at = _safe_fromiso(row["posted_at"])
        if (posted_at or first_seen) and not last_seen:
            last_seen = posted_at or first_seen
        latest_jobs.append(
            {
                "source": source_display_map.get(row["source"], row["source"]),
                "title": row["title"],
                "location": row["location"],
                "url": row["url"],
                "posted_at": posted_at,
                "first_seen": first_seen,
                "notified_at": _safe_fromiso(row["notified_at"]),
            }
        )

    by_source_rows = conn.execute(
        """
        SELECT source, COUNT(*) AS count
        FROM jobs
        GROUP BY source
        ORDER BY count DESC
        LIMIT 12
        """
    ).fetchall()
    by_source_acc: Dict[str, int] = {}
    for row in by_source_rows:
        display = source_display_map.get(row["source"], row["source"])
        by_source_acc[display] = by_source_acc.get(display, 0) + row["count"]
    by_source = [{"source": key, "count": value} for key, value in by_source_acc.items()]
    by_source.sort(key=lambda item: item["count"], reverse=True)

    since = datetime.now(tz=last_seen.tzinfo) - timedelta(hours=24) if last_seen else datetime.now() - timedelta(hours=24)
    jobs_last_24h = 0
    for row in latest_rows:
        first_seen = _safe_fromiso(row["first_seen"])
        posted_at = _safe_fromiso(row["posted_at"])
        basis = posted_at or first_seen
        if basis and basis >= since:
            jobs_last_24h += 1

    conn.close()

    return {
        "total_jobs": total_jobs,
        "jobs_last_24h": jobs_last_24h,
        "unnotified": unnotified,
        "latest_jobs": latest_jobs,
        "by_source": by_source,
        "last_seen": last_seen,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    db_path = Path(os.getenv("JOB_MONITOR_DB_PATH", str(DEFAULT_DB)))
    log_path = Path(os.getenv("JOB_MONITOR_LOG_PATH", str(DEFAULT_LOG)))
    diag_path = Path(os.getenv("JOB_MONITOR_DIAGNOSTICS_PATH", str(DEFAULT_DIAGNOSTICS)))

    config_summary = _load_config_summary()
    source_display_map = config_summary.get("source_display_map", {})
    stats = _fetch_db_stats(db_path, source_display_map=source_display_map)
    diagnostics = _load_diagnostics(diag_path)
    log_tail = _load_log_tail(log_path, limit=120)

    summary = diagnostics.get("summary") if diagnostics else {}
    sources_ok = summary.get("ok_sources")
    sources_error = summary.get("error_sources")
    source_options = sorted({job["source"] for job in stats["latest_jobs"]})
    if not source_options and source_display_map:
        source_options = sorted(set(source_display_map.values()))

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "stats": stats,
            "diagnostics": diagnostics,
            "log_tail": log_tail,
            "sources_ok": sources_ok,
            "sources_error": sources_error,
            "config_summary": config_summary,
            "source_options": source_options,
        },
    )
