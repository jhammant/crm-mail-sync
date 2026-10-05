# crm-mail-sync

Log selected business emails to HubSpot using explicit rules and a human review queue. Python 3.11+, MIT licensed, no LLM, no server. Runs on macOS or Linux.

HubSpot's automatic logging and Never Log list cannot express "log this introduction to the partner, but never to the investor". Keep sensitive addresses or domains on HubSpot's own Never Log list and let this tool create selected email engagements. It associates eligible existing contacts, their primary companies and up to five recent open deals per contact.

## Quick start and offline acceptance

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
source .venv/bin/activate
python3 -m pytest -q
python3 -m crm_mail_sync.cli run --config examples/config.example.yaml --source fixtures/sample-mailbox.json --crm fake --dry-run
```

Installation downloads dependencies; both acceptance commands subsequently run offline. The checked-in root symlink supports the requested `python3 -m` invocation without installation. The installed console command is `crm-mail-sync`. Tests refuse network connections and keep their temporary files inside this directory.

The sample contains 13 messages: one log, eight skips and four reviews. Fake CRM data lives alongside messages in the fixture. A fake CRM starts fresh on each invocation; local state and queue files persist on non-dry runs. Use a separate config and state directory for experiments.

## Setup for a real mailbox

Copy `examples/config.example.yaml` to a private directory outside your checkout. Keep real addresses, credentials, state and review files there. Set an explicit initial date; the first run starts at midnight UTC on that date.

### Gmail desktop OAuth

Enable the Gmail API in a Google Cloud project, configure the consent screen, add your account as a test user if needed, and create an OAuth client of type **Desktop app**. Download its client JSON to your private configuration directory. Authorise only `https://www.googleapis.com/auth/gmail.readonly`. Google documents the [desktop Python authorisation flow](https://developers.google.com/workspace/gmail/api/quickstart/python) and [full thread retrieval](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.threads/get).

Run this once with the installed dependencies, substituting your private paths:

```python
import os
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

private_dir = Path.home() / '.config' / 'crm-mail-sync'
private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
flow = InstalledAppFlow.from_client_secrets_file(
    str(private_dir / 'client-credentials.json'),
    ['https://www.googleapis.com/auth/gmail.readonly'],
)
credentials = flow.run_local_server(port=0, access_type='offline', prompt='consent')
fd = os.open(private_dir / 'gmail-token.json', os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write(credentials.to_json())
```

Set `gmail_token_path` to the resulting file. The run command reads existing credentials and refreshes access tokens in memory. It does not open an interactive OAuth flow or rewrite the token file. Expired/revoked refresh credentials require repeating authorisation. Configure the consent app appropriately for unattended use; testing-mode credentials can expire.

### HubSpot

Create a private-app/service-key token with these scopes: `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.companies.read`, `crm.objects.deals.read`, `sales-email-read`. Put the token in the environment variable named by `hubspot_token_env`; never in YAML or command arguments. The scheduler must supply that environment too.

