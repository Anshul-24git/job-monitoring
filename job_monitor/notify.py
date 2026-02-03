import smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import Iterable, List

from .models import Job


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
    recipient: str,
    subject: str,
    body: str,
) -> None:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(user, app_password)
        server.send_message(msg)
