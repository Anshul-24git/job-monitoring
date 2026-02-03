import os
import re
from typing import Dict, Iterable, List, Tuple


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def humanize_slug(slug: str) -> str:
    raw = slug.strip().lower()
    if not raw:
        return slug

    text = re.sub(r"[-_]+", " ", raw)
    text = re.sub(r"([a-z])and([a-z])", r"\1 and \2", text)
    text = re.sub(r"([a-z])with([a-z])", r"\1 with \2", text)
    text = re.sub(r"([a-z])for([a-z])", r"\1 for \2", text)
    text = re.sub(r"([a-z])of([a-z])", r"\1 of \2", text)
    text = re.sub(r"([a-z])ai\\b", r"\1 ai", text)
    text = re.sub(r"([a-z])ml\\b", r"\1 ml", text)
    text = re.sub(r"([a-z])io\\b", r"\1 io", text)
    text = re.sub(r"([a-z])hr\\b", r"\1 hr", text)

    abbreviations = {
        "ai": "AI",
        "ml": "ML",
        "io": "IO",
        "hr": "HR",
        "qa": "QA",
        "ux": "UX",
        "ui": "UI",
    }

    words = []
    for part in text.split():
        if part in abbreviations:
            words.append(abbreviations[part])
        else:
            words.append(part.capitalize())

    return " ".join(words) if words else slug


LEGACY_NAME_OVERRIDES = {
    "sofi": "S Of I",
}


def pretty_display_name(slug: str) -> str:
    raw = slug.strip().lower()
    if not raw:
        return slug

    special = {
        "openai": "OpenAI",
        "scaleai": "Scale AI",
        "characterai": "Character AI",
        "inflectionai": "Inflection AI",
        "perplexityai": "Perplexity AI",
        "mistralai": "Mistral AI",
        "weightsandbiases": "Weights & Biases",
        "runwithsafety": "Runway (Safety)",
        "flyio": "Fly.io",
        "nocodeai": "NoCode AI",
        "sofi": "SoFi",
    }
    if raw in special:
        return special[raw]

    text = re.sub(r"[-_]+", " ", raw)
    abbreviations = {
        "ai": "AI",
        "ml": "ML",
        "io": "IO",
        "hr": "HR",
        "qa": "QA",
        "ux": "UX",
        "ui": "UI",
    }

    words = []
    for part in text.split():
        if part in abbreviations:
            words.append(abbreviations[part])
        else:
            words.append(part.capitalize())

    return " ".join(words) if words else slug


def _iter_entries(items: Iterable) -> Iterable[Tuple[str, str]]:
    for item in items:
        if isinstance(item, dict):
            slug = item.get("slug") or item.get("company")
            name = item.get("name") or item.get("display_name")
            if slug:
                yield str(slug).strip(), str(name).strip() if name else ""
        elif isinstance(item, str):
            yield item.strip(), ""


def build_auto_sources(
    auto_sources: Dict[str, Iterable],
    existing_sources: List[Dict],
    default_interval_minutes: int,
) -> List[Dict]:
    existing_urls = {s.get("url", "").strip() for s in existing_sources if s.get("url")}
    existing_names = {
        normalize_name(s.get("name", "")) for s in existing_sources if s.get("name")
    }

    generated: List[Dict] = []

    for provider, entries in (auto_sources or {}).items():
        if not entries:
            continue
        provider_key = provider.lower().strip()
        for slug, display in _iter_entries(entries):
            if not slug:
                continue

            slug_key = slug.strip().lower()
            url, kind, company = _provider_to_url(provider_key, slug)
            if not url:
                continue

            if slug_key in LEGACY_NAME_OVERRIDES:
                name_base = LEGACY_NAME_OVERRIDES[slug_key]
            else:
                name_base = display or humanize_slug(slug)
            if normalize_name(name_base) in existing_names:
                continue

            display_value = display or pretty_display_name(slug_key)
            display_name = f"{name_base} ({provider_key})"
            if normalize_name(display_name) in existing_names:
                continue

            if url in existing_urls:
                continue

            source = {
                "name": display_name,
                "display_name": display_value,
                "url": url,
                "interval_minutes": default_interval_minutes,
                "assume_us_only": False,
                "kind": kind,
                "auto_source": True,
            }
            if company:
                source["company"] = company

            generated.append(source)
            existing_urls.add(url)
            existing_names.add(normalize_name(display_name))
            existing_names.add(normalize_name(name_base))

    return generated


def _provider_to_url(provider: str, slug: str) -> Tuple[str, str, str]:
    slug = slug.strip()
    if not slug:
        return "", "", ""

    if provider == "greenhouse":
        return f"https://boards.greenhouse.io/{slug}", "greenhouse", slug

    if provider == "lever":
        return f"https://jobs.lever.co/{slug}", "lever", slug

    if provider == "ashby":
        return f"https://jobs.ashbyhq.com/{slug}", "ashby", ""

    if provider == "smartrecruiters":
        return f"https://careers.smartrecruiters.com/{slug}", "smartrecruiters", slug

    if provider == "bamboohr":
        return f"https://{slug}.bamboohr.com/careers/list", "bamboohr", ""

    return "", "", ""


def merge_auto_sources(primary: Dict, secondary: Dict) -> Dict:
    merged = {**(primary or {})}
    for key, values in (secondary or {}).items():
        if key in merged and isinstance(merged[key], list) and isinstance(values, list):
            merged[key] = merged[key] + values
        else:
            merged[key] = values
    return merged


def load_auto_sources_file(base_dir: str, path: str) -> Dict:
    if not path:
        return {}

    resolved = path
    if not os.path.isabs(path):
        resolved = os.path.join(base_dir, path)

    if not os.path.exists(resolved):
        return {}

    import yaml

    with open(resolved, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    return data
