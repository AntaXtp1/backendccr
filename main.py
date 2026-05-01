from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import os
import re
import base64
from typing import Optional
import logging
import tempfile

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

# ─── PLAYWRIGHT PERSISTENT BROWSER ──────────────────────────────────────────
# 1 Chromium instance hidup terus — ga spawn/mati tiap request
# Context di-recycle tiap 2 jam buat fresh session
# Stream URL di-intercept dari network request ke googlevideo.com

COOKIES_PATH: Optional[str] = None          # fallback yt-dlp manual cookies
_pw_browser  = None                          # playwright Browser object
_pw_context  = None                          # playwright BrowserContext
_pw_lock     = asyncio.Lock()               # 1 scrape at a time (RAM safe)
_pw_ready    = False
_browser_refresh_task: Optional[asyncio.Task] = None
_BROWSER_REFRESH_INTERVAL = 2 * 60 * 60    # recycle context tiap 2 jam

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--no-first-run",
    "--mute-audio",
    # Anti-detection: sembunyiin tanda headless
    "--disable-blink-features=AutomationControlled",
]

YT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

async def _init_browser() -> bool:
    """Launch Chromium + buat context baru. Return True kalau sukses."""
    global _pw_browser, _pw_context, _pw_ready
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright tidak terinstall")
        return False

    try:
        logger.info("Playwright: launching Chromium persistent...")
        # Simpan instance playwright supaya bisa di-close proper
        _pw_playwright = await async_playwright().__aenter__()
        _pw_browser = await _pw_playwright.chromium.launch(
            headless=True,
            args=CHROMIUM_ARGS,
        )
        _pw_context = await _pw_browser.new_context(
            user_agent=YT_UA,
            viewport={"width": 1280, "height": 720},
            # Patch navigator.webdriver = false via JS
            java_script_enabled=True,
        )
        # Inject stealth script — hapus jejak automation di setiap page baru
        await _pw_context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)

        # Skip warm up — langsung mark ready, context akan di-init saat request pertama
        # Warm up via youtube.com sering timeout di restricted environment
        _pw_ready = True
        logger.info("Playwright: Chromium ready ✓ (lazy warm up)")
        return True
    except Exception as e:
        logger.error(f"Playwright init gagal: {e}")
        _pw_ready = False
        return False


async def _recycle_context():
    """Buat context baru tanpa restart browser — fresh session tiap 2 jam."""
    global _pw_context, _pw_ready
    if not _pw_browser:
        return
    try:
        logger.info("Playwright: recycling browser context...")
        old_ctx = _pw_context
        _pw_context = await _pw_browser.new_context(
            user_agent=YT_UA,
            viewport={"width": 1280, "height": 720},
            java_script_enabled=True,
        )
        await _pw_context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)
        # Skip warm up — langsung mark ready
        _pw_ready = True
        logger.info("Playwright: context recycled ✓")
    except Exception as e:
        logger.error(f"Playwright recycle gagal: {e}")


async def _browser_refresh_loop():
    """Background task: recycle context tiap 2 jam."""
    while True:
        await asyncio.sleep(_BROWSER_REFRESH_INTERVAL)
        await _recycle_context()


@app.on_event("startup")
async def startup():
    global COOKIES_PATH, _browser_refresh_task

    # 1️⃣ Init persistent Playwright browser
    pw_ok = await _init_browser()
    if pw_ok:
        _browser_refresh_task = asyncio.create_task(_browser_refresh_loop())
        logger.info("Startup: Playwright persistent browser aktif ✓")
    else:
        logger.warning("Startup: Playwright gagal — hanya embed fallback tersedia")

    # 2️⃣ Load manual cookies (opsional, buat yt-dlp fallback)
    raw = os.getenv("YT_COOKIES_B64", "").strip()
    if raw:
        try:
            decoded = base64.b64decode(raw)
            cookie_file = "/tmp/cookies_manual.txt"
            with open(cookie_file, "wb") as f:
                f.write(decoded)
            COOKIES_PATH = cookie_file
            logger.info(f"Manual cookies loaded: {cookie_file}")
        except Exception as e:
            logger.warning(f"Gagal load manual cookies: {e}")


