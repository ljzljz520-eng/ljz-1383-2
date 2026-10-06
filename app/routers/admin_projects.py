from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    ChangeOrder,
    FreezeMode,
    Project,
    RevisionRequest,
    ScheduleSlot,
    Stage,
    StageStatus,
)
from ..schemas import (
    AcceptChangeIn,
    BookSlotIn,
    ChangeOrderIn,
    FreezeIn,
    ProjectIn,
    RevisionIn,
    SlotIn,
    StageIn,
)
from ..security import require_admin
from ..services import audit
from ..services import revisions as rev_service
from ..services.schedule import book_slot, expire_holds, hold_slot

router = APIRouter(prefix="/api/admin", tags=["admin-projects"],
                   dependencies=[Depends(require_admin)])


def _stage_out(s: Stage) -> dict:
    return {
        "id": s.id, "name": s.name, "seq": s.seq, "status": s.status.value,
        "rounds_included": s.edit_rounds_included, "rounds_used": s.rounds_used,
        "stage_due": s.stage_due.isoformat() if s.stage_due else None,
        "frozen_at": s.frozen_at.isoformat() if s.frozen_at else None,
    }


def _project_out(db: Session, p: Project) -> dict:
    return {
        "id": p.id, "title": p.title, "client_id": p.client_id, "work_id": p.work_id,
        "freeze_mode": p.freeze_mode.value, "frozen": p.frozen,
        "frozen_at": p.frozen_at.isoformat() if p.frozen_at else None,
        "price_cents": p.price_cents,
        "edit_rounds_included": p.edit_rounds_included,
        "rounds_used": p.rounds_used,
        "due_date": p.due_date.isoformat() if p.due_date else None,
        "stages": [_stage_out(s) for s in sorted(p.stages, key=lambda x: x.seq)],
    }


