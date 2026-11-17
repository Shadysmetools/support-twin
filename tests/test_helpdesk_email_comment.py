"""Principal 2026-11-12: every routed ticket emails him its status, description, analysis and an "Open as a session in
Claude Code" link. The email is a comment on the Acme ticket to his own partner (908). He is also the author
(the twin posts as his user), so Odoo would skip him without mail_notify_author. The guard allows exactly that
comment and nothing wider: one partner (his), that context, a body."""
import pytest

from twin_agent.helpdesk import guard

PRINCIPAL_PARTNER = 908


def _kw(**over):
    kw = {"body": "<p>#2690 routed to Functional</p>", "subtype_xmlid": "mail.mt_comment", "message_type": "comment",
          "body_is_html": True, "partner_ids": [PRINCIPAL_PARTNER], "context": {"mail_notify_author": True}}
    kw.update(over)
    return kw


def test_comment_to_principal_only_is_allowed():
    guard.check("helpdesk", "helpdesk.ticket", "message_post", [[2690]], _kw(), notify_partner=PRINCIPAL_PARTNER)


@pytest.mark.parametrize("over", [
    {"partner_ids": [3001]},                          # the client, or anyone else
    {"partner_ids": [PRINCIPAL_PARTNER, 3001]},
    {"partner_ids": []},
    {"context": {"mail_notify_author": True, "lang": "en_US", "active_test": False}},
    {"context": {"mail_post_autofollow": True}},
    {"body": "  "},
    {"message_type": "email"},
    {"attachment_ids": [1]},
])
def test_anything_wider_is_refused(over):
    with pytest.raises(guard.WriteRefused):
        guard.check("helpdesk", "helpdesk.ticket", "message_post", [[2690]], _kw(**over),
                    notify_partner=PRINCIPAL_PARTNER)


def test_without_the_configured_partner_a_comment_is_refused():
    with pytest.raises(guard.WriteRefused):
        guard.check("helpdesk", "helpdesk.ticket", "message_post", [[2690]], _kw())


def test_internal_notes_unchanged():
    guard.check("helpdesk", "helpdesk.ticket", "message_post", [[2690]],
                {"body": "<p>x</p>", "subtype_xmlid": "mail.mt_note", "message_type": "comment", "body_is_html": True})
    with pytest.raises(guard.WriteRefused):
        guard.check("helpdesk", "helpdesk.ticket", "message_post", [[2690]],
                    {"body": "<p>x</p>", "subtype_xmlid": "mail.mt_note", "partner_ids": [PRINCIPAL_PARTNER]})
