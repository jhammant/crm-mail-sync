"""Log the right emails to HubSpot, with explicit rules and a human review queue."""
import argparse
from datetime import date
import sys

from .adapters.fake import FakeCrm
from .adapters.fixture import FixtureSource
from .adapters.gmail import GmailSource
from .adapters.hubspot import HubSpotCrm
from .adapters.review_jsonl import JsonlReviewSink
from .config import load_config
from .models import timestamp
from .pipeline import format_outcome, resolve_review, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='crm-mail-sync', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('run', 'review', 'approve', 'skip'):
        sub = commands.add_parser(command)
        sub.add_argument('--config', required=True)
        if command in {'approve', 'skip'}:
            sub.add_argument('message_id')
        if command in {'run', 'approve'}:
            sub.add_argument('--source', help='Fixture JSON path; default: Gmail')
            sub.add_argument('--crm', choices=['hubspot', 'fake'], default='hubspot')
        if command == 'run':
            sub.add_argument('--since', help='Inclusive UTC date, YYYY-MM-DD')
            sub.add_argument('--dry-run', action='store_true')
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
        else:
            outcomes = run(config, source, crm, review, since=since, dry_run=args.dry_run,
                           emit=lambda result: print(format_outcome(result)))
            counts = {action: sum(o.decision.action == action for o in outcomes) for action in ('log', 'skip', 'review')}
            print(f'{"dry-run" if args.dry_run else "run"}: {len(outcomes)} message(s); '
                  f'log={counts["log"]} skip={counts["skip"]} review={counts["review"]}')
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f'crm-mail-sync: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
