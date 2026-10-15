import json
from pathlib import Path
import os
import re
import subprocess
import sys
import yaml

from crm_mail_sync.cli import main
from crm_mail_sync.config import load_config


def configured(tmp_path):
    data = yaml.safe_load(Path('examples/config.example.yaml').read_text())
    data.update(state_path='state.json', review_path='review.jsonl', gmail_token_path='token.json',
                learned_rules_path='learned.yaml')
    path = tmp_path/'config.yaml'
    path.write_text(yaml.safe_dump(data))
    return path


def test_cli_run_review_approve_skip_end_to_end(tmp_path):
    config = configured(tmp_path)
    def cli(*args):
        return subprocess.run([sys.executable, '-m', 'crm_mail_sync.cli', *args, '--config',str(config)],
                              text=True, capture_output=True, check=True)
    first = cli('run','--source','fixtures/sample-mailbox.json','--crm','fake')
    assert '13 message(s); log=1 skip=8 review=4' in first.stdout
    assert '4 pending review item(s)' in cli('review').stdout
    assert 'log keyword: review approved' in cli('approve','keyword','--source','fixtures/sample-mailbox.json','--crm','fake').stdout
    assert 'unknown: skipped' in cli('skip','unknown').stdout
    assert '2 pending review item(s)' in cli('review').stdout
    second = cli('run','--source','fixtures/sample-mailbox.json','--crm','fake','--since','2026-10-01')
    assert '0 message(s)' in second.stdout


def test_cli_dry_run_leaves_files_unchanged(tmp_path):
    config = configured(tmp_path)
    before = {p.name:p.read_bytes() for p in tmp_path.iterdir()}
    result = subprocess.run([sys.executable,'-m','crm_mail_sync.cli','run','--config',str(config),
                             '--source','fixtures/sample-mailbox.json','--crm','fake','--dry-run'],
                            text=True,capture_output=True,check=True)
    assert 'dry-run: 13 message(s); log=1 skip=8 review=4' in result.stdout
    assert {p.name:p.read_bytes() for p in tmp_path.iterdir()} == before


def test_config_validation_and_cli_errors(tmp_path, capsys):
    config = configured(tmp_path)
    data = yaml.safe_load(config.read_text())
    data['never_log'] = ['not a domain']
    config.write_text(yaml.safe_dump(data))
    assert main(['review','--config',str(config)]) == 1
    assert 'never_log entries' in capsys.readouterr().err


