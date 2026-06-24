import logging
import time as time_module
from datetime import datetime, time, timedelta
from collections import defaultdict
from typing import Dict, List

import requests
from zoneinfo import ZoneInfo

from .config import resolve_email_config
from .db import (
    count_due_pending_notifications,
    get_pending_notifications,
    has_jobs_for_source,
    init_db,
    insert_job,
    job_exists,
    mark_notification_failed,
    mark_notification_pending,
    mark_notified,
    recover_recent_unnotified_notifications,
)
from .filters import compile_keywords, extend_patterns, matches_filters
from .http_utils import build_session
from .notify import OutboundEmail, build_error_email, build_jobs_email, send_email, send_email_batch
from .sources import fetch_jobs_for_source
from .utils import is_date_only_posted_at


logger = logging.getLogger(__name__)


TRANSIENT_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "name resolution",
    "failed to resolve",
    "connection aborted",
    "connection reset",
    "connection unexpectedly closed",
    "remote end closed connection",
    "temporarily unavailable",
    "expecting value: line 1 column 1",
)


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
    noun = "job" if count == 1 else "jobs"
    return f"{count} new {noun} from {source_name}, check them out."


def format_header(source_name: str, count: int) -> str:
    noun = "job" if count == 1 else "jobs"
    return f"{count} new {noun} from {source_name}, check them out:"


def should_notify_seed(
    *,
    seeded: bool,
    job,
    now: datetime,
    seed_recent_hours: int,
    seed_require_posted_at: bool,
) -> bool:
    if not seeded:
        return True
    if not seed_recent_hours:
        return False
    if seed_require_posted_at and not job.posted_at:
        return False
    if not job.posted_at:
        return False
    return job.posted_at >= (now - timedelta(hours=seed_recent_hours))


def should_notify_recent(
    *,
    job,
    now: datetime,
    notify_recent_hours: int,
    notify_require_posted_at: bool,
) -> bool:
    if not notify_recent_hours:
        return True
    if not job.posted_at:
        return not notify_require_posted_at
    return job.posted_at >= (now - timedelta(hours=notify_recent_hours))


def normalize_job_posted_at(job, *, now: datetime) -> None:
    if job.posted_at is None or is_date_only_posted_at(job):
        job.posted_at = now


def is_transient_fetch_error(exc: Exception) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    message = f"{exc.__class__.__name__}: {exc}".lower()
    return any(marker in message for marker in TRANSIENT_ERROR_MARKERS)


def source_error_backoff_minutes(*, interval: int, consecutive_errors: int, max_minutes: int) -> int:
    multiplier = 2 ** min(max(consecutive_errors - 1, 0), 4)
    return min(max(interval, 1) * multiplier, max(max_minutes, interval, 1))


def retry_delay_for_jobs(notifications_cfg: dict, jobs: List) -> timedelta:
    base_minutes = max(1, int(notifications_cfg.get("notification_retry_base_minutes", 10) or 10))
    max_minutes = max(base_minutes, int(notifications_cfg.get("notification_retry_max_minutes", 120) or 120))
    attempts = max((int(job.raw.get("notification_attempts", 0) or 0) for job in jobs), default=0)
    return timedelta(minutes=min(max_minutes, base_minutes * (2 ** attempts)))


def group_jobs_by_source(jobs: List) -> Dict[str, List]:
    jobs_by_source: Dict[str, List] = defaultdict(list)
    for job in jobs:
        jobs_by_source[job.source].append(job)
    return jobs_by_source