@app.on_event("shutdown")
async def shutdown():
    global _browser_refresh_task, _pw_browser, _pw_context
    if _browser_refresh_task:
        _browser_refresh_task.cancel()
        try:
            await _browser_refresh_task
        except asyncio.CancelledError:
            pass
    try:
        if _pw_context: await _pw_context.close()
        if _pw_browser: await _pw_browser.close()
    except Exception:
        pass

# ─── CORS ────────────────────────────────────────────────────────────────────
_origins = [
    "http://localhost:5173",
    "http://localhost:3000",
    "https://play-muzix.vercel.app/",
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

# Cache stream URL — TTL 5 jam (googlevideo URL expire ~6 jam)
_stream_cache: dict = {}
_STREAM_CACHE_TTL = 5 * 60 * 60  # 5 jam dalam detik

def _stream_cache_get(video_id: str, quality: str) -> Optional[str]:
    key = f"{video_id}_{quality}"
    entry = _stream_cache.get(key)
    if not entry:
        return None
    if _time.monotonic() - entry["ts"] > _STREAM_CACHE_TTL:
        del _stream_cache[key]
        return None
    return entry["url"]

def _stream_cache_set(video_id: str, quality: str, url: str):
    key = f"{video_id}_{quality}"
    # Max 50 entry — buang yang paling lama
    if len(_stream_cache) >= 50:
        oldest = min(_stream_cache, key=lambda k: _stream_cache[k]["ts"])
        del _stream_cache[oldest]
    _stream_cache[key] = {"url": url, "ts": _time.monotonic()}


async def resolve_via_playwright(video_id: str, quality: str = "normal") -> Optional[str]:
    """
    PRIMARY: Intercept stream URL dari network request browser.
    Buka /watch?v={id}, tangkap request ke googlevideo.com dengan itag audio.
    Timeout 15 detik — kalau lewat, return None dan fallback ke yt-dlp.
    Lock: 1 scrape at a time biar ga OOM.
    """
    global _pw_ready, _pw_context

    if not _pw_ready or not _pw_context:
        logger.warning("Playwright belum ready — skip ke yt-dlp")
        return None

    # itag audio: 140=m4a/128k (normal), 251=webm/160k (high)
    AUDIO_ITAGS = {"140", "251", "250", "249"}
    target_itag = "251" if quality == "high" else "140"

    async with _pw_lock:
        page = None
        try:
            page = await _pw_context.new_page()
            captured_url: list[str] = []

            # Intercept via RESPONSE — lebih reliable dari request
            # karena beberapa request ke googlevideo pakai redirect chain
            async def handle_response(response):
                url = response.url
                if "googlevideo.com" in url and "videoplayback" in url:
                    for itag in AUDIO_ITAGS:
                        if f"itag={itag}" in url:
                            captured_url.append(url)
                            logger.info(f"Playwright: captured itag={itag} untuk {video_id}")
                            return

            page.on("response", handle_response)

            # Buka watch page biasa — lebih reliable trigger video load vs embed
            # nocookie embed butuh user gesture buat autoplay, watch page tidak
            yt_url = f"https://www.youtube.com/watch?v={video_id}"
            try:
                await page.goto(yt_url, wait_until="domcontentloaded", timeout=25000)
            except Exception as nav_err:
                logger.warning(f"Playwright: goto error {video_id}: {nav_err}")
                return None

            # Tunggu halaman settle dulu
            await asyncio.sleep(2)

            # Trigger play via JS — bypass autoplay policy
            # YT player ada di #movie_player, method playVideo() tersedia
            try:
                await page.evaluate("""
                    () => {
                        const player = document.getElementById('movie_player');
                        if (player && player.playVideo) {
                            player.playVideo();
                        }
                        // Fallback: klik tombol play kalau playVideo() ga ada
                        const btn = document.querySelector('button.ytp-play-button');
                        if (btn) btn.click();
                    }
                """)
            except Exception:
                pass  # kalau JS gagal, tetap tunggu — mungkin autoplay udah jalan

            # Tunggu sampai URL ke-capture, max 15 detik
            deadline = _time.monotonic() + 15
            while not captured_url and _time.monotonic() < deadline:
                await asyncio.sleep(0.4)

            if captured_url:
                url = captured_url[0]
                _stream_cache_set(video_id, quality, url)
                logger.info(f"Playwright resolve OK: {video_id}")
                return url
            else:
                logger.warning(f"Playwright: tidak ada googlevideo request untuk {video_id}")
                return None

        except Exception as e:
            logger.error(f"Playwright scrape error {video_id}: {e}")
            return None
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass



# ─── SOUNDCLOUD RESOLVER ─────────────────────────────────────────────────────
SC_CLIENT_ID = os.getenv("SC_CLIENT_ID", "1Gbi6DBGBMULQH8MuhNvI1HzL9AiX2Pa")
SC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://soundcloud.com/",
    "Origin": "https://soundcloud.com",
}