# ----------------------------------------------------------- project lifecycle
@router.post("/projects")
def create_project(body: ProjectIn, db: Session = Depends(get_db)):
    try:
        mode = FreezeMode(body.freeze_mode)
    except ValueError:
        raise HTTPException(422, "freeze_mode 必须是 package|stage")
    p = Project(
        artist_id=body.artist_id, client_id=body.client_id, work_id=body.work_id,
        title=body.title, freeze_mode=mode, price_cents=body.price_cents,
        edit_rounds_included=body.edit_rounds_included, due_date=body.due_date,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    audit.record(db, "project.create", entity="project", entity_id=p.id,
                 detail=f"{p.title} mode={mode.value}")
    db.commit()
    return _project_out(db, p)


@router.get("/projects")
def list_projects(db: Session = Depends(get_db)):
    expire_holds(db)
    db.commit()
    return [_project_out(db, p) for p in db.query(Project).order_by(Project.id)]


@router.get("/projects/{project_id}")
def get_project(project_id: int, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    return _project_out(db, p)


@router.post("/projects/{project_id}/stages")
def add_stage(project_id: int, body: StageIn, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    seq = len(p.stages) + 1
    s = Stage(project_id=p.id, name=body.name, seq=seq,
              edit_rounds_included=body.edit_rounds_included, stage_due=body.stage_due)
    db.add(s)
    db.commit()
    return _stage_out(s)


@router.post("/projects/{project_id}/freeze")
def freeze_project(project_id: int, body: FreezeIn, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    if p.freeze_mode is not FreezeMode.PACKAGE:
        raise HTTPException(422, "该项目为逐阶段冻结模式；请冻结具体阶段（或新建整包模式项目）")
    rev_service.freeze_package(db, p, body.reason)
    audit.record(db, "project.freeze_package", entity="project", entity_id=p.id,
                 detail=body.reason)
    db.commit()
    return _project_out(db, p)


@router.post("/projects/{project_id}/stages/{stage_id}/freeze")
def freeze_one_stage(project_id: int, stage_id: int, body: FreezeIn,
                     db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    s = db.get(Stage, stage_id)
    if not p or not s or s.project_id != p.id:
        raise HTTPException(404, "项目/阶段不存在")
    try:
        rev_service.freeze_stage(db, p, s, body.reason)
    except ValueError as e:
        raise HTTPException(422, str(e))
    audit.record(db, "stage.freeze", entity="stage", entity_id=s.id, detail=body.reason)
    db.commit()
    return _stage_out(s)


# ------------------------------------------------------------- revisions
@router.get("/projects/{project_id}/revision-check")
def revision_check(project_id: int, stage_id: int | None = None,
                   db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    s = db.get(Stage, stage_id) if stage_id else None
    return rev_service.evaluate(db, p, s)


@router.post("/projects/{project_id}/revisions")
def request_revision(project_id: int, body: RevisionIn, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    s = db.get(Stage, body.stage_id) if body.stage_id else None
    req = rev_service.request_revision(
        db, p, body.detail, stage=s, version_id=body.version_id,
        requested_by=body.requested_by,
    )
    audit.record(db, "revision.request", entity="revision_request", entity_id=req.id,
                 detail=f"status={req.status.value} reason={req.decision_reason}")
    db.commit()
    return {
        "id": req.id, "status": req.status.value,
        "decision_reason": req.decision_reason,
        "extra_charge_cents": req.extra_charge_cents,
    }


@router.post("/change-orders")
def open_change_order(body: ChangeOrderIn, db: Session = Depends(get_db)):
    req = db.get(RevisionRequest, body.revision_id)
    if not req:
        raise HTTPException(404, "修改申请不存在")
    try:
        co = rev_service.create_change_order(
            db, req, body.extra_charge_cents, body.extra_rounds, body.new_due
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    audit.record(db, "changeorder.create", entity="revision_request", entity_id=req.id,
                 detail=f"+{co.extra_charge_cents} cents +{co.extra_rounds} rounds")
    db.commit()
    return {"change_order_id": co.id, "accepted_by_client": False, "applied": False}


@router.post("/change-orders/accept")
def client_accept_change(body: AcceptChangeIn, db: Session = Depends(get_db)):
    """In real flow the CLIENT accepts; here admin records the client's
    acceptance before the studio applies it."""
    req = db.get(RevisionRequest, body.revision_id)
    if not req:
        raise HTTPException(404, "修改申请不存在")
    co = db.query(ChangeOrder).filter_by(revision_id=req.id).one_or_none()
    if not co:
        raise HTTPException(404, "变更单不存在")
    co.accepted_by_client = body.accepted
    if body.accepted:
        rev_service.apply_change_order(db, req)
    audit.record(db, "changeorder.apply" if body.accepted else "changeorder.decline",
                 entity="revision_request", entity_id=req.id)
    db.commit()
    return {"applied": co.applied, "status": req.status.value}


@router.get("/revisions")
def list_revisions(project_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(RevisionRequest)
    if project_id:
        q = q.filter(RevisionRequest.project_id == project_id)
    return [{
        "id": r.id, "project_id": r.project_id, "stage_id": r.stage_id,
        "status": r.status.value, "detail": r.detail,
        "decision_reason": r.decision_reason,
        "extra_charge_cents": r.extra_charge_cents,
    } for r in q.order_by(RevisionRequest.id)]


# ---------------------------------------------------------------- schedule
@router.post("/slots")
def create_slot(body: SlotIn, db: Session = Depends(get_db)):
    try:
        slot = hold_slot(db, body.starts_at, body.ends_at, body.ttl_hours,
                         body.project_id)
    except ValueError as e:
        raise HTTPException(422, str(e))
    audit.record(db, "slot.hold", entity="schedule_slot", entity_id=slot.id,
                 detail=f"until {slot.hold_expires_at}")
    db.commit()
    return _slot_out(slot)


@router.post("/slots/book")
def book(body: BookSlotIn, db: Session = Depends(get_db)):
    slot = db.get(ScheduleSlot, body.slot_id)
    p = db.get(Project, body.project_id)
    if not slot or not p:
        raise HTTPException(404, "档期或项目不存在")
    try:
        book_slot(db, slot, p)
    except ValueError as e:
        raise HTTPException(422, str(e))
    audit.record(db, "slot.book", entity="schedule_slot", entity_id=slot.id,
                 detail=f"project={p.id}")
    db.commit()
    return _slot_out(slot)


@router.get("/slots")
def list_slots(db: Session = Depends(get_db)):
    n = expire_holds(db)
    db.commit()
    return [_slot_out(s) for s in db.query(ScheduleSlot).order_by(ScheduleSlot.id)]


def _slot_out(s: ScheduleSlot) -> dict:
    return {
        "id": s.id, "project_id": s.project_id,
        "starts_at": s.starts_at.isoformat(), "ends_at": s.ends_at.isoformat(),
        "status": s.status.value,
        "hold_expires_at": s.hold_expires_at.isoformat() if s.hold_expires_at else None,
    }
