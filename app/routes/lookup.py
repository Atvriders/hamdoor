from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.integrations.provider import CallsignProvider, get_provider
from app.models import Ham
from app.schemas import LookupResponse, normalize_callsign

router = APIRouter(prefix="/api/lookup", tags=["lookup"])


@router.get("/{callsign}", response_model=LookupResponse)
def lookup(
    callsign: str,
    provider: CallsignProvider = Depends(get_provider),
    db: Session = Depends(get_db),
):
    """Public autofill endpoint used by the signup form.

    Prefers the live provider (callook.info — fresher for brand-new licenses)
    and falls back to the local weekly FCC ULS import, which also supplies an
    email address when the licensee published one.
    """
    try:
        cs = normalize_callsign(callsign)
    except ValueError:
        # malformed callsigns are "not found", not a server error
        return LookupResponse(found=False, callsign=callsign.upper())

    ham = db.get(Ham, cs)
    record = provider.lookup(cs)

    if record is None and ham is None:
        return LookupResponse(found=False, callsign=cs)

    if record is None:
        # local ULS import only
        return LookupResponse(
            found=True,
            callsign=cs,
            name=ham.name,
            address_line=ham.street,
            city=ham.city,
            state=ham.state,
            zip=ham.zip,
            email=ham.email,
            license_class=ham.license_class,
            expires=ham.expires,
            source="FCC ULS (weekly snapshot)",
        )

    return LookupResponse(
        found=True,
        callsign=record.callsign,
        name=record.name,
        address_line=record.address_line,
        city=record.city,
        state=record.state,
        zip=record.zip,
        email=ham.email if ham else "",
        grid=record.grid,
        license_class=record.license_class,
        expires=record.expires,
        source=record.source + (" + FCC ULS email" if ham and ham.email else ""),
    )
