"""Derivation jobs: thumbnail / stylised "process" image from a source asset.

Jobs run asynchronously via a small in-process queue. A task that completes
*after* approval/delivery is fully traceable (source_asset_id) and its output
gets NO license of its own — derivatives must be licensed explicitly, so a
late thumbnail can never leak onto the public site or into an already sealed
delivery manifest.
"""
from __future__ import annotations

import hashlib
import io
from datetime import datetime, timezone

from PIL import Image, ImageOps, ImageEnhance, ImageFilter
from sqlalchemy.orm import Session

from ..models import (
    AssetKind,
    DerivationTask,
    FileAsset,
    TaskStatus,
)
from . import storage


def _load(data: bytes) -> Image.Image:
    try:
        return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        # source may be a generated placeholder png; fallback canvas
        return Image.new("RGB", (1200, 900), (230, 230, 240))


def make_thumbnail(data: bytes, size=(480, 480)) -> bytes:
    img = _load(data)
    img = ImageOps.fit(img, size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_process(data: bytes) -> bytes:
    """A stylised 'sketch/process' derivative (grayscale, posterised)."""
    img = _load(data)
    img = ImageOps.grayscale(img).convert("RGB")
    img = ImageEnhance.Contrast(img).enhance(1.8)
    img = img.filter(ImageFilter.CONTOUR)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_placeholder(title: str, w=1200, h=900, color=(110, 132, 200)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, w - 20, h - 20], outline=(255, 255, 255), width=6)
    d.text((60, h // 2), title[:60], fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def enqueue(db: Session, source_asset: FileAsset, target_version_id: int,
            kind: AssetKind) -> DerivationTask:
    task = DerivationTask(
        source_asset_id=source_asset.id,
        target_version_id=target_version_id,
        kind=kind,
        status=TaskStatus.QUEUED,
    )
    db.add(task)
    db.flush()
    return task


def _create_output(db: Session, task: DerivationTask, data: bytes) -> FileAsset:
    digest, size = storage.put(data)
    ext = "png"
    out = FileAsset(
        version_id=task.target_version_id,
        kind=task.kind,
        filename=f"{task.kind.value}-{task.source_asset_id}.{ext}",
        media_type="image/png",
        sha256=digest,
        size_bytes=size,
        source_asset_id=task.source_asset_id,
    )
    db.add(out)
    db.flush()
    task.output_asset_id = out.id
    return out


def process_task(db: Session, task: DerivationTask) -> FileAsset:
    src = db.get(FileAsset, task.source_asset_id)
    try:
        data = storage.get(src.sha256)
        if task.kind is AssetKind.THUMBNAIL:
            out_data = make_thumbnail(data)
        elif task.kind is AssetKind.PROCESS:
            out_data = make_process(data)
        else:
            raise ValueError(f"cannot derive {task.kind}")
        out = _create_output(db, task, out_data)
        task.status = TaskStatus.DONE
        task.finished_at = datetime.now(timezone.utc)
        task.detail = "derived output created; no license granted automatically"
        db.flush()
        return out
    except Exception as exc:  # pragma: no cover - defensive
        task.status = TaskStatus.FAILED
        task.detail = f"derivation failed: {exc}"
        task.finished_at = datetime.now(timezone.utc)
        db.flush()
        raise


def process_due(db: Session, limit: int = 10) -> list[FileAsset]:
    tasks = (
        db.query(DerivationTask)
        .filter(DerivationTask.status == TaskStatus.QUEUED)
        .order_by(DerivationTask.id)
        .limit(limit)
        .all()
    )
    return [process_task(db, t) for t in tasks]
