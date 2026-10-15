"""In-memory CRM with the same duplicate matching as the live adapter."""
from ..models import Associations, Contact, Deal, Message, timestamp


class FakeCrm:
    def __init__(self, data: dict | None = None):
        data = data or {}
        self.contacts = {c['email'].lower(): Contact(**c) for c in data.get('contacts', [])}
        self.contact_deals = {cid: [Deal(id=d['id'], stage=d['stage'], modified=timestamp(d['modified']),
                                       closed=d.get('closed', False)) for d in rows]
                              for cid, rows in data.get('deals', {}).items()}
        self.companies = data.get('companies', [])
        self.company_domains = data.get('company_domains', {})
        self.created_contacts: list[Contact] = []
        self.emails = list(data.get('emails', []))
        self.created: list[dict] = []

    def contact(self, email: str) -> Contact | None:
        return self.contacts.get(email.lower())

    def deals(self, contact_id: str) -> list[Deal]:
        return self.contact_deals.get(contact_id, [])

    def existing_email(self, message: Message) -> bool:
        return any(e['subject'] == message.subject and
                   abs((timestamp(e['timestamp']) - message.timestamp).total_seconds()) <= 120
                   for e in self.emails)

    def create_email(self, message: Message, associations: Associations, direction: str) -> str:
        record = {'id': str(len(self.emails) + 1), 'subject': message.subject,
                  'timestamp': message.timestamp.isoformat(), 'message': message,
                  'associations': associations, 'direction': direction}
        self.emails.append(record)
        self.created.append(record)
        return record['id']

    def create_contact(self, email: str, name: str = '', company_domain: str | None = None) -> Contact:
        email = email.lower()
        if email in self.contacts:
            return self.contacts[email]
        contact = Contact(f'new-{len(self.created_contacts) + 1}', email,
                          self.company_domains.get(company_domain) if company_domain else None)
        self.contacts[email] = contact
        self.created_contacts.append(contact)
        return contact
