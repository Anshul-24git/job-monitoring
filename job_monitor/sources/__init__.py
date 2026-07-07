from urllib.parse import urlparse

from .adobe import fetch_adobe_jobs
from .amazon import fetch_amazon_jobs
from .apple import fetch_apple_jobs
from .atlassian import fetch_atlassian_jobs
from .ashby import fetch_ashby_jobs
from .bankofamerica import fetch_bankofamerica_jobs
from .capitalone import fetch_capitalone_jobs
from .eightfold import fetch_eightfold_jobs
from .expedia import fetch_expedia_jobs
from .generic import fetch_generic_jobs
from .google import fetch_google_jobs
from .greenhouse import fetch_greenhouse_jobs
from .hnhiring import fetch_hnhiring_jobs
from .highergs import fetch_highergs_jobs
from .lever import fetch_lever_jobs
from .microsoft import fetch_microsoft_jobs
from .oraclecloud import fetch_oraclecloud_jobs
from .playwright_links import fetch_playwright_links_jobs
from .salesforce import fetch_salesforce_jobs
from .simplyhired import fetch_simplyhired_jobs
from .smartrecruiters import fetch_smartrecruiters_jobs
from .uber import fetch_uber_jobs
from .workday import fetch_workday_jobs


def detect_kind_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

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
    if "amazon.jobs" in host and "/search" in path:
        return "amazon"
    if "capitalonecareers.com" in host and path.startswith("/search-jobs"):
        return "capitalone"
    if "atlassian.com" in host and "/company/careers/all-jobs" in path:
        return "atlassian"
    if "careers.bankofamerica.com" in host and "/job-search" in path:
        return "bankofamerica"
    if "eightfold.ai" in host:
        return "eightfold"
    if "jobs.nvidia.com" in host and path.startswith("/careers"):
        return "eightfold"
    if "oraclecloud.com" in host and "/candidateexperience/" in path:
        return "oraclecloud"
    if "higher.gs.com" in host and path.startswith("/results"):
        return "highergs"
    if "google.com/about/careers" in parsed.geturl():
        return "google"
    if "uber.com" in host and ("/careers/list" in path or "/jobs" in path):
        return "uber"
    if "careers.adobe.com" in host:
        return "adobe"
    if "careers.expediagroup.com" in host and path.startswith("/jobs"):
        return "expedia"
    if "careers.salesforce.com" in host and "/jobs" in path:
        return "salesforce"
    if "simplyhired.com" in host and path.startswith("/search"):
        return "simplyhired"
    if "hnhiring.com" in host:
        return "hnhiring"
    if "metacareers.com" in host and "/jobsearch" in path:
        return "playwright_links"

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
    if kind == "eightfold":
        return fetch_eightfold_jobs(source, session)
    if kind == "smartrecruiters":
        return fetch_smartrecruiters_jobs(source, session)
    if kind == "microsoft":
        return fetch_microsoft_jobs(source, session)
    if kind == "apple":
        return fetch_apple_jobs(source, session)
    if kind == "amazon":
        return fetch_amazon_jobs(source, session)
    if kind == "capitalone":
        return fetch_capitalone_jobs(source, session)
    if kind == "bankofamerica":
        return fetch_bankofamerica_jobs(source, session)
    if kind == "oraclecloud":
        return fetch_oraclecloud_jobs(source, session)
    if kind == "highergs":
        return fetch_highergs_jobs(source, session)
    if kind == "google":
        return fetch_google_jobs(source, session)
    if kind == "atlassian":
        return fetch_atlassian_jobs(source, session)
    if kind == "uber":
        return fetch_uber_jobs(source, session)
    if kind == "adobe":
        return fetch_adobe_jobs(source, session)
    if kind == "expedia":
        return fetch_expedia_jobs(source, session)
    if kind == "salesforce":
        return fetch_salesforce_jobs(source, session)
    if kind == "simplyhired":
        return fetch_simplyhired_jobs(source, session)
    if kind == "hnhiring":
        return fetch_hnhiring_jobs(source, session)
    if kind == "playwright_links":
        return fetch_playwright_links_jobs(source, session)
    if kind == "bamboohr":
        return fetch_generic_jobs(source, session)

    return fetch_generic_jobs(source, session)
