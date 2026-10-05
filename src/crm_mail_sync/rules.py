"""Pure decisions: hard exclusions, human review, then eligible contacts."""
import re
from html.parser import HTMLParser
from collections.abc import Mapping, Sequence

from .config import Config, EMAIL
from .models import Contact, Deal, Decision, Message

CALENDAR = re.compile(r"^\s*(accepted|declined|tentative|tentatively accepted|invitation|updated invitation|cancelled event|canceled event):", re.I)
BULK = re.compile(r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|notifications?|mailer-daemon)(?:[+._-].*)?$", re.I)


def domain_matches(email: str, domain: str) -> bool:
    actual = email.rpartition("@")[2].lower()
    return actual == domain or actual.endswith("." + domain)


def internal(email: str, config: Config) -> bool:
    return email in config.mailbox_addresses or any(domain_matches(email, d) for d in config.internal_domains)


def blocked(email: str, config: Config) -> bool:
    return any(email == entry if "@" in entry else domain_matches(email, entry) for entry in config.never_log)


def eligible_addresses(message: Message, config: Config) -> tuple[str, ...]:
    return tuple(sorted({p.email for p in message.participants if EMAIL.fullmatch(p.email)
                         and not internal(p.email, config) and not blocked(p.email, config)}))


def hard_skip(message: Message, config: Config) -> str | None:
    if config.private_label in (*message.labels, *message.thread_labels):
        return "private label"
    if {"DRAFT", "SPAM", "TRASH"}.intersection(message.labels):
        return "draft, spam or trash"
    if message.has_calendar or CALENDAR.match(message.subject):
        return "calendar robot"
    if BULK.fullmatch(message.sender.email.partition("@")[0]):
        return "bulk sender"
    return None


def visible_html(value: str) -> str:
    """Preserve inline words and separate block text for keyword matching."""
    class Text(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []

        def handle_data(self, data: str) -> None:
            self.parts.append(data)

        def handle_starttag(self, tag: str, attrs: list) -> None:
            if tag in {'br', 'p', 'div', 'li', 'tr', 'h1', 'h2', 'h3'}:
                self.parts.append(' ')

        def handle_endtag(self, tag: str) -> None:
            self.handle_starttag(tag, [])

    parser = Text()
    parser.feed(value)
    return ''.join(parser.parts)


def classify(message: Message, config: Config, contacts: Mapping[str, Contact | None], *, approved: bool = False) -> Decision:
    reason = hard_skip(message, config)
    if reason:
        return Decision("skip", reason)
    if message.incomplete or any(not EMAIL.fullmatch(p.email) for p in message.participants):
        return Decision("review", "incomplete message or participants")
    external = [p.email for p in message.participants if not internal(p.email, config)]
    if not external:
        return Decision("skip", "internal-only participants")
    eligible = eligible_addresses(message, config)
    if not eligible:
        return Decision("skip", "only never-log/internal participants")
    matched = tuple({c.id: c for addr in eligible if (c := contacts.get(addr))}.values())
    if not approved:
        content = f"{message.subject}\n{message.text}\n{message.html}\n{visible_html(message.html)}".casefold()
        for keyword in config.review_keywords:
            if re.search(r"(?<!\w)" + re.escape(keyword.casefold()).replace(r"\ ", r"\s+") + r"(?!\w)", content):
                return Decision("review", f"review keyword: {keyword}")
    if matched:
        return Decision("log", "review approved" if approved else "existing eligible contacts", matched)
    if any(blocked(addr, config) for addr in external):
        return Decision("review", "unknown external participants with never-log participant")
    return Decision("review", "no eligible CRM contacts; cannot decide")


def recent_open_deals(deals: Sequence[Deal]) -> tuple[Deal, ...]:
    unique = {d.id: d for d in deals if not d.closed and d.stage.lower() not in {"closedwon", "closedlost"}}
    return tuple(sorted(unique.values(), key=lambda d: (d.modified, d.id), reverse=True)[:5])
