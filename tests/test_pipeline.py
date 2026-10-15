from dataclasses import replace
from datetime import timedelta
import json

import pytest

from crm_mail_sync.models import timestamp
from crm_mail_sync.learned import learn
from crm_mail_sync.pipeline import evaluate, recheck, resolve_review, review_groups, run
from crm_mail_sync.state import State, run_lock


def test_intro_associations_and_idempotency(config, source, crm, review):
    first = run(config, source, crm, review)
    second = run(config, source, crm, review, since=timestamp('2026-10-01T00:00:00Z'))
    assert len(first) == 13
    assert second == []
    assert len(crm.created) == 1
    record = crm.created[0]
    assert record['message'].id == 'intro'
    assert [c.id for c in record['associations'].contacts] == ['partner']
    assert record['associations'].companies == ('company-partner',)
    assert record['associations'].deals == ('d7','d6','d5','d4','d3')
    assert len(review.pending()) == 4


@pytest.mark.parametrize('existing', [False, True])
def test_dry_run_changes_nothing(config, source, crm, review, existing):
    if existing:
        config.state_path.write_text('{"version":1,"outcomes":{}}')
        review.enqueue(source.get_message('keyword'), 'existing review')
    before = {p.name: p.read_bytes() for p in config.state_path.parent.iterdir()}
    outcomes = run(config, source, crm, review, dry_run=True)
    after = {p.name: p.read_bytes() for p in config.state_path.parent.iterdir()}
    assert before == after
    assert len(outcomes) == 13
    assert not crm.created


@pytest.mark.parametrize(('offset', 'action'), [(-121,'log'),(-120,'skip'),(120,'skip'),(121,'log')])
def test_duplicate_window(config, source, crm, offset, action):
    message = source.get_message('intro')
    crm.emails = [{'subject':message.subject, 'timestamp':(message.timestamp+timedelta(seconds=offset)).isoformat()}]
    assert evaluate(message, config, crm).decision.action == action


def test_approval_and_skip(config, source, crm, review):
    run(config, source, crm, review)
    outcome = resolve_review(config, source, crm, review, 'keyword', approve=True)
    assert outcome.decision.action == 'log'
    assert len(crm.created) == 2
    assert 'keyword' not in review.pending()
    resolve_review(config, None, None, review, 'unknown', approve=False)
    assert 'unknown' not in review.pending()
    with pytest.raises(ValueError, match='No pending'):
        resolve_review(config, source, crm, review, 'keyword', approve=True)
    # Approving people who aren't in the CRM creates them; a never-log contact is never created or linked.
    outcome = resolve_review(config, source, crm, review, 'unknown-with-investor', approve=True)
    assert [c.email for c in crm.created_contacts] == ['unknown@example.org']
    assert [c.email for c in outcome.associations.contacts] == []
    assert [c.email for c in crm.created[-1]['associations'].contacts] == ['unknown@example.org']


def test_approval_refreshes_private_label(config, source, crm, review, monkeypatch):
    run(config, source, crm, review)
    message = replace(source.get_message('keyword'), thread_labels=(config.private_label,))
    monkeypatch.setattr(source, 'get_message', lambda mid: message)
    outcome = resolve_review(config, source, crm, review, 'keyword', approve=True)
    assert outcome.decision.reason == 'private label'
    assert len(crm.created) == 1
    assert 'keyword' not in review.pending()


def test_failure_does_not_record_or_advance_cursor(config, source, crm, review, monkeypatch):
    def fail(*args):
        raise RuntimeError('service unavailable')
    monkeypatch.setattr(crm, 'create_email', fail)
    with pytest.raises(RuntimeError, match='service unavailable'):
        run(config, source, crm, review)
    assert not config.state_path.exists()
    assert not review.pending()


