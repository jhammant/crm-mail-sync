"""The Roost menu-bar plugin in integrations/roost, run as Roost runs it."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

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
    entries = {item['title']: item for item in menu['items'] if 'items' in item}
    keyword = [a['title'] for a in entries['Project term sheet']['items']]
    assert 'Log to HubSpot…' in keyword and 'Skip (never log it)' in keyword
    approve = next(a for a in entries['Project term sheet']['items'] if a['title'] == 'Log to HubSpot…')
    assert approve['confirm'].startswith('Log "Project term sheet" to HubSpot?')
    assert not any(a.get('run', [''])[-2:-1] == ['approve'] for a in entries['incomplete']['items']), \
        'an unreadable message can never be approved, so it is not offered'

    skipped = plugin(plugin_env, 'skip', 'keyword')
    assert (skipped.returncode, skipped.stdout.strip()) == (0, 'keyword: skipped')
    refused = plugin(plugin_env, 'approve', 'incomplete')
    assert refused.returncode == 1 and 'Cannot approve safely' in refused.stdout
    assert json.loads(plugin(plugin_env).stdout)['badge'] == {'text': '3', 'level': 'warn'}
    assert plugin(plugin_env, 'dry-run').stdout.strip() == 'dry-run: 0 message(s); log=0 skip=0 review=0'
