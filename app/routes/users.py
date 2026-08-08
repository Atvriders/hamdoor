from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.integrations.geocode import resolve_location
from app.integrations.provider import CallsignProvider, get_provider
from app.models import User
from app.routes.auth import user_self
from app.schemas import PasswordChange, UserSelf, UserUpdate
from app.security import get_current_user, hash_password, verify_password

router = APIRouter(prefix="/api/me", tags=["users"])


@router.get("", response_model=UserSelf)
def get_me(user: User = Depends(get_current_user)):
    return user_self(user)


@router.patch("", response_model=UserSelf)
def update_me(body: UserUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    updates = body.model_dump(exclude_none=True)
    address_touched = any(
        k in updates for k in ("address_line", "city", "state", "zip", "grid")
    )
    for key, value in updates.items():
        setattr(user, key, value.strip() if isinstance(value, str) else value)
    if address_touched:
        latlon = resolve_location(user.grid, user.address_line, user.city, user.state, user.zip)
        user.lat, user.lon = (latlon if latlon else (None, None))
    db.commit()
    db.refresh(user)
    return user_self(user)


@router.post("/password", status_code=204)
def change_password(
    body: PasswordChange,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    db.commit()


@router.post("/refresh-callsign", response_model=UserSelf)
def refresh_callsign(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    provider: CallsignProvider = Depends(get_provider),
):
    """Re-pull FCC data for the user's callsign and update stored fields."""
    record = provider.lookup(user.callsign)
    if record is None:
        raise HTTPException(404, f"callsign {user.callsign} not found at the provider right now")
    user.name = record.name or user.name
    user.address_line = record.address_line or user.address_line
    user.city = record.city or user.city
    user.state = record.state or user.state
    user.zip = record.zip or user.zip
    user.grid = record.grid or user.grid
    latlon = resolve_location(user.grid, user.address_line, user.city, user.state, user.zip)
    user.lat, user.lon = (latlon if latlon else (None, None))
    db.commit()
    db.refresh(user)
    return user_self(user)
