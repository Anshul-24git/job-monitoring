import re
from typing import Iterable, List, Optional

from .utils import normalize_text

US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
    "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri",
    "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "district of columbia",
}

US_STRONG = re.compile(r"\b(united states|united states of america|u\.s\.a\.|usa|u\.s\.|us)\b", re.I)
REMOTE_US = re.compile(r"\b(remote)\b.*\b(us|u\.s\.|usa|united states)\b|\b(us)\b.*\b(remote)\b", re.I)
CITY_STATE = re.compile(r",\s*([A-Z]{2})(\s+\d{5})?\b")
NON_US_HINTS = re.compile(
    r"\b(canada|toronto|vancouver|ontario|quebec|british columbia|uk|united kingdom|london|india|"
    r"england|bangalore|bengaluru|hyderabad|australia|sydney|germany|berlin|singapore|mexico|"
    r"mississauga|dublin|belfast|pune|noida|glasgow|paris|france|ireland|poland|japan|tokyo|emea|europe)\b",
    re.I,
)


def compile_keywords(keywords: Iterable[str]) -> List[re.Pattern]:
    patterns: List[re.Pattern] = []
    for kw in keywords:
        kw = normalize_text(kw).lower()
        if not kw:
            continue
        if re.fullmatch(r"[a-z0-9]+", kw):
            patterns.append(re.compile(rf"\b{re.escape(kw)}\b", re.I))
        else:
            patterns.append(re.compile(re.escape(kw), re.I))
    return patterns


def extend_patterns(base_patterns: List[re.Pattern], keywords: Iterable[str]) -> List[re.Pattern]:
    extra_patterns = compile_keywords(keywords)
    if not extra_patterns:
        return base_patterns
    return [*base_patterns, *extra_patterns]


def matches_any(text: str, patterns: List[re.Pattern]) -> bool:
    for pattern in patterns:
        if pattern.search(text):
            return True
    return False


def is_us_location(
    location: str,
    *,
    allow_remote_without_us_signal: bool,
    assume_us_only: bool,
    context_text: str = "",
) -> bool:
    loc = normalize_text(location)
    fallback = normalize_text(context_text)
    if not loc:
        loc = fallback
    if not loc:
        return assume_us_only

    if NON_US_HINTS.search(loc):
        return False

    if US_STRONG.search(loc) or REMOTE_US.search(loc):
        return True

    if "remote" in loc.lower():
        return allow_remote_without_us_signal or assume_us_only

    match = CITY_STATE.search(loc)
    if match and match.group(1).upper() in US_STATE_CODES:
        return True

    loc_lower = loc.lower()
    for name in US_STATE_NAMES:
        if name in loc_lower:
            return True

    return False


def matches_filters(
    title: str,
    *,
    include_patterns: List[re.Pattern],
    exclude_patterns: List[re.Pattern],
    location: str,
    url: str,
    us_only: bool,
    allow_remote_without_us_signal: bool,
    assume_us_only: bool,
) -> bool:
    title_normalized = normalize_text(title).lower()

    if exclude_patterns and matches_any(title_normalized, exclude_patterns):
        return False

    if include_patterns and not matches_any(title_normalized, include_patterns):
        return False

    if us_only and not is_us_location(
        location,
        allow_remote_without_us_signal=allow_remote_without_us_signal,
        assume_us_only=assume_us_only,
        context_text=f"{title} {url}",
    ):
        return False

    return True
