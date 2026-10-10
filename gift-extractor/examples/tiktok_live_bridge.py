#!/usr/bin/env python3
"""Play a converted gift animation whenever someone sends that gift on stream.

This is the last link in the chain: `giftkit build` produces the library,
`giftkit serve` renders it in OBS, and this script listens to a live room and
tells the overlay what to play.

    pip install TikTokLive
    python examples/tiktok_live_bridge.py --user someone_live

Gifts that can be sent in a streak (roses, etc.) fire once when the streak
ends, so a viewer spamming 50 roses gets one animation, not fifty.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import urllib.error
import urllib.request


def trigger(base_url: str, gift: str, repeat: int = 1) -> None:
    payload = json.dumps({"gift": gift, "repeat": repeat}).encode()
    request = urllib.request.Request(
        f"{base_url}/trigger", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read())
        print(f"  -> playing {body['asset']['src']}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"  -> no converted asset for {gift!r}; re-run `giftkit build`")
        else:
            print(f"  -> overlay refused the trigger: {exc}")
    except OSError as exc:
        print(f"  -> overlay unreachable at {base_url}: {exc}")


async def run(username: str, overlay: str) -> None:
    from TikTokLive import TikTokLiveClient
    from TikTokLive.events import ConnectEvent, GiftEvent

    client = TikTokLiveClient(unique_id=username)

    @client.on(ConnectEvent)
    async def on_connect(event: ConnectEvent) -> None:
        print(f"connected to @{event.unique_id} (room {client.room_id})")

    @client.on(GiftEvent)
    async def on_gift(event: GiftEvent) -> None:
        # streakable gifts repeat; wait for the streak to finish before playing
        if event.gift.streakable and not event.streaking:
            count = event.repeat_count
        elif not event.gift.streakable:
            count = 1
        else:
            return
        print(f"{event.user.nickname} sent {count}x {event.gift.name}")
        trigger(overlay, event.gift.name, repeat=1)

    await client.start()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True, help="TikTok account to listen to")
    parser.add_argument("--overlay", default="http://127.0.0.1:8722",
                        help="where `giftkit serve` is running")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.user.lstrip("@"), args.overlay.rstrip("/")))
    except ImportError:
        print("TikTokLive is not installed. Run: pip install TikTokLive")
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
