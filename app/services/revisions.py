"""Revision constraint enforcement + freeze policies.

Two freeze models are explicitly modelled:
* PACKAGE — one freeze locks the ENTIRE project; stage-level freeze is rejected.
* STAGE   — each stage freezes independently; other stages remain open.

Every revision decision is explained (quote / included rounds / deadline) and
persisted on RevisionRequest.decision_reason for traceability.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models import (
    ChangeOrder,
    FreezeEvent,
    FreezeMode,
    Project,
    RevisionRequest,
    RevisionStatus,
    Stage,
    StageStatus,
)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------- freeze
def freeze_package(db: Session, project: Project, reason: str = "") -> FreezeEvent:
    project.frozen = True
    project.frozen_at = datetime.now(timezone.utc)
    for st in project.stages:
        if st.status == StageStatus.OPEN:
            st.status = StageStatus.FROZEN
            st.frozen_at = project.frozen_at
    ev = FreezeEvent(project_id=project.id, stage_id=None,
                     mode=FreezeMode.PACKAGE, reason=reason)
    db.add(ev)
    db.flush()
    return ev


def freeze_stage(db: Session, project: Project, stage: Stage,
                 reason: str = "") -> FreezeEvent:
    if project.freeze_mode is not FreezeMode.STAGE:
        raise ValueError("当前项目采用整包冻结模式，不能单独冻结阶段")
    if project.frozen:
        raise ValueError("项目已整包冻结")
    stage.status = StageStatus.FROZEN
    stage.frozen_at = datetime.now(timezone.utc)
    ev = FreezeEvent(project_id=project.id, stage_id=stage.id,
                     mode=FreezeMode.STAGE, reason=reason)
    db.add(ev)
    db.flush()
    return ev


# ----------------------------------------------------------------- constraints
def _rounds_budget(project: Project, stage: Stage | None) -> tuple[int, int]:
    """Return (included, used) applying stage override when present."""
    if stage and stage.edit_rounds_included:
        return stage.edit_rounds_included, stage.rounds_used
    return project.edit_rounds_included, project.rounds_used


def _due(project: Project, stage: Stage | None) -> datetime | None:
    if stage and stage.stage_due:
        return _aware(stage.stage_due)
    return _aware(project.due_date)


def evaluate(db: Session, project: Project, stage: Stage | None,
             now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    blockers: list[str] = []
    warnings: list[str] = []

    if project.frozen:
        blockers.append("项目已整包冻结，冻结范围内不再接受修改")
    elif stage and stage.status == StageStatus.FROZEN:
        blockers.append(f"阶段「{stage.name}」已冻结，不再接受修改")

    included, used = _rounds_budget(project, stage)
    remaining = max(0, included - used)
    needs_change_order = False
    if remaining <= 0:
        needs_change_order = True
        blockers_hint = f"含 {included} 次修改已用完，新增修改需报价变更单"
        warnings.append(blockers_hint)

    due = _due(project, stage)
    overdue = bool(due and due < now)
    if overdue:
        needs_change_order = True
        warnings.append("已过截止日，逾期修改需变更单确认新截止日")

    return {
        "blocked": bool(blockers),
        "blockers": blockers,
        "needs_change_order": needs_change_order,
        "rounds_included": included,
        "rounds_used": used,
        "rounds_remaining": remaining,
        "due": due.isoformat() if due else None,
        "overdue": overdue,
        "warnings": warnings,
        "price_cents": project.price_cents,
    }


def request_revision(db: Session, project: Project, detail: str,
                     stage: Stage | None = None, version_id: int | None = None,
                     requested_by: str = "client") -> RevisionRequest:
    verdict = evaluate(db, project, stage)
    req = RevisionRequest(
        project_id=project.id,
        stage_id=stage.id if stage else None,
        version_id=version_id,
        detail=detail,
        requested_by=requested_by,
    )
    if verdict["blocked"]:
        req.status = RevisionStatus.REJECTED
        req.decision_reason = "；".join(verdict["blockers"])
    elif verdict["needs_change_order"]:
        req.status = RevisionStatus.REQUESTED
        parts = [
            f"修改次数 {verdict['rounds_used']}/{verdict['rounds_included']}（剩余 0）"
            if verdict["rounds_remaining"] == 0
            else f"修改次数剩余 {verdict['rounds_remaining']}",
            f"基准报价 {verdict['price_cents']} 分",
        ]
        parts.extend(verdict["warnings"])
        req.decision_reason = "超出合同条款，待变更单：" + "；".join(parts)
    else:
        req.status = RevisionStatus.APPROVED
        req.decision_reason = (
            f"在含 {verdict['rounds_included']} 次修改范围内，"
            f"本次后剩余 {verdict['rounds_remaining'] - 1} 次；"
            f"截止 {verdict['due'] or '未约定'}"
        )
        req.decided_at = datetime.now(timezone.utc)
        # consume a round the moment an in-scope revision is accepted
        if stage and stage.edit_rounds_included:
            stage.rounds_used += 1
        else:
            project.rounds_used += 1
    db.add(req)
    db.flush()
    return req


def create_change_order(db: Session, req: RevisionRequest, extra_charge_cents: int,
                        extra_rounds: int, new_due: datetime | None = None) -> ChangeOrder:
    if req.status not in (RevisionStatus.REQUESTED,):
        raise ValueError("只有待商务确认的修改申请可以开立变更单")
    co = ChangeOrder(
        revision_id=req.id,
        extra_charge_cents=extra_charge_cents,
        extra_rounds=extra_rounds,
        new_due=_aware(new_due),
    )
    req.extra_charge_cents = extra_charge_cents
    db.add(co)
    db.flush()
    return co


def apply_change_order(db: Session, req: RevisionRequest) -> ChangeOrder:
    co = db.query(ChangeOrder).filter(ChangeOrder.revision_id == req.id).one_or_none()
    if co is None:
        raise ValueError("没有变更单")
    if not co.accepted_by_client:
        raise ValueError("变更单需客户先接受报价与新截止日")
    project = db.get(Project, req.project_id)
    stage = db.get(Stage, req.stage_id) if req.stage_id else None
    if stage and stage.edit_rounds_included:
        stage.edit_rounds_included += co.extra_rounds
        stage.rounds_used += 1
        if co.new_due:
            stage.stage_due = co.new_due
    else:
        project.edit_rounds_included += co.extra_rounds
        project.rounds_used += 1
        if co.new_due:
            project.due_date = co.new_due
    co.applied = True
    req.status = RevisionStatus.APPROVED
    req.decided_at = datetime.now(timezone.utc)
    req.decision_reason += (
        f"｜变更单已执行：+{co.extra_charge_cents} 分，+{co.extra_rounds} 次修改"
    )
    db.flush()
    return co
