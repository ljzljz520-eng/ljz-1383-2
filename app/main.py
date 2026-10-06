from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .database import Base, SessionLocal, engine
from .routers import (
    admin_people,
    admin_projects,
    admin_trace,
    admin_works,
    auth,
    client,
    drafts,
    public,
    seed,
)
from .services.catalog import reindex


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    config.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="青年插画师工作室 · 作品/委托/交付管理", lifespan=lifespan)

api_routers = [
    auth.router, admin_people.router, admin_works.router,
    admin_projects.router, admin_trace.router, client.router,
    drafts.router, public.router, seed.router,
]
for r in api_routers:
    app.include_router(r)


@app.get("/health")
def health():
    return {"ok": True}


_web_dir = Path(__file__).resolve().parent.parent / "web"
if _web_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_web_dir)), name="static")

    @app.get("/")
    def index():
        return FileResponse(str(_web_dir / "index.html"))

    @app.get("/{page}.html")
    def html_page(page: str):
        f = _web_dir / f"{page}.html"
        if not f.exists():
            from fastapi import HTTPException
            raise HTTPException(404, "页面不存在")
        return FileResponse(str(f))
