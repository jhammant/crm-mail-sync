from datetime import date

import pytest

from crm_mail_sync.config import DEFAULT_PERSONAL_EMAIL_DOMAINS as PERSONAL
from crm_mail_sync.learned import LearnedRules, learn, load_learned, party


def test_missing_file_is_no_rules(tmp_path):
    assert load_learned(tmp_path / 'learned.yaml') == LearnedRules()


def test_learn_round_trip_is_private_and_readable(tmp_path):
    # Arrange
    path = tmp_path / 'learned.yaml'
    # Act
    changed = learn(path, 'log', ['Partner.Example.org', 'partner.example.org'], PERSONAL, note='review page', today=date(2026, 10, 15))
    # Assert
    assert changed == ['partner.example.org']
    assert load_learned(path) == LearnedRules(log=('partner.example.org',))
    text = path.read_text()
    assert text.startswith('# Learned by crm-mail-sync')
    assert 'added: \'2026-10-15\'' in text and 'note: review page' in text
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_new_decision_replaces_the_opposite_one(tmp_path):
    path = tmp_path / 'learned.yaml'
    learn(path, 'log', ['vendor.example.com'], PERSONAL)
    learn(path, 'skip', ['vendor.example.com'], PERSONAL)
    assert load_learned(path) == LearnedRules(skip=('vendor.example.com',))


def test_personal_domains_are_remembered_per_address(tmp_path):
    path = tmp_path / 'learned.yaml'
    with pytest.raises(ValueError, match='personal email domain'):
        learn(path, 'log', ['gmail.com'], PERSONAL)
    learn(path, 'log', ['someone@gmail.com'], PERSONAL)
    assert party('Someone@Gmail.com', PERSONAL) == 'someone@gmail.com'
    assert party('pat@partner.example.org', PERSONAL) == 'partner.example.org'


@pytest.mark.parametrize('bad', ['not a domain', 'someone@', ''])
def test_invalid_matches_are_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        learn(tmp_path / 'learned.yaml', 'log', [bad], PERSONAL)


@pytest.mark.parametrize(('rules', 'email', 'expected'), [
    (LearnedRules(log=('example.org',)), 'a@example.org', 'log'),
    (LearnedRules(log=('example.org',)), 'a@eu.example.org', 'log'),
    (LearnedRules(log=('example.org',)), 'a@notexample.org', None),
    (LearnedRules(log=('example.org',), skip=('a@example.org',)), 'a@example.org', 'skip'),
    (LearnedRules(log=('a@example.org',), skip=('example.org',)), 'a@example.org', 'log'),
    (LearnedRules(log=('example.org',), skip=('example.org',)), 'b@example.org', 'skip'),
])
def test_address_beats_domain_and_skip_beats_log(rules, email, expected):
    assert rules.action_for(email) == expected


def test_hand_edits_are_validated(tmp_path):
    path = tmp_path / 'learned.yaml'
    path.write_text('log:\n  - match: not a domain\n')
    with pytest.raises(ValueError, match='address or a domain'):
        load_learned(path)
    path.write_text('allow: []\n')
    with pytest.raises(ValueError, match='only log and skip'):
        load_learned(path)
