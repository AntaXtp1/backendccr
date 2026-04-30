from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import os
import re
import base64
from typing import Optional
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── IN-MEMORY CACHE ─────────────────────────────────────────────────────────
# Simple dict cache — Redis overkill buat single-instance deployment
# key → {"data": ..., "ts": float}
import time as _time

_cache: dict = {}
_CACHE_TTL = {
    "charts":  10 * 60,   # 10 menit — charts ga se-realtime itu
    "search":   5 * 60,   # 5 menit — search result cukup fresh
}

def cache_get(key: str) -> dict | None:
    entry = _cache.get(key)
    if not entry:
        return None
    ttl_key = key.split(":")[0]  # "charts", "search", dll
    ttl = _CACHE_TTL.get(ttl_key, 5 * 60)
    if _time.monotonic() - entry["ts"] > ttl:
        del _cache[key]
        return None
    return entry["data"]

def cache_set(key: str, data) -> None:
    # Buang entry lama kalau > 200 (biar ga bloat di memory)
    if len(_cache) > 200:
        oldest_key = min(_cache, key=lambda k: _cache[k]["ts"])
        del _cache[oldest_key]
    _cache[key] = {"data": data, "ts": _time.monotonic()}
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="iMuzik API", version="1.0.0")

# ─── COOKIES SETUP ───────────────────────────────────────────────────────────
COOKIES_PATH: Optional[str] = None

@app.on_event("startup")
async def setup_cookies():
    global COOKIES_PATH
    raw = os.getenv("YT_COOKIES_B64", "").strip()
    if not raw:
        logger.info("YT_COOKIES_B64 tidak ada — yt-dlp jalan tanpa cookies")
        return
    try:
        decoded = base64.b64decode(raw)
        cookie_file = "/tmp/cookies.txt"
        with open(cookie_file, "wb") as f:
            f.write(decoded)
        COOKIES_PATH = cookie_file
        logger.info(f"Cookies berhasil di-load ke {cookie_file} ({len(decoded)} bytes)")
    except Exception as e:
        logger.warning(f"Gagal decode/tulis cookies: {e} — yt-dlp jalan tanpa cookies")

# ─── CORS ────────────────────────────────────────────────────────────────────
_origins = [
    "http://localhost:5173",
    "http://localhost:3000",
    "https://i-muzix.vercel.app",
]
_frontend_url = os.getenv("FRONTEND_URL", "").strip().rstrip("/")
if _frontend_url:
    _origins.append(_frontend_url)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── YTMUSICAPI INIT ─────────────────────────────────────────────────────────
try:
    from ytmusicapi import YTMusic
    ytmusic = YTMusic()
    YTM_AVAILABLE = True
    logger.info("ytmusicapi initialized OK")
except Exception as e:
    YTM_AVAILABLE = False
    logger.warning(f"ytmusicapi not available: {e}")

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def clean_thumbnail(thumbnails: list) -> str:
    """
    Ambil thumbnail resolusi tertinggi dan upgrade URL-nya kalau bisa.
    Handle 2 format utama: lh3.googleusercontent.com dan i.ytimg.com
    """
    if not thumbnails:
        return ""

    sorted_thumbs = sorted(
        thumbnails,
        key=lambda x: x.get("width", 0) * x.get("height", 0),
        reverse=True
    )
    url = sorted_thumbs[0].get("url", "")
    if not url:
        return ""

    # Format 1: lh3.googleusercontent.com — replace semua =wN-hN-... ke ukuran besar
    # Contoh: =w226-h226-l90-rj → =w500-h500-l90-rj
    if "lh3.googleusercontent.com" in url:
        url = re.sub(r"=w\d+-h\d+", "=w500-h500", url)
        return url

    # Format 2: i.ytimg.com — coba upgrade ke maxresdefault
    if "i.ytimg.com" in url:
        # hqdefault / mqdefault / sddefault → maxresdefault
        url = re.sub(r"/(hqdefault|mqdefault|sddefault|default)(\.jpg)", "/maxresdefault\2", url)
        return url

    # Format lain: coba replace pattern umum kalau ada
    url = re.sub(r"=w\d+-h\d+", "=w500-h500", url)
    return url

def format_duration(seconds) -> str:
    if not seconds:
        return "0:00"
    try:
        s = int(seconds)
        m, s = divmod(s, 60)
        return f"{m}:{s:02d}"
    except:
        return "0:00"

