"""Approval snapshot + two-party acceptance lifecycle."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models import (
    Acceptance,
    AssetKind,
    Client,
    FileAsset,
    Project,
    VersionApproval,
    WorkVersion,
)
from .rights import asset_snapshot, assets_fingerprint, items_fingerprint


# -------------------------------------------------------------- version approval
def approve_version(db: Session, version: WorkVersion, client: Client,
                    note: str = "") -> VersionApproval:
    # Mark any earlier still-active approval of the same version as superseded.
    older = (
        db.query(VersionApproval)
        .filter(
            VersionApproval.version_id == version.id,
            VersionApproval.client_id == client.id,
            VersionApproval.superseded_at.is_(None),
        )
        .all()
    )
    now = datetime.now(timezone.utc)
    for o in older:
        o.superseded_at = now
    assets = sorted(version.assets, key=lambda a: a.id)
    ap = VersionApproval(
        version_id=version.id,
        client_id=client.id,
        version_seq=version.seq,
        label=version.label,
        asset_fingerprint=assets_fingerprint(assets),
        asset_snapshot=asset_snapshot(assets),
        note=note,
    )
    db.add(ap)
    db.flush()
    return ap


def approval_drift(db: Session, ap: VersionApproval) -> dict:
    """Has the version's current file set changed since approval?

    New files never overwrite the approval; we only REPORT the difference and
    keep the approved snapshot intact.
    """
    version = db.get(WorkVersion, ap.version_id)
    current = sorted(version.assets, key=lambda a: a.id) if version else []
    current_fp = assets_fingerprint(current)
    return {
        "changed": current_fp != ap.asset_fingerprint,
        "approved_fingerprint": ap.asset_fingerprint,
        "current_fingerprint": current_fp,
        "approved_at": ap.created_at.isoformat(),
    }


# ------------------------------------------------------------------ acceptance
def default_acceptance_items(db: Session, work_versions: list[WorkVersion]) -> list[dict]:
    """Default: the approved MASTER of each given version (or its master if no
    approval filter applied). Callers validate approval separately."""
    items: list[dict] = []
    for v in work_versions:
        masters = [a for a in v.assets if a.kind == AssetKind.MASTER]
        if not masters:
            raise ValueError(f"版本 {v.label} 没有成稿素材，无法发起验收")
        a = sorted(masters, key=lambda x: x.id)[-1]
        items.append({
            "version_id": v.id,
            "asset_id": a.id,
            "kind": a.kind.value,
            "sha256": a.sha256,
        })
    return items


def start_acceptance(db: Session, project: Project, client: Client,
                     items: list[dict]) -> Acceptance:
    if not items:
        raise ValueError("验收清单为空")
    # cancel open (incomplete) acceptances of this project to avoid ambiguity
    for old in (
        db.query(Acceptance)
        .filter(
            Acceptance.project_id == project.id,
            Acceptance.completed_at.is_(None),
            Acceptance.cancelled.is_(False),
        )
        .all()
    ):
        old.cancelled = True
    # integrity: referenced assets/versions must exist and checksums match
    for it in items:
        a = db.get(FileAsset, it["asset_id"])
        if a is None or a.version_id != it["version_id"] or a.sha256 != it["sha256"]:
            raise ValueError("验收条目与素材不一致（版本/文件指纹不匹配）")
    acc = Acceptance(
        project_id=project.id,
        client_id=client.id,
        items_json=__import__("json").dumps(items, ensure_ascii=False),
        items_fingerprint=items_fingerprint(items),
    )
    db.add(acc)
    db.flush()
    return acc


def same_items(acc: Acceptance, items: list[dict]) -> bool:
    return items_fingerprint(items) == acc.items_fingerprint


def confirm(db: Session, acc: Acceptance, who: str, items: list[dict]) -> Acceptance:
    if acc.cancelled:
        raise ValueError("该验收单已取消")
    if acc.completed_at:
        raise ValueError("该验收单已完成，如需调整请重新发起")
    if not same_items(acc, items):
        raise ValueError("确认的素材清单与验收单不一致：两方必须确认同一组版本/文件")
    now = datetime.now(timezone.utc)
    if who == "artist":
        acc.artist_confirmed_at = now
    elif who == "client":
        acc.client_confirmed_at = now
    else:
        raise ValueError("who must be artist|client")
    if acc.artist_confirmed_at and acc.client_confirmed_at:
        acc.completed_at = now
    db.flush()
    return acc