def send_job_notifications(
    *,
    conn,
    email_cfg: dict,
    notifications_cfg: dict,
    jobs_by_source: Dict[str, List],
    source_display_map: Dict[str, str],
    tz,
    now: datetime,
    context: str,
) -> None:
    if not jobs_by_source:
        return

    groups = []
    messages: List[OutboundEmail] = []
    for source_name, jobs in jobs_by_source.items():
        display_name = source_display_map.get(source_name, source_name)
        subject = format_subject(display_name, len(jobs))
        header = format_header(display_name, len(jobs))
        body = build_jobs_email(jobs, header, tz=tz)
        groups.append((source_name, jobs))
        messages.append(OutboundEmail(recipient=email_cfg["to"], subject=subject, body=body))
        mark_notification_pending(conn, [job.job_id for job in jobs])

    max_attempts = int(notifications_cfg.get("email_max_attempts", 3) or 3)
    retry_seconds = int(notifications_cfg.get("email_retry_initial_seconds", 5) or 5)

    try:
        errors = send_email_batch(
            smtp_host=email_cfg["smtp_host"],
            smtp_port=email_cfg["smtp_port"],
            user=email_cfg["user"],
            app_password=email_cfg["app_password"],
            sender=email_cfg["from"],
            messages=messages,
            max_attempts=max_attempts,
            initial_retry_delay_seconds=retry_seconds,
        )
    except Exception as exc:
        logger.exception("Failed to send notification batch during %s", context)
        for _, jobs in groups:
            next_notify_at = now + retry_delay_for_jobs(notifications_cfg, jobs)
            mark_notification_failed(
                conn,
                [job.job_id for job in jobs],
                error=str(exc),
                next_notify_at=next_notify_at,
            )
        return

    for index, (source_name, jobs) in enumerate(groups):
        job_ids = [job.job_id for job in jobs]
        if index not in errors:
            mark_notified(conn, job_ids, notified_at=now)
            logger.info("Sent %s notification email for %s jobs from %s", context, len(jobs), source_name)
            continue

        error = errors[index]
        next_notify_at = now + retry_delay_for_jobs(notifications_cfg, jobs)
        mark_notification_failed(conn, job_ids, error=str(error), next_notify_at=next_notify_at)
        logger.error(
            "Failed to send %s notification email for %s jobs from %s; retry after %s",
            context,
            len(jobs),
            source_name,
            next_notify_at.isoformat(),
            exc_info=(type(error), error, error.__traceback__),
        )


def process_pending_notifications(
    *,
    conn,
    email_cfg: dict,
    notifications_cfg: dict,
    source_display_map: Dict[str, str],
    tz,
    now: datetime,
) -> None:
    limit = int(notifications_cfg.get("pending_notification_limit", 50) or 50)
    pending_jobs = get_pending_notifications(conn, now=now, limit=limit)
    if not pending_jobs:
        return

    logger.info("Retrying %s pending job notification(s)", len(pending_jobs))
    send_job_notifications(
        conn=conn,
        email_cfg=email_cfg,
        notifications_cfg=notifications_cfg,
        jobs_by_source=group_jobs_by_source(pending_jobs),
        source_display_map=source_display_map,
        tz=tz,
        now=now,
        context="pending",
    )


def recover_pending_notifications_if_needed(conn, notifications_cfg: dict, *, now: datetime) -> None:
    lookback_hours = int(notifications_cfg.get("recover_unnotified_since_last_success_hours", 48) or 0)
    if not lookback_hours:
        return

    recovered = recover_recent_unnotified_notifications(
        conn,
        now=now,
        lookback_hours=lookback_hours,
    )
    if recovered:
        logger.warning("Recovered %s recently stranded unnotified job(s) for retry", recovered)


def run_once(cfg: dict, *, db_path: str) -> None:
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    email_cfg = resolve_email_config(cfg)
    now = datetime.now(tz)

    include_patterns = compile_keywords(cfg["filters"]["include_keywords"])
    exclude_patterns = compile_keywords(cfg["filters"]["exclude_keywords"])
    location_cfg = cfg["filters"]["location"]
    notifications_cfg = cfg.get("notifications", {})
    skip_first_run = bool(notifications_cfg.get("skip_first_run", False))
    seed_recent_hours = int(notifications_cfg.get("seed_recent_hours") or 0)
    seed_require_posted_at = bool(notifications_cfg.get("seed_require_posted_at", True))
    notify_recent_hours = int(notifications_cfg.get("notify_recent_hours") or 0)
    notify_require_posted_at = bool(notifications_cfg.get("notify_require_posted_at", True))
    ignore_error_statuses = set(notifications_cfg.get("ignore_error_statuses", []))
    source_display_map = {s["name"]: s.get("display_name", s["name"]) for s in cfg["sources"]}

    conn = init_db(db_path)

    session = build_session()
    recover_pending_notifications_if_needed(conn, notifications_cfg, now=now)
    process_pending_notifications(
        conn=conn,
        email_cfg=email_cfg,
        notifications_cfg=notifications_cfg,
        source_display_map=source_display_map,
        tz=tz,
        now=now,
    )

    new_jobs: List = []

    for source in cfg["sources"]:
        source_include_patterns = extend_patterns(include_patterns, source.get("include_keywords", []))
        source_exclude_patterns = extend_patterns(exclude_patterns, source.get("exclude_keywords", []))
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
        source_seed_recent_hours = int(source.get("seed_recent_hours", seed_recent_hours) or 0)
        source_seed_require_posted_at = bool(source.get("seed_require_posted_at", seed_require_posted_at))
        source_notify_recent_hours = int(source.get("notify_recent_hours", notify_recent_hours) or 0)
        source_notify_require_posted_at = bool(source.get("notify_require_posted_at", notify_require_posted_at))

        for job in jobs:
            normalize_job_posted_at(job, now=now)
            if not matches_filters(
                job.title,
                include_patterns=source_include_patterns,
                exclude_patterns=source_exclude_patterns,
                location=job.location,
                url=job.url,
                us_only=location_cfg.get("us_only", True),
                allow_remote_without_us_signal=location_cfg.get("allow_remote_without_us_signal", False),
                assume_us_only=source.get("assume_us_only", False),
            ):
                continue

            if job_exists(conn, job.job_id):
                continue

            insert_job(conn, job, first_seen=now)
            if should_notify_seed(
                seeded=seeded,
                job=job,
                now=now,
                seed_recent_hours=source_seed_recent_hours,
                seed_require_posted_at=source_seed_require_posted_at,
            ) and should_notify_recent(
                job=job,
                now=now,
                notify_recent_hours=source_notify_recent_hours,
                notify_require_posted_at=source_notify_require_posted_at,
            ):
                new_jobs.append(job)

    if new_jobs:
        send_job_notifications(
            conn=conn,
            email_cfg=email_cfg,
            notifications_cfg=notifications_cfg,
            jobs_by_source=group_jobs_by_source(new_jobs),
            source_display_map=source_display_map,
            tz=tz,
            now=now,
            context="new-job",
        )


