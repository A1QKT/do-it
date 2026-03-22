#!/usr/bin/env python3
"""
Crawl https://media-seed-bot.lovable.app/ (or MEDIA_SEED_URL), extract visible text,
heuristically map place-like labels to numeric scores, merge into a JSON file, and log text.

Edit the CONFIG block below for default behavior. CLI flags override those defaults.

Downloads image/audio URLs from the page into MEDIA_DOWNLOAD_DIR and can score them via OpenRouter
(same idea as POST /v1/media/*-score); for scoring, install: pip install -r api/requirements.txt and set OPENAI_API_KEY or OPENROUTER_API_KEY.

With HARVEST_MEDIA_SEED_UI_PANELS (default True), Playwright is required — install playwright + chromium.
Use --browser if you disable harvest but still need a rendered page.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

try:
    import httpx
except ModuleNotFoundError as e:
    if e.name != "httpx":
        raise
    print(
        "Missing dependency: httpx. Fix one of:\n"
        "  • From repo root: ./script/crawl.sh — auto-runs `pip install -r requirements-crawl.txt` "
        "into .venv or venv on first use (unless CRAWL_NO_BOOTSTRAP=1).\n"
        "  • Manual: .venv/bin/pip install -r requirements-crawl.txt  (-r reads the file; required.)\n"
        "  • Or use the same Python you used for `pip install` to run this script.",
        file=sys.stderr,
    )
    raise SystemExit(1) from e

from bs4 import BeautifulSoup

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(_REPO_ROOT / ".env")

# =============================================================================
# CONFIG — change defaults here (no CLI required)
# =============================================================================
# Repeat crawling until Ctrl+C, sleeping this many seconds between runs.
CRAWL_LOOP_FOREVER = True
CRAWL_INTERVAL_SECONDS = 10
# JSON output path (override: -o / --output)
DEFAULT_OUTPUT_PATH = "media_seed_export.json"
# Max characters of HTML stored under _meta.raw.html_preview (0 = omit preview text)
MAX_RAW_HTML_PREVIEW_CHARS = 262_144
# HTTP / Playwright page load timeout (seconds)
FETCH_TIMEOUT_SECONDS = 45.0
# Start URL if MEDIA_SEED_URL env is unset (override: --url)
DEFAULT_PAGE_URL = "https://media-seed-bot.lovable.app/"
# Discover <img>/<audio>/… and regex URLs; save under this folder (relative to repo root)
DOWNLOAD_PAGE_MEDIA = True
MEDIA_DOWNLOAD_DIR = "media_seed_downloads"
MAX_MEDIA_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_MEDIA_ITEMS_PER_CRAWL = 12
# After download, call OpenAI or OpenRouter (same 1–100 + explanation as API; needs OPENAI_API_KEY or OPENROUTER_API_KEY)
SCORE_DOWNLOADED_MEDIA_OPENROUTER = True
# Use Playwright to read the three app panels (Audio / Paragraph Text / Image) and save real
# files (including blob: previews) — not a full-page screenshot and not random site chrome images.
HARVEST_MEDIA_SEED_UI_PANELS = True
# =============================================================================

ENV_URL = "MEDIA_SEED_URL"
DEFAULT_URL = DEFAULT_PAGE_URL
DEFAULT_OUT = DEFAULT_OUTPUT_PATH

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif")
_AUDIO_EXTS = (".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".webm")
_ALL_MEDIA_EXTS = _IMAGE_EXTS + _AUDIO_EXTS

# "location label" : 1–3 digit score (whole line)
LINE_SCORE = re.compile(
    r"^\s*(?P<loc>[^\d:{}\[\]]{2,120}?)\s*[:：|–\-]\s*(?P<score>\d{1,3})\s*$",
    re.MULTILINE | re.UNICODE,
)
# Reversed: 42 — ngã 4 …
LINE_SCORE_REV = re.compile(
    r"^\s*(?P<score>\d{1,3})\s*[:：|–\-]\s*(?P<loc>[^\d:{}\[\]]{2,120}?)\s*$",
    re.MULTILINE | re.UNICODE,
)


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stderr,
    )


def fetch_html(url: str, timeout: float) -> tuple[str, dict[str, Any]]:
    headers = {
        "User-Agent": "traffic-simulation-media-seed-crawl/1.0 (+local cron)",
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    }
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        meta: dict[str, Any] = {
            "mode": "http",
            "http_status": r.status_code,
            "final_url": str(r.url),
            "content_type": r.headers.get("content-type", ""),
        }
        return r.text, meta


def _chromium_launch_args() -> list[str]:
    """Flags for headless Chromium on Linux servers, Docker, and when euid is root (sandbox not allowed)."""
    args: list[str] = []
    if sys.platform == "linux":
        # Small /dev/shm in containers often crashes Chromium without this.
        args.append("--disable-dev-shm-usage")

    _truthy = {"1", "true", "yes", "on"}
    no_sandbox_env = os.getenv("PLAYWRIGHT_CHROMIUM_NO_SANDBOX", "").strip().lower() in _truthy
    ci = os.getenv("CI", "").strip().lower() in _truthy
    root = False
    ge = getattr(os, "geteuid", None)
    if callable(ge):
        try:
            root = ge() == 0
        except OSError:
            pass

    if root or no_sandbox_env or ci:
        args.extend(["--no-sandbox", "--disable-setuid-sandbox"])

    extra = os.getenv("PLAYWRIGHT_CHROMIUM_EXTRA_ARGS", "").strip()
    if extra:
        args.extend(shlex.split(extra))
    return args


def _launch_chromium(p: Any, *, headless: bool = True) -> Any:
    launch_args = _chromium_launch_args()
    kw: dict[str, Any] = {"headless": headless}
    if launch_args:
        kw["args"] = launch_args

    def _go(k: dict[str, Any]) -> Any:
        return p.chromium.launch(**k)

    try:
        return _go(kw)
    except Exception as e:
        err = str(e).lower()
        if "executable doesn't exist" in err or "download new browsers" in err:
            raise RuntimeError(
                "Playwright Chromium is not installed. From the repo root run:\n"
                "  python -m playwright install chromium\n"
                "(use the same venv as the crawl, e.g. venv/bin/python -m playwright install chromium)\n"
                "./script/crawl.sh runs this automatically unless CRAWL_NO_PLAYWRIGHT_INSTALL=1."
            ) from e
        closed = "target closed" in err or "has been closed" in err or "browser has been closed" in err
        # Docker / hardened images often need --no-sandbox even when not root; retry once.
        if closed and "--no-sandbox" not in launch_args:
            retry_kw = {
                "headless": headless,
                "args": [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            }
            try:
                return _go(retry_kw)
            except Exception as e2:
                e = e2
                err = str(e2).lower()
                closed = "target closed" in err or "has been closed" in err or "browser has been closed" in err
        if closed:
            raise RuntimeError(
                "Playwright Chromium exited right after launch. Typical fixes:\n"
                "  • If running as root (euid 0): Chromium needs --no-sandbox (enabled automatically; "
                "or set PLAYWRIGHT_CHROMIUM_NO_SANDBOX=1).\n"
                "  • In Docker: mount a larger /dev/shm (e.g. docker run --shm-size=1g) or rely on "
                "--disable-dev-shm-usage (added on Linux).\n"
                "  • Install OS libraries: python -m playwright install-deps chromium (Debian/Ubuntu).\n"
                "  • Extra flags: PLAYWRIGHT_CHROMIUM_EXTRA_ARGS e.g. --single-process (last resort)."
            ) from e
        raise


def fetch_rendered(url: str, timeout_ms: int) -> tuple[str, str]:
    """Return (visible inner text, raw HTML string)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _launch_chromium(p)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            inner = page.inner_text("body")
            html = page.content()
            if isinstance(html, bytes):
                html = html.decode("utf-8", errors="replace")
            return inner, html
        finally:
            browser.close()


