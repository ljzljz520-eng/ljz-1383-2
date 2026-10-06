"""Commission enquiry drafts, isolated per browser via a random draft token.

The token is stored in a cookie AND required in the request body for restore
(double-submit). There is no endpoint that lists/enumerates drafts, so one
browser can never read another browser's unfinished commission draft.
"""
import secrets

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import CommissionDraft
from ..schemas import DraftIn
from pydantic import BaseModel
from ..services import audit

router = APIRouter(prefix="/api/commission", tags=["commission-draft"])
COOKIE = "draft_token"


class SubmitIn(BaseModel):
    draft_token: str


def _new_token() -> str:
    return secrets.token_urlsafe(24)


def _load(db: Session, token: str | None, draft_token: str | None) -> CommissionDraft:
    # body token must equal cookie token
    if not token or not draft_token or token != draft_token:
        raise HTTPException(403, "无法访问该草稿：草稿仅限原浏览器恢复")
    d = db.query(CommissionDraft).filter(CommissionDraft.draft_token == token).one_or_none()
    if not d:
        raise HTTPException(404, "草稿不存在或已提交")
    return d


@router.post("/drafts")
def save_draft(body: DraftIn, response: Response, db: Session = Depends(get_db),
               draft_token: str | None = Cookie(default=None)):
    from datetime import datetime, timezone

    if draft_token:
        d = db.query(CommissionDraft).filter(
            CommissionDraft.draft_token == draft_token).one_or_none()
    else:
        d = None
    new = d is None
    if new:
        d = CommissionDraft(draft_token=_new_token())
        db.add(d)
        response.set_cookie(COOKIE, d.draft_token, httponly=True,
                            samesite="lax", max_age=60 * 60 * 24 * 14)
    d.intended_use = body.intended_use
    d.delivery_scope = body.delivery_scope
    d.contact = body.contact
    d.budget_cents = body.budget_cents
    d.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(d)
    return {"draft_token": d.draft_token, "submitted": d.submitted, "new": new}


@router.get("/drafts/restore")
def restore_draft(draft_token: str | None = None, db: Session = Depends(get_db),
                  cookie_token: str | None = Cookie(default=None, alias=COOKIE)):
    d = _load(db, cookie_token, draft_token)
    if d.submitted:
        raise HTTPException(410, "草稿已提交，不可再恢复")
    return {
        "draft_token": d.draft_token,
        "intended_use": d.intended_use,
        "delivery_scope": d.delivery_scope,
        "contact": d.contact,
        "budget_cents": d.budget_cents,
        "submitted": d.submitted,
    }


@router.post("/drafts/submit")
def submit_draft(body: SubmitIn, db: Session = Depends(get_db),
                 cookie_token: str | None = Cookie(default=None, alias=COOKIE)):
    d = _load(db, cookie_token, body.draft_token)
    if not d.intended_use.strip() or not d.delivery_scope.strip():
        raise HTTPException(422, "请完整填写用途与交付范围后再提交委托")
    d.submitted = True
    audit.record(db, "commission.submit", actor="anonymous",
                 entity="commission_draft", entity_id=d.id,
                 detail=f"use={d.intended_use[:30]} scope={d.delivery_scope[:30]}")
    db.commit()
    return {"ok": True, "message": "委托已提交，作者将根据用途与交付范围回复报价与档期"}
