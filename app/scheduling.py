"""档期占位：held(带过期) → booked；确认与占用检查都是条件更新，可安全并发。"""
from .errors import Conflict, Gone, NotFound


def hold_slot(conn, slot_start, slot_end, expires_at, now, project_id=None):
    overlap = conn.execute(
        "SELECT id FROM schedule_holds "
        "WHERE (status='booked' OR (status='held' AND expires_at>?)) "
        "AND slot_start < ? AND slot_end > ? LIMIT 1",
        (now, slot_end, slot_start)).fetchone()
    if overlap:
        raise Conflict("时段已被占用")
    cur = conn.execute(
        "INSERT INTO schedule_holds(slot_start,slot_end,project_id,status,expires_at,"
        " created_at) VALUES(?,?,?,'held',?,?)",
        (slot_start, slot_end, project_id, expires_at, now))
    return cur.lastrowid


def confirm_hold(conn, hold_id, now):
    """占位确认：只有未过期的 held 能转 booked；过期占位返回 410。"""
    cur = conn.execute(
        "UPDATE schedule_holds SET status='booked' "
        "WHERE id=? AND status='held' AND expires_at>?", (hold_id, now))
    if cur.rowcount:
        return "booked"
    row = conn.execute("SELECT status, expires_at FROM schedule_holds WHERE id=?",
                       (hold_id,)).fetchone()
    if row is None:
        raise NotFound("占位不存在")
    if row["status"] == "held" and row["expires_at"] <= now:
        conn.execute("UPDATE schedule_holds SET status='expired' WHERE id=?", (hold_id,))
        raise Gone("占位已过期，请重新占位")
    raise Conflict(f"占位状态 {row['status']} 不可确认")


def sweep_expired(conn, now):
    return conn.execute(
        "UPDATE schedule_holds SET status='expired' WHERE status='held' AND expires_at<=?",
        (now,)).rowcount
