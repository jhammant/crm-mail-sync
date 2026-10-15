"""Log the right emails to HubSpot, with explicit rules and a human review queue."""
import argparse
from datetime import date
import json
import sys

from .adapters.fake import FakeCrm
from .adapters.fixture import FixtureSource
from .adapters.gmail import GmailSource
from .adapters.hubspot import HubSpotCrm
from .adapters.review_jsonl import JsonlReviewSink
from .config import load_config
from .learned import learn, party
from .models import Message, timestamp
from .pipeline import format_outcome, recheck, resolve_review, review_groups, run
from .rules import eligible_addresses
from .state import State


def status(config, review: JsonlReviewSink) -> dict:
    """Last run, outcome counts and the pending review queue. Read-only: no lock, no writes."""
    state = State(config.state_path)
    outcomes = list(state.data['outcomes'].values())
    pending = [{'id': message_id, 'reason': row['reason'], 'subject': row['message']['subject'],
                'from': row['message']['sender'], 'timestamp': row['message']['timestamp'],
                'thread_id': row['message']['thread_id']}
               for message_id, row in review.pending().items()]
    return {'mailbox': config.mailbox_addresses[0], 'last_run': state.data.get('last_run'),
            'outcomes': {action: sum(o['action'] == action for o in outcomes) for action in ('log', 'skip', 'review')},
            'pending': sorted(pending, key=lambda item: item['timestamp'])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='crm-mail-sync', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('run', 'review', 'approve', 'skip', 'status', 'groups', 'learn', 'recheck'):
        sub = commands.add_parser(command)
        sub.add_argument('--config', required=True)
        if command in {'status', 'groups'}:
            sub.add_argument('--json', action='store_true', help='Machine-readable, for integrations')
        if command in {'approve', 'skip'}:
            sub.add_argument('message_id')
            sub.add_argument('--remember', action='store_true',
                             help='Also decide future mail with these people: their company, or the address on a personal domain')
        if command == 'learn':
            sub.add_argument('action', choices=['log', 'skip'])
            sub.add_argument('matches', nargs='+', metavar='DOMAIN_OR_ADDRESS')
            sub.add_argument('--note', default='')
        if command in {'run', 'approve', 'recheck'}:
            sub.add_argument('--source', help='Fixture JSON path; default: Gmail')
            sub.add_argument('--crm', choices=['hubspot', 'fake'], default='hubspot')
        if command in {'run', 'recheck'}:
            sub.add_argument('--dry-run', action='store_true')
        if command == 'run':
            sub.add_argument('--since', help='Inclusive UTC date, YYYY-MM-DD')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        review = JsonlReviewSink(config.review_path)
        if args.command == 'review':
            pending = review.pending()
            for message_id, row in pending.items():
                print(' '.join(f"{message_id}: {row['reason']} | {row['message']['subject']}".split()))
            print(f'{len(pending)} pending review item(s)')
            return 0
        if args.command == 'status':
            report = status(config, review)
            if args.json:
                print(json.dumps(report))
            else:
                counts = report['outcomes']
                print(f"last run: {report['last_run'] or 'never'}; logged {counts['log']}, skipped {counts['skip']}, "
                      f"{len(report['pending'])} pending review")
            return 0
        if args.command == 'groups':
            groups = review_groups(config, review)
            if args.json:
                print(json.dumps(groups))
            else:
                for group in groups:
                    reasons = ', '.join(f'{n} {r}' for r, n in group['reasons'].items())
                    print(f"{group['key']}: {len(group['ids'])} email(s) | {reasons}")
                print(f'{len(groups)} group(s), {len(review.pending())} pending review item(s)')
            return 0
        if args.command == 'learn':
            changed = learn(config.learned_rules_path, args.action, args.matches, config.personal_email_domains,
                            note=args.note)
            print(f"learned {args.action}: {', '.join(changed) or 'nothing new'}")
            return 0
        if getattr(args, 'remember', False):
            row = review.pending().get(args.message_id)
            if row is None:
                raise ValueError(f'No pending review item: {args.message_id}')
            message = Message.from_dict(row['message'])
            parties = sorted({party(a, config.personal_email_domains) for a in eligible_addresses(message, config)})
            if not parties:
                raise ValueError('Nobody on this email can be remembered')
            learn(config.learned_rules_path, 'log' if args.command == 'approve' else 'skip', parties,
                  config.personal_email_domains, note=f'review decision on {message.subject[:60]}')
            print(f"remembered {'log' if args.command == 'approve' else 'skip'}: {', '.join(parties)}")
        if args.command == 'skip':
            resolve_review(config, None, None, review, args.message_id, approve=False)
            print(f'{args.message_id}: skipped')
            return 0
        if args.crm == 'fake' and not args.source:
            raise ValueError('--crm fake requires --source with a fixture mailbox')
        since = timestamp(date.fromisoformat(args.since).isoformat() + 'T00:00:00Z') if getattr(args, 'since', None) else None
        source = FixtureSource(args.source) if args.source else GmailSource(config.gmail_token_path)
        crm = FakeCrm(source.data.get('crm', {})) if args.crm == 'fake' else HubSpotCrm(
            config.hubspot_token_env, config.hubspot_owner_id)
        if args.command == 'approve':
            outcome = resolve_review(config, source, crm, review, args.message_id, approve=True)
            print(format_outcome(outcome) if outcome else f'{args.message_id}: already resolved')
        elif args.command == 'recheck':
            outcomes = recheck(config, source, crm, review, dry_run=args.dry_run,
                               emit=lambda result: print(format_outcome(result)))
            counts = {action: sum(o.decision.action == action for o in outcomes) for action in ('log', 'skip', 'review')}
            print(f'{"recheck dry-run" if args.dry_run else "recheck"}: {len(outcomes)} pending; '
                  f'log={counts["log"]} skip={counts["skip"]} still review={counts["review"]}')
        else:
            outcomes = run(config, source, crm, review, since=since, dry_run=args.dry_run,
                           emit=lambda result: print(format_outcome(result)))
            counts = {action: sum(o.decision.action == action for o in outcomes) for action in ('log', 'skip', 'review')}
            # Queued emails are decided as soon as a rule or a new CRM contact can decide them.
            queued = recheck(config, source, crm, review, dry_run=args.dry_run, exclude=frozenset(o.message.id for o in outcomes),
                             emit=lambda result: result.decision.action != 'review' and print('queue ' + format_outcome(result)))
            decided = sum(o.decision.action != 'review' for o in queued)
            print(f'{"dry-run" if args.dry_run else "run"}: {len(outcomes)} message(s); '
                  f'log={counts["log"]} skip={counts["skip"]} review={counts["review"]}; '
                  f'queue: {decided} decided, {len(queued) - decided} waiting')
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f'crm-mail-sync: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
