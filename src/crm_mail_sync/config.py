"""Strict configuration loading. Relative paths belong to the config directory."""
from dataclasses import dataclass
from pathlib import Path
import re

import yaml

EMAIL = re.compile(r"[^\s@]+@(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}", re.I)
DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}", re.I)


DEFAULT_AUTOMATED_SENDER_PATTERNS = (
    "noreply*@*", "no[-_.]reply*@*", "donotreply*@*", "do[-_.]not[-_.]reply*@*",
    "notification@*", "notifications*@*", "mailer-daemon*@*", "drive-shares*@*",
    "*docusign*@*", "*echosign*@*", "*@*docusign*", "*@*echosign*",
)


@dataclass(frozen=True)
class Config:
    mailbox_addresses: tuple[str, ...]
    internal_domains: tuple[str, ...]
    never_log: tuple[str, ...]
    private_label: str
    review_keywords: tuple[str, ...]
    state_path: Path
    review_path: Path
    gmail_token_path: Path
    hubspot_token_env: str = "HUBSPOT_TOKEN"
    hubspot_owner_id: str | None = None
    initial_since: str = ""
    skip_automated_senders: bool = True
    automated_sender_patterns: tuple[str, ...] = DEFAULT_AUTOMATED_SENDER_PATTERNS


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Config must be a YAML mapping")
    unknown = set(data) - set(Config.__dataclass_fields__)
    if unknown:
        raise ValueError(f"Unknown config keys: {', '.join(sorted(unknown))}")
    data.setdefault("automated_sender_patterns", list(DEFAULT_AUTOMATED_SENDER_PATTERNS))
    if not isinstance(data.get("skip_automated_senders", True), bool):
        raise ValueError("skip_automated_senders must be a boolean")
    for key in ("automated_sender_patterns", "mailbox_addresses", "internal_domains", "never_log", "review_keywords"):
        value = data.get(key)
        if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
            raise ValueError(f"{key} must be a list of non-empty strings")
        data[key] = tuple(x.strip().lower() for x in value)
    for key, pattern in (("mailbox_addresses", EMAIL), ("internal_domains", DOMAIN)):
        if not data[key] or any(not pattern.fullmatch(x) for x in data[key]):
            raise ValueError(f"{key} must contain valid {'addresses' if key == 'mailbox_addresses' else 'domains'}")
    if any(not (EMAIL.fullmatch(x) or DOMAIN.fullmatch(x)) for x in data["never_log"]):
        raise ValueError("never_log entries must be email addresses or domains")
    for key in ("private_label", "initial_since"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    from datetime import date
    date.fromisoformat(data["initial_since"])
    for key in ("state_path", "review_path", "gmail_token_path"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"{key} must be a path")
        candidate = Path(data[key]).expanduser()
        data[key] = (path.parent / candidate).resolve()
    if len({data[k] for k in ("state_path", "review_path", "gmail_token_path")}) != 3:
        raise ValueError("State, review and token paths must be distinct")
    token_env = data.get("hubspot_token_env", "HUBSPOT_TOKEN")
    if not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
        raise ValueError("hubspot_token_env must be an environment variable name")
    if data.get("hubspot_owner_id") is not None:
        if not str(data["hubspot_owner_id"]).isdigit():
            raise ValueError("hubspot_owner_id must be a numeric ID")
        data["hubspot_owner_id"] = str(data["hubspot_owner_id"])
    return Config(**data)