def test_repository_sample_privacy():
    paths = [Path('README.md')]
    for directory in ['src','examples','fixtures','integrations']:
        paths.extend(p for p in Path(directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    email = re.compile(r'[\w.+-]+@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})')
    failures = []
    for path in paths:
        for domain in email.findall(path.read_text()):
            if domain.lower() not in {'example.com','example.org'}:
                failures.append(f'{path}: non-example address')
    assert failures == []


def test_status_reports_queue_and_counts_without_writing(tmp_path, capsys):
    config = tmp_path / 'config.yaml'
    example = Path('examples/config.example.yaml').read_text()
    config.write_text(example.replace('../.local/', str(tmp_path) + '/'))
    assert main(['status', '--config', str(config), '--json']) == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty == {'mailbox': 'operator@example.com', 'last_run': None,
                     'outcomes': {'log': 0, 'skip': 0, 'review': 0}, 'pending': []}
    assert not (tmp_path / 'state.json').exists(), 'status must never create state'

    assert main(['run', '--config', str(config), '--source', 'fixtures/sample-mailbox.json', '--crm', 'fake']) == 0
    capsys.readouterr()
    before = (tmp_path / 'state.json').read_bytes(), (tmp_path / 'review.jsonl').read_bytes()
    assert main(['status', '--config', str(config), '--json']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['last_run'] is not None
    assert report['outcomes'] == {'log': 1, 'skip': 8, 'review': 4}
    assert [item['id'] for item in report['pending']] == ['unknown-with-investor', 'keyword', 'unknown', 'incomplete']
    assert set(report['pending'][0]) == {'id', 'reason', 'subject', 'from', 'timestamp', 'thread_id'}
    assert ((tmp_path / 'state.json').read_bytes(), (tmp_path / 'review.jsonl').read_bytes()) == before

    assert main(['status', '--config', str(config)]) == 0
    assert capsys.readouterr().out.startswith('last run: ')


def test_automated_sender_config_defaults_and_overrides(tmp_path):
    path = configured(tmp_path)
    data = yaml.safe_load(path.read_text())
    data.pop('automated_sender_patterns', None)
    data.pop('skip_automated_senders', None)
    path.write_text(yaml.safe_dump(data))
    config = load_config(path)
    assert config.skip_automated_senders is True
    assert 'drive-shares*@*' in config.automated_sender_patterns
    data.update(automated_sender_patterns=[' CUSTOM-*@EXAMPLE.COM '], skip_automated_senders=False)
    path.write_text(yaml.safe_dump(data))
    config = load_config(path)
    assert config.automated_sender_patterns == ('custom-*@example.com',)
    assert config.skip_automated_senders is False
    data['automated_sender_patterns'] = []
    path.write_text(yaml.safe_dump(data))
    assert load_config(path).automated_sender_patterns == ()


def test_automated_sender_config_rejects_invalid_values(tmp_path):
    import pytest
    path = configured(tmp_path)
    original = yaml.safe_load(path.read_text())
    for key, value in [('automated_sender_patterns', 'noreply*'),
                       ('automated_sender_patterns', ['']),
                       ('automated_sender_patterns', [1]),
                       ('skip_automated_senders', 'false')]:
        path.write_text(yaml.safe_dump(dict(original, **{key: value})))
        with pytest.raises(ValueError, match=key):
            load_config(path)


def test_cli_learned_rules_end_to_end(tmp_path):
    # Arrange
    config = configured(tmp_path)
    fixture = ['--source', 'fixtures/sample-mailbox.json', '--crm', 'fake']
    def cli(*args):
        return subprocess.run([sys.executable, '-m', 'crm_mail_sync.cli', *args, '--config', str(config)],
                              text=True, capture_output=True, check=True).stdout
    cli('run', *fixture)
    # Act and assert: one group, then a remembered skip, then a learned log that recheck applies
    assert 'example.org: 4 email(s)' in cli('groups')
    assert json.loads(cli('groups', '--json'))[0]['key'] == 'example.org'
    assert 'learned log: example.org' in cli('learn', 'log', 'example.org', '--note', 'test')
    assert 'nothing new' in cli('learn', 'log', 'example.org')
    assert 'recheck dry-run: 4 pending; log=3 skip=0 still review=1' in cli('recheck', '--dry-run', *fixture)
    assert '4 pending review item(s)' in cli('review')
    assert 'recheck: 4 pending; log=3 skip=0 still review=1' in cli('recheck', *fixture)
    assert '1 pending review item(s)' in cli('review')
    assert 'remembered skip: example.org' in cli('skip', 'incomplete', '--remember')
    learned = yaml.safe_load((tmp_path / 'learned.yaml').read_text())
    assert [r['match'] for r in learned['skip']] == ['example.org'] and learned['log'] == []
    assert (tmp_path / 'learned.yaml').stat().st_mode & 0o777 == 0o600


def test_cli_refuses_a_personal_domain_rule(tmp_path, capsys):
    config = configured(tmp_path)
    assert main(['learn', 'log', 'gmail.com', '--config', str(config)]) == 1
    assert 'personal email domain' in capsys.readouterr().err


def test_every_run_rechecks_the_queue(tmp_path):
    # Arrange
    config = configured(tmp_path)
    fixture = ['--source', 'fixtures/sample-mailbox.json', '--crm', 'fake']
    def cli(*args):
        return subprocess.run([sys.executable, '-m', 'crm_mail_sync.cli', *args, '--config', str(config)],
                              text=True, capture_output=True, check=True).stdout
    assert 'review=4; queue: 0 decided, 0 waiting' in cli('run', *fixture)
    cli('learn', 'log', 'example.org')
    # Act
    second = cli('run', *fixture)
    # Assert
    assert 'run: 0 message(s); log=0 skip=0 review=0; queue: 3 decided, 1 waiting' in second
    assert len([line for line in second.splitlines() if line.startswith('queue ')]) == 3
    assert '1 pending review item(s)' in cli('review')
