import base64
from dataclasses import replace
import json
from urllib.error import HTTPError
from unittest.mock import Mock

import pytest

from crm_mail_sync.adapters.gmail import READ_RETRIES, GmailSource, normalise
from crm_mail_sync.adapters.hubspot import EMAIL_TO_DEAL, HubSpotCrm, email_payload
from crm_mail_sync.models import Associations, timestamp
from crm_mail_sync.pipeline import evaluate


def encoded(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')


def raw_message():
    return {'id':'gmail-1','threadId':'thread-1','internalDate':'1790931600000','labelIds':['SENT'],
            'payload':{'mimeType':'multipart/mixed','headers':[
                {'name':'From','value':'=?utf-8?b?T3BlcmF0b3I=?= <operator@example.com>'},
                {'name':'To','value':'Partner Person <partner@example.org>'},
                {'name':'Cc','value':'Investor <investor@example.org>'},
                {'name':'Subject','value':'Project'}], 'parts':[
                    {'mimeType':'multipart/alternative','parts':[
                        {'mimeType':'text/plain','body':{'data':encoded('Hello')}},
                        {'mimeType':'text/html','body':{'data':encoded('<p>Hello</p>')}}]},
                    {'mimeType':'text/calendar','body':{'data':encoded('calendar')}}]}}


def test_nested_gmail_mime_and_names():
    message = normalise(raw_message(), {}, ('Private - no CRM',))
    assert message.sender.name == 'Operator'
    assert message.to[0].name == 'Partner Person'
    assert message.cc[0].email == 'investor@example.org'
    assert (message.text, message.html) == ('Hello', '<p>Hello</p>')
    assert message.has_calendar
    assert message.thread_labels == ('Private - no CRM',)
    assert not message.incomplete


def test_gmail_missing_and_attachment_body():
    raw = raw_message()
    raw['payload']['parts'] = [{'mimeType':'text/plain','body':{'attachmentId':'body-id'}}]
    assert normalise(raw, {}, ()).incomplete
    result = normalise(raw, {}, (), lambda aid: encoded('Attached body'))
    assert result.text == 'Attached body'
    assert not result.incomplete
    raw['payload']['headers'] = []
    assert normalise(raw, {}, ()).incomplete


def test_gmail_pagination_and_thread_privacy(config):
    service = Mock()
    users = service.users.return_value
    users.labels.return_value.list.return_value.execute.return_value = {'labels':[{'id':'private','name':config.private_label}]}
    mailbox = users.messages.return_value
    mailbox.list.return_value.execute.side_effect = [
        {'messages':[{'id':'gmail-1'}], 'nextPageToken':'page-2'}, {'messages':[{'id':'gmail-2'}]}]
    raw = raw_message()
    second = dict(raw, id='gmail-2')
    mailbox.get.return_value.execute.side_effect = [raw, second]
    users.threads.return_value.get.return_value.execute.return_value = {'messages':[dict(raw, labelIds=['private'])]}
    source = GmailSource(config.gmail_token_path, service=service)
    messages = source.messages(timestamp('2026-10-01T00:00:00Z'))
    assert len(messages) == 2
    assert all(config.private_label in m.thread_labels for m in messages)
    assert mailbox.list.call_args_list[1].kwargs['pageToken'] == 'page-2'
    assert users.threads.return_value.get.call_count == 1
    # Threads are read for labels only, and every read backs off on rate limits.
    assert users.threads.return_value.get.call_args.kwargs['format'] == 'minimal'
    for request in (mailbox.list.return_value, mailbox.get.return_value, users.threads.return_value.get.return_value):
        assert all(call.kwargs == {'num_retries': READ_RETRIES} for call in request.execute.call_args_list)


def test_gmail_http_errors_stop_cleanly_without_content(config):
    from googleapiclient.errors import HttpError
    service = Mock()
    users = service.users.return_value
    users.labels.return_value.list.return_value.execute.return_value = {'labels': []}
    response = Mock(status=403, reason='Forbidden')
    users.messages.return_value.list.return_value.execute.side_effect = HttpError(
        response, b'{"error": "Quota exceeded; quoted body: secret-subject"}')
    source = GmailSource(config.gmail_token_path, service=service)
    with pytest.raises(RuntimeError) as exc:
        source.messages(timestamp('2026-10-01T00:00:00Z'))
    assert '403' in str(exc.value)
    assert 'secret-subject' not in str(exc.value)


def test_hubspot_payload_contract(config, source, crm):
    message = source.get_message('intro')
    outcome = evaluate(message, config, crm)
    payload = email_payload(message, outcome.associations, 'received', '123')
    properties = payload['properties']
    assert properties['hs_email_direction'] == 'INCOMING_EMAIL'
    assert properties['hs_email_status'] == 'SENT'
    assert properties['hubspot_owner_id'] == '123'
    assert properties['hs_timestamp'] == str(int(message.timestamp.timestamp()*1000))
    headers = json.loads(properties['hs_email_headers'])
    assert headers['from']['email'] == 'investor@example.org'
    assert headers['bcc'] == []
    associations = {(a['to']['id'], a['types'][0]['associationTypeId']) for a in payload['associations']}
    assert associations == {('partner',198),('company-partner',186),*(('d'+str(i),210) for i in range(3,8))}
    assert EMAIL_TO_DEAL == 210
    assert email_payload(message, Associations(), 'sent')['properties']['hs_email_direction'] == 'EMAIL'


def test_hubspot_contact_primary_company_and_cache():
    transport = Mock(side_effect=[{'results':[{'id':'123'}]}, {'results':[
        {'toObjectId':'other','associationTypes':[{'category':'HUBSPOT_DEFINED','typeId':279}]},
        {'toObjectId':'primary','associationTypes':[{'category':'HUBSPOT_DEFINED','typeId':1}]}]}])
    crm = HubSpotCrm('UNUSED', transport=transport)
    contact = crm.contact('partner@example.org')
    assert contact.company_id == 'primary'
    assert crm.contact('partner@example.org') == contact
    assert transport.call_count == 2
    filters = transport.call_args_list[0].args[2]['filterGroups'][0]['filters']
    assert filters == [{'propertyName':'email','operator':'EQ','value':'partner@example.org'}]


def test_hubspot_deal_pagination_batching_and_closed_flag():
    calls = []
    def transport(method, path, body):
        calls.append((method,path,body))
        if method == 'GET':
            if 'after=' in path:
                return {'results':[{'toObjectId':str(i)} for i in range(100,105)]}
            return {'results':[{'toObjectId':str(i)} for i in range(100)], 'paging':{'next':{'after':'next'}}}
        return {'results':[{'id':r['id'], 'properties':{'dealstage':'custom',
                            'hs_lastmodifieddate':'2026-10-01T00:00:00Z','hs_is_closed':'true'}} for r in body['inputs']]}
    deals = HubSpotCrm('UNUSED', transport=transport).deals('123')
    assert len(deals) == 105
    assert all(d.closed for d in deals)
    assert [len(body['inputs']) for method, _, body in calls if method == 'POST'] == [100,5]


def test_hubspot_incomplete_deal_batch_fails():
    transport = Mock(side_effect=[{'results':[{'toObjectId':'1'}]}, {'results':[]}])
    with pytest.raises(RuntimeError, match='Incomplete deal'):
        HubSpotCrm('UNUSED', transport=transport).deals('123')


def test_hubspot_dedupe_and_create_contract(source):
    transport = Mock(side_effect=[{'results':[{'id':'existing'}]}, {'id':'created'}])
    crm = HubSpotCrm('UNUSED', transport=transport)
    message = source.get_message('intro')
    assert crm.existing_email(message)
    filters = transport.call_args.args[2]['filterGroups'][0]['filters']
    assert int(filters[2]['value']) - int(filters[1]['value']) == 240000
    assert crm.create_email(message, Associations(), 'received') == 'created'
    assert transport.call_args.args[:2] == ('POST','/crm/v3/objects/emails')


def test_hubspot_http_errors_do_not_expose_secrets(monkeypatch):
    monkeypatch.setenv('TEST_TOKEN', 'secret-not-to-print')
    monkeypatch.setattr('crm_mail_sync.adapters.hubspot.time.sleep', lambda seconds: None)
    def fail(*args, **kwargs):
        raise HTTPError('https://api.hubapi.com', 429, 'secret-not-to-print', {}, None)
    monkeypatch.setattr('crm_mail_sync.adapters.hubspot.urlopen', fail)
    with pytest.raises(RuntimeError) as exc:
        HubSpotCrm('TEST_TOKEN').contact('partner@example.org')
    assert '429' in str(exc.value)
    assert 'secret-not-to-print' not in str(exc.value)


def test_unreadable_and_encrypted_mime_are_incomplete():
    raw = raw_message()
    raw['payload']['parts'] = [{'mimeType':'application/pkcs7-mime','body':{'data':encoded('encrypted')}}]
    assert normalise(raw, {}, ()).incomplete
    raw['payload']['parts'] = [{'mimeType':'text/plain','body':{'data':'a'}}]
    assert normalise(raw, {}, ()).incomplete


@pytest.mark.parametrize('name,value,expected', [
    ('List-Unsubscribe', '<mailto:leave@example.org>', True),
    ('List-Id', 'News <news.example.org>', True),
    ('Precedence', 'bulk', True),
    ('Precedence', 'first-class', False),
    ('Auto-Submitted', 'auto-generated', True),
    ('Auto-Submitted', 'no', False),
])
def test_mailing_list_headers(name, value, expected):
    raw = raw_message()
    raw['payload']['headers'].append({'name': name, 'value': value})
    assert normalise(raw, {}, ()).mailing_list is expected
    assert normalise(raw_message(), {}, ()).mailing_list is False


def test_hubspot_retries_rate_limited_reads_but_never_creates(monkeypatch):
    monkeypatch.setenv('TEST_TOKEN', 'token')
    sleeps, calls = [], []
    monkeypatch.setattr('crm_mail_sync.adapters.hubspot.time.sleep', sleeps.append)

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, *args): return b'{"results": []}'

    def flaky(request, timeout):
        calls.append(request.full_url)
        if len(calls) <= 2:
            raise HTTPError(request.full_url, 429, 'slow down', {'Retry-After': '3'}, None)
        return Response()
    monkeypatch.setattr('crm_mail_sync.adapters.hubspot.urlopen', flaky)
    assert HubSpotCrm('TEST_TOKEN').contact('partner@example.org') is None
    assert len(calls) == 3 and 3.0 in sleeps

    calls.clear()
    def always_429(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 429, 'slow down', {}, None)
    monkeypatch.setattr('crm_mail_sync.adapters.hubspot.urlopen', always_429)
    crm = HubSpotCrm('TEST_TOKEN')
    with pytest.raises(RuntimeError, match='429'):
        crm.request('POST', '/crm/v3/objects/emails', {'properties': {}})
    assert len(calls) == 1, 'a create must never be retried blindly'


