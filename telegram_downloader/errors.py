from __future__ import annotations

import errno

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from telethon import errors


class TransferError(Exception):
    """An error whose message is safe to show to a requester."""


class SourceAccessError(TransferError):
    def __init__(self):
        super().__init__(
            "The shared account cannot access this source. A private post link alone cannot "
            "join a channel. Send its private invite link first, then send the post link."
        )


def user_error(exc: Exception) -> str:
    if isinstance(exc, TransferError):
        return str(exc)
    if isinstance(exc, errors.ChatForwardsRestrictedError):
        return "Telegram blocked access to protected content. This post could not be downloaded."
    if isinstance(exc, (errors.UnauthorizedError, errors.AuthKeyError)):
        return "The shared account session expired. Ask the owner to run the terminal login again."
    if isinstance(exc, (errors.ChannelPrivateError, errors.ChatAdminRequiredError)):
        return str(SourceAccessError())
    if isinstance(exc, errors.InviteHashExpiredError):
        return "This invite has expired or was revoked. Send a new invite link."
    if isinstance(exc, (errors.InviteHashInvalidError, errors.InviteHashEmptyError)):
        return "This invite is invalid. Check the link or ask the channel owner for a new one."
    if isinstance(exc, errors.UserBannedInChannelError):
        return "The shared account is banned from this channel and cannot join it."
    if isinstance(exc, errors.ChannelsTooMuchError):
        return "The shared account has reached Telegram's channel/group membership limit."
    if isinstance(exc, errors.UsersTooMuchError):
        return "This group has reached its member limit and cannot accept the shared account."
    if isinstance(exc, (errors.MessageIdInvalidError, errors.MsgIdInvalidError)):
        return "The source message is missing, deleted, or inaccessible."
    if isinstance(exc, errors.FloodWaitError):
        return "Telegram is rate limiting the shared account. Please try again later."
    if isinstance(exc, (errors.RPCError, ValueError)):
        return (
            "Telegram could not read this message. Check the link and the shared account's access."
        )
    if isinstance(exc, TelegramForbiddenError):
        return "The bot cannot send files to this chat. Unblock the bot and press Start."
    if isinstance(exc, TelegramAPIError):
        return "Telegram rejected the bot request. Check the local Bot API server and file size."
    if isinstance(exc, TimeoutError):
        return "The transfer timed out. Check the chat before retrying: content may have arrived."
    if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT}:
        return "The server has insufficient disk space. Ask its owner to free space."
    if isinstance(exc, (OSError, ConnectionError)):
        return "A connection or server storage error interrupted the transfer. Please try again."
    return "The transfer failed. Ask the bot owner to check the service logs."
