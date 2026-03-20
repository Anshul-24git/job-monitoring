import json
import os
import re
from typing import Any, Iterable, List
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..models import Job
from ..utils import ensure_absolute_url, extract_jsonld_objects, hash_job_id, normalize_text, parse_date

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - optional runtime dependency
    PlaywrightError = RuntimeError
    PlaywrightTimeoutError = RuntimeError
    sync_playwright = None


JOB_PATH_RE = re.compile(r"/jobs?/[^/?#]+", re.I)
VIEW_JOB_TITLES = {
    "view job",
    "search jobs",
    "job search",
    "learn more",
}
TITLE_KEYS = ("title", "jobTitle", "name", "positionTitle", "postingTitle")
URL_KEYS = (
    "url",
    "jobUrl",
    "applyUrl",
    "externalUrl",
    "jobPostingUrl",
    "postingUrl",
    "detailUrl",
    "jobDetailUrl",
    "canonicalUrl",
    "link",
)
LOCATION_KEYS = ("location", "locationsText", "locationName", "cityStateCountry", "cityState", "city")
DATE_KEYS = ("datePosted", "postedAt", "postingDate", "updatedAt", "createdAt", "publishedAt")


def _is_probable_job_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    full = url.lower()
    if parsed.scheme not in {"http", "https"}:
        return False
    if "javascript:" in full:
        return False
    if not JOB_PATH_RE.search(path):
        return False
    if path.rstrip("/") in {"/job", "/jobs"}:
        return False
    if "filter%5b" in full or "filter[" in full:
        return False
    return True


def _extract_country(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("name", "") or value.get("@id", "") or ""
    return ""


def _extract_location_from_jsonld(obj: dict) -> str:
    loc = obj.get("jobLocation")
    if isinstance(loc, list):
        parts = []
        for entry in loc:
            address = entry.get("address", {}) if isinstance(entry, dict) else {}
            parts.append(
                normalize_text(
                    ", ".join(
                        [
                            address.get("addressLocality", ""),
                            address.get("addressRegion", ""),
                            _extract_country(address.get("addressCountry")),
                        ]
                    )
                )
            )
        return normalize_text("; ".join([p for p in parts if p]))
    if isinstance(loc, dict):
        address = loc.get("address", {})
        return normalize_text(
            ", ".join(
                [
                    address.get("addressLocality", ""),
                    address.get("addressRegion", ""),
                    _extract_country(address.get("addressCountry")),
                ]
            )
        )
    return ""


def _extract_jobs_from_rendered_jsonld(source: dict, page_url: str, html: str) -> List[Job]:
    soup = BeautifulSoup(html, "lxml")
    jobs: List[Job] = []
    seen = set()

    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not tag.string:
            continue
        for obj in extract_jsonld_objects(tag.string):
            title = normalize_text(obj.get("title") or obj.get("name"))
            url = normalize_text(obj.get("url"))
            if not title or not url:
                continue
            url = ensure_absolute_url(page_url, url)
            if not _is_probable_job_url(url):
                continue
            job_id = hash_job_id(source["name"], url)
            if job_id in seen:
                continue
            seen.add(job_id)
            jobs.append(
                Job(
                    job_id=job_id,
                    source=source["name"],
                    title=title,
                    location=_extract_location_from_jsonld(obj),
                    url=url,
                    posted_at=parse_date(obj.get("datePosted") or obj.get("validFrom")),
                    raw=obj,
                )
            )
    return jobs


def _iter_candidate_jobs(node: Any) -> Iterable[dict]:
    if isinstance(node, dict):
        title = next((node.get(k) for k in TITLE_KEYS if node.get(k)), None)
        url = next((node.get(k) for k in URL_KEYS if node.get(k)), None)
        if title and (url or node.get("jobId") or node.get("id")):
            yield node
        for value in node.values():
            yield from _iter_candidate_jobs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_candidate_jobs(item)


def _format_location(value: Any) -> str:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, dict):
        return normalize_text(
            ", ".join(
                [
                    normalize_text(value.get("city")),
                    normalize_text(value.get("region") or value.get("state")),
                    normalize_text(value.get("countryName") or value.get("country")),
                ]
            )
        )
    if isinstance(value, list):
        parts = [_format_location(item) for item in value]
        return normalize_text("; ".join([p for p in parts if p]))
    return ""


