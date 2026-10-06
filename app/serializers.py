"""Read-model serializers that evaluate RIGHTS per asset (never a single flag)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import FileAsset, Right, Work, WorkVersion
from .services.rights import PUBLIC, _decide, grant_map_for


def serialize_asset(db: Session, a: FileAsset) -> dict:
    gm = grant_map_for(db, [a.id]).get(a.id, [])
    scopes: dict[str, dict[str, bool]] = {}
    for g in gm:
        scopes.setdefault(g.scope, {})[g.right.value] = {
            "granted": g.granted,
            "revoked": g.revoked_at is not None,
            "grant_id": g.id,
        }
    # evaluated convenience view for public rights
    public_rights = {
        r.value: _decide(gm, r, PUBLIC) for r in Right
    }
    return {
        "id": a.id,
        "kind": a.kind.value,
        "filename": a.filename,
        "media_type": a.media_type,
        "sha256": a.sha256,
        "size_bytes": a.size_bytes,
        "source_asset_id": a.source_asset_id,
        "public_rights": public_rights,
        "scope_decisions": scopes,
    }


def serialize_version(db: Session, v: WorkVersion, include_assets: bool = True) -> dict:
    out = {
        "id": v.id,
        "seq": v.seq,
        "label": v.label,
        "note": v.note,
        "created_at": v.created_at.isoformat(),
    }
    if include_assets:
        out["assets"] = [serialize_asset(db, a) for a in sorted(v.assets, key=lambda x: x.id)]
    return out


def serialize_work(db: Session, w: Work) -> dict:
    versions = sorted(w.versions, key=lambda v: v.seq)
    return {
        "id": w.id,
        "title": w.title,
        "summary": w.summary,
        "artist_id": w.artist_id,
        "confidential": w.confidential,
        "featured": w.featured,
        "versions": [serialize_version(db, v) for v in versions],
    }
