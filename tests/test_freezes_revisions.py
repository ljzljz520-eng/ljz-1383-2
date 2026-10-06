"""Package vs stage freeze, and revision constraints: quote / edit rounds /
deadline enforced with a change-order path."""
from datetime import datetime, timedelta, timezone

from tests.conftest import grant


def _project(admin, data, mode="stage", rounds=2, due_days=10, price=80000):
    due = (datetime.now(timezone.utc) + timedelta(days=due_days)).isoformat()
    r = admin.post("/api/admin/projects", json={
        "artist_id": data["artist"]["id"], "client_id": data["client"]["id"],
        "work_id": data["work"]["id"], "title": "委托", "freeze_mode": mode,
        "price_cents": price, "edit_rounds_included": rounds, "due_date": due})
    assert r.status_code == 200, r.text
    return r.json()


def _revise(admin, pid, detail="调整配色", stage_id=None, version_id=None):
    return admin.post(f"/api/admin/projects/{pid}/revisions", json={
        "detail": detail, "stage_id": stage_id, "version_id": version_id}).json()


def test_package_freeze_blocks_all_stages(make, admin):
    data = make(versions=("v1", "v2"))
    p = _project(admin, data, mode="package")
    pid = p["id"]
    s1 = admin.post(f"/api/admin/projects/{pid}/stages",
                    json={"name": "草图阶段"}).json()
    # cannot freeze a single stage in package mode
    r = admin.post(f"/api/admin/projects/{pid}/stages/{s1['id']}/freeze",
                   json={"reason": ""})
    assert r.status_code == 422
    r = admin.post(f"/api/admin/projects/{pid}/freeze", json={"reason": "整包定稿"})
    assert r.status_code == 200
    pj = admin.get(f"/api/admin/projects/{pid}").json()
    assert pj["frozen"] is True
    assert all(s["status"] == "frozen" for s in pj["stages"])
    out = _revise(admin, pid)
    assert out["status"] == "rejected"
    assert "整包冻结" in out["decision_reason"]


def test_stage_freeze_keeps_other_stages_open(make, admin):
    data = make(versions=("v1",))
    p = _project(admin, data, mode="stage", rounds=5)
    pid = p["id"]
    s1 = admin.post(f"/api/admin/projects/{pid}/stages",
                    json={"name": "草图", "edit_rounds_included": 1}).json()
    s2 = admin.post(f"/api/admin/projects/{pid}/stages",
                    json={"name": "上色", "edit_rounds_included": 1}).json()
    admin.post(f"/api/admin/projects/{pid}/stages/{s1['id']}/freeze", json={})
    blocked = _revise(admin, pid, stage_id=s1["id"])
    assert blocked["status"] == "rejected" and "已冻结" in blocked["decision_reason"]
    open_rev = _revise(admin, pid, stage_id=s2["id"])
    assert open_rev["status"] == "approved"


def test_rounds_quota_consumed_then_change_order(make, admin):
    data = make(versions=("v1",))
    p = _project(admin, data, mode="stage", rounds=1)
    pid = p["id"]
    # first revision in scope consumes the single round
    first = _revise(admin, pid, detail="第一次修改")
    assert first["status"] == "approved" and "剩余 0" in first["decision_reason"]
    # second revision needs commercial change order
    second = _revise(admin, pid, detail="第二次修改")
    assert second["status"] == "requested"
    assert "变更单" in second["decision_reason"]
    co = admin.post("/api/admin/change-orders", json={
        "revision_id": second["id"], "extra_charge_cents": 12000,
        "extra_rounds": 1,
        "new_due": (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()}).json()
    assert co["change_order_id"]
    # cannot apply before client accepts
    r = admin.post("/api/admin/change-orders/accept",
                   json={"revision_id": second["id"], "accepted": False})
    assert r.json()["applied"] is False
    r = admin.post("/api/admin/change-orders/accept",
                   json={"revision_id": second["id"], "accepted": True})
    assert r.status_code == 200 and r.json()["applied"] is True
    pj = admin.get(f"/api/admin/projects/{pid}").json()
    assert pj["edit_rounds_included"] == 2
    assert pj["rounds_used"] == 2


def test_overdue_revision_requires_change_order(make, admin):
    data = make(versions=("v1",))
    p = _project(admin, data, mode="package", rounds=5, due_days=-2)
    pid = p["id"]
    out = _revise(admin, pid, detail="逾期修改")
    assert out["status"] == "requested"
    assert "截止日" in out["decision_reason"]


def test_revision_check_endpoint_reports_constraints(make, admin):
    data = make(versions=("v1",))
    p = _project(admin, data, rounds=2, due_days=3)
    chk = admin.get(f"/api/admin/projects/{p['id']}/revision-check").json()
    assert chk["rounds_remaining"] == 2 and chk["blocked"] is False
