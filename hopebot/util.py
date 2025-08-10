from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mautrix.types import MatrixURI


if TYPE_CHECKING:
    from mautrix.types import EventID

LOGGER = logging.getLogger(__name__)


def room_mention(
    room_id, event_id: EventID | None = None, text: str = "", html: bool = False
) -> str:
    """Build a link to a room (or event in a room).
    html=False returns markdown.
    """
    LOGGER.debug("Building room mention for %r %r %r", room_id, event_id, text)
    uri = MatrixURI.build(
        room_id, event_id, ["hope.net", "matrix.org", "fairydust.space"]
    )
    if html:
        return "<a href='{}'>{}</a>".format(uri, text)
    return "[{}]({})".format(text, uri)
