import secrets

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Artist, Client
from ..schemas import ArtistIn, ClientIn
from ..security import require_admin
from ..services import audit
from ..services.images import render_placeholder
from ..services import storage

router = APIRouter(prefix="/api/admin", tags=["admin-people"],
                   dependencies=[Depends(require_admin)])


@router.post("/artists")
def create_artist(body: ArtistIn, db: Session = Depends(get_db)):
    a = Artist(name=body.name, bio=body.bio)
    db.add(a)
    db.commit()
    audit.record(db, "artist.create", entity="artist", entity_id=a.id, detail=a.name)
    db.commit()
    return {"id": a.id, "name": a.name}


@router.get("/artists")
def list_artists(db: Session = Depends(get_db)):
    return [{"id": a.id, "name": a.name, "bio": a.bio} for a in db.query(Artist)]


@router.post("/clients")
def create_client(body: ClientIn, db: Session = Depends(get_db)):
    c = Client(name=body.name, email=body.email, access_token=secrets.token_urlsafe(24))
    db.add(c)
    db.commit()
    audit.record(db, "client.create", entity="client", entity_id=c.id, detail=c.name)
    db.commit()
    return {"id": c.id, "name": c.name, "access_token": c.access_token,
            "portal_url": f"/client.html?token={c.access_token}"}


@router.get("/clients")
def list_clients(db: Session = Depends(get_db)):
    return [{"id": c.id, "name": c.name, "email": c.email,
             "access_token": c.access_token} for c in db.query(Client)]


@router.post("/demo-placeholder")
def demo_placeholder(title: str = "demo"):
    """Helper to materialize bytes used by seed scripts/tests."""
    data = render_placeholder(title)
    digest, size = storage.put(data)
    return {"sha256": digest, "size": size}
