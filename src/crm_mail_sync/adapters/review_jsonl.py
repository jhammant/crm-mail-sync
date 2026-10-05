"""Append-only review event log; terminal events remove pending items."""
import json
import os
from pathlib import Path

from ..models import Message
from ..state import private_directory


class JsonlReviewSink:
    def __init__(self, path: Path):
        self.path = path

    def pending(self) -> dict[str, dict]:
        pending: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                event = json.loads(line)
                if event['action'] == 'review':
                    pending[event['id']] = event
                else:
                    pending.pop(event['id'], None)
        return pending

    def _append(self, event: dict) -> None:
        private_directory(self.path.parent)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a') as stream:
            stream.write(json.dumps(event) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def enqueue(self, message: Message, reason: str) -> None:
        if message.id not in self.pending():
            self._append({'id': message.id, 'action': 'review', 'reason': reason, 'message': message.to_dict()})

    def resolve(self, message_id: str, action: str) -> None:
        if action not in {'approved', 'skipped'}:
            raise ValueError('Invalid review resolution')
        self._append({'id': message_id, 'action': action})
