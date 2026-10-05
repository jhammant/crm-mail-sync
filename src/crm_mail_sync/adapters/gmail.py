"""Gmail read-only adapter and MIME normalisation."""
import base64
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import getaddresses
import math
from pathlib import Path
from collections.abc import Callable

from ..models import Address, Message, timestamp

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']


def decode(value: str) -> str:
    return str(make_header(decode_header(value)))


def normalise(raw: dict, label_names: dict[str, str], thread_labels: tuple[str, ...],
              attachment: Callable[[str], str] | None = None) -> Message:
    payload = raw.get('payload', {})
    headers: dict[str, list[str]] = {}
    for header in payload.get('headers', []):
        headers.setdefault(header['name'].lower(), []).append(header['value'])

    def addresses(key: str) -> tuple[Address, ...]:
        return tuple(Address(email, decode(name)) for name, email in getaddresses(headers.get(key, [])))

    text, html = [], []
    has_calendar = False
    incomplete = False

    def walk(part: dict) -> None:
        nonlocal has_calendar, incomplete
        mime = part.get('mimeType', '').lower()
        has_calendar |= mime == 'text/calendar'
        if mime in {'text/plain', 'text/html'} and not part.get('filename'):
            body = part.get('body', {})
            encoded = body.get('data', '')
            if body.get('attachmentId'):
                if attachment is None:
                    incomplete = True
                else:
                    encoded = attachment(body['attachmentId'])
            charset = 'utf-8'
            from email.message import Message as MimeMessage
            mime_headers = MimeMessage()
            for header in part.get('headers', []):
                if header['name'].lower() == 'content-type':
                    mime_headers['content-type'] = header['value']
            charset = mime_headers.get_content_charset() or charset
            try:
                content = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode(charset)
            except (ValueError, UnicodeError, LookupError):
                content = ''
                incomplete = True
            (text if mime == 'text/plain' else html).append(content)
        for child in part.get('parts', []):
            walk(child)

    walk(payload)
    senders = addresses('from')
    to, cc = addresses('to'), addresses('cc')
    incomplete |= len(senders) != 1 or not (to or cc) or not payload or not (text or html)
    return Message(id=raw['id'], thread_id=raw['threadId'], timestamp=timestamp(raw['internalDate']),
                   sender=senders[0] if senders else Address(''), to=to, cc=cc,
                   subject=decode(headers.get('subject', [''])[0]), text='\n'.join(text), html='\n'.join(html),
                   labels=tuple(label_names.get(x, x) for x in raw.get('labelIds', [])),
                   thread_labels=thread_labels, has_calendar=has_calendar, incomplete=incomplete)


class GmailSource:
    def __init__(self, token_path: Path, *, service=None):
        if service is None:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
            credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            if not credentials.valid:
                if not credentials.refresh_token:
                    raise ValueError('Gmail token requires desktop OAuth authorisation')
                credentials.refresh(Request())
                # Keep refresh in memory: even a dry-run must never rewrite the token file.
            service = build('gmail', 'v1', credentials=credentials, cache_discovery=False)
        self.service = service
        self.label_names = {label['id']: label['name'] for label in
                            service.users().labels().list(userId='me').execute().get('labels', [])}
        self._threads: dict[str, dict] = {}

    def _thread(self, thread_id: str) -> dict:
        if thread_id not in self._threads:
            self._threads[thread_id] = self.service.users().threads().get(
                userId='me', id=thread_id, format='full').execute()
        return self._threads[thread_id]

    def _normalise(self, raw: dict) -> Message:
        thread = self._thread(raw['threadId'])
        labels = tuple(sorted({self.label_names.get(label, label) for m in thread.get('messages', [])
                               for label in m.get('labelIds', [])}))
        def attachment(attachment_id: str) -> str:
            return self.service.users().messages().attachments().get(
                userId='me', messageId=raw['id'], id=attachment_id).execute()['data']
        message = normalise(raw, self.label_names, labels, attachment)
        if not thread.get('messages'):
            from dataclasses import replace
            message = replace(message, incomplete=True)
        return message

    def messages(self, since: datetime) -> list[Message]:
        self._threads.clear()
        messages = []
        page = None
        while True:
            result = self.service.users().messages().list(
                userId='me', q=f'after:{math.floor(since.timestamp()) - 1}',
                maxResults=500, pageToken=page).execute()
            for row in result.get('messages', []):
                raw = self.service.users().messages().get(userId='me', id=row['id'], format='full').execute()
                message = self._normalise(raw)
                if message.timestamp >= since:
                    messages.append(message)
            page = result.get('nextPageToken')
            if not page:
                break
        return sorted(messages, key=lambda m: (m.timestamp, m.id))

    def get_message(self, message_id: str) -> Message:
        self._threads.clear()
        raw = self.service.users().messages().get(userId='me', id=message_id, format='full').execute()
        return self._normalise(raw)
