from fastapi import APIRouter, Depends, HTTPException, Response

from .. import config
from ..schemas import LoginIn
from ..security import create_admin_token, verify_admin

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(body: LoginIn, response: Response):
    if body.username != config.ADMIN_USER or body.password != config.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_admin_token(body.username)
    response.set_cookie(
        "session", token, httponly=True, samesite="lax", max_age=60 * 60 * 8
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("session")
    return {"ok": True}


@router.get("/me")
def me(session: str | None = None):
    return {"admin": bool(session and verify_admin(session))}
