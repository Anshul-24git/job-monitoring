import re
from typing import List

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - optional runtime dependency
    sync_playwright = None

from ..models import Job
from ..utils import ensure_absolute_url, hash_job_id, normalize_text, parse_date


DEFAULT_LINK_PATTERNS = [
    "/job/",
    "/jobs/",
    "/roles/",
    "/profile/job_details/",
    "/en/job/",
    "jobid=",
    "gh_jid=",
]

DEFAULT_EXCLUDE_TEXT = {
    "apply",
    "apply now",
    "join our talent community",
    "search jobs",
    "saved jobs",
    "jobs",
}

TITLE_LOCATION_COMPACT_RE = re.compile(
    r"^(?P<title>.+?)(?P<location>[A-Z][A-Za-z .&'/-]+,\s*(?:[A-Z]{2}(?:\s*\+\d+\s*locations?)?|[A-Za-z][A-Za-z .'-]+))$"
)
LOCATION_LABELED_RE = re.compile(r"\bLocation:\s*(?P<location>.+)$", re.I)
LOCATION_LINE_RE = re.compile(
    r"\b(remote|united states|usa|u\.s\.|[A-Z][A-Za-z .'-]+,\s*[A-Z]{2}\b|"
    r"[A-Z][A-Za-z .'-]+,\s*[A-Za-z][A-Za-z .'-]+|"
    r"United States\s*-\s*[A-Za-z .'-]+\s*-\s*[A-Za-z .'-]+)\b",
    re.I,
)
POSTED_LABELED_RE = re.compile(r"\b(?:posted|date posted|posted on|updated):\s*(?P<value>.+)$", re.I)
DATE_TEXT_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})\b"
)


def _infer_location_from_url(url: str) -> str:
    match = re.search(r"/job/(Virtual-US|US-[A-Za-z]+-[A-Za-z-]+)(?:/|$)", url)
    if not match:
        return ""

    token = match.group(1)
    if token == "Virtual-US":
        return "Remote, United States"

    parts = [part for part in token.split("-") if part]
    if len(parts) < 3 or parts[0] != "US":
        return ""

    region = parts[1].replace("_", " ")
    city = " ".join(parts[2:]).replace("_", " ")
    return normalize_text(f"{city}, {region}, United States")


def _extract_title_and_location(raw_text: str) -> tuple[str, str]:
    text = normalize_text(raw_text)
    if not text:
        return "", ""

    if "⋅" in text:
        first_segment = normalize_text(text.split("⋅", 1)[0])
        match = TITLE_LOCATION_COMPACT_RE.match(first_segment)
        if match:
            title = normalize_text(match.group("title"))
            location = normalize_text(match.group("location"))
            return title, location
        return first_segment, ""

    lines = [normalize_text(part) for part in re.split(r"[\r\n]+", raw_text) if normalize_text(part)]
    if not lines:
        return "", ""

    title = lines[0]
    location = ""
    for candidate in lines[1:4]:
        if re.search(r",\s*[A-Z]{2}\b|,\s*[A-Za-z]{3,}|United States|Remote", candidate, re.I):
            location = candidate
            break
    return title, location


def _split_text_lines(text: str) -> list[str]:
    return [normalize_text(part) for part in re.split(r"[\r\n]+", text or "") if normalize_text(part)]


def _extract_context_location(lines: list[str], title: str) -> str:
    for line in lines:
        if normalize_text(line).lower() == normalize_text(title).lower():
            continue
        match = LOCATION_LABELED_RE.search(line)
        if match:
            return normalize_text(match.group("location"))
        if LOCATION_LINE_RE.search(line):
            return normalize_text(line)
    return ""


def _extract_context_posted(lines: list[str]) -> tuple[str, bool]:
    for line in lines:
        match = POSTED_LABELED_RE.search(line)
        if match:
            value = normalize_text(match.group("value"))
            return value, bool(DATE_TEXT_RE.search(value) and ":" not in value and "T" not in value and "t" not in value)
        match = DATE_TEXT_RE.search(line)
        if match:
            value = normalize_text(match.group(0))
            return value, True
    return "", False


