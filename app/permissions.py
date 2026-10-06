"""权限矩阵：不存在统一的"公开开关"。

每个派生资产(缩略图/过程图/成稿/水印图...) × 受众(public/client/admin) × 动作
(display/download/index/share_card/relicense) 是独立一行授权，默认拒绝。
保密项目对非管理员硬覆盖；有效权限在读取时解析，撤回授权即刻生效。
"""

ACTIONS = ("display", "download", "index", "share_card", "relicense")
AUDIENCES = ("public", "client", "admin")


def set_permission(conn, asset_id, audience, action, allowed):
    conn.execute(
        "INSERT INTO asset_permissions(asset_id,audience,action,allowed) VALUES(?,?,?,?) "
        "ON CONFLICT(asset_id,audience,action) DO UPDATE SET allowed=excluded.allowed",
        (asset_id, audience, action, 1 if allowed else 0),
    )


def grant_matrix(conn, asset_id, audience, **actions):
    for action, allowed in actions.items():
        assert action in ACTIONS, action
        set_permission(conn, asset_id, audience, action, allowed)


def asset_context(conn, asset_id):
    return conn.execute(
        "SELECT a.id AS asset_id, v.artwork_id AS artwork_id, aw.project_id AS project_id, "
        "       COALESCE(p.confidential,0) AS confidential "
        "FROM assets a "
        "JOIN artwork_versions v ON v.id=a.version_id "
        "JOIN artworks aw ON aw.id=v.artwork_id "
        "LEFT JOIN projects p ON p.id=aw.project_id "
        "WHERE a.id=?", (asset_id,)).fetchone()


def effective(conn, asset_id, audience, action) -> bool:
    """有效权限 = 管理员放行 → 保密硬拒绝 → 显式授权行 → 默认拒绝。"""
    if audience == "admin":
        return True
    ctx = asset_context(conn, asset_id)
    if ctx is None:
        return False
    if ctx["confidential"]:
        return False
    row = conn.execute(
        "SELECT allowed FROM asset_permissions WHERE asset_id=? AND audience=? AND action=?",
        (asset_id, audience, action)).fetchone()
    return bool(row["allowed"]) if row else False
