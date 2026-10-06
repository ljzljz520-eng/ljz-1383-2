"""Permission evaluation.

There is deliberately no ``work.is_public`` switch. A right exists only when a
matching, non-revoked LicenseGrant row says so, and decisions are made per
asset (master/process/thumbnail) and per right (display/download/sublicense).

Precedence
----------
1. Explicit grant in the exact scope ("public" or "client:<id>").
   granted=False is a hard DENY; revoked rows count as if absent.
2. For client scopes only, fall back to the "public" grant.
3. Otherwise the right does NOT exist (deny by default).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from sqlalchemy.orm import Session

from ..models import AssetKind, FileAsset, LicenseGrant, Right

PUBLIC = "public"


def client_scope(client_id: int) -> str:
    return f"client:{client_id}"


def _decide(rows: Iterable[LicenseGrant], right: Right, scope: str) -> bool:
    rows = [g for g in rows if g.right == right and g.revoked_at is None]
    exact = [g for g in rows if g.scope == scope]
    if exact:
        # latest explicit decision wins (unique constraint keeps this to 1)
        return any(g.granted for g in exact)
    if scope != PUBLIC:
        pub = [g for g in rows if g.scope == PUBLIC]
        if pub:
            # an explicit public denial blocks unless exact-scope row existed
            return any(g.granted for g in pub)
    return False  # default: no license


def grant_map_for(db: Session, asset_ids: list[int]) -> dict[int, list[LicenseGrant]]:
    if not asset_ids:
        return {}
    rows = db.query(LicenseGrant).filter(LicenseGrant.asset_id.in_(asset_ids)).all()
    out: dict[int, list[LicenseGrant]] = {aid: [] for aid in asset_ids}
    for g in rows:
        out.setdefault(g.asset_id, []).append(g)
    return out


def has_right(db: Session, asset: FileAsset, right: Right, scope: str = PUBLIC) -> bool:
    return _decide(grant_map_for(db, [asset.id]).get(asset.id, []), right, scope)


def assets_with_right(
    db: Session, assets: list[FileAsset], right: Right, scope: str = PUBLIC
) -> list[FileAsset]:
    gm = grant_map_for(db, [a.id for a in assets])
    return [a for a in assets if _decide(gm.get(a.id, []), right, scope)]


# --------------------------------------------------------------- fingerprints
def items_fingerprint(items: list[dict]) -> str:
    """Stable hash over an acceptance item set."""
    payload = sorted(
        f"{i['version_id']}:{i['asset_id']}:{i['kind']}:{i['sha256']}" for i in items
    )
    return hashlib.sha256("\n".join(payload).encode()).hexdigest()


def assets_fingerprint(assets: list[FileAsset]) -> str:
    payload = sorted(f"{a.id}:{a.kind.value}:{a.sha256}" for a in assets)
    return hashlib.sha256("\n".join(payload).encode()).hexdigest()


def asset_snapshot(assets: list[FileAsset]) -> str:
    return json.dumps(
        sorted(
            (
                {"id": a.id, "kind": a.kind.value, "sha256": a.sha256,
                 "filename": a.filename}
                for a in assets
            ),
            key=lambda x: x["id"],
        ),
        ensure_ascii=False,
    )


def public_kind_rights() -> dict[str, bool]:
    """Shape used by serializers; rights evaluated per call."""
    return {r.value: False for r in Right}
