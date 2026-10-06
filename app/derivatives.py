"""派生任务管线：幂等入队；执行前校验源哈希/版本新旧/授权状态。

权限不在任务落库时烘焙，读取时按当前授权解析 —— 晚到的任务即使完成，
也无法让已撤回的内容重新公开。
"""
from . import licensing
from .errors import NotFound


def enqueue(conn, version_id, role, source_hash, idempotency_key, now):
    conn.execute(
        "INSERT OR IGNORE INTO derivative_jobs(version_id,role,source_hash,idempotency_key,"
        " created_at) VALUES(?,?,?,?,?)",
        (version_id, role, source_hash, idempotency_key, now))
    return conn.execute("SELECT * FROM derivative_jobs WHERE idempotency_key=?",
                        (idempotency_key,)).fetchone()["id"]


def run_job(conn, job_id, now, produce):
    """produce(job) -> (storage_path, file_hash)。返回 done/superseded/already-claimed。"""
    cur = conn.execute(
        "UPDATE derivative_jobs SET status='running' WHERE id=? AND status='pending'",
        (job_id,))
    if cur.rowcount == 0:
        return "already-claimed"
    job = conn.execute("SELECT * FROM derivative_jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise NotFound("任务不存在")

    def finish(status):
        conn.execute("UPDATE derivative_jobs SET status=?, finished_at=? WHERE id=?",
                     (status, now, job_id))

    v = conn.execute("SELECT * FROM artwork_versions WHERE id=?",
                     (job["version_id"],)).fetchone()
    latest = conn.execute(
        "SELECT file_hash FROM artwork_versions WHERE artwork_id=? AND invalidated=0 "
        "ORDER BY seq DESC LIMIT 1", (v["artwork_id"],)).fetchone() if v else None
    # 晚到守卫：源已被更新版本取代 / 版本被作废 / 源哈希漂移
    if v is None or v["invalidated"] or latest is None \
            or latest["file_hash"] != job["source_hash"]:
        finish("superseded")
        return "superseded"
    # 授权守卫：授权已撤回的图像不再产出新派生物
    if not licensing.license_active_for_artwork(conn, v["artwork_id"]):
        finish("superseded")
        return "superseded-license-revoked"
    path, h = produce(job)
    conn.execute(
        "INSERT INTO assets(version_id,role,storage_path,file_hash,created_at)"
        " VALUES(?,?,?,?,?) "
        "ON CONFLICT(version_id,role) DO UPDATE SET storage_path=excluded.storage_path,"
        " file_hash=excluded.file_hash, created_at=excluded.created_at",
        (v["id"], job["role"], path, h, now))
    finish("done")
    return "done"
