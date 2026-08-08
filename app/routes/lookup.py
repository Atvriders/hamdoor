from fastapi import APIRouter, Depends

from app.integrations.provider import CallsignProvider, get_provider
from app.schemas import LookupResponse, normalize_callsign

router = APIRouter(prefix="/api/lookup", tags=["lookup"])


@router.get("/{callsign}", response_model=LookupResponse)
def lookup(callsign: str, provider: CallsignProvider = Depends(get_provider)):
    """Public autofill endpoint used by the signup form."""
    cs = normalize_callsign(callsign)
    record = provider.lookup(cs)
    if record is None:
        return LookupResponse(found=False, callsign=cs)
    return LookupResponse(
        found=True,
        callsign=record.callsign,
        name=record.name,
        address_line=record.address_line,
        city=record.city,
        state=record.state,
        zip=record.zip,
        grid=record.grid,
        license_class=record.license_class,
        expires=record.expires,
        source=record.source,
    )
