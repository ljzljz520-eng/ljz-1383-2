import json


def log(conn, actor, action, entity, entity_id, payload=None, now=""):
    conn.execute(
        "INSERT INTO audit_log(actor,action,entity,entity_id,payload,created_at) VALUES(?,?,?,?,?,?)",
        (actor, action, entity, entity_id, json.dumps(payload or {}, ensure_ascii=False), now),
    )


def trace(conn, project_id):
    """管理页可追溯：审批链 + 变更差异 + 交付(下载包)依赖 + 项目相关审计事件。"""
    approvals = conn.execute(
        "SELECT ap.id, ap.version_id, ap.file_hash, ap.approved_at, u.name AS approved_by "
        "FROM approvals ap JOIN users u ON u.id=ap.approved_by "
        "WHERE ap.project_id=? ORDER BY ap.id", (project_id,)).fetchall()
    diffs = conn.execute(
        "SELECT * FROM change_diffs WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
    packages = conn.execute(
        "SELECT id, requested_by, created_at FROM download_packages WHERE project_id=? ORDER BY id",
        (project_id,)).fetchall()
    events = conn.execute(
        "SELECT actor, action, entity, entity_id, payload, created_at FROM audit_log "
        "WHERE (entity='project' AND entity_id=?) OR entity IN ('approval','license','revision','package') "
        "ORDER BY id", (project_id,)).fetchall()
    return {
        "approvals": [dict(r) for r in approvals],
        "change_diffs": [dict(r) for r in diffs],
        "download_packages": [dict(r) for r in packages],
        "events": [dict(r) for r in events],
    }
