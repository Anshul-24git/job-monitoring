import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

import requests
from zoneinfo import ZoneInfo

from .filters import compile_keywords, extend_patterns, matches_filters
from .http_utils import build_session
from .sources import detect_kind_from_url, fetch_jobs_for_source


def run_diagnostics(cfg: Dict[str, Any], *, output_path: str | None = None) -> Dict[str, Any]:
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    include_patterns = compile_keywords(cfg["filters"]["include_keywords"])
    exclude_patterns = compile_keywords(cfg["filters"]["exclude_keywords"])
    location_cfg = cfg["filters"]["location"]

    session = build_session()

    results: List[Dict[str, Any]] = []
    for source in cfg["sources"]:
        started = time.perf_counter()
        result: Dict[str, Any] = {
            "name": source["name"],
            "display_name": source.get("display_name", source["name"]),
            "url": source["url"],
            "kind": source.get("kind") or detect_kind_from_url(source["url"]),
            "interval_minutes": source.get("interval_minutes", cfg["schedule"]["default_interval_minutes"]),
            "assume_us_only": bool(source.get("assume_us_only", False)),
            "auto_source": bool(source.get("auto_source", False)),
        }

        try:
            jobs = fetch_jobs_for_source(source, session)
            source_include_patterns = extend_patterns(include_patterns, source.get("include_keywords", []))
            source_exclude_patterns = extend_patterns(exclude_patterns, source.get("exclude_keywords", []))
            matched = 0
            for job in jobs:
                if matches_filters(
                    job.title,
                    include_patterns=source_include_patterns,
                    exclude_patterns=source_exclude_patterns,
                    location=job.location,
                    url=job.url,
                    us_only=location_cfg.get("us_only", True),
                    allow_remote_without_us_signal=location_cfg.get("allow_remote_without_us_signal", False),
                    assume_us_only=source.get("assume_us_only", False),
                ):
                    matched += 1

            result.update({
                "status": "ok",
                "raw_jobs": len(jobs),
                "matched_jobs": matched,
            })
        except Exception as exc:
            result["status"] = "error"
            result["error"] = str(exc)
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                result["http_status"] = exc.response.status_code
            result["error_type"] = exc.__class__.__name__

        result["duration_seconds"] = round(time.perf_counter() - started, 3)
        results.append(result)

    summary = _summarize_results(results)
    report = {
        "generated_at": datetime.now(tz).isoformat(),
        "summary": summary,
        "results": results,
    }

    if output_path:
        _write_report(output_path, report)

    return report


def _summarize_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_sources = len(results)
    ok_sources = [r for r in results if r.get("status") == "ok"]
    error_sources = [r for r in results if r.get("status") == "error"]

    total_raw = sum(r.get("raw_jobs", 0) for r in ok_sources)
    total_matched = sum(r.get("matched_jobs", 0) for r in ok_sources)

    by_kind: Dict[str, Dict[str, int]] = {}
    for r in results:
        kind = r.get("kind") or "unknown"
        bucket = by_kind.setdefault(kind, {"total": 0, "ok": 0, "error": 0})
        bucket["total"] += 1
        if r.get("status") == "ok":
            bucket["ok"] += 1
        else:
            bucket["error"] += 1

    return {
        "total_sources": total_sources,
        "ok_sources": len(ok_sources),
        "error_sources": len(error_sources),
        "total_raw_jobs": total_raw,
        "total_matched_jobs": total_matched,
        "by_kind": by_kind,
    }


def _write_report(path: str, report: Dict[str, Any]) -> None:
    output_dir = os.path.dirname(path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
