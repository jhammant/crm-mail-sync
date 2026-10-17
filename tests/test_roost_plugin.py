"""The Roost menu-bar plugin in integrations/roost, run as Roost runs it."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from crm_mail_sync.cli import main

PLUGIN = Path('integrations/roost/roost_plugin.py').resolve()
FIXTURE = Path('fixtures/sample-mailbox.json').resolve()


@pytest.fixture
def plugin_env(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text(Path('examples/config.example.yaml').read_text().replace('../.local/', str(tmp_path) + '/'))
    binary = shutil.which('crm-mail-sync') or str(Path(sys.executable).parent / 'crm-mail-sync')
    return dict(os.environ, CRM_MAIL_SYNC_CONFIG=str(config), CRM_MAIL_SYNC_BIN=binary,
                CRM_MAIL_SYNC_SOURCE=str(FIXTURE), CRM_MAIL_SYNC_CRM='fake', ROOST_PLUGIN_DIR=str(PLUGIN.parent))


def plugin(env, *args):
    return subprocess.run([sys.executable, str(PLUGIN), *args], capture_output=True, text=True, env=env, timeout=60)


def test_manifest_is_valid_for_roost():
    manifest = json.loads(Path('integrations/roost/plugin.json').read_text())
    assert manifest['id'] == 'crm-mail-sync'
    assert manifest['command'] == ['./roost_plugin.py']
    assert os.access(PLUGIN, os.X_OK)
    assert set(manifest['requires']) == {'python3', 'crm-mail-sync'}


def test_missing_config_explains_setup(plugin_env, tmp_path):
    menu = json.loads(plugin(dict(plugin_env, CRM_MAIL_SYNC_CONFIG=str(tmp_path / 'nope.yaml'))).stdout)
    assert menu['items'][0]['title'].startswith('No config at')
    assert 'badge' not in menu


def test_queue_badges_and_actions_resolve_items(plugin_env):
    menu = json.loads(plugin(plugin_env).stdout)
    assert menu['badge'] is None and menu['items'][0]['title'] == 'Never synced (dry-runs are not recorded)'

    assert main(['run', '--config', plugin_env['CRM_MAIL_SYNC_CONFIG'], '--source', str(FIXTURE), '--crm', 'fake']) == 0
    menu = json.loads(plugin(plugin_env).stdout)
    assert menu['badge'] == {'text': '4', 'level': 'warn'}
    assert any(i['title'] == '4 emails waiting for review from 1 company or person' for i in menu['items'])
    group = next(item for item in menu['items'] if item['title'] == 'example.org · 4 emails')
    actions = {a['title']: a for a in group['items']}
    assert actions['Log all and remember…']['run'][-2:] == ['log-group', 'example.org']
    assert 'log future mail with example.org automatically' in actions['Log all and remember…']['confirm']
    assert actions['Skip all and remember']['run'][-2:] == ['skip-group', 'example.org']
    emails = {e['title']: [a['title'] for a in e['items']] for e in actions['One by one']['items']}
    assert 'Log just this one…' in emails['Project term sheet'] and 'Skip just this one' in emails['Project term sheet']
    assert 'Log just this one…' not in emails['incomplete'], 'an unreadable message can never be approved'

    skipped = plugin(plugin_env, 'skip', 'keyword')
    assert (skipped.returncode, skipped.stdout.strip()) == (0, 'keyword: skipped')
    refused = plugin(plugin_env, 'approve', 'incomplete')
    assert refused.returncode == 1 and 'Cannot approve safely' in refused.stdout
    assert json.loads(plugin(plugin_env).stdout)['badge'] == {'text': '3', 'level': 'warn'}

    logged = plugin(plugin_env, 'log-group', 'example.org')
    assert (logged.returncode, logged.stdout.strip()) == (0, 'recheck: 3 pending; log=2 skip=0 still review=1')
    assert json.loads(plugin(plugin_env).stdout)['badge'] == {'text': '1', 'level': 'warn'}
    refused = plugin(plugin_env, 'log-group', 'gmail.com')
    assert refused.returncode == 1 and 'personal email domain' in refused.stdout
    assert plugin(plugin_env, 'dry-run').stdout.strip() == 'dry-run: 0 message(s); log=0 skip=0 review=0; queue: 0 decided, 1 waiting'


def test_each_person_in_a_company_can_be_flagged_on_their_own(plugin_env):
    # Arrange
    assert main(['run', '--config', plugin_env['CRM_MAIL_SYNC_CONFIG'], '--source', str(FIXTURE), '--crm', 'fake']) == 0
    menu = json.loads(plugin(plugin_env).stdout)
    group = next(item for item in menu['items'] if item['title'] == 'example.org · 4 emails')
    people = next(a for a in group['items'] if a['title'] == '2 people')
    partner = next(p for p in people['items'] if p['title'] == 'Partner <partner@example.org>')
    actions = {a['title']: a for a in partner['items']}
    learned = Path(plugin_env['CRM_MAIL_SYNC_CONFIG']).parent / 'learned.yaml'

    # Act
    synced = plugin(plugin_env, *actions['Sync this person…']['run'][1:])
    skipped = plugin(plugin_env, 'skip-person', 'unknown@example.org')

    # Assert
    assert actions['Sync this person…']['run'][-2:] == ['log-person', 'partner@example.org']
    assert 'Log email with Partner <partner@example.org>' in actions['Sync this person…']['confirm']
    assert actions['Skip this person from now on']['run'][-2:] == ['skip-person', 'partner@example.org']
    assert synced.returncode == 0 and synced.stdout.startswith('recheck:')
    assert skipped.returncode == 0 and skipped.stdout.startswith('recheck:')
    rules = {action: [row['match'] for row in rows] for action, rows in yaml.safe_load(learned.read_text()).items()}
    assert rules == {'log': ['partner@example.org'], 'skip': ['unknown@example.org']}, \
        'each person is remembered on their own, and the rest of the company is left alone'


def test_flag_someone_who_was_never_in_the_queue(plugin_env):
    # Arrange
    menu = json.loads(plugin(plugin_env).stdout)
    flag = next(item for item in menu['items'] if item['title'] == 'Flag someone to sync…')
    learned = Path(plugin_env['CRM_MAIL_SYNC_CONFIG']).parent / 'learned.yaml'

    # Act
    person = plugin(plugin_env, 'flag', 'New.Contact@Acme.test')
    company = plugin(plugin_env, 'flag', 'globex.test')
    refused = plugin(plugin_env, 'flag', 'not an address')

    # Assert
    assert flag['run'][-1] == 'flag'
    assert person.returncode == 0 and company.returncode == 0
    assert 'match: new.contact@acme.test' in learned.read_text() and 'match: globex.test' in learned.read_text()
    assert refused.returncode == 1 and 'Not an address or a domain' in refused.stdout
