import asyncio
import hashlib
import io
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from PIL import Image

# Load .env variables
load_dotenv()

logger = logging.getLogger("uvicorn.error")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME")

# Public URL of this API (e.g. https://octo-pinned-api.onrender.com).
# Used to build absolute image URLs. Behind a proxy (Render),
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

# Image cache: preview images are downloaded once, resized and served by the API
IMAGE_TIMEOUT = 20.0  # seconds, per connect/read step of an image download
IMAGE_TOTAL_TIMEOUT = 45.0  # seconds, whole image download
IMAGE_TTL = 24 * 60 * 60  # seconds, re-download each source image daily
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # skip source images larger than this
IMAGE_MAX_WIDTH = 800  # px, cards are ~270-400px wide (x2 for retina)
IMAGE_QUALITY = 80  # WebP quality
IMAGE_BROWSER_CACHE = 7 * 24 * 60 * 60  # seconds; URLs are content-hashed

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
    # restart doesn't wait. Needs PUBLIC_BASE_URL to build image URLs.
    if PUBLIC_BASE_URL:
        start_refresh(PUBLIC_BASE_URL)
    yield
    for task in (_refresh_task, _image_task):
        if task and not task.done():
            task.cancel()
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
# Image cache (download once, resize, serve from the API)
# ---------------------------------------------------------------------------

# cache filename -> WebP bytes
_image_store: dict[str, bytes] = {}
# source URL -> (cache filename, expires_at)
_image_sources: dict[str, tuple[str, float]] = {}
# filenames used by the previous refresh, kept so clients holding the
# previous API response don't get 404s
_previous_image_names: set[str] = set()


def to_webp(data: bytes) -> bytes:
    """Resize to IMAGE_MAX_WIDTH (never upscale) and encode as WebP."""
    with Image.open(io.BytesIO(data)) as img:
        img.seek(0)  # first frame of animated images
        img = img.convert("RGBA" if "A" in img.getbands() or "transparency" in img.info else "RGB")
        if img.width > IMAGE_MAX_WIDTH:
            height = round(img.height * IMAGE_MAX_WIDTH / img.width)
            img = img.resize((IMAGE_MAX_WIDTH, height), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, "WEBP", quality=IMAGE_QUALITY, method=4)
        return out.getvalue()


async def download_image(url: str) -> bytes:
    async with http_client.stream("GET", url, timeout=IMAGE_TIMEOUT) as resp:
        resp.raise_for_status()
        if not resp.headers.get("content-type", "").lower().startswith("image/"):
            raise ValueError(f"not an image: {resp.headers.get('content-type')}")
        chunks, size = [], 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                raise ValueError("image too large")
            chunks.append(chunk)
    return b"".join(chunks)


async def cache_image(repo_name: str, url: str | None, base_url: str) -> str | None:
    """
    Return a URL served by this API for the image at `url`, downloading and
    resizing it at most once per IMAGE_TTL. Falls back to the original URL
    if the image can't be downloaded or processed.
    """
    if not url or is_override(url, base_url):
        return url  # nothing to cache, or an override file we already serve

    cached = _image_sources.get(url)
    if cached and cached[1] > time.time() and cached[0] in _image_store:
        return f"{base_url}/cached-images/{cached[0]}"

    try:
        data = await asyncio.wait_for(download_image(url), IMAGE_TOTAL_TIMEOUT)
        webp = await asyncio.to_thread(to_webp, data)
    except Exception as exc:
        logger.warning(
            "Could not cache image for %s (%s): %s %s",
            repo_name, url, type(exc).__name__, exc,
        )
        if cached and cached[0] in _image_store:
            return f"{base_url}/cached-images/{cached[0]}"  # keep the old copy
        return url

    slug = re.sub(r"[^a-z0-9]+", "-", repo_name.lower()).strip("-") or "repo"
    name = f"{slug}-{hashlib.sha1(webp).hexdigest()[:10]}.webp"
    _image_store[name] = webp
    _image_sources[url] = (name, time.time() + IMAGE_TTL)
    return f"{base_url}/cached-images/{name}"


def is_override(url: str, base_url: str) -> bool:
    return url.startswith(f"{base_url}/images/")


def cached_image_url(url: str | None, base_url: str) -> str | None:
    """API URL of the cached copy of `url` (even if due for refresh), else None."""
    if not url or is_override(url, base_url):
        return None
    cached = _image_sources.get(url)
    if cached and cached[0] in _image_store:
        return f"{base_url}/cached-images/{cached[0]}"
    return None


def needs_download(url: str | None, base_url: str) -> bool:
    if not url or is_override(url, base_url):
        return False
    cached = _image_sources.get(url)
    return not cached or cached[1] <= time.time() or cached[0] not in _image_store


_image_task: asyncio.Task | None = None


async def cache_images(names: list[str], sources: list[str | None], base_url: str):
    """Download/resize images; point each repo at its copy as soon as it's ready."""

    async def cache_one(name, src):
        image = await cache_image(name, src, base_url)
        data = _repos_cache["data"]
        if data and any(r["name"] == name for r in data):
            _repos_cache["data"] = [
                {**r, "image": image} if r["name"] == name else r for r in data
            ]
        return image

    images = await asyncio.gather(*(cache_one(n, s) for n, s in zip(names, sources)))
    prune_image_store(
        {img.rsplit("/", 1)[1] for img in images if img and "/cached-images/" in img}
    )


def start_image_caching(names, sources, base_url):
    """Run cache_images in the background unless it is already running."""
    global _image_task
    if _image_task is None or _image_task.done():
        _image_task = asyncio.create_task(cache_images(names, sources, base_url))
        _image_task.add_done_callback(_log_refresh_error)


def prune_image_store(current_names: set[str]):
    """Drop images not used by the current or the previous refresh."""
    global _previous_image_names
    keep = current_names | _previous_image_names
    for name in list(_image_store):
        if name not in keep:
            del _image_store[name]
    for url, (name, _) in list(_image_sources.items()):
        if name not in _image_store:
            del _image_sources[url]
    _previous_image_names = current_names


@app.get("/cached-images/{name}")
async def get_cached_image(name: str):
    data = _image_store.get(name)
    if data is None:
        raise HTTPException(status_code=404, detail="Image not found")
    return Response(
        content=data,
        media_type="image/webp",
        headers={"Cache-Control": f"public, max-age={IMAGE_BROWSER_CACHE}, immutable"},
    )


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

    sources = await asyncio.gather(*(resolve_image(r, base_url) for r in nodes))

    # Never wait on image downloads: use the cached copy when there is one,
    # otherwise the original URL, and download missing images in the background.
    images = [cached_image_url(src, base_url) or src for src in sources]

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

    if any(needs_download(src, base_url) for src in sources):
        start_image_caching([r["name"] for r in nodes], sources, base_url)
    return repos


def _log_refresh_error(task: asyncio.Task):
    if not task.cancelled() and task.exception() is not None:
        exc = task.exception()
        logger.warning("Background task failed: %s", getattr(exc, "detail", exc))


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
