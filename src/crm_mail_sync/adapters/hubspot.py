"""HubSpot REST adapter. Transport is injectable for offline contract tests."""
import json
import os
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ..models import Address, Associations, Contact, Deal, Message, timestamp

EMAIL_TO_CONTACT = 198
EMAIL_TO_COMPANY = 186
# Confirm this HUBSPOT_DEFINED type against the live associations API before first real use.
EMAIL_TO_DEAL = 210


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


class HubSpotCrm:
    def __init__(self, token_env: str, owner_id: str | None = None, *,
                 transport: Callable[[str, str, dict | None], dict] | None = None):
        self.owner_id = owner_id
        self.token = '' if transport else os.environ.get(token_env, '')
        if not transport and not self.token:
            raise ValueError(f'Missing HubSpot token environment variable: {token_env}')
        self.request = transport or self._request
        self._contacts: dict[str, Contact | None] = {}

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        request = Request('https://api.hubapi.com' + path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'},
                          method=method)
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            # Never print response bodies, request headers or tokens.
            raise RuntimeError(f'HubSpot {method} request failed (HTTP {exc.code}); run stopped') from None
        except URLError:
            raise RuntimeError('HubSpot connection failed; run stopped') from None

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
