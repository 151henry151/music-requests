"""
Music Request App - Jellyseerr-like flow for Airsonic users.
- Auth via Subsonic API (ping)
- Artist search via MusicBrainz
- Album list via MusicBrainz
- TPB search via Apibay
- Add magnet to qBittorrent (category: lidarr)
- YouTube search + rip to tagged MP3 files

Copyright (C) 2026  Music Request contributors
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import qbittorrentapi
import yt_dlp
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from mutagen.easyid3 import EasyID3
from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.mp3 import MP3
from pydantic import BaseModel
from ytmusicapi import YTMusic


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


# Config from env
AIRSONIC_URL = os.environ.get("AIRSONIC_URL", "https://music.romptele.com").rstrip("/")
QBIT_HOST = os.environ.get("QBIT_HOST", "qbittorrent:5080")
QBIT_USER = os.environ.get("QBIT_USER", "admin")
QBIT_PASS = os.environ.get("QBIT_PASS", "adminadmin")
CATEGORY = os.environ.get("QBIT_CATEGORY", "lidarr")

MUSICBRAINZ_BASE = "https://musicbrainz.org/ws/2"
APIBAY_BASE = "https://apibay.org"
DEEZER_BASE = "https://api.deezer.com"
USER_AGENT = "MusicRequests/1.0 (https://music-requests.romptele.com)"
YT_IMPORT_DIR = os.environ.get("YT_IMPORT_DIR", "/tmp/music-requests-imports")
YT_SEARCH_LIMIT = max(_env_int("YT_SEARCH_LIMIT", 8), 1)
YT_RIPPED_IDS_FILE = os.environ.get("YT_RIPPED_IDS_FILE", str(Path(YT_IMPORT_DIR) / ".ripped_youtube_ids.json"))
YT_AUDIO_QUALITY = (os.environ.get("YT_AUDIO_QUALITY", "192").strip().lower().removesuffix("k") or "192")
YT_DLP_COOKIES_FILE = os.environ.get("YT_DLP_COOKIES_FILE", "").strip()
TRIGGER_AIRSONIC_SCAN = os.environ.get("TRIGGER_AIRSONIC_SCAN", "true").strip().lower() in ("1", "true", "yes")

logger = logging.getLogger("music_requests")
YT_MUSIC_BASE = "https://music.youtube.com"
_ytmusic_client: YTMusic | None = None
RIP_JOB_RETENTION_SECONDS = max(_env_int("RIP_JOB_RETENTION_SECONDS", 86400), 600)


@dataclass
class RipJob:
    id: str
    requested_by: str
    request: dict
    status: str = "queued"  # queued | running | completed | failed
    progress_percent: float = 0.0
    step: str = "Queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    error: str | None = None
    result: dict | None = None


RIP_JOBS: dict[str, RipJob] = {}
RIP_JOBS_LOCK = threading.Lock()


# --- Models ---
class LoginRequest(BaseModel):
    username: str
    password: str


class AddTorrentRequest(BaseModel):
    magnet: str


def _get_ripped_youtube_ids() -> set[str]:
    try:
        p = Path(YT_RIPPED_IDS_FILE)
        if p.exists():
            data = json.loads(p.read_text())
            return set(data) if isinstance(data, list) else set(data.get("ids", []))
    except Exception:
        pass
    return set()


def _add_ripped_youtube_id(youtube_id: str) -> None:
    if not youtube_id:
        return
    ids = _get_ripped_youtube_ids()
    ids.add(youtube_id)
    try:
        p = Path(YT_RIPPED_IDS_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(list(ids)))
    except Exception as e:
        logger.warning("Could not save ripped ID %s: %s", youtube_id, e)


class RipYouTubeRequest(BaseModel):
    url: str
    artist: str
    album: str
    year: str | None = None


def _cleanup_old_jobs() -> None:
    cutoff = time.time() - RIP_JOB_RETENTION_SECONDS
    stale = [jid for jid, job in RIP_JOBS.items() if job.updated_at < cutoff and job.status in ("completed", "failed")]
    for jid in stale:
        RIP_JOBS.pop(jid, None)


def _job_snapshot(job: RipJob) -> dict:
    return {
        "job_id": job.id,
        "status": job.status,
        "progress_percent": round(job.progress_percent, 2),
        "step": job.step,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "log": job.log[-400:],
        "error": job.error,
        "result": job.result,
    }


def _update_job(job_id: str, *, status: str | None = None, step: str | None = None, percent: float | None = None,
                log_line: str | None = None, error: str | None = None, result: dict | None = None) -> None:
    with RIP_JOBS_LOCK:
        job = RIP_JOBS.get(job_id)
        if not job:
            return
        if status:
            job.status = status
        if step is not None:
            job.step = step
        if percent is not None:
            job.progress_percent = max(0.0, min(float(percent), 100.0))
        if log_line:
            job.log.append(log_line)
            if len(job.log) > 1200:
                job.log = job.log[-1200:]
        if error is not None:
            job.error = error
        if result is not None:
            job.result = result
        job.updated_at = time.time()
        _cleanup_old_jobs()


def _get_job_for_user(job_id: str, username: str) -> RipJob:
    with RIP_JOBS_LOCK:
        job = RIP_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Rip job not found")
    if job.requested_by != username:
        raise HTTPException(status_code=403, detail="Not allowed to access this rip job")
    return job


# --- Auth: verify Airsonic credentials via Subsonic ping ---
async def verify_airsonic(username: str, password: str) -> bool:
    """Verify credentials against Airsonic/Subsonic ping.view endpoint."""
    params = {"u": username, "p": password, "v": "1.15.0", "c": "music-requests"}
    url = f"{AIRSONIC_URL}/rest/ping.view"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url, params=params)
    if r.status_code != 200:
        return False
    # Subsonic returns XML; "ok" in body means success
    return "status=\"ok\"" in r.text or '"status":"ok"' in r.text


def get_auth_header(authorization: str | None = Header(default=None, alias="Authorization")) -> tuple[str, str]:
    """Extract Basic Auth from Authorization header. Returns (username, password)."""
    if not authorization or not authorization.startswith("Basic "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization")
    import base64
    try:
        decoded = base64.b64decode(authorization[6:]).decode("utf-8")
        if ":" not in decoded:
            raise ValueError()
        u, p = decoded.split(":", 1)
        return u, p
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid Authorization")


# --- MusicBrainz ---
async def mb_search_artists(query: str) -> list[dict]:
    url = f"{MUSICBRAINZ_BASE}/artist/"
    params = {"query": query, "fmt": "json", "limit": 25}
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        r = await client.get(url, params=params)
    r.raise_for_status()
    data = r.json()
    return data.get("artists", [])


# --- Deezer (artist images) ---
def _norm_name(s: str) -> str:
    return "".join(c.lower() for c in s if c.isalnum() or c.isspace()).strip()


async def deezer_artist_images(query: str) -> dict[str, list[str]]:
    """Fetch artist images from Deezer. Returns norm_name -> list of image URLs in Deezer order.
    Multiple artists with same name get distinct images by position (avoids wrong image for e.g. Sublime band vs Sublime Afropop)."""
    url = f"{DEEZER_BASE}/search/artist"
    params = {"q": query, "limit": 25}
    try:
        headers = {"User-Agent": USER_AGENT}
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return {}
    result: dict[str, list[str]] = {}
    for a in data.get("data", []):
        name = a.get("name")
        img = a.get("picture_medium") or a.get("picture_small")
        if name and img and "/artist//" not in img:
            norm = _norm_name(name)
            result.setdefault(norm, []).append(img)
    return result


async def deezer_search_artists(query: str) -> list[dict]:
    """Fallback: fetch artists from Deezer when MusicBrainz is unreachable."""
    url = f"{DEEZER_BASE}/search/artist"
    params = {"q": query, "limit": 25}
    try:
        headers = {"User-Agent": USER_AGENT}
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    out = []
    for a in data.get("data", []):
        name = a.get("name")
        if not name:
            continue
        did = a.get("id")
        img = a.get("picture_medium") or a.get("picture_small") or ""
        if img and "/artist//" in img:
            img = ""
        out.append({"id": f"deezer:{did}", "name": name, "type": "Artist", "image": img or None})
    return out


async def deezer_get_albums(deezer_id: str) -> list[dict]:
    """Fetch albums for a Deezer artist."""
    url = f"{DEEZER_BASE}/artist/{deezer_id}/albums"
    params = {"limit": 100}
    try:
        headers = {"User-Agent": USER_AGENT}
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    out = []
    for a in data.get("data", []):
        title = a.get("title")
        if not title:
            continue
        rg_type = a.get("record_type", "album")
        if rg_type not in ("album", "ep", "single"):
            continue
        date = (a.get("release_date") or "")[:4]
        cover = a.get("cover_medium") or a.get("cover_small") or None
        out.append({"id": a.get("id"), "title": title, "type": rg_type.title(), "date": date, "cover": cover})
    return out


async def mb_get_release_groups(artist_id: str) -> list[dict]:
    url = f"{MUSICBRAINZ_BASE}/release-group/"
    params = {"artist": artist_id, "fmt": "json", "limit": 100}
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        r = await client.get(url, params=params)
    r.raise_for_status()
    data = r.json()
    return data.get("release-groups", [])


# --- Apibay (TPB) ---
async def apibay_search(query: str) -> list[dict]:
    """Search TPB via Apibay. Returns list of {id, name, info_hash, seeders, leechers, size, added}."""
    url = f"{APIBAY_BASE}/q.php"
    params = {"q": query, "cat": "0"}
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        r = await client.get(url, params=params)
    r.raise_for_status()
    data = r.json()
    if not data or (isinstance(data, list) and len(data) == 1 and data[0].get("id") == "0"):
        return []
    return data if isinstance(data, list) else []


def info_hash_to_magnet(info_hash: str, name: str) -> str:
    """Build magnet link from Apibay info_hash and name."""
    dn = urllib.parse.quote(name)
    return f"magnet:?xt=urn:btih:{info_hash}&dn={dn}"


def _safe_path_component(name: str, fallback: str = "Unknown") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
    if not cleaned:
        return fallback
    return cleaned[:120]


def _fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return "-"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _normalize_youtube_url(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("YouTube URL is required")
    # Allow clients to pass either an ID or a full URL.
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw):
        return f"https://www.youtube.com/watch?v={raw}"
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Invalid URL")
    host = parsed.netloc.lower()
    allowed_hosts = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be")
    if host not in allowed_hosts:
        raise ValueError("Only YouTube URLs are supported")
    if host == "youtu.be":
        video_id = parsed.path.strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError("Invalid YouTube video ID")
        return f"https://www.youtube.com/watch?v={video_id}"
    qs = parse_qs(parsed.query)
    list_id = (qs.get("list") or [None])[0]
    video_id = (qs.get("v") or [None])[0]
    if list_id and not video_id:
        return f"https://www.youtube.com/playlist?list={list_id}"
    if video_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return f"https://www.youtube.com/watch?v={video_id}"
    raise ValueError("Invalid YouTube URL")


def _youtube_url_to_id(url: str) -> str | None:
    """Return a stable id for this YouTube URL (video id or 'playlist_<id>' for playlists)."""
    try:
        normalized = _normalize_youtube_url(url)
        parsed = urlparse(normalized)
        qs = parse_qs(parsed.query)
        list_id = (qs.get("list") or [None])[0]
        video_id = (qs.get("v") or [None])[0]
        if list_id:
            return f"playlist_{list_id}"
        if video_id and len(str(video_id)) == 11:
            return str(video_id)
        if parsed.path.startswith("/watch") and video_id:
            return str(video_id)
        if "/playlist" in parsed.path and list_id:
            return f"playlist_{list_id}"
        return None
    except Exception:
        return None


def _yt_dlp_common_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "geo_bypass": True,
    }
    if YT_DLP_COOKIES_FILE:
        cookie_path = Path(YT_DLP_COOKIES_FILE)
        if cookie_path.exists():
            opts["cookiefile"] = str(cookie_path)
    return opts


def _get_ytmusic_client() -> YTMusic:
    global _ytmusic_client
    if _ytmusic_client is None:
        _ytmusic_client = YTMusic()
    return _ytmusic_client


def _norm_lookup(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", (value or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _ytmusic_find_artist(artist_name: str) -> dict | None:
    yt = _get_ytmusic_client()
    results = yt.search(artist_name, filter="artists", limit=20) or []
    if not results:
        return None
    target = _norm_lookup(artist_name)
    exact = [
        a for a in results
        if _norm_lookup((a.get("artist") or a.get("title") or "")) == target
    ]
    preferred = exact or results
    artist = preferred[0]
    browse_id = artist.get("browseId")
    if not browse_id:
        return None
    display_name = (artist.get("artist") or artist.get("title") or artist_name).strip()
    channel_url = f"{YT_MUSIC_BASE}/channel/{browse_id}" if browse_id.startswith("UC") else ""
    thumb = None
    thumbs = artist.get("thumbnails") or []
    if thumbs:
        thumb = thumbs[-1].get("url") or thumbs[0].get("url")
    return {
        "name": display_name,
        "browse_id": browse_id,
        "channel_url": channel_url,
        "thumbnail": thumb,
    }


def _total_duration_from_tracks(tracks: list[dict]) -> int:
    total = 0
    for t in tracks:
        sec = t.get("duration_seconds")
        if isinstance(sec, int):
            total += sec
    return total


def _youtube_album_results_via_ytmusic(artist_name: str, album_hint: str | None, limit: int) -> tuple[list[dict], dict | None]:
    artist_match = _ytmusic_find_artist(artist_name)
    if not artist_match:
        return [], None
    yt = _get_ytmusic_client()
    details = yt.get_artist(artist_match["browse_id"])
    sections = []
    for key in ("albums", "singles"):
        sec = details.get(key) or {}
        sections.extend(sec.get("results") or [])

    seen_playlists: set[str] = set()
    results: list[dict] = []
    target_artist_norm = _norm_lookup(artist_match["name"])
    hint_norm = _norm_lookup(album_hint or "")

    for item in sections:
        browse_id = item.get("browseId")
        if not browse_id:
            continue
        try:
            album = yt.get_album(browse_id)
        except Exception:
            continue
        playlist_id = album.get("audioPlaylistId") or item.get("playlistId")
        if not playlist_id or playlist_id in seen_playlists:
            continue
        album_title = (album.get("title") or item.get("title") or "").strip()
        artists = album.get("artists") or item.get("artists") or []
        artist_names = [a.get("name", "") for a in artists]
        # Keep only releases clearly tied to the matched artist.
        if artist_names and all(_norm_lookup(n) != target_artist_norm for n in artist_names):
            continue

        tracks = album.get("tracks") or []
        duration_sec = _total_duration_from_tracks(tracks)
        url = f"{YT_MUSIC_BASE}/playlist?list={playlist_id}"
        thumbs = album.get("thumbnails") or item.get("thumbnails") or []
        thumbnail = thumbs[-1].get("url") if thumbs else None
        year = str(album.get("year") or item.get("year") or "")
        if year == "None":
            year = ""
        score = 1
        title_norm = _norm_lookup(album_title)
        if hint_norm:
            if title_norm == hint_norm:
                score = 4
            elif hint_norm in title_norm or title_norm in hint_norm:
                score = 3
            elif any(tok in title_norm for tok in hint_norm.split(" ")):
                score = 2
        results.append({
            "id": f"playlist_{playlist_id}",
            "title": album_title or "Unknown album",
            "channel": artist_match["name"],
            "duration": duration_sec,
            "duration_human": _fmt_duration(duration_sec),
            "url": url,
            "thumbnail": thumbnail,
            "year": year,
            "score": score,
            "artist_channel_url": artist_match["channel_url"],
        })
        seen_playlists.add(playlist_id)

    results.sort(key=lambda x: (x.get("score", 1), x.get("year") or "", x.get("title") or ""), reverse=True)
    trimmed = results[:limit]
    for r in trimmed:
        r.pop("score", None)
    return trimmed, artist_match


def _youtube_search_sync(query: str, limit: int, mode: str = "album") -> list[dict]:
    opts = _yt_dlp_common_opts()
    opts.update({
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": True,
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    entries = info.get("entries") or []
    out = []
    for e in entries:
        if not e:
            continue
        video_id = e.get("id")
        url = e.get("webpage_url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
        out.append({
            "id": video_id,
            "title": e.get("title", "Unknown title"),
            "channel": e.get("channel") or e.get("uploader") or "",
            "duration": e.get("duration") or 0,
            "duration_human": _fmt_duration(e.get("duration") or 0),
            "url": url,
            "thumbnail": e.get("thumbnail"),
        })
    # album mode: only show longer content (full albums/playlists), then sort long first
    MIN_ALBUM_DURATION_SEC = 600  # 10 min; single songs are typically 2-5 min
    if mode == "album":
        out = [x for x in out if (x.get("duration") or 0) >= MIN_ALBUM_DURATION_SEC]
        out.sort(key=lambda x: (x.get("duration") or 0), reverse=True)
    else:
        out.sort(key=lambda x: (x.get("duration") or 0))
    return out


async def youtube_search(query: str, limit: int, mode: str = "album") -> list[dict]:
    return await asyncio.to_thread(_youtube_search_sync, query, limit, mode)


def _run_ffmpeg(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="ignore").strip() if e.stderr else ""
        raise RuntimeError(stderr or "ffmpeg command failed")


def _friendly_ytdlp_error(exc: Exception) -> str:
    msg = str(exc)
    lower = msg.lower()
    if "not a bot" in lower or "sign in to confirm" in lower:
        return (
            f"{msg} "
            "Set YT_DLP_COOKIES_FILE to a valid YouTube cookies export if this persists."
        )
    return msg


def _collect_cover_urls(info: dict) -> list[str]:
    urls: list[str] = []
    thumb = info.get("thumbnail")
    if thumb:
        urls.append(thumb)
    for t in info.get("thumbnails", []):
        u = t.get("url")
        if u and u not in urls:
            urls.append(u)
    return urls


def _download_cover_jpg(urls: list[str], target_dir: Path) -> Path | None:
    headers = {"User-Agent": USER_AGENT}
    for idx, url in enumerate(urls):
        try:
            with httpx.Client(timeout=20.0, headers=headers, follow_redirects=True) as client:
                r = client.get(url)
                r.raise_for_status()
            ext = Path(urlparse(url).path).suffix.lower() or ".jpg"
            raw_path = target_dir / f"cover-{idx}{ext}"
            raw_path.write_bytes(r.content)
            if ext in (".jpg", ".jpeg"):
                return raw_path
            jpg_path = target_dir / f"cover-{idx}.jpg"
            _run_ffmpeg([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(raw_path), str(jpg_path),
            ])
            if jpg_path.exists():
                return jpg_path
        except Exception:
            continue
    return None


def _progress_percent_from_hook(data: dict) -> float | None:
    total = data.get("total_bytes") or data.get("total_bytes_estimate")
    downloaded = data.get("downloaded_bytes")
    if total and downloaded is not None:
        try:
            return max(0.0, min(float(downloaded) / float(total) * 100.0, 100.0))
        except Exception:
            pass
    pct_str = str(data.get("_percent_str") or "").strip()
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", pct_str)
    if m:
        try:
            return max(0.0, min(float(m.group(1)), 100.0))
        except ValueError:
            return None
    return None


def _download_audio_sync(
    source_url: str,
    work_dir: Path,
    prefix: str,
    *,
    progress_cb: Callable[[float, str], None] | None = None,
) -> tuple[Path, dict]:
    opts = _yt_dlp_common_opts()
    if progress_cb:
        def _hook(hook_data: dict) -> None:
            status = hook_data.get("status")
            if status == "downloading":
                pct = _progress_percent_from_hook(hook_data)
                if pct is not None:
                    progress_cb(pct, "Downloading audio")
            elif status == "finished":
                progress_cb(100.0, "Download finished; extracting MP3")
        opts["progress_hooks"] = [_hook]
    opts.update({
        "format": "bestaudio/best",
        "outtmpl": str(work_dir / f"{prefix}-%(id)s.%(ext)s"),
        "noplaylist": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": YT_AUDIO_QUALITY},
            {"key": "FFmpegMetadata"},
        ],
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(source_url, download=True)

    requested = info.get("requested_downloads") or []
    for item in requested:
        fp = item.get("filepath")
        if not fp:
            continue
        p = Path(fp)
        mp3_candidate = p.with_suffix(".mp3")
        if mp3_candidate.exists():
            return mp3_candidate, info
        if p.exists():
            return p, info

    candidates = sorted(work_dir.glob(f"{prefix}-*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    if candidates:
        return candidates[0], info

    raise RuntimeError("yt-dlp did not produce an audio file")


def _tag_mp3(
    file_path: Path,
    *,
    artist: str,
    album: str,
    title: str,
    track_no: int,
    total_tracks: int,
    year: str | None,
    cover_path: Path | None,
) -> None:
    try:
        tags = EasyID3(str(file_path))
    except ID3NoHeaderError:
        audio = MP3(str(file_path))
        audio.add_tags()
        audio.save()
        tags = EasyID3(str(file_path))

    tags["title"] = [title]
    tags["artist"] = [artist]
    tags["albumartist"] = [artist]
    tags["album"] = [album]
    tags["tracknumber"] = [f"{track_no}/{total_tracks}"]
    if year:
        tags["date"] = [year]
    tags.save(v2_version=3)

    if cover_path and cover_path.exists():
        mime = "image/png" if cover_path.suffix.lower() == ".png" else "image/jpeg"
        image_data = cover_path.read_bytes()
        try:
            id3 = ID3(str(file_path))
        except ID3NoHeaderError:
            id3 = ID3()
        id3.delall("APIC")
        id3.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image_data))
        id3.save(str(file_path), v2_version=3)


def _split_by_chapters(source_mp3: Path, chapters: list[dict], out_dir: Path) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for idx, chapter in enumerate(chapters, start=1):
        start = float(chapter.get("start_time") or 0.0)
        end = float(chapter.get("end_time") or 0.0)
        if end <= start:
            continue
        title = str(chapter.get("title") or f"Track {idx}").strip()
        target = out_dir / f"chapter-{idx:03d}-{uuid4().hex[:8]}.mp3"
        _run_ffmpeg([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
            "-i", str(source_mp3),
            "-vn", "-c:a", "libmp3lame", "-b:a", f"{YT_AUDIO_QUALITY}k",
            str(target),
        ])
        out.append((target, title))
    return out


def _verify_mp3_tags(file_path: Path, *, artist: str, album: str, title: str, track_no: int, year: str | None) -> dict:
    issues: list[str] = []
    expected_year = (year or "").strip()
    try:
        tags = EasyID3(str(file_path))
    except Exception as e:
        return {
            "file": file_path.name,
            "ok": False,
            "issues": [f"Cannot read ID3 tags: {e}"],
            "cover_embedded": False,
        }

    def _first(name: str) -> str:
        vals = tags.get(name, [])
        return vals[0] if vals else ""

    actual_title = _first("title")
    actual_artist = _first("artist")
    actual_album = _first("album")
    actual_album_artist = _first("albumartist")
    actual_track = _first("tracknumber")
    actual_date = _first("date")
    if actual_title != title:
        issues.append(f"title mismatch: expected '{title}', got '{actual_title}'")
    if actual_artist != artist:
        issues.append(f"artist mismatch: expected '{artist}', got '{actual_artist}'")
    if actual_album != album:
        issues.append(f"album mismatch: expected '{album}', got '{actual_album}'")
    if actual_album_artist != artist:
        issues.append(f"albumartist mismatch: expected '{artist}', got '{actual_album_artist}'")
    if not actual_track.startswith(f"{track_no}/"):
        issues.append(f"tracknumber mismatch: expected prefix '{track_no}/', got '{actual_track}'")
    if expected_year and actual_date != expected_year:
        issues.append(f"date mismatch: expected '{expected_year}', got '{actual_date}'")

    try:
        id3 = ID3(str(file_path))
        cover_count = len(id3.getall("APIC"))
    except Exception:
        cover_count = 0
    if cover_count < 1:
        issues.append("cover art not embedded")

    return {
        "file": file_path.name,
        "ok": len(issues) == 0,
        "issues": issues,
        "cover_embedded": cover_count > 0,
        "title": actual_title,
        "artist": actual_artist,
        "album": actual_album,
        "albumartist": actual_album_artist,
        "tracknumber": actual_track,
        "date": actual_date,
    }


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    i = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def _rip_youtube_sync(
    url: str,
    artist: str,
    album: str,
    year: str | None,
    progress_log: list[str] | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
) -> dict:
    def emit(step: str, percent: float | None = None) -> None:
        pct = max(0.0, min(percent if percent is not None else 0.0, 100.0))
        if progress_log is not None:
            if percent is None:
                progress_log.append(step)
            else:
                progress_log.append(f"[{pct:.1f}%] {step}")
        if progress_cb is not None:
            progress_cb(pct, step)

    source_url = _normalize_youtube_url(url)
    emit(f"Resolved URL: {source_url}", 1.0)
    import_root = Path(YT_IMPORT_DIR)
    import_root.mkdir(parents=True, exist_ok=True)

    safe_artist = _safe_path_component(artist, "Unknown Artist")
    safe_album = _safe_path_component(album, "Unknown Album")
    clean_year = (year or "").strip()[:4]
    album_dir_name = f"{safe_album} ({clean_year})" if clean_year else safe_album
    album_dir = import_root / safe_artist / album_dir_name
    album_dir.mkdir(parents=True, exist_ok=True)
    emit(f"Output directory: {album_dir}", 2.5)

    with tempfile.TemporaryDirectory(prefix="yt-rip-", dir=str(import_root)) as tmp:
        tmp_dir = Path(tmp)
        info_opts = _yt_dlp_common_opts()
        info_opts.update({"skip_download": True})
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(source_url, download=False)
        emit("Fetched source metadata", 8.0)

        cover_path = _download_cover_jpg(_collect_cover_urls(info), tmp_dir)
        final_cover: Path | None = None
        if cover_path:
            final_cover = album_dir / "cover.jpg"
            shutil.copy2(cover_path, final_cover)
            folder_cover = album_dir / "folder.jpg"
            if not folder_cover.exists():
                shutil.copy2(cover_path, folder_cover)
            emit("Downloaded and saved cover art", 12.0)
        else:
            emit("No cover art could be downloaded; proceeding without embedded art", 12.0)

        track_sources: list[tuple[Path, str]] = []
        expected_tracks = 0
        download_start = 12.0
        download_span = 68.0
        if info.get("_type") == "playlist" and info.get("entries"):
            entries = [e for e in info.get("entries", []) if e]
            expected_tracks = len(entries)
            for idx, entry in enumerate(entries, start=1):
                entry_url = entry.get("webpage_url") or entry.get("url") or ""
                if entry_url and not str(entry_url).startswith("http"):
                    entry_id = entry.get("id")
                    entry_url = f"https://www.youtube.com/watch?v={entry_id}" if entry_id else ""
                if not entry_url:
                    continue
                title_hint = (entry.get("title") or f"Track {idx}").strip()
                track_base = download_start + ((idx - 1) / max(expected_tracks, 1)) * download_span
                track_span = download_span / max(expected_tracks, 1)
                emit(f"Downloading track {idx}/{expected_tracks}: {title_hint}", track_base)
                audio_path, entry_info = _download_audio_sync(
                    str(entry_url),
                    tmp_dir,
                    f"track-{idx:03d}",
                    progress_cb=lambda pct, msg, idx=idx, title_hint=title_hint, track_base=track_base, track_span=track_span:
                    emit(f"{msg} ({idx}/{expected_tracks}: {title_hint})", track_base + (track_span * (pct / 100.0))),
                )
                title = (entry.get("title") or entry_info.get("title") or f"Track {idx}").strip()
                track_sources.append((audio_path, title))
                emit(f"Finished downloading track {idx}/{expected_tracks}: {title}", track_base + track_span)
        else:
            expected_tracks = 1
            emit("Downloading source audio", download_start)
            audio_path, downloaded_info = _download_audio_sync(
                source_url,
                tmp_dir,
                "album",
                progress_cb=lambda pct, msg: emit(msg, download_start + (download_span * (pct / 100.0))),
            )
            chapters = downloaded_info.get("chapters") or info.get("chapters") or []
            usable_chapters = [
                c for c in chapters
                if float(c.get("end_time") or 0.0) > float(c.get("start_time") or 0.0) and c.get("title")
            ]
            if len(usable_chapters) >= 2:
                emit(f"Splitting source into {len(usable_chapters)} chapter tracks", 82.0)
                track_sources = _split_by_chapters(audio_path, usable_chapters, tmp_dir)
                expected_tracks = len(track_sources)
            else:
                title = (downloaded_info.get("title") or info.get("title") or album).strip()
                track_sources = [(audio_path, title)]

        emit(f"Preparing to tag {len(track_sources)} track(s)", 83.0)
        if not track_sources:
            raise RuntimeError("No tracks were produced from the YouTube source")

        total_tracks = len(track_sources)
        saved_files: list[str] = []
        verification: list[dict] = []
        for idx, (source_file, title) in enumerate(track_sources, start=1):
            safe_title = _safe_path_component(title, f"Track {idx}")
            final_path = _unique_path(album_dir / f"{idx:02d} - {safe_title}.mp3")
            shutil.move(str(source_file), str(final_path))
            _tag_mp3(
                final_path,
                artist=artist.strip() or safe_artist,
                album=album.strip() or safe_album,
                title=title,
                track_no=idx,
                total_tracks=total_tracks,
                year=clean_year or None,
                cover_path=final_cover,
            )
            saved_files.append(final_path.name)
            verification.append(_verify_mp3_tags(
                final_path,
                artist=artist.strip() or safe_artist,
                album=album.strip() or safe_album,
                title=title,
                track_no=idx,
                year=clean_year or None,
            ))
            emit(f"Tagged track {idx}/{total_tracks}: {final_path.name}", 83.0 + (17.0 * idx / max(total_tracks, 1)))

    metadata_ok = all(v.get("ok") for v in verification)
    cover_ok = all(v.get("cover_embedded") for v in verification) if verification else False
    emit("Rip complete", 100.0)

    return {
        "tracks_added": len(saved_files),
        "expected_tracks": expected_tracks,
        "output_dir": str(album_dir),
        "tracks": saved_files,
        "cover_saved": bool((album_dir / "cover.jpg").exists()),
        "verification": {
            "all_metadata_ok": metadata_ok,
            "all_cover_embedded": cover_ok,
            "tracks": verification,
        },
    }


async def trigger_airsonic_scan(username: str, password: str) -> tuple[bool, str]:
    params = {"u": username, "p": password, "v": "1.15.0", "c": "music-requests", "f": "json"}
    url = f"{AIRSONIC_URL}/rest/startScan.view"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, params=params)
    except Exception as e:
        msg = f"Scan request failed: {e!s}"
        logger.warning("trigger_airsonic_scan: %s", msg)
        return False, msg
    if r.status_code != 200:
        body = (r.text or "")[:200]
        msg = f"Airsonic returned HTTP {r.status_code}: {body}"
        logger.warning("trigger_airsonic_scan: %s", msg)
        return False, msg
    body = r.text or ""
    ok = False
    # JSON response (common when f=json is honored)
    try:
        parsed = r.json()
        status = (parsed.get("subsonic-response") or {}).get("status")
        ok = (status == "ok")
    except Exception:
        # XML/other response fallback
        if re.search(r'status\s*=\s*"ok"', body, flags=re.IGNORECASE):
            ok = True
        elif re.search(r'"status"\s*:\s*"ok"', body, flags=re.IGNORECASE):
            ok = True
    if not ok:
        msg = f"Airsonic scan response indicated failure: {body[:200]}"
        logger.warning("trigger_airsonic_scan: %s", msg)
        return False, msg
    return True, "Scan triggered."


def _run_rip_job(job_id: str, username: str, password: str) -> None:
    with RIP_JOBS_LOCK:
        job = RIP_JOBS.get(job_id)
    if not job:
        return
    req = job.request
    artist = str(req.get("artist") or "").strip()
    album = str(req.get("album") or "").strip()
    url = str(req.get("url") or "").strip()
    year = req.get("year")

    _update_job(job_id, status="running", step="Starting rip job", percent=0.0, log_line="Starting rip job")
    progress_log: list[str] = []

    def _progress(percent: float, step: str) -> None:
        _update_job(job_id, status="running", step=step, percent=percent, log_line=f"[{percent:.1f}%] {step}")

    try:
        result = _rip_youtube_sync(url, artist, album, year, progress_log, _progress)
        rip_id = _youtube_url_to_id(url)
        if rip_id:
            _add_ripped_youtube_id(rip_id)
        scan_ok, scan_msg = False, "Scan not attempted."
        if TRIGGER_AIRSONIC_SCAN:
            try:
                scan_ok, scan_msg = asyncio.run(trigger_airsonic_scan(username, password))
            except Exception as e:
                scan_ok = False
                scan_msg = f"Scan trigger failed: {e}"
        result["airsonic_scan_triggered"] = scan_ok
        result["airsonic_scan_message"] = scan_msg
        _update_job(
            job_id,
            status="completed",
            step="Rip completed",
            percent=100.0,
            log_line=f"Rip completed. {result.get('tracks_added', 0)} track(s) saved to {result.get('output_dir', '-')}.",
            result=result,
        )
    except Exception as e:
        friendly = _friendly_ytdlp_error(e)
        _update_job(job_id, status="failed", step="Rip failed", percent=100.0, error=friendly, log_line=f"Rip failed: {friendly}")

# --- qBittorrent ---
def add_magnet_to_qbit(magnet: str) -> None:
    host, _, port = QBIT_HOST.partition(":")
    port = int(port) if port else 5080
    client = qbittorrentapi.Client(
        host=host or "localhost",
        port=port,
        username=QBIT_USER,
        password=QBIT_PASS,
    )
    client.auth_log_in()
    client.torrents_add(urls=magnet, category=CATEGORY)


# --- App ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="Music Request", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse("static/index.html")


# --- API (all require auth) ---
@app.post("/api/login")
async def login(req: LoginRequest):
    ok = await verify_airsonic(req.username, req.password)
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid Airsonic credentials")
    return {"ok": True, "username": req.username}


@app.get("/api/artists")
async def search_artists(q: str, _: tuple = Depends(get_auth_header)):
    if len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="Query too short")
    query = q.strip()
    try:
        artists, images = await asyncio.gather(mb_search_artists(query), deezer_artist_images(query))
        out = []
        used: dict[str, int] = {}
        for a in artists:
            name = a.get("name", "")
            norm = _norm_name(name)
            imgs = images.get(norm, [])
            idx = used.get(norm, 0)
            img = imgs[idx] if idx < len(imgs) else None
            used[norm] = idx + 1
            out.append({"id": a["id"], "name": name, "type": a.get("type", ""), "image": img})
    except (httpx.ConnectError, httpx.ConnectTimeout) as _:
        # MusicBrainz unreachable (e.g. container network); fall back to Deezer
        artists = await deezer_search_artists(query)
        out = [{"id": a["id"], "name": a["name"], "type": a.get("type", ""), "image": a.get("image")} for a in artists]
    return {"artists": out}


@app.get("/api/albums/{artist_id}")
async def get_albums(artist_id: str, _: tuple = Depends(get_auth_header)):
    if artist_id.startswith("deezer:"):
        deezer_id = artist_id[7:]
        groups = await deezer_get_albums(deezer_id)
    else:
        groups = await mb_get_release_groups(artist_id)
    def _fmt_album(g: dict) -> dict | None:
        t = g.get("primary-type") or g.get("type") or "Album"
        if t not in ("Album", "EP", "Single", None):
            return None
        date_val = (g.get("first-release-date") or g.get("date") or "")[:4]
        # Cover: Deezer has cover; MusicBrainz uses Cover Art Archive
        img = g.get("cover")
        if not img and g.get("id"):
            img = f"https://coverartarchive.org/release-group/{g['id']}/front-250"
        return {"id": g.get("id", ""), "title": g.get("title", ""), "type": t or "Album", "date": date_val, "image": img}
    albums = [a for g in groups if (a := _fmt_album(g))]
    return {"albums": albums[:100]}


@app.get("/api/search-tpb")
async def search_tpb(q: str, _: tuple = Depends(get_auth_header)):
    """Search TPB. Used for both album and discography requests."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query required")
    results = await apibay_search(q.strip())
    out = []
    for r in results:
        info_hash = r.get("info_hash") or r.get("info hash", "")
        name = r.get("name", "Unknown")
        seeders = int(r.get("seeders", 0) or 0)
        leechers = int(r.get("leechers", 0) or 0)
        size = int(r.get("size", 0) or 0)
        added = r.get("added", 0)
        magnet = info_hash_to_magnet(info_hash, name) if info_hash else ""
        out.append({
            "name": name,
            "seeders": seeders,
            "leechers": leechers,
            "size": size,
            "added": added,
            "magnet": magnet,
        })
    return {"results": out}