@pytest.fixture
def mismatched_charset_message():
    raw = raw_message()
    raw['payload']['headers'] = [
        {'name': 'From', 'value': 'sender@example.com'},
        {'name': 'To', 'value': 'operator@example.com'},
    ]
    raw['payload']['parts'] = [{
        'mimeType': 'text/plain',
        'headers': [{'name': 'Content-Type', 'value': 'text/plain; charset=windows-1252'}],
        'body': {'data': encoded('“Hello José” — £25')},
    }]
    return raw


@pytest.mark.parametrize('charset', ['windows-1252', 'iso-8859-1', 'unknown-charset'])
def test_gmail_prefers_utf8_over_declared_charset(mismatched_charset_message, charset):
    raw = mismatched_charset_message
    raw['payload']['parts'][0]['headers'][0]['value'] = f'text/plain; charset={charset}'
    message = normalise(raw, {}, ())
    assert message.text == '“Hello José” — £25'
    assert not message.incomplete


@pytest.mark.parametrize('charset,body,expected', [
    ('windows-1252', '“Hello José” — £25'.encode('windows-1252'), '“Hello José” — £25'),
    ('iso-8859-1', 'José £25'.encode('iso-8859-1'), 'José £25'),
    ('unknown-charset', b'Hello \xff', 'Hello �'),
    ('ascii', b'Hello \xff', 'Hello �'),
])
def test_gmail_charset_fallbacks(mismatched_charset_message, charset, body, expected):
    raw = mismatched_charset_message
    part = raw['payload']['parts'][0]
    part['headers'][0]['value'] = f'text/plain; charset={charset}'
    part['body']['data'] = base64.urlsafe_b64encode(body).decode()
    message = normalise(raw, {}, ())
    assert message.text == expected
    assert not message.incomplete


