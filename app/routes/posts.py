from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.geo import bounding_box, haversine_miles
from app.db import get_db
from app.models import Comment, Post, User
from app.schemas import (
    CommentCreate,
    CommentOut,
    PostCreate,
    PostOut,
    PostUpdate,
)
from app.security import get_current_user

router = APIRouter(prefix="/api", tags=["posts"])

PAGE_SIZE = 20


def comment_out(c: Comment) -> CommentOut:
    return CommentOut(
        id=c.id,
        post_id=c.post_id,
        author_callsign=c.author.callsign,
        author_name=c.author.name,
        body=c.body,
        created_at=c.created_at,
    )


def post_out(p: Post, viewer: User | None = None, with_comments: bool = False) -> PostOut:
    dist = None
    if viewer is not None and None not in (viewer.lat, viewer.lon, p.lat, p.lon):
        dist = round(haversine_miles(viewer.lat, viewer.lon, p.lat, p.lon), 1)
    return PostOut(
        id=p.id,
        title=p.title,
        body=p.body,
        category=p.category,
        author_callsign=p.author.callsign,
        author_name=p.author.name,
        author_grid=p.author.grid,
        lat=p.lat,
        lon=p.lon,
        distance_miles=dist,
        created_at=p.created_at,
        updated_at=p.updated_at,
        comment_count=len(p.comments),
        comments=[comment_out(c) for c in p.comments] if with_comments else [],
    )


def _get_post_or_404(db: Session, post_id: int) -> Post:
    post = db.scalar(
        select(Post).options(joinedload(Post.author), joinedload(Post.comments).joinedload(Comment.author)).where(Post.id == post_id)
    )
    if post is None:
        raise HTTPException(404, "post not found")
    return post


# ---------- feed ----------


@router.get("/posts/feed", response_model=list[PostOut])
def feed(
    category: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Posts from operators within the viewer's configured range, newest first."""
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

    out = []
    for post in db.scalars(stmt).unique().all():
        dist = haversine_miles(user.lat, user.lon, post.lat, post.lon)
        if dist > user.range_miles:
            continue
        out.append((dist, post))

    start = (page - 1) * PAGE_SIZE
    return [post_out(p, user) for _, p in out[start : start + PAGE_SIZE]]


# ---------- posts CRUD ----------


@router.post("/posts", response_model=PostOut, status_code=201)
def create_post(body: PostCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    post = Post(
        author_id=user.id,
        title=body.title.strip(),
        body=body.body,
        category=body.category,
        lat=user.lat,
        lon=user.lon,
    )
    db.add(post)
    db.commit()
    return post_out(_get_post_or_404(db, post.id), user)


@router.get("/posts/{post_id}", response_model=PostOut)
def get_post(post_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return post_out(_get_post_or_404(db, post_id), user, with_comments=True)


@router.patch("/posts/{post_id}", response_model=PostOut)
def update_post(
    post_id: int,
    body: PostUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    post = _get_post_or_404(db, post_id)
    if post.author_id != user.id:
        raise HTTPException(403, "only the author can edit a post")
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(post, key, value.strip() if isinstance(value, str) else value)
    db.commit()
    return post_out(_get_post_or_404(db, post_id), user, with_comments=True)


@router.delete("/posts/{post_id}", status_code=204)
def delete_post(post_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    post = _get_post_or_404(db, post_id)
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
    _get_post_or_404(db, post_id)
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
