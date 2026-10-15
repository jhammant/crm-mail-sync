"""Classify, associate and persist, with a read-only dry-run path."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from collections.abc import Callable

from .adapters.interfaces import Crm, MailSource, ReviewSink
from .config import Config
from .learned import LearnedRules, load_learned, party
from .models import Associations, Decision, Message, Outcome, timestamp
from .rules import classify, eligible_addresses, hard_skip, recent_open_deals
from .state import State, run_lock


def learned_rules(config: Config) -> LearnedRules:
    return load_learned(config.learned_rules_path) if config.learned_rules_path else LearnedRules()


def evaluate(message: Message, config: Config, crm: Crm, *, approved: bool = False,
             learned: LearnedRules = LearnedRules()) -> Outcome:
    direction = 'sent' if message.sender.email in config.mailbox_addresses or 'SENT' in message.labels else 'received'
    contacts = {} if hard_skip(message, config) else {a: crm.contact(a) for a in eligible_addresses(message, config)}
    decision = classify(message, config, contacts, approved=approved, learned=learned)
    associations = Associations()
    if decision.action == 'log':
        if crm.existing_email(message):
            decision = Decision('skip', 'already in CRM (subject within 2 minutes)')
        else:
            companies = tuple(sorted({c.company_id for c in decision.contacts if c.company_id}))
            deals = tuple(dict.fromkeys(d.id for c in decision.contacts for d in recent_open_deals(crm.deals(c.id))))
            associations = Associations(decision.contacts, companies, deals)
    return Outcome(message, decision, direction, associations)


def apply(outcome: Outcome, state: State, crm: Crm, review: ReviewSink, config: Config) -> None:
    message, decision = outcome.message, outcome.decision
    crm_id = None
    if decision.action == 'log':
        associations = outcome.associations
        if decision.new_contacts:
            names = {p.email: p.name for p in message.participants}
            created = tuple(crm.create_contact(a, names.get(a, ''), None if party(a, config.personal_email_domains) == a
                                               else a.rpartition('@')[2]) for a in decision.new_contacts)
            contacts = tuple({c.id: c for c in (*associations.contacts, *created)}.values())
            companies = tuple(sorted({c.company_id for c in contacts if c.company_id}))
            associations = Associations(contacts, companies, associations.deals)
        crm_id = crm.create_email(message, associations, outcome.direction)
    elif decision.action == 'review':
        review.enqueue(message, decision.reason)
    state.record(message.id, decision.action, decision.reason, crm_id)


def run(config: Config, source: MailSource, crm: Crm, review: ReviewSink, *,
        since: datetime | None = None, dry_run: bool = False,
        emit: Callable[[Outcome], None] | None = None) -> list[Outcome]:
    with nullcontext() if dry_run else run_lock(config.state_path):
        state = State(config.state_path)
        learned = learned_rules(config)
        started = datetime.now(timezone.utc)
        # Overlap catches messages indexed late; IDs make repeated reads harmless.
        start = since or (timestamp(state.data['last_run']) - timedelta(days=1)
                          if state.data.get('last_run') else timestamp(config.initial_since + 'T00:00:00Z'))
        outcomes = []
        for message in source.messages(start):
            if state.recorded(message.id):
                continue
            outcome = evaluate(message, config, crm, learned=learned)
            if not dry_run:
                apply(outcome, state, crm, review, config)
            outcomes.append(outcome)
            if emit:
                emit(outcome)
        if not dry_run:
            state.checkpoint(started.isoformat())
        return outcomes


def resolve_review(config: Config, source: MailSource | None, crm: Crm | None,
                   review: ReviewSink, message_id: str, *, approve: bool) -> Outcome | None:
    with run_lock(config.state_path):
        pending = review.pending()
        if message_id not in pending:
            raise ValueError(f'No pending review item: {message_id}')
        state = State(config.state_path)
        previous = state.data['outcomes'].get(message_id, {})
        # Recover a crash between the durable outcome and its queue resolution.
        if previous.get('action') in {'log', 'skip'}:
            review.resolve(message_id, 'approved' if previous['action'] == 'log' else 'skipped')
            return None
        if not approve:
            state.record(message_id, 'skip', 'manually skipped')
            review.resolve(message_id, 'skipped')
            return None
        if source is None or crm is None:
            raise ValueError('Approval requires a mail source and CRM')
        # Fetch again so newly applied private labels are never overridden by approval.
        message = source.get_message(message_id)
        outcome = evaluate(message, config, crm, approved=True, learned=learned_rules(config))
        if outcome.decision.action == 'review':
            raise ValueError('Cannot approve safely: ' + outcome.decision.reason)
        apply(outcome, state, crm, review, config)
        review.resolve(message_id, 'approved' if outcome.decision.action == 'log' else 'skipped')
        return outcome


def recheck(config: Config, source: MailSource, crm: Crm, review: ReviewSink, *,
            dry_run: bool = False, emit: Callable[[Outcome], None] | None = None,
            exclude: frozenset[str] = frozenset()) -> list[Outcome]:
    """Test every pending review item against the current rules and CRM; resolve the ones they now decide."""
    with nullcontext() if dry_run else run_lock(config.state_path):
        state = State(config.state_path)
        learned = learned_rules(config)
        outcomes = []
        for message_id in [m for m in review.pending() if m not in exclude]:
            # Fetch again so newly applied private labels always win.
            outcome = evaluate(source.get_message(message_id), config, crm, learned=learned)
            if outcome.decision.action != 'review' and not dry_run:
                apply(outcome, state, crm, review, config)
                review.resolve(message_id, 'approved' if outcome.decision.action == 'log' else 'skipped')
            outcomes.append(outcome)
            if emit:
                emit(outcome)
        return outcomes


def review_groups(config: Config, review: ReviewSink) -> list[dict]:
    """Pending review items grouped by who they're with: one decision per company, or per person on a personal domain."""
    groups: dict[str, dict] = {}
    for message_id, row in review.pending().items():
        message = Message.from_dict(row['message'])
        names = {p.email: p.name for p in message.participants}
        for address in eligible_addresses(message, config) or ('(nobody eligible)',):
            key = party(address, config.personal_email_domains) if '@' in address else address
            group = groups.setdefault(key, {'key': key, 'personal': '@' in key, 'people': {}, 'ids': [],
                                            'subjects': [], 'reasons': {}})
            if '@' in address:
                group['people'][address] = names.get(address, '')
            if message_id not in group['ids']:
                group['ids'].append(message_id)
                group['subjects'].append(message.subject or '(no subject)')
                group['reasons'][row['reason']] = group['reasons'].get(row['reason'], 0) + 1
    return sorted(groups.values(), key=lambda g: (-len(g['ids']), g['key']))


def format_outcome(outcome: Outcome) -> str:
    clean = lambda text: ' '.join(text.split())
    line = (f'{outcome.message.timestamp.isoformat()} {outcome.direction} {outcome.decision.action} '
            f'{clean(outcome.message.id)}: {clean(outcome.decision.reason)}')
    if outcome.decision.action == 'log':
        a = outcome.associations
        line += (f' contacts={",".join(c.email for c in a.contacts) or "-"}'
                 f' companies={",".join(a.companies) or "-"} deals={",".join(a.deals) or "-"}')
        if outcome.decision.new_contacts:
            line += f' new={",".join(outcome.decision.new_contacts)}'
    return line
