"""Rules learned from review decisions, kept in a YAML file the owner can read and edit."""
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from .config import DOMAIN, EMAIL
from .state import atomic_text

HEADER = """\
# Learned by crm-mail-sync from your review decisions. Edit freely: delete an entry to
# start asking again, or move it between lists. Each entry is an address or a domain.
# An address beats a domain, and for the same entry skip beats log.
#   log:  log their email to the CRM, creating the contact if they aren't in it yet
#   skip: never log their email, and stop asking
"""


def party(email: str, personal_domains: tuple[str, ...]) -> str:
    """What a decision about this address is remembered for: its domain, or the address itself on a personal domain."""
    domain = email.rpartition("@")[2].lower()
    return email.lower() if domain in personal_domains else domain


def _matches(email: str, entry: str) -> bool:
    if "@" in entry:
        return email == entry
    actual = email.rpartition("@")[2]
    return actual == entry or actual.endswith("." + entry)


@dataclass(frozen=True)
class LearnedRules:
    log: tuple[str, ...] = ()
    skip: tuple[str, ...] = ()

    def action_for(self, email: str) -> str | None:
        email = email.lower()
        for exact in (True, False):
            hits = {action for action, entries in (("skip", self.skip), ("log", self.log))
                    for entry in entries if ("@" in entry) == exact and _matches(email, entry)}
            if hits:
                return "skip" if "skip" in hits else "log"
        return None


def _entries(data: dict, action: str, path: Path) -> list[dict]:
    rows = data.get(action) or []
    if not isinstance(rows, list):
        raise ValueError(f"{path}: {action} must be a list")
    for row in rows:
        match = row.get("match") if isinstance(row, dict) else None
        if not isinstance(match, str) or not (EMAIL.fullmatch(match.strip()) or DOMAIN.fullmatch(match.strip())):
            raise ValueError(f"{path}: every {action} entry needs a match that is an address or a domain")
        row["match"] = match.strip().lower()
    return rows


def _read(path: Path) -> dict:
    data = yaml.safe_load(path.read_text()) if path.exists() else None
    data = data or {}
    if not isinstance(data, dict) or set(data) - {"log", "skip"}:
        raise ValueError(f"{path}: learned rules must be a mapping with only log and skip lists")
    return {action: _entries(data, action, path) for action in ("log", "skip")}


def load_learned(path: Path) -> LearnedRules:
    data = _read(path)
    return LearnedRules(tuple(r["match"] for r in data["log"]), tuple(r["match"] for r in data["skip"]))


def learn(path: Path, action: str, matches: list[str], personal_domains: tuple[str, ...], *,
          note: str = "", today: date | None = None) -> list[str]:
    """Remember a decision for each match. A new decision replaces an earlier opposite one. Returns what changed."""
    if action not in {"log", "skip"}:
        raise ValueError("Learned action must be log or skip")
    cleaned = []
    for match in matches:
        match = match.strip().lower()
        if not (EMAIL.fullmatch(match) or DOMAIN.fullmatch(match)):
            raise ValueError(f"Not an address or a domain: {match}")
        if "@" not in match and match in personal_domains:
            raise ValueError(f"{match} is a personal email domain: remember the address instead")
        cleaned.append(match)
    data = _read(path)
    other = "skip" if action == "log" else "log"
    changed = []
    for match in dict.fromkeys(cleaned):
        data[other] = [r for r in data[other] if r["match"] != match]
        if any(r["match"] == match for r in data[action]):
            continue
        row = {"match": match, "added": (today or date.today()).isoformat()}
        if note:
            row["note"] = note
        data[action].append(row)
        changed.append(match)
    body = yaml.safe_dump({k: sorted(v, key=lambda r: r["match"]) for k, v in data.items()},
                          sort_keys=False, allow_unicode=True)
    atomic_text(path, HEADER + body)
    return changed
