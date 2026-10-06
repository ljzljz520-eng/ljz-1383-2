"""后台 API：纯标准库 http.server 实现的 JSON 接口层。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import (audit, derivatives, drafts, freezing, licensing, packages,
               permissions, projects, public, scheduling, versions)
from .db import Database
from .errors import DomainError
from .util import utcnow


def make_handler(db):
    def create_user(body):
        with db.tx():
            cur = db.conn.execute("INSERT INTO users(role,name,email) VALUES(?,?,?)",
                                  (body["role"], body["name"], body.get("email")))
        return {"id": cur.lastrowid}

    def create_project(body):
        with db.tx():
            pid = projects.create_project(
                db.conn, body["title"], body.get("freeze_mode", "stage"),
                body.get("quote_cents", 0), body.get("revision_limit", 0),
                body.get("deadline"), utcnow(), body.get("confidential", False),
                body.get("commission_id"), body.get("stage_revision_limit", 0))
        return {"id": pid}

    def create_artwork(body):
        with db.tx():
            cur = db.conn.execute(
                "INSERT INTO artworks(project_id,title,description,created_at)"
                " VALUES(?,?,?,?)",
                (body.get("project_id"), body["title"], body.get("description", ""),
                 utcnow()))
        return {"id": cur.lastrowid}

    def add_version(body, artwork_id):
        with db.tx():
            vid = versions.add_version(db.conn, int(artwork_id), body.get("stage_id"),
                                       body["file_hash"], body.get("storage_path", ""),
                                       body.get("note", ""), body.get("actor_id"), utcnow())
        return {"id": vid}

    def approve(body, project_id):
        with db.tx():
            aid = versions.approve(db.conn, int(project_id), body["version_id"],
                                   body["client_id"], utcnow(), body.get("note", ""))
        return {"id": aid}

    def approval_status(_body, artwork_id):
        return versions.approval_status(db.conn, int(artwork_id))

    def freeze(body, project_id, stage_id):
        with db.tx():
            freezing.freeze_stage(db.conn, int(stage_id), utcnow())
        return {"frozen": True}

    def freeze_all(body, project_id):
        with db.tx():
            freezing.freeze_project(db.conn, int(project_id), utcnow())
        return {"frozen": True}

    def revision(body, project_id, stage_id):
        with db.tx():
            freezing.request_revision(db.conn, int(project_id), int(stage_id),
                                      body.get("actor", "api"), utcnow())
        return {"ok": True}

    def amendment(body, project_id):
        with db.tx():
            freezing.purchase_amendment(db.conn, int(project_id), body["stage_id"],
                                        body["extra_revisions"], body["extra_cents"],
                                        body.get("actor", "api"), utcnow())
        return {"ok": True}

    def confirm(body, project_id):
        with db.tx():
            return projects.confirm_acceptance(db.conn, int(project_id), body["party"],
                                               body["version_id"], utcnow())

    def confidential(body, project_id):
        with db.tx():
            projects.set_confidential(db.conn, int(project_id), body["confidential"],
                                      body.get("actor", "api"), utcnow())
        return {"ok": True}

    def create_draft(body):
        with db.tx():
            token = drafts.create_draft(db.conn, utcnow(), body.get("contact_email"))
        return {"token": token}

    def get_draft(body, token):
        return drafts.get_draft(db.conn, token)

    def update_draft(body, token):
        with db.tx():
            drafts.update_draft(db.conn, token, utcnow(), **body)
        return {"ok": True}

    def submit_draft(body, token):
        with db.tx():
            return {"id": drafts.submit_draft(db.conn, token, utcnow())}

    def hold(body):
        with db.tx():
            hid = scheduling.hold_slot(db.conn, body["slot_start"], body["slot_end"],
                                       body["expires_at"], utcnow(),
                                       body.get("project_id"))
        return {"id": hid}

    def confirm_hold(body, hold_id):
        with db.tx():
            return {"status": scheduling.confirm_hold(db.conn, int(hold_id), utcnow())}

    def enqueue_derivative(body):
        with db.tx():
            jid = derivatives.enqueue(db.conn, body["version_id"], body["role"],
                                      body["source_hash"], body["idempotency_key"], utcnow())
        return {"id": jid}

    def run_derivative(body, job_id):
        def produce(job):
            return (f"/cdn/{job['version_id']}/{job['role']}.webp", job["source_hash"])
        with db.tx():
            return {"result": derivatives.run_job(db.conn, int(job_id), utcnow(), produce)}

    def set_perm(body, asset_id):
        with db.tx():
            for audience, actions in body["matrix"].items():
                for action, allowed in actions.items():
                    permissions.set_permission(db.conn, int(asset_id), audience,
                                               action, allowed)
            ctx = permissions.asset_context(db.conn, int(asset_id))
            if ctx:
                public.rebuild_listing(db.conn, ctx["artwork_id"], utcnow())
        return {"ok": True}

    def grant_license(body):
        with db.tx():
            lid = licensing.grant(db.conn, body["licensee"], body["scope"],
                                  body.get("terms", ""), utcnow(),
                                  body.get("project_id"), body.get("artwork_id"))
        return {"id": lid}

    def revoke_license(body, grant_id):
        with db.tx():
            licensing.revoke(db.conn, int(grant_id), body.get("reason", ""),
                             body.get("actor", "api"), utcnow())
        return {"ok": True}

    def rebuild(body, artwork_id):
        with db.tx():
            return {"listed": public.rebuild_listing(db.conn, int(artwork_id), utcnow())}

    def search(body):
        return {"results": public.search(db.conn, body.get("q", ""))}

    def share_card(body, artwork_id):
        return public.share_card(db.conn, int(artwork_id))

    def feature(body):
        with db.tx():
            public.set_featured(db.conn, body["artwork_id"], body.get("on", True),
                                utcnow(), body.get("position", 0))
        return {"ok": True}

    def featured(body):
        return {"items": public.homepage_featured(db.conn)}

    def package(body, project_id):
        with db.tx():
            return packages.build_package(db.conn, int(project_id),
                                          body["requester_id"], utcnow())

    def trace(body, project_id):
        return audit.trace(db.conn, int(project_id))

    routes = [
        ("POST", r"^/users$", create_user),
        ("POST", r"^/projects$", create_project),
        ("POST", r"^/artworks$", create_artwork),
        ("POST", r"^/artworks/(\d+)/versions$", add_version),
        ("POST", r"^/artworks/(\d+)/rebuild$", rebuild),
        ("GET", r"^/artworks/(\d+)/approval-status$", approval_status),
        ("POST", r"^/projects/(\d+)/approvals$", approve),
        ("POST", r"^/projects/(\d+)/stages/(\d+)/freeze$", freeze),
        ("POST", r"^/projects/(\d+)/freeze$", freeze_all),
        ("POST", r"^/projects/(\d+)/stages/(\d+)/revisions$", revision),
        ("POST", r"^/projects/(\d+)/amendments$", amendment),
        ("POST", r"^/projects/(\d+)/confirm$", confirm),
        ("POST", r"^/projects/(\d+)/confidential$", confidential),
        ("POST", r"^/projects/(\d+)/package$", package),
        ("GET", r"^/projects/(\d+)/audit$", trace),
        ("POST", r"^/drafts$", create_draft),
        ("GET", r"^/drafts/([^/]+)$", get_draft),
        ("PUT", r"^/drafts/([^/]+)$", update_draft),
        ("POST", r"^/drafts/([^/]+)/submit$", submit_draft),
        ("POST", r"^/holds$", hold),
        ("POST", r"^/holds/(\d+)/confirm$", confirm_hold),
        ("POST", r"^/derivative-jobs$", enqueue_derivative),
        ("POST", r"^/derivative-jobs/(\d+)/run$", run_derivative),
        ("POST", r"^/assets/(\d+)/permissions$", set_perm),
        ("POST", r"^/licenses$", grant_license),
        ("POST", r"^/licenses/(\d+)/revoke$", revoke_license),
        ("GET", r"^/search$", search),
        ("GET", r"^/share-card/(\d+)$", share_card),
        ("POST", r"^/featured$", feature),
        ("GET", r"^/featured$", featured),
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                path = self.path.split("?", 1)[0]
                if method == "GET" and "?" in self.path:
                    from urllib.parse import parse_qs, urlparse
                    body.update({k: v[0] for k, v in
                                 parse_qs(urlparse(self.path).query).items()})
                for m, pattern, fn in routes:
                    if m != method:
                        continue
                    match = re.match(pattern, path)
                    if match:
                        self._send(200, fn(body, *match.groups()))
                        return
                self._send(404, {"error": "not_found"})
            except DomainError as e:
                self._send(e.status, {"error": e.code, "message": str(e)})
            except Exception as e:  # 兜底：不裸断连接
                self._send(500, {"error": "internal", "message": str(e)})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

    return Handler


def serve(db_path=":memory:", host="127.0.0.1", port=8000):
    db = Database(db_path)
    server = ThreadingHTTPServer((host, port), make_handler(db))
    print(f"serving on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    serve()