def run_scheduler(cfg: dict, *, db_path: str) -> None:
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    email_cfg = resolve_email_config(cfg)

    include_patterns = compile_keywords(cfg["filters"]["include_keywords"])
    exclude_patterns = compile_keywords(cfg["filters"]["exclude_keywords"])
    location_cfg = cfg["filters"]["location"]
    notifications_cfg = cfg.get("notifications", {})
    skip_first_run = bool(notifications_cfg.get("skip_first_run", False))
    seed_recent_hours = int(notifications_cfg.get("seed_recent_hours") or 0)
    seed_require_posted_at = bool(notifications_cfg.get("seed_require_posted_at", True))
    notify_recent_hours = int(notifications_cfg.get("notify_recent_hours") or 0)
    notify_require_posted_at = bool(notifications_cfg.get("notify_require_posted_at", True))
    ignore_error_statuses = set(notifications_cfg.get("ignore_error_statuses", []))
    source_display_map = {s["name"]: s.get("display_name", s["name"]) for s in cfg["sources"]}

    conn = init_db(db_path)

    session = build_session()
    recover_pending_notifications_if_needed(conn, notifications_cfg, now=datetime.now(tz))

    start_time = parse_clock(cfg["schedule"]["active_hours"]["start"])
    end_time = parse_clock(cfg["schedule"]["active_hours"]["end"])
    sleep_seconds = int(cfg["schedule"]["sleep_seconds"])
    heartbeat_minutes = int(notifications_cfg.get("heartbeat_minutes", 30) or 30)
    last_heartbeat = None

    source_state: List[Dict] = []
    now = datetime.now(tz)
    for source in cfg["sources"]:
        interval = int(source.get("interval_minutes", cfg["schedule"]["default_interval_minutes"]))
        source_state.append({
            "source": source,
            "interval": interval,
            "next_run": now,
            "last_error_notified": None,
            "consecutive_errors": 0,
        })

    logger.info("Scheduler started. Active hours: %s-%s %s", start_time, end_time, cfg["schedule"]["timezone"])

    while True:
        now = datetime.now(tz)
        if not last_heartbeat or (now - last_heartbeat) >= timedelta(minutes=heartbeat_minutes):
            due_pending = count_due_pending_notifications(conn, now=now)
            logger.info(
                "Scheduler heartbeat. sources=%s due_pending_notifications=%s",
                len(source_state),
                due_pending,
            )
            last_heartbeat = now

        if not within_active_hours(now, start_time, end_time):
            sleep_for = seconds_until_start(now, start_time)
            logger.info("Outside active hours. Sleeping for %.0f seconds", sleep_for)
            time_module.sleep(max(sleep_for, 30))
            continue

        process_pending_notifications(
            conn=conn,
            email_cfg=email_cfg,
            notifications_cfg=notifications_cfg,
            source_display_map=source_display_map,
            tz=tz,
            now=now,
        )

        for state in source_state:
            if now < state["next_run"]:
                continue

            source = state["source"]
            source_include_patterns = extend_patterns(include_patterns, source.get("include_keywords", []))
            source_exclude_patterns = extend_patterns(exclude_patterns, source.get("exclude_keywords", []))
            try:
                jobs = fetch_jobs_for_source(source, session)
            except Exception as exc:
                status_code = None
                if isinstance(exc, requests.HTTPError) and exc.response is not None:
                    status_code = exc.response.status_code
                is_transient = is_transient_fetch_error(exc)
                state["consecutive_errors"] += 1
                consecutive_errors = state["consecutive_errors"]
                backoff_minutes = source_error_backoff_minutes(
                    interval=state["interval"],
                    consecutive_errors=consecutive_errors,
                    max_minutes=email_cfg["error_backoff_max_minutes"],
                )
                state["next_run"] = datetime.now(tz) + timedelta(minutes=backoff_minutes)
                threshold_key = (
                    "transient_error_notify_after_consecutive_failures"
                    if is_transient
                    else "error_notify_after_consecutive_failures"
                )
                notify_after = max(1, email_cfg[threshold_key])

                if source.get("auto_source") and status_code in ignore_error_statuses:
                    logger.warning(
                        "Skipping error notification for %s (HTTP %s)",
                        source["name"],
                        status_code,
                    )
                    continue
                if source.get("auto_source") and is_transient:
                    logger.warning(
                        "Skipping transient error for %s; consecutive_failures=%s retry_in=%sm",
                        source["name"],
                        consecutive_errors,
                        backoff_minutes,
                    )
                    continue

                if is_transient and consecutive_errors < notify_after:
                    logger.warning(
                        "Transient error fetching %s: %s; consecutive_failures=%s retry_in=%sm",
                        source["name"],
                        exc,
                        consecutive_errors,
                        backoff_minutes,
                    )
                else:
                    logger.exception("Error fetching %s", source["name"])

                if email_cfg["notify_on_errors"]:
                    cooldown = timedelta(minutes=email_cfg["error_email_cooldown_minutes"])
                    last_notified = state["last_error_notified"]
                    cooldown_elapsed = not last_notified or (now - last_notified) > cooldown
                    if consecutive_errors >= notify_after and cooldown_elapsed:
                        try:
                            error_message = (
                                f"{exc}\n\n"
                                f"Consecutive failures: {consecutive_errors}\n"
                                f"Next retry in approximately {backoff_minutes} minutes."
                            )
                            send_error_notification(email_cfg, source["name"], error_message)
                            state["last_error_notified"] = now
                        except Exception:
                            logger.exception("Failed to send error notification")
                    elif consecutive_errors < notify_after:
                        logger.warning(
                            "Suppressing error email for %s; consecutive_failures=%s threshold=%s retry_in=%sm",
                            source["name"],
                            consecutive_errors,
                            notify_after,
                            backoff_minutes,
                        )
                continue

            if state["consecutive_errors"]:
                logger.info(
                    "Source %s recovered after %s consecutive failure(s)",
                    source["name"],
                    state["consecutive_errors"],
                )
                state["consecutive_errors"] = 0

            new_jobs: List = []
            seeded = skip_first_run and not has_jobs_for_source(conn, source["name"])
            source_seed_recent_hours = int(source.get("seed_recent_hours", seed_recent_hours) or 0)
            source_seed_require_posted_at = bool(source.get("seed_require_posted_at", seed_require_posted_at))
            source_notify_recent_hours = int(source.get("notify_recent_hours", notify_recent_hours) or 0)
            source_notify_require_posted_at = bool(source.get("notify_require_posted_at", notify_require_posted_at))
            for job in jobs:
                normalize_job_posted_at(job, now=now)
                if not matches_filters(
                    job.title,
                    include_patterns=source_include_patterns,
                    exclude_patterns=source_exclude_patterns,
                    location=job.location,
                    url=job.url,
                    us_only=location_cfg.get("us_only", True),
                    allow_remote_without_us_signal=location_cfg.get("allow_remote_without_us_signal", False),
                    assume_us_only=source.get("assume_us_only", False),
                ):
                    continue

                if job_exists(conn, job.job_id):
                    continue

                insert_job(conn, job, first_seen=now)
                if should_notify_seed(
                    seeded=seeded,
                    job=job,
                    now=now,
                    seed_recent_hours=source_seed_recent_hours,
                    seed_require_posted_at=source_seed_require_posted_at,
                ) and should_notify_recent(
                    job=job,
                    now=now,
                    notify_recent_hours=source_notify_recent_hours,
                    notify_require_posted_at=source_notify_require_posted_at,
                ):
                    new_jobs.append(job)

            if new_jobs:
                send_job_notifications(
                    conn=conn,
                    email_cfg=email_cfg,
                    notifications_cfg=notifications_cfg,
                    jobs_by_source=group_jobs_by_source(new_jobs),
                    source_display_map=source_display_map,
                    tz=tz,
                    now=now,
                    context="new-job",
                )

            state["next_run"] = now + timedelta(minutes=state["interval"])

        time_module.sleep(sleep_seconds)
