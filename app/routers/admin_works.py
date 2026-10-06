import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    AssetKind,
    Client,
    DerivationTask,
    FileAsset,
    LicenseGrant,
    Right,
    TaskStatus,
    Work,
    WorkVersion,
)
from ..schemas import DeriveIn, GrantIn, RevokeIn, VersionIn, WorkIn, WorkPatch
from ..security import require_admin
from ..serializers import serialize_asset, serialize_work
from ..services import audit, storage
from ..services.catalog import reindex
from ..services.images import enqueue, process_due, render_placeholder

router = APIRouter(prefix="/api/admin", tags=["admin-works"],
                   dependencies=[Depends(require_admin)])

VALID_SCOPES_PREFIX = ("public", "client:")
VALID_RIGHTS = {r.value for r in Right}
VALID_KINDS = {k.value for k in AssetKind}


# ------------------------------------------------------------- works / versions
@router.post("/works")
def create_work(body: WorkIn, db: Session = Depends(get_db)):
    w = Work(artist_id=body.artist_id, title=body.title, summary=body.summary,
             confidential=body.confidential, featured=body.featured)
    db.add(w)
    db.commit()
    db.refresh(w)
    audit.record(db, "work.create", entity="work", entity_id=w.id, detail=w.title)
    db.commit()
    return serialize_work(db, w)


@router.get("/works")
def list_works(db: Session = Depends(get_db)):
    return [serialize_work(db, w) for w in db.query(Work).order_by(Work.id)]


@router.get("/works/{work_id}")
def get_work(work_id: int, db: Session = Depends(get_db)):
    w = db.get(Work, work_id)
    if not w:
        raise HTTPException(404, "作品不存在")
    return serialize_work(db, w)


@router.patch("/works/{work_id}")
def patch_work(work_id: int, body: WorkPatch, db: Session = Depends(get_db)):
    w = db.get(Work, work_id)
    if not w:
        raise HTTPException(404, "作品不存在")
    for f in ("title", "summary", "confidential", "featured"):
        val = getattr(body, f)
        if val is not None:
            setattr(w, f, val)
    reindex(db)
    db.commit()
    audit.record(db, "work.update", entity="work", entity_id=w.id,
                 detail=f"confidential={w.confidential} featured={w.featured}")
    db.commit()
    return serialize_work(db, w)


def _next_seq(db: Session, work_id: int) -> int:
    last = (
        db.query(WorkVersion).filter(WorkVersion.work_id == work_id)
        .order_by(WorkVersion.seq.desc()).first()
    )
    return 1 if last is None else last.seq + 1


@router.post("/works/{work_id}/versions")
def create_version(work_id: int, body: VersionIn, db: Session = Depends(get_db)):
    w = db.get(Work, work_id)
    if not w:
        raise HTTPException(404, "作品不存在")
    v = WorkVersion(work_id=work_id, label=body.label, note=body.note,
                    seq=_next_seq(db, work_id))
    db.add(v)
    db.flush()
    # demo seed: auto-generate an immutable placeholder master
    color = body.placeholder_color or (120, 120 + (v.seq * 25) % 120, 200)
    data = render_placeholder(f"{w.title} - {body.label}", color=color)
    digest, size = storage.put(data)
    master = FileAsset(version_id=v.id, kind=AssetKind.MASTER,
                       filename=f"master-v{v.seq}.png", media_type="image/png",
                       sha256=digest, size_bytes=size)
    db.add(master)
    db.flush()
    audit.record(db, "version.create", entity="version", entity_id=v.id,
                 detail=f"work={work_id} seq={v.seq} new files get NO license by default")
    db.commit()
    return serialize_work(db, w)


