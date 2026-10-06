"""Client capability-URL portal: approve exact versions, two-party acceptance,
download sealed packages. Every client action is scoped by access token and
checked against the client that owns the project."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Acceptance, Client, DeliveryPackage, Project, Work, WorkVersion
from ..schemas import AcceptanceConfirmIn, AcceptanceStartIn, ApproveIn, SealIn
from ..services import audit
from ..services.approvals import (
    approval_drift,
    approve_version,
    confirm,
    default_acceptance_items,
    start_acceptance,
)
from ..security import require_client
from ..services.delivery import can_download, render_zip, seal_package

router = APIRouter(prefix="/api/client", tags=["client"])


def _own_project(db: Session, client: Client, project_id: int) -> Project:
    p = db.get(Project, project_id)
    if not p or p.client_id != client.id:
        raise HTTPException(404, "项目不存在")
    return p


@router.get("/me")
def me(client: Client = Depends(require_client)):
    return {"id": client.id, "name": client.name}


@router.get("/projects")
def list_projects(client: Client = Depends(require_client),
                  db: Session = Depends(get_db)):
    out = []
    for p in db.query(Project).filter(Project.client_id == client.id).order_by(Project.id):
        out.append({"id": p.id, "title": p.title, "work_id": p.work_id,
                    "frozen": p.frozen, "freeze_mode": p.freeze_mode.value})
    return out


@router.get("/works/{work_id}")
def get_work_versions(work_id: int,
                      client: Client = Depends(require_client),
                      db: Session = Depends(get_db)):
    w = db.get(Work, work_id)
    if not w:
        raise HTTPException(404, "作品不存在")
    return {
        "work_id": w.id, "title": w.title,
        "versions": [
            {"id": v.id, "seq": v.seq, "label": v.label, "note": v.note,
             "asset_kinds": sorted({a.kind.value for a in v.assets})}
            for v in sorted(w.versions, key=lambda x: x.seq)
        ],
    }


@router.post("/approvals")
def approve(body: ApproveIn,
            client: Client = Depends(require_client),
            db: Session = Depends(get_db)):
    v = db.get(WorkVersion, body.version_id)
    if not v:
        raise HTTPException(404, "版本不存在")
    work = v.work
    owns = (
        db.query(Project)
        .filter(Project.client_id == client.id, Project.work_id == work.id)
        .first()
    )
    if not owns:
        raise HTTPException(403, "只能批准自己委托项目下的版本")
    ap = approve_version(db, v, client, body.note)
    audit.record(db, "version.approve", actor=f"client:{client.id}",
                 entity="version", entity_id=v.id,
                 detail=f"fingerprint={ap.asset_fingerprint[:16]}...")
    db.commit()
    return {
        "approval_id": ap.id,
        "asset_fingerprint": ap.asset_fingerprint,
        "asset_snapshot": ap.asset_snapshot,
        "drift": {"changed": False},
    }


@router.get("/approvals/{approval_id}/drift")
def get_drift(approval_id: int,
              client: Client = Depends(require_client),
              db: Session = Depends(get_db)):
    from ..models import VersionApproval

    ap = db.get(VersionApproval, approval_id)
    if not ap or ap.client_id != client.id:
        raise HTTPException(404, "批准记录不存在")
    return approval_drift(db, ap)


@router.get("/versions/{version_id}/approvals")
def list_version_approvals(version_id: int,
                           client: Client = Depends(require_client),
                           db: Session = Depends(get_db)):
    from ..models import VersionApproval

    v = db.get(WorkVersion, version_id)
    if not v:
        raise HTTPException(404, "版本不存在")
    owns = db.query(Project).filter(Project.client_id == client.id,
                                    Project.work_id == v.work_id).first()
    if not owns:
        raise HTTPException(403, "无权查看该版本的批准")
    out = []
    for ap in db.query(VersionApproval).filter(
            VersionApproval.version_id == version_id,
            VersionApproval.client_id == client.id).order_by(VersionApproval.id):
        out.append({"approval_id": ap.id, "created_at": ap.created_at.isoformat(),
                    "superseded": ap.superseded_at is not None,
                    **approval_drift(db, ap)})
    return out


# ----------------------------------------------------------------- acceptance
@router.post("/acceptances")
def start(body: AcceptanceStartIn,
          client: Client = Depends(require_client),
          db: Session = Depends(get_db)):
    p = _own_project(db, client, body.project_id)
    if not p.work_id:
        raise HTTPException(422, "项目未关联作品")
    if body.items is not None:
        items = body.items
    else:
        versions = (
            db.query(WorkVersion)
            .filter(WorkVersion.work_id == p.work_id,
                    WorkVersion.id.in_(body.version_ids))
            .all()
        )
        if len(versions) != len(set(body.version_ids)):
            raise HTTPException(422, "版本不存在于本项目作品")
        try:
            items = default_acceptance_items(db, versions)
        except ValueError as e:
            raise HTTPException(422, str(e))
    acc = start_acceptance(db, p, client, items)
    audit.record(db, "acceptance.start", actor=f"client:{client.id}",
                 entity="acceptance", entity_id=acc.id)
    db.commit()
    return {"acceptance_id": acc.id, "items": items,
            "fingerprint": acc.items_fingerprint}


@router.post("/acceptances/artist-confirm")
def artist_confirm(body: AcceptanceConfirmIn,
                   client: Client = Depends(require_client),
                   db: Session = Depends(get_db)):
    """In the demo the studio side acts through the same portal but is recorded
    as the artist party; real deployment would require artist auth."""
    acc = db.get(Acceptance, body.acceptance_id)
    if not acc or acc.client_id != client.id:
        raise HTTPException(404, "验收单不存在")
    try:
        confirm(db, acc, "artist", body.items)
    except ValueError as e:
        raise HTTPException(422, str(e))
    audit.record(db, "acceptance.artist_confirm", actor="artist",
                 entity="acceptance", entity_id=acc.id)
    db.commit()
    return _acceptance_out(acc)


@router.post("/acceptances/client-confirm")
def client_confirm(body: AcceptanceConfirmIn,
                   client: Client = Depends(require_client),
                   db: Session = Depends(get_db)):
    acc = db.get(Acceptance, body.acceptance_id)
    if not acc or acc.client_id != client.id:
        raise HTTPException(404, "验收单不存在")
    try:
        confirm(db, acc, "client", body.items)
    except ValueError as e:
        raise HTTPException(422, str(e))
    audit.record(db, "acceptance.client_confirm", actor=f"client:{client.id}",
                 entity="acceptance", entity_id=acc.id)
    db.commit()
    return _acceptance_out(acc)


@router.get("/acceptances")
def list_acceptances(project_id: int,
                     client: Client = Depends(require_client),
                     db: Session = Depends(get_db)):
    _own_project(db, client, project_id)
    rows = db.query(Acceptance).filter(Acceptance.project_id == project_id).order_by(Acceptance.id)
    return [_acceptance_out(a) for a in rows]


@router.post("/packages/seal")
def seal(body: SealIn,
         client: Client = Depends(require_client),
         db: Session = Depends(get_db)):
    acc = db.get(Acceptance, body.acceptance_id)
    if not acc or acc.client_id != client.id:
        raise HTTPException(404, "验收单不存在")
    p = db.get(Project, acc.project_id)
    try:
        pkg = seal_package(db, acc, p, client)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    audit.record(db, "package.seal", actor=f"client:{client.id}",
                 entity="delivery_package", entity_id=pkg.id)
    db.commit()
    return {"package_id": pkg.id, "archive_sha256": pkg.archive_sha256,
            "manifest": __import__("json").loads(pkg.manifest_json)}


@router.get("/packages")
def list_packages(project_id: int,
                  client: Client = Depends(require_client),
                  db: Session = Depends(get_db)):
    _own_project(db, client, project_id)
    pkgs = db.query(DeliveryPackage).join(Acceptance).filter(
        Acceptance.project_id == project_id, Acceptance.client_id == client.id
    )
    return [{"id": x.id, "sealed": x.sealed,
             "sealed_at": x.sealed_at.isoformat() if x.sealed_at else None,
             "downloadable": can_download(db, x, client)[0]}
            for x in pkgs]


@router.get("/packages/{package_id}/download")
def download_package(package_id: int,
                     client: Client = Depends(require_client),
                     db: Session = Depends(get_db)):
    from fastapi.responses import Response

    pkg = db.get(DeliveryPackage, package_id)
    if not pkg:
        raise HTTPException(404, "交付包不存在")
    acc = db.get(Acceptance, pkg.acceptance_id)
    if acc.client_id != client.id:
        raise HTTPException(403, "无权访问")
    ok, msg = can_download(db, pkg, client)
    if not ok:
        raise HTTPException(403, msg)
    data = render_zip(db, pkg)
    audit.record(db, "package.download", actor=f"client:{client.id}",
                 entity="delivery_package", entity_id=pkg.id)
    db.commit()
    return Response(
        content=data, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="delivery-{pkg.id}.zip"'},
    )


def _acceptance_out(acc: Acceptance) -> dict:
    return {
        "id": acc.id, "project_id": acc.project_id,
        "items": __import__("json").loads(acc.items_json),
        "fingerprint": acc.items_fingerprint,
        "artist_confirmed": acc.artist_confirmed_at is not None,
        "client_confirmed": acc.client_confirmed_at is not None,
        "completed": acc.completed_at is not None,
        "cancelled": acc.cancelled,
    }
