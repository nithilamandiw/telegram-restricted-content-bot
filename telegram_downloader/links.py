from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit


class InvalidLink(ValueError):
    pass


@dataclass(frozen=True)
class MessageLink:
    peer: int | str
    message_id: int


@dataclass(frozen=True)
class InviteLink:
    invite_hash: str


def parse_invite_link(text: str) -> InviteLink | None:
    """Return an invite, or None so the existing message-link parser can handle it."""
    text = text.strip()
    if text.startswith(("t.me/", "telegram.me/", "www.t.me/")):
        text = "https://" + text
    try:
        url = urlsplit(text)
        if (
            url.scheme not in {"http", "https"}
            or url.hostname not in {"t.me", "www.t.me", "telegram.me"}
            or url.username
            or url.password
            or url.port
            or url.fragment
            or url.query
        ):
            return None
        match = re.fullmatch(r"/(?:\+|joinchat/)([A-Za-z0-9_-]{1,128})/?", url.path)
        return InviteLink(match[1]) if match else None
    except ValueError:
        return None


def parse_message_link(text: str) -> MessageLink:
    """Parse message links only; never fetch arbitrary URLs or join invite links."""
    text = text.strip()
    if text.startswith(("t.me/", "telegram.me/", "www.t.me/")):
        text = "https://" + text
    try:
        url = urlsplit(text)
        if (
            url.scheme not in {"https", "http"}
            or url.hostname not in {"t.me", "www.t.me", "telegram.me"}
            or url.username
            or url.password
            or url.port
            or url.fragment
        ):
            raise ValueError
        parts = url.path.strip("/").split("/")
        if parts[0] == "s":
            parts = parts[1:]
        if parts[0] == "c":
            if len(parts) not in {3, 4} or not parts[1].isdigit() or int(parts[1]) <= 0:
                raise ValueError
            peer: int | str = -(1_000_000_000_000 + int(parts[1]))
            ids = parts[2:]
        else:
            if len(parts) not in {2, 3} or not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_]{3,31}", parts[0]
            ):
                raise ValueError
            peer = parts[0]
            ids = parts[1:]
        if any(not value.isascii() or not value.isdigit() or int(value) <= 0 for value in ids):
            raise ValueError
        # A comment link refers to a discussion message, not the linked channel post.
        if "comment" in parse_qs(url.query, keep_blank_values=True):
            raise ValueError
        if any(int(value) > 2**31 - 1 for value in ids):
            raise ValueError
        if isinstance(peer, int) and peer < -(2**63):
            raise ValueError
        return MessageLink(peer, int(ids[-1]))
    except (ValueError, IndexError):
        raise InvalidLink(
            "Send one message link, such as https://t.me/channel/123 or "
            "https://t.me/c/1234567890/123. Topic links are supported; "
            "send a private invite such as https://t.me/+HASH to join first. "
            "Comment links are not supported."
        ) from None
