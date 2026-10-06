import json
import threading
import unittest
import urllib.request

from app import (audit, derivatives, drafts, freezing, licensing, packages,
                 permissions, projects, public, scheduling, versions)
from app.api import make_handler
from app.db import Database
from app.errors import (BadRequest, Conflict, Forbidden, Gone, NotFound,
                        QuotaExceeded)

T0 = "2026-10-01T09:00:00"
T1 = "2026-10-05T09:00:00"
T2 = "2026-10-06T09:00:00"


class Base(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.c = self.db.conn
        self.artist = self._user("artist", "画师")
        self.client = self._user("client", "客户")

    def _user(self, role, name):
        return self.c.execute("INSERT INTO users(role,name) VALUES(?,?)",
                              (role, name)).lastrowid

    def _project(self, **kw):
        args = dict(title="委托A", freeze_mode="stage", quote_cents=300000,
                    revision_limit=3, deadline="2026-12-31", now=T0,
                    stage_revision_limit=2)
        args.update(kw)
        return projects.create_project(self.c, **args)

    def _artwork(self, project_id=None, title="月见"):
        return self.c.execute(
            "INSERT INTO artworks(project_id,title,description,created_at)"
            " VALUES(?,?,?,?)",
            (project_id, title, "夜晚与猫的插画", T0)).lastrowid

    def _stage(self, project_id, kind):
        return self.c.execute(
            "SELECT id FROM stages WHERE project_id=? AND kind=?",
            (project_id, kind)).fetchone()["id"]

    def _asset(self, version_id, role, h="h"):
        return self.c.execute(
            "INSERT INTO assets(version_id,role,storage_path,file_hash,created_at)"
            " VALUES(?,?,?,?,?)",
            (version_id, role, f"/cdn/{version_id}/{role}.webp", h, T0)).lastrowid


class PermissionMatrixTests(Base):
    """同一图像：可公开展示 ≠ 可下载 ≠ 可再授权；派生物各自独立授权。"""

    def test_display_without_download_or_relicense(self):
        pid = self._project()
        art = self._artwork(pid)
        v = versions.add_version(self.c, art, None, "hash1", "/o/1.png", "", self.artist, T0)
        final = self._asset(v, "final")
        permissions.grant_matrix(self.c, final, "public",
                                 display=True, download=False, relicense=False)
        self.assertTrue(permissions.effective(self.c, final, "public", "display"))
        self.assertFalse(permissions.effective(self.c, final, "public", "download"))
        self.assertFalse(permissions.effective(self.c, final, "public", "relicense"))

    def test_derivatives_have_independent_switches(self):
        pid = self._project()
        art = self._artwork(pid)
        v = versions.add_version(self.c, art, None, "hash1", "/o/1.png", "", self.artist, T0)
        thumb = self._asset(v, "thumbnail")
        process = self._asset(v, "process")
        final = self._asset(v, "final")
        # 缩略图：公开可展示可索引；过程图：仅客户可见；成稿：可展示不可下载
        permissions.grant_matrix(self.c, thumb, "public", display=True, index=True)
        permissions.grant_matrix(self.c, process, "client", display=True, download=True)
        permissions.grant_matrix(self.c, final, "public", display=True, download=False)
        self.assertFalse(permissions.effective(self.c, process, "public", "display"))
        self.assertTrue(permissions.effective(self.c, process, "client", "display"))
        self.assertFalse(permissions.effective(self.c, thumb, "public", "download"))
        self.assertFalse(permissions.effective(self.c, final, "public", "download"))

    def test_default_deny_and_confidential_override(self):
        pid = self._project()
        art = self._artwork(pid)
        v = versions.add_version(self.c, art, None, "hash1", "/o/1.png", "", self.artist, T0)
        a = self._asset(v, "final")
        self.assertFalse(permissions.effective(self.c, a, "public", "display"))  # 默认拒绝
        permissions.grant_matrix(self.c, a, "public", display=True, download=True)
        projects.set_confidential(self.c, pid, True, "artist", T1)
        self.assertFalse(permissions.effective(self.c, a, "public", "display"))
        self.assertFalse(permissions.effective(self.c, a, "client", "download"))
        self.assertTrue(permissions.effective(self.c, a, "admin", "display"))  # 管理员留档


class ApprovalTests(Base):
    """客户批准某稿后作者又修改：批准对象不动、差异留痕、最新版不冒名。"""

    def test_approval_pinned_and_diff_recorded(self):
        pid = self._project()
        art = self._artwork(pid)
        v1 = versions.add_version(self.c, art, None, "aaa", "/o/1.png", "初稿", self.artist, T0)
        v2 = versions.add_version(self.c, art, None, "bbb", "/o/2.png", "二稿", self.artist, T0)
        versions.approve(self.c, pid, v2, self.client, T1)
        # 作者批准后又改了一版
        v3 = versions.add_version(self.c, art, None, "ccc", "/o/3.png", "微调", self.artist, T2)
        st = versions.approval_status(self.c, art)
        self.assertEqual(st["approved_version_id"], v2)
        self.assertEqual(st["approved_hash"], "bbb")
        self.assertEqual(st["latest_version_id"], v3)
        self.assertTrue(st["stale"])  # 最新文件 ≠ 已批准文件
        diffs = self.c.execute(
            "SELECT * FROM change_diffs WHERE project_id=?", (pid,)).fetchall()
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["from_version_id"], v2)
        self.assertEqual(diffs[0]["to_version_id"], v3)

    def test_package_contains_only_approved_assets(self):
        pid = self._project()
        art = self._artwork(pid)
        v1 = versions.add_version(self.c, art, None, "aaa", "/o/1.png", "", self.artist, T0)
        v2 = versions.add_version(self.c, art, None, "bbb", "/o/2.png", "", self.artist, T0)
        versions.approve(self.c, pid, v2, self.client, T1)
        self._asset(v2, "final", "bbb")
        self._asset(v2, "thumbnail", "bbb")
        v3 = versions.add_version(self.c, art, None, "ccc", "/o/3.png", "", self.artist, T2)
        self._asset(v3, "final", "ccc")  # 未获批的新文件
        manifest = packages.build_package(self.c, pid, self.client, T2)
        ids = [i["version_id"] for i in manifest["items"]]
        self.assertEqual(ids, [v2])  # 只有实际获批的 v2
        roles = sorted(a["role"] for a in manifest["items"][0]["assets"])
        self.assertEqual(roles, ["final", "thumbnail"])


