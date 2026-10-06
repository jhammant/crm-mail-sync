# Security

crm-mail-sync reads a mailbox and writes to a CRM, so a bug can leak private email to the wrong record.

Please report security or privacy problems privately through GitHub's
[private vulnerability reporting](https://github.com/jhammant/crm-mail-sync/security/advisories/new)
rather than in a public issue. That includes any case where a rule lets a message through that it
should have skipped, or where a token, message body or address ends up in logs or error output.

Never paste real email content, addresses or tokens into an issue. Reproduce with `example.com`
addresses in a fixture mailbox like `fixtures/sample-mailbox.json`.
