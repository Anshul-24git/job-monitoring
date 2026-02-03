import logging
import time as time_module
from datetime import datetime, time, timedelta
from collections import defaultdict
from typing import Dict, List

import requests
from zoneinfo import ZoneInfo

from .config import resolve_email_config
from .db import has_jobs_for_source, init_db, insert_job, job_exists, mark_notified
from .filters import compile_keywords, matches_filters
from .http_utils import build_session
from .notify import build_error_email, build_jobs_email, send_email
from .sources import fetch_jobs_for_source


logger = logging.getLogger(__name__)


def parse_clock(value: str) -> time:
    hour, minute = value.split(":")
    return time(hour=int(hour), minute=int(minute))


def within_active_hours(now: datetime, start: time, end: time) -> bool:
    if start <= end:
        return start <= now.time() <= end
    return now.time() >= start or now.time() <= end


def seconds_until_start(now: datetime, start: time) -> float:
    next_start = datetime.combine(now.date(), start, tzinfo=now.tzinfo)
    if now.time() >= start:
        next_start += timedelta(days=1)
    return max((next_start - now).total_seconds(), 0)


def send_error_notification(email_cfg: dict, source_name: str, error_message: str) -> None:
    subject = f"Job Monitor Error: {source_name}"
    body = build_error_email(source_name, error_message)
    send_email(
        smtp_host=email_cfg["smtp_host"],
        smtp_port=email_cfg["smtp_port"],
        user=email_cfg["user"],
        app_password=email_cfg["app_password"],
        sender=email_cfg["from"],
        recipient=email_cfg["to"],
        subject=subject,
        body=body,
    )


def format_subject(source_name: str, count: int) -> str:
    if count == 1:
        return f"New job from {source_name}, check them out."
    return f"New jobs from {source_name}, check them out."


def format_header(source_name: str, count: int) -> str:
    if count == 1:
        return f"New job from {source_name}, check them out:"
    return f"New jobs from {source_name}, check them out:"


def run_once(cfg: dict, *, db_path: str) -> None:
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    email_cfg = resolve_email_config(cfg)

    include_patterns = compile_keywords(cfg["filters"]["include_keywords"])
    exclude_patterns = compile_keywords(cfg["filters"]["exclude_keywords"])
    location_cfg = cfg["filters"]["location"]
    notifications_cfg = cfg.get("notifications", {})
    skip_first_run = bool(notifications_cfg.get("skip_first_run", False))
    ignore_error_statuses = set(notifications_cfg.get("ignore_error_statuses", []))
    source_display_map = {s["name"]: s.get("display_name", s["name"]) for s in cfg["sources"]}

    conn = init_db(db_path)

    session = build_session()

    new_jobs: List = []

    for source in cfg["sources"]:
        try:
            jobs = fetch_jobs_for_source(source, session)
        except Exception as exc:
            status_code = None
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                status_code = exc.response.status_code
            is_transient = isinstance(exc, (requests.Timeout, requests.ConnectionError))

            if source.get("auto_source") and status_code in ignore_error_statuses:
                logger.warning(
                    "Skipping error notification for %s (HTTP %s)",
                    source["name"],
                    status_code,
                )
                continue
            if source.get("auto_source") and is_transient:
                logger.warning("Skipping transient error for %s", source["name"])
                continue

            logger.exception("Error fetching %s", source["name"])
            if email_cfg["notify_on_errors"]:
                try:
                    send_error_notification(email_cfg, source["name"], str(exc))
                except Exception:
                    logger.exception("Failed to send error notification")
            continue

        seeded = skip_first_run and not has_jobs_for_source(conn, source["name"])

        for job in jobs:
            if not matches_filters(
                job.title,
                include_patterns=include_patterns,
                exclude_patterns=exclude_patterns,
                location=job.location,
                us_only=location_cfg.get("us_only", True),
                allow_remote_without_us_signal=location_cfg.get("allow_remote_without_us_signal", False),
                assume_us_only=source.get("assume_us_only", False),
            ):
                continue

            if job_exists(conn, job.job_id):
                continue

            insert_job(conn, job, first_seen=datetime.now(tz))
            if not seeded:
                new_jobs.append(job)

    if new_jobs:
        jobs_by_source = defaultdict(list)
        for job in new_jobs:
            jobs_by_source[job.source].append(job)

        for source_name, jobs in jobs_by_source.items():
            display_name = source_display_map.get(source_name, source_name)
            subject = format_subject(display_name, len(jobs))
            header = format_header(display_name, len(jobs))
            body = build_jobs_email(jobs, header)
            send_email(
                smtp_host=email_cfg["smtp_host"],
                smtp_port=email_cfg["smtp_port"],
                user=email_cfg["user"],
                app_password=email_cfg["app_password"],
                sender=email_cfg["from"],
                recipient=email_cfg["to"],
                subject=subject,
                body=body,
            )
            mark_notified(conn, [job.job_id for job in jobs], notified_at=datetime.now(tz))