class RevisionConstraintTests(Base):
    """报价/修改次数/截止日对修订的约束（逐阶段冻结）。"""

    def test_revision_consumes_quota_and_unfreezes_stage(self):
        pid = self._project()
        sketch = self._stage(pid, "sketch")
        freezing.freeze_stage(self.c, sketch, T0)
        freezing.request_revision(self.c, pid, sketch, "client", T1)
        row = self.c.execute("SELECT revisions_used, frozen FROM stages WHERE id=?",
                             (sketch,)).fetchone()
        self.assertEqual(row["revisions_used"], 1)
        self.assertEqual(row["frozen"], 0)

    def test_unfrozen_stage_revision_rejected(self):
        pid = self._project()
        sketch = self._stage(pid, "sketch")
        with self.assertRaises(BadRequest):
            freezing.request_revision(self.c, pid, sketch, "client", T1)

    def test_quota_exceeded_then_amendment(self):
        pid = self._project()  # 每阶段额度 2
        sketch = self._stage(pid, "sketch")
        for i in range(2):
            freezing.freeze_stage(self.c, sketch, T0)
            freezing.request_revision(self.c, pid, sketch, "client", T1)
        freezing.freeze_stage(self.c, sketch, T0)
        with self.assertRaises(QuotaExceeded):
            freezing.request_revision(self.c, pid, sketch, "client", T1)
        # 追加报价：+1 次修改，+500 元
        freezing.purchase_amendment(self.c, pid, sketch, 1, 50000, "client", T1)
        freezing.request_revision(self.c, pid, sketch, "client", T1)
        p = self.c.execute("SELECT quote_cents FROM projects WHERE id=?", (pid,)).fetchone()
        self.assertEqual(p["quote_cents"], 300000 + 50000)

    def test_revision_after_deadline_rejected(self):
        pid = self._project(deadline="2026-10-10")
        sketch = self._stage(pid, "sketch")
        freezing.freeze_stage(self.c, sketch, T0)
        with self.assertRaises(Conflict):
            freezing.request_revision(self.c, pid, sketch, "client", "2026-10-11T00:00:00")

    def test_upstream_revision_invalidates_downstream(self):
        pid = self._project()
        sketch, line, color = (self._stage(pid, k) for k in ("sketch", "line", "color"))
        for s in (sketch, line, color):
            freezing.freeze_stage(self.c, s, T0)
        art = self._artwork(pid)
        v_color = versions.add_version(self.c, art, color, "hhh", "/o/c.png", "上色",
                                       self.artist, T0)
        freezing.request_revision(self.c, pid, sketch, "client", T1)  # 草图返工
        v = self.c.execute("SELECT invalidated FROM artwork_versions WHERE id=?",
                           (v_color,)).fetchone()
        self.assertEqual(v["invalidated"], 1)  # 下游上色成果作废
        downstream = self.c.execute("SELECT frozen FROM stages WHERE id=?",
                                    (color,)).fetchone()
        self.assertEqual(downstream["frozen"], 0)


