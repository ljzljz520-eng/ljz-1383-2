"""Public catalog: featured homepage, search and share-card data.

Every read path enforces, in real time:
* work.confidential == False  (保密项目永不进首页/搜索/分享卡)
* the concrete rendered asset has a live public DISPLAY grant
There is no read path that trusts a single "public" boolean.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models import AssetKind, FileAsset, Right, SearchEntry, Work, WorkVersion
from .rights import PUBLIC, assets_with_right, grant_map_for


def _versions_of(db: Session, work: Work) -> list[WorkVersion]:
    return (
        db.query(WorkVersion)
        .filter(WorkVersion.work_id == work.id)
        .order_by(WorkVersion.seq)
        .all()
    )


def public_version_view(db: Session, v: WorkVersion) -> dict | None:
    """Return only the asset types of ONE version that currently have a public
    display grant. Missing kinds are simply omitted (so a master with display
    right but a thumbnail/process without it is rendered partially, never by
    leaking the unlicensed files)."""
    visible = assets_with_right(db, list(v.assets), Right.DISPLAY, PUBLIC)
    if not visible:
        return None
    groups: dict[str, list[dict]] = {}
    for a in visible:
        groups.setdefault(a.kind.value, []).append(
            {"asset_id": a.id, "filename": a.filename, "sha256": a.sha256[:12]}
        )
    return {
        "version_id": v.id,
        "seq": v.seq,
        "label": v.label,
        "note": v.note,
        "assets": groups,
    }


def public_story(db: Session, work: Work) -> dict | None:
    """The 草图→过程图→成稿 journey, exposing only licensed assets per version."""
    if work.confidential:
        return None
    versions = [pv for v in _versions_of(db, work)
                if (pv := public_version_view(db, v))]
    if not versions:
        return None
    # choose a display-licensed thumbnail across all versions for the card
    all_assets = [a for v in _versions_of(db, work) for a in v.assets]
    thumbs = assets_with_right(db, all_assets, Right.DISPLAY, PUBLIC)
    thumbs = [a for a in thumbs if a.kind == AssetKind.THUMBNAIL]
    return {
        "work_id": work.id,
        "title": work.title,
        "summary": work.summary,
        "thumbnail_asset_id": thumbs[0].id if thumbs else None,
        "versions": versions,
    }


def list_public_works(db: Session) -> list[Work]:
    return db.query(Work).filter(Work.confidential.is_(False)).order_by(Work.id).all()


def featured_work_ids(db: Session) -> list[int]:
    """Featured curation is only EFFECTIVE when each guard passes. A
    confidential work, even if featured flag was set, is excluded; and we
    require at least one display-licensed render."""
    works = (
        db.query(Work)
        .filter(Work.featured.is_(True), Work.confidential.is_(False))
        .all()
    )
    ids = []
    for w in works:
        if public_story(db, w):
            ids.append(w.id)
    return ids


# ------------------------------------------------------------- search index ops
def reindex(db: Session) -> int:
    """Rebuild SearchEntry from current confidentiality + display grants.

    The read path (search_works) still re-validates live grants, so this index
    is a performance/denormalisation layer, never the authorization boundary.
    """
    db.query(SearchEntry).delete()
    n = 0
    for w in list_public_works(db):
        story = public_story(db, w)
        if not story:
            continue
        db.add(SearchEntry(
            work_id=w.id,
            title=w.title,
            summary=w.summary,
            thumb_asset_id=story["thumbnail_asset_id"],
            updated_at=datetime.now(timezone.utc),
        ))
        n += 1
    db.flush()
    return n


def search_works(db: Session, q: str, limit: int = 20) -> list[dict]:
    entries = (
        db.query(SearchEntry)
        .filter(or_(SearchEntry.title.contains(q), SearchEntry.summary.contains(q)))
        .limit(limit)
        .all()
    )
    out = []
    for e in entries:
        work = db.get(Work, e.work_id)
        if work is None or work.confidential:
            continue
        story = public_story(db, work)
        if story:  # live re-check: revoked licenses / confidentiality drop out
            out.append({
                "work_id": work.id,
                "title": work.title,
                "summary": work.summary,
                "thumbnail_asset_id": story["thumbnail_asset_id"],
            })
    return out
