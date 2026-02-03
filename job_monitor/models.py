from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class Job:
    job_id: str
    source: str
    title: str
    location: str
    url: str
    posted_at: Optional[datetime]
    raw: Dict[str, Any]
