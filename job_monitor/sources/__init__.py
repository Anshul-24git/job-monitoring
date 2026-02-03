from urllib.parse import urlparse

from .apple import fetch_apple_jobs
from .ashby import fetch_ashby_jobs
from .generic import fetch_generic_jobs
from .greenhouse import fetch_greenhouse_jobs
from .lever import fetch_lever_jobs
from .microsoft import fetch_microsoft_jobs
from .smartrecruiters import fetch_smartrecruiters_jobs
from .workday import fetch_workday_jobs


def detect_kind_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()

    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
    if "myworkdayjobs.com" in host:
        return "workday"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if "apply.careers.microsoft.com" in host or "careers.microsoft.com" in host:
        return "microsoft"
    if "jobs.apple.com" in host:
        return "apple"

    return "generic"


def fetch_jobs_for_source(source: dict, session) -> list:
    kind = source.get("kind") or detect_kind_from_url(source["url"])

    if kind == "greenhouse":
        return fetch_greenhouse_jobs(source, session)
    if kind == "lever":
        return fetch_lever_jobs(source, session)
    if kind == "ashby":
        return fetch_ashby_jobs(source, session)
    if kind == "workday":
        return fetch_workday_jobs(source, session)
    if kind == "smartrecruiters":
        return fetch_smartrecruiters_jobs(source, session)
    if kind == "microsoft":
        return fetch_microsoft_jobs(source, session)
    if kind == "apple":
        return fetch_apple_jobs(source, session)
    if kind == "bamboohr":
        return fetch_generic_jobs(source, session)

    return fetch_generic_jobs(source, session)
