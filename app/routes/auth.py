from typing import get_args

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ratelimit
from app.db import get_db
from app.integrations.geocode import (resolve_user_location_source, shows_exact_location,
                                     sync_post_locations)
from app.integrations.provider import CallsignProvider, CallsignRecord, get_provider
from app.models import Ham, User
from app.schemas import (
    LocationSource,
    LoginRequest,
    SignupRequest,
    TokenResponse,
    UserSelf,
)
from app.security import create_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _location_source(user: User) -> str | None:
    """users.location_source when the user has a location and the value is
    one the API defines (a hand-edited row must not turn /api/me into a 500)."""
    if user.lat is None or user.lon is None:
        return None
    return user.location_source if user.location_source in get_args(LocationSource) else None


def user_self(user: User) -> UserSelf:
    return UserSelf(
        callsign=user.callsign,
        name=user.name,
        email=user.email,
        address_line=user.address_line,
        city=user.city,
        state=user.state,
        zip=user.zip,
        grid=user.grid,
        lat=user.lat,
        lon=user.lon,
        range_miles=user.range_miles,
        map_precision="address" if shows_exact_location(user) else "grid",
        location_source=_location_source(user),
        created_at=user.created_at,
    )


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def signup(
    body: SignupRequest,
    request: Request,
    db: Session = Depends(get_db),
    provider: CallsignProvider = Depends(get_provider),
):
    # every attempt counts (a taken callsign too): each one hashes a password
    # and asks the callsign provider and the geocoder
    ratelimit.hit((ratelimit.SIGNUP_PER_ADDRESS, ratelimit.client_address(request)))
    if db.scalar(select(User).where(User.callsign == body.callsign)):
        raise HTTPException(status.HTTP_409_CONFLICT, "that callsign is already registered")

    record = provider.lookup(body.callsign)
    ham = None
    if record is None:
        # fall back to the local weekly FCC ULS import (works even if the
        # live provider is unreachable)
        ham = db.get(Ham, body.callsign)
        if ham is not None:
            record = CallsignRecord(
                callsign=ham.callsign, name=ham.name, address_line=ham.street,
                city=ham.city, state=ham.state, zip=ham.zip,
                license_class=ham.license_class, expires=ham.expires,
                source="FCC ULS (weekly snapshot)",
            )
    if record is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"callsign {body.callsign} was not found in the FCC database — check the spelling",
        )

    # User-supplied (possibly edited) fields win; fall back to the record.
    name = body.name.strip() or record.name
    address_line = body.address_line.strip() or record.address_line
    city = body.city.strip() or record.city
    state = body.state.strip() or record.state
    zip_ = body.zip.strip() or record.zip
    grid = body.grid.strip() or record.grid

    # street pin (same address only) -> provider coordinates -> grid ->
    # Nominatim -> ZIP anchor; see resolve_user_location_source
    placed = resolve_user_location_source(db, body.callsign, grid, address_line, city, state, zip_,
                                          record=record)

    user = User(
        callsign=body.callsign,
        password_hash=hash_password(body.password),
        email=body.email,
        name=name,
        address_line=address_line,
        city=city,
        state=state,
        zip=zip_,
        grid=grid,
        lat=placed[0] if placed else None,
        lon=placed[1] if placed else None,
        location_source=placed[2] if placed else None,
        range_miles=25,
        map_precision=body.map_precision,
    )
    db.add(user)
    db.commit()
    if not shows_exact_location(user) and sync_post_locations(db, user):
        db.commit()  # a new account has no posts; kept so 'grid' is honoured on every path
    db.refresh(user)
    return TokenResponse(token=create_token(user), user=user_self(user))


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """Wrong passwords are throttled per callsign (5 in 5 minutes, 20 an
    hour) and every attempt per client address (20 a minute): 429 with
    Retry-After, before any password is checked."""
    call = body.callsign
    per_call = ((ratelimit.LOGIN_FAILS_SHORT, call), (ratelimit.LOGIN_FAILS_LONG, call))
    ratelimit.check(*per_call)
    ratelimit.hit((ratelimit.LOGIN_PER_ADDRESS, ratelimit.client_address(request)))
    user = db.scalar(select(User).where(User.callsign == call))
    if user is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no account registered for {call} on this server — sign up first",
        )
    if not verify_password(body.password, user.password_hash):
        if ratelimit.enabled():
            for t, k in per_call:
                t.add(k)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "incorrect password")
    for t, k in per_call:
        t.clear(k)
    return TokenResponse(token=create_token(user), user=user_self(user))
