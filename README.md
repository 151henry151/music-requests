# Music Request

A Jellyseerr-style web app for Airsonic/Subsonic users to request music. Search artists, pick albums or discographies, find torrents via The Pirate Bay (Apibay), and add magnets to qBittorrent with one click.

## Features

- **Authentication** — Log in with your Airsonic/Subsonic credentials (verified via Subsonic API)
- **Artist search** — MusicBrainz + Deezer fallback for artist lookup with thumbnails
- **Album artwork** — Cover art from Cover Art Archive (MusicBrainz) or Deezer
- **Album or discography** — Request a single album or a full discography
- **TPB search** — Search The Pirate Bay via Apibay (no API key required)
- **Smart filtering** — Zero-seeder torrents hidden by default with an option to reveal
- **qBittorrent integration** — Add magnets directly with category `lidarr` for Lidarr import

## Flow

1. Log in with your Airsonic credentials
2. Search for an artist by name
3. Choose **Album** (pick one) or **Discography** (full collection)
4. Search results show torrents; click **Add** to send a magnet to qBittorrent
5. Torrents are added with category `lidarr` for your existing pipeline (e.g. Lidarr → beets → Airsonic)

## Prerequisites

- [Docker](https://www.docker.com/) and Docker Compose
- Airsonic or Subsonic server (for auth)
- qBittorrent (for adding torrents)

## Quick Start

### Docker

```bash
# Build and run
docker build -t music-requests .
docker run -p 8000:8000 \
  -e AIRSONIC_URL=http://your-airsonic:4040 \
  -e QBIT_HOST=your-qbittorrent:8080 \
  -e QBIT_USER=admin \
  -e QBIT_PASS=your_password \
  -e QBIT_CATEGORY=lidarr \
  music-requests
```

### Docker Compose

Add to your `docker-compose.yml`:

```yaml
music-requests:
  build: ./music-requests
  ports:
    - 8001:8000
  environment:
    - AIRSONIC_URL=http://airsonic:4040
    - QBIT_HOST=qbittorrent:8080
    - QBIT_USER=${QBIT_USER}
    - QBIT_PASS=${QBIT_PASS}
    - QBIT_CATEGORY=lidarr
```

**Important:** Use environment variables or secrets for `QBIT_PASS` and similar credentials. Never commit credentials to version control.

## Configuration

| Variable        | Default                      | Description                          |
|----------------|------------------------------|--------------------------------------|
| `AIRSONIC_URL` | `http://airsonic:4040`       | Airsonic/Subsonic base URL (auth)    |
| `QBIT_HOST`    | `qbittorrent:8080`           | qBittorrent host and port            |
| `QBIT_USER`    | `admin`                      | qBittorrent Web UI username          |
| `QBIT_PASS`    | —                            | qBittorrent Web UI password          |
| `QBIT_CATEGORY`| `lidarr`                     | Category for added torrents          |

## Reverse Proxy (Nginx)

Example config for HTTPS:

```nginx
server {
    listen 443 ssl http2;
    server_name music-requests.example.com;

    ssl_certificate /etc/letsencrypt/live/music-requests.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/music-requests.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Tech Stack

- **Backend:** Python, FastAPI
- **Frontend:** Vanilla HTML/CSS/JS
- **APIs:** MusicBrainz, Deezer, Apibay (TPB), Subsonic
- **Auth:** Basic Auth (credentials verified via Subsonic `ping.view`)

## Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
uvicorn main:app --reload --port 8000
```

## Security

- Credentials are verified server-side via the Subsonic API
- All API endpoints require valid Airsonic login (Basic Auth)
- No credentials are stored; they are validated on each request
- Use HTTPS in production

## License

GPL-3.0. See [LICENSE](LICENSE) for details.
