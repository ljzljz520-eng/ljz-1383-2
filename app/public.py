"""公开读模型：搜索、分享卡、首页精选只读 public_listings，绝不直查资产表。

任何权限/保密/授权变化都通过 rebuild_listing 重建投影；精选有写守卫 + 读时
JOIN 双重防线，保密项目不可能出现在首页。
"""
from . import permissions
from .errors import Forbidden, NotFound


def _purge(conn, artwork_id):
    conn.execute("DELETE FROM public_listings WHERE artwork_id=?", (artwork_id,))
    conn.execute("DELETE FROM featured_items WHERE artwork_id=?", (artwork_id,))


def rebuild_listing(conn, artwork_id, now):
    aw = conn.execute(
        "SELECT a.*, COALESCE(p.confidential,0) AS conf FROM artworks a "
        "LEFT JOIN projects p ON p.id=a.project_id WHERE a.id=?", (artwork_id,)).fetchone()
    if aw is None or aw["conf"]:
        _purge(conn, artwork_id)
        return False
    v = conn.execute(
        "SELECT * FROM artwork_versions WHERE artwork_id=? AND invalidated=0 "
        "ORDER BY seq DESC LIMIT 1", (artwork_id,)).fetchone()
    if v is None:
        _purge(conn, artwork_id)
        return False
    assets = conn.execute("SELECT * FROM assets WHERE version_id=?", (v["id"],)).fetchall()

    def pick(role, action):
        for a in assets:
            if a["role"] == role and permissions.effective(conn, a["id"], "public", action):
                return a["id"]
        return None

    displayable = any(permissions.effective(conn, a["id"], "public", "display")
                      for a in assets)
    if not displayable:
        _purge(conn, artwork_id)
        return False
    thumb = pick("thumbnail", "display") or pick("web", "display")
    og = pick("watermark", "share_card") or pick("web", "share_card")
    conn.execute(
        "INSERT INTO public_listings(artwork_id,title,summary,thumb_asset_id,og_asset_id,"
        " searchable_text,updated_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(artwork_id) DO UPDATE SET title=excluded.title,"
        " summary=excluded.summary, thumb_asset_id=excluded.thumb_asset_id,"
        " og_asset_id=excluded.og_asset_id, searchable_text=excluded.searchable_text,"
        " updated_at=excluded.updated_at",
        (artwork_id, aw["title"], aw["description"][:200], thumb, og,
         f"{aw['title']} {aw['description']}", now))
    return True


def search(conn, q):
    return [dict(r) for r in conn.execute(
        "SELECT artwork_id, title, summary, thumb_asset_id FROM public_listings "
        "WHERE searchable_text LIKE ? ORDER BY updated_at DESC",
        (f"%{q}%",)).fetchall()]


def share_card(conn, artwork_id):
    row = conn.execute("SELECT * FROM public_listings WHERE artwork_id=?",
                       (artwork_id,)).fetchone()
    if row is None:
        raise NotFound("该作品无公开分享卡")
    return dict(row)


def set_featured(conn, artwork_id, on, now, position=0):
    if on:
        # 写守卫：不在公开读模型中（保密/无展示权）一律拒绝
        row = conn.execute("SELECT artwork_id FROM public_listings WHERE artwork_id=?",
                           (artwork_id,)).fetchone()
        if row is None:
            raise Forbidden("保密或无公开权限的作品不能进入首页精选")
        conn.execute(
            "INSERT INTO featured_items(artwork_id,position,created_at) VALUES(?,?,?) "
            "ON CONFLICT(artwork_id) DO UPDATE SET position=excluded.position",
            (artwork_id, position, now))
    else:
        conn.execute("DELETE FROM featured_items WHERE artwork_id=?", (artwork_id,))


def homepage_featured(conn):
    # 读时防线：JOIN 读模型再过滤一次，即使 featured_items 有脏数据也不外泄
    return [dict(r) for r in conn.execute(
        "SELECT f.artwork_id, l.title, l.thumb_asset_id FROM featured_items f "
        "JOIN public_listings l ON l.artwork_id=f.artwork_id ORDER BY f.position",
        ).fetchall()]