class ProjectFreezeTests(Base):
    """整包冻结：全局计数、解冻即全解冻。"""

    def test_project_mode_global_quota(self):
        pid = self._project(freeze_mode="project", revision_limit=1)
        freezing.freeze_project(self.c, pid, T0)
        sketch = self._stage(pid, "sketch")
        freezing.request_revision(self.c, pid, sketch, "client", T1)
        p = self.c.execute("SELECT revisions_used FROM projects WHERE id=?",
                           (pid,)).fetchone()
        self.assertEqual(p["revisions_used"], 1)
        stages = self.c.execute(
            "SELECT COUNT(*) AS n FROM stages WHERE project_id=? AND frozen=1",
            (pid,)).fetchone()
        self.assertEqual(stages["n"], 0)  # 整包解冻
        freezing.freeze_project(self.c, pid, T1)
        with self.assertRaises(QuotaExceeded):
            freezing.request_revision(self.c, pid, sketch, "client", T2)


class AcceptanceRaceTests(Base):
    """验收：两方同时确认不同版本 => mismatch，任何一方都不能单方面成交。"""

    def test_mismatch_then_converge(self):
        pid = self._project()
        art = self._artwork(pid)
        v3 = versions.add_version(self.c, art, None, "v3", "/o/3.png", "", self.artist, T0)
        v4 = versions.add_version(self.c, art, None, "v4", "/o/4.png", "", self.artist, T0)
        r1 = projects.confirm_acceptance(self.c, pid, "client", v3, T1)
        self.assertEqual(r1["status"], "pending")
        r2 = projects.confirm_acceptance(self.c, pid, "artist", v4, T1)  # 同时确认不同版本
        self.assertEqual(r2["status"], "mismatch")
        p = self.c.execute("SELECT status, accepted_version_id FROM projects WHERE id=?",
                           (pid,)).fetchone()
        self.assertNotEqual(p["status"], "accepted")
        self.assertIsNone(p["accepted_version_id"])
        r3 = projects.confirm_acceptance(self.c, pid, "artist", v3, T2)  # 作者改认 v3
        self.assertEqual(r3["status"], "accepted")
        self.assertEqual(r3["version_id"], v3)

    def test_confirm_after_accept_conflict(self):
        pid = self._project()
        art = self._artwork(pid)
        v = versions.add_version(self.c, art, None, "v1", "/o/1.png", "", self.artist, T0)
        projects.confirm_acceptance(self.c, pid, "client", v, T1)
        projects.confirm_acceptance(self.c, pid, "artist", v, T1)
        with self.assertRaises(Conflict):
            projects.confirm_acceptance(self.c, pid, "client", v, T2)

    def test_confirm_foreign_version_not_found(self):
        pid, other = self._project(), self._project()
        art = self._artwork(other)
        v = versions.add_version(self.c, art, None, "v1", "/o/1.png", "", self.artist, T0)
        with self.assertRaises(NotFound):
            projects.confirm_acceptance(self.c, pid, "client", v, T1)


