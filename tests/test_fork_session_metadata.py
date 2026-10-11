"""Forking a session must not mutate the source session's messages.

ChatMessage.metadata is a dict. add_message() -> _persist_message() stamps
_db_id (and timestamp) onto that dict in place. The fork handler used to pass
the source message's metadata dict by reference into the new session, so
persisting the fork rewrote the SOURCE messages' _db_id — breaking
edit/delete-by-id on the original conversation. The fork must copy the dict.
"""
import asyncio
from types import SimpleNamespace

from core.models import ChatMessage
import routes.history_routes as mod


class _FakeSession:
    def __init__(self, name="", owner=None):
        self.name = name
        self.owner = owner
        self.endpoint_url = ""
        self.model = ""
        self.history = []

    def add_message(self, message):
        # Mirror _persist_message: stamp the in-memory message's metadata.
        if message.metadata is None:
            message.metadata = {}
        message.metadata["_db_id"] = f"new-{len(self.history)}"
        self.history.append(message)


class _FakeSessionManager:
    def __init__(self, source):
        self.sessions = {"src-id": source}
        self.created = None

    def get_session(self, session_id):
        # Fork looks the source up through get_session — the hydration seam —
        # so a session only present in the DB still forks a real transcript.
        return self.sessions[session_id]

    def create_session(self, session_id=None, name=None, endpoint_url=None,
                       model=None, rag=False, owner=None, endpoint_id=None):
        self.created = _FakeSession(name=name, owner=owner)
        self.created.endpoint_id = endpoint_id
        return self.created

    def save_sessions(self):
        pass


def _fork_handler(router):
    for route in router.routes:
        if "/fork" in getattr(route, "path", "") and "POST" in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError("fork route not found")


def test_fork_does_not_corrupt_source_message_metadata(monkeypatch):
    monkeypatch.setattr(mod, "_verify_session_owner", lambda *a, **k: None)

    source = _FakeSession(name="Original", owner="alice")
    source.endpoint_id = "chatgpt-account-b"
    source.history = [
        ChatMessage("user", "hi", {"_db_id": "src-0"}),
        ChatMessage("assistant", "yo", {"_db_id": "src-1"}),
    ]
    sm = _FakeSessionManager(source)

    req = SimpleNamespace()

    async def _json():
        return {"keep_count": 2}

    req.json = _json

    router = mod.setup_history_routes(sm)
    fork = _fork_handler(router)
    result = asyncio.run(fork(request=req, session_id="src-id"))

    assert result["status"] == "ok"
    assert result["kept"] == 2
    assert sm.created.endpoint_id == "chatgpt-account-b"

    # The forked session got its own metadata dicts...
    new_session = sm.created
    assert new_session.history[0].metadata is not source.history[0].metadata
    assert new_session.history[1].metadata is not source.history[1].metadata

    # ...and the source session's _db_id values are untouched.
    assert source.history[0].metadata["_db_id"] == "src-0"
    assert source.history[1].metadata["_db_id"] == "src-1"


def _fork_with_body(monkeypatch, body):
    monkeypatch.setattr(mod, "_verify_session_owner", lambda *a, **k: None)

    source = _FakeSession(name="Long chat", owner="alice")
    source.history = [
        ChatMessage("user" if i % 2 == 0 else "assistant", f"m{i}", {"_db_id": f"src-{i}"})
        for i in range(6)
    ]
    sm = _FakeSessionManager(source)

    async def _json():
        return body

    req = SimpleNamespace(json=_json)
    fork = _fork_handler(mod.setup_history_routes(sm))
    return sm, asyncio.run(fork(request=req, session_id="src-id"))


def test_fork_through_message_id_copies_up_to_and_including_it(monkeypatch):
    # #6657: the page renders only the newest messages of a long chat, so the
    # clicked bubble's DOM index (here it would be 1) is not its stored
    # position. The DB id resolves the real position.
    sm, result = _fork_with_body(monkeypatch, {"through_msg_id": "src-3", "keep_count": 2})

    assert result["kept"] == 4
    assert [m.content for m in sm.created.history] == ["m0", "m1", "m2", "m3"]


def test_fork_through_unknown_message_id_is_404(monkeypatch):
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _fork_with_body(monkeypatch, {"through_msg_id": "missing"})

    assert exc.value.status_code == 404
