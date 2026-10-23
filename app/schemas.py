import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

CALLSIGN_RE = re.compile(r"^[A-Za-z0-9]{3,7}[A-Za-z0-9]?$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

POST_CATEGORIES = ("general", "for-sale", "nets-events", "help-wanted", "elmer")

# longest post body (the composer's maxlength) and the users table's columns
POST_BODY_MAX = 10000
NAME_MAX, EMAIL_MAX, ADDRESS_MAX, CITY_MAX, STATE_MAX, ZIP_MAX, GRID_MAX = 128, 255, 255, 128, 32, 16, 16
PASSWORD_INPUT_MAX = 256  # a password being checked (sign up and changes allow 128)

# bidirectional embedding, override and isolate controls: an unterminated
# U+202E in a member's name reverses the app's own text around it
_BIDI_CONTROLS = dict.fromkeys([*range(0x202A, 0x202F), *range(0x2066, 0x206A)])


def strip_bidi_controls(value: str | None) -> str | None:
    return value.translate(_BIDI_CONTROLS) if isinstance(value, str) else value

# where other members see you: your stored location (the FCC licence
# address), or the centre of your 6-character Maidenhead subsquare
MapPrecision = Literal["address", "grid"]

# where a member's stored location came from (resolve_user_location's
# order): the FCC street geocode of their licence address, the callsign
# provider's coordinates, their grid-square centre, a Nominatim geocode of
# their profile address, or a ZIP anchor
LocationSource = Literal["licence", "provider", "grid", "geocoded", "zip"]

# an event time: local date-time without a zone, minutes (seconds accepted
# and dropped), e.g. "2026-10-28T20:00" — what <input type="datetime-local"> sends
_EVENT_AT_RE = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}:[0-9]{2})(?::([0-9]{2}))?$")


def normalize_event_at(value: str | None) -> str | None:
    """'YYYY-MM-DDTHH:MM' (or None). Rejects zones, offsets, date-only
    values and impossible dates (2026-02-30T20:00, 24:00)."""
    if value is None:
        return None
    m = _EVENT_AT_RE.match(value.strip())
    if m is None:
        raise ValueError("event_at must be a local date and time like 2026-10-28T20:00 (no time zone)")
    try:
        datetime.strptime(f"{m.group(1)}T{m.group(2)}:{m.group(3) or '00'}", "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        raise ValueError("event_at is not a real date and time") from None
    return f"{m.group(1)}T{m.group(2)}"


def normalize_callsign(value: str) -> str:
    cs = (value or "").strip().upper()
    if not (3 <= len(cs) <= 8) or not cs.isalnum():
        raise ValueError("invalid callsign")
    return cs


# ---------- auth ----------


class SignupRequest(BaseModel):
    callsign: str
    password: str = Field(min_length=8, max_length=128)
    email: str = Field(max_length=EMAIL_MAX)
    # autofilled from the provider, but the user may edit before submitting
    name: str = Field(default="", max_length=NAME_MAX)
    address_line: str = Field(default="", max_length=ADDRESS_MAX)
    city: str = Field(default="", max_length=CITY_MAX)
    state: str = Field(default="", max_length=STATE_MAX)
    zip: str = Field(default="", max_length=ZIP_MAX)
    grid: str = Field(default="", max_length=GRID_MAX)
    map_precision: MapPrecision = "address"

    @field_validator("callsign")
    @classmethod
    def _cs(cls, v: str) -> str:
        return normalize_callsign(v)

    @field_validator("name", "address_line", "city", "state", "zip", "grid")
    @classmethod
    def _text(cls, v: str) -> str:
        return strip_bidi_controls(v)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip()
        if not EMAIL_RE.match(v):
            raise ValueError("invalid email address")
        return v


class LoginRequest(BaseModel):
    callsign: str
    password: str = Field(max_length=PASSWORD_INPUT_MAX)

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
    # FCC grant date (MM/DD/YYYY) and the new-ham flag, from the local FCC
    # row ("" / false when it has none)
    granted: str = ""
    new: bool = False
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
    map_precision: MapPrecision
    # where lat/lon came from; null when there is no location — or for a
    # location placed before hamdoor recorded this that it could not attribute
    location_source: LocationSource | None = None
    created_at: datetime


class UserPublic(BaseModel):
    """What other users see — never street address or email."""

    callsign: str
    name: str
    # map_precision 'grid': the subsquare they are shown at (never more precise)
    grid: str
    # map_precision 'address' (default): the user's stored location — their
    # FCC address (street geocode or the provider's coordinates), else
    # grid-square centre or ZIP area. 'grid': the centre of their 6-character
    # Maidenhead subsquare, with distance_miles rounded to whole miles.
    lat: float | None
    lon: float | None
    distance_miles: float | None = None
    map_precision: MapPrecision = "address"
    # from the member's FCC row ("" / false when the directory has none)
    license_class: str = ""
    new: bool = False


class UserUpdate(BaseModel):
    email: str | None = Field(default=None, max_length=EMAIL_MAX)
    name: str | None = Field(default=None, max_length=NAME_MAX)
    address_line: str | None = Field(default=None, max_length=ADDRESS_MAX)
    city: str | None = Field(default=None, max_length=CITY_MAX)
    state: str | None = Field(default=None, max_length=STATE_MAX)
    zip: str | None = Field(default=None, max_length=ZIP_MAX)
    grid: str | None = Field(default=None, max_length=GRID_MAX)
    range_miles: int | None = Field(default=None, ge=5, le=500)
    map_precision: MapPrecision | None = None

    @field_validator("name", "address_line", "city", "state", "zip", "grid")
    @classmethod
    def _text(cls, v: str | None) -> str | None:
        return strip_bidi_controls(v)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not EMAIL_RE.match(v):
            raise ValueError("invalid email address")
        return v

    @model_validator(mode="after")
    def _precision_not_null(self):
        # omitted = unchanged; an explicit null is not a precision
        if "map_precision" in self.model_fields_set and self.map_precision is None:
            raise ValueError("map_precision must be 'address' or 'grid'")
        return self


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=PASSWORD_INPUT_MAX)
    new_password: str = Field(min_length=8, max_length=128)


