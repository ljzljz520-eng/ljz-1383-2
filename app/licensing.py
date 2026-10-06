"""许可授权与撤回：撤回是事件 => 写拒绝行 + 清公开读模型 + 派生任务失效。"""
import json

from . import audit, permissions, public
from .errors import Conflict, NotFound


def grant(conn, licensee, scope, terms, now, project_id=None, artwork_id=None):
    cur = conn.execute(
        "INSERT INTO license_grants(project_id,artwork_id,licensee,scope,terms,granted_at)"
        " VALUES(?,?,?,?,?,?)",
        (project_id, artwork_id, licensee, json.dumps(scope), terms, now))
    return cur.lastrowid


def _affected_artworks(conn, g):
    if g["artwork_id"]:
        return [g["artwork_id"]]
    return [r["id"] for r in conn.execute(
        "SELECT id FROM artworks WHERE project_id=?", (g["project_id"],)).fetchall()]


def revoke(conn, grant_id, reason, actor, now):
    g = conn.execute("SELECT * FROM license_grants WHERE id=?", (grant_id,)).fetchone()
    if g is None:
        raise NotFound("授权不存在")
    if g["revoked_at"]:
        raise Conflict("授权已撤回")
    conn.execute("UPDATE license_grants SET revoked_at=?, revoke_reason=? WHERE id=?",
                 (now, reason, grant_id))
    for artwork_id in _affected_artworks(conn, g):
        # 1) 技术权限：对 public/client 全动作写拒绝行（管理员留档可见）
        for a in conn.execute(
                "SELECT a.id FROM assets a JOIN artwork_versions v ON v.id=a.version_id "
                "WHERE v.artwork_id=?", (artwork_id,)).fetchall():
            for audience in ("public", "client"):
                for action in permissions.ACTIONS:
                    permissions.set_permission(conn, a["id"], audience, action, False)
        # 2) 公开读模型与精选即刻清除
        public.rebuild_listing(conn, artwork_id, now)
        # 3) 未完成的派生任务失效，防止晚到任务复活已撤回内容
        conn.execute(
            "UPDATE derivative_jobs SET status='superseded', finished_at=? "
            "WHERE status IN ('pending','running') AND version_id IN "
            "(SELECT id FROM artwork_versions WHERE artwork_id=?)", (now, artwork_id))
    audit.log(conn, actor, "revoke_license", "license", grant_id,
              {"reason": reason}, now)


def license_active_for_artwork(conn, artwork_id) -> bool:
    """从未授权=不限制；存在授权且全部被撤回=失效。"""
    rows = conn.execute(
        "SELECT revoked_at FROM license_grants WHERE artwork_id=? OR project_id="
        "(SELECT project_id FROM artworks WHERE id=?)",
        (artwork_id, artwork_id)).fetchall()
    if not rows:
        return True
    return any(r["revoked_at"] is None for r in rows)
