#!/usr/bin/env python3
"""Roost plugin for crm-mail-sync: the review queue in the menu bar.

With no arguments it prints Roost's menu JSON: the queue grouped by company (or by
person on a personal address), so one click decides every email with them and
crm-mail-sync remembers it for future mail. Inside a company, each person can be flagged
on their own, and `Flag someone to sync…` takes any address or domain. Roost calls it
back with `log-group KEY`, `skip-group KEY`, `log-person ADDRESS`, `skip-person ADDRESS`,
`flag [ADDRESS_OR_DOMAIN]`, `approve ID`, `skip ID`, `sync` or `dry-run` when you click,
and shows the first line it prints as a notification.

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

SHOWN = 15          # review groups listed before "and N more"
EMAILS_SHOWN = 10   # emails listed inside one group
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
    if live and os.environ.get('CRM_MAIL_SYNC_SOURCE'):  # demo mode
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
        groups = tool('groups', '--json')
        groups = json.loads(groups.stdout) if groups.returncode == 0 else []
        by_id = {item['id']: item for item in pending}
        items.append({'title': f'{len(pending)} email{"s" if len(pending) != 1 else ""} waiting for review'
                               f' from {len(groups)} {"company or person" if len(groups) == 1 else "companies and people"}'})
        demo = bool(os.environ.get('CRM_MAIL_SYNC_SOURCE'))
        for group in groups[:SHOWN]:
            count = len(group['ids'])
            label = f"{group['key']} · {count} email{'s' if count != 1 else ''}"
            if group['personal'] or len(group['people']) < 2:
                actions = [{'title': short(', '.join(f'{n} <{e}>' if n else e for e, n in group['people'].items()), 90)}] \
                    if group['people'] else []
            else:  # a company: each person can be flagged on their own, whatever the rest of it gets
                actions = [{'title': f'{len(group["people"])} people', 'items': [person_node(e, n) for e, n in group['people'].items()]}]
            actions.append({'title': 'Why: ' + short('; '.join(f'{n} {r}' for r, n in group['reasons'].items()), 90)})
            if group['people']:
                who = 'this person' if group['personal'] else group['key']
                actions.append({'title': 'Log all and remember…', 'run': [me(), 'log-group', group['key']],
                                'confirm': f'Log these {count} emails to HubSpot, adding anyone who isn\'t there yet, '
                                           f'and log future mail with {who} automatically?'})
                actions.append({'title': 'Skip all and remember', 'run': [me(), 'skip-group', group['key']]})
            emails = []
            for message_id in group['ids'][:EMAILS_SHOWN]:
                item = by_id.get(message_id)
                if not item:
                    continue
                subject = short(item['subject'], 60)
                one = [{'title': f"Why: {item['reason']}"}]
                if not demo:
                    one.append({'title': 'Open in Gmail',
                                'open': f"https://mail.google.com/mail/?authuser={report['mailbox']}#all/{item['thread_id']}"})
                if 'incomplete' in item['reason']:
                    # Approval re-checks the message, and an unreadable one never passes.
                    one.append({'title': "Can't be logged automatically: log it by hand if it matters"})
                else:
                    one.append({'title': 'Log just this one…', 'run': [me(), 'approve', message_id],
                                'confirm': f'Log "{subject}" to HubSpot, adding anyone who isn\'t there yet?'})
                one.append({'title': 'Skip just this one', 'run': [me(), 'skip', message_id]})
                emails.append({'title': subject, 'items': one})
            if count > EMAILS_SHOWN:
                emails.append({'title': f'…and {count - EMAILS_SHOWN} more'})
            actions.append({'title': 'One by one', 'items': emails})
            items.append({'title': label, 'items': actions})
        if len(groups) > SHOWN:
            items.append({'title': f'…and {len(groups) - SHOWN} more: crm-mail-sync groups'})
    else:
        items.append({'title': 'Review queue empty'})

    items += [
        {'title': 'Flag someone to sync…', 'run': [me(), 'flag'],
         'tooltip': 'An address or a company domain: their email is logged to HubSpot from now on, '
                    'even if they have never been in the review queue'},
        {'title': 'Dry-run now (writes nothing)', 'run': [me(), 'dry-run']},
        {'title': 'Sync now…', 'run': [me(), 'sync'], 'confirm': 'Log matching emails to HubSpot now?'},
        {'title': 'Open config folder', 'open': str(config.parent)},
    ]
    return {'title': 'CRM mail sync', 'badge': badge, 'items': items}


def person_node(email: str, name: str) -> dict:
    who = f'{name} <{email}>' if name else email
    return {'title': short(who, 70), 'items': [
        {'title': 'Sync this person…', 'run': [me(), 'log-person', email],
         'confirm': f'Log email with {who} to HubSpot from now on, adding them if they aren\'t there yet?'},
        {'title': 'Skip this person from now on', 'run': [me(), 'skip-person', email],
         'tooltip': "Stops asking about them. If they're already in HubSpot their email still logs (never_log in the config stops that)"},
    ]}


def ask_who() -> str | None:
    """A dialog for the address or domain to flag, since Roost's menu has no text field. None when cancelled."""
    script = ('text returned of (display dialog "Flag someone to sync to HubSpot: their email address, '
              'or a company domain for everyone there" default answer "" with title "CRM mail sync")')
    answer = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
    return answer.stdout.strip() or None if answer.returncode == 0 else None


def act(action: str, args: list[str]) -> int:
    if action == 'flag' and len(args) <= 1:
        who = args[0] if args else ask_who()
        if not who:
            print('Nothing flagged')
            return 0
        action, args = 'log-person', [who]
    if action in {'log-group', 'skip-group', 'log-person', 'skip-person'} and len(args) == 1:
        learned = tool('learn', action.partition('-')[0], args[0])
        if learned.returncode != 0:
            print((learned.stderr.strip() or f'crm-mail-sync exited {learned.returncode}').splitlines()[0])
            return learned.returncode
        result = tool('recheck', live=True)
    elif action in {'approve', 'skip'} and len(args) == 1:
        result = tool(action, args[0], live=action == 'approve')
    elif action in {'sync', 'dry-run'} and not args:
        result = tool('run', *(['--dry-run'] if action == 'dry-run' else []), live=True)
    else:
        print(f'usage: {sys.argv[0]} [log-group KEY | skip-group KEY | log-person ADDRESS | skip-person ADDRESS | '
              f'flag [ADDRESS_OR_DOMAIN] | approve ID | skip ID | sync | dry-run]',
              file=sys.stderr)
        return 2
    lines = (result.stdout.strip() or result.stderr.strip()).splitlines()
    # run prints one line per message and its summary last; approve and skip print one line.
    print(lines[-1] if lines else f'crm-mail-sync exited {result.returncode}')
    return result.returncode


if __name__ == '__main__':
    if len(sys.argv) > 1:
        sys.exit(act(sys.argv[1], sys.argv[2:]))
    print(json.dumps(menu()))
