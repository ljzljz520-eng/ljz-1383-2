"""Schedule slot lifecycle: hold -> book, with hold expiry and overlap rules."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .. import config
from ..models import Project, ScheduleSlot, SlotStatus


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def expire_holds(db: Session, now: datetime | None = None) -> int:
    """Mark temporary holds whose TTL elapsed as EXPIRED. Idempotent."""
    now = now or datetime.now(timezone.utc)
    rows = db.query(ScheduleSlot).filter(ScheduleSlot.status == SlotStatus.HELD).all()
    n = 0
    for s in rows:
        if s.hold_expires_at and _aware(s.hold_expires_at) < now:
            s.status = SlotStatus.EXPIRED
            n += 1
    if n:
        db.flush()
    return n


def _has_booked_overlap(db: Session, start: datetime, end: datetime,
                        exclude_id: int | None = None) -> bool:
    q = db.query(ScheduleSlot).filter(ScheduleSlot.status == SlotStatus.BOOKED)
    for s in q:
        if exclude_id and s.id == exclude_id:
            continue
        if _aware(s.starts_at) < end and start < _aware(s.ends_at):
            return True
    return False


def hold_slot(db: Session, start: datetime, end: datetime,
              ttl_hours: int | None = None, project_id: int | None = None) -> ScheduleSlot:
    start, end = _aware(start), _aware(end)
    if end <= start:
        raise ValueError("结束时间必须晚于开始时间")
    ttl = config.HOLD_TTL_HOURS if ttl_hours is None else ttl_hours
    slot = ScheduleSlot(
        project_id=project_id,
        starts_at=start,
        ends_at=end,
        status=SlotStatus.HELD,
        hold_expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl),
    )
    db.add(slot)
    db.flush()
    return slot


def book_slot(db: Session, slot: ScheduleSlot, project: Project) -> ScheduleSlot:
    expire_holds(db)
    if slot.status == SlotStatus.EXPIRED:
        raise ValueError("档期占位已过期，请重新申请档期")
    if slot.status == SlotStatus.BOOKED:
        raise ValueError("该档期已被预约")
    if _has_booked_overlap(db, _aware(slot.starts_at), _aware(slot.ends_at), slot.id):
        raise ValueError("与已预约档期冲突")
    slot.status = SlotStatus.BOOKED
    slot.hold_expires_at = None
    slot.project_id = project.id
    db.flush()
    return slot