def ytm_track_to_dict(track: dict) -> dict:
    try:
        thumbnails = []
        if "thumbnails" in track:
            thumbnails = track["thumbnails"]
        elif "album" in track and track["album"] and "thumbnails" in track.get("album", {}):
            thumbnails = track["album"]["thumbnails"]

        artists = track.get("artists", [])
        artist_names = ", ".join([a.get("name", "") for a in artists]) if artists else "Unknown Artist"

        return {
            "id": track.get("videoId", ""),
            "title": track.get("title", "Unknown"),
            "artist": artist_names,
            "album": track.get("album", {}).get("name", "") if track.get("album") else "",
            "duration": track.get("duration", ""),
            "thumbnail": clean_thumbnail(thumbnails),
            "videoId": track.get("videoId", ""),
        }
    except Exception as e:
        logger.error(f"Error formatting track: {e}")
        return {}

# ─── STREAM RESOLVERS ────────────────────────────────────────────────────────

async def resolve_via_ytdlp(video_id: str, quality: str = "normal") -> Optional[str]:
    """
    PRIMARY: yt-dlp langsung hit YouTube.
    ClawCloud = real VM, ga ada SSL block → ini harus selalu jalan.

    Early-exit: kalau stderr langsung ngeprint captcha/Sign in keywords,
    kill process immediately → fallback ~1s, bukan nunggu full 8s timeout.
    Docker bgutil-ytdlp aware: keyword "bgutil" di stderr = solver aktif, bukan error.
    """
    # Keywords yang nandain captcha / bot-check — kill early
    CAPTCHA_KEYWORDS = ("captcha", "sign in", "signin", "bot", "confirm you're not a bot")
    # Keywords yang nandain bgutil solver lagi kerja — JANGAN kill
    BGUTIL_OK_KEYWORDS = ("bgutil", "potoken", "po_token")

    try:
        fmt = (
            "140/251/250/249/bestaudio[abr<=160]/bestaudio/best[height<=480]/best"
            if quality == "normal" else
            "251/140/250/bestaudio/best[height<=720]/best"
        )

        args = [
            "yt-dlp",
            "--get-url",
            "-f", fmt,
            "--no-playlist",
            "--no-warnings",
            "--no-check-certificate",
            # web_creator = less restricted, support audio-only formats, lower bot detection
            "--extractor-args", "youtube:player_client=web_creator,ios",
            "--sleep-requests", "1",
        ]

        if COOKIES_PATH:
            args += ["--cookies", COOKIES_PATH]
            logger.info(f"yt-dlp menggunakan cookies dari {COOKIES_PATH}")

        args.append(f"https://www.youtube.com/watch?v={video_id}")

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # ── Realtime stderr monitor ───────────────────────────────────────────
        stderr_chunks: list[bytes] = []
        captcha_detected = False

        async def watch_stderr():
            nonlocal captcha_detected
            async for line in proc.stderr:
                decoded = line.decode(errors="replace").lower()
                stderr_chunks.append(line)

                # Kalau bgutil/potoken kedetect → solver lagi aktif, skip kill
                if any(k in decoded for k in BGUTIL_OK_KEYWORDS):
                    logger.info(f"bgutil solver aktif ({video_id}), tunggu hasilnya...")
                    continue

                if any(k in decoded for k in CAPTCHA_KEYWORDS):
                    captcha_detected = True
                    logger.warning(f"Captcha/bot-check kedetect early ({video_id}), kill yt-dlp")
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    return

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    asyncio.wait_for(proc.wait(), timeout=8),
                    watch_stderr(),
                ),
                timeout=9,  # outer safety net
            )
        except asyncio.TimeoutError:
            logger.error(f"yt-dlp timeout (8s) - {video_id} skip ke fallback")
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return None

        if captcha_detected:
            return None

        stdout_data = await proc.stdout.read()
        if proc.returncode == 0:
            url = stdout_data.decode().strip().split("\n")[0]
            if url.startswith("http"):
                logger.info(f"yt-dlp OK: {video_id}")
                return url
        else:
            full_stderr = b"".join(stderr_chunks).decode(errors="replace")
            logger.warning(f"yt-dlp gagal: {full_stderr[:200]}")

    except Exception as e:
        logger.error(f"yt-dlp error: {e}")
    return None


