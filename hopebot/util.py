from __future__ import annotations

from typing import TYPE_CHECKING

from mautrix.types import MatrixURI


if TYPE_CHECKING:
    from mautrix.types import EventID


def room_mention(
    room_id, event_id: EventID | None = None, text: str = "", html: bool = False
):
    """Build a link to a room (or event in a room).
    html=False returns markdown.
    """
    uri = MatrixURI.build(
        room_id, event_id, ["hope.net", "matrix.org", "fairydust.space"]
    )
    if html:
        return "<a href='{}'>{}</a>".format(uri, text)
    return "[{}]({})".format(text, uri)
