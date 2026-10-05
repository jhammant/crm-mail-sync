"""Private, atomic local state and a process lock for scheduled runs."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
from collections.abc import Iterator


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def atomic_json(path: Path, data: dict) -> None:
    private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    private_directory(path.parent)
    fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another run or review command is using this state file') from exc
        yield
    finally:
        os.close(fd)


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {'version': 1, 'outcomes': {}}
        if self.data.get('version') != 1 or not isinstance(self.data.get('outcomes'), dict):
            raise ValueError('Unsupported or invalid state file')

    def recorded(self, message_id: str) -> bool:
        return message_id in self.data['outcomes']

    def record(self, message_id: str, action: str, reason: str, crm_id: str | None = None) -> None:
        self.data['outcomes'][message_id] = {'action': action, 'reason': reason, 'crm_id': crm_id}
        atomic_json(self.path, self.data)

    def checkpoint(self, since: str) -> None:
        self.data['last_run'] = since
        atomic_json(self.path, self.data)