# ─── API ROUTES ──────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "status": "iMuzik API running",
        "ytmusicapi": YTM_AVAILABLE,
        "platform": "ClawCloud Run",
    }

@app.get("/health")
async def health():
    return {"status": "ok", "ytm": YTM_AVAILABLE}


@app.get("/charts")
async def get_charts(region: str = "ID"):
    """
    Charts via search-based approach (ytmusicapi v1.11.5+).
    v1.11.5 mengubah struktur get_charts() — sekarang return playlist bukan tracks.
    Solusi: pakai search() sebagai proxy trending yang lebih reliable.
    """
    cache_key = f"charts:{region}"
    cached = cache_get(cache_key)
    if cached:
        logger.info(f"Charts cache hit: {region}")
        return cached

    if YTM_AVAILABLE:
        try:
            # v1.11.5+: get_charts() return playlist objects, bukan individual tracks
            # Workaround: pakai search sebagai proxy trending per region
            region_queries = {
                "ID": ["trending musik indonesia 2026", "lagu viral indonesia 2026", "hits terbaru indonesia 2026"],
                "US": ["trending music 2026", "top hits usa 2026", "billboard hot 100 2026"],
                "GB": ["trending music uk 2026", "top hits uk 2026"],
            }
            queries = region_queries.get(region, [f"trending music {region} 2026", "top hits 2026"])

            result = {"trending": [], "top_songs": [], "top_videos": [], "source": "ytmusicapi_search"}
            seen_ids = set()

            for i, query in enumerate(queries):
                try:
                    search_results = ytmusic.search(query, filter="songs", limit=15)
                    for t in search_results:
                        f = ytm_track_to_dict(t)
                        if f.get("id") and f["id"] not in seen_ids:
                            seen_ids.add(f["id"])
                            if i == 0:
                                result["trending"].append(f)
                            elif i == 1:
                                result["top_songs"].append(f)
                            else:
                                result["top_videos"].append(f)
                except Exception as qe:
                    logger.warning(f"Search query gagal ({query}): {qe}")
                    continue

            total = len(result["top_songs"]) + len(result["trending"]) + len(result["top_videos"])
            if total > 0:
                logger.info(f"Charts OK via search: {total} tracks")
                cache_set(cache_key, result)
                return result

            logger.warning("ytmusicapi search charts kosong")
        except Exception as e:
            logger.warning(f"ytmusicapi charts gagal: {e}")

    raise HTTPException(503, "Charts tidak tersedia, ytmusicapi down")


@app.get("/search")
async def search(q: str = Query(..., min_length=1), limit: int = 20, filter: str = "songs"):
    if not YTM_AVAILABLE:
        raise HTTPException(503, "ytmusicapi unavailable")

    cache_key = f"search:{filter}:{q.lower().strip()}:{limit}"
    cached = cache_get(cache_key)
    if cached:
        logger.info(f"Search cache hit: {q!r}")
        return cached

    try:
        filter_map = {
            "songs": "songs", "albums": "albums",
            "artists": "artists", "playlists": "playlists", "videos": "videos",
        }
        ytm_filter = filter_map.get(filter, "songs")
        results = ytmusic.search(q, filter=ytm_filter, limit=limit)

        formatted = []
        for item in results:
            if ytm_filter == "songs":
                f = ytm_track_to_dict(item)
                if f.get("id"): formatted.append(f)
            elif ytm_filter == "albums":
                formatted.append({
                    "id": item.get("browseId", ""), "type": "album",
                    "title": item.get("title", ""),
                    "artist": ", ".join([a.get("name", "") for a in item.get("artists", [])]),
                    "year": item.get("year", ""),
                    "thumbnail": clean_thumbnail(item.get("thumbnails", [])),
                })
            elif ytm_filter == "artists":
                formatted.append({
                    "id": item.get("browseId", ""), "type": "artist",
                    "title": item.get("artist", ""),
                    "thumbnail": clean_thumbnail(item.get("thumbnails", [])),
                    "subscribers": item.get("subscribers", ""),
                })

        result = {"results": formatted, "query": q, "filter": filter}
        cache_set(cache_key, result)
        return result
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(500, str(e))


