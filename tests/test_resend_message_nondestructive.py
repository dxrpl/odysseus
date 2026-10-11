"""Regression guard for SFT-safe resend behavior.

chat.js is browser-heavy, so this pins the source-level contract: the footer's
plain "Resend message" path replaces the failed user+assistant attempt instead
of appending a duplicate user turn. Append-only resend must be explicit.
"""

from pathlib import Path
from types import SimpleNamespace

from routes.history.history_routes import _keep_count_before_message, _keep_count_through_message
from tests.helpers.stylesheets import app_css


_REPO = Path(__file__).resolve().parent.parent
_CHAT_JS = _REPO / "static" / "js" / "chat.js"
_CHAT_RENDERER_JS = _REPO / "static" / "js" / "chatRenderer.js"


def _resend_body() -> str:
    src = _CHAT_JS.read_text(encoding="utf-8")
    start = src.index("export async function resendUserMessage(")
    end = src.index("export async function regenerateFrom(", start)
    return src[start:end]


def _regenerate_body() -> str:
    src = _CHAT_JS.read_text(encoding="utf-8")
    start = src.index("export async function regenerateFrom(")
    end = src.index("export async function deleteMessage(", start)
    return src[start:end]


def _edit_body() -> str:
    src = _CHAT_JS.read_text(encoding="utf-8")
    start = src.index("export async function editUserMessage(")
    end = src.index("export async function resendUserMessage(", start)
    return src[start:end]


def _fork_body() -> str:
    src = _CHAT_JS.read_text(encoding="utf-8")
    start = src.index("export async function forkFrom(")
    return src[start:start + 2000]


def test_resend_message_replaces_by_default():
    body = _resend_body()

    assert "opts = {}" in body
    assert "const appendOnly = Boolean(opts && opts.append);" in body
    assert "const replaceFromHere = !appendOnly || Boolean(opts && opts.replaceFromHere);" in body

    guard_idx = body.index("if (replaceFromHere)")
    truncate_idx = body.index("/api/session/${sessionId}/truncate")

    assert guard_idx < truncate_idx
    assert "appendOnly" in body[:guard_idx]
    assert "_hideUserBubble = true;" not in body
    assert "const beforeMsgId = userMsgElement.dataset.dbId || '';" in body
    assert "before_msg_id: beforeMsgId" in body
    assert "keep_count: keepCount" in body


def test_resend_removes_rendered_tool_threads_between_turns():
    body = _resend_body()

    assert "including agent-thread tool history that is not a `.msg` bubble" in body
    assert "let node = userMsgElement;" in body
    assert "node = node.nextElementSibling;" in body
    assert "prev.remove();" in body
    assert "querySelectorAll('.msg')" not in body[body.index("if (replaceFromHere)"):]


def test_regenerate_clears_tool_threads_after_user_bubble():
    body = _regenerate_body()

    assert "agent-thread tool history between the user and AI bubble" in body
    assert "let node = userMsgEl.nextElementSibling;" in body
    assert "node = node.nextElementSibling;" in body
    assert "prev.remove();" in body


def test_footer_resend_uses_default_replacement_behavior():
    renderer = _CHAT_RENDERER_JS.read_text(encoding="utf-8")

    assert "window.chatModule.resendUserMessage(msgElement);" in renderer
    assert "window.chatModule.resendUserMessage(userMsgEl, { replaceFromHere: true });" in renderer


def test_footer_resend_uses_round_svg_icon_not_text_glyph():
    renderer = _CHAT_RENDERER_JS.read_text(encoding="utf-8")
    style = app_css()

    assert "const RESEND_ICON =" in renderer
    assert "resend-message-icon" in renderer
    assert "icon: RESEND_ICON" in renderer
    assert "html: true" in renderer
    assert "icon: '\\u21BB'" not in renderer
    assert ".msg-action-btn .resend-message-icon" in style
    assert ".overflow-icon .resend-message-icon" in style


def test_truncate_can_resolve_keep_count_from_db_message_id():
    rows = [
        SimpleNamespace(id="first"),
        SimpleNamespace(id="clicked-user"),
        SimpleNamespace(id="assistant-after"),
    ]

    assert _keep_count_before_message(rows, "clicked-user") == 1
    assert _keep_count_before_message(rows, "missing") is None
    assert _keep_count_before_message(rows, "") is None


def test_edit_and_regenerate_truncate_by_db_message_id():
    # #6657: a long chat renders only its newest messages, so the clicked
    # bubble's DOM index is smaller than its stored position. Sending it as
    # keep_count deleted real messages above the one clicked.
    for body in (_edit_body(), _regenerate_body()):
        assert ".dataset.dbId || '';" in body
        assert "beforeMsgId ? { before_msg_id: beforeMsgId } : { keep_count: keepCount }" in body
        assert "body: JSON.stringify(truncateBody)" in body
        assert "if (!truncateRes.ok)" in body
        assert "JSON.stringify({ keep_count: keepCount })" not in body


def test_fork_from_page_sends_db_message_id():
    body = _fork_body()

    assert "const throughMsgId = aiMsgElement.dataset.dbId || '';" in body
    assert "throughMsgId ? { through_msg_id: throughMsgId } : { keep_count: keepCount }" in body
    assert "body: JSON.stringify(forkBody)" in body


def test_fork_can_resolve_keep_count_through_db_message_id():
    history = [
        SimpleNamespace(metadata={"_db_id": "first"}),
        SimpleNamespace(metadata=None),
        SimpleNamespace(metadata={"_db_id": "clicked-ai"}),
        SimpleNamespace(metadata={"_db_id": "later"}),
    ]

    assert _keep_count_through_message(history, "clicked-ai") == 3
    assert _keep_count_through_message(history, "missing") is None
    assert _keep_count_through_message(history, "") is None
