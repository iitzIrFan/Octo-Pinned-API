import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Load .env variables
load_dotenv()

logger = logging.getLogger("uvicorn.error")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME")

# Public URL of this API (e.g. https://octo-pinned-api.onrender.com).
# Used to build absolute URLs for override images. Behind a proxy (Render),
# request.base_url can report http://, which browsers block on https sites.
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")

allowed_origins = os.getenv("ALLOWED_ORIGINS")
allowed_origins = allowed_origins.split(",") if allowed_origins else ["*"]

GITHUB_TIMEOUT = 10.0  # seconds, GitHub GraphQL request
SITE_TIMEOUT = 5.0  # seconds, per live-site / screenshot lookup
REPOS_TTL = 10 * 60  # seconds, pinned repo data cache
REFRESH_RETRY_DELAY = 60  # seconds, wait before retrying a failed refresh
SITE_IMAGE_TTL = 24 * 60 * 60  # seconds, resolved live-site image cache
SITE_IMAGE_RETRY_TTL = 60 * 60  # seconds, when no site image was found
MAX_HTML_BYTES = 256 * 1024  # only read the top of each page

USER_AGENT = "Mozilla/5.0 (compatible; Octo-Pinned-API/1.0)"

# Override images: static/projects/<repo-name>.<ext> (repo name, any case)
OVERRIDES_DIR = Path(__file__).parent / "static" / "projects"
IMAGE_EXTENSIONS = {".webp", ".png", ".jpg", ".jpeg", ".avif", ".gif", ".svg"}


def load_overrides():
    """Map lowercase repo name -> override filename (with ?v= cache buster)."""
    overrides = {}
    if OVERRIDES_DIR.is_dir():
        for path in OVERRIDES_DIR.iterdir():
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                version = int(path.stat().st_mtime)
                overrides[path.stem.lower()] = f"{quote(path.name)}?v={version}"
    return overrides


OVERRIDES = load_overrides()

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )
    # Warm the cache at startup so the first visitor after a deploy or
    # restart doesn't wait. Needs PUBLIC_BASE_URL to build override URLs.
    if PUBLIC_BASE_URL:
        start_refresh(PUBLIC_BASE_URL)
    yield
    if _refresh_task and not _refresh_task.done():
        _refresh_task.cancel()
    await http_client.aclose()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Serve override images at /images/<file>
app.mount(
    "/images",
    StaticFiles(directory=OVERRIDES_DIR, check_dir=False),
    name="images",
)

# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

GRAPHQL_QUERY = """
query ($login: String!) {
  user(login: $login) {
    pinnedItems(first: 6, types: [REPOSITORY]) {
      nodes {
        ... on Repository {
          name
          description
          url
          homepageUrl
          stargazerCount
          forkCount
          openGraphImageUrl
          usesCustomOpenGraphImage
          languages(first: 1) {
            nodes {
              name
            }
          }
        }
      }
    }
  }
}
"""


async def fetch_pinned_from_github():
    if not GITHUB_TOKEN or not GITHUB_USERNAME:
        raise HTTPException(
            status_code=500,
            detail="Server misconfigured: GITHUB_TOKEN and GITHUB_USERNAME are required",
        )
    try:
        response = await http_client.post(
            "https://api.github.com/graphql",
            json={"query": GRAPHQL_QUERY, "variables": {"login": GITHUB_USERNAME}},
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
            timeout=GITHUB_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"GitHub request failed: {exc}")

    user = (payload.get("data") or {}).get("user")
    if payload.get("errors") or not user:
        errors = payload.get("errors") or [{"message": "user not found"}]
        message = "; ".join(e.get("message", "unknown error") for e in errors)
        raise HTTPException(status_code=502, detail=f"GitHub GraphQL error: {message}")

    return user["pinnedItems"]["nodes"]


# ---------------------------------------------------------------------------
# Image resolution
# ---------------------------------------------------------------------------


class SocialImageParser(HTMLParser):
    """Collects og:image / twitter:image meta tags from a page."""

    KEYS = ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src")

    def __init__(self):
        super().__init__()
        self.found = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        attrs = {k.lower(): (v or "") for k, v in attrs}
        key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        content = attrs.get("content", "").strip()
        if key in self.KEYS and content and key not in self.found:
            self.found[key] = content

    def best(self):
        for key in self.KEYS:
            if key in self.found:
                return self.found[key]
        return None


def https_only(url):
    return url if url and url.startswith("https://") else None


