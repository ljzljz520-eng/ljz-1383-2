"""Per-asset rights: display without download/sublicense; thumbnails/process/
master licensed independently; default deny; revocation propagation."""
from tests.conftest import grant


def test_default_no_public_rights(make, client, admin):
    data = make()
    w = admin.get(f"/api/admin/works/{data['work']['id']}").json()
    master = w["versions"][-1]["assets"][0]
    # everything denied by default
    assert all(v is False for v in master["public_rights"].values())
    # public site exposes nothing
    assert client.get("/api/public/works").json() == []
    assert client.get(f"/api/public/files/{master['id']}/bytes").status_code == 404


def test_display_without_download_is_partial(make, client, admin):
    data = make()
    w = admin.get(f"/api/admin/works/{data['work']['id']}").json()
    master = w["versions"][-1]["assets"][0]
    grant(admin, master["id"], "display", True)
    # visible on public site, file bytes served
    story = client.get(f"/api/public/works/{w['id']}/story").json()
    assert "master" in story["versions"][-1]["assets"]
    r = client.get(f"/api/public/files/{master['id']}/bytes")
    assert r.status_code == 200 and len(r.content) > 0

    # explicit denial of download: no download grant exists; public endpoint
    # never serves downloads regardless
    grant(admin, master["id"], "download", False)
    assert client.get(f"/api/public/files/{master['id']}/bytes").status_code == 200
    # and sublicense remains absent
    detail = admin.get(f"/api/admin/works/{w['id']}").json()
    rights = detail["versions"][-1]["assets"][0]["public_rights"]
    assert rights == {"display": True, "download": False, "sublicense": False}


def test_derivatives_licensed_separately(make, client, admin):
    data = make()
    w = admin.get(f"/api/admin/works/{data['work']['id']}").json()
    master = w["versions"][-1]["assets"][0]
    vid = w["versions"][-1]["id"]
    # master is public display; queue thumbnail + process derivations
    grant(admin, master["id"], "display", True)
    for kind in ("thumbnail", "process"):
        r = admin.post("/api/admin/derivations",
                       json={"source_asset_id": master["id"],
                             "target_version_id": vid, "kind": kind})
        assert r.status_code == 200
    run = admin.post("/api/admin/derivations/run").json()
    assert len(run["processed"]) == 2
    # derivatives must NOT inherit the master's public display
    for item in run["processed"]:
        assert item["public_rights"]["display"] is False

    w2 = admin.get(f"/api/admin/works/{w['id']}").json()
    assets = {a["kind"]: a for v in w2["versions"] for a in v["assets"]}
    # license ONLY the thumbnail publicly; process stays hidden
    grant(admin, assets["thumbnail"]["id"], "display", True)
    story = client.get(f"/api/public/works/{w['id']}/story").json()
    kinds = {k for v in story["versions"] for k in v["assets"]}
    assert "thumbnail" in kinds and "master" in kinds
    assert "process" not in kinds  # 过程图未授权 -> 不出现
    # bytes for process is 404 even though same version is public
    proc_id = assets["process"]["id"]
    assert client.get(f"/api/public/files/{proc_id}/bytes").status_code == 404


def test_revocation_propagates_immediately(make, client, admin):
    data = make()
    w = admin.get(f"/api/admin/works/{data['work']['id']}").json()
    master = w["versions"][-1]["assets"][0]
    g = grant(admin, master["id"], "display", True)
    gid = g["scope_decisions"]["public"]["display"]["grant_id"]
    assert client.get(f"/api/public/files/{master['id']}/bytes").status_code == 200
    assert client.get("/api/public/search?q=星夜").json() != []
    r = admin.post(f"/api/admin/grants/{gid}/revoke")
    assert r.status_code == 200
    assert client.get(f"/api/public/files/{master['id']}/bytes").status_code == 404
    # search re-checks live grants: dropped before/after any reindex
    assert client.get("/api/public/search?q=星夜").json() == []


def test_explicit_deny_overrides_public(make, admin):
    data = make()
    scope = f"client:{data['client']['id']}"
    w = admin.get(f"/api/admin/works/{data['work']['id']}").json()
    master = w["versions"][-1]["assets"][0]
    grant(admin, master["id"], "download", True, "public")
    grant(admin, master["id"], "download", False, scope)
    detail = admin.get(f"/api/admin/works/{w['id']}").json()
    dec = detail["versions"][-1]["assets"][0]["scope_decisions"]
    assert dec["public"]["download"]["granted"] is True
    assert dec[scope]["download"]["granted"] is False
