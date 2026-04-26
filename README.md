---
title: iMuzik API
emoji: 🎵
colorFrom: green
colorTo: yellow
sdk: docker
pinned: false
---

# iMuzik Backend API

FastAPI backend for iMuzik web music player.

## Endpoints
- `GET /` - Health check
- `GET /charts?region=ID` - Trending charts
- `GET /search?q=...` - Search music
- `GET /stream/{videoId}?quality=normal|high` - Get audio stream URL
- `GET /album/{browseId}` - Album details
- `GET /artist/{channelId}` - Artist details
- `GET /genres` - Genre list
