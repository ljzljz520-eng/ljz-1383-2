"""One-click demo data used by the admin UI button (idempotent by title)."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    Artist,
    AssetKind,
    Client,
    FileAsset,
    FreezeMode,
    LicenseGrant,
    Project,
    Right,
    Stage,
    Work,
    WorkVersion,
)
from ..security import require_admin
from ..services import storage
from ..services.catalog import reindex
from ..services.images import enqueue, render_placeholder

router = APIRouter(prefix="/api/admin", tags=["seed"],
                   dependencies=[Depends(require_admin)])


@router.post("/seed")
def seed(db: Session = Depends(get_db)):
    if db.query(Artist).first():
        return {"ok": True, "note": "already seeded"}
    import secrets

    artist = Artist(name="小林", bio="自由插画师，关注童书与独立出版")
    db.add(artist)
    client = Client(name="蒲公英童书", email="edit@pugongying.com",
                    access_token=secrets.token_urlsafe(24))
    db.add(client)
    db.flush()

    # public work: sketch version (process only) and final (display licensed)
    pub = Work(artist_id=artist.id, title="星夜绘本",
               summary="从铅笔草图到数字成稿的完整过程", featured=True)
    secret = Work(artist_id=artist.id, title="未公开品牌联名",
                  summary="保密项目", confidential=True, featured=True)
    db.add_all([pub, secret])
    db.flush()

    def add_version(work: Work, seq: int, label: str, color):
        v = WorkVersion(work_id=work.id, seq=seq, label=label)
        db.add(v)
        db.flush()
        data = render_placeholder(f"{work.title} {label}", color=color)
        digest, size = storage.put(data)
        a = FileAsset(version_id=v.id, kind=AssetKind.MASTER,
                      filename=f"master-v{seq}.png", media_type="image/png",
                      sha256=digest, size_bytes=size)
        db.add(a)
        db.flush()
        return v, a

    v1, m1 = add_version(pub, 1, "v1 铅笔草图", (170, 170, 170))
    v2, m2 = add_version(pub, 2, "v2 上色稿", (110, 132, 200))

    # per-asset licensing (the crux): final master display-only public;
    # sketch master no public rights; thumbnail derived later with no license
    db.add(LicenseGrant(asset_id=m2.id, scope="public",
                        right=Right.DISPLAY, granted=True, note="公开展示，不提供下载"))
    db.add(LicenseGrant(asset_id=m2.id, scope=f"client:{client.id}",
                        right=Right.DOWNLOAD, granted=True, note="客户交付下载"))
    # explicit public denial for the sketch
    db.add(LicenseGrant(asset_id=m1.id, scope="public",
                        right=Right.DISPLAY, granted=False, note="草图不公开展示"))
    # queued thumbnail task (demonstrates async + unlicensed derivative)
    enqueue(db, m2, v2.id, AssetKind.THUMBNAIL)

    # confidential work assets (even if granted, confidentiality hides them)
    sv, sm = add_version(secret, 1, "v1 保密稿", (60, 60, 80))
    db.add(LicenseGrant(asset_id=sm.id, scope="public",
                        right=Right.DISPLAY, granted=True))

    # stage-mode project with realistic commercial terms
    project = Project(
        artist_id=artist.id, client_id=client.id, work_id=pub.id,
        title="蒲公英绘本封面委托", freeze_mode=FreezeMode.STAGE,
        price_cents=80000, edit_rounds_included=2,
        due_date=datetime.now(timezone.utc) + timedelta(days=14))
    db.add(project)
    db.flush()
    db.add_all([
        Stage(project_id=project.id, seq=1, name="草图确认",
              edit_rounds_included=1),
        Stage(project_id=project.id, seq=2, name="上色精修",
              edit_rounds_included=1),
    ])
    reindex(db)
    db.commit()
    return {"ok": True, "client_token": client.access_token,
            "work_ids": [pub.id, secret.id], "project_id": project.id}
