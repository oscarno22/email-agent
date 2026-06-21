DEFAULT_RULES = """\
- Substack, beehiiv, Mailchimp, and similar sender domains are always newsletters.
- Anything from stripe.com, square.com, paypal.com, or with "receipt" / "invoice" in the
  subject is a receipt.
- Calendar invites (.ics attachments, "invitation:" in subject, calendar.google.com sender)
  are calendar.
- Emails from people I correspond with personally (no marketing domain) are personal.
- Statements, balance/transaction alerts, and account notices from banks or card issuers
  (e.g. chase.com, bankofamerica.com, wellsfargo.com, capitalone.com, amex.com,
  citi.com, discover.com) are banking. Banking is for account activity — a purchase
  receipt from a store is still a receipt, not banking.
- I am actively job hunting (software engineer). Recruiter/role outreach, application
  confirmations and status updates, and interview scheduling are application — including
  mail from applicant-tracking systems (greenhouse.io, lever.co, ashbyhq.com,
  myworkday.com, smartrecruiters.com, workable.com). Only obviously spammy mass-blast
  recruiter mail with no specific role is junk.
- Technical-assessment invitations and reminders (HackerRank, Codility, CodeSignal,
  CoderPad, Karat, or a take-home coding challenge) are assessment.
- When in doubt between work and personal, prefer personal — work emails usually have a
  company domain match or a thread history. Job-hunt mail (application/assessment) takes
  priority over the generic work category.
"""