@router.post("/works/{work_id}/versions/{version_id}/upload")
async def upload_asset(
    work_id: int,
    version_id: int,
    kind: str = Form(...),
    file: UploadFile = File(...),
    source_asset_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    v = db.get(WorkVersion, version_id)
    if not v or v.work_id != work_id:
        raise HTTPException(404, "版本不存在")
    if kind not in VALID_KINDS:
        raise HTTPException(422, "kind 必须是 master/process/thumbnail")
    data = await file.read()
    digest, size = storage.put(data)
    a = FileAsset(version_id=v.id, kind=AssetKind(kind), filename=file.filename or "file",
                  media_type=file.content_type or "application/octet-stream",
                  sha256=digest, size_bytes=size, source_asset_id=source_asset_id)
    db.add(a)
    db.flush()
    audit.record(db, "asset.upload", entity="asset", entity_id=a.id,
                 detail=f"version={v.id} kind={kind}; immutable, unlicensed by default")
    db.commit()
    return serialize_asset(db, a)


# ------------------------------------------------------------------- licenses
def _validate_scope(scope: str, db: Session) -> str:
    if scope == "public":
        return scope
    if scope.startswith("client:"):
        cid = scope.split(":", 1)[1]
        if not cid.isdigit() or db.get(Client, int(cid)) is None:
            raise HTTPException(422, f"未知客户范围 {scope}")
        return scope
    raise HTTPException(422, "scope 必须是 public 或 client:<id>")


@router.post("/grants")
def set_grant(body: GrantIn, db: Session = Depends(get_db)):
    if body.right not in VALID_RIGHTS:
        raise HTTPException(422, "right 必须是 display/download/sublicense")
    scope = _validate_scope(body.scope, db)
    a = db.get(FileAsset, body.asset_id)
    if not a:
        raise HTTPException(404, "素材不存在")
    g = (
        db.query(LicenseGrant)
        .filter(LicenseGrant.asset_id == a.id, LicenseGrant.scope == scope,
                LicenseGrant.right == Right(body.right))
        .one_or_none()
    )
    created = g is None
    if g is None:
        g = LicenseGrant(asset_id=a.id, scope=scope, right=Right(body.right))
        db.add(g)
    g.granted = body.granted
    g.note = body.note
    # re-issuing after a previous revocation: explicit new decision
    if not g.granted:
        pass
    db.flush()
    reindex(db)
    audit.record(db, "grant.set" if created else "grant.update", entity="asset",
                 entity_id=a.id,
                 detail=f"{scope}/{body.right} granted={body.granted} (per-asset, not a global switch)")
    db.commit()
    return serialize_asset(db, a)


@router.post("/grants/{grant_id}/revoke")
def revoke_grant(grant_id: int, db: Session = Depends(get_db)):
    from datetime import datetime, timezone

    g = db.get(LicenseGrant, grant_id)
    if not g:
        raise HTTPException(404, "授权记录不存在")
    if g.revoked_at is None:
        g.revoked_at = datetime.now(timezone.utc)
    reindex(db)
    audit.record(db, "grant.revoke", entity="asset", entity_id=g.asset_id,
                 detail=f"grant {grant_id} {g.scope}/{g.right.value} 撤回立即生效于公开读取与下载")
    db.commit()
    return {"ok": True, "revoked_at": g.revoked_at.isoformat()}


@router.get("/grants")
def list_grants(db: Session = Depends(get_db)):
    out = []
    for g in db.query(LicenseGrant).order_by(LicenseGrant.id):
        out.append({
            "id": g.id, "asset_id": g.asset_id, "scope": g.scope,
            "right": g.right.value, "granted": g.granted,
            "revoked": g.revoked_at is not None,
        })
    return out


# ------------------------------------------------------------ derivation queue
@router.post("/derivations")
def create_derivation(body: DeriveIn, db: Session = Depends(get_db)):
    src = db.get(FileAsset, body.source_asset_id)
    if not src:
        raise HTTPException(404, "源素材不存在")
    if body.kind not in ("thumbnail", "process"):
        raise HTTPException(422, "kind 必须是 thumbnail/process")
    target_version_id = body.target_version_id or src.version_id
    tv = db.get(WorkVersion, target_version_id)
    if not tv:
        raise HTTPException(404, "目标版本不存在")
    task = enqueue(db, src, target_version_id, AssetKind(body.kind))
    audit.record(db, "derivation.enqueue", entity="derivation_task", entity_id=task.id,
                 detail=f"source={src.id} -> {body.kind}; output gets no automatic license")
    db.commit()
    return {"task_id": task.id, "status": task.status.value}


@router.post("/derivations/run")
def run_derivations(limit: int = 10, db: Session = Depends(get_db)):
    """Process queued derivation jobs. Tasks created late (after an approval or
    delivery) produce standalone assets that still need explicit licensing and
    never alter sealed approvals/packages."""
    outputs = process_due(db, limit=limit)
    reindex(db)
    audit.record(db, "derivation.run", detail=f"{len(outputs)} tasks processed")
    db.commit()
    return {
        "processed": [
            {"asset_id": a.id, "kind": a.kind.value, "source_asset_id": a.source_asset_id,
             "public_rights": serialize_asset(db, a)["public_rights"]}
            for a in outputs
        ]
    }


@router.get("/derivations")
def list_derivations(db: Session = Depends(get_db)):
    return [
        {"id": t.id, "source_asset_id": t.source_asset_id,
         "target_version_id": t.target_version_id, "kind": t.kind.value,
         "status": t.status.value, "output_asset_id": t.output_asset_id,
         "created_at": t.created_at.isoformat(),
         "finished_at": t.finished_at.isoformat() if t.finished_at else None,
         "detail": t.detail}
        for t in db.query(DerivationTask).order_by(DerivationTask.id)
    ]
