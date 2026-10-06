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
    data.update(state_path='state.json', review_path='review.jsonl', gmail_token_path='token.json')
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
