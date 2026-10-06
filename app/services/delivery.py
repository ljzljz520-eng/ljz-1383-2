"""Delivery package sealing/download with frozen, approved manifest."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models import (
    Acceptance,
    AssetKind,
    Client,
    DeliveryPackage,
    FileAsset,
    LicenseGrant,
    Project,
    Right,
    VersionApproval,
    WorkVersion,
)
from . import storage
from .rights import PUBLIC, client_scope, grant_map_for


def _license_snapshot(db: Session, asset: FileAsset, scope: str) -> dict:
    gm = grant_map_for(db, [asset.id]).get(asset.id, [])
    out = {}
    for right in Right:
        rows = [g for g in gm if g.right == right and g.revoked_at is None]
        exact = [g for g in rows if g.scope == scope]
        if exact:
            out[right.value] = {"granted": exact[-1].granted, "scope": scope,
                                "grant_id": exact[-1].id}
        else:
            pub = [g for g in rows if g.scope == PUBLIC]
            out[right.value] = (
                {"granted": pub[-1].granted, "scope": PUBLIC, "grant_id": pub[-1].id}
                if pub else {"granted": False, "scope": None, "grant_id": None}
            )
    return out


def build_manifest(db: Session, acc: Acceptance, client: Client) -> list[dict]:
    """Manifest = the EXACT items accepted by both parties + license snapshot.

    Even if the author uploads a newer master afterward, the manifest still
    points at the sha256 that was accepted. Derivatives arriving late are not
    present here because they were not part of the accepted item set.
    """
    items = json.loads(acc.items_json)
    scope = client_scope(client.id)
    manifest = []
    for it in items:
        a = db.get(FileAsset, it["asset_id"])
        v = db.get(WorkVersion, it["version_id"])
        if a is None or a.sha256 != it["sha256"]:
            raise ValueError("验收素材发生不一致，无法生成交付包")
        manifest.append({
            "asset_id": a.id,
            "version_id": v.id,
            "version_seq": v.seq,
            "version_label": v.label,
            "kind": a.kind.value,
            "filename": a.filename,
            "sha256": a.sha256,
            "size_bytes": a.size_bytes,
            "license_at_delivery": _license_snapshot(db, a, scope),
        })
    return manifest


def seal_package(db: Session, acc: Acceptance, project: Project,
                 client: Client) -> DeliveryPackage:
    if not acc.completed_at:
        raise ValueError("两方尚未同时确认，不能生成交付包")
    manifest = build_manifest(db, acc, client)
    # Every delivered file must carry a live download grant at sealing time.
    blocked = []
    scope = client_scope(client.id)
    for m in manifest:
        snap = m["license_at_delivery"][Right.DOWNLOAD.value]
        if not snap["granted"]:
            blocked.append(m["filename"])
    if blocked:
        raise PermissionError("以下素材当前没有下载授权，已被排除/拒绝打包："
                              + "、".join(blocked))
    pkg = DeliveryPackage(
        acceptance_id=acc.id,
        project_id=project.id,
        manifest_json=json.dumps(manifest, ensure_ascii=False, indent=2),
        sealed=True,
        sealed_at=datetime.now(timezone.utc),
    )
    archive = render_zip(db, pkg)
    pkg.archive_sha256 = hashlib.sha256(archive).hexdigest()
    db.add(pkg)
    db.flush()
    return pkg


def render_zip(db: Session, pkg: DeliveryPackage) -> bytes:
    manifest = json.loads(pkg.manifest_json)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("MANIFEST.json", json.dumps(
            {"package_id": pkg.id, "sealed_at": pkg.sealed_at.isoformat()
             if pkg.sealed_at else None, "items": manifest},
            ensure_ascii=False, indent=2))
        for m in manifest:
            data = storage.get(m["sha256"])
            # guard against on-disk tampering
            if hashlib.sha256(data).hexdigest() != m["sha256"]:
                raise ValueError(f"素材 {m['filename']} 校验失败")
            zf.writestr(f"files/{m['sha256'][:12]}-{m['filename']}", data)
    return buf.getvalue()


def can_download(db: Session, pkg: DeliveryPackage, client: Client) -> tuple[bool, str]:
    """Post-seal gate: a later license withdrawal blocks fresh downloads even
    though the historical manifest remains intact for traceability."""
    acc = db.get(Acceptance, pkg.acceptance_id)
    if acc.client_id != client.id:
        return False, "无权访问该交付包"
    manifest = json.loads(pkg.manifest_json)
    scope = client_scope(client.id)
    for m in manifest:
        a = db.get(FileAsset, m["asset_id"])
        gm = grant_map_for(db, [a.id]).get(a.id, [])
        rows = [g for g in gm if g.right == Right.DOWNLOAD and g.revoked_at is None]
        ok = any(g.scope == scope and g.granted for g in rows) or any(
            g.scope == PUBLIC and g.granted for g in rows
        )
        if not ok:
            return False, f"素材 {m['filename']} 的下载授权已撤回，下载已暂停"
    return True, ""
