import argparse
import os

from .config import ConfigError, load_config
from .diagnostics import run_diagnostics
from .logging_setup import setup_logging
from .scheduler import run_once, run_scheduler


def main() -> None:
    parser = argparse.ArgumentParser(description="Job monitoring tool")
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    parser.add_argument("--db", default="job_monitor.db", help="Path to sqlite db file")
    parser.add_argument("--log", default=os.path.join("logs", "job-monitor.log"), help="Log file path")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--diagnose", action="store_true", help="Run diagnostics and exit")
    parser.add_argument(
        "--diagnostics-out",
        default=os.path.join("logs", "diagnostics.json"),
        help="Path to diagnostics output JSON",
    )

    args = parser.parse_args()

    log_dir = os.path.dirname(args.log)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    setup_logging(args.log)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        raise SystemExit(f"Config error: {exc}")

    if args.diagnose:
        report = run_diagnostics(cfg, output_path=args.diagnostics_out)
        summary = report["summary"]
        print(
            "Diagnostics complete. Sources: {total} (ok={ok}, error={err}), "
            "raw_jobs={raw}, matched_jobs={matched}. Report: {path}".format(
                total=summary["total_sources"],
                ok=summary["ok_sources"],
                err=summary["error_sources"],
                raw=summary["total_raw_jobs"],
                matched=summary["total_matched_jobs"],
                path=args.diagnostics_out,
            )
        )
    elif args.once:
        run_once(cfg, db_path=args.db)
    else:
        run_scheduler(cfg, db_path=args.db)


if __name__ == "__main__":
    main()
