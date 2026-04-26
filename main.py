from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
import os
import re
from typing import Optional
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="iMuzik API", version="1.0.0")

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

# ─── PIPED INSTANCES ─────────────────────────────────────────────────────────
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://piped-api.garudalinux.org",
    "https://api.piped.projectsegfau.lt",
    "https://piped.video/api",
    "https://watchapi.whatever.social",
    "https://api.piped.yt",
]

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
    if not thumbnails:
        return ""
    sorted_thumbs = sorted(
        thumbnails,
        key=lambda x: x.get("width", 0) * x.get("height", 0),
        reverse=True
    )
    return sorted_thumbs[0].get("url", "").replace("=w226-h226", "=w500-h500")

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

def piped_video_to_dict(video: dict) -> dict:
    try:
        raw_url = video.get("url", "")
        video_id = raw_url.split("=")[-1] if "=" in raw_url else ""
        return {
            "id": video_id,
            "title": video.get("title", "Unknown"),
            "artist": video.get("uploaderName", "Unknown Artist"),
            "album": "",
            "duration": format_duration(video.get("duration", 0)),
            "thumbnail": video.get("thumbnail", ""),
            "videoId": video_id,
        }
    except Exception as e:
        logger.error(f"Error formatting piped video: {e}")
        return {}

# ─── STREAM RESOLVERS ────────────────────────────────────────────────────────

async def resolve_via_ytdlp(video_id: str, quality: str = "normal") -> Optional[str]:
    """
    PRIMARY: yt-dlp langsung hit YouTube.
    ClawCloud = real VM, ga ada SSL block → ini harus selalu jalan.
    """
    try:
        fmt = "140/251/bestaudio[abr<=130]/bestaudio" if quality == "normal" else "251/140/bestaudio"
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "--get-url",
            "-f", fmt,
            "--no-playlist",
            "--no-warnings",
            f"https://www.youtube.com/watch?v={video_id}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=25)
        if proc.returncode == 0:
            url = stdout.decode().strip().split("\n")[0]
            if url.startswith("http"):
                logger.info(f"yt-dlp OK: {video_id}")
                return url
        else:
            logger.warning(f"yt-dlp gagal: {stderr.decode()[:200]}")
    except asyncio.TimeoutError:
        logger.error("yt-dlp timeout (25s)")
    except Exception as e:
        logger.error(f"yt-dlp error: {e}")
    return None


async def resolve_via_piped(video_id: str, quality: str = "normal") -> Optional[str]:
    """FALLBACK: Piped instances kalau yt-dlp gagal."""
    async with httpx.AsyncClient(timeout=10) as client:
        for instance in PIPED_INSTANCES:
            try:
                resp = await client.get(f"{instance}/streams/{video_id}")
                if resp.status_code == 200:
                    data = resp.json()
                    audio_streams = data.get("audioStreams", [])
                    if not audio_streams:
                        continue

                    audio_streams.sort(key=lambda x: x.get("bitrate", 0), reverse=True)
                    target = next(
                        (s for s in audio_streams if s.get("bitrate", 0) <= 130000),
                        audio_streams[-1]
                    ) if quality == "normal" else audio_streams[0]

                    stream_url = target.get("url")
                    if stream_url:
                        logger.info(f"Piped OK ({instance}): {target.get('bitrate',0)//1000}kbps")
                        return stream_url
            except Exception as e:
                logger.warning(f"Piped {instance} failed: {e}")
                continue
    return None

# ─── CHARTS FALLBACK VIA PIPED ───────────────────────────────────────────────

