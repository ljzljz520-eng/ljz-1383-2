"""Anonymous public site: homepage featured, story view, search, share card,
license-gated file bytes. Confidential/unlicensed assets are unreachable."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AssetKind, FileAsset, Right, Work
from ..services.catalog import (
    featured_work_ids,
    list_public_works,
    public_story,
    search_works,
)
from ..services.rights import PUBLIC, has_right

router = APIRouter(prefix="/api/public", tags=["public"])


@router.get("/featured")
def featured(db: Session = Depends(get_db)):
    ids = featured_work_ids(db)
    return [{"work_id": i} for i in ids]


@router.get("/works")
def works(db: Session = Depends(get_db)):
    out = []
    for w in list_public_works(db):
        story = public_story(db, w)
        if story:
            out.append(story)
    return out


@router.get("/works/{work_id}/story")
def story(work_id: int, db: Session = Depends(get_db)):
    w = db.get(Work, work_id)
    if not w or w.confidential:
        raise HTTPException(404, "作品不存在或未公开发布")
    s = public_story(db, w)
    if not s:
        raise HTTPException(404, "该作品当前没有可公开展示的版本素材")
    return s


@router.get("/search")
def search(q: str = "", db: Session = Depends(get_db)):
    return search_works(db, q)


@router.get("/share/{work_id}")
def share_card(work_id: int, db: Session = Depends(get_db)):
    """Data for social share cards (og:* equivalents). Same guards as the site:
    confidential works and works without a display-licensed render 404."""
    w = db.get(Work, work_id)
    if not w or w.confidential:
        raise HTTPException(404, "无法生成分享卡")
    s = public_story(db, w)
    if not s or not s["thumbnail_asset_id"]:
        raise HTTPException(404, "缺少可公开展示的缩略图，无法生成分享卡")
    return {
        "card": "summary_large_image",
        "title": w.title,
        "description": (w.summary or w.title)[:120],
        "image_url": f"/api/public/files/{s['thumbnail_asset_id']}/bytes",
        "url": f"/work.html?id={w.id}",
    }


@router.get("/files/{asset_id}/bytes")
def file_bytes(asset_id: int, db: Session = Depends(get_db)):
    """Serve only assets with a live public DISPLAY grant. Download right is
    intentionally NOT honored here — the public site never hands out masters."""
    a = db.get(FileAsset, asset_id)
    if not a:
        raise HTTPException(404, "素材不存在")
    v = a.version
    if v.work.confidential or not has_right(db, a, Right.DISPLAY, PUBLIC):
        # do not reveal whether it exists
        raise HTTPException(404, "素材不存在或未公开展示")
    from ..services import storage

    data = storage.get(a.sha256)
    return Response(content=data, media_type=a.media_type,
                    headers={"Cache-Control": "no-store"})