class ScheduleHoldTests(Base):
    def test_overlap_conflict(self):
        scheduling.hold_slot(self.c, "2026-11-01", "2026-11-10", "2026-10-20", T0)
        with self.assertRaises(Conflict):
            scheduling.hold_slot(self.c, "2026-11-05", "2026-11-15", "2026-10-20", T0)

    def test_expired_hold_cannot_confirm(self):
        hid = scheduling.hold_slot(self.c, "2026-11-01", "2026-11-10", "2026-10-20", T0)
        with self.assertRaises(Gone):
            scheduling.confirm_hold(self.c, hid, "2026-10-21T00:00:00")
        row = self.c.execute("SELECT status FROM schedule_holds WHERE id=?", (hid,)).fetchone()
        self.assertEqual(row["status"], "expired")
        # 过期释放后同一时段可再占位
        scheduling.hold_slot(self.c, "2026-11-01", "2026-11-10", "2026-10-25", T2)

    def test_confirm_and_sweep(self):
        h1 = scheduling.hold_slot(self.c, "2026-11-01", "2026-11-10", "2026-10-20", T0)
        h2 = scheduling.hold_slot(self.c, "2026-12-01", "2026-12-10", "2026-10-08", T0)
        self.assertEqual(scheduling.confirm_hold(self.c, h1, T1), "booked")
        self.assertEqual(scheduling.sweep_expired(self.c, "2026-10-09T00:00:00"), 1)
        with self.assertRaises(Conflict):
            scheduling.confirm_hold(self.c, h1, T1)  # 重复确认


class DerivativeJobTests(Base):
    def _produce(self, job):
        return (f"/cdn/{job['version_id']}/{job['role']}.webp", job["source_hash"])

    def test_idempotent_enqueue(self):
        art = self._artwork()
        v = versions.add_version(self.c, art, None, "aaa", "/o/1.png", "", self.artist, T0)
        j1 = derivatives.enqueue(self.c, v, "thumbnail", "aaa", "key-1", T0)
        j2 = derivatives.enqueue(self.c, v, "thumbnail", "aaa", "key-1", T0)
        self.assertEqual(j1, j2)

    def test_stale_source_superseded(self):
        art = self._artwork()
        v1 = versions.add_version(self.c, art, None, "aaa", "/o/1.png", "", self.artist, T0)
        job = derivatives.enqueue(self.c, v1, "thumbnail", "aaa", "k1", T0)
        versions.add_version(self.c, art, None, "bbb", "/o/2.png", "", self.artist, T1)
        # 任务晚到：源已被新版本取代
        self.assertEqual(derivatives.run_job(self.c, job, T2, self._produce), "superseded")
        n = self.c.execute("SELECT COUNT(*) AS n FROM assets WHERE version_id=?",
                           (v1,)).fetchone()
        self.assertEqual(n["n"], 0)  # 不产出旧内容的派生物

    def test_late_job_after_revoke_cannot_leak(self):
        pid = self._project()
        art = self._artwork(pid)
        v = versions.add_version(self.c, art, None, "aaa", "/o/1.png", "", self.artist, T0)
        gid = licensing.grant(self.c, "客户X", ["display"], "测试授权", T0, project_id=pid)
        pending = derivatives.enqueue(self.c, v, "web", "aaa", "k9", T0)
        licensing.revoke(self.c, gid, "客户违约", "artist", T1)
        # 撤回事件已把 pending 任务置为 superseded
        row = self.c.execute("SELECT status FROM derivative_jobs WHERE id=?",
                             (pending,)).fetchone()
        self.assertEqual(row["status"], "superseded")
        self.assertEqual(derivatives.run_job(self.c, pending, T2, self._produce),
                         "already-claimed")
        # 撤回后才入队的晚到任务：执行时被授权守卫拦截
        late = derivatives.enqueue(self.c, v, "web", "aaa", "k10", T2)
        self.assertEqual(derivatives.run_job(self.c, late, T2, self._produce),
                         "superseded-license-revoked")
        n = self.c.execute("SELECT COUNT(*) AS n FROM assets WHERE version_id=?",
                           (v,)).fetchone()
        self.assertEqual(n["n"], 0)  # 两条路径都不产出资产


class LicenseRevocationTests(Base):
    def test_revoke_clears_public_surface(self):
        pid = self._project()
        art = self._artwork(pid)
        v = versions.add_version(self.c, art, None, "aaa", "/o/1.png", "", self.artist, T0)
        thumb = self._asset(v, "thumbnail")
        final = self._asset(v, "final")
        permissions.grant_matrix(self.c, thumb, "public",
                                 display=True, index=True, share_card=True)
        permissions.grant_matrix(self.c, final, "public", display=True, download=True)
        public.rebuild_listing(self.c, art, T0)
        self.assertEqual(len(public.search(self.c, "月见")), 1)
        self.assertTrue(permissions.effective(self.c, final, "public", "download"))
        gid = licensing.grant(self.c, "客户X", ["display", "download"], "商用授权", T0,
                              project_id=pid)
        licensing.revoke(self.c, gid, "违约撤回", "artist", T1)
        self.assertEqual(public.search(self.c, "月见"), [])          # 搜索消失
        with self.assertRaises(NotFound):
            public.share_card(self.c, art)                            # 分享卡 404
        self.assertFalse(permissions.effective(self.c, final, "public", "download"))
        self.assertFalse(permissions.effective(self.c, thumb, "public", "display"))