@app.get("/api/search-youtube")
async def search_youtube_api(
    q: str,
    limit: int | None = None,
    mode: str = "album",
    artist: str | None = None,
    album: str | None = None,
    _: tuple = Depends(get_auth_header),
):
    """Search YouTube. mode=album prefers full albums (long first); mode=song prefers single tracks."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query required")
    use_limit = limit if limit is not None else YT_SEARCH_LIMIT
    use_limit = max(1, min(use_limit, 25))
    search_mode = "album" if (mode or "").strip().lower() == "album" else "song"
    matched_artist = None
    try:
        if search_mode == "album" and artist and artist.strip():
            results, matched_artist = await asyncio.to_thread(
                _youtube_album_results_via_ytmusic,
                artist.strip(),
                (album or "").strip(),
                use_limit,
            )
            if not results:
                # Fall back to generic yt-dlp search if YT Music artist albums could not be resolved.
                results = await youtube_search(q.strip(), use_limit, search_mode)
        else:
            results = await youtube_search(q.strip(), use_limit, search_mode)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"YouTube search failed: {_friendly_ytdlp_error(e)}")
    ripped_ids = _get_ripped_youtube_ids()
    for r in results:
        vid = r.get("id") or _youtube_url_to_id(r.get("url") or "")
        r["already_ripped"] = bool(vid and vid in ripped_ids)
    return {"results": results, "matched_artist": matched_artist}


@app.post("/api/rip-youtube")
async def rip_youtube(req: RipYouTubeRequest, auth: tuple[str, str] = Depends(get_auth_header)):
    artist = req.artist.strip()
    album = req.album.strip()
    if not artist:
        raise HTTPException(status_code=400, detail="Artist is required")
    if not album:
        raise HTTPException(status_code=400, detail="Album is required")
    try:
        normalized = _normalize_youtube_url(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job_id = uuid4().hex
    job = RipJob(
        id=job_id,
        requested_by=auth[0],
        request={
            "url": normalized,
            "artist": artist,
            "album": album,
            "year": (req.year or "").strip()[:4] or None,
        },
        status="queued",
        step="Queued",
        progress_percent=0.0,
        log=["Queued rip job"],
    )
    with RIP_JOBS_LOCK:
        RIP_JOBS[job_id] = job
        _cleanup_old_jobs()

    worker = threading.Thread(target=_run_rip_job, args=(job_id, auth[0], auth[1]), daemon=True)
    worker.start()
    return {"ok": True, "job_id": job_id, "status": "queued"}


@app.get("/api/rip-youtube/jobs/{job_id}")
async def rip_youtube_job(job_id: str, auth: tuple[str, str] = Depends(get_auth_header)):
    job = _get_job_for_user(job_id, auth[0])
    return _job_snapshot(job)


@app.post("/api/add-torrent")
async def add_torrent(req: AddTorrentRequest, _: tuple = Depends(get_auth_header)):
    if not req.magnet.strip().startswith("magnet:"):
        raise HTTPException(status_code=400, detail="Invalid magnet link")
    try:
        add_magnet_to_qbit(req.magnet.strip())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to add torrent: {e}")
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
