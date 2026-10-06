from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import AuditEvent


def record(db: Session, action: str, actor: str = "admin", entity: str = "",
           entity_id=None, detail: str = "") -> AuditEvent:
    ev = AuditEvent(
        action=action, actor=actor, entity=entity,
        entity_id=None if entity_id is None else str(entity_id), detail=detail,
    )
    db.add(ev)
    db.flush()
    return ev
