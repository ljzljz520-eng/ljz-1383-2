"""委托草稿：capability token 隔离。

token 只存在于填写者浏览器的 localStorage；服务端无枚举接口、无顺序 id；
提交即失效；草稿永不进入公开读模型 —— 其他浏览器用户无法恢复他人草稿。
"""
import json
import secrets

from .errors import BadRequest, NotFound


def create_draft(conn, now, contact_email=None):
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO commission_requests(contact_email,status,draft_token,created_at,"
        " updated_at) VALUES(?,?,?,?,?)",
        (contact_email, "draft", token, now, now))
    return token


def _get(conn, token):
    row = conn.execute(
        "SELECT * FROM commission_requests WHERE draft_token=? AND status='draft'",
        (token,)).fetchone()
    if row is None:
        raise NotFound("草稿不存在或已提交")
    return row


def get_draft(conn, token):
    row = _get(conn, token)
    return {"purpose": row["purpose"], "delivery_scope": json.loads(row["delivery_scope"]),
            "contact_email": row["contact_email"], "budget_cents": row["budget_cents"],
            "desired_deadline": row["desired_deadline"]}


def update_draft(conn, token, now, purpose=None, delivery_scope=None, contact_email=None,
                 budget_cents=None, desired_deadline=None):
    _get(conn, token)
    conn.execute(
        "UPDATE commission_requests SET purpose=COALESCE(?,purpose),"
        " delivery_scope=COALESCE(?,delivery_scope),"
        " contact_email=COALESCE(?,contact_email), budget_cents=COALESCE(?,budget_cents),"
        " desired_deadline=COALESCE(?,desired_deadline), updated_at=? WHERE draft_token=?",
        (purpose, json.dumps(delivery_scope, ensure_ascii=False)
         if delivery_scope is not None else None,
         contact_email, budget_cents, desired_deadline, now, token))


def submit_draft(conn, token, now):
    row = _get(conn, token)
    if not row["purpose"]:
        raise BadRequest("用途(purpose)必填")
    conn.execute(
        "UPDATE commission_requests SET status='submitted', draft_token=NULL,"
        " submitted_at=?, updated_at=? WHERE id=?", (now, now, row["id"]))
    return row["id"]


def expire_drafts(conn, now):
    return conn.execute(
        "UPDATE commission_requests SET draft_token=NULL WHERE status='draft' "
        "AND updated_at < ?", (now,)).rowcount
