import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from job_monitor.db import (
    count_due_pending_notifications,
    get_pending_notifications,
    init_db,
    insert_job,
    mark_notification_failed,
    mark_notification_pending,
    mark_notified,
    recover_recent_unnotified_notifications,
)
from job_monitor.models import Job
from job_monitor.notify import OutboundEmail, send_email_batch


class NotificationQueueTests(unittest.TestCase):
    def test_pending_notification_lifecycle_and_recovery(self):
        tz = ZoneInfo("America/Chicago")
        now = datetime(2026, 6, 9, 10, 0, tzinfo=tz)
        last_success = now - timedelta(hours=1)
        stranded_seen = now - timedelta(minutes=30)
        old_seed_seen = now - timedelta(days=4)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "jobs.db")
            conn = init_db(db_path)
            self._insert_row(conn, "sent", notified_at=last_success, first_seen=last_success)
            self._insert_row(conn, "stranded", notified_at=None, first_seen=stranded_seen)
            self._insert_row(conn, "old-seed", notified_at=None, first_seen=old_seed_seen)

            recovered = recover_recent_unnotified_notifications(conn, now=now, lookback_hours=48)
            self.assertEqual(recovered, 1)
            self.assertEqual(count_due_pending_notifications(conn, now=now), 1)

            pending = get_pending_notifications(conn, now=now, limit=10)
            self.assertEqual([job.job_id for job in pending], ["stranded"])

            mark_notification_failed(
                conn,
                ["stranded"],
                error="smtp disconnected",
                next_notify_at=now + timedelta(minutes=10),
            )
            self.assertEqual(count_due_pending_notifications(conn, now=now), 0)
            self.assertEqual(count_due_pending_notifications(conn, now=now + timedelta(minutes=11)), 1)

            mark_notified(conn, ["stranded"], notified_at=now + timedelta(minutes=11))
            self.assertEqual(count_due_pending_notifications(conn, now=now + timedelta(hours=1)), 0)

    def test_recovery_suppresses_stale_source_posted_jobs(self):
        tz = ZoneInfo("America/Chicago")
        now = datetime(2026, 7, 7, 9, 30, tzinfo=tz)
        last_success = now - timedelta(minutes=20)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "jobs.db")
            conn = init_db(db_path)
            self._insert_row(conn, "sent", notified_at=last_success, first_seen=last_success)
            self._insert_row(
                conn,
                "fresh",
                source="SimplyHired",
                notified_at=None,
                first_seen=now - timedelta(minutes=5),
                posted_at=now - timedelta(hours=2),
            )
            self._insert_row(
                conn,
                "stale",
                source="SimplyHired",
                notified_at=None,
                first_seen=now - timedelta(minutes=5),
                posted_at=now - timedelta(days=30),
            )

            recovered = recover_recent_unnotified_notifications(
                conn,
                now=now,
                lookback_hours=48,
                source_notify_policies={
                    "SimplyHired": {
                        "notify_recent_hours": 72,
                        "notify_require_posted_at": True,
                    }
                },
            )

            self.assertEqual(recovered, 1)
            pending = get_pending_notifications(conn, now=now, limit=10)
            self.assertEqual([job.job_id for job in pending], ["fresh"])

            stale_row = conn.execute("SELECT notified_at, notify_pending FROM jobs WHERE job_id = 'stale'").fetchone()
            self.assertIsNotNone(stale_row[0])
            self.assertEqual(stale_row[1], 0)

    def test_pending_notifications_preserve_posted_display_metadata(self):
        tz = ZoneInfo("America/Chicago")
        now = datetime(2026, 7, 7, 9, 30, tzinfo=tz)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "jobs.db")
            conn = init_db(db_path)
            insert_job(
                conn,
                Job(
                    job_id="seen-job",
                    source="TestCo",
                    title="Software Engineer",
                    location="Remote",
                    url="https://example.com/seen-job",
                    posted_at=now,
                    raw={
                        "posted_at_display_label": "seen",
                        "posted_at_display_date_only": False,
                        "posted_at_fallback": True,
                    },
                ),
                first_seen=now,
            )
            mark_notification_pending(conn, ["seen-job"])

            [pending] = get_pending_notifications(conn, now=now, limit=10)
            self.assertEqual(pending.raw["posted_at_display_label"], "seen")
            self.assertFalse(pending.raw["posted_at_display_date_only"])
            self.assertTrue(pending.raw["posted_at_fallback"])

    def _insert_row(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        *,
        notified_at,
        first_seen,
        source="TestCo",
        posted_at=None,
    ):
        posted_at = posted_at or first_seen
        conn.execute(
            """
            INSERT INTO jobs (job_id, source, url, title, location, posted_at, first_seen, notified_at)
            VALUES (?, ?, ?, 'Software Engineer', 'United States', ?, ?, ?)
            """,
            (
                job_id,
                source,
                f"https://example.com/{job_id}",
                posted_at.isoformat(),
                first_seen.isoformat(),
                notified_at.isoformat() if notified_at else None,
            ),
        )
        conn.commit()


class SendEmailBatchTests(unittest.TestCase):
    def test_retries_after_transient_login_failure(self):
        attempts = {"count": 0, "sent": 0}

        class FakeSMTP:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def starttls(self):
                pass

            def login(self, user, password):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise OSError("temporary smtp disconnect")

            def send_message(self, msg, to_addrs):
                attempts["sent"] += 1

        import job_monitor.notify as notify

        original_smtp = notify.smtplib.SMTP
        notify.smtplib.SMTP = FakeSMTP
        try:
            errors = send_email_batch(
                smtp_host="smtp.example.com",
                smtp_port=587,
                user="user",
                app_password="password",
                sender="sender@example.com",
                messages=[OutboundEmail(recipient=["to@example.com"], subject="Test", body="Body")],
                max_attempts=2,
                initial_retry_delay_seconds=0,
            )
        finally:
            notify.smtplib.SMTP = original_smtp

        self.assertEqual(errors, {})
        self.assertEqual(attempts["count"], 2)
        self.assertEqual(attempts["sent"], 1)


if __name__ == "__main__":
    unittest.main()
