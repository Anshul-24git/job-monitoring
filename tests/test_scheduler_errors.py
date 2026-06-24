import unittest

import requests

from job_monitor.scheduler import is_transient_fetch_error, source_error_backoff_minutes


class SchedulerErrorHandlingTests(unittest.TestCase):
    def test_classifies_network_and_playwright_timeouts_as_transient(self):
        cases = [
            requests.ConnectionError("Failed to resolve jobs.example.com"),
            requests.Timeout("read timed out"),
            RuntimeError("Page.goto: Timeout 120000ms exceeded"),
            RuntimeError("BrowserType.launch: Timeout 180000ms exceeded"),
            RuntimeError("Remote end closed connection without response"),
            RuntimeError("Expecting value: line 1 column 1 (char 0)"),
        ]
        for exc in cases:
            with self.subTest(exc=exc):
                self.assertTrue(is_transient_fetch_error(exc))

    def test_does_not_classify_configuration_errors_as_transient(self):
        self.assertFalse(is_transient_fetch_error(ValueError("invalid source configuration")))

    def test_source_backoff_is_exponential_and_capped(self):
        self.assertEqual(source_error_backoff_minutes(interval=30, consecutive_errors=1, max_minutes=360), 30)
        self.assertEqual(source_error_backoff_minutes(interval=30, consecutive_errors=2, max_minutes=360), 60)
        self.assertEqual(source_error_backoff_minutes(interval=30, consecutive_errors=3, max_minutes=360), 120)
        self.assertEqual(source_error_backoff_minutes(interval=30, consecutive_errors=10, max_minutes=360), 360)


if __name__ == "__main__":
    unittest.main()
