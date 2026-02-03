# Job Monitor

Monitor company job boards for US-based software engineering roles and get notified by email when new jobs appear.

## Features
- Supports Greenhouse, Lever, Workday, SmartRecruiters, Ashby (best-effort), plus generic HTML/JSON-LD pages
- Per-source polling intervals
- US-only filtering with optional remote support
- Keyword include/exclude filtering
- SQLite de-duplication so each job is notified once
- Runs only during your active hours (default: 9 AM–7 PM America/Chicago)

## Quick Start
1. Create and activate a Python venv (optional but recommended).
2. `pip install -r requirements.txt`
3. `cp config.example.yaml config.yaml`
4. Set your Gmail App Password as an env var:
   - `export JOB_MONITOR_GMAIL_APP_PASSWORD="your_app_password"`
5. Run:
   - `python -m job_monitor --config config.yaml`

Optional: you can also put the app password in a `.env` file and set `env_file: .env` in config.

### One-time run (no scheduler)
- `python -m job_monitor --config config.yaml --once`

## Gmail App Password
Use a Gmail App Password (not your main Gmail password). You can create one in your Google account security settings after enabling 2FA.

## Notes
- If a portal does not expose a posted date, the system uses `first_seen`.
- For custom portals, you can add selectors in `config.yaml` or rely on JSON-LD JobPosting data if present.
- Errors from a single source do not stop the overall loop.
- Set `notifications.skip_first_run: true` to seed the database without sending a first-run email flood.
- Use `notifications.seed_recent_hours` to allow notifications for jobs posted recently even on a source's first run.
- Set `notifications.seed_require_posted_at: true` to only send seed notifications when a posted date is available.
- Use `notifications.ignore_error_statuses` to suppress noisy 404/410 emails for auto-generated sources.
- Auto-sources can provide a nicer email name via `display_name` (default uses a prettified slug).

## Auto Sources (ATS Slugs)
You can generate sources from ATS slugs by adding an `auto_sources.yaml` file and pointing to it in config:

```
auto_sources_file: auto_sources.yaml
```

Example `auto_sources.yaml`:
```
greenhouse:
  - stripe
  - databricks
ashby:
  - openai
lever:
  - scaleai
smartrecruiters:
  - discord
bamboohr:
  - gitlab
```

These are expanded into ATS URLs automatically and deduplicated against existing manual sources.

## Dashboard (Local)
Run a lightweight dashboard that reads from `job_monitor.db`, `logs/diagnostics.json`, and `logs/job-monitor.log`:

```bash
cd "/Users/anshuljoshi/UT Drive/Projects/job-monitoring"
source .venv/bin/activate
pip install -r requirements.txt
uvicorn dashboard.app:app --reload --port 8080
```

Then open `http://127.0.0.1:8080` in your browser.

Optional environment overrides:
- `JOB_MONITOR_DB_PATH` (default: `job_monitor.db`)
- `JOB_MONITOR_DIAGNOSTICS_PATH` (default: `logs/diagnostics.json`)
- `JOB_MONITOR_LOG_PATH` (default: `logs/job-monitor.log`)
- `JOB_MONITOR_CONFIG` (default: `config.yaml`)