# Cache SC search result — biar lagu yang sama ga search ulang (TTL 24 jam)
_sc_search_cache: dict = {}
_SC_SEARCH_TTL = 24 * 60 * 60

def _sc_cache_get(key: str) -> Optional[str]:
    entry = _sc_search_cache.get(key)
    if not entry:
        return None
    if _time.monotonic() - entry["ts"] > _SC_SEARCH_TTL:
        del _sc_search_cache[key]
        return None
    return entry["url"]

def _sc_cache_set(key: str, url: str):
    if len(_sc_search_cache) >= 100:
        oldest = min(_sc_search_cache, key=lambda k: _sc_search_cache[k]["ts"])
        del _sc_search_cache[oldest]
    _sc_search_cache[key] = {"url": url, "ts": _time.monotonic()}


async def resolve_via_soundcloud(title: str, artist: str) -> Optional[str]:
    """
    PRIMARY resolver: cari lagu di SoundCloud pakai title+artist,
    ambil HLS stream URL dari transcoding.
    Return None kalau ga ketemu atau SC error.
    """
    cache_key = f"{title}_{artist}".lower().strip()
    cached = _sc_cache_get(cache_key)
    if cached:
        logger.info(f"SC cache hit: {title}")
        return cached

    query = f"{artist} {title}".strip()
    try:
        async with httpx.AsyncClient(timeout=8, headers=SC_HEADERS) as hc:
            # 1. Search track
            r = await hc.get(
                "https://api-v2.soundcloud.com/search/tracks",
                params={
                    "q": query,
                    "client_id": SC_CLIENT_ID,
                    "limit": 5,
                    "filter.downloadable": "false",
                }
            )
            if r.status_code != 200:
                logger.warning(f"SC search failed: {r.status_code} untuk '{query}'")
                return None

            tracks = r.json().get("collection", [])
            if not tracks:
                logger.warning(f"SC: tidak ada hasil untuk '{query}'")
                return None

            # Pilih track terbaik — prioritas: streamable + judul paling mirip
            best = None
            query_lower = query.lower()
            for t in tracks:
                if not t.get("streamable"):
                    continue
                # Simple match: cek apakah title track mengandung artist atau title query
                track_title = t.get("title", "").lower()
                track_user = t.get("user", {}).get("username", "").lower()
                artist_lower = artist.lower()
                title_lower = title.lower()
                if artist_lower in track_title or artist_lower in track_user or title_lower in track_title:
                    best = t
                    break

            # Kalau ga ada yang match proper, ambil hasil pertama yang streamable
            if not best:
                for t in tracks:
                    if t.get("streamable"):
                        best = t
                        break

            if not best:
                logger.warning(f"SC: tidak ada track streamable untuk '{query}'")
                return None

            logger.info(f"SC match: '{best.get('title')}' untuk query '{query}'")

            # 2. Resolve HLS stream URL dari transcoding
            transcodings = best.get("media", {}).get("transcodings", [])
            hls_tc = next(
                (tc for tc in transcodings if tc.get("format", {}).get("protocol") == "hls"),
                None
            )
            if not hls_tc:
                logger.warning(f"SC: tidak ada HLS transcoding untuk '{query}'")
                return None

            r2 = await hc.get(
                hls_tc["url"],
                params={"client_id": SC_CLIENT_ID},
                headers=SC_HEADERS,
            )
            if r2.status_code != 200:
                logger.warning(f"SC transcoding resolve failed: {r2.status_code}")
                return None

            stream_url = r2.json().get("url")
            if not stream_url:
                return None

            _sc_cache_set(cache_key, stream_url)
            logger.info(f"SC resolve OK: '{best.get('title')}'")
            return stream_url

    except Exception as e:
        logger.error(f"SC resolver error: {e}")
        return None


