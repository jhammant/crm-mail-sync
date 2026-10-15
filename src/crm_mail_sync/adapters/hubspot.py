"""HubSpot REST adapter. Transport is injectable for offline contract tests."""
import json
import os
import time
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ..models import Address, Associations, Contact, Deal, Message, timestamp

EMAIL_TO_CONTACT = 198
EMAIL_TO_COMPANY = 186
# Confirm this HUBSPOT_DEFINED type against the live associations API before first real use.
EMAIL_TO_DEAL = 210
CONTACT_TO_COMPANY_PRIMARY = 1

# CRM search allows about five requests a second per token, shared with anything else
# using it. Pace every request, and retry rate limits and gateway errors on reads only.
MIN_INTERVAL = 0.25
READ_RETRIES = 5
RETRYABLE = {429, 502, 503, 504}


def idempotent(method: str, path: str) -> bool:
    """Reads and searches are safe to repeat; creating an email is not (it could log twice)."""
    return method == 'GET' or path.split('?')[0].endswith(('/search', '/batch/read'))


def email_payload(message: Message, associations: Associations, direction: str,
                  owner_id: str | None = None) -> dict:
    def address(value: Address) -> dict:
        first, _, last = value.name.strip().partition(' ')
        return {'email': value.email, 'firstName': first, 'lastName': last}
    headers = {'from': address(message.sender), 'to': [address(a) for a in message.to],
               'cc': [address(a) for a in message.cc], 'bcc': []}
    properties = {'hs_timestamp': str(int(message.timestamp.timestamp() * 1000)),
                  'hs_email_direction': 'EMAIL' if direction == 'sent' else 'INCOMING_EMAIL',
                  'hs_email_status': 'SENT', 'hs_email_subject': message.subject,
                  'hs_email_text': message.text, 'hs_email_html': message.html,
                  'hs_email_headers': json.dumps(headers)}
    if owner_id:
        properties['hubspot_owner_id'] = owner_id
    links = []
    for ids, type_id in (([c.id for c in associations.contacts], EMAIL_TO_CONTACT),
                         (associations.companies, EMAIL_TO_COMPANY), (associations.deals, EMAIL_TO_DEAL)):
        for identifier in dict.fromkeys(ids):
            links.append({'to': {'id': identifier}, 'types': [
                {'associationCategory': 'HUBSPOT_DEFINED', 'associationTypeId': type_id}]})
    return {'properties': properties, 'associations': links}


def retry_after(exc: HTTPError, attempt: int) -> float:
    """HubSpot's Retry-After header when present, else 1, 2, 4, 8, 16 seconds."""
    try:
        return min(60.0, float(exc.headers.get('Retry-After', '')))
    except (AttributeError, TypeError, ValueError):
        return float(2 ** attempt)