def test_review_enqueue_idempotent_and_private_permissions(config, source, review):
    review.enqueue(source.get_message('keyword'), 'keyword')
    review.enqueue(source.get_message('keyword'), 'keyword')
    assert len(config.review_path.read_text().splitlines()) == 1
    assert config.review_path.stat().st_mode & 0o777 == 0o600
    state = State(config.state_path)
    state.record('test', 'skip', 'test')
    assert config.state_path.stat().st_mode & 0o777 == 0o600


def test_lock_refuses_concurrent_mutation(config):
    with run_lock(config.state_path):
        with pytest.raises(RuntimeError, match='Another run'):
            with run_lock(config.state_path):
                pass


def test_inclusive_since(config, source, crm, review):
    outcomes = run(config, source, crm, review, dry_run=True, since=timestamp('2026-10-02T09:12:00Z'))
    assert [o.message.id for o in outcomes] == ['incomplete']


def test_review_resolution_recovers_after_state_write(config, source, crm, review):
    review.enqueue(source.get_message('keyword'), 'keyword')
    State(config.state_path).record('keyword', 'log', 'review approved', 'existing-id')
    assert resolve_review(config, source, crm, review, 'keyword', approve=True) is None
    assert not review.pending()
    assert not crm.created


def test_learned_log_creates_the_contact_then_logs(config, source, crm, review):
    # Arrange
    crm.company_domains = {'example.org': 'company-partner'}
    learn(config.learned_rules_path, 'log', ['example.org'], config.personal_email_domains)
    # Act
    run(config, source, crm, review)
    # Assert
    assert [c.email for c in crm.created_contacts] == ['unknown@example.org']
    logged = {r['message'].id: r for r in crm.created}
    assert set(logged) == {'intro', 'keyword', 'unknown', 'unknown-with-investor'}
    assert [c.email for c in logged['unknown']['associations'].contacts] == ['unknown@example.org']
    assert logged['unknown']['associations'].companies == ('company-partner',)
    assert set(review.pending()) == {'incomplete'}


def test_dry_run_with_learned_rules_creates_nothing(config, source, crm, review):
    learn(config.learned_rules_path, 'log', ['example.org'], config.personal_email_domains)
    outcomes = run(config, source, crm, review, dry_run=True)
    assert not crm.created and not crm.created_contacts
    assert {o.message.id: o.decision.new_contacts for o in outcomes}['unknown'] == ('unknown@example.org',)


def test_recheck_clears_what_new_rules_decide(config, source, crm, review):
    # Arrange
    run(config, source, crm, review)
    assert set(review.pending()) == {'keyword', 'unknown', 'unknown-with-investor', 'incomplete'}
    learn(config.learned_rules_path, 'log', ['example.org'], config.personal_email_domains)
    # Act
    dry = recheck(config, source, crm, review, dry_run=True)
    assert len(review.pending()) == 4 and len(crm.created) == 1
    outcomes = recheck(config, source, crm, review)
    # Assert
    assert [o.decision.action for o in dry] == [o.decision.action for o in outcomes]
    assert set(review.pending()) == {'incomplete'}
    assert State(config.state_path).data['outcomes']['unknown']['action'] == 'log'
    assert [c.email for c in crm.created_contacts] == ['unknown@example.org']


def test_recheck_skips_what_was_learned_as_skip(config, source, crm, review):
    run(config, source, crm, review)
    learn(config.learned_rules_path, 'skip', ['example.org'], config.personal_email_domains)
    recheck(config, source, crm, review)
    # The keyword email is with an existing contact, so the skip doesn't apply and it stays held.
    assert set(review.pending()) == {'incomplete', 'keyword'}
    assert len(crm.created) == 1 and not crm.created_contacts


def test_review_groups(config, source, crm, review):
    run(config, source, crm, review)
    groups = review_groups(config, review)
    assert [(g['key'], sorted(g['ids'])) for g in groups] == [
        ('example.org', ['incomplete', 'keyword', 'unknown', 'unknown-with-investor'])]
    assert groups[0]['people'] == {'partner@example.org': 'Partner', 'unknown@example.org': 'Unknown'}
    assert groups[0]['personal'] is False