async def _get_track_meta(video_id: str) -> tuple[str, str]:
    """Ambil title + artist dari YTMusic buat dipakai SC search."""
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, lambda: ytmusic.get_song(video_id))
        details = info.get("videoDetails", {})
        title = details.get("title", "")
        artist = details.get("author", "")
        return title, artist
    except Exception:
        return "", ""


async def resolve_via_ytdlp(video_id: str, quality: str = "normal") -> Optional[str]:
    """
    SECONDARY: yt-dlp langsung hit YouTube.
    Fallback kalau Playwright gagal intercept URL.

    Early-exit: kalau stderr langsung ngeprint captcha/Sign in keywords,
    kill process immediately → fallback ~1s, bukan nunggu full 8s timeout.
    """
    # Keywords yang nandain captcha / bot-check — kill early
    CAPTCHA_KEYWORDS = ("captcha", "sign in", "signin", "bot", "confirm you're not a bot")
    # Keywords yang nandain bgutil solver lagi kerja — JANGAN kill
    BGUTIL_OK_KEYWORDS = ("bgutil", "potoken", "po_token")

    try:
        # 140 = m4a audio (ios serve ini), 250/249 = webm opus
        # bestaudio* = any audio-only stream apapun formatnya
        fmt = (
            "140/250/249/bestaudio*/bestaudio"
            if quality == "normal" else
            "140/250/249/bestaudio*/bestaudio"
        )

        args = [
            "yt-dlp",
            "--get-url",
            "-f", fmt,
            "--no-playlist",
            "--no-warnings",
            "--no-check-certificate",
            "--extractor-args", "youtube:player_client=ios",
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
    1. Cache         ← instant
    2. SoundCloud    ← primary, HLS native audio, no throttle
    3. yt-dlp        ← secondary fallback
    4. Embed         ← last resort (background tab throttle)
    """
    if not re.match(r'^[a-zA-Z0-9_-]{11}$', video_id):
        raise HTTPException(400, "Invalid video ID")

    # 1️⃣ Cache hit — stream URL (SC atau yt-dlp) masih fresh
    cached = _stream_cache_get(video_id, quality)
    if cached:
        logger.info(f"Stream cache hit: {video_id}")
        return {"url": cached, "videoId": video_id, "method": "stream", "quality": quality, "source": "cache"}

    # 2️⃣ SoundCloud — ambil metadata dulu, terus search SC
    title, artist = await _get_track_meta(video_id)
    if title:
        sc_url = await resolve_via_soundcloud(title, artist)
        if sc_url:
            _stream_cache_set(video_id, quality, sc_url)
            return {"url": sc_url, "videoId": video_id, "method": "stream", "quality": quality, "source": "soundcloud"}

    # 3️⃣ yt-dlp fallback
    stream_url = await resolve_via_ytdlp(video_id, quality)
    if stream_url:
        _stream_cache_set(video_id, quality, stream_url)
        return {"url": stream_url, "videoId": video_id, "method": "stream", "quality": quality, "source": "ytdlp"}

    # 4️⃣ Last resort — embed
    logger.warning(f"Semua resolver gagal untuk {video_id}, fallback embed")
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