Email creation uses REST v3 and explicit contact/company/deal associations. Contact association type 198, company 186 and deal 210 are listed in the [official association table](https://developers.hubspot.com/docs/api-reference/latest/crm/associations/associate-records/guide#email-to-object). Before first real use, confirm the email-to-deal type against `GET /crm/v4/associations/emails/deals/labels` in your account. The code keeps 210 in one constant with that reminder. Primary company selection uses the labelled primary association, not an arbitrary associated company.

```bash
crm-mail-sync run --config /private/config.yaml --since 2026-10-01 --dry-run
crm-mail-sync run --config /private/config.yaml
crm-mail-sync review --config /private/config.yaml
crm-mail-sync approve MESSAGE_ID --config /private/config.yaml
crm-mail-sync skip MESSAGE_ID --config /private/config.yaml
```

A live dry-run makes read/search API requests, prints decisions and associations, and writes no CRM records, state, queue, lock or token files. It still requires credentials. Each output row includes the UTC timestamp, sent/received direction, action and reason; log rows include eligible contact emails, company IDs and deal IDs.

## Configuration reference

All keys are top-level YAML. Unknown keys and malformed values fail before processing. Relative paths resolve against the config file's directory.

| Key | Meaning |
| --- | --- |
| `mailbox_addresses` | Non-empty list of your mailbox addresses and sending aliases. Used for internal membership and sent direction. |
| `internal_domains` | Non-empty list of company domains. Subdomains match too. |
| `never_log` | List of exact addresses or domains, including subdomains, excluded from associations. Can be empty. |
| `private_label` | Exact Gmail label name. Any occurrence on the message or another message in its thread is a hard skip. |
| `review_keywords` | Case-insensitive subject/body words or phrases requiring review. Word boundaries avoid matching `board` inside `onboarding`. Can be empty. |
| `state_path` | JSON outcome ledger and successful-run cursor. A neighbouring `.lock` file serialises writers. |
| `review_path` | Append-only JSONL queue, including message snapshots and resolution events. |
| `gmail_token_path` | Authorised-user OAuth token JSON, distinct from both state paths. |
| `hubspot_token_env` | Token environment variable name. Default `HUBSPOT_TOKEN`. |
| `hubspot_owner_id` | Optional numeric owner ID; `null` omits it. |
| `initial_since` | Required initial UTC date in `YYYY-MM-DD` form. |

## Rules and privacy

Hard skips win: private labels, drafts/spam/trash, calendar subject prefixes or calendar MIME parts, and bulk senders. Malformed/incomplete messages go to review. Internal-only messages and threads containing only internal/never-log participants are skipped. Review keywords then take priority over eligible contact matches. An unknown external participant with a never-log participant goes to review; other unknown external mail also goes to review because the tool cannot safely choose a CRM record.

Otherwise, eligible existing CRM contacts cause one email engagement. Never-log participants do not prevent logging to another eligible participant. Open deals exclude `closedwon`, `closedlost` and custom stages whose `hs_is_closed` property is true, then use the five most recently modified deals per contact. Shared associations are deduplicated.

**Never Log controls association, not redaction.** The full logged message can still include an investor's name, address or quoted text. A private label is the hard exclusion mechanism. Keyword matching is deterministic and cannot understand intent or guarantee confidentiality. Attachments are not uploaded or classified; calendar parts are detected recursively. Review approval bypasses keyword review only: it re-fetches the live message, re-evaluates privacy exclusions and CRM contacts, and refuses unresolved ambiguity. Unknown people must become eligible CRM contacts before approval can log their message. Review approval never creates contacts.

State files contain IDs and reasons; review files contain full email snapshots. Newly created files are mode `0600` and new parent directories `0700`. Protect existing directories, backups and scheduler logs too. The queue is append-only: skipping/approving does not erase its original snapshot. Set a retention policy outside this tool. Changing a rule does not replay previously recorded messages or remove emails already in HubSpot.

## Scheduling

Run twice daily with cron, using absolute executable/config paths. Set `HUBSPOT_TOKEN` in the scheduler's securely supplied environment; cron does not inherit your interactive shell environment.

```cron
0 8,17 * * * /path/to/checkout/.venv/bin/crm-mail-sync run --config /private/config.yaml >> /private/crm-mail-sync.log 2>&1
```

The schedule follows the machine's local timezone. Exit status is non-zero on configuration, API or persistence failures. A per-state-file process lock prevents overlapping writes; use one state/queue pair per mailbox and CRM account. Read-only commands do not acquire a lock. Monitor failures and review the queue regularly.

## Design notes

- Pure rules operate on normalised dataclasses and supplied CRM lookups. `MailSource`, `Crm` and `ReviewSink` are structural interfaces; implement their methods to add another source, CRM or queue. Adapters and HTTP transports are independently testable without network access.
- Successful runs checkpoint their start time. Subsequent reads overlap by 24 hours to catch ordinary delayed indexing; recorded message IDs suppress repeated work. `--since` overrides the fetch window, not the outcome ledger. Imports delayed by more than one day need an explicit earlier `--since`.
- Each completed message is saved atomically; a failure never advances the run cursor. The queue is enqueued before the review outcome is saved, and repeated enqueue calls do not duplicate pending items. Review resolution can recover after the outcome is saved but before the terminal queue event is appended.
- Before creation, HubSpot is searched for the exact subject within an inclusive two-minute window either side of the timestamp. This required heuristic can suppress unrelated same-subject emails. HubSpot search indexing and remote writes are not transactional: a crash or timeout after remote creation but before local save can require manual reconciliation before retrying. The adapter intentionally does not blindly retry writes or HTTP 429 responses.
- Sent direction uses configured mailbox aliases or Gmail's `SENT` label. External contacts are matched by exact email search. Names are retained in normalisation and split into first name/remainder for HubSpot headers. Bodies are not silently truncated.
- Gmail resolves labels across the whole current thread, even when the labelled message falls outside the fetch window. MIME traversal is recursive and text bodies stored as Gmail attachment data are fetched read-only. Unreadable content goes to review.
- The fake CRM is intentionally in-memory. `approve` accepts `--source` and `--crm fake` for offline lifecycle testing; production approval defaults to Gmail and HubSpot. An isolated fake run is not proof of live permissions or account behaviour.
- Dependencies are pinned in `pyproject.toml` and the complete development environment in `requirements.lock`. Regenerate with `uv pip compile pyproject.toml --extra dev --output-file requirements.lock`. The implementation is synchronous and uses the standard library for HubSpot HTTP; no framework or background service is needed.
- Automatic learning from approvals, richer personal/social rules and queue integrations are future work. No rule is taught by approving a message in v1.
- This checkout retains private fork provenance in `FORK.md` and historical reference material in `reference/`. Those are not shipped in the Python package. Before public release, remove the reference and private provenance and review Git history; the automated privacy test covers `src/`, `examples/`, `fixtures/` and this README, not historical commits.

## Verification

Tests cover every acceptance case plus MIME parsing, Gmail pagination/thread labels, HubSpot request payloads and pagination, closed custom deal stages, failed runs, locks, queue recovery and CLI lifecycle. They use injected services/transports and local fixtures. Real credentials and live account writes are deliberately outside offline acceptance.
