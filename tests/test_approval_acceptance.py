"""Approval object immutability, drift after edits, two-party same-version
acceptance, and sealed package = actually approved assets."""
import json

from tests.conftest import grant


def _client_headers(client_token):
    return {"X-Client-Token": client_token}


def _project(admin, data, mode="stage", rounds=3):
    r = admin.post("/api/admin/projects", json={
        "artist_id": data["artist"]["id"], "client_id": data["client"]["id"],
        "work_id": data["work"]["id"], "title": "封面委托",
        "freeze_mode": mode, "price_cents": 80000,
        "edit_rounds_included": rounds})
    assert r.status_code == 200
    return r.json()["id"]


def test_new_upload_does_not_become_approved(make, admin, client):
    data = make()
    token = data["client"]["access_token"]
    h = _client_headers(token)
    wid = data["work"]["id"]
    v1, v2 = data["version_ids"]
    _project(admin, data)
    # client approves v2 (the 成稿)
    r = client.post("/api/client/approvals", json={"version_id": v2}, headers=h)
    assert r.status_code == 200
    approval_id = r.json()["approval_id"]
    approved_fp = r.json()["asset_fingerprint"]
    approved_snap = json.loads(r.json()["asset_snapshot"])
    approved_sha = approved_snap[0]["sha256"]

    # artist uploads ANOTHER master into the same version after approval
    w = admin.get(f"/api/admin/works/{wid}").json()
    cur = w["versions"][-1]
    files = {"file": ("new-master.png", b"\x89PNG\r\n\x1a\n-binary-", "image/png")}
    up = admin.post(
        f"/api/admin/works/{wid}/versions/{cur['id']}/upload",
        files=files, data={"kind": "master"})
    assert up.status_code == 200

    # drift is reported; the approval object fingerprint is unchanged
    drift = client.get(f"/api/client/approvals/{approval_id}/drift", headers=h).json()
    assert drift["changed"] is True
    assert drift["approved_fingerprint"] == approved_fp
    trace = admin.get(f"/api/admin/trace/works/{cur['id']}/approvals").json()
    snap_shas = {a["sha256"] for a in trace[0]["snapshot"]}
    assert approved_sha in snap_shas
    assert up.json()["sha256"] not in snap_shas


def test_two_party_must_confirm_same_items(make, admin, client):
    data = make(versions=("v1",))
    token = data["client"]["access_token"]
    h = _client_headers(token)
    pid = _project(admin, data)
    wid = data["work"]["id"]
    w = admin.get(f"/api/admin/works/{wid}").json()
    assets = [a for v in w["versions"] for a in v["assets"]]
    v = w["versions"][0]
    items_a = [{"version_id": v["id"], "asset_id": assets[0]["id"],
                "kind": "master", "sha256": assets[0]["sha256"]}]
    r = client.post("/api/client/acceptances",
                    json={"project_id": pid, "items": items_a}, headers=h)
    acc = r.json()
    acc_id = acc["acceptance_id"]

    # artist confirms same items
    r = client.post("/api/client/acceptances/artist-confirm",
                    json={"acceptance_id": acc_id, "items": items_a}, headers=h)
    assert r.json()["completed"] is False  # only one party
    # client tries confirming a DIFFERENT (newer) version item set -> rejected
    w2 = admin.get(f"/api/admin/works/{wid}").json()
    vnew = admin.post(f"/api/admin/works/{wid}/versions",
                      json={"label": "v2 新稿"}).json()["versions"][-1]
    new_items = [{"version_id": vnew["id"], "asset_id": vnew["assets"][0]["id"],
                  "kind": "master", "sha256": vnew["assets"][0]["sha256"]}]
    r = client.post("/api/client/acceptances/client-confirm",
                    json={"acceptance_id": acc_id, "items": new_items}, headers=h)
    assert r.status_code == 422
    # correct same-item confirmation completes acceptance
    r = client.post("/api/client/acceptances/client-confirm",
                    json={"acceptance_id": acc_id, "items": items_a}, headers=h)
    assert r.status_code == 200 and r.json()["completed"] is True