class AccountDelete(BaseModel):
    password: str = Field(max_length=PASSWORD_INPUT_MAX)


# ---------- posts / comments ----------


class PostCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=POST_BODY_MAX)
    category: str = "general"
    # optional, on any category (the UI offers it for nets-events)
    event_at: str | None = None
    repeats_weekly: bool = False

    @field_validator("title", "body")
    @classmethod
    def _text(cls, v: str) -> str:
        return strip_bidi_controls(v)

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in POST_CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(POST_CATEGORIES)}")
        return v

    @field_validator("event_at")
    @classmethod
    def _event_at(cls, v: str | None) -> str | None:
        return normalize_event_at(v)

    @model_validator(mode="after")
    def _repeats_need_a_time(self):
        if self.repeats_weekly and self.event_at is None:
            raise ValueError("repeats_weekly needs an event_at")
        return self


class PostUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=POST_BODY_MAX)
    category: str | None = None
    # omitted = unchanged; event_at null clears the event (and its weekly repeat)
    event_at: str | None = None
    repeats_weekly: bool | None = None

    @field_validator("title", "body")
    @classmethod
    def _text(cls, v: str | None) -> str | None:
        return strip_bidi_controls(v)

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower()
        if v not in POST_CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(POST_CATEGORIES)}")
        return v

    @field_validator("event_at")
    @classmethod
    def _event_at(cls, v: str | None) -> str | None:
        return normalize_event_at(v)

    @model_validator(mode="after")
    def _repeats_not_null(self):
        if "repeats_weekly" in self.model_fields_set and self.repeats_weekly is None:
            raise ValueError("repeats_weekly must be true or false")
        return self


class CommentOut(BaseModel):
    id: int
    post_id: int
    author_callsign: str
    author_name: str
    body: str
    created_at: datetime


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)

    @field_validator("body")
    @classmethod
    def _text(cls, v: str) -> str:
        return strip_bidi_controls(v)


class PostOut(BaseModel):
    id: int
    title: str
    body: str
    category: str
    author_callsign: str
    author_name: str
    # for a 'grid' author: the locator they are shown at (as /api/operators/nearby)
    author_grid: str
    # the author's FCC licence class and new-ham flag ("" / false when absent)
    author_class: str = ""
    author_new: bool = False
    # a 'grid' author's posts sit at their subsquare centre, with
    # distance_miles in whole miles (never their exact point)
    lat: float | None
    lon: float | None
    distance_miles: float | None = None
    event_at: str | None = None
    repeats_weekly: bool = False
    created_at: datetime
    updated_at: datetime
    comment_count: int = 0
    comments: list[CommentOut] = []
