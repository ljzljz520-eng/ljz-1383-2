"""Relational data model for the illustrator studio platform.

Domain highlights
-----------------
* FileAsset is IMMUTABLE: uploading a revision creates a new WorkVersion + new
  FileAsset rows. Approvals/acceptances point at concrete asset SHA256s, so a
  later upload can never silently become "the approved file".
* Permissions are per FileAsset (master / thumbnail / process) and per scope
  (public vs. a specific client). There is intentionally NO single
  is_public flag on a work or version.
* Projects may be frozen whole ("package") or stage-by-stage ("stage");
  revision constraints (price, edit rounds, deadline) live on stage/project.
* Acceptance requires BOTH artist and client to confirm the same item set.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- enums
class AssetKind(str, enum.Enum):
    MASTER = "master"          # finished artwork file
    PROCESS = "process"        # sketch / process image (草图/过程图)
    THUMBNAIL = "thumbnail"    # derived thumbnail (派生缩略图)


class Right(str, enum.Enum):
    DISPLAY = "display"        # 公开展示权
    DOWNLOAD = "download"      # 下载权
    SUBLICENSE = "sublicense"  # 再授权权


class FreezeMode(str, enum.Enum):
    PACKAGE = "package"  # 整包冻结：冻结整个项目
    STAGE = "stage"      # 逐阶段冻结：只冻结单个阶段


class StageStatus(str, enum.Enum):
    DRAFT = "draft"
    OPEN = "open"
    FROZEN = "frozen"


class RevisionStatus(str, enum.Enum):
    REQUESTED = "requested"
    APPROVED = "approved"       # change order approved, revision may proceed
    REJECTED = "rejected"       # constraints refused it (frozen / no rounds ...)
    DELIVERED = "delivered"


class TaskStatus(str, enum.Enum):
    QUEUED = "queued"
    DONE = "done"
    FAILED = "failed"


class ApprovalRole(str, enum.Enum):
    CLIENT = "client"
    ARTIST = "artist"


class SlotStatus(str, enum.Enum):
    HELD = "held"
    BOOKED = "booked"
    EXPIRED = "expired"


class DraftKind(str, enum.Enum):
    THUMBNAIL = "thumbnail"
    PROCESS = "process"


# --------------------------------------------------------------------------- people
class Artist(Base):
    __tablename__ = "artists"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    bio: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # token embedded in the approval/acceptance portal link (capability URL)
    access_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------- works
class Work(Base):
    __tablename__ = "works"
    id: Mapped[int] = mapped_column(primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id"))
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, default="")
    # Confidential works must never be shown in the public site, homepage
    # featured selection, search index or share cards.
    confidential: Mapped[bool] = mapped_column(Boolean, default=False)
    # Curator flag for homepage. Only effective when work is non-confidential
    # AND every rendered item actually has a public display grant.
    featured: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    artist: Mapped[Artist] = relationship()
    versions: Mapped[list["WorkVersion"]] = relationship(
        back_populates="work", cascade="all, delete-orphan"
    )


class WorkVersion(Base):
    __tablename__ = "work_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(120))   # e.g. "v1 草图", "v2 成稿"
    seq: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    work: Mapped[Work] = relationship(back_populates="versions")
    assets: Mapped[list["FileAsset"]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("work_id", "seq", name="uq_version_seq"),)


class FileAsset(Base):
    """Immutable binary file + per-asset licenses.

    master / process / thumbnail are *different rows* so that, e.g. the
    finished master may be downloadable while the process sketches or derived
    thumbnails have display-only or no rights at all.
    """

    __tablename__ = "file_assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("work_versions.id", ondelete="CASCADE"))
    kind: Mapped[AssetKind] = mapped_column(Enum(AssetKind))
    filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    # When this file was produced by an async derivation task, link it so a
    # "late" task arrival is traceable.
    source_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("file_assets.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    version: Mapped[WorkVersion] = relationship(back_populates="assets", foreign_keys=[version_id])
    grants: Mapped[list["LicenseGrant"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan"
    )


class LicenseGrant(Base):
    """One scope+right decision for one asset.

    scope='public'   -> anonymous visitors
    scope='client:<id>' -> a specific client (never readable by public APIs)

    Explicit ``granted=False`` is a DENIAL and overrides inherited/broader
    grants. ``revoked_at`` instantly removes the right everywhere except for
    the immutable snapshot stored inside a sealed delivery package.
    """

    __tablename__ = "license_grants"
    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("file_assets.id", ondelete="CASCADE"))
    scope: Mapped[str] = mapped_column(String(40), index=True)
    right: Mapped[Right] = mapped_column(Enum(Right))
    granted: Mapped[bool] = mapped_column(Boolean, default=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    asset: Mapped[FileAsset] = relationship(back_populates="grants")

    __table_args__ = (
        UniqueConstraint("asset_id", "scope", "right", name="uq_grant"),
    )


class DerivationTask(Base):
    """Async job that derives a thumbnail/process image from a source asset."""

    __tablename__ = "derivation_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_asset_id: Mapped[int] = mapped_column(ForeignKey("file_assets.id"))
    target_version_id: Mapped[int] = mapped_column(ForeignKey("work_versions.id"))
    kind: Mapped[AssetKind] = mapped_column(Enum(AssetKind))
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.QUEUED)
    output_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("file_assets.id"), nullable=True
    )
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# --------------------------------------------------------------------------- projects
class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id"))
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    work_id: Mapped[int | None] = mapped_column(ForeignKey("works.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    freeze_mode: Mapped[FreezeMode] = mapped_column(Enum(FreezeMode), default=FreezeMode.STAGE)
    # PACKAGE-level freeze flag
    frozen: Mapped[bool] = mapped_column(Boolean, default=False)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Commercial constraints applied to revisions
    price_cents: Mapped[int] = mapped_column(Integer, default=0)
    edit_rounds_included: Mapped[int] = mapped_column(Integer, default=2)
    rounds_used: Mapped[int] = mapped_column(Integer, default=0)
    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    client: Mapped[Client] = relationship()
    work: Mapped[Work | None] = relationship()
    stages: Mapped[list["Stage"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    slots: Mapped[list["ScheduleSlot"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Stage(Base):
    __tablename__ = "stages"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    seq: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[StageStatus] = mapped_column(Enum(StageStatus), default=StageStatus.OPEN)
    # stage-specific constraints (override project level when set)
    edit_rounds_included: Mapped[int] = mapped_column(Integer, default=0)
    rounds_used: Mapped[int] = mapped_column(Integer, default=0)
    stage_due: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    project: Mapped[Project] = relationship(back_populates="stages")
    freezes: Mapped[list["FreezeEvent"]] = relationship(
        back_populates="stage", cascade="all, delete-orphan"
    )


class FreezeEvent(Base):
    __tablename__ = "freeze_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    stage_id: Mapped[int | None] = mapped_column(
        ForeignKey("stages.id", ondelete="CASCADE"), nullable=True
    )  # null => whole-package freeze
    mode: Mapped[FreezeMode] = mapped_column(Enum(FreezeMode))
    reason: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    stage: Mapped[Stage | None] = relationship(back_populates="freezes")


class RevisionRequest(Base):
    __tablename__ = "revision_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    stage_id: Mapped[int | None] = mapped_column(ForeignKey("stages.id"), nullable=True)
    version_id: Mapped[int | None] = mapped_column(ForeignKey("work_versions.id"), nullable=True)
    requested_by: Mapped[str] = mapped_column(String(40), default="client")
    detail: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[RevisionStatus] = mapped_column(
        Enum(RevisionStatus), default=RevisionStatus.REQUESTED
    )
    # Why accepted/refused — audit of quote / rounds / deadline enforcement
    decision_reason: Mapped[str] = mapped_column(Text, default="")
    extra_charge_cents: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ChangeOrder(Base):
    """Approved commercial adjustment when included terms are exceeded."""

    __tablename__ = "change_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("revision_requests.id"))
    extra_charge_cents: Mapped[int] = mapped_column(Integer, default=0)
    extra_rounds: Mapped[int] = mapped_column(Integer, default=0)
    new_due: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accepted_by_client: Mapped[bool] = mapped_column(Boolean, default=False)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ScheduleSlot(Base):
    __tablename__ = "schedule_slots"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[SlotStatus] = mapped_column(Enum(SlotStatus), default=SlotStatus.HELD)
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    project: Mapped[Project | None] = relationship(back_populates="slots")


# --------------------------------------------------------------------- approvals
class VersionApproval(Base):
    """Client approval of an exact version+file snapshot.

    The snapshot (label/seq + every asset's sha256) is frozen at approval
    time. New uploads never mutate it; the API explicitly flags when the
    current file set differs from what was approved.
    """

    __tablename__ = "version_approvals"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("work_versions.id"))
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    version_seq: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(120))
    asset_fingerprint: Mapped[str] = mapped_column(String(64))  # hash of asset sha list
    asset_snapshot: Mapped[str] = mapped_column(Text)  # JSON [{id,kind,sha256,filename}]
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Acceptance(Base):
    """Two-party acceptance. Valid only when BOTH sides confirm the SAME
    immutable item set (version_id, asset_id, sha256)."""

    __tablename__ = "acceptances"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    # Items the parties must agree on: JSON [{version_id, asset_id, kind, sha256}]
    items_json: Mapped[str] = mapped_column(Text)
    items_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    artist_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    client_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    packages: Mapped[list["DeliveryPackage"]] = relationship(back_populates="acceptance")


class DeliveryPackage(Base):
    """Sealed, downloadable delivery.

    ``manifest_json`` is frozen at sealing time: the exact approved asset list,
    their sha256 and a snapshot of the license each had. A later license
    withdrawal cannot rewrite history, but active-right checks gate downloads.
    """

    __tablename__ = "delivery_packages"
    id: Mapped[int] = mapped_column(primary_key=True)
    acceptance_id: Mapped[int] = mapped_column(ForeignKey("acceptances.id"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    manifest_json: Mapped[str] = mapped_column(Text)
    archive_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sealed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    acceptance: Mapped[Acceptance] = relationship(back_populates="packages")


# ----------------------------------------------------------------- commission etc
class CommissionDraft(Base):
    """Not-yet-submitted commission enquiry, scoped to ONE browser session.

    Bound to a random draft_token (in a non-HttpOnly cookie + echoed in the
    body) so restoring a draft can never leak to another browser/user.
    """

    __tablename__ = "commission_drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    draft_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    intended_use: Mapped[str] = mapped_column(Text, default="")   # 用途
    delivery_scope: Mapped[str] = mapped_column(Text, default="")  # 交付范围
    contact: Mapped[str] = mapped_column(String(200), default="")
    budget_cents: Mapped[int] = mapped_column(Integer, default=0)
    submitted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SearchEntry(Base):
    """Denormalised public search index.

    Rebuilt on permission/feature changes, but the READ path always re-checks
    live grants + confidentiality, so a revoked license disappears even before
    reindex and a confidential work is never returned.
    """

    __tablename__ = "search_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), unique=True)
    title: Mapped[str] = mapped_column(String(200), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    # Snapshot of thumbnail asset id that HAD public display at index time.
    thumb_asset_id: Mapped[int | None] = mapped_column(ForeignKey("file_assets.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(80), default="anonymous")
    action: Mapped[str] = mapped_column(String(80))
    entity: Mapped[str] = mapped_column(String(80), default="")
    entity_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
