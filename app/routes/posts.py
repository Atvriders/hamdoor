from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.geo import bounding_box, haversine_miles
from app.db import get_db
from app.integrations.geocode import post_point, shown_location, shows_exact_location
from app.models import Comment, Post, User
from app.routes.hams import fcc_badges
from app.schemas import (
    CommentCreate,
    CommentOut,
    PostCreate,
    PostOut,
    PostUpdate,
    normalize_callsign,
)
from app.security import get_current_user

router = APIRouter(prefix="/api", tags=["posts"])

PAGE_SIZE = 20
_NO_BADGE = (None, False)  # no FCC row: class not listed (null), not a club ("")


def comment_out(c: Comment) -> CommentOut:
    return CommentOut(
        id=c.id,
        post_id=c.post_id,
        author_callsign=c.author.callsign,
        author_name=c.author.name,
        body=c.body,
        created_at=c.created_at,
    )


def post_place(p: Post) -> tuple[float | None, float | None, str]:
    """(lat, lon, author_grid) a post shows. A 'grid' author's post: the
    subsquare centre and locator /api/operators/nearby shows for them —
    computed from the author, so not even a row written before posts
    honoured map precision can return their exact point. Otherwise the
    post's stored point and the author's grid."""
    if not shows_exact_location(p.author):
        return shown_location(p.author)
    return (p.lat, p.lon, p.author.grid)


def post_out(p: Post, viewer: User | None = None, with_comments: bool = False,
             badge: tuple[str | None, bool] = _NO_BADGE) -> PostOut:
    """`badge` = the author's (licence class, new-ham flag), see fcc_badges."""
    lat, lon, grid = post_place(p)
    dist = None
    if viewer is not None and None not in (viewer.lat, viewer.lon, lat, lon):
        d = haversine_miles(viewer.lat, viewer.lon, lat, lon)
        dist = round(d, 1) if shows_exact_location(p.author) else float(round(d))
    return PostOut(
        id=p.id,
        title=p.title,
        body=p.body,
        category=p.category,
        author_callsign=p.author.callsign,
        author_name=p.author.name,
        author_grid=grid,
        author_class=badge[0],
        author_new=badge[1],
        lat=lat,
        lon=lon,
        distance_miles=dist,
        event_at=p.event_at,
        repeats_weekly=bool(p.repeats_weekly),
        created_at=p.created_at,
        updated_at=p.updated_at,
        comment_count=len(p.comments),
        comments=[comment_out(c) for c in p.comments] if with_comments else [],
    )


def _author_badge(db: Session, p: Post) -> tuple[str | None, bool]:
    return fcc_badges(db, [p.author.callsign]).get(p.author.callsign, _NO_BADGE)


def visible_to(post: Post, viewer: User) -> bool:
    """The feed's own rule: a member reads a post — and its thread — when it
    is theirs, or when the point it shows is within their range. Post ids
    are sequential, so without this anyone anywhere could read every post
    (and learn a grid-precision author's name and subsquare)."""
    if post.author_id == viewer.id:
        return True
    if viewer.lat is None or viewer.lon is None:
        return False
    lat, lon, _ = post_place(post)
    if lat is None or lon is None:
        return False
    return haversine_miles(viewer.lat, viewer.lon, lat, lon) <= viewer.range_miles


def _get_post_or_404(db: Session, post_id: int, viewer: User | None = None) -> Post:
    """The post; 404 when it doesn't exist or (with `viewer`) is out of
    their range — the same answer, so a far member can't tell the two apart."""
    post = db.scalar(
        select(Post).options(joinedload(Post.author), joinedload(Post.comments).joinedload(Comment.author)).where(Post.id == post_id)
    )
    if post is None or (viewer is not None and not visible_to(post, viewer)):
        raise HTTPException(404, "post not found")
    return post


# ---------- feed ----------