# App marks crawl targets: data-crawl-content="text"|"image"|"audio" (see section#image, etc.)
PANEL_EXTRACT_JS = r"""() => {
  function crawlText() {
    const root =
      document.querySelector('[data-crawl-content="text"]') ||
      document.querySelector('section#text [data-crawl-content="text"]');
    if (!root) return "";
    const ta = root.querySelector("textarea");
    if (ta) return (ta.value || "").trim();
    return (root.innerText || "").trim();
  }
  function crawlImageSrc() {
    let img =
      document.querySelector('[data-crawl-content="image"] img[src]') ||
      document.querySelector('section#image [data-crawl-content="image"] img[src]') ||
      document.querySelector("section#image img[src]");
    if (!img) return null;
    const s = img.getAttribute("src") || "";
    if (!s || s.startsWith("data:image/svg")) return null;
    return s;
  }
  function crawlAudioSrc() {
    const wrap =
      document.querySelector('[data-crawl-content="audio"]') ||
      document.querySelector("section#audio");
    if (!wrap) return null;
    const au = wrap.querySelector("audio");
    if (au) {
      let s = au.currentSrc || au.getAttribute("src") || "";
      if (s) return s;
      const so = au.querySelector("source[src]");
      if (so) return so.getAttribute("src") || so.src;
    }
    const so = wrap.querySelector("source[src]");
    if (so) return so.getAttribute("src") || so.src;
    const a = wrap.querySelector("a[href]");
    if (a && a.href && /\.(mp3|wav|ogg|m4a|aac|flac|webm)(\?|$)/i.test(a.href)) return a.href;
    return null;
  }
  return {
    paragraphText: crawlText(),
    imageSrc: crawlImageSrc(),
    audioSrc: crawlAudioSrc(),
  };
}"""


