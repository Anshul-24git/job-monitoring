from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.app import (
    DEFAULT_DB,
    DEFAULT_DIAGNOSTICS,
    DEFAULT_PUBLIC_SNAPSHOT,
    _fetch_db_stats,
    _load_config_summary,
    _load_diagnostics,
)


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return _iso(value)


def _sanitize_diagnostics(diagnostics: Dict[str, Any]) -> Dict[str, Any]:
    if not diagnostics:
        return {}
    results = []
    for result in diagnostics.get("results", []):
        results.append(
            {
                "name": result.get("name"),
                "display_name": result.get("display_name"),
                "status": result.get("status"),
                "raw_jobs": result.get("raw_jobs"),
                "matched_jobs": result.get("matched_jobs"),
                "duration_seconds": result.get("duration_seconds"),
            }
        )
    return {
        "summary": diagnostics.get("summary", {}),
        "results": results,
    }


def main() -> None:
    config_summary = _load_config_summary()
    stats = _fetch_db_stats(
        DEFAULT_DB,
        source_display_map=config_summary.get("source_display_map", {}),
    )
    diagnostics = _sanitize_diagnostics(_load_diagnostics(DEFAULT_DIAGNOSTICS))

    # Do not publish raw log lines or local scheduler paths in the public demo.
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_summary": {
            "timezone": config_summary.get("timezone"),
            "active_start": config_summary.get("active_start"),
            "active_end": config_summary.get("active_end"),
            "default_interval": config_summary.get("default_interval"),
            "sources_count": config_summary.get("sources_count"),
            "source_display_map": config_summary.get("source_display_map", {}),
        },
        "stats": stats,
        "diagnostics": diagnostics,
        "log_tail": ["Raw local logs are intentionally omitted from the public demo."],
    }

    DEFAULT_PUBLIC_SNAPSHOT.write_text(
        json.dumps(_json_safe(snapshot), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"Wrote {DEFAULT_PUBLIC_SNAPSHOT}")


if __name__ == "__main__":
    main()
