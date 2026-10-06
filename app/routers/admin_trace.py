"""Admin traceability: approval/delivery dependency graph, audit timeline,
submitted commissions."""
import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    Acceptance,
    AuditEvent,
    CommissionDraft,
    DeliveryPackage,
    VersionApproval,
)
from ..security import require_admin
from ..services.approvals import approval_drift

router = APIRouter(prefix="/api/admin", tags=["admin-trace"],
                   dependencies=[Depends(require_admin)])


@router.get("/trace/works/{version_id}/approvals")
def version_approvals(version_id: int, db: Session = Depends(get_db)):
    out = []
    for ap in db.query(VersionApproval).filter(
        VersionApproval.version_id == version_id).order_by(VersionApproval.id):
        out.append({
            "id": ap.id, "client_id": ap.client_id, "version_seq": ap.version_seq,
            "label": ap.label, "note": ap.note,
            "approved_at": ap.created_at.isoformat(),
            "superseded_at": ap.superseded_at.isoformat() if ap.superseded_at else None,
            "fingerprint": ap.asset_fingerprint,
            "snapshot": json.loads(ap.asset_snapshot),
            "drift": approval_drift(db, ap),  # latest files vs approved object
        })
    return out


@router.get("/trace/projects/{project_id}/delivery")
def project_delivery_trace(project_id: int, db: Session = Depends(get_db)):
    """Dependency chain: approval -> acceptance items -> sealed package items,
    with sha256 links so one can verify the package contains the actually
    approved assets and detect late substitutions/derivatives."""
    accs = db.query(Acceptance).filter(Acceptance.project_id == project_id).order_by(Acceptance.id)
    result = []
    for acc in accs:
        items = json.loads(acc.items_json)
        # which items had a client approval at acceptance time (by asset sha)
        related_pkgs = db.query(DeliveryPackage).filter(
            DeliveryPackage.acceptance_id == acc.id).all()
        manifests = []
        for p in related_pkgs:
            manifests.append({
                "package_id": p.id, "sealed": p.sealed,
                "sealed_at": p.sealed_at.isoformat() if p.sealed_at else None,
                "items": json.loads(p.manifest_json),
            })
        approved_shas: dict[str, list[int]] = {}
        for ap in db.query(VersionApproval).filter(
            VersionApproval.client_id == acc.client_id,
            VersionApproval.version_id.in_({i["version_id"] for i in items}),
        ):
            for snap in json.loads(ap.asset_snapshot):
                approved_shas.setdefault(snap["sha256"], []).append(ap.id)
        line_items = []
        for it in items:
            line_items.append({
                **it,
                "covered_by_approvals": approved_shas.get(it["sha256"], []),
                "in_sealed_packages": [
                    m["package_id"] for m in manifests
                    if any(x["sha256"] == it["sha256"] for x in m["items"])],
            })
        result.append({
            "acceptance_id": acc.id,
            "completed": acc.completed_at is not None,
            "cancelled": acc.cancelled,
            "artist_confirmed": acc.artist_confirmed_at is not None,
            "client_confirmed": acc.client_confirmed_at is not None,
            "items": line_items,
            "packages": manifests,
        })
    return result


@router.get("/audit")
def audit_log(limit: int = 100, db: Session = Depends(get_db)):
    rows = db.query(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit).all()
    return [{
        "id": e.id, "actor": e.actor, "action": e.action,
        "entity": e.entity, "entity_id": e.entity_id, "detail": e.detail,
        "at": e.created_at.isoformat(),
    } for e in rows]


@router.get("/commissions")
def list_commissions(submitted: bool = True, db: Session = Depends(get_db)):
    q = db.query(CommissionDraft).filter(CommissionDraft.submitted.is_(submitted))
    # expose only the submitted ones' content; drafts of other browsers are
    # never listed (submitted=False returns count only)
    if not submitted:
        return {"open_draft_count": q.count()}
    return [{
        "id": d.id, "intended_use": d.intended_use,
        "delivery_scope": d.delivery_scope, "contact": d.contact,
        "budget_cents": d.budget_cents, "submitted_at": d.updated_at.isoformat(),
    } for d in q.order_by(CommissionDraft.id.desc())]
