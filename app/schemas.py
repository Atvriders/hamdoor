import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

CALLSIGN_RE = re.compile(r"^[A-Za-z0-9]{3,7}[A-Za-z0-9]?$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

POST_CATEGORIES = ("general", "for-sale", "nets-events", "help-wanted", "elmer")


def normalize_callsign(value: str) -> str:
    cs = (value or "").strip().upper()
    if not (3 <= len(cs) <= 8) or not cs.isalnum():
        raise ValueError("invalid callsign")
    return cs


# ---------- auth ----------


class SignupRequest(BaseModel):
    callsign: str
    password: str = Field(min_length=8, max_length=128)
    email: str
    # autofilled from the provider, but the user may edit before submitting
    name: str = ""
    address_line: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    grid: str = ""

    @field_validator("callsign")
    @classmethod
    def _cs(cls, v: str) -> str:
        return normalize_callsign(v)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip()
        if not EMAIL_RE.match(v):
            raise ValueError("invalid email address")
        return v


class LoginRequest(BaseModel):
    callsign: str
    password: str

    @field_validator("callsign")
    @classmethod
    def _cs(cls, v: str) -> str:
        return normalize_callsign(v)


class TokenResponse(BaseModel):
    token: str
    user: "UserSelf"


# ---------- lookup ----------


class LookupResponse(BaseModel):
    found: bool
    callsign: str
    name: str = ""
    address_line: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    grid: str = ""
    license_class: str = ""
    expires: str = ""
    email: str = ""
    source: str = ""


# ---------- users ----------


class UserSelf(BaseModel):
    """What a user sees about themselves (includes private fields)."""

    callsign: str
    name: str
    email: str
    address_line: str
    city: str
    state: str
    zip: str
    grid: str
    lat: float | None
    lon: float | None
    range_miles: int
    created_at: datetime


class UserPublic(BaseModel):
    """What other users see — never street address or email."""

    callsign: str
    name: str
    grid: str
    lat: float | None  # grid-center precision only
    lon: float | None
    distance_miles: float | None = None


class UserUpdate(BaseModel):
    email: str | None = None
    name: str | None = None
    address_line: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    grid: str | None = None
    range_miles: int | None = Field(default=None, ge=5, le=500)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not EMAIL_RE.match(v):
            raise ValueError("invalid email address")
        return v


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


# ---------- posts / comments ----------


class PostCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = ""
    category: str = "general"

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in POST_CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(POST_CATEGORIES)}")
        return v


class PostUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = None
    category: str | None = None

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower()
        if v not in POST_CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(POST_CATEGORIES)}")
        return v


class CommentOut(BaseModel):
    id: int
    post_id: int
    author_callsign: str
    author_name: str
    body: str
    created_at: datetime


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class PostOut(BaseModel):
    id: int
    title: str
    body: str
    category: str
    author_callsign: str
    author_name: str
    author_grid: str
    lat: float | None
    lon: float | None
    distance_miles: float | None = None
    created_at: datetime
    updated_at: datetime
    comment_count: int = 0
    comments: list[CommentOut] = []
