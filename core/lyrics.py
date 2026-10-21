"""Búsqueda de letras (lrclib con fallback a lyrics.ovh)."""

import asyncio
import logging
import re
from urllib.parse import quote

import aiohttp

log = logging.getLogger("lyrics")

MAX_CHARS = 1800
REQUEST_TIMEOUT = 8
TOTAL_TIMEOUT = 25

TITLE_NOISE_RE = re.compile(
    r"\s*(\(|\[).*(official|video|audio|lyrics?|lyric video|HD|4K|visualizer|sub español|sub esp|remaster|live|en vivo).*(\)|\])",
    re.IGNORECASE,
)
TRAILING_TITLE_NOISE_RE = re.compile(
    r"\s+[-|]?\s*(official\s+)?(music\s+)?(video|audio|lyrics?|lyric video|visualizer|remaster(ed)?|live|en vivo)\s*$",
    re.IGNORECASE,
)
LIVE_SESSION_RE = re.compile(r"\s+(desde|en vivo desde|live at|live from)\b.*$", re.IGNORECASE)
FEAT_RE = re.compile(r"\s*(\(|\[)?\s*(feat\.?|ft\.?|featuring)\s+[^)\]]+(\)|\])?", re.IGNORECASE)
LRC_TIMESTAMP_RE = re.compile(r"^\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]\s*", re.MULTILINE)


def clean_song_title(title: str) -> str:
    title = TITLE_NOISE_RE.sub("", title)
    title = LIVE_SESSION_RE.sub("", title)
    title = TRAILING_TITLE_NOISE_RE.sub("", title)
    title = FEAT_RE.sub("", title)
    title = re.sub(r"\s+", " ", title)
    return title.strip(" -|")


def split_artist_song(title: str) -> tuple[str, str]:
    title = clean_song_title(title)
    for separator in (" - ", " – ", " — ", " | "):
        if separator in title:
            artist, song = title.split(separator, 1)
            return artist.strip(), song.strip()
    return "", title


def clean_lyrics_text(text: str) -> str:
    text = LRC_TIMESTAMP_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def search_terms(title: str, artist: str, song: str) -> list[str]:
    clean_title = clean_song_title(title)
    clean_song = clean_song_title(song)
    terms = []
    seen = set()
    for term in (
        f"{artist} {clean_song}" if artist else "",
        f"{clean_song} {artist}" if artist else "",
        clean_title,
        clean_song,
        re.sub(r"^(el|la|los|las)\s+", "", clean_song, flags=re.IGNORECASE),
    ):
        term = term.strip()
        key = term.lower()
        if term and key not in seen:
            seen.add(key)
            terms.append(term)
    return terms


async def find(session: aiohttp.ClientSession, title: str, artist: str, song: str) -> str | None:
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

    searches = [{"track_name": song}]
    if artist:
        searches.insert(0, {"track_name": song, "artist_name": artist})
    searches.extend({"q": term} for term in search_terms(title, artist, song))

    for params in searches:
        log.debug("buscando %s", params)
        try:
            async with session.get("https://lrclib.net/api/search", params=params, timeout=timeout) as resp:
                if resp.status == 200:
                    results = await resp.json()
                    for item in results:
                        text = clean_lyrics_text(item.get("plainLyrics") or item.get("syncedLyrics") or "")
                        if text:
                            return text
                else:
                    log.debug("lrclib status %s para %s", resp.status, params)
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            log.debug("lrclib falló %s: %r", params, error)

    if not artist:
        return None

    log.debug("fallback lyrics.ovh %s - %s", artist, song)
    url = f"https://api.lyrics.ovh/v1/{quote(artist, safe='')}/{quote(song, safe='')}"
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                return clean_lyrics_text(data.get("lyrics", "")) or None
            log.debug("lyrics.ovh status %s para %s - %s", resp.status, artist, song)
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        log.debug("lyrics.ovh falló: %r", error)
    return None
