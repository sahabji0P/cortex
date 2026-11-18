"""
Times, as they are kept and sent.

Every time is written in UTC. The database hands them back without a zone, so a time
read from it is given its zone again here: sent to a client without one, it could not be
told from a local time.
"""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator

__all__ = ["UtcDatetime", "in_utc"]


def in_utc(value: datetime) -> datetime:
    """The same instant, in UTC. A time without a zone was written in UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# A time in a stored document or an answer to a client.
type UtcDatetime = Annotated[datetime, AfterValidator(in_utc)]
