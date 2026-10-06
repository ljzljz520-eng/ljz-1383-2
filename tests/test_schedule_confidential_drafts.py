"""Schedule hold expiry/booking conflicts; confidential work never featured;
commission draft isolation between browsers; late derivation does not enter a
sealed delivery; search/share only read allowed versions."""
import json
from datetime import datetime, timedelta, timezone

from tests.conftest import grant


def _iso(delta_hours):
    return (datetime.now(timezone.utc) + timedelta(hours=delta_hours)).isoformat()


def test_hold_expires_and_blocks_booking(make, admin):
    data = make()
    p = admin.post("/api/admin/projects", json={
        "artist_id": data["artist"]["id"], "client_id": data["client"]["id"],
        "title": "档期委托", "freeze_mode": "stage", "edit_rounds_included": 1})
    pid = p.json()["id"]
    slot = admin.post("/api/admin/slots", json={
        "starts_at": _iso(72), "ends_at": _iso(76), "ttl_hours": 0}).json()
    # ttl_hours=0 -> already expired; listing marks it EXPIRED
    slots = admin.get("/api/admin/slots").json()
    target = next(s for s in slots if s["id"] == slot["id"])
    assert target["status"] == "expired"
    r = admin.post("/api/admin/slots/book",
                   json={"slot_id": slot["id"], "project_id": pid})
    assert r.status_code == 422 and "过期" in r.json()["detail"]


def test_booking_conflict_detection(make, admin):
    d1 = make(title="项目甲")
    d2 = make(title="项目乙")
    p1 = admin.post("/api/admin/projects", json={
        "artist_id": d1["artist"]["id"], "client_id": d1["client"]["id"],
        "title": "甲", "freeze_mode": "stage"}).json()["id"]
    p2 = admin.post("/api/admin/projects", json={
        "artist_id": d2["artist"]["id"], "client_id": d2["client"]["id"],
        "title": "乙", "freeze_mode": "stage"}).json()["id"]
    s1 = admin.post("/api/admin/slots", json={
        "starts_at": _iso(100), "ends_at": _iso(108), "ttl_hours": 24}).json()
    s2 = admin.post("/api/admin/slots", json={
        "starts_at": _iso(104), "ends_at": _iso(112), "ttl_hours": 24}).json()
    assert admin.post("/api/admin/slots/book",
                      json={"slot_id": s1["id"], "project_id": p1}).status_code == 200
    r = admin.post("/api/admin/slots/book",
                   json={"slot_id": s2["id"], "project_id": p2})
    assert r.status_code == 422 and "冲突" in r.json()["detail"]


def test_confidential_featured_never_public(make, admin, client):
    data = make(title="保密项目", confidential=True, featured=True, versions=("v1",))
    w = admin.get(f"/api/admin/works/{data['work']['id']}").json()
    master = w["versions"][0]["assets"][0]
    # even with a display grant, confidentiality wins
    grant(admin, master["id"], "display", True)
    assert client.get("/api/public/featured").json() == []
    assert client.get("/api/public/search?q=保密").json() == []
    assert client.get(f"/api/public/works/{w['id']}/story").status_code == 404
    assert client.get(f"/api/public/share/{w['id']}").status_code == 404
    # and flipping featured on a normal work still needs display rights
    data2 = make(title="普通作品", featured=False, versions=("v1",))
    w2 = admin.get(f"/api/admin/works/{data2['work']['id']}").json()
    m2 = w2["versions"][0]["assets"][0]
    admin.patch(f"/api/admin/works/{w2['id']}", json={"featured": True})
    assert client.get("/api/public/featured").json() == []  # no display license yet
    grant(admin, m2["id"], "display", True)
    ids = [x["work_id"] for x in client.get("/api/public/featured").json()]
    assert w2["id"] in ids and w["id"] not in ids


def test_share_card_only_with_licensed_thumbnail(make, admin, client):
    data = make(versions=("v1",))
    wid = data["work"]["id"]
    w = admin.get(f"/api/admin/works/{wid}").json()
    master = w["versions"][0]["assets"][0]
    vid = w["versions"][0]["id"]
    grant(admin, master["id"], "display", True)
    # master display alone isn't enough for a card image (needs thumbnail)
    assert client.get(f"/api/public/share/{wid}").status_code == 404
    admin.post("/api/admin/derivations",
               json={"source_asset_id": master["id"], "target_version_id": vid,
                     "kind": "thumbnail"})
    admin.post("/api/admin/derivations/run")
    # late/unlicensed thumbnail still blocks the card
    assert client.get(f"/api/public/share/{wid}").status_code == 404
    w2 = admin.get(f"/api/admin/works/{wid}").json()
    thumb = [a for v in w2["versions"] for a in v["assets"]
             if a["kind"] == "thumbnail"][0]
    grant(admin, thumb["id"], "display", True)
    card = client.get(f"/api/public/share/{wid}")
    assert card.status_code == 200
    assert card.json()["image_url"].endswith(f"/files/{thumb['id']}/bytes")


