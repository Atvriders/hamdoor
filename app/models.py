from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    callsign: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    address_line: Mapped[str] = mapped_column(String(255), default="")
    city: Mapped[str] = mapped_column(String(128), default="")
    state: Mapped[str] = mapped_column(String(32), default="")
    zip: Mapped[str] = mapped_column(String(16), default="")
    grid: Mapped[str] = mapped_column(String(16), default="")
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    range_miles: Mapped[int] = mapped_column(Integer, default=25)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # where other members see this one: 'address' = the stored location (the
    # FCC licence address), 'grid' = the centre of its 6-character Maidenhead
    # subsquare. Existing databases get the column from a startup migration.
    map_precision: Mapped[str] = mapped_column(Text, nullable=False, default="address",
                                               server_default="address")
    # where the stored lat/lon came from: 'licence' (FCC street geocode),
    # 'provider' (callook coordinates), 'grid' (grid-square centre),
    # 'geocoded' (Nominatim), 'zip' (ZIP anchor); NULL = no location, or a
    # point placed before this column existed that could not be attributed
    location_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 1 after an admin reset the password (the member is asked for a new one at
    # every log in until they change it); 0 again once they do. Existing
    # databases get the column from a startup migration.
    must_change_password: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                                      server_default=text("0"))

    posts: Mapped[list["Post"]] = relationship(back_populates="author")
    comments: Mapped[list["Comment"]] = relationship(back_populates="author")


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(32), default="general", index=True)
    # where the post is: for an author with map precision 'address', a
    # snapshot of their stored location when the post was created (a post
    # with none gets theirs once they have one); for 'grid', the centre of the
    # subsquare they are shown at (never their exact point). See
    # geocode.sync_post_locations
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    # optional event time: local date-time without zone, "2026-10-28T20:00"
    event_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    repeats_weekly: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                                server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    author: Mapped[User] = relationship(back_populates="posts")
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="post", cascade="all, delete-orphan", order_by="Comment.created_at"
    )


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    post: Mapped[Post] = relationship(back_populates="comments")
    author: Mapped[User] = relationship(back_populates="comments")


class Ham(Base):
    """One row per active US amateur license, imported from the FCC ULS
    weekly dump. Location is the geocoded street address when available
    (falling back to the ZIP centroid) — street address and email text are
    stored only for signup prefill and are never exposed via the API."""

    __tablename__ = "hams"

    callsign: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    street: Mapped[str] = mapped_column(String(255), default="")
    city: Mapped[str] = mapped_column(String(128), default="")
    state: Mapped[str] = mapped_column(String(32), default="")
    zip: Mapped[str] = mapped_column(String(16), default="", index=True)
    email: Mapped[str] = mapped_column(String(255), default="")
    license_class: Mapped[str] = mapped_column(String(8), default="")
    expires: Mapped[str] = mapped_column(String(16), default="")
    granted: Mapped[str] = mapped_column(String(16), default="")  # FCC grant date
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    # "address" = Census-geocoded street location, "zip" = centroid fallback
    loc_source: Mapped[str] = mapped_column(String(16), default="")


class Repeater(Base):
    """One amateur repeater (or linked simplex node), merged from HearHam,
    BrandMeister and IRLP. Coordinates are WGS84; frequencies in MHz.
    Rebuilt by the repeaters refresh job; owned by app/integrations/repeaters.py."""

    __tablename__ = "repeaters"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # "{source}:{source_id}"
    source: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[str] = mapped_column(String(32))
    source_url: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255), default="")
    callsign: Mapped[str] = mapped_column(String(32), default="", index=True)
    output_mhz: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_mhz: Mapped[float | None] = mapped_column(Float, nullable=True)
    offset_mhz: Mapped[float | None] = mapped_column(Float, nullable=True)  # signed input - output
    band: Mapped[str] = mapped_column(String(8), default="other", index=True)
    modes: Mapped[str] = mapped_column(String(64), default="")  # comma list
    tone_up: Mapped[float | None] = mapped_column(Float, nullable=True)
    tone_down: Mapped[float | None] = mapped_column(Float, nullable=True)
    dcs: Mapped[str | None] = mapped_column(String(8), nullable=True)
    tone_burst: Mapped[float | None] = mapped_column(Float, nullable=True)
    dmr_color_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dmr_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    dmr_network: Mapped[str | None] = mapped_column(String(32), nullable=True)
    talkgroups: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    nxdn_ran: Mapped[int | None] = mapped_column(Integer, nullable=True)
    p25_nac: Mapped[str | None] = mapped_column(String(8), nullable=True)
    m17_can: Mapped[int | None] = mapped_column(Integer, nullable=True)
    city: Mapped[str] = mapped_column(String(128), default="")
    county: Mapped[str] = mapped_column(String(128), default="")
    state: Mapped[str] = mapped_column(String(32), default="")
    country: Mapped[str] = mapped_column(String(8), default="")
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    precise: Mapped[bool] = mapped_column(Integer, default=0)
    location_quality: Mapped[str] = mapped_column(String(16), default="unknown")
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    use: Mapped[str] = mapped_column(String(16), default="unknown")
    kind: Mapped[str] = mapped_column(String(16), default="repeater")
    echolink: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allstar: Mapped[int | None] = mapped_column(Integer, nullable=True)
    irlp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wiresx: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    last_update: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fetched_at: Mapped[str] = mapped_column(String(32), default="")
    also_in: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list


class Geocode(Base):
    """Persistent address → lat/lon cache (Census batch geocoder results).
    Survives the weekly hams table rebuild so only new/changed addresses
    ever need geocoding."""

    __tablename__ = "geocodes"

    address_key: Mapped[str] = mapped_column(String(400), primary_key=True)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    quality: Mapped[str] = mapped_column(String(16), default="")