class HubSpotCrm:
    def __init__(self, token_env: str, owner_id: str | None = None, *,
                 transport: Callable[[str, str, dict | None], dict] | None = None):
        self.owner_id = owner_id
        self.token = '' if transport else os.environ.get(token_env, '')
        if not transport and not self.token:
            raise ValueError(f'Missing HubSpot token environment variable: {token_env}')
        self.request = transport or self._request
        self._contacts: dict[str, Contact | None] = {}
        self._last_request = 0.0

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        attempts = READ_RETRIES + 1 if idempotent(method, path) else 1
        for attempt in range(attempts):
            wait = MIN_INTERVAL - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            request = Request('https://api.hubapi.com' + path,
                              data=json.dumps(body).encode() if body is not None else None,
                              headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'},
                              method=method)
            try:
                with urlopen(request, timeout=30) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code in RETRYABLE and attempt + 1 < attempts:
                    time.sleep(retry_after(exc, attempt))
                    continue
                # Never print response bodies, request headers or tokens.
                raise RuntimeError(f'HubSpot {method} request failed (HTTP {exc.code}); run stopped') from None
            except URLError:
                raise RuntimeError('HubSpot connection failed; run stopped') from None
        raise AssertionError('unreachable')

    def _associations(self, contact_id: str, kind: str) -> list[dict]:
        rows = []
        after = None
        while True:
            query = {'limit': 500}
            if after is not None:
                query['after'] = after
            result = self.request('GET', f'/crm/v4/objects/contacts/{quote(contact_id, safe="")}/associations/{kind}?'
                                  + urlencode(query), None)
            rows.extend(result.get('results', []))
            after = result.get('paging', {}).get('next', {}).get('after')
            if after is None:
                return rows

    def contact(self, email: str) -> Contact | None:
        if email not in self._contacts:
            result = self.request('POST', '/crm/v3/objects/contacts/search', {
                'filterGroups': [{'filters': [{'propertyName': 'email', 'operator': 'EQ', 'value': email}]}],
                'properties': ['email'], 'limit': 2})
            rows = result.get('results', [])
            if len(rows) > 1:
                raise RuntimeError('Ambiguous CRM contact lookup; run stopped')
            contact = None
            if rows:
                row = rows[0]
                primary = {str(a['toObjectId']) for a in self._associations(str(row['id']), 'companies')
                           if any(t.get('category') == 'HUBSPOT_DEFINED' and t.get('typeId') == 1
                                  for t in a.get('associationTypes', []))}
                if len(primary) > 1:
                    raise RuntimeError('Multiple primary companies; run stopped')
                contact = Contact(str(row['id']), email, next(iter(primary), None))
            self._contacts[email] = contact
        return self._contacts[email]

    def deals(self, contact_id: str) -> list[Deal]:
        ids = list(dict.fromkeys(str(a['toObjectId']) for a in self._associations(contact_id, 'deals')))
        deals = []
        for offset in range(0, len(ids), 100):
            batch = ids[offset:offset + 100]
            result = self.request('POST', '/crm/v3/objects/deals/batch/read', {
                'properties': ['dealstage', 'hs_lastmodifieddate', 'hs_is_closed'],
                'inputs': [{'id': identifier} for identifier in batch]})
            if result.get('errors') or {str(r['id']) for r in result.get('results', [])} != set(batch):
                raise RuntimeError('Incomplete deal batch response; run stopped')
            for row in result.get('results', []):
                p = row['properties']
                deals.append(Deal(str(row['id']), p['dealstage'], timestamp(p['hs_lastmodifieddate']),
                                  str(p.get('hs_is_closed', '')).lower() == 'true'))
        return deals

    def existing_email(self, message: Message) -> bool:
        millis = int(message.timestamp.timestamp() * 1000)
        result = self.request('POST', '/crm/v3/objects/emails/search', {
            'filterGroups': [{'filters': [
                {'propertyName': 'hs_email_subject', 'operator': 'EQ', 'value': message.subject},
                {'propertyName': 'hs_timestamp', 'operator': 'GTE', 'value': str(millis - 120000)},
                {'propertyName': 'hs_timestamp', 'operator': 'LTE', 'value': str(millis + 120000)}]}],
            'limit': 1})
        return bool(result.get('results'))

    def create_email(self, message: Message, associations: Associations, direction: str) -> str:
        result = self.request('POST', '/crm/v3/objects/emails',
                              email_payload(message, associations, direction, self.owner_id))
        return str(result['id'])

    def company_for_domain(self, domain: str) -> str | None:
        """The one company whose domain is exactly this; None when there is none or several."""
        result = self.request('POST', '/crm/v3/objects/companies/search', {
            'filterGroups': [{'filters': [{'propertyName': 'domain', 'operator': 'EQ', 'value': domain}]}],
            'properties': ['domain'], 'limit': 2})
        rows = result.get('results', [])
        return str(rows[0]['id']) if len(rows) == 1 else None

    def create_contact(self, email: str, name: str = '', company_domain: str | None = None) -> Contact:
        self._contacts.pop(email, None)  # look again: someone may have added them since the run read HubSpot
        existing = self.contact(email)
        if existing:
            return existing
        company = self.company_for_domain(company_domain) if company_domain else None
        first, _, last = name.strip().partition(' ')
        properties = {'email': email}
        if first:
            properties['firstname'] = first
        if last:
            properties['lastname'] = last
        if self.owner_id:
            properties['hubspot_owner_id'] = self.owner_id
        links = [{'to': {'id': company}, 'types': [
            {'associationCategory': 'HUBSPOT_DEFINED', 'associationTypeId': CONTACT_TO_COMPANY_PRIMARY}]}] if company else []
        result = self.request('POST', '/crm/v3/objects/contacts', {'properties': properties, 'associations': links})
        contact = Contact(str(result['id']), email, company)
        self._contacts[email] = contact
        return contact
