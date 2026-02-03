import os
from typing import Any, Dict, List

import yaml

from .auto_sources import build_auto_sources, load_auto_sources_file, merge_auto_sources


class ConfigError(RuntimeError):
    pass


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}

    env_file = cfg.get("env_file")
    if env_file:
        base_dir = os.path.dirname(os.path.abspath(path))
        _load_env_file(base_dir, env_file)

    if "sources" not in cfg or not cfg["sources"]:
        raise ConfigError("No sources configured in config.yaml")

    cfg.setdefault("schedule", {})
    cfg["schedule"].setdefault("timezone", "America/Chicago")
    cfg["schedule"].setdefault("default_interval_minutes", 30)
    cfg["schedule"].setdefault("sleep_seconds", 20)
    cfg["schedule"].setdefault("active_hours", {"start": "09:00", "end": "19:00"})

    cfg.setdefault("filters", {})
    cfg["filters"].setdefault("include_keywords", [])
    cfg["filters"].setdefault("exclude_keywords", [])
    cfg["filters"].setdefault("location", {})
    cfg["filters"]["location"].setdefault("us_only", True)
    cfg["filters"]["location"].setdefault("allow_remote_without_us_signal", False)

    cfg.setdefault("email", {})
    cfg["email"].setdefault("smtp_host", "smtp.gmail.com")
    cfg["email"].setdefault("smtp_port", 587)
    cfg["email"].setdefault("notify_on_errors", True)
    cfg["email"].setdefault("error_email_cooldown_minutes", 60)

    cfg.setdefault("notifications", {})
    cfg["notifications"].setdefault("skip_first_run", False)
    cfg["notifications"].setdefault("ignore_error_statuses", [404, 410])

    auto_sources = cfg.get("auto_sources") or {}
    auto_sources_file = cfg.get("auto_sources_file")
    if auto_sources_file:
        base_dir = os.path.dirname(os.path.abspath(path))
        file_sources = load_auto_sources_file(base_dir, auto_sources_file)
        auto_sources = merge_auto_sources(auto_sources, file_sources)

    if auto_sources:
        generated = build_auto_sources(
            auto_sources,
            cfg["sources"],
            cfg["schedule"]["default_interval_minutes"],
        )
        cfg["sources"].extend(generated)

    for source in cfg["sources"]:
        if "interval_minutes" not in source:
            source["interval_minutes"] = cfg["schedule"]["default_interval_minutes"]
        source.setdefault("assume_us_only", False)

    return cfg


def _load_env_file(base_dir: str, path: str) -> None:
    resolved = path
    if not os.path.isabs(path):
        resolved = os.path.join(base_dir, path)
    if not os.path.exists(resolved):
        return

    with open(resolved, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"").strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def resolve_email_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    email_cfg = cfg.get("email", {})
    app_password = email_cfg.get("app_password")

    app_password_env = email_cfg.get("app_password_env")
    if not app_password and app_password_env:
        app_password = os.getenv(app_password_env)

    if not app_password:
        raise ConfigError("Missing Gmail app password. Set app_password or app_password_env.")

    required_fields = ["user", "from", "to", "smtp_host", "smtp_port"]
    for field in required_fields:
        if not email_cfg.get(field):
            raise ConfigError(f"Missing email config field: {field}")

    return {
        "smtp_host": email_cfg["smtp_host"],
        "smtp_port": int(email_cfg["smtp_port"]),
        "user": email_cfg["user"],
        "from": email_cfg["from"],
        "to": email_cfg["to"],
        "app_password": app_password,
        "notify_on_errors": bool(email_cfg.get("notify_on_errors", True)),
        "error_email_cooldown_minutes": int(email_cfg.get("error_email_cooldown_minutes", 60)),
    }
