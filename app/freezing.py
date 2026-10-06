"""冻结与修订约束：报价、修改次数、截止日的落实点。

整包冻结(project)：全局一个计数器，解冻=全部阶段解冻。
逐阶段冻结(stage)：每阶段独立计数，解冻只影响该阶段，并级联作废下游成果。
"""
from . import audit
from .errors import BadRequest, Conflict, NotFound, QuotaExceeded


def freeze_stage(conn, stage_id, now):
    conn.execute("UPDATE stages SET frozen=1, frozen_at=? WHERE id=?", (now, stage_id))


def freeze_project(conn, project_id, now):
    conn.execute("UPDATE stages SET frozen=1, frozen_at=? WHERE project_id=?", (now, project_id))


def request_revision(conn, project_id, stage_id, actor, now):
    """冻结后提出修改 = 消耗一次修订额度。原子扣减，超限/过期直接拒绝。"""
    p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    s = conn.execute("SELECT * FROM stages WHERE id=? AND project_id=?",
                     (stage_id, project_id)).fetchone()
    if p is None or s is None:
        raise NotFound("项目或阶段不存在")
    if not s["frozen"]:
        raise BadRequest("阶段未冻结：直接修改即可，不计入修订")
    if p["deadline"] and now > p["deadline"]:
        raise Conflict("已过截止日：修订须先重排档期与报价")

    if p["freeze_mode"] == "project":
        cur = conn.execute(
            "UPDATE projects SET revisions_used=revisions_used+1 "
            "WHERE id=? AND revisions_used<revision_limit", (project_id,))
        if cur.rowcount == 0:
            raise QuotaExceeded("项目修改次数已用尽，需追加报价(amendment)")
        conn.execute("UPDATE stages SET frozen=0, frozen_at=NULL WHERE project_id=?",
                     (project_id,))
    else:
        cur = conn.execute(
            "UPDATE stages SET revisions_used=revisions_used+1 "
            "WHERE id=? AND revisions_used<revision_limit", (stage_id,))
        if cur.rowcount == 0:
            raise QuotaExceeded("该阶段修改次数已用尽，需追加报价(amendment)")
        conn.execute("UPDATE stages SET frozen=0, frozen_at=NULL WHERE id=?", (stage_id,))
        # 级联：上游返工 => 下游阶段成果失效并解冻待重做
        conn.execute(
            "UPDATE artwork_versions SET invalidated=1 WHERE stage_id IN "
            "(SELECT id FROM stages WHERE project_id=? AND seq>?)", (project_id, s["seq"]))
        conn.execute(
            "UPDATE stages SET frozen=0, frozen_at=NULL WHERE project_id=? AND seq>?",
            (project_id, s["seq"]))

    audit.log(conn, actor, "revision", "revision", stage_id,
              {"project_id": project_id, "mode": p["freeze_mode"]}, now)
    return True


def purchase_amendment(conn, project_id, stage_id, extra_revisions, extra_cents, actor, now):
    """超限后的追加报价：提高额度 + 计入报价，全程留痕。"""
    if extra_revisions <= 0 or extra_cents < 0:
        raise BadRequest("追加额度/金额非法")
    p = conn.execute("SELECT freeze_mode FROM projects WHERE id=?", (project_id,)).fetchone()
    if p is None:
        raise NotFound("项目不存在")
    if p["freeze_mode"] == "project":
        conn.execute("UPDATE projects SET revision_limit=revision_limit+? WHERE id=?",
                     (extra_revisions, project_id))
    else:
        conn.execute("UPDATE stages SET revision_limit=revision_limit+? WHERE id=?",
                     (extra_revisions, stage_id))
    conn.execute("UPDATE projects SET quote_cents=quote_cents+? WHERE id=?",
                 (extra_cents, project_id))
    audit.log(conn, actor, "amendment", "project", project_id,
              {"stage_id": stage_id, "extra_revisions": extra_revisions,
               "extra_cents": extra_cents}, now)
