"""Auth: admin cookie session + client capability-token helpers."""
from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeSerializer

from . import config
from .database import get_db
from .models import Client

_signer = URLSafeSerializer(config.SECRET_KEY, salt="studio-session")


def create_admin_token(username: str) -> str:
    return _signer.dumps({"u": username, "role": "admin"})


def verify_admin(token: str) -> bool:
    try:
        data = _signer.loads(token)
    except BadSignature:
        return False
    return data.get("role") == "admin" and data.get("u") == config.ADMIN_USER


def require_admin(session: str | None = Cookie(default=None)) -> dict:
    if not session or not verify_admin(session):
        raise HTTPException(status_code=401, detail="需要管理员登录")
    return {"role": "admin"}


def client_for_token(db, token: str | None) -> Client | None:
    if not token:
        return None
    return db.query(Client).filter(Client.access_token == token).one_or_none()


def require_client(
    request: Request,
    db=Depends(get_db),
    client_token: str | None = Cookie(default=None),
) -> Client:
    token = request.headers.get("X-Client-Token") or client_token
    client = client_for_token(db, token)
    if client is None:
        raise HTTPException(status_code=401, detail="无效的客户访问令牌")
    return client