@app.get("/stream/{video_id}")
async def get_stream(video_id: str, quality: str = "normal"):
    """
    Stream resolver priority:
    1. yt-dlp  ← PRIMARY (ios,mweb,web client)
    2. Embed   ← LAST RESORT
    """
    if not re.match(r'^[a-zA-Z0-9_-]{11}$', video_id):
        raise HTTPException(400, "Invalid video ID")

    # 1️⃣ yt-dlp
    stream_url = await resolve_via_ytdlp(video_id, quality)

    if stream_url:
        return {"url": stream_url, "videoId": video_id, "method": "stream", "quality": quality}

    # 2️⃣ Last resort — embed
    logger.warning(f"yt-dlp gagal untuk {video_id}, fallback embed")
    return {
        "url": None,
        "embedUrl": f"https://www.youtube.com/embed/{video_id}?autoplay=1&enablejsapi=1",
        "videoId": video_id,
        "method": "embed",
        "quality": quality,
    }


@app.get("/song/{video_id}")
async def get_song_info(video_id: str):
    if not YTM_AVAILABLE:
        raise HTTPException(503, "ytmusicapi unavailable")
    try:
        info = ytmusic.get_song(video_id)
        vd = info.get("videoDetails", {})
        return {
            "id": video_id,
            "title": vd.get("title", ""),
            "artist": vd.get("author", ""),
            "duration": format_duration(vd.get("lengthSeconds")),
            "thumbnail": clean_thumbnail(vd.get("thumbnail", {}).get("thumbnails", [])),
            "views": vd.get("viewCount", ""),
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/album/{browse_id}")
async def get_album(browse_id: str):
    if not YTM_AVAILABLE:
        raise HTTPException(503, "ytmusicapi unavailable")
    try:
        album = ytmusic.get_album(browse_id)
        tracks = []
        for t in album.get("tracks", []):
            f = ytm_track_to_dict(t)
            if not f.get("thumbnail"):
                f["thumbnail"] = clean_thumbnail(album.get("thumbnails", []))
            if f.get("id"): tracks.append(f)
        return {
            "id": browse_id,
            "title": album.get("title", ""),
            "artist": ", ".join([a.get("name", "") for a in album.get("artists", [])]),
            "year": album.get("year", ""),
            "thumbnail": clean_thumbnail(album.get("thumbnails", [])),
            "tracks": tracks,
            "trackCount": album.get("trackCount", 0),
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/artist/{channel_id}")
async def get_artist(channel_id: str):
    if not YTM_AVAILABLE:
        raise HTTPException(503, "ytmusicapi unavailable")
    try:
        artist = ytmusic.get_artist(channel_id)
        top_songs = []
        for s in artist.get("songs", {}).get("results", [])[:10]:
            f = ytm_track_to_dict(s)
            if f.get("id"): top_songs.append(f)
        return {
            "id": channel_id,
            "name": artist.get("name", ""),
            "description": artist.get("description", ""),
            "thumbnail": clean_thumbnail(artist.get("thumbnails", [])),
            "subscribers": artist.get("subscribers", ""),
            "topSongs": top_songs,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/genres")
async def get_genres():
    return {"genres": [
        {"id": "pop",        "name": "Pop",        "color": "#C8FF3E", "query": "pop indonesia 2026"},
        {"id": "hiphop",     "name": "Hip-Hop",    "color": "#FF6B35", "query": "hip hop rap indonesia"},
        {"id": "rnb",        "name": "R&B / Soul", "color": "#9B59B6", "query": "rnb soul indonesia"},
        {"id": "indie",      "name": "Indie",      "color": "#3498DB", "query": "indie indonesia 2026"},
        {"id": "rock",       "name": "Rock",       "color": "#E74C3C", "query": "rock indonesia"},
        {"id": "electronic", "name": "Electronic", "color": "#1ABC9C", "query": "electronic edm indonesia"},
        {"id": "jazz",       "name": "Jazz",       "color": "#F39C12", "query": "jazz indonesia lofi"},
        {"id": "dangdut",    "name": "Dangdut",    "color": "#E91E63", "query": "dangdut viral indonesia"},
        {"id": "kpop",       "name": "K-Pop",      "color": "#FF4081", "query": "kpop viral"},
        {"id": "acoustic",   "name": "Acoustic",   "color": "#795548", "query": "acoustic cover indonesia"},
        {"id": "classical",  "name": "Klasik",     "color": "#607D8B", "query": "musik klasik indonesia"},
        {"id": "viral",      "name": "Viral 🔥",   "color": "#FF5722", "query": "lagu viral tiktok indonesia 2026"},
    ]}