def test_package_contains_approved_assets_and_blocks_without_download(make, admin, client):
    data = make(versions=("v1 成稿",))
    token = data["client"]["access_token"]
    h = _client_headers(token)
    pid = _project(admin, data)
    wid = data["work"]["id"]
    v1 = data["version_ids"][0]

    # client approval
    ap = client.post("/api/client/approvals", json={"version_id": v1}, headers=h).json()
    approved_sha = json.loads(ap["asset_snapshot"])[0]["sha256"]

    # start acceptance by default versions, both parties confirm
    start = client.post("/api/client/acceptances",
                        json={"project_id": pid, "version_ids": [v1]}, headers=h).json()
    items = start["items"]
    client.post("/api/client/acceptances/artist-confirm",
                json={"acceptance_id": start["acceptance_id"], "items": items}, headers=h)
    done = client.post("/api/client/acceptances/client-confirm",
                       json={"acceptance_id": start["acceptance_id"], "items": items},
                       headers=h).json()
    assert done["completed"]

    # sealing WITHOUT a download license must fail
    r = client.post("/api/client/packages/seal",
                    json={"acceptance_id": start["acceptance_id"]}, headers=h)
    assert r.status_code == 403

    # grant client-scoped download; seal succeeds and manifest matches approval
    w = admin.get(f"/api/admin/works/{wid}").json()
    master = w["versions"][0]["assets"][0]
    grant(admin, master["id"], "download", True, f"client:{data['client']['id']}")
    sealed = client.post("/api/client/packages/seal",
                         json={"acceptance_id": start["acceptance_id"]}, headers=h).json()
    assert sealed["manifest"][0]["sha256"] == approved_sha
    pkg_id = sealed["package_id"]

    # download the zip and inspect: MANIFEST + actual approved file
    import io, zipfile
    r = client.get(f"/api/client/packages/{pkg_id}/download", headers=h)
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert "MANIFEST.json" in zf.namelist()
    manifest = json.loads(zf.read("MANIFEST.json"))
    assert manifest["items"][0]["sha256"] == approved_sha
    zf.testzip() is None


def test_post_seal_revocation_blocks_download_but_keeps_trace(make, admin, client):
    data = make(versions=("v1",))
    token = data["client"]["access_token"]
    h = _client_headers(token)
    pid = _project(admin, data)
    v1 = data["version_ids"][0]
    client.post("/api/client/approvals", json={"version_id": v1}, headers=h)
    w = admin.get(f"/api/admin/works/{data['work']['id']}").json()
    master = w["versions"][0]["assets"][0]
    g = grant(admin, master["id"], "download", True, f"client:{data['client']['id']}")
    start = client.post("/api/client/acceptances",
                        json={"project_id": pid, "version_ids": [v1]}, headers=h).json()
    items = start["items"]
    client.post("/api/client/acceptances/artist-confirm",
                json={"acceptance_id": start["acceptance_id"], "items": items}, headers=h)
    client.post("/api/client/acceptances/client-confirm",
                json={"acceptance_id": start["acceptance_id"], "items": items}, headers=h)
    pkg = client.post("/api/client/packages/seal",
                      json={"acceptance_id": start["acceptance_id"]}, headers=h).json()
    gid = g["scope_decisions"][f"client:{data['client']['id']}"]["download"]["grant_id"]
    admin.post(f"/api/admin/grants/{gid}/revoke")
    r = client.get(f"/api/client/packages/{pkg['package_id']}/download", headers=h)
    assert r.status_code == 403
    # trace still shows the sealed manifest (history intact)
    trace = admin.get(f"/api/admin/trace/projects/{pid}/delivery").json()
    assert trace[0]["packages"][0]["items"][0]["sha256"] == master["sha256"]
