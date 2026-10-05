"""Offline mailbox and matching fake CRM data in one JSON document."""
import json
from datetime import datetime
from pathlib import Path

from ..models import Message


class FixtureSource:
    def __init__(self, path: str | Path):
        self.data = json.loads(Path(path).read_text())
        self._messages = [Message.from_dict(m) for m in self.data['messages']]
        labels: dict[str, set[str]] = {}
        for message in self._messages:
            labels.setdefault(message.thread_id, set()).update((*message.labels, *message.thread_labels))
        from dataclasses import replace
        self._messages = [replace(m, thread_labels=tuple(sorted(labels[m.thread_id]))) for m in self._messages]

    def messages(self, since: datetime) -> list[Message]:
        return sorted((m for m in self._messages if m.timestamp >= since), key=lambda m: (m.timestamp, m.id))

    def get_message(self, message_id: str) -> Message:
        for message in self._messages:
            if message.id == message_id:
                return message
        raise ValueError(f'Message not found in fixture: {message_id}')
