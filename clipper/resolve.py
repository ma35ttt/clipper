"""Turn a page URL (twitch.tv/..., youtube.com/...) into a playable stream URL."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .chat import normalize_channel
from .util import have, run

TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_QUERY_HASH = "0828119ded1c13477966434e15800ff57ddacf13ba1911c129dc2200705b0712"
USHER = "https://usher.ttvnw.net/api/channel/hls/{login}.m3u8"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 clipper/0.1"

PLAYLIST = re.compile(r"\.m3u8($|\?)", re.I)
TWITCH_URL = re.compile(r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]+)", re.I)


@dataclass
class Source:
    url: str
    expires_at: float | None = None
    note: str = ""

    @property
    def refresh_in(self) -> float | None:
        return None if not self.expires_at else self.expires_at - time.time()


def _post_json(url: str, payload: dict, headers: dict, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"User-Agent": UA, "Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


@lru_cache(maxsize=1)
def has_ytdlp() -> bool:
    return have("yt-dlp")


@lru_cache(maxsize=1)
def has_streamlink() -> bool:
    return have("streamlink")


def _twitch(login: str) -> Source:
    payload = {
        "operationName": "PlaybackAccessToken",
        "variables": {"isLive": True, "isVod": False, "login": login, "playerType": "site"},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": TWITCH_QUERY_HASH}},
    }
    data = _post_json(TWITCH_GQL, payload, {"Client-ID": TWITCH_CLIENT_ID})
    token = (data.get("data") or {}).get("streamPlaybackAccessToken")
    if not token:
        raise RuntimeError("no playback token (channel offline or restricted)")
    value, signature = token["value"], token["signature"]
    try:
        expires = float(json.loads(value).get("expires"))
    except (ValueError, TypeError):
        expires = None
    url = (
        USHER.format(login=login)
        + f"?client_id={TWITCH_CLIENT_ID}"
        + f"&token={urllib.parse.quote(value, safe='')}"
        + f"&sig={urllib.parse.quote(signature, safe='')}"
        + "&allow_source=true&allow_audio_only=true&player=twitchweb"
    )
    return Source(url=url, expires_at=expires, note=f"twitch/{login}")


def _ytdlp(url: str) -> Source:
    p = run(
        ["yt-dlp", "--no-warnings", "--no-playlist", "-g",
         "-f", "best[protocol^=m3u8]/best", url],
        timeout=90,
    )
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError((p.stderr or "yt-dlp could not resolve the stream").strip()[-300:])
    return Source(url=p.stdout.strip().splitlines()[0], note="yt-dlp")


def _streamlink(url: str) -> Source:
    p = run(["streamlink", "--stream-url", url, "best"], timeout=90)
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError((p.stderr or "streamlink could not resolve the stream").strip()[-300:])
    return Source(url=p.stdout.strip().splitlines()[0], note="streamlink")


def _is_page(url: str) -> bool:
    if Path(url).exists():
        return False
    if not url.lower().startswith(("http://", "https://")):
        return False
    return not PLAYLIST.search(url)


def resolve(url: str) -> Source:
    """Return a playable URL for a page link, or the input unchanged for media."""
    url = (url or "").strip()
    if not url:
        raise RuntimeError("empty input")
    if not _is_page(url):
        return Source(url=url)

    twitch = TWITCH_URL.match(url)
    if twitch:
        login = normalize_channel(twitch.group(1))
        try:
            return _twitch(login)
        except Exception as exc:  # noqa: BLE001 - fall through to other resolvers
            for resolver, name in ((_streamlink, "streamlink"), (_ytdlp, "yt-dlp")):
                if (has_streamlink() if name == "streamlink" else has_ytdlp()):
                    try:
                        return resolver(url)
                    except Exception:
                        continue
            raise RuntimeError(
                f"could not resolve twitch.tv/{login}: {exc}\n"
                "hint: install streamlink or yt-dlp, or pass the HLS URL directly"
            ) from exc

    if has_streamlink():
        try:
            return _streamlink(url)
        except Exception:
            pass
    if has_ytdlp():
        return _ytdlp(url)

    raise RuntimeError(
        f"cannot resolve {url}\n"
        "hint: pass a .m3u8 URL directly, or install yt-dlp / streamlink"
    )