@pytest.mark.parametrize('name,value,skip', [
    ('Auto-Submitted', 'auto-generated', True),
    ('Auto-Submitted', ' NO ', False),
    ('List-Unsubscribe', '<mailto:leave@example.com>', True),
    ('Precedence', ' BULK ', True),
    ('Precedence', 'first-class', False),
])
def test_automated_headers_skip_before_crm(config, name, value, skip):
    raw = raw_message()
    raw['payload']['parts'] = [{'mimeType': 'text/plain', 'body': {'data': encoded('Hello')}}]
    raw['payload']['headers'].append({'name': name, 'value': value})
    crm = Mock()
    outcome = evaluate(normalise(raw, {}, ()), config, crm)
    if skip:
        assert (outcome.decision.action, outcome.decision.reason) == ('skip', 'automated sender')
        crm.contact.assert_not_called()
    else:
        assert outcome.decision.reason != 'automated sender'
        crm.contact.assert_called()


def test_gmail_without_visible_recipients_skips_as_bcc(config):
    raw = raw_message()
    raw['labelIds'] = ['INBOX']
    raw['payload']['headers'] = [{'name': 'From', 'value': 'host@example.com'}]
    raw['payload']['parts'] = [{'mimeType': 'text/plain', 'body': {'data': encoded('Event details')}}]
    message = normalise(raw, {}, ())
    assert message.incomplete  # Missing recipients remains an adapter-level fact.
    crm = Mock()
    outcome = evaluate(message, config, crm)
    assert (outcome.decision.action, outcome.decision.reason) == ('skip', "mailbox owner only bcc'd")
    crm.contact.assert_not_called()
