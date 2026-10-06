"""Immutable file storage on local disk, content-addressed by SHA256."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .. import config


def _root() -> Path:
    config.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    return config.STORAGE_DIR


def put(data: bytes, sha256: str | None = None) -> tuple[str, int]:
    digest = sha256 or hashlib.sha256(data).hexdigest()
    # sharded by first 2 chars; same content stored once (immutable)
    d = _root() / digest[:2]
    d.mkdir(exist_ok=True)
    path = d / digest
    if not path.exists():
        path.write_bytes(data)
    return digest, len(data)


def path_for(sha256: str) -> Path:
    return _root() / sha256[:2] / sha256


def get(sha256: str) -> bytes:
    return path_for(sha256).read_bytes()


def exists(sha256: str) -> bool:
    return path_for(sha256).exists()
