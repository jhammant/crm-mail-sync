from dataclasses import replace

import pytest

from crm_mail_sync.models import Address, Contact
from crm_mail_sync.rules import classify, domain_matches, recent_open_deals


@pytest.mark.parametrize(('mid', 'action', 'reason'), [
    ('intro', 'log', 'existing eligible contacts'),
    ('investor-only', 'skip', 'only never-log/internal participants'),
    ('internal-only', 'skip', 'internal-only participants'),
    ('private-message', 'skip', 'private label'),
    ('private-thread', 'skip', 'private label'),
    ('calendar-subject', 'skip', 'calendar robot'),
    ('calendar-part', 'skip', 'calendar robot'),
    ('bulk', 'skip', 'automated sender'),
    ('unknown-with-investor', 'review', 'unknown external participants with never-log participant'),
    ('keyword', 'review', 'review keyword: term sheet'),
    ('unknown', 'review', 'no eligible CRM contacts; cannot decide'),
    ('incomplete', 'review', 'incomplete message or participants'),
])
def test_acceptance_rules(config, source, crm, mid, action, reason):
    message = source.get_message(mid)
    decision = classify(message, config, crm.contacts)
    assert (decision.action, decision.reason) == (action, reason)
    assert all(c.email != 'investor@example.org' for c in decision.contacts)


@pytest.mark.parametrize('prefix', ['Accepted:', 'Declined:', 'Tentative:', 'Invitation:', 'Updated invitation:'])
def test_calendar_prefixes(config, source, crm, prefix):
    message = replace(source.get_message('intro'), subject=prefix+' Meeting')
    assert classify(message, config, crm.contacts).reason == 'calendar robot'


@pytest.mark.parametrize('sender', ['no-reply', 'noreply', 'notifications', 'mailer-daemon', 'no_reply+robot'])
def test_bulk_variants(config, source, crm, sender):
    message = replace(source.get_message('intro'), sender=Address(sender+'@example.org'))
    assert classify(message, config, crm.contacts).reason == 'automated sender'


def test_mailing_list_headers_are_bulk_even_from_a_known_contact(config, source, crm):
    message = replace(source.get_message('intro'), mailing_list=True)
    assert classify(message, config, crm.contacts).reason == 'automated sender'


def test_domain_boundary_and_never_log_domain(config, source, crm):
    assert domain_matches('someone@child.example.org', 'example.org')
    assert not domain_matches('someone@notexample.org', 'example.org')
    config = replace(config, never_log=('example.org',))
    assert classify(source.get_message('intro'), config, crm.contacts).reason == 'only never-log/internal participants'


def test_closed_deals_excluded_and_ordered(crm):
    assert [d.id for d in recent_open_deals(crm.deals('partner'))] == ['d7','d6','d5','d4','d3']


def test_approval_never_overrides_hard_rules(config, source, crm):
    assert classify(source.get_message('private-message'), config, crm.contacts, approved=True).action == 'skip'
    assert classify(source.get_message('keyword'), config, crm.contacts, approved=True).action == 'log'
    assert classify(source.get_message('unknown'), config, crm.contacts, approved=True).action == 'review'


def test_keyword_boundaries(config, source, crm):
    message = replace(source.get_message('intro'), subject='Onboarding keyboard', text='A dashboard')
    assert classify(message, config, crm.contacts).action == 'log'


@pytest.mark.parametrize('html', ['<p>term&nbsp;sheet</p>', '<p>term</p><p>sheet</p>', '<p>sa<b>la</b>ry</p>'])
def test_html_keywords_cannot_hide_in_markup(config, source, crm, html):
    message = replace(source.get_message('intro'), text='', html=html)
    assert classify(message, config, crm.contacts).action == 'review'


@pytest.mark.parametrize('sender', [
    'noreply', 'no-reply', 'no_reply', 'no.reply', 'do-not-reply',
    'donotreply', 'do_not_reply', 'do.not.reply', 'notifications',
    'drive-shares-noreply', 'docusign', 'echosign', 'mailer-daemon',
])
def test_automated_senders_skip_before_review(config, source, crm, sender):
    message = replace(source.get_message('keyword'), sender=Address(sender + '@example.com'), incomplete=True)
    decision = classify(message, config, crm.contacts, approved=True)
    assert (decision.action, decision.reason) == ('skip', 'automated sender')


def test_automated_sender_patterns_override_defaults(config, source, crm):
    config = replace(config, automated_sender_patterns=('custom-*@example.com',))
    message = replace(source.get_message('intro'), sender=Address('CUSTOM-ALERT@example.com'))
    assert classify(message, config, crm.contacts).reason == 'automated sender'
    message = replace(message, sender=Address('noreply@example.com'))
    assert classify(message, config, crm.contacts).reason != 'automated sender'
    assert classify(replace(message, mailing_list=True), config, crm.contacts).reason == 'automated sender'


def test_automated_sender_skip_can_be_disabled(config, source, crm):
    config = replace(config, skip_automated_senders=False)
    message = replace(source.get_message('intro'), sender=Address('noreply@example.com'), mailing_list=True)
    assert classify(message, config, crm.contacts).reason != 'automated sender'
