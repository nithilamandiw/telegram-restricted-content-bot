import pytest

from telegram_downloader.links import (
    InvalidLink,
    InviteLink,
    MessageLink,
    parse_invite_link,
    parse_message_link,
)


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://t.me/channel_name/123", MessageLink("channel_name", 123)),
        ("t.me/channel_name/123?single", MessageLink("channel_name", 123)),
        ("https://t.me/s/channel_name/123", MessageLink("channel_name", 123)),
        ("https://telegram.me/channel_name/123", MessageLink("channel_name", 123)),
        ("https://t.me/c/1234567890/987", MessageLink(-1001234567890, 987)),
        ("https://t.me/c/1234567890/10/987?single", MessageLink(-1001234567890, 987)),
        ("https://t.me/channel_name/10/987", MessageLink("channel_name", 987)),
        ("https://t.me/c/555/12", MessageLink(-1000000000555, 12)),
    ],
)
def test_message_links(link, expected):
    assert parse_message_link(link) == expected


@pytest.mark.parametrize(
    "link",
    [
        "",
        "https://example.org/channel/1",
        "https://t.me.evil.test/channel/1",
        "https://t.me@evil.test/channel/1",
        "https://user@t.me/channel/1",
        "https://t.me:8081/channel/1",
        "https://t.me/+invite",
        "https://t.me/joinchat/invite",
        "https://t.me/channel/0",
        "https://t.me/c/0/1",
        "https://t.me/channel/-1",
        "https://t.me/channel/1/2/3",
        "https://t.me/channel/1?comment=2",
        "https://t.me/channel/1 https://t.me/channel/2",
        "file:///etc/passwd",
        "https://t.me/channel/%31",
        "https://t.me/channel/١",
        "https://t.me/",
    ],
)
def test_rejects_invalid_or_ambiguous_links(link):
    with pytest.raises(InvalidLink):
        parse_message_link(link)


@pytest.mark.parametrize(
    "link",
    [
        "https://t.me/channel/1?comment=",
        "https://t.me/channel/1?comment",
        "https://t.me/channel/2147483648",
        "https://t.me/c/9999999999999999999999/1",
    ],
)
def test_rejects_out_of_range_ids_and_empty_comment_parameters(link):
    with pytest.raises(InvalidLink):
        parse_message_link(link)


@pytest.mark.parametrize(
    "link",
    [
        "https://t.me/+AbCd_123-xyz",
        "https://t.me/joinchat/AbCd_123-xyz",
        "t.me/+AbCd_123-xyz",
        "https://telegram.me/+AbCd_123-xyz/",
    ],
)
def test_invite_formats(link):
    assert parse_invite_link(link) == InviteLink("AbCd_123-xyz")


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.test/+HASH",
        "https://t.me.evil.test/+HASH",
        "https://user@t.me/+HASH",
        "https://t.me/+HASH/extra",
        "https://t.me/+HASH?comment=1",
        "https://t.me/+HASH#fragment",
        "https://t.me/+",
        "https://t.me/joinchat/",
        "https://t.me/+哈希",
        "file:///+HASH",
        "https://t.me/+one https://t.me/+two",
        "https://t.me/channel/12",
    ],
)
def test_invite_parser_rejects_other_inputs(link):
    assert parse_invite_link(link) is None