@router.get("/posts/feed", response_model=list[PostOut])
def feed(
    category: str | None = Query(default=None),
    author: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Posts from operators within the viewer's configured range, newest
    first. `author` (a callsign) keeps one member's posts — still only those
    within range; an unknown callsign gives an empty list."""
    if user.lat is None or user.lon is None:
        raise HTTPException(400, "set your location (grid square or address) in your profile first")

    min_lat, max_lat, min_lon, max_lon = bounding_box(user.lat, user.lon, user.range_miles)
    stmt = (
        select(Post)
        .options(joinedload(Post.author), joinedload(Post.comments))
        .where(
            Post.lat.is_not(None),
            Post.lon.is_not(None),
            Post.lat.between(min_lat, max_lat),
            Post.lon.between(min_lon, max_lon),
        )
        .order_by(Post.created_at.desc())
    )
    if category:
        stmt = stmt.where(Post.category == category)
    if author:
        try:
            author_id = db.scalar(select(User.id).where(User.callsign == normalize_callsign(author)))
        except ValueError:
            author_id = None  # not a callsign: nobody's posts
        if author_id is None:
            return []
        stmt = stmt.where(Post.author_id == author_id)

    out = []
    for post in db.scalars(stmt).unique().all():
        lat, lon, _ = post_place(post)
        if lat is None or lon is None:
            continue
        if haversine_miles(user.lat, user.lon, lat, lon) > user.range_miles:
            continue
        out.append(post)

    start = (page - 1) * PAGE_SIZE
    shown = out[start : start + PAGE_SIZE]
    badges = fcc_badges(db, (p.author.callsign for p in shown))
    return [post_out(p, user, badge=badges.get(p.author.callsign, _NO_BADGE)) for p in shown]


# ---------- posts CRUD ----------


@router.post("/posts", response_model=PostOut, status_code=201)
def create_post(body: PostCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    lat, lon = post_point(user)  # a 'grid' member's post never stores their exact point
    post = Post(
        author_id=user.id,
        title=body.title.strip(),
        body=body.body,
        category=body.category,
        lat=lat,
        lon=lon,
        event_at=body.event_at,
        repeats_weekly=int(body.repeats_weekly),
    )
    db.add(post)
    db.commit()
    post = _get_post_or_404(db, post.id)
    return post_out(post, user, badge=_author_badge(db, post))


@router.get("/posts/{post_id}", response_model=PostOut)
def get_post(post_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    post = _get_post_or_404(db, post_id, user)
    return post_out(post, user, with_comments=True, badge=_author_badge(db, post))


@router.patch("/posts/{post_id}", response_model=PostOut)
def update_post(
    post_id: int,
    body: PostUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    post = _get_post_or_404(db, post_id, user)
    if post.author_id != user.id:
        raise HTTPException(403, "only the author can edit a post")
    for key, value in body.model_dump(exclude_none=True, exclude={"event_at", "repeats_weekly"}).items():
        setattr(post, key, value.strip() if isinstance(value, str) else value)
    # event_at: omitted = unchanged, null = no event (which also ends the
    # weekly repeat, unless this request asks for one — then 422)
    fields = body.model_fields_set
    event_at = body.event_at if "event_at" in fields else post.event_at
    repeats = body.repeats_weekly if "repeats_weekly" in fields else bool(post.repeats_weekly)
    if event_at is None:
        if "repeats_weekly" in fields and repeats:
            raise HTTPException(422, "repeats_weekly needs an event_at")
        repeats = False
    post.event_at, post.repeats_weekly = event_at, int(repeats)
    db.commit()
    post = _get_post_or_404(db, post_id)
    return post_out(post, user, with_comments=True, badge=_author_badge(db, post))


@router.delete("/posts/{post_id}", status_code=204)
def delete_post(post_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    post = _get_post_or_404(db, post_id, user)
    if post.author_id != user.id:
        raise HTTPException(403, "only the author can delete a post")
    db.delete(post)
    db.commit()


# ---------- comments ----------


@router.post("/posts/{post_id}/comments", response_model=CommentOut, status_code=201)
def add_comment(
    post_id: int,
    body: CommentCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_post_or_404(db, post_id, user)
    comment = Comment(post_id=post_id, author_id=user.id, body=body.body.strip())
    db.add(comment)
    db.commit()
    db.refresh(comment)
    comment.author = user
    return comment_out(comment)


@router.delete("/comments/{comment_id}", status_code=204)
def delete_comment(comment_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    comment = db.scalar(select(Comment).where(Comment.id == comment_id))
    if comment is None:
        raise HTTPException(404, "comment not found")
    post = db.get(Post, comment.post_id)
    if comment.author_id != user.id and (post is None or post.author_id != user.id):
        raise HTTPException(403, "only the comment author or post author can delete a comment")
    db.delete(comment)
    db.commit()