def test_draft_isolation_between_browsers(make, admin, client):
    from app.main import app as _app
    from fastapi.testclient import TestClient
    other = TestClient(_app)  # separate cookie jar / browser
    # browser A saves draft
    r = client.post("/api/commission/drafts", json={
        "intended_use": "童书封面", "delivery_scope": "成稿+展示授权",
        "contact": "a@x.com"})
    assert r.status_code == 200
    token_a = r.json()["draft_token"]
    # browser B cannot restore A's draft even knowing the token (cookie mismatch)
    r = other.get(f"/api/commission/drafts/restore?draft_token={token_a}")
    assert r.status_code == 403
    # browser B with no cookie cannot access anything
    assert other.get("/api/commission/drafts/restore").status_code in (403, 422)
    # A restores its own draft fine
    r = client.get(f"/api/commission/drafts/restore?draft_token={token_a}")
    assert r.status_code == 200 and r.json()["intended_use"] == "童书封面"
    # submit requires both fields (use a third browser with an empty draft)
    third = TestClient(_app)
    bad = third.post("/api/commission/drafts", json={
        "intended_use": "", "delivery_scope": ""})
    token_bad = bad.json()["draft_token"]
    r = third.post("/api/commission/drafts/submit", json={"draft_token": token_bad})
    assert r.status_code == 422
    # submit A
    r = client.post("/api/commission/drafts/submit", json={"draft_token": token_a})
    assert r.status_code == 200
    # after submit it can't be restored
    r = client.get(f"/api/commission/drafts/restore?draft_token={token_a}")
    assert r.status_code == 410
    # admin only sees submitted commissions; can't enumerate other open drafts
    listing = admin.get("/api/admin/commissions?submitted=true").json()
    assert any(x["intended_use"] == "童书封面" for x in listing)
    open_count = admin.get("/api/admin/commissions?submitted=false").json()
    assert open_count == {"open_draft_count": 1}  # token_bad exists, content hidden


def test_late_derivation_not_in_approved_delivery(make, admin, client):
    data = make(versions=("v1 成稿",))
    token = data["client"]["access_token"]
    h = {"X-Client-Token": token}
    pid = admin.post("/api/admin/projects", json={
        "artist_id": data["artist"]["id"], "client_id": data["client"]["id"],
        "work_id": data["work"]["id"], "title": "交付委托",
        "freeze_mode": "stage", "edit_rounds_included": 1}).json()["id"]
    v1 = data["version_ids"][0]
    client.post("/api/client/approvals", json={"version_id": v1}, headers=h)
    w = admin.get(f"/api/admin/works/{data['work']['id']}").json()
    master = w["versions"][0]["assets"][0]
    scope = f"client:{data['client']['id']}"
    grant(admin, master["id"], "download", True, scope)
    start = client.post("/api/client/acceptances",
                        json={"project_id": pid, "version_ids": [v1]},
                        headers=h).json()
    items = start["items"]
    client.post("/api/client/acceptances/artist-confirm",
                json={"acceptance_id": start["acceptance_id"], "items": items},
                headers=h)
    client.post("/api/client/acceptances/client-confirm",
                json={"acceptance_id": start["acceptance_id"], "items": items},
                headers=h)
    pkg = client.post("/api/client/packages/seal",
                      json={"acceptance_id": start["acceptance_id"]},
                      headers=h).json()
    sealed_shas = {i["sha256"] for i in pkg["manifest"]}

    # thumbnail derivation task arrives LATE and finishes after sealing
    admin.post("/api/admin/derivations",
               json={"source_asset_id": master["id"], "target_version_id": v1,
                     "kind": "thumbnail"})
    run = admin.post("/api/admin/derivations/run").json()
    late_sha = admin.get(f"/api/admin/works/{data['work']['id']}").json()
    thumb = [a for v in late_sha["versions"] for a in v["assets"]
             if a["kind"] == "thumbnail"][0]
    assert run["processed"][0]["public_rights"]["display"] is False
    assert thumb["sha256"] not in sealed_shas
    # sealed package bytes remain identical (only approved master)
    import io, zipfile
    r = client.get(f"/api/client/packages/{pkg['package_id']}/download", headers=h)
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    payload = [n for n in zf.namelist() if n.startswith("files/")]
    assert len(payload) == 1
    trace = admin.get(f"/api/admin/trace/projects/{pid}/delivery").json()
    assert trace[0]["items"][0]["covered_by_approvals"] != []