def _parse_job_item(source: dict, page_url: str, item: dict) -> Job | None:
    title = normalize_text(next((item.get(k) for k in TITLE_KEYS if item.get(k)), ""))
    if not title:
        return None
    if title.lower() in VIEW_JOB_TITLES:
        return None

    raw_url = next((item.get(k) for k in URL_KEYS if item.get(k)), "")
    url = normalize_text(raw_url)
    if not url:
        return None
    url = ensure_absolute_url(page_url, url)
    if not _is_probable_job_url(url):
        return None

    location = ""
    for key in LOCATION_KEYS:
        value = item.get(key)
        if value:
            location = _format_location(value)
            if location:
                break

    posted_at = None
    for key in DATE_KEYS:
        value = item.get(key)
        if value:
            posted_at = parse_date(value)
            if posted_at:
                break

    return Job(
        job_id=hash_job_id(source["name"], url),
        source=source["name"],
        title=title,
        location=location,
        url=url,
        posted_at=posted_at,
        raw=item,
    )


def _extract_jobs_from_captured_json(source: dict, page_url: str, payloads: List[dict]) -> List[Job]:
    jobs: List[Job] = []
    seen = set()
    for payload in payloads:
        for item in _iter_candidate_jobs(payload):
            job = _parse_job_item(source, page_url, item)
            if not job:
                continue
            if job.job_id in seen:
                continue
            seen.add(job.job_id)
            jobs.append(job)
    return jobs


def _extract_jobs_from_embedded_json(source: dict, page_url: str, html: str) -> List[Job]:
    soup = BeautifulSoup(html, "lxml")
    jobs: List[Job] = []
    seen = set()
    for tag in soup.find_all("script", attrs={"type": "application/json"}):
        if not tag.string:
            continue
        try:
            payload = tag.string.strip()
            if not payload:
                continue
            import json
            data = json.loads(payload)
        except Exception:
            continue
        for item in _iter_candidate_jobs(data):
            job = _parse_job_item(source, page_url, item)
            if not job:
                continue
            if job.job_id in seen:
                continue
            seen.add(job.job_id)
            jobs.append(job)
    return jobs


