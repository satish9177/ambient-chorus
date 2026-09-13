# SES Receipt Event Fixtures (Synthetic)

All JSON files in this directory are **synthetic fixtures** based on official AWS SES documentation:
https://docs.aws.amazon.com/ses/latest/dg/receiving-email-notifications-contents.html

**PROVENANCE DISCLAIMER:**
None of these events were captured from a live SES delivery. Every fixture is tagged with:
`"_provenance": "NOT_CAPTURED_FROM_LIVE_SES_AWS_DOCUMENTATION_SYNTHETIC"`

These fixtures test the ADR-030 decoding rules, verdict gates, action discrimination, recipient/correspondent validation, and error paths.
