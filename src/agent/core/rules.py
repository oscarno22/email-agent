DEFAULT_RULES = """\
- Substack, beehiiv, Mailchimp, and similar sender domains are always newsletters.
- Anything from stripe.com, square.com, paypal.com, or with "receipt" / "invoice" in the
  subject is a receipt.
- Calendar invites (.ics attachments, "invitation:" in subject, calendar.google.com sender)
  are calendar.
- Emails from people I correspond with personally (no marketing domain) are personal.
- Recruiter outreach with no prior thread is junk unless the company is a top-tier name I'd
  actually consider.
- When in doubt between work and personal, prefer personal — work emails usually have a
  company domain match or a thread history.
"""
