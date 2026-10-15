from dataclasses import replace

import pytest

from crm_mail_sync.learned import LearnedRules
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
    approved = classify(source.get_message('unknown'), config, crm.contacts, approved=True)
    assert (approved.action, approved.new_contacts) == ('log', ('unknown@example.org',))


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


@pytest.mark.parametrize('recipients', [(), (Address('event@example.com'),)])
@pytest.mark.parametrize('incomplete', [False, True])
def test_bcc_only_skips_before_review_and_crm(config, source, recipients, incomplete):
    from unittest.mock import Mock
    from crm_mail_sync.pipeline import evaluate
    message = replace(source.get_message('intro'), sender=Address('host@example.com'),
                      to=recipients, cc=(), incomplete=incomplete, subject='term sheet', labels=())
    crm = Mock()
    outcome = evaluate(message, config, crm, approved=True)
    assert (outcome.decision.action, outcome.decision.reason) == ('skip', "mailbox owner only bcc'd")
    crm.contact.assert_not_called()


@pytest.mark.parametrize('placement', ['to', 'cc', 'sender', 'sent-label'])
def test_visible_owner_alias_or_sent_message_is_not_bcc_only(config, source, crm, placement):
    config = replace(config, mailbox_addresses=(*config.mailbox_addresses, 'alias@example.com'))
    message = replace(source.get_message('intro'), sender=Address('host@example.com'), to=(), cc=(), labels=())
    if placement in {'to', 'cc'}:
        message = replace(message, **{placement: (Address('ALIAS@example.com'),)})
    elif placement == 'sender':
        message = replace(message, sender=Address('alias@example.com'), to=(Address('guest@example.com'),))
    else:
        message = replace(message, labels=('SENT',), to=(Address('guest@example.com'),))
    assert classify(message, config, crm.contacts).reason != "mailbox owner only bcc'd"


def test_learned_log_creates_unknown_people_and_covers_keyword_holds(config, source, crm):
    # Arrange
    learned = LearnedRules(log=('example.org',))
    # Act
    unknown = classify(source.get_message('unknown'), config, crm.contacts, learned=learned)
    keyword = classify(source.get_message('keyword'), config, crm.contacts, learned=learned)
    # Assert
    assert (unknown.action, unknown.reason, unknown.new_contacts) == (
        'log', 'learned rule: log example.org', ('unknown@example.org',))
    assert (keyword.action, keyword.new_contacts) == ('log', ())
    assert [c.id for c in keyword.contacts] == ['partner']


def test_learned_log_never_creates_a_never_log_contact(config, source, crm):
    decision = classify(source.get_message('unknown-with-investor'), config, crm.contacts,
                        learned=LearnedRules(log=('example.org',)))
    assert decision.new_contacts == ('unknown@example.org',)
    assert all(c.email != 'investor@example.org' for c in decision.contacts)


def test_learned_skip_wins_even_over_an_existing_contact(config, source, crm):
    decision = classify(source.get_message('intro'), config, crm.contacts, learned=LearnedRules(skip=('example.org',)))
    assert (decision.action, decision.reason) == ('skip', 'learned rule: skip example.org')


def test_learned_rules_leave_hard_skips_alone(config, source, crm):
    decision = classify(source.get_message('private-message'), config, crm.contacts, learned=LearnedRules(log=('example.org',)))
    assert decision.reason == 'private label'


def test_personal_address_is_named_in_the_reason(config, source, crm):
    message = replace(source.get_message('unknown'), to=(Address('operator@example.com'), Address('Someone@gmail.com')))
    decision = classify(message, config, crm.contacts, learned=LearnedRules(log=('someone@gmail.com',)))
    assert (decision.reason, decision.new_contacts) == ('learned rule: log someone@gmail.com', ('someone@gmail.com',))
