"""Normalised values shared by the rules and adapters."""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal


def timestamp(value: str | int) -> datetime:
    if isinstance(value, int) or str(value).isdigit():
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Address:
    email: str
    name: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "email", self.email.strip().lower())


@dataclass(frozen=True)
class Message:
    id: str
    thread_id: str
    timestamp: datetime
    sender: Address
    to: tuple[Address, ...]
    cc: tuple[Address, ...] = ()
    subject: str = ""
    text: str = ""
    html: str = ""
    labels: tuple[str, ...] = ()
    thread_labels: tuple[str, ...] = ()
    has_calendar: bool = False
    incomplete: bool = False
    mailing_list: bool = False  # List-Id, List-Unsubscribe, Precedence or Auto-Submitted header

    @property
    def participants(self) -> tuple[Address, ...]:
        return (self.sender, *self.to, *self.cc)

    def to_dict(self) -> dict:
        result = asdict(self)
        result["timestamp"] = self.timestamp.isoformat()
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        data = dict(data)
        data["timestamp"] = timestamp(data["timestamp"])
        data["sender"] = Address(**data["sender"])
        for key in ("to", "cc"):
            data[key] = tuple(Address(**a) for a in data.get(key, []))
        for key in ("labels", "thread_labels"):
            data[key] = tuple(data.get(key, []))
        return cls(**data)


@dataclass(frozen=True)
class Contact:
    id: str
    email: str
    company_id: str | None = None


@dataclass(frozen=True)
class Deal:
    id: str
    stage: str
    modified: datetime
    closed: bool = False


@dataclass(frozen=True)
class Decision:
    action: Literal["log", "skip", "review"]
    reason: str
    contacts: tuple[Contact, ...] = ()


@dataclass(frozen=True)
class Associations:
    contacts: tuple[Contact, ...] = ()
    companies: tuple[str, ...] = ()
    deals: tuple[str, ...] = ()


@dataclass(frozen=True)
class Outcome:
    message: Message
    decision: Decision
    direction: str
    associations: Associations = field(default_factory=Associations)
