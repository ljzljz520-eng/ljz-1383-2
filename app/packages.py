"""下载包：清单只包含"实际获批"的素材 —— 以批准记录为准，而不是最新文件。"""
import json

from . import audit, licensing
from .errors import NotFound


def build_package(conn, project_id, requester_id, now):
    p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if p is None:
        raise NotFound("项目不存在")
    # 每个作品取"最后一次被批准的版本"；批准后新上传的版本不会混入
    rows = conn.execute(
        "SELECT ap.id AS approval_id, v.id AS version_id, v.file_hash, v.artwork_id, v.seq "
        "FROM approvals ap JOIN artwork_versions v ON v.id=ap.version_id "
        "WHERE ap.project_id=? ORDER BY ap.id", (project_id,)).fetchall()
    best = {}
    for r in rows:
        best[r["artwork_id"]] = r
    items = []
    for art_id, r in best.items():
        if not licensing.license_active_for_artwork(conn, art_id):
            continue  # 授权已撤回 => 不入包
        assets = conn.execute(
            "SELECT id, role, file_hash, storage_path FROM assets WHERE version_id=?",
            (r["version_id"],)).fetchall()
        items.append({
            "artwork_id": art_id,
            "version_id": r["version_id"],
            "version_seq": r["seq"],
            "file_hash": r["file_hash"],
            "approval_id": r["approval_id"],
            "assets": [dict(a) for a in assets],
        })
    licenses = conn.execute(
        "SELECT id, licensee, scope, terms, granted_at FROM license_grants "
        "WHERE project_id=? AND revoked_at IS NULL", (project_id,)).fetchall()
    manifest = {
        "project_id": project_id,
        "generated_at": now,
        "items": items,
        "licenses": [dict(l) for l in licenses],
    }
    cur = conn.execute(
        "INSERT INTO download_packages(project_id,requested_by,manifest,created_at)"
        " VALUES(?,?,?,?)",
        (project_id, requester_id, json.dumps(manifest, ensure_ascii=False), now))
    audit.log(conn, f"user:{requester_id}", "build_package", "package", cur.lastrowid,
              {"project_id": project_id, "items": len(items)}, now)
    return manifest