def _dismiss_cookie_banner(page) -> None:
    selectors = [
        "button:has-text('Accept All')",
        "button:has-text('Accept all')",
        "button:has-text('I Accept')",
        "button:has-text('Allow All')",
        "button:has-text('Agree')",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator and locator.is_visible(timeout=600):
                locator.click(timeout=1200)
                page.wait_for_timeout(300)
                return
        except Exception:
            continue


def _expand_results(page, *, max_actions: int, wait_ms: int) -> None:
    stable_steps = 0
    for _ in range(max_actions):
        clicked_more = False
        try:
            clicked_more = bool(
                page.evaluate(
                    """
                    () => {
                      const tokens = ['load more', 'show more', 'view more', 'more jobs', 'see more'];
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
            )
        except Exception:
            clicked_more = False

        try:
            before = int(page.evaluate("() => document.body.scrollHeight"))
        except Exception:
            before = 0
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(wait_ms)
        try:
            after = int(page.evaluate("() => document.body.scrollHeight"))
        except Exception:
            after = before

        if clicked_more or after > before:
            stable_steps = 0
        else:
            stable_steps += 1

        if stable_steps >= 3:
            break


def _extract_jobs_from_rendered_dom(source: dict, page) -> List[Job]:
    records = page.evaluate(
        """
        () => {
          const collectRoots = () => {
            const roots = [document];
            for (let i = 0; i < roots.length; i++) {
              const root = roots[i];
              const elems = Array.from(root.querySelectorAll('*'));
              for (const el of elems) {
                if (el.shadowRoot) roots.push(el.shadowRoot);
              }
            }
            return roots;
          };

          const roots = collectRoots();
          const queryAllDeep = (selector) => {
            const nodes = [];
            for (const root of roots) {
              nodes.push(...Array.from(root.querySelectorAll(selector)));
            }
            return nodes;
          };

          const isLocationLine = (value) => {
            if (!value) return false;
            return /united states|remote|alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|district of columbia|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming/i.test(value);
          };

          const toText = (node) => ((node && (node.innerText || node.textContent)) || '').trim();
          const cleanUrl = (value) => {
            if (!value) return '';
            let raw = String(value).trim();
            if (!raw || raw.startsWith('javascript:') || raw === '#') return '';
            const match = raw.match(/https?:\\/\\/[^\\s"']+|\\/jobs?\\/[^\\s"']+|\\/job\\/[^\\s"']+/i);
            raw = match ? match[0] : raw;
            try {
              return new URL(raw, window.location.origin).href;
            } catch (err) {
              return '';
            }
          };
          const isJobUrl = (url) => {
            const lower = (url || '').toLowerCase();
            if (!lower) return false;
            if (!(lower.includes('/job/') || lower.includes('/jobs/'))) return false;
            if (lower.endsWith('/jobs/') || lower.endsWith('/job/')) return false;
            if (lower.includes('/jobs/?') || lower.includes('/job/?')) return false;
            if (lower.includes('filter%5b') || lower.includes('filter[')) return false;
            return true;
          };
          const pickUrlFromNode = (node) => {
            if (!node) return '';
            const attrs = ['href', 'data-href', 'data-url', 'data-link', 'data-job-url', 'data-job-link', 'onclick'];
            for (const name of attrs) {
              const value = node.getAttribute && node.getAttribute(name);
              const url = cleanUrl(value);
              if (isJobUrl(url)) return url;
            }
            return '';
          };
          const pickUrlFromCard = (card) => {
            if (!card) return '';
            const anchor = card.querySelector('a[href]');
            const direct = pickUrlFromNode(anchor);
            if (direct) return direct;
            const nodes = card.querySelectorAll('*');
            for (const node of nodes) {
              const url = pickUrlFromNode(node);
              if (isJobUrl(url)) return url;
            }
            return '';
          };
          const pickTitleFromCard = (card) => {
            if (!card) return '';
            const heading = card.querySelector('h1,h2,h3,h4,h5,[class*="title"],[data-testid*="title"],[data-qa*="title"]');
            let title = toText(heading);
            if (title) return title;
            const lines = toText(card).split('\\n').map((line) => line.trim()).filter(Boolean);
            for (const line of lines) {
              const low = line.toLowerCase();
              if (low.includes('view job') || low.includes('search jobs') || low.includes('clear filters')) continue;
              if (line.length < 5) continue;
              if (isLocationLine(line)) continue;
              return line;
            }
            return '';
          };
          const pickLocationFromCard = (card, title) => {
            if (!card) return '';
            const lines = toText(card).split('\\n').map((line) => line.trim()).filter(Boolean);
            const titleLower = (title || '').toLowerCase();
            for (const line of lines) {
              const low = line.toLowerCase();
              if (!line) continue;
              if (titleLower && low === titleLower) continue;
              if (low.includes('view job') || low.includes('search jobs') || low.includes('clear filters')) continue;
              if (isLocationLine(line)) return line;
            }
            return '';
          };

          const seen = new Set();
          const rows = [];
          const anchors = queryAllDeep('a[href]');

          for (const anchor of anchors) {
            const absoluteUrl = pickUrlFromNode(anchor);
            if (!isJobUrl(absoluteUrl)) continue;
            if (seen.has(absoluteUrl)) continue;
            seen.add(absoluteUrl);

            const card = anchor.closest('article, li, section, div');
            let title = pickTitleFromCard(card);
            let location = pickLocationFromCard(card, title);

            if (!title) {
              title = toText(anchor);
            }
            rows.push({ title, location, url: absoluteUrl });
          }

          const viewJobControls = queryAllDeep('button, a[role="button"], a');
          for (const control of viewJobControls) {
            const label = toText(control).toLowerCase();
            if (!label.includes('view job')) continue;
            const card = control.closest('article, li, section, div');
            if (!card) continue;
            const absoluteUrl = pickUrlFromCard(card) || pickUrlFromNode(control);
            if (!isJobUrl(absoluteUrl)) continue;
            if (seen.has(absoluteUrl)) continue;
            seen.add(absoluteUrl);

            const title = pickTitleFromCard(card);
            const location = pickLocationFromCard(card, title);
            rows.push({ title, location, url: absoluteUrl });
          }

          return rows;
        }
        """
    )

    jobs: List[Job] = []
    seen = set()
    for item in records:
        if not isinstance(item, dict):
            continue
        url = normalize_text(item.get("url"))
        title = normalize_text(item.get("title"))
        location = normalize_text(item.get("location"))
        if not url or not _is_probable_job_url(url):
            continue
        if not title or title.lower() in VIEW_JOB_TITLES:
            continue

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
                posted_at=None,
                raw=item,
            )
        )
    return jobs


def fetch_expedia_jobs(source: dict, session) -> List[Job]:
    del session

    if sync_playwright is None:
        raise RuntimeError(
            "Playwright is not installed. Install dependencies and run: python -m playwright install chromium"
        )

    headless = bool(source.get("playwright_headless", True))
    timeout_ms = int(source.get("playwright_timeout_ms", 60_000))
    wait_ms = int(source.get("playwright_wait_ms", 1200))
    max_actions = int(source.get("playwright_max_actions", 25))
    debug_enabled = bool(source.get("expedia_debug", False))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context(locale="en-US")
            page = context.new_page()
            captured_json_payloads: List[dict] = []
            captured_json_urls: List[str] = []

            def on_response(response) -> None:
                try:
                    req = response.request
                    if req.resource_type not in {"xhr", "fetch"}:
                        return
                    url = response.url.lower()
                    if any(skip in url for skip in ("google-analytics", "doubleclick", "cookielaw", "onetrust", "clarity")):
                        return
                    content_type = (response.headers.get("content-type") or "").lower()
                    if "json" not in content_type:
                        return
                    payload = response.json()
                    if isinstance(payload, (dict, list)):
                        captured_json_payloads.append(payload)
                        captured_json_urls.append(response.url)
                except Exception:
                    return

            page.on("response", on_response)
            page.goto(source["url"], wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(wait_ms)
            _dismiss_cookie_banner(page)
            _expand_results(page, max_actions=max_actions, wait_ms=wait_ms)

            html = page.content()
            jobs_from_json = _extract_jobs_from_captured_json(source, source["url"], captured_json_payloads)
            jobs_from_jsonld = _extract_jobs_from_rendered_jsonld(source, source["url"], html)
            jobs_from_embedded = _extract_jobs_from_embedded_json(source, source["url"], html)
            jobs_from_dom = _extract_jobs_from_rendered_dom(source, page)

            jobs = jobs_from_json or jobs_from_jsonld or jobs_from_embedded or jobs_from_dom

            if debug_enabled:
                try:
                    dom_debug = page.evaluate(
                        """
                        () => {
                          const collectRoots = () => {
                            const roots = [document];
                            for (let i = 0; i < roots.length; i++) {
                              const root = roots[i];
                              const elems = Array.from(root.querySelectorAll('*'));
                              for (const el of elems) {
                                if (el.shadowRoot) roots.push(el.shadowRoot);
                              }
                            }
                            return roots;
                          };
                          const roots = collectRoots();
                          const urls = [];
                          for (const root of roots) {
                            for (const node of Array.from(root.querySelectorAll('*'))) {
                              for (const attr of ['href', 'data-href', 'data-url', 'data-link', 'data-job-url', 'data-job-link', 'onclick']) {
                                const raw = node.getAttribute && node.getAttribute(attr);
                                if (!raw) continue;
                                const text = String(raw);
                                if (!/\\/job\\b|\\/jobs\\b|jobid|requisition|posting/i.test(text)) continue;
                                urls.push({ attr, value: text.slice(0, 240) });
                              }
                            }
                          }
                          const titleSnippets = [];
                          const headings = [];
                          for (const root of roots) {
                            headings.push(...Array.from(root.querySelectorAll('h1,h2,h3,h4,h5')));
                          }
                          for (const node of headings.slice(0, 120)) {
                            const t = (node.innerText || node.textContent || '').trim();
                            if (t) titleSnippets.push(t.slice(0, 200));
                          }
                          return {
                            deep_root_count: roots.length,
                            candidate_url_attr_count: urls.length,
                            candidate_url_attr_sample: urls.slice(0, 120),
                            heading_sample: titleSnippets.slice(0, 120),
                          };
                        }
                        """
                    )
                    os.makedirs("logs", exist_ok=True)
                    debug_path = os.path.join("logs", "expedia_debug.json")
                    debug_report = {
                        "source": source.get("name", "Expedia"),
                        "url": source.get("url"),
                        "captured_json_response_count": len(captured_json_payloads),
                        "captured_json_urls_sample": captured_json_urls[:50],
                        "jobs_from_captured_json": len(jobs_from_json),
                        "jobs_from_jsonld": len(jobs_from_jsonld),
                        "jobs_from_embedded_json": len(jobs_from_embedded),
                        "jobs_from_dom": len(jobs_from_dom),
                        "returned_jobs": len(jobs),
                        "page_title": page.title(),
                        "html_length": len(html),
                        "dom_debug": dom_debug,
                    }
                    with open(debug_path, "w", encoding="utf-8") as handle:
                        json.dump(debug_report, handle, indent=2, ensure_ascii=False)
                except Exception:
                    pass

            context.close()
            browser.close()
            return jobs
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        raise RuntimeError(f"Expedia Playwright fetch failed: {exc}") from exc
