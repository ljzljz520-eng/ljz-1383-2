"""版本与批准：批准指向 (version_id, file_hash) 快照，永不迁移到更新的版本。"""
from . import audit
from .errors import NotFound


def add_version(conn, artwork_id, stage_id, file_hash, storage_path, note, actor_id, now):
    row = conn.execute(
        "SELECT COALESCE(MAX(seq),0) AS m FROM artwork_versions WHERE artwork_id=?",
        (artwork_id,)).fetchone()
    seq = row["m"] + 1
    prev = conn.execute(
        "SELECT id, file_hash FROM artwork_versions WHERE artwork_id=? ORDER BY seq DESC LIMIT 1",
        (artwork_id,)).fetchone()
    cur = conn.execute(
        "INSERT INTO artwork_versions(artwork_id,stage_id,seq,file_hash,storage_path,note,"
        " created_by,created_at,supersedes_version_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (artwork_id, stage_id, seq, file_hash, storage_path, note, actor_id, now,
         prev["id"] if prev else None))
    new_id = cur.lastrowid

    # 若上一版已被客户批准，本次修改自动登记"变更差异"，批准对象仍锁定旧版
    ctx = conn.execute("SELECT project_id FROM artworks WHERE id=?", (artwork_id,)).fetchone()
    if prev and ctx and ctx["project_id"]:
        ap = conn.execute(
            "SELECT ap.* FROM approvals ap JOIN artwork_versions v ON v.id=ap.version_id "
            "WHERE v.artwork_id=? ORDER BY ap.id DESC LIMIT 1", (artwork_id,)).fetchone()
        if ap and ap["version_id"] == prev["id"]:
            conn.execute(
                "INSERT INTO change_diffs(project_id,from_version_id,to_version_id,summary,created_at)"
                " VALUES(?,?,?,?,?)",
                (ctx["project_id"], prev["id"], new_id,
                 f"批准后修改: hash {prev['file_hash'][:8]}→{file_hash[:8]}; {note}", now))
    return new_id


def latest_version(conn, artwork_id):
    return conn.execute(
        "SELECT * FROM artwork_versions WHERE artwork_id=? AND invalidated=0 "
        "ORDER BY seq DESC LIMIT 1", (artwork_id,)).fetchone()


def approve(conn, project_id, version_id, client_id, now, note=""):
    v = conn.execute(
        "SELECT v.* FROM artwork_versions v JOIN artworks a ON a.id=v.artwork_id "
        "WHERE v.id=? AND a.project_id=?", (version_id, project_id)).fetchone()
    if v is None:
        raise NotFound("版本不属于该项目")
    cur = conn.execute(
        "INSERT INTO approvals(project_id,stage_id,version_id,file_hash,approved_by,approved_at,note)"
        " VALUES(?,?,?,?,?,?,?)",
        (project_id, v["stage_id"], version_id, v["file_hash"], client_id, now, note))
    audit.log(conn, f"user:{client_id}", "approve", "approval", cur.lastrowid,
              {"project_id": project_id, "version_id": version_id, "hash": v["file_hash"]}, now)
    return cur.lastrowid


def approval_status(conn, artwork_id):
    """批准对象 vs 最新版本：stale=True 表示"最新文件不是已批准文件"。"""
    ap = conn.execute(
        "SELECT ap.* FROM approvals ap JOIN artwork_versions v ON v.id=ap.version_id "
        "WHERE v.artwork_id=? ORDER BY ap.id DESC LIMIT 1", (artwork_id,)).fetchone()
    lv = latest_version(conn, artwork_id)
    return {
        "approved_version_id": ap["version_id"] if ap else None,
        "approved_hash": ap["file_hash"] if ap else None,
        "latest_version_id": lv["id"] if lv else None,
        "latest_hash": lv["file_hash"] if lv else None,
        "stale": bool(ap and lv and ap["version_id"] != lv["id"]),
    }
