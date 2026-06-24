import smtplib
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Dict, List

from .models import Job


@dataclass
class OutboundEmail:
    recipient: str | List[str]
    subject: str
    body: str


def format_job_line(job: Job, *, tz=None) -> str:
    if job.posted_at:
        posted_at = job.posted_at
        if tz:
            posted_at = posted_at.astimezone(tz)
        posted = posted_at.strftime("%Y-%m-%d | %H:%M %Z")
    else:
        posted = "unknown"
    location = job.location or "unknown"
    return f"- {job.title} | {location} | posted: {posted}\n  {job.url}"


def build_jobs_email(jobs: List[Job], header: str, *, tz=None) -> str:
    lines = [header, ""]
    for job in jobs:
        lines.append(format_job_line(job, tz=tz))
    lines.append("")
    lines.append("You are receiving this because it matched your filters.")
    return "\n".join(lines)


def build_error_email(source_name: str, error_message: str) -> str:
    lines = [f"Error while fetching {source_name}:", "", error_message]
    return "\n".join(lines)


def send_email(
    *,
    smtp_host: str,
    smtp_port: int,
    user: str,
    app_password: str,
    sender: str,
    recipient: str | List[str],
    subject: str,
    body: str,
) -> None:
    errors = send_email_batch(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        user=user,
        app_password=app_password,
        sender=sender,
        messages=[OutboundEmail(recipient=recipient, subject=subject, body=body)],
    )
    if errors:
        raise next(iter(errors.values()))


def send_email_batch(
    *,
    smtp_host: str,
    smtp_port: int,
    user: str,
    app_password: str,
    sender: str,
    messages: List[OutboundEmail],
    max_attempts: int = 3,
    initial_retry_delay_seconds: int = 5,
) -> Dict[int, Exception]:
    """Send a batch of messages with SMTP retries.

    Returns a mapping of message index -> last exception for messages that did
    not send. Successful message indexes are omitted from the result.
    """
    if not messages:
        return {}

    max_attempts = max(1, int(max_attempts))
    initial_retry_delay_seconds = max(0, int(initial_retry_delay_seconds))
    pending = list(range(len(messages)))
    last_errors: Dict[int, Exception] = {}

    for attempt in range(max_attempts):
        next_pending: List[int] = []
        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                server.starttls()
                server.login(user, app_password)

                for position, message_index in enumerate(pending):
                    msg = _build_message(sender, messages[message_index])
                    try:
                        server.send_message(msg, to_addrs=_normalize_recipients(messages[message_index].recipient))
                    except Exception as exc:
                        for failed_index in pending[position:]:
                            last_errors[failed_index] = exc
                        next_pending.extend(pending[position:])
                        break
        except Exception as exc:
            for message_index in pending:
                last_errors[message_index] = exc
            next_pending = pending[:]

        pending = next_pending
        if not pending:
            return {}

        if attempt < max_attempts - 1 and initial_retry_delay_seconds:
            time.sleep(initial_retry_delay_seconds * (2 ** attempt))

    return {message_index: last_errors[message_index] for message_index in pending}


def _build_message(sender: str, payload: OutboundEmail) -> EmailMessage:
    recipients = _normalize_recipients(payload.recipient)
    if not recipients:
        raise ValueError("No recipients provided")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = payload.subject
    msg.set_content(payload.body)
    return msg


def _normalize_recipients(recipient: str | List[str]) -> List[str]:
    if isinstance(recipient, str):
        return [part.strip() for part in recipient.split(",") if part.strip()]
    return [part.strip() for part in recipient if isinstance(part, str) and part.strip()]
