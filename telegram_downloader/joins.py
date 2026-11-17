from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from telethon import TelegramClient, errors, functions, types, utils

from .config import Settings
from .errors import SourceAccessError, TransferError, user_error
from .links import parse_message_link
from .store import Invitation, Job, JobStore

logger = logging.getLogger(__name__)
POLL_SECONDS = 60
PENDING_MESSAGE = (
    "Join request sent; waiting for a channel admin to approve the shared account. "
    "Send the post link now. If it is inaccessible, I will wait for approval for up to "
    "24 hours after you submit that post."
)
MISMATCH = (
    "The post does not belong to your selected invite's channel. Send the correct invite first."
)


class JoinService:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        client: TelegramClient,
        notify: Callable[[Job, str], Awaitable[None]],
        transfers_ready: asyncio.Event,
    ):
        self.settings = settings
        self.store = store
        self.client = client
        self.notify = notify
        self.transfers_ready = transfers_ready
        self.wakeup = asyncio.Event()
        self.lock = asyncio.Lock()
        self.cooldown_until = 0.0

    async def _check(self, invite_hash: str):
        """Checks are safe to retry. A flood wait defers all invite RPCs, not transfers."""
        for attempt in range(3):
            try:
                async with asyncio.timeout(45):
                    return await self.client(functions.messages.CheckChatInviteRequest(invite_hash))
            except errors.FloodWaitError as exc:
                self.cooldown_until = time.time() + exc.seconds + 1
                raise
            except (ConnectionError, TimeoutError, errors.ServerError):
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** (attempt + 1))
        raise AssertionError("Unreachable")

    def _joined(self, invite_hash: str, chat) -> Invitation:
        if not isinstance(chat, (types.Channel, types.Chat)):
            raise TransferError("This invite does not identify a supported channel or group.")
        # Persist the access hash so private post IDs resolve after approval and restart.
        self.client.session.process_entities([chat])
        return self.store.save_invite(
            invite_hash,
            "joined",
            peer_id=utils.get_peer_id(chat),
            next_check_at=time.time() + POLL_SECONDS,
        )

    async def join(self, user_id: int, invite_hash: str) -> str:
        async with self.lock:
            invite = self.store.get_invite(invite_hash)
            now = time.time()
            if invite is None:
                invite = self.store.save_invite(invite_hash, "checking")
            self.store.remember_invite(user_id, invite_hash)
            if invite.next_check_at > now:
                if invite.state == "pending":
                    return PENDING_MESSAGE
                if invite.state == "joined":
                    return (
                        "The shared account already joined this channel/group. Send the post link."
                    )
                if invite.state == "failed":
                    raise TransferError(invite.error or "This invite could not be used.")
            if self.cooldown_until > now:
                raise TransferError(
                    "Telegram is rate limiting joins. Please try this invite later."
                )
            try:
                info = await self._check(invite_hash)
                if isinstance(info, types.ChatInviteAlready):
                    self._joined(invite_hash, info.chat)
                    self.wakeup.set()
                    return (
                        "The shared account is a member of this channel/group. Send the post link."
                    )
                if getattr(info, "subscription_pricing", None) is not None:
                    raise TransferError("This is a paid invite. The bot will not make payments.")
                if invite.state == "pending":
                    # A pending import must not be resubmitted even after a restart.
                    self.store.save_invite(invite_hash, "pending", next_check_at=now + POLL_SECONDS)
                    return PENDING_MESSAGE
                # Record before the RPC: a crash or lost response must not duplicate a join request.
                self.store.save_invite(invite_hash, "pending", next_check_at=now + POLL_SECONDS)
                try:
                    async with asyncio.timeout(45):
                        await self.client(functions.messages.ImportChatInviteRequest(invite_hash))
                except errors.InviteRequestSentError:
                    self.wakeup.set()
                    return PENDING_MESSAGE
                except errors.UserAlreadyParticipantError:
                    pass
                except (ConnectionError, TimeoutError, errors.ServerError):
                    return (
                        "Telegram's join response was lost. Send the post link; I will check "
                        "membership and wait up to 24 hours rather than repeat the join request."
                    )
                # Never infer membership from a preview or unrelated chats in Updates.
                try:
                    info = await self._check(invite_hash)
                except (ConnectionError, TimeoutError, errors.ServerError, errors.FloodWaitError):
                    self.store.save_invite(
                        invite_hash,
                        "pending",
                        next_check_at=max(time.time() + POLL_SECONDS, self.cooldown_until),
                    )
                    return "Join submitted. Send the post link; I will verify access automatically."
                if isinstance(info, types.ChatInviteAlready):
                    self._joined(invite_hash, info.chat)
                    self.wakeup.set()
                    return "Joined the channel/group using the shared account. Send the post link."
                self.wakeup.set()
                return PENDING_MESSAGE
            except Exception as exc:
                text = user_error(exc)
                if isinstance(exc, errors.FloodWaitError):
                    self.cooldown_until = time.time() + exc.seconds + 1
                self.store.save_invite(
                    invite_hash,
                    "failed",
                    error=text,
                    next_check_at=max(time.time() + POLL_SECONDS, self.cooldown_until),
                )
                logger.warning("Invite operation failed (%s)", type(exc).__name__)
                raise TransferError(text) from None

    async def _matches(self, job: Job, peer_id: int) -> bool:
        peer = parse_message_link(job.link).peer
        if isinstance(peer, str):
            peer = utils.get_peer_id(await self.client.get_entity(peer))
        return peer == peer_id

    async def defer(self, job: Job) -> bool:
        """Called only for an inaccessible source, using its submission-time invite."""
        invite = self.store.get_invite(job.invite_hash) if job.invite_hash else None
        if invite is None:
            return False
        if invite.state == "failed":
            raise TransferError(invite.error or "The selected invite failed. Send a new invite.")
        if invite.state == "joined":
            if not await self._matches(job, invite.peer_id):
                raise TransferError(MISMATCH)
            raise SourceAccessError()
        if not job.approval_deadline or job.approval_deadline <= time.time():
            raise TransferError(
                "Approval wait expired after 24 hours. Send the invite and post again."
            )
        if self.store.set_state(job.id, "awaiting_approval", "Waiting for channel admin approval."):
            self.wakeup.set()
            await self.notify(
                job,
                f"Job #{job.id}: awaiting admin approval. I will check every 60 seconds "
                "and resume automatically, for up to 24 hours from post submission. "
                f"Use /cancel {job.id} to stop waiting.",
            )
        return True

    async def _finish(self, job: Job, state: str, error: str) -> None:
        if self.store.update_waiting(job.id, state, error):
            await self.notify(job, f"Job #{job.id}: {error}")

    async def _promote(self, invite: Invitation) -> None:
        for job in self.store.waiting_jobs(invite.invite_hash):
            if not job.approval_deadline or job.approval_deadline <= time.time():
                await self._finish(job, "expired", "Approval wait expired after 24 hours.")
                continue
            try:
                matches = await self._matches(job, invite.peer_id)
            except errors.FloodWaitError:
                raise
            except errors.ServerError:
                raise
            except errors.RPCError as exc:
                await self._finish(job, "failed", user_error(exc))
                continue
            except ValueError:
                await self._finish(
                    job, "failed", "Could not match this post to the invited channel."
                )
                continue
            if not matches:
                await self._finish(job, "failed", MISMATCH)
            elif job.approval_deadline <= time.time():
                await self._finish(job, "expired", "Approval wait expired after 24 hours.")
            elif self.store.update_waiting(job.id, "queued"):
                self.transfers_ready.set()
                await self.notify(job, f"Job #{job.id}: approval received; download queued.")

    async def check_pending(self) -> None:
        # Expiration is independent of rate limits or a slow membership RPC.
        for job in self.store.waiting_jobs():
            if job.user_id not in self.settings.allowed_user_ids:
                self.store.update_waiting(job.id, "cancelled")
            elif not job.approval_deadline or job.approval_deadline <= time.time():
                await self._finish(
                    job,
                    "expired",
                    "Approval wait expired after 24 hours. "
                    "Send the invite and post again to retry.",
                )
        hashes = dict.fromkeys(job.invite_hash for job in self.store.waiting_jobs())
        for invite_hash in hashes:
            async with self.lock:
                # A cancelled last subscriber must not cause another membership check.
                if not self.store.waiting_jobs(invite_hash):
                    continue
                invite = self.store.get_invite(invite_hash)
                if invite is None:
                    for job in self.store.waiting_jobs(invite_hash):
                        await self._finish(job, "failed", "Send the private invite again first.")
                    continue
                if invite.state == "failed":
                    for job in self.store.waiting_jobs(invite_hash):
                        await self._finish(job, "failed", invite.error or "The invite failed.")
                    continue
                if max(invite.next_check_at, self.cooldown_until) > time.time():
                    continue
                try:
                    if invite.state == "joined":
                        await self._promote(invite)
                    else:
                        info = await self._check(invite_hash)
                        if isinstance(info, types.ChatInviteAlready):
                            await self._promote(self._joined(invite_hash, info.chat))
                        else:
                            self.store.save_invite(
                                invite_hash, "pending", next_check_at=time.time() + POLL_SECONDS
                            )
                except errors.FloodWaitError as exc:
                    self.cooldown_until = time.time() + exc.seconds + 1
                    self.store.save_invite(
                        invite_hash,
                        "joined" if invite.state == "joined" else "pending",
                        next_check_at=self.cooldown_until,
                    )
                except (ConnectionError, TimeoutError, errors.ServerError):
                    self.store.save_invite(
                        invite_hash,
                        "joined" if invite.state == "joined" else "pending",
                        next_check_at=time.time() + POLL_SECONDS,
                    )
                except Exception as exc:
                    text = user_error(exc)
                    self.store.save_invite(invite_hash, "failed", error=text)
                    logger.warning("Membership check failed (%s)", type(exc).__name__)
                    for job in self.store.waiting_jobs(invite_hash):
                        await self._finish(job, "failed", text)

    async def run(self) -> None:
        while True:
            self.wakeup.clear()
            await self.check_pending()
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=POLL_SECONDS)
            except TimeoutError:
                pass
