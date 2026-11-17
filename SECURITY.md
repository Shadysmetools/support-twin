# Security policy

support-twin sits between your customers, your helpdesk and an AI model, so security reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private vulnerability reporting
(*Security -> Report a vulnerability* on the repository), or contact the maintainer via the GitHub profile.
Include the affected version or commit, the steps to reproduce, and the impact you expect.

You can expect an acknowledgement within 5 working days and a fix or mitigation plan within 30 days for
confirmed issues.

## Scope that matters most

- Any path where a client-facing message could be sent **without** a human approval (the approval gate,
  one-time nonces, the kill switch, `send_enabled`, `run_mode`).
- Prompt injection from inbound messages reaching tools, the browser or the helpdesk write path.
- Secrets leaving the local vault, or PII reaching a team channel or a log.
- The Odoo API-only rule and the browser host bans.

## Supported versions

Only the latest release on the default branch receives security fixes.
