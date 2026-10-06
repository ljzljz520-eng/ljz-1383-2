from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ------------------------------------------------------------------------- auth
class LoginIn(BaseModel):
    username: str
    password: str


# ------------------------------------------------------------------------ people
class ArtistIn(BaseModel):
    name: str
    bio: str = ""


class ClientIn(BaseModel):
    name: str
    email: str | None = None


# ------------------------------------------------------------------------- works
class WorkIn(BaseModel):
    artist_id: int
    title: str
    summary: str = ""
    confidential: bool = False
    featured: bool = False


class WorkPatch(BaseModel):
    title: str | None = None
    summary: str | None = None
    confidential: bool | None = None
    featured: bool | None = None


class VersionIn(BaseModel):
    label: str
    note: str = ""
    # when provided, generate a placeholder master (demo/seed) instead of upload
    placeholder_color: tuple[int, int, int] | None = None


class GrantIn(BaseModel):
    asset_id: int
    scope: str = "public"          # 'public' | 'client:<id>'
    right: str                     # display | download | sublicense
    granted: bool = True
    note: str = ""


class RevokeIn(BaseModel):
    grant_id: int


class DeriveIn(BaseModel):
    source_asset_id: int
    target_version_id: int | None = None
    kind: str                      # thumbnail | process


# ---------------------------------------------------------------------- projects
class ProjectIn(BaseModel):
    artist_id: int
    client_id: int
    work_id: int | None = None
    title: str
    freeze_mode: str = "stage"
    price_cents: int = 0
    edit_rounds_included: int = 2
    due_date: datetime | None = None


class StageIn(BaseModel):
    name: str
    edit_rounds_included: int = 0
    stage_due: datetime | None = None


class FreezeIn(BaseModel):
    reason: str = ""


class RevisionIn(BaseModel):
    stage_id: int | None = None
    version_id: int | None = None
    detail: str
    requested_by: str = "client"


class ChangeOrderIn(BaseModel):
    revision_id: int
    extra_charge_cents: int = 0
    extra_rounds: int = 1
    new_due: datetime | None = None


class AcceptChangeIn(BaseModel):
    revision_id: int
    accepted: bool


class SlotIn(BaseModel):
    starts_at: datetime
    ends_at: datetime
    ttl_hours: int | None = None
    project_id: int | None = None


class BookSlotIn(BaseModel):
    slot_id: int
    project_id: int


# ------------------------------------------------------- approvals / acceptance
class ApproveIn(BaseModel):
    version_id: int
    note: str = ""


class AcceptanceStartIn(BaseModel):
    project_id: int
    version_ids: list[int] = Field(default_factory=list)
    # explicit items optional; default = approved masters of versions
    items: list[dict] | None = None


class AcceptanceConfirmIn(BaseModel):
    acceptance_id: int
    items: list[dict]


class SealIn(BaseModel):
    acceptance_id: int


# -------------------------------------------------------------------- commission
class DraftIn(BaseModel):
    intended_use: str = ""
    delivery_scope: str = ""
    contact: str = ""
    budget_cents: int = 0
