"""Callsign lookup provider interface.

New sources (QRZ XML, HamQTH, country-specific ULS dumps, ...) implement
CallsignProvider and can be swapped in via `get_provider`.
"""

from dataclasses import dataclass, field


@dataclass
class CallsignRecord:
    callsign: str
    name: str = ""
    address_line: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    grid: str = ""
    license_class: str = ""
    expires: str = ""
    source: str = ""
    extra: dict = field(default_factory=dict)


class CallsignProvider:
    """Protocol for callsign lookup sources."""

    name = "base"

    def lookup(self, callsign: str) -> CallsignRecord | None:
        raise NotImplementedError


def get_provider() -> CallsignProvider:
    from app.integrations.callook import CallookProvider

    return CallookProvider()
