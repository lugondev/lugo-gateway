"""Adapts the real TikTokLive client's callback API to the connect()/events()/
close() protocol TikTokLiveIngestor expects (see ingestor.LiveClientProtocol),
normalizing its event objects into SocialEvent.

Mapping helpers (map_comment etc.) are pure and duck-typed so they're unit
testable without the real TikTokLive proto classes; only TikTokLiveClientAdapter
itself touches the actual library.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from app.services.livehost.ingestor import RoomOfflineError
from app.services.livehost.schemas import SocialEvent


def avatar_url(user) -> str | None:
    thumb = getattr(user, "avatar_thumb", None)
    urls = getattr(thumb, "m_urls", None) if thumb else None
    return urls[0] if urls else None


def map_comment(event) -> SocialEvent:
    return SocialEvent(
        id=str(uuid.uuid4()), kind="comment",
        user_id=event.user.unique_id, user_name=event.user.nickname,
        user_avatar_url=avatar_url(event.user), text=event.comment,
        timestamp=time.time(),
    )


def map_gift(event) -> SocialEvent | None:
    if event.streaking:
        return None  # wait for the streak to finish so gift_value is final
    return SocialEvent(
        id=str(uuid.uuid4()), kind="gift",
        user_id=event.user.unique_id, user_name=event.user.nickname,
        user_avatar_url=avatar_url(event.user), gift_name=event.gift.name,
        gift_value=event.repeat_count * event.gift.diamond_count,
        timestamp=time.time(),
    )


def map_like(event) -> SocialEvent:
    return SocialEvent(
        id=str(uuid.uuid4()), kind="like",
        user_id=event.user.unique_id, user_name=event.user.nickname,
        user_avatar_url=avatar_url(event.user), like_count=event.count,
        timestamp=time.time(),
    )


def map_follow(event) -> SocialEvent:
    return SocialEvent(
        id=str(uuid.uuid4()), kind="follow",
        user_id=event.user.unique_id, user_name=event.user.nickname,
        user_avatar_url=avatar_url(event.user), timestamp=time.time(),
    )


def map_share(event) -> SocialEvent:
    return SocialEvent(
        id=str(uuid.uuid4()), kind="share",
        user_id=event.user.unique_id, user_name=event.user.nickname,
        user_avatar_url=avatar_url(event.user), timestamp=time.time(),
    )


class TikTokLiveClientAdapter:
    def __init__(self, unique_id: str) -> None:
        from TikTokLive import TikTokLiveClient

        self._client = TikTokLiveClient(unique_id=unique_id)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._register_handlers()

    def _register_handlers(self) -> None:
        from TikTokLive.events import CommentEvent, FollowEvent, GiftEvent, LikeEvent, ShareEvent

        self._client.on(CommentEvent, self._on_comment)
        self._client.on(GiftEvent, self._on_gift)
        self._client.on(LikeEvent, self._on_like)
        self._client.on(FollowEvent, self._on_follow)
        self._client.on(ShareEvent, self._on_share)

        from TikTokLive.events.custom_events import DisconnectEvent, LiveEndEvent

        self._client.on(DisconnectEvent, self._on_disconnect)
        self._client.on(LiveEndEvent, self._on_disconnect)

    async def _on_comment(self, event) -> None:
        await self._queue.put(map_comment(event))

    async def _on_gift(self, event) -> None:
        mapped = map_gift(event)
        if mapped is not None:
            await self._queue.put(mapped)

    async def _on_like(self, event) -> None:
        await self._queue.put(map_like(event))

    async def _on_follow(self, event) -> None:
        await self._queue.put(map_follow(event))

    async def _on_share(self, event) -> None:
        await self._queue.put(map_share(event))

    async def _on_disconnect(self, event) -> None:
        await self._queue.put(None)  # signals TikTokLiveIngestor to reconnect immediately

    async def connect(self) -> None:
        from TikTokLive.client.errors import UserNotFoundError, UserOfflineError

        try:
            await self._client.start(fetch_live_check=True)
        except (UserOfflineError, UserNotFoundError) as exc:
            raise RoomOfflineError(str(exc)) from exc

    async def events(self):
        while True:
            yield await self._queue.get()

    async def close(self) -> None:
        await self._client.disconnect(close_client=True)
