"""Admin tools (You -> Admin): find a member, reset their password, let them
log in again after too many wrong passwords, delete their account (never an
admin's: see app.routes.users.refuse_if_admin).

Admins are the members whose callsigns are listed in ADMIN_CALLSIGNS
(app.security.is_admin): only someone who can change the server's
configuration can make one. Every endpoint answers 401 without a valid token
and 403 to every other member, before it looks at the callsign, so no one
else can learn who has an account. Each action an admin takes is logged as
one INFO line starting "[admin]"; a temporary password never is."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import ratelimit
from app.db import get_db
from app.integrations.geocode import shows_exact_location
from app.models import Comment, Post, User
from app.routes.users import delete_account
from app.schemas import AdminPasswordReset, AdminUser, AdminUserList, normalize_callsign
from app.security import admin_callsigns, hash_password, require_admin, temporary_password

log = logging.getLogger(__name__)

# the router-wide dependency guards any endpoint added here later as well;
# FastAPI resolves it once per request, together with the endpoints' own
router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

USERS_MAX_LIMIT = 200


def announce_admins(db: Session) -> list[str]:
    """At startup: log who the admins are, and warn about each listed
    callsign that has no account yet. Sign up checks only that a callsign is
    in FCC records, so whoever signs up with such a callsign first becomes an
    admin. Returns those callsigns."""
    admins = admin_callsigns()
    if not admins:
        return []
    log.info("[admin] admins (ADMIN_CALLSIGNS): %s", ", ".join(sorted(admins)))
    have = set(db.scalars(select(User.callsign).where(User.callsign.in_(admins))))
    missing = sorted(admins - have)
    for call in missing:
        log.warning("[admin] %s is in ADMIN_CALLSIGNS but has no account here yet: whoever signs up as %s first "
                    "becomes an admin. Sign up as %s now, or take it out of ADMIN_CALLSIGNS.", call, call, call)
    return missing


def _member(db: Session, callsign: str) -> User:
    """The member with this callsign (any case), or 404."""
    try:
        call = normalize_callsign(callsign)
    except ValueError:
        call = ""
    user = db.scalar(select(User).where(User.callsign == call)) if call else None
    if user is None:
        raise HTTPException(404, f"There's no hamdoor account for {call}." if call
                            else "There's no hamdoor account for that callsign.")
    return user


def _counts(db: Session, column, ids: list[int]) -> dict[int, int]:
    """{author id: rows} for posts or comments written by these members."""
    if not ids:
        return {}
    rows = db.execute(select(column, func.count()).where(column.in_(ids)).group_by(column))
    return {uid: n for uid, n in rows}


@router.get("/users", response_model=AdminUserList)
def list_users(
    q: str = Query("", max_length=128, description="a callsign prefix or part of a name, any case"),
    limit: int = Query(50, description="clamped to 1-200"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Members sorted by callsign: everyone when q is empty, else those whose
    callsign starts with q or whose name contains it (any case, Unicode
    aware). total counts every match; users holds the first `limit`."""
    limit = max(1, min(USERS_MAX_LIMIT, limit))
    q = q.strip()
    rows = db.execute(select(User.id, User.callsign, User.name).order_by(User.callsign)).all()
    if q:
        prefix, part = q.upper(), q.casefold()
        rows = [r for r in rows if r.callsign.upper().startswith(prefix) or part in (r.name or "").casefold()]
    ids = [r.id for r in rows[:limit]]
    members = db.scalars(select(User).where(User.id.in_(ids)).order_by(User.callsign)).all() if ids else []
    posts, comments = _counts(db, Post.author_id, ids), _counts(db, Comment.author_id, ids)
    admins = admin_callsigns()
    return AdminUserList(total=len(rows), users=[
        AdminUser(
            callsign=u.callsign,
            name=u.name or "",
            email=u.email or "",
            city=u.city or "",
            state=u.state or "",
            created_at=u.created_at,
            is_admin=u.callsign.upper() in admins,
            posts=posts.get(u.id, 0),
            comments=comments.get(u.id, 0),
            map_precision="address" if shows_exact_location(u) else "grid",
            must_change_password=bool(u.must_change_password),
            locked=ratelimit.login_locked(u.callsign),
        )
        for u in members
    ])


@router.post("/users/{callsign}/reset-password", response_model=AdminPasswordReset)
def reset_password(
    callsign: str,
    response: Response,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Give the member a new temporary password, returned here once for the
    admin to pass on. hamdoor keeps only its bcrypt hash and never logs it.
    Their tokens carry the old password's fingerprint, so every session they
    have ends; they are asked to choose a new password when they log in
    (must_change_password), and their log-in throttles are cleared."""
    user = _member(db, callsign)
    password = temporary_password()
    user.password_hash = hash_password(password)
    user.must_change_password = 1
    db.commit()
    ratelimit.unlock_login(user.callsign)
    log.info("[admin] %s reset the password of %s", admin.callsign, user.callsign)
    response.headers["Cache-Control"] = "no-store"
    return AdminPasswordReset(callsign=user.callsign, temporary_password=password)


@router.post("/users/{callsign}/unlock", status_code=204)
def unlock(callsign: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Clear the wrong passwords counted against the member's callsign, so
    they can log in again at once."""
    user = _member(db, callsign)
    ratelimit.unlock_login(user.callsign)
    log.info("[admin] %s unlocked %s", admin.callsign, user.callsign)
    return Response(status_code=204)


@router.delete("/users/{callsign}", status_code=204)
def delete_user(callsign: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Delete the member's account as DELETE /api/me does: their posts (with
    every comment on them), their comments and the account. Not your own:
    that goes through You -> Delete account, with your password. Not another
    admin's either (400): while their callsign is in ADMIN_CALLSIGNS,
    whoever signed up with it next would be an admin (refuse_if_admin)."""
    user = _member(db, callsign)
    if user.id == admin.id:
        raise HTTPException(400, "Use You → Delete account to delete your own account.")
    call = user.callsign
    delete_account(db, user)  # refuses an admin's account
    log.info("[admin] %s deleted the account %s", admin.callsign, call)
    return Response(status_code=204)
