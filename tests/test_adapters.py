import base64
from dataclasses import replace
import json
from urllib.error import HTTPError
from unittest.mock import Mock

import pytest

from crm_mail_sync.adapters.gmail import GmailSource, normalise
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