class FeaturedTests(Base):
    def test_confidential_cannot_be_featured(self):
        pid = self._project(confidential=True)
        art = self._artwork(pid)
        v = versions.add_version(self.c, art, None, "aaa", "/o/1.png", "", self.artist, T0)
        a = self._asset(v, "thumbnail")
        permissions.grant_matrix(self.c, a, "public", display=True)
        public.rebuild_listing(self.c, art, T0)  # 保密 => 不进读模型
        with self.assertRaises(Forbidden):
            public.set_featured(self.c, art, True, T0)

    def test_confidential_change_purges_featured(self):
        pid = self._project()
        art = self._artwork(pid)
        v = versions.add_version(self.c, art, None, "aaa", "/o/1.png", "", self.artist, T0)
        a = self._asset(v, "thumbnail")
        permissions.grant_matrix(self.c, a, "public", display=True)
        public.rebuild_listing(self.c, art, T0)
        public.set_featured(self.c, art, True, T0)
        self.assertEqual(len(public.homepage_featured(self.c)), 1)
        projects.set_confidential(self.c, pid, True, "artist", T1)  # 误加入后转保密
        self.assertEqual(public.homepage_featured(self.c), [])


class DraftIsolationTests(Base):
    def test_token_isolation_and_submit_invalidates(self):
        token = drafts.create_draft(self.c, T0, "a@example.com")
        drafts.update_draft(self.c, token, T0, purpose="书籍封面",
                            delivery_scope={"size": "A4", "format": ["png", "psd"]})
        d = drafts.get_draft(self.c, token)
        self.assertEqual(d["purpose"], "书籍封面")
        self.assertEqual(d["delivery_scope"]["format"], ["png", "psd"])
        with self.assertRaises(NotFound):
            drafts.get_draft(self.c, "other-browser-token")  # 别的浏览器拿不到
        rid = drafts.submit_draft(self.c, token, T1)
        self.assertIsInstance(rid, int)
        with self.assertRaises(NotFound):
            drafts.get_draft(self.c, token)  # 提交后恢复链接失效

    def test_submit_requires_purpose(self):
        token = drafts.create_draft(self.c, T0)
        with self.assertRaises(BadRequest):
            drafts.submit_draft(self.c, token, T1)


class AuditTraceTests(Base):
    def test_trace_contains_approvals_diffs_and_packages(self):
        pid = self._project()
        art = self._artwork(pid)
        v1 = versions.add_version(self.c, art, None, "aaa", "/o/1.png", "", self.artist, T0)
        versions.approve(self.c, pid, v1, self.client, T1)
        versions.add_version(self.c, art, None, "bbb", "/o/2.png", "改", self.artist, T2)
        packages.build_package(self.c, pid, self.client, T2)
        tr = audit.trace(self.c, pid)
        self.assertEqual(len(tr["approvals"]), 1)
        self.assertEqual(len(tr["change_diffs"]), 1)
        self.assertEqual(len(tr["download_packages"]), 1)
        self.assertTrue(any(e["action"] == "approve" for e in tr["events"]))


class ApiSmokeTests(Base):
    def test_draft_flow_over_http(self):
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db))
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def call(method, path, body=None):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}", method=method,
                    data=json.dumps(body).encode() if body is not None else None,
                    headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req) as r:
                        return r.status, json.loads(r.read())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read())

            status, body = call("POST", "/drafts", {"contact_email": "a@b.c"})
            self.assertEqual(status, 200)
            token = body["token"]
            status, body = call("PUT", f"/drafts/{token}",
                                {"purpose": "海报", "delivery_scope": {"size": "A3"}})
            self.assertEqual(status, 200)
            status, body = call("GET", f"/drafts/{token}")
            self.assertEqual(body["purpose"], "海报")
            status, _ = call("GET", "/drafts/someone-elses-token")
            self.assertEqual(status, 404)
            status, body = call("POST", f"/drafts/{token}/submit")
            self.assertEqual(status, 200)
            status, _ = call("GET", f"/drafts/{token}")
            self.assertEqual(status, 404)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
