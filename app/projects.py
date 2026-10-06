"""项目：创建、双方验收(并发安全)、保密切换。"""
from . import audit, public
from .errors import Conflict, NotFound

DEFAULT_STAGES = (("sketch", 1), ("line", 2), ("color", 3), ("final", 4))


def create_project(conn, title, freeze_mode, quote_cents, revision_limit, deadline,
                   now, confidential=False, commission_id=None, stage_revision_limit=0):
    cur = conn.execute(
        "INSERT INTO projects(commission_id,title,confidential,freeze_mode,quote_cents,"
        " revision_limit,deadline,status) VALUES(?,?,?,?,?,?,?, 'active')",
        (commission_id, title, 1 if confidential else 0, freeze_mode, quote_cents,
         revision_limit, deadline))
    pid = cur.lastrowid
    for kind, seq in DEFAULT_STAGES:
        conn.execute(
            "INSERT INTO stages(project_id,kind,seq,revision_limit) VALUES(?,?,?,?)",
            (pid, kind, seq, stage_revision_limit))
    return pid


def confirm_acceptance(conn, project_id, party, version_id, now):
    """双方验收：各自确认的版本一致才成交。

    并发守卫：乐观锁条件更新；双方同时确认不同版本 => mismatch 状态，
    任何一方都不能把"自己确认的版本"单方面写成验收结果。
    """
    col = {"client": "client_version_id", "artist": "artist_version_id"}.get(party)
    if col is None:
        raise NotFound("未知确认方")
    v = conn.execute(
        "SELECT v.id FROM artwork_versions v JOIN artworks a ON a.id=v.artwork_id "
        "WHERE v.id=? AND a.project_id=?", (version_id, project_id)).fetchone()
    if v is None:
        raise NotFound("版本不属于该项目")
    p = conn.execute("SELECT status, lock_version FROM projects WHERE id=?",
                     (project_id,)).fetchone()
    if p is None:
        raise NotFound("项目不存在")
    if p["status"] == "accepted":
        raise Conflict("项目已验收，确认不可再变更")
    cur = conn.execute(
        f"UPDATE projects SET {col}=?, lock_version=lock_version+1 "
        f"WHERE id=? AND lock_version=?", (version_id, project_id, p["lock_version"]))
    if cur.rowcount == 0:
        raise Conflict("并发冲突：请刷新后重试")
    p = conn.execute(
        "SELECT client_version_id AS c, artist_version_id AS a FROM projects WHERE id=?",
        (project_id,)).fetchone()
    if p["c"] and p["a"]:
        if p["c"] == p["a"]:
            conn.execute(
                "UPDATE projects SET accepted_version_id=?, status='accepted' WHERE id=?",
                (p["c"], project_id))
            audit.log(conn, f"{party}", "accept", "project", project_id,
                      {"version_id": p["c"]}, now)
            return {"status": "accepted", "version_id": p["c"]}
        return {"status": "mismatch",
                "client_version_id": p["c"], "artist_version_id": p["a"]}
    return {"status": "pending"}


def set_confidential(conn, project_id, confidential, actor, now):
    conn.execute("UPDATE projects SET confidential=? WHERE id=?",
                 (1 if confidential else 0, project_id))
    if confidential:
        # 保密化是事件：立即把该项目所有作品从公开读模型与精选中清除
        for row in conn.execute("SELECT id FROM artworks WHERE project_id=?",
                                (project_id,)).fetchall():
            public.rebuild_listing(conn, row["id"], now)
    audit.log(conn, actor, "set_confidential", "project", project_id,
              {"confidential": bool(confidential)}, now)
