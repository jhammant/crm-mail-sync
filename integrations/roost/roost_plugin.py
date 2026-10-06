#!/usr/bin/env python3
"""Roost plugin for crm-mail-sync: the review queue in the menu bar.

With no arguments it prints Roost's menu JSON. Roost calls it back with
`approve ID`, `skip ID`, `sync` or `dry-run` when you click, and shows the
first line it prints as a notification.

Environment (set in plugin.json):
  CRM_MAIL_SYNC_CONFIG    your private config file
  CRM_MAIL_SYNC_ENV_FILE  optional 0600 file of `export NAME=value` lines (HUBSPOT_TOKEN),
                          the same file the README's cron example sources
  CRM_MAIL_SYNC_BIN       the crm-mail-sync executable, if not on the PATH
  CRM_MAIL_SYNC_SOURCE    demo mode: a fixture mailbox, with CRM_MAIL_SYNC_CRM=fake
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

SHOWN = 15          # review items listed before "and N more"
STALE_HOURS = 26    # twice-daily schedule: a day and a bit without a run means it stopped


def environment() -> dict:
    env = dict(os.environ)
    env_file = Path(os.environ.get('CRM_MAIL_SYNC_ENV_FILE', '')).expanduser()
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            line = line.strip().removeprefix('export ').strip()
            if line and not line.startswith('#') and '=' in line:
                name, _, value = line.partition('=')
                env[name.strip()] = value.strip().strip('"').strip("'")
    return env


def tool(*args: str, live: bool = False) -> subprocess.CompletedProcess:
    command = [os.environ.get('CRM_MAIL_SYNC_BIN', 'crm-mail-sync'), *args,
               '--config', str(Path(os.environ['CRM_MAIL_SYNC_CONFIG']).expanduser())]
    if live and os.environ.get('CRM_MAIL_SYNC_SOURCE'):
        command += ['--source', os.environ['CRM_MAIL_SYNC_SOURCE'], '--crm', os.environ.get('CRM_MAIL_SYNC_CRM', 'fake')]
    return subprocess.run(command, capture_output=True, text=True, env=environment(), timeout=600)


def ago(iso: str) -> tuple[str, float]:
    then = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    hours = (datetime.now(timezone.utc) - then).total_seconds() / 3600
    if hours < 1:
        return f'{max(1, round(hours * 60))}m ago', hours
    return (f'{round(hours)}h ago' if hours < 48 else f'{round(hours / 24)}d ago'), hours


def short(text: str, limit: int) -> str:
    text = ' '.join(text.split()) or '(no subject)'
    return text if len(text) <= limit else text[:limit - 1] + '…'


def me() -> str:
    return os.environ.get('ROOST_PLUGIN_DIR', '.') + '/roost_plugin.py'


def menu() -> dict:
    config = Path(os.environ.get('CRM_MAIL_SYNC_CONFIG', '')).expanduser()
    if not config.is_file():
        return {'title': 'CRM mail sync', 'items': [
            {'title': f'No config at {config}'},
            {'title': 'Set up crm-mail-sync…', 'open': 'https://github.com/jhammant/crm-mail-sync#setup-for-a-real-mailbox'}]}
    result = tool('status', '--json')
    if result.returncode != 0:
        reason = (result.stderr.strip() or result.stdout.strip() or f'exit {result.returncode}').splitlines()[0]
        return {'title': 'CRM mail sync', 'badge': {'text': '!', 'level': 'alert'},
                'items': [{'title': short(reason, 90)}, {'title': 'Open config folder', 'open': str(config.parent)}]}
    report = json.loads(result.stdout)
    pending, counts = report['pending'], report['outcomes']
    items, badge = [], None

    if report['last_run']:
        when, hours = ago(report['last_run'])
        items.append({'title': f"Last sync {when} · {counts['log']} logged · {counts['skip']} skipped"})
        if hours > STALE_HOURS:
            items.append({'title': f'⚠︎ No sync for {when.removesuffix(" ago")}: is the schedule running?'})
            badge = {'text': 'sync', 'level': 'warn'}
    else:
        items.append({'title': 'Never synced (dry-runs are not recorded)'})

    if pending:
        badge = {'text': str(len(pending)), 'level': 'warn'}
        items.append({'title': f'{len(pending)} email{"s" if len(pending) != 1 else ""} waiting for review'})
        demo = bool(os.environ.get('CRM_MAIL_SYNC_SOURCE'))
        for item in pending[:SHOWN]:
            sender = item['from']
            who = f"{sender['name']} <{sender['email']}>" if sender.get('name') else sender['email']
            subject = short(item['subject'], 60)
            actions = [{'title': f"From {short(who, 70)} · {item['timestamp'][:16].replace('T', ' ')}"},
                       {'title': f"Why: {item['reason']}"}]
            if not demo:
                actions.append({'title': 'Open in Gmail',
                                'open': f"https://mail.google.com/mail/?authuser={report['mailbox']}#all/{item['thread_id']}"})
            approve = {'run': [me(), 'approve', item['id']],
                       'confirm': f'Log "{subject}" to HubSpot? Privacy rules are checked again first.'}
            if item['reason'].startswith('review keyword'):
                actions.append({'title': 'Log to HubSpot…', **approve})
            elif 'incomplete' in item['reason']:
                # Approval re-checks the message, and an unreadable one never passes.
                actions.append({'title': "Can't be logged automatically: log it by hand if it matters"})
            else:
                # Approval never creates contacts, so the people have to exist in HubSpot first.
                actions.append({'title': 'Log to HubSpot… (add them to HubSpot first)', **approve})
            actions.append({'title': 'Skip (never log it)', 'run': [me(), 'skip', item['id']]})
            items.append({'title': subject, 'items': actions})
        if len(pending) > SHOWN:
            items.append({'title': f'…and {len(pending) - SHOWN} more: crm-mail-sync review'})
    else:
        items.append({'title': 'Review queue empty'})

    items += [
        {'title': 'Dry-run now (writes nothing)', 'run': [me(), 'dry-run']},
        {'title': 'Sync now…', 'run': [me(), 'sync'], 'confirm': 'Log matching emails to HubSpot now?'},
        {'title': 'Open config folder', 'open': str(config.parent)},
    ]
    return {'title': 'CRM mail sync', 'badge': badge, 'items': items}


def act(action: str, args: list[str]) -> int:
    if action in {'approve', 'skip'} and len(args) == 1:
        result = tool(action, args[0], live=action == 'approve')
    elif action in {'sync', 'dry-run'} and not args:
        result = tool('run', *(['--dry-run'] if action == 'dry-run' else []), live=True)
    else:
        print(f'usage: {sys.argv[0]} [approve ID | skip ID | sync | dry-run]', file=sys.stderr)
        return 2
    lines = (result.stdout.strip() or result.stderr.strip()).splitlines()
    # run prints one line per message and its summary last; approve and skip print one line.
    print(lines[-1] if lines else f'crm-mail-sync exited {result.returncode}')
    return result.returncode


if __name__ == '__main__':
    if len(sys.argv) > 1:
        sys.exit(act(sys.argv[1], sys.argv[2:]))
    print(json.dumps(menu()))