def run_scheduler(cfg: dict, *, db_path: str) -> None:
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    email_cfg = resolve_email_config(cfg)

    include_patterns = compile_keywords(cfg["filters"]["include_keywords"])
    exclude_patterns = compile_keywords(cfg["filters"]["exclude_keywords"])
    location_cfg = cfg["filters"]["location"]
    notifications_cfg = cfg.get("notifications", {})
    skip_first_run = bool(notifications_cfg.get("skip_first_run", False))
    ignore_error_statuses = set(notifications_cfg.get("ignore_error_statuses", []))
    source_display_map = {s["name"]: s.get("display_name", s["name"]) for s in cfg["sources"]}

    conn = init_db(db_path)

    session = build_session()

    start_time = parse_clock(cfg["schedule"]["active_hours"]["start"])
    end_time = parse_clock(cfg["schedule"]["active_hours"]["end"])
    sleep_seconds = int(cfg["schedule"]["sleep_seconds"])

    source_state: List[Dict] = []
    now = datetime.now(tz)
    for source in cfg["sources"]:
        interval = int(source.get("interval_minutes", cfg["schedule"]["default_interval_minutes"]))
        source_state.append({
            "source": source,
            "interval": interval,
            "next_run": now,
            "last_error_notified": None,
        })

    logger.info("Scheduler started. Active hours: %s-%s %s", start_time, end_time, cfg["schedule"]["timezone"])

    while True:
        now = datetime.now(tz)
        if not within_active_hours(now, start_time, end_time):
            sleep_for = seconds_until_start(now, start_time)
            logger.info("Outside active hours. Sleeping for %.0f seconds", sleep_for)
            time_module.sleep(max(sleep_for, 30))
            continue

        for state in source_state:
            if now < state["next_run"]:
                continue

            source = state["source"]
            try:
                jobs = fetch_jobs_for_source(source, session)
            except Exception as exc:
                status_code = None
                if isinstance(exc, requests.HTTPError) and exc.response is not None:
                    status_code = exc.response.status_code
                is_transient = isinstance(exc, (requests.Timeout, requests.ConnectionError))

                if source.get("auto_source") and status_code in ignore_error_statuses:
                    logger.warning(
                        "Skipping error notification for %s (HTTP %s)",
                        source["name"],
                        status_code,
                    )
                    state["next_run"] = now + timedelta(minutes=state["interval"])
                    continue
                if source.get("auto_source") and is_transient:
                    logger.warning("Skipping transient error for %s", source["name"])
                    state["next_run"] = now + timedelta(minutes=state["interval"])
                    continue

                logger.exception("Error fetching %s", source["name"])

                if email_cfg["notify_on_errors"]:
                    cooldown = timedelta(minutes=email_cfg["error_email_cooldown_minutes"])
                    last_notified = state["last_error_notified"]
                    if not last_notified or (now - last_notified) > cooldown:
                        try:
                            send_error_notification(email_cfg, source["name"], str(exc))
                            state["last_error_notified"] = now
                        except Exception:
                            logger.exception("Failed to send error notification")

                state["next_run"] = now + timedelta(minutes=state["interval"])
                continue

            new_jobs: List = []
            seeded = skip_first_run and not has_jobs_for_source(conn, source["name"])
            for job in jobs:
                if not matches_filters(
                    job.title,
                    include_patterns=include_patterns,
                    exclude_patterns=exclude_patterns,
                    location=job.location,
                    us_only=location_cfg.get("us_only", True),
                    allow_remote_without_us_signal=location_cfg.get("allow_remote_without_us_signal", False),
                    assume_us_only=source.get("assume_us_only", False),
                ):
                    continue

                if job_exists(conn, job.job_id):
                    continue

                insert_job(conn, job, first_seen=now)
                if not seeded:
                    new_jobs.append(job)

            if new_jobs:
                jobs_by_source = defaultdict(list)
                for job in new_jobs:
                    jobs_by_source[job.source].append(job)

                for source_name, jobs in jobs_by_source.items():
                    display_name = source_display_map.get(source_name, source_name)
                    subject = format_subject(display_name, len(jobs))
                    header = format_header(display_name, len(jobs))
                    body = build_jobs_email(jobs, header)
                    try:
                        send_email(
                            smtp_host=email_cfg["smtp_host"],
                            smtp_port=email_cfg["smtp_port"],
                            user=email_cfg["user"],
                            app_password=email_cfg["app_password"],
                            sender=email_cfg["from"],
                            recipient=email_cfg["to"],
                            subject=subject,
                            body=body,
                        )
                        mark_notified(conn, [job.job_id for job in jobs], notified_at=now)
                    except Exception:
                        logger.exception("Failed to send notification email")

            state["next_run"] = now + timedelta(minutes=state["interval"])

        time_module.sleep(sleep_seconds)
