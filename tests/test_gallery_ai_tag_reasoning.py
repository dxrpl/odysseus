import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import GalleryImage
import routes.gallery_routes as gallery_routes


class _FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, message):
        self._message = message

    def json(self):
        return {"choices": [{"message": self._message}]}


def _setup(monkeypatch, tmp_path, message):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'gallery.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(gallery_routes, "SessionLocal", session_factory)
    monkeypatch.setattr(gallery_routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(gallery_routes, "GALLERY_IMAGE_DIR", tmp_path)
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n")

    db = session_factory()
    db.add(GalleryImage(id="img1", filename="pic.png", owner="alice", ai_tags="", is_active=True, file_size=6))
    db.commit()
    db.close()

    import src.document_processor as dp
    monkeypatch.setattr(dp, "_load_vl_settings", lambda: {"vision_enabled": True, "vision_model": "m"})
    monkeypatch.setattr(dp, "_resolve_vl_model", lambda configured, owner=None: ("http://vl.test/v1/chat/completions", "gemma-vl", {}))

    sent = []

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            sent.append(json)
            return _FakeResponse(message)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    app = FastAPI()
    app.include_router(gallery_routes.setup_gallery_routes())
    return TestClient(app), session_factory, sent


def _stored_tags(session_factory):
    db = session_factory()
    try:
        return db.query(GalleryImage).filter_by(id="img1").one().ai_tags
    finally:
        db.close()


def test_ai_tag_gives_a_thinking_model_room_to_reason(monkeypatch, tmp_path):
    client, _, sent = _setup(monkeypatch, tmp_path, {"content": "Cat, Sofa"})
    assert client.post("/api/gallery/img1/ai-tag").json() == {"ok": True, "ai_tags": "cat, sofa"}
    assert sent[0]["max_tokens"] >= 2048


def test_ai_tag_reports_an_empty_reply_instead_of_saving_nothing(monkeypatch, tmp_path):
    client, session_factory, _ = _setup(
        monkeypatch, tmp_path, {"content": "", "reasoning": "Let me look at the image..."}
    )
    body = client.post("/api/gallery/img1/ai-tag").json()
    assert "ok" not in body
    assert body["error"]
    assert _stored_tags(session_factory) == ""


def test_ai_tag_drops_think_blocks_from_the_tags(monkeypatch, tmp_path):
    client, _, _ = _setup(
        monkeypatch, tmp_path, {"content": "<think>two cats?</think>Cat, Sofa"}
    )
    assert client.post("/api/gallery/img1/ai-tag").json()["ai_tags"] == "cat, sofa"