def extract_crawl_markers_from_html(html: str, base_url: str) -> dict[str, Any]:
    """Parse data-crawl-content="text"|image|audio from HTML (SSR or saved snapshot)."""
    soup = BeautifulSoup(html, "html.parser")
    paragraph = ""
    text_root = soup.select_one('[data-crawl-content="text"]')
    if text_root:
        ta = text_root.find("textarea")
        if ta:
            paragraph = (ta.get_text() or "").strip()
        else:
            paragraph = text_root.get_text(separator="\n", strip=True)

    img_el = (
        soup.select_one('[data-crawl-content="image"] img[src]')
        or soup.select_one('section#image [data-crawl-content="image"] img[src]')
        or soup.select_one("section#image img[src]")
    )
    image_src: str | None = None
    if img_el and img_el.get("src"):
        s = img_el["src"].strip()
        if s and not s.startswith("data:image/svg"):
            image_src = urljoin(base_url, s)

    audio_wrap = soup.select_one('[data-crawl-content="audio"]') or soup.select_one("section#audio")
    audio_src: str | None = None
    if audio_wrap:
        au = audio_wrap.find("audio")
        if au and au.get("src"):
            audio_src = urljoin(base_url, au["src"])
        if not audio_src and au:
            so = au.find("source", src=True)
            if so:
                audio_src = urljoin(base_url, so["src"])
        if not audio_src:
            so = audio_wrap.find("source", src=True)
            if so:
                audio_src = urljoin(base_url, so["src"])
        if not audio_src:
            a = audio_wrap.find("a", href=True)
            if a and re.search(r"\.(mp3|wav|ogg|m4a|aac|flac|webm)(\?|$)", a["href"], re.I):
                audio_src = urljoin(base_url, a["href"])

    return {
        "paragraph_text": paragraph,
        "image_src": image_src,
        "audio_src": audio_src,
    }


