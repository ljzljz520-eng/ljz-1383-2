import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp(prefix="studio-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["STORAGE_DIR"] = f"{_tmp}/files"
os.environ["ADMIN_PASSWORD"] = "testpass"
os.environ["SECRET_KEY"] = "test-secret"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin(client):
    r = client.post("/api/auth/login",
                    json={"username": "admin", "password": "testpass"})
    assert r.status_code == 200
    return client


@pytest.fixture()
def make(admin):
    """Factory that builds artist + client + work with versions via the API."""
    def _make(title="星夜系列", confidential=False, featured=False, versions=("v1 草图", "v2 成稿")):
        a = admin.post("/api/admin/artists", json={"name": "小林", "bio": "青年插画师"}).json()
        c = admin.post("/api/admin/clients", json={"name": "某出版社", "email": "e@x.com"}).json()
        w = admin.post("/api/admin/works", json={
            "artist_id": a["id"], "title": title, "summary": "数字插画",
            "confidential": confidential, "featured": featured,
        }).json()
        vids = []
        for label in versions:
            r = admin.post(f"/api/admin/works/{w['id']}/versions", json={"label": label})
            assert r.status_code == 200
            vids.append(r.json()["versions"][-1]["id"])
        return {"artist": a, "client": c, "work": w, "version_ids": vids}
    return _make


def grant(admin, asset_id, right, granted=True, scope="public"):
    r = admin.post("/api/admin/grants", json={
        "asset_id": asset_id, "right": right, "granted": granted, "scope": scope})
    assert r.status_code == 200, r.text
    return r.json()