async def get_charts_from_piped(region: str = "ID") -> dict:
    """Fallback kalau ytmusicapi down."""
    async with httpx.AsyncClient(timeout=15) as client:
        for instance in PIPED_INSTANCES:
            try:
                resp = await client.get(f"{instance}/trending?region={region}")
                if resp.status_code == 200 and resp.text.strip():
                    videos = resp.json()
                    if not isinstance(videos, list) or not videos:
                        continue
                    tracks = [t for t in [piped_video_to_dict(v) for v in videos[:30]] if t.get("id")]
                    if tracks:
                        logger.info(f"Charts Piped OK ({instance}): {len(tracks)} tracks")
                        return {
                            "trending": tracks[:12],
                            "top_songs": tracks[12:24],
                            "top_videos": tracks[24:],
                            "source": "piped_fallback",
                        }
            except Exception as e:
                logger.warning(f"Piped charts {instance} gagal: {e}")
                continue
    raise HTTPException(503, "Semua sumber charts gagal")

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
    Primary: ytmusicapi (data kaya, region ID)
    Fallback: Piped /trending
    """
    if YTM_AVAILABLE:
        try:
            # ✅ v1.x: 'country' bukan 'country_code'
            charts = ytmusic.get_charts(country=region)
            result = {"trending": [], "top_songs": [], "top_videos": [], "source": "ytmusicapi"}

            def extract_items(data):
                """Handle both struktur lama (dict dgn 'items') dan baru (langsung list)."""
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return data.get("items", [])
                return []

            if "songs" in charts:
                for t in extract_items(charts["songs"])[:20]:
                    f = ytm_track_to_dict(t)
                    if f.get("id"): result["top_songs"].append(f)

            if "trending" in charts:
                for t in extract_items(charts["trending"])[:12]:
                    f = ytm_track_to_dict(t)
                    if f.get("id"): result["trending"].append(f)

            if "videos" in charts:
                for t in extract_items(charts["videos"])[:10]:
                    f = ytm_track_to_dict(t)
                    if f.get("id"): result["top_videos"].append(f)

            total = len(result["top_songs"]) + len(result["trending"]) + len(result["top_videos"])
            if total > 0:
                logger.info(f"Charts OK ytmusicapi: {total} tracks")
                return result

            logger.warning("ytmusicapi charts kosong, fallback Piped")
        except Exception as e:
            logger.warning(f"ytmusicapi charts gagal: {e}", exc_info=True)

    return await get_charts_from_piped(region)


@app.get("/search")
async def search(q: str = Query(..., min_length=1), limit: int = 20, filter: str = "songs"):
    if not YTM_AVAILABLE:
        raise HTTPException(503, "ytmusicapi unavailable")
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

        return {"results": formatted, "query": q, "filter": filter}
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(500, str(e))


@app.get("/stream/{video_id}")
async def get_stream(video_id: str, quality: str = "normal"):
    """
    Stream resolver priority:
    1. yt-dlp  ← PRIMARY (real VM, langsung YouTube)
    2. Piped   ← FALLBACK
    3. Embed   ← LAST RESORT
    """
    if not re.match(r'^[a-zA-Z0-9_-]{11}$', video_id):
        raise HTTPException(400, "Invalid video ID")

    # 1️⃣ yt-dlp
    stream_url = await resolve_via_ytdlp(video_id, quality)

    # 2️⃣ Piped fallback
    if not stream_url:
        logger.info(f"yt-dlp gagal, coba Piped untuk {video_id}")
        stream_url = await resolve_via_piped(video_id, quality)

    if stream_url:
        return {"url": stream_url, "videoId": video_id, "method": "stream", "quality": quality}

    # 3️⃣ Last resort
    logger.warning(f"Semua resolver gagal untuk {video_id}")
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
        {"id": "pop",        "name": "Pop",        "color": "#C8FF3E", "query": "pop indonesia 2024"},
        {"id": "hiphop",     "name": "Hip-Hop",    "color": "#FF6B35", "query": "hip hop rap indonesia"},
        {"id": "rnb",        "name": "R&B / Soul", "color": "#9B59B6", "query": "rnb soul indonesia"},
        {"id": "indie",      "name": "Indie",      "color": "#3498DB", "query": "indie indonesia 2024"},
        {"id": "rock",       "name": "Rock",       "color": "#E74C3C", "query": "rock indonesia"},
        {"id": "electronic", "name": "Electronic", "color": "#1ABC9C", "query": "electronic edm indonesia"},
        {"id": "jazz",       "name": "Jazz",       "color": "#F39C12", "query": "jazz indonesia lofi"},
        {"id": "dangdut",    "name": "Dangdut",    "color": "#E91E63", "query": "dangdut viral indonesia"},
        {"id": "kpop",       "name": "K-Pop",      "color": "#FF4081", "query": "kpop viral"},
        {"id": "acoustic",   "name": "Acoustic",   "color": "#795548", "query": "acoustic cover indonesia"},
        {"id": "classical",  "name": "Klasik",     "color": "#607D8B", "query": "musik klasik indonesia"},
        {"id": "viral",      "name": "Viral 🔥",   "color": "#FF5722", "query": "lagu viral tiktok indonesia 2024"},
    ]}