def _safe_filename_from_url(url: str, fallback_stem: str, ext: str) -> str:
    """Prefer storage filename from URL path when safe."""
    path = urlparse(url).path
    try:
        base = unquote(Path(path).name)
    except Exception:
        base = Path(path).name
    if (
        base
        and 0 < len(base) <= 200
        and "/" not in base
        and "\\" not in base
        and ".." not in base
    ):
        return base
    digest = hashlib.sha256(url.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{fallback_stem}_{digest}{ext}"


def download_crawl_marker_assets(
    client: httpx.Client,
    marker: dict[str, Any],
    dest_dir: Path,
    timeout: float,
    max_bytes: int,
) -> list[dict[str, Any]]:
    """Download image/audio from https URLs and save paragraph text (no blob: — use Playwright for those)."""
    rows: list[dict[str, Any]] = []
    dest_dir.mkdir(parents=True, exist_ok=True)

    ptext = (marker.get("paragraph_text") or "").strip()
    if ptext:
        path = dest_dir / "crawl_text.txt"
        path.write_text(ptext, encoding="utf-8")
        rows.append(
            {
                "source": "data-crawl-content=text",
                "kind": "text",
                "url": "(textarea)",
                "local_path": _rel_repo_path(path),
                "bytes": path.stat().st_size,
                "content_type": "text/plain;charset=utf-8",
            }
        )

    for kind, key, prefix in (
        ("image", "image_src", "crawl_image"),
        ("audio", "audio_src", "crawl_audio"),
    ):
        src = marker.get(key)
        if not src or not isinstance(src, str):
            continue
        if src.startswith("blob:") or src.startswith("data:"):
            rows.append(
                {
                    "source": f"data-crawl-content={kind}",
                    "kind": "audio" if kind == "audio" else "image",
                    "url": src[:200] + ("…" if len(src) > 200 else ""),
                    "error": "blob_or_data_requires_playwright",
                }
            )
            continue
        row: dict[str, Any] = {
            "source": f"data-crawl-content={kind}",
            "kind": "audio" if kind == "audio" else "image",
            "url": src[:500] + ("…" if len(src) > 500 else ""),
        }
        try:
            r = client.get(src, follow_redirects=True, timeout=timeout)
            r.raise_for_status()
            body = r.content
            if len(body) > max_bytes:
                row["error"] = f"too_large:{len(body)}"
                rows.append(row)
                continue
            ct = r.headers.get("content-type", "").split(";")[0].strip()
            ext = _pick_extension(kind, src, ct, body)
            fname = _safe_filename_from_url(src, prefix, ext)
            if not fname.lower().endswith(ext.lower()) and "." not in fname:
                fname = f"{fname}{ext}"
            out = dest_dir / fname
            if out.exists():
                d = hashlib.sha256(src.encode("utf-8", errors="replace")).hexdigest()[:8]
                stem = Path(fname).stem
                suf = Path(fname).suffix or ext
                out = dest_dir / f"{stem}_{d}{suf}"
            out.write_bytes(body)
            row["local_path"] = _rel_repo_path(out)
            row["bytes"] = len(body)
            row["content_type"] = ct
        except Exception as e:
            row["error"] = str(e)
        rows.append(row)

    return rows


def _decode_data_url(url: str, max_bytes: int) -> tuple[bytes, str | None]:
    if not url.startswith("data:"):
        raise ValueError("not a data URL")
    head, _, b64part = url.partition(",")
    if not b64part:
        raise ValueError("malformed data URL")
    if ";base64" in head:
        raw = base64.standard_b64decode(b64part)
    else:
        from urllib.parse import unquote_to_bytes

        raw = unquote_to_bytes(b64part)
    if len(raw) > max_bytes:
        raise ValueError(f"too_large:{len(raw)}")
    ct = None
    if head.lower().startswith("data:"):
        meta = head[5:].split(";")[0].strip()
        if meta:
            ct = meta
    return raw, ct


def _download_resource_from_page(page: Any, url: str, max_bytes: int) -> tuple[bytes, str | None]:
    if url.startswith("data:"):
        return _decode_data_url(url, max_bytes)
    if url.startswith("blob:"):
        data = page.evaluate(
            """async (u) => {
                const r = await fetch(u);
                if (!r.ok) throw new Error("fetch " + r.status);
                const buf = await r.arrayBuffer();
                return Array.from(new Uint8Array(buf));
            }""",
            url,
        )
        b = bytes(data)
        if len(b) > max_bytes:
            raise ValueError(f"too_large:{len(b)}")
        return b, None
    resp = page.request.get(url, timeout=120_000)
    if resp.status >= 400:
        raise ValueError(f"HTTP {resp.status}")
    b = resp.body()
    if len(b) > max_bytes:
        raise ValueError(f"too_large:{len(b)}")
    ct = resp.headers.get("content-type")
    return b, ct


def _pick_extension(kind: str, url: str, content_type: str | None, body: bytes) -> str:
    ct = (content_type or "").split(";")[0].lower()
    if "png" in ct:
        return ".png"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if "webp" in ct:
        return ".webp"
    if "gif" in ct:
        return ".gif"
    if "mpeg" in ct or "mp3" in ct:
        return ".mp3"
    if "wav" in ct:
        return ".wav"
    if "ogg" in ct:
        return ".ogg"
    path = urlparse(url).path.lower()
    for e in _IMAGE_EXTS + _AUDIO_EXTS:
        if path.endswith(e):
            return e
    if kind == "image":
        if body.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if body.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if len(body) > 12 and body.startswith(b"RIFF") and body[8:12] == b"WEBP":
            return ".webp"
        return ".bin"
    if kind == "audio":
        if body.startswith(b"ID3") or (len(body) > 1 and body[0:1] == b"\xff" and (body[1] & 0xE0) == 0xE0):
            return ".mp3"
        if body.startswith(b"RIFF") and b"WAVE" in body[:12]:
            return ".wav"
        return ".bin"
    return ".bin"


def _rel_repo_path(path: Path) -> str:
    return str(path.resolve().relative_to(_REPO_ROOT.resolve())).replace("\\", "/")


def playwright_fetch_page_and_harvest(
    url: str,
    timeout_ms: int,
    dest_dir: Path,
    max_bytes: int,
    *,
    save_files: bool,
) -> tuple[str, str, list[dict[str, Any]], dict[str, Any]]:
    """Load page in Chromium; optionally save paragraph + panel image/audio (real src/blob/data, not screenshots)."""
    from playwright.sync_api import sync_playwright

    rows: list[dict[str, Any]] = []
    fetch_meta: dict[str, Any] = {
        "mode": "browser+ui_panels" if save_files else "browser+ui_panels_readonly",
        "requested_url": url,
    }

    with sync_playwright() as p:
        browser = _launch_chromium(p)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            page.wait_for_timeout(900)
            inner = page.inner_text("body")
            html = page.content()
            if isinstance(html, bytes):
                html = html.decode("utf-8", errors="replace")

            info: dict[str, Any] = page.evaluate(PANEL_EXTRACT_JS)
            ptext = (info.get("paragraphText") or "").strip()
            fetch_meta["panel_paragraph_preview_chars"] = len(ptext)
            fetch_meta["panel_image_src"] = info.get("imageSrc")
            fetch_meta["panel_audio_src"] = info.get("audioSrc")

            if not save_files:
                return inner, html, rows, fetch_meta

            dest_dir.mkdir(parents=True, exist_ok=True)

            if ptext:
                path = dest_dir / "crawl_text.txt"
                path.write_text(ptext, encoding="utf-8")
                rows.append(
                    {
                        "source": "data-crawl-content=text",
                        "kind": "text",
                        "url": "(textarea)",
                        "local_path": _rel_repo_path(path),
                        "bytes": path.stat().st_size,
                        "content_type": "text/plain;charset=utf-8",
                    }
                )

            for kind, key, prefix in (
                ("image", "imageSrc", "crawl_image"),
                ("audio", "audioSrc", "crawl_audio"),
            ):
                src = info.get(key)
                if not src or not isinstance(src, str):
                    continue
                row: dict[str, Any] = {
                    "source": f"data-crawl-content={kind}",
                    "kind": "audio" if kind == "audio" else "image",
                    "url": src[:500] + ("…" if len(src) > 500 else ""),
                }
                try:
                    body, ct = _download_resource_from_page(page, src, max_bytes)
                    ext = _pick_extension(kind, src, ct, body)
                    fname = _safe_filename_from_url(src, prefix, ext)
                    if not Path(fname).suffix:
                        fname = f"{fname}{ext}"
                    out = dest_dir / fname
                    if out.exists():
                        d = hashlib.sha256(src.encode("utf-8", errors="replace")).hexdigest()[:8]
                        stem = Path(fname).stem
                        suf = Path(fname).suffix or ext
                        out = dest_dir / f"{stem}_{d}{suf}"
                    out.write_bytes(body)
                    row["local_path"] = _rel_repo_path(out)
                    row["bytes"] = len(body)
                    row["content_type"] = ct or ""
                except Exception as e:
                    row["error"] = str(e)
                rows.append(row)

            return inner, html, rows, fetch_meta
        finally:
            browser.close()


def html_to_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def extract_json_blobs(html: str) -> list[Any]:
    """Parse JSON from script tags and common embedded patterns."""
    found: list[Any] = []
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        if raw.startswith("{") or raw.startswith("["):
            try:
                found.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
        m = re.search(r"__NEXT_DATA__\s*=\s*(\{.*?\})\s*;", raw, re.DOTALL)
        if m:
            try:
                found.append(json.loads(m.group(1)))
            except json.JSONDecodeError:
                pass
    return found


def _collect_str_num_pairs(obj: Any, out: dict[str, str], depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                continue
            ks = k.strip()
            if ks.startswith("_"):
                continue
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                n = int(v)
                if 1 <= n <= 100 and len(ks) >= 3:
                    out[ks] = str(n)
            elif isinstance(v, str) and v.strip().isdigit():
                n = int(v.strip())
                if 1 <= n <= 100 and len(ks) >= 3:
                    out[ks] = str(n)
            else:
                _collect_str_num_pairs(v, out, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _collect_str_num_pairs(item, out, depth + 1)


def parse_scores_from_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pat in (LINE_SCORE, LINE_SCORE_REV):
        for m in pat.finditer(text):
            loc = (m.group("loc") or "").strip()
            score = (m.group("score") or "").strip()
            if not loc or not score:
                continue
            n = int(score)
            if 1 <= n <= 100:
                out[loc] = str(n)
    return out


def parse_scores_from_json_blobs(blobs: list[Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for b in blobs:
        _collect_str_num_pairs(b, out)
    return out


def _truncate(s: str, max_len: int) -> tuple[str, bool]:
    if max_len <= 0:
        return "", True
    if len(s) <= max_len:
        return s, False
    return s[:max_len], True


def build_raw_payload(
    *,
    fetch_meta: dict[str, Any],
    html: str,
    extracted_text: str,
    json_blobs: list[Any],
    max_html_chars: int,
) -> dict[str, Any]:
    html_preview, html_truncated = _truncate(html, max_html_chars)
    return {
        **fetch_meta,
        "html_length": len(html),
        "html_preview": html_preview,
        "html_truncated": html_truncated,
        "extracted_text": extracted_text,
        "json_blobs_count": len(json_blobs),
    }


def _path_suffix_media(url: str) -> str | None:
    path = urlparse(url).path.lower()
    if "?" in path:
        path = path.split("?")[0]
    for ext in _ALL_MEDIA_EXTS:
        if path.endswith(ext):
            return ext
    return None


def collect_media_urls(html: str, base_url: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["img", "audio", "video", "source"]):
        u = tag.get("src") or tag.get("data-src") or tag.get("data-lazy-src")
        if not u or not isinstance(u, str):
            continue
        u = u.strip()
        if u.startswith("data:") or u.startswith("javascript:"):
            continue
        abs_u = urljoin(base_url, u)
        if abs_u.startswith("http") and abs_u not in seen and _path_suffix_media(abs_u):
            seen.add(abs_u)
            out.append(abs_u)
    for m in re.finditer(r'https?://[^\s"\'<>]+', html):
        u = m.group(0).rstrip(").,;]>'\"")
        if u not in seen and _path_suffix_media(u):
            seen.add(u)
            out.append(u)
    return out


def media_kind_from_url(u: str) -> str:
    p = urlparse(u).path.lower()
    for e in _AUDIO_EXTS:
        if p.endswith(e):
            return "audio"
    return "image"


def download_media_items(
    client: httpx.Client,
    urls: list[str],
    dest_dir: Path,
    timeout: float,
    max_bytes: int,
    max_items: int,
) -> list[dict[str, Any]]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for u in urls[:max_items]:
        kind = media_kind_from_url(u)
        digest = hashlib.sha256(u.encode("utf-8")).hexdigest()[:16]
        ext = _path_suffix_media(u) or ".bin"
        fname = f"{digest}{ext}"
        path = dest_dir / fname
        row: dict[str, Any] = {"url": u, "kind": kind}
        try:
            r = client.get(u, follow_redirects=True, timeout=timeout)
            r.raise_for_status()
            body = r.content
            if len(body) > max_bytes:
                row["error"] = f"too_large:{len(body)}"
                rows.append(row)
                continue
            path.write_bytes(body)
            ct = r.headers.get("content-type", "").split(";")[0].strip()
            rel = path.resolve().relative_to(_REPO_ROOT.resolve())
            row["local_path"] = str(rel).replace("\\", "/")
            row["bytes"] = len(body)
            row["content_type"] = ct
        except Exception as e:
            row["error"] = str(e)
        rows.append(row)
    return rows


def _export_key_for_scored_media_item(item: dict[str, Any], idx: int) -> str:
    """Human-readable key for top-level JSON (e.g. first line of paragraph, image:file.jfif)."""
    kind = item.get("kind") or "unknown"
    lp = item.get("local_path")
    if kind == "text" and lp:
        path = _REPO_ROOT / lp
        try:
            t = path.read_text(encoding="utf-8", errors="replace").strip()
            if t:
                line = (t.splitlines() or [t])[0].strip()
                if len(line) >= 6:
                    return line[:220]
        except OSError:
            pass
        return "crawl:text"
    if lp:
        name = Path(lp).name
        if kind == "image":
            return f"image:{name}"
        if kind == "audio":
            return f"audio:{name}"
        return f"{kind}:{name}"
    return f"crawl:{kind}:{idx}"


# When the crawl saves a Vietnamese paragraph alongside TTS audio, force Whisper to use ``vi`` so
# transcription matches the spoken content (auto-detect often mislabels short clips as English).
_WHISPER_VI_HINT_RE = re.compile(
    r"[ăâđêôơưĂÂĐÊÔƠƯạảãấầẩậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]"
)


def _whisper_language_hint_from_crawl_items(items: list[dict[str, Any]]) -> str | None:
    for it in items:
        if it.get("kind") != "text" or it.get("error"):
            continue
        lp = it.get("local_path")
        if not lp:
            continue
        path = _REPO_ROOT / lp
        if not path.is_file():
            continue
        try:
            blob = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(blob.strip()) < 10:
            continue
        if _WHISPER_VI_HINT_RE.search(blob):
            return "vi"
    return None


def export_keys_from_media_openrouter(media_downloads: list[dict[str, Any]]) -> dict[str, str]:
    """Map scored media rows to location-key → score string for the root JSON object."""
    result: dict[str, str] = {}
    for idx, item in enumerate(media_downloads):
        o = item.get("openrouter")
        if not isinstance(o, dict) or o.get("error"):
            continue
        if "score" not in o:
            continue
        try:
            score_str = str(int(o["score"]))
        except (TypeError, ValueError):
            continue
        key = _export_key_for_scored_media_item(item, idx)
        base_key = key
        n = 2
        while key in result:
            key = f"{base_key} ({n})"
            n += 1
        result[key] = score_str
    return result


def score_media_items_with_openrouter(items: list[dict[str, Any]]) -> None:
    from api import media_score as ms
    from api.llm_provider import default_chat_model

    whisper_lang = _whisper_language_hint_from_crawl_items(items)
    if whisper_lang:
        logging.info("Using Whisper language=%s (inferred from crawl paragraph text)", whisper_lang)

    for it in items:
        if it.get("error") or not it.get("local_path"):
            continue
        p = _REPO_ROOT / it["local_path"]
        if not p.is_file():
            it["openrouter"] = {"error": "local file missing"}
            continue
        try:
            if it.get("kind") == "text":
                txt = p.read_text(encoding="utf-8", errors="replace")
                res = ms.score_media_text_sync(txt, default_chat_model())
                it["openrouter"] = res.model_dump()
                continue
            data = p.read_bytes()
            if it.get("kind") == "audio":
                fmt = ms.audio_openrouter_format(it.get("content_type"), p.name)
                res = ms.score_media_audio_via_transcript_sync(
                    data,
                    openrouter_format=fmt,
                    filename_hint=p.name,
                    whisper_language=whisper_lang,
                )
            else:
                mime = it.get("content_type") or mimetypes.guess_type(p.name)[0] or "image/jpeg"
                if not mime.startswith("image/"):
                    mime = "image/jpeg"
                res = ms.score_media_image_sync(data, mime, default_chat_model())
            it["openrouter"] = res.model_dump()
        except Exception as e:
            logging.exception("OpenRouter scoring failed for %s", it.get("url"))
            it["openrouter"] = {"error": str(e)}


def load_json_path(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("Existing output is not valid JSON; starting fresh: %s", path)
        return {}
    return data if isinstance(data, dict) else {}


def merge_output(
    existing: dict[str, Any],
    new_scores: dict[str, str],
    *,
    url: str,
    text_len: int,
    replace_all_scores: bool,
    raw: dict[str, Any],
    media_downloads: list[dict[str, Any]],
    openrouter_media_keys: list[str] | None = None,
) -> dict[str, Any]:
    base: dict[str, Any] = {}
    if not replace_all_scores:
        for k, v in existing.items():
            if k == "_meta":
                continue
            if not k.startswith("_"):
                base[k] = v
    base.update(new_scores)
    omk = openrouter_media_keys or []
    base["_meta"] = {
        "crawled_at": datetime.now(timezone.utc).isoformat(),
        "url": url,
        "extracted_text_chars": text_len,
        "score_keys_this_run": sorted(new_scores.keys()),
        "openrouter_media_score_keys": sorted(omk),
        "raw": raw,
        "media_downloads": media_downloads,
    }
    return base


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def run_once(args: argparse.Namespace) -> dict[str, str]:
    url = args.url or os.environ.get(ENV_URL) or DEFAULT_URL
    max_html = int(args.max_raw_html)
    use_playwright = args.browser or HARVEST_MEDIA_SEED_UI_PANELS
    do_download = DOWNLOAD_PAGE_MEDIA and not args.no_download_media

    if use_playwright and HARVEST_MEDIA_SEED_UI_PANELS:
        dest = _REPO_ROOT / MEDIA_DOWNLOAD_DIR
        text, html, panel_rows, fetch_meta = playwright_fetch_page_and_harvest(
            url,
            timeout_ms=int(args.timeout * 1000),
            dest_dir=dest,
            max_bytes=MAX_MEDIA_DOWNLOAD_BYTES,
            save_files=do_download,
        )
        blobs = extract_json_blobs(html)
        base_for_media = url
    elif args.browser:
        text, html = fetch_rendered(url, timeout_ms=int(args.timeout * 1000))
        blobs = []
        fetch_meta = {
            "mode": "browser",
            "requested_url": url,
        }
        base_for_media = url
    else:
        html, fetch_meta = fetch_html(url, timeout=args.timeout)
        text = html_to_visible_text(html)
        blobs = extract_json_blobs(html)
        base_for_media = str(fetch_meta.get("final_url") or url)

    raw = build_raw_payload(
        fetch_meta=fetch_meta,
        html=html,
        extracted_text=text,
        json_blobs=blobs,
        max_html_chars=max_html,
    )

    logging.info("Extracted text (%d chars):\n%s", len(text), text)

    from_text = parse_scores_from_text(text)
    from_json = parse_scores_from_json_blobs(blobs)
    merged_scores: dict[str, str] = {**from_json, **from_text}
    if from_text and from_json:
        logging.debug("Scores from JSON keys: %s", sorted(from_json.keys()))
        logging.debug("Scores from text keys: %s", sorted(from_text.keys()))
    if merged_scores:
        logging.info("Scores from page text/JSON heuristics: %s", merged_scores)

    media_downloads: list[dict[str, Any]] = []
    wants_openrouter = SCORE_DOWNLOADED_MEDIA_OPENROUTER and not args.no_openrouter

    if use_playwright and HARVEST_MEDIA_SEED_UI_PANELS and do_download:
        media_downloads = panel_rows
        logging.info(
            "UI panel harvest: %d item(s) (paragraph / image / audio — files not screenshots)",
            len(media_downloads),
        )
    elif do_download:
        dest = _REPO_ROOT / MEDIA_DOWNLOAD_DIR
        marker = extract_crawl_markers_from_html(html, base_for_media)
        has_crawl_markers = (
            bool((marker.get("paragraph_text") or "").strip())
            or bool(marker.get("image_src"))
            or bool(marker.get("audio_src"))
        )
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=args.timeout,
                headers={
                    "User-Agent": "traffic-simulation-media-seed-crawl/1.0 (+media)",
                    "Accept": "*/*",
                },
            ) as dl:
                if has_crawl_markers:
                    logging.info(
                        "Parsed data-crawl-content=text|image|audio from HTML; downloading https assets"
                    )
                    media_downloads = download_crawl_marker_assets(
                        dl,
                        marker,
                        dest,
                        timeout=args.timeout,
                        max_bytes=MAX_MEDIA_DOWNLOAD_BYTES,
                    )
                else:
                    found_urls = collect_media_urls(html, base_for_media)
                    logging.info("Found %d media URL(s) in HTML (legacy scan)", len(found_urls))
                    media_downloads = download_media_items(
                        dl,
                        found_urls,
                        dest,
                        timeout=args.timeout,
                        max_bytes=MAX_MEDIA_DOWNLOAD_BYTES,
                        max_items=MAX_MEDIA_ITEMS_PER_CRAWL,
                    )
        except Exception:
            logging.exception("Media download batch failed")

    if wants_openrouter and media_downloads:
        if not (os.getenv("OPENAI_API_KEY", "").strip() or os.getenv("OPENROUTER_API_KEY", "").strip()):
            logging.warning(
                "Skipping LLM media scoring: set OPENAI_API_KEY or OPENROUTER_API_KEY (see .env.example)"
            )
        else:
            try:
                score_media_items_with_openrouter(media_downloads)
            except Exception:
                logging.exception(
                    "OpenRouter media scoring failed (pip install -r api/requirements.txt?)"
                )

    media_flat = export_keys_from_media_openrouter(media_downloads)
    merged_scores = {**merged_scores, **media_flat}
    if media_flat:
        logging.info("OpenRouter media → top-level score keys: %s", media_flat)
    if not merged_scores:
        logging.warning(
            "No scores in export: no location:text patterns on the page and no OpenRouter media scores "
            "(check downloads + OPENAI_API_KEY or OPENROUTER_API_KEY + api/requirements.txt)."
        )

    out_path = Path(args.output)
    existing = load_json_path(out_path)
    final = merge_output(
        existing,
        merged_scores,
        url=url,
        text_len=len(text),
        replace_all_scores=args.replace,
        raw=raw,
        media_downloads=media_downloads,
        openrouter_media_keys=list(media_flat.keys()),
    )
    atomic_write_json(out_path, final)
    logging.info("Wrote %s", out_path.resolve())
    return merged_scores


def main() -> int:
    p = argparse.ArgumentParser(description="Crawl Media Seed Bot page → JSON scores + log text.")
    p.add_argument("--url", default=None, help=f"Override {ENV_URL} / default {DEFAULT_URL}")
    p.add_argument(
        "--output",
        "-o",
        default=DEFAULT_OUT,
        help=f"JSON output path (default: {DEFAULT_OUT})",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=FETCH_TIMEOUT_SECONDS,
        help=f"HTTP or page load timeout in seconds (default: {FETCH_TIMEOUT_SECONDS})",
    )
    p.add_argument(
        "--browser",
        action="store_true",
        help="Use headless Chromium (Playwright) for rendered text",
    )
    p.add_argument(
        "--replace",
        action="store_true",
        help="Drop previous location keys; only keep keys from this crawl + _meta",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Single crawl then exit (overrides CRAWL_LOOP_FOREVER in this file)",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=CRAWL_INTERVAL_SECONDS,
        metavar="SEC",
        help=f"Seconds between crawls when looping (default: {CRAWL_INTERVAL_SECONDS} from CONFIG)",
    )
    p.add_argument(
        "--loop-seconds",
        type=int,
        default=0,
        metavar="N",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--max-raw-html",
        type=int,
        default=MAX_RAW_HTML_PREVIEW_CHARS,
        metavar="N",
        help=(
            f"Max chars of HTML in _meta.raw.html_preview "
            f"(default: {MAX_RAW_HTML_PREVIEW_CHARS} from CONFIG; 0 = empty preview)"
        ),
    )
    p.add_argument(
        "--no-download-media",
        action="store_true",
        help="Do not download image/audio URLs from the page (overrides DOWNLOAD_PAGE_MEDIA)",
    )
    p.add_argument(
        "--no-openrouter",
        action="store_true",
        help="Do not score downloads with OpenRouter (overrides SCORE_DOWNLOADED_MEDIA_OPENROUTER)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    _load_dotenv()
    setup_logging(args.verbose)

    if args.browser or HARVEST_MEDIA_SEED_UI_PANELS:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            logging.error("Install playwright: pip install playwright && playwright install chromium")
            return 1

    import time

    if args.once:
        sleep_sec = 0
    elif args.loop_seconds > 0:
        sleep_sec = args.loop_seconds
    elif CRAWL_LOOP_FOREVER:
        sleep_sec = max(1, args.interval)
    else:
        sleep_sec = 0

    if sleep_sec > 0:
        logging.info(
            "Repeating every %s s (CONFIG CRAWL_LOOP_FOREVER=%s; override with --once)",
            sleep_sec,
            CRAWL_LOOP_FOREVER,
        )
    else:
        logging.info("Single run then exit (--once or CONFIG CRAWL_LOOP_FOREVER=False)")

    while True:
        try:
            run_once(args)
        except httpx.HTTPError as e:
            logging.exception("HTTP error: %s", e)
        except Exception:
            logging.exception("Crawl failed")
        if sleep_sec <= 0:
            break
        logging.info("Sleeping %s s until next crawl", sleep_sec)
        time.sleep(sleep_sec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