def fetch_playwright_links_jobs(source: dict, session) -> List[Job]:
    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed. Run: python -m playwright install chromium")

    source_url = source["url"]
    timeout_ms = int(source.get("playwright_timeout_ms", 120_000))
    wait_ms = int(source.get("playwright_wait_ms", 7_000))
    scroll_rounds = int(source.get("playwright_scroll_rounds", 2))
    scroll_wait_ms = int(source.get("playwright_scroll_wait_ms", 1_500))
    load_more_clicks = int(source.get("playwright_load_more_clicks", 3))
    headless = bool(source.get("playwright_headless", True))

    link_patterns = [pattern.lower() for pattern in source.get("playwright_link_patterns", DEFAULT_LINK_PATTERNS)]
    exclude_text_patterns = {normalize_text(x).lower() for x in source.get("playwright_exclude_text", [])}
    exclude_text_patterns |= DEFAULT_EXCLUDE_TEXT

    with sync_playwright() as p:
        browser = None
        try:
            browser = p.chromium.launch(headless=headless)
            page = browser.new_page(viewport={"width": 1440, "height": 2200})
            page.goto(source_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(wait_ms)

            for _ in range(max(scroll_rounds, 0)):
                page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(scroll_wait_ms)

            for _ in range(max(load_more_clicks, 0)):
                clicked_more = page.evaluate(
                    """
                    () => {
                      const tokens = ['load more', 'show more', 'view more', 'more jobs', 'see more', 'show next'];
                      const nodes = Array.from(document.querySelectorAll('button, a[role="button"], a'));
                      for (const node of nodes) {
                        const text = ((node.innerText || node.textContent || '').trim().toLowerCase());
                        if (!text) continue;
                        if (!tokens.some((token) => text.includes(token))) continue;
                        if (node.hasAttribute('disabled') || node.getAttribute('aria-disabled') === 'true') continue;
                        node.click();
                        return true;
                      }
                      return false;
                    }
                    """
                )
                if not clicked_more:
                    break
                page.wait_for_timeout(scroll_wait_ms)
                page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(scroll_wait_ms)

            anchors = page.evaluate(
                """
                () => {
                  const out = [];
                  for (const a of document.querySelectorAll('a[href]')) {
                    const href = (a.getAttribute('href') || '').trim();
                    const text = (a.innerText || a.textContent || '').trim();
                    if (!href || !text) continue;
                    let card = a.closest('article, li, section, tr, div[class*="job"], div[data-job-id], div[data-automation-id]');
                    if (!card) card = a.parentElement;
                    const cardText = ((card?.innerText || card?.textContent || '')).trim();
                    out.push({
                      href,
                      text,
                      cardText,
                      ariaLabel: (a.getAttribute('aria-label') || '').trim(),
                      titleAttr: (a.getAttribute('title') || '').trim(),
                    });
                  }
                  return out;
                }
                """
            )
            current_page_url = page.url
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

    jobs: List[Job] = []
    seen = set()

    for entry in anchors:
        href = normalize_text(entry.get("href"))
        text = entry.get("text") or ""
        if not href or not text:
            continue

        href_lower = href.lower()
        if not any(pattern in href_lower for pattern in link_patterns):
            continue

        title, location = _extract_title_and_location(text)
        if not title:
            continue
        if normalize_text(title).lower() in exclude_text_patterns:
            continue

        card_lines = _split_text_lines(entry.get("cardText") or "")
        if not location:
            location = _extract_context_location(card_lines, title)
        posted_text, posted_at_is_date_only = _extract_context_posted(card_lines)

        url = ensure_absolute_url(current_page_url, href)
        if not location:
            location = _infer_location_from_url(url)
        job_id = hash_job_id(source["name"], url)
        if job_id in seen:
            continue
        seen.add(job_id)

        jobs.append(
            Job(
                job_id=job_id,
                source=source["name"],
                title=title,
                location=location,
                url=url,
                posted_at=parse_date(posted_text) if posted_text else None,
                raw={
                    "source": "playwright_links",
                    "posted_at_text": posted_text,
                    "posted_at_is_date_only": posted_at_is_date_only,
                },
            )
        )

    return jobs
