from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.integrations.geocode import resolve_user_location_source, shows_exact_location, sync_post_locations
from app.integrations.provider import CallsignProvider, get_provider
from app.models import Comment, Post, User
from app.routes.auth import user_self
from app.schemas import AccountDelete, PasswordChange, TokenResponse, UserSelf, UserUpdate
from app.security import create_token, get_current_user, hash_password, verify_password

router = APIRouter(prefix="/api/me", tags=["users"])


def _place(db: Session, user: User, record=None) -> None:
    """Re-resolve the user's stored location (and where it came from)."""
    placed = resolve_user_location_source(db, user.callsign, user.grid, user.address_line,
                                          user.city, user.state, user.zip, record=record)
    user.lat, user.lon, user.location_source = placed if placed else (None, None, None)


@router.get("", response_model=UserSelf)
def get_me(user: User = Depends(get_current_user)):
    return user_self(user)


@router.patch("", response_model=UserSelf)
def update_me(body: UserUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    updates = body.model_dump(exclude_none=True)
    address_touched = any(
        k in updates for k in ("address_line", "city", "state", "zip", "grid")
    )
    precision_before = shows_exact_location(user)
    for key, value in updates.items():
        setattr(user, key, value.strip() if isinstance(value, str) else value)
    if address_touched:
        _place(db, user)
    # posts follow the precision and the point (see sync_post_locations):
    # switching to 'grid' moves every post to the subsquare centre, switching
    # back restores the precise point, a 'grid' member's posts follow their
    # shown point when it moves, and an 'address' member's posts with no
    # location get theirs once the member has one again
    sync_post_locations(db, user, restore=shows_exact_location(user) and not precision_before)
    db.commit()
    db.refresh(user)
    return user_self(user)


@router.delete("", status_code=204)
def delete_me(body: AccountDelete, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Delete the account: the member's comments, their posts (with every
    comment on them) and the member. Their FCC directory entry is public
    record and stays. Needs the current password."""
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(400, "password is incorrect")
    uid = user.id
    own_posts = select(Post.id).where(Post.author_id == uid)
    db.execute(delete(Comment).where(or_(Comment.author_id == uid, Comment.post_id.in_(own_posts)))
               .execution_options(synchronize_session=False))
    db.execute(delete(Post).where(Post.author_id == uid).execution_options(synchronize_session=False))
    db.execute(delete(User).where(User.id == uid).execution_options(synchronize_session=False))
    db.commit()
    db.expunge(user)
    return Response(status_code=204)


@router.post("/password", response_model=TokenResponse)
def change_password(
    body: PasswordChange,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Every token carries a fingerprint of the password it was issued
    under, so changing it ends every other session (a stolen token stops
    working); the session that made the change gets a fresh token here."""
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    db.refresh(user)
    return TokenResponse(token=create_token(user), user=user_self(user))


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
    _place(db, user, record=record)
    # a 'grid' member's posts follow their shown point; an 'address' member's
    # posts with no location get the new point
    sync_post_locations(db, user)
    db.commit()
    db.refresh(user)
    return user_self(user)


def sync_user_locations(db: Session) -> int:
    """Re-place registered users from the directory (street pins, fixed
    joins). Runs at startup and after every ULS import; the logic lives in
    app.integrations.geocode so the importer can call it too."""
    from app.integrations.geocode import sync_user_locations as _sync

    return _sync(db)