async def find_site_image(homepage):
    """Step 3: the live site's own og:image / twitter:image."""
    try:
        async with http_client.stream("GET", homepage, timeout=SITE_TIMEOUT) as resp:
            if resp.status_code >= 400:
                return None
            if "html" not in resp.headers.get("content-type", "").lower():
                return None
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= MAX_HTML_BYTES:
                    break
            final_url = str(resp.url)
            encoding = resp.encoding or "utf-8"
        html = b"".join(chunks).decode(encoding, errors="replace")
    except httpx.HTTPError:
        return None

    parser = SocialImageParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    image = parser.best()
    return https_only(urljoin(final_url, image)) if image else None


async def get_screenshot(homepage):
    """Step 4: screenshot of the live site via microlink (cached by caller)."""
    try:
        resp = await http_client.get(
            "https://api.microlink.io/",
            params={"url": homepage, "screenshot": "true", "meta": "false"},
            timeout=SITE_TIMEOUT * 3,  # screenshots are slower to render
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    if data.get("status") != "success":
        return None
    return https_only(((data.get("data") or {}).get("screenshot") or {}).get("url"))


# homepage -> (image or None, expires_at)
_site_image_cache: dict[str, tuple[str | None, float]] = {}


async def site_image(homepage):
    """Steps 3 + 4 for a live site, cached for 24h (1h if nothing was found)."""
    cached = _site_image_cache.get(homepage)
    if cached and cached[1] > time.time():
        return cached[0]

    image = await find_site_image(homepage) or await get_screenshot(homepage)
    ttl = SITE_IMAGE_TTL if image else SITE_IMAGE_RETRY_TTL
    _site_image_cache[homepage] = (image, time.time() + ttl)
    return image


async def resolve_image(repo, base_url):
    """
    1. Override file in static/projects/
    2. Custom GitHub social preview
    3. Live site's og:image / twitter:image
    4. Live site screenshot (microlink)
    5. GitHub's automatic card
    """
    override = OVERRIDES.get(repo["name"].lower())
    if override:
        return f"{base_url}/images/{override}"

    github_image = repo.get("openGraphImageUrl")
    if repo.get("usesCustomOpenGraphImage") and github_image:
        return github_image

    homepage = (repo.get("homepageUrl") or "").strip()
    if homepage.startswith(("http://", "https://")):
        image = await site_image(homepage)
        if image:
            return image

    return github_image


# ---------------------------------------------------------------------------
# Repo cache + endpoint
# ---------------------------------------------------------------------------

_repos_cache: dict = {"data": None, "expires_at": 0.0}
_refresh_task: asyncio.Task | None = None


async def refresh_repos(base_url):
    """Fetch repos + images from scratch and store them in the cache."""
    try:
        nodes = await fetch_pinned_from_github()
    except HTTPException as exc:
        if _repos_cache["data"] is None:
            raise
        # Keep serving the old data; retry after a short delay, not on every request.
        logger.warning("Refresh failed, serving cached data: %s", exc.detail)
        _repos_cache["expires_at"] = time.time() + REFRESH_RETRY_DELAY
        return _repos_cache["data"]

    images = await asyncio.gather(*(resolve_image(r, base_url) for r in nodes))

    repos = []
    for repo, image in zip(nodes, images):
        item = {
            k: v
            for k, v in repo.items()
            if k not in ("openGraphImageUrl", "usesCustomOpenGraphImage")
        }
        item["image"] = image
        repos.append(item)

    _repos_cache["data"] = repos
    _repos_cache["expires_at"] = time.time() + REPOS_TTL
    return repos


def _log_refresh_error(task: asyncio.Task):
    if not task.cancelled() and task.exception() is not None:
        exc = task.exception()
        logger.warning("Refresh failed: %s", getattr(exc, "detail", exc))


def start_refresh(base_url):
    """Start a refresh unless one is already running; return the shared task."""
    global _refresh_task
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = asyncio.create_task(refresh_repos(base_url))
        _refresh_task.add_done_callback(_log_refresh_error)
    return _refresh_task


async def get_repos(base_url):
    if _repos_cache["data"] is not None:
        if _repos_cache["expires_at"] <= time.time():
            # Stale: answer right away with the old data, refresh in the background.
            start_refresh(base_url)
        return _repos_cache["data"]

    # Nothing cached yet (fresh start): wait for the refresh. Concurrent
    # requests share the same task; shield it so a client disconnect
    # doesn't cancel it for everyone.
    return await asyncio.shield(start_refresh(base_url))


# GET endpoint to fetch pinned repositories
@app.get("/api/pinned")
async def get_pinned(request: Request):
    base_url = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return await get_repos(base_url)
