from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.integrations.geocode import resolve_user_location
from app.integrations.provider import CallsignProvider, CallsignRecord, get_provider
from app.models import Ham, User
from app.schemas import (
    LoginRequest,
    SignupRequest,
    TokenResponse,
    UserSelf,
)
from app.security import create_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


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
        created_at=user.created_at,
    )


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def signup(
    body: SignupRequest,
    db: Session = Depends(get_db),
    provider: CallsignProvider = Depends(get_provider),
):
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

    latlon = resolve_user_location(db, body.callsign, grid, address_line, city, state, zip_)
    if latlon is None and ham is not None and ham.lat is not None:
        latlon = (ham.lat, ham.lon)  # ZIP centroid from the ULS import

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
        lat=latlon[0] if latlon else None,
        lon=latlon[1] if latlon else None,
        range_miles=25,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return TokenResponse(token=create_token(user), user=user_self(user))


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.callsign == body.callsign))
    if user is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no account registered for {body.callsign} on this server — sign up first",
        )
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "incorrect password")
    return TokenResponse(token=create_token(user), user=user_self(user))
