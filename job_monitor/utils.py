import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin

from dateutil import parser as date_parser


def hash_job_id(source: str, url: str) -> str:
    payload = f"{source}::{url}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalize_text(text: Optional[str]) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def parse_date(value: Any) -> Optional[datetime]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        try:
            # Heuristic: treat large numbers as milliseconds
            if value > 10_000_000_000:
                value = value / 1000.0
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except Exception:
            return None

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            dt = date_parser.parse(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    return None


def ensure_absolute_url(base_url: str, url: str) -> str:
    if not url:
        return url
    return urljoin(base_url, url)


def extract_jsonld_objects(raw_text: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    try:
        payload = json.loads(raw_text)
    except Exception:
        return items

    def _walk(node: Any) -> Iterable[Dict[str, Any]]:
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from _walk(value)
        elif isinstance(node, list):
            for child in node:
                yield from _walk(child)

    for obj in _walk(payload):
        if not isinstance(obj, dict):
            continue
        obj_type = obj.get("@type")
        if obj_type == "JobPosting":
            items.append(obj)
        elif isinstance(obj_type, list) and "JobPosting" in obj_type:
            items.append(obj)

    return items
