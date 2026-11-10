#!/usr/bin/env python3
"""
Universal Media Hub - Flask backend.

Stack: Flask + yt-dlp (library) + `snapchat-dl -u` + gallery-dl (Instagram photos/stories/profiles) + ffmpeg.

Run (dev):   python3 app.py
Run (prod):  gunicorn -w 1 --threads 8 -b 0.0.0.0:5000 app:app
             (keep -w 1: job state is held in this process's memory)

Optional lock:  HUB_TOKEN=somesecret HUB_FILES_PIN=yourpassword python3 app.py   (HUB_STRICT=1 refuses to start with weak ones)
                then open  http://server:5000/?token=somesecret  once (a cookie remembers it).

Files live in ~/media-downloader/downloads/<category>/<Creator>/ ; state files
(jobs.json, watches.json, settings.json, archive.txt, cookies.txt) live in ~/media-downloader/.

How a job knows "its" files (important when several downloads run at once):
  * yt-dlp jobs   -> the file paths yt-dlp reports for THAT job (requested_downloads, thumbnails,
                     subtitles) plus side-files that share the same name prefix. Never a folder diff.
  * Snapchat jobs -> only the snapchat/<username>/ folder, one job per username at a time.
  * Instagram     -> the paths gallery-dl prints for THAT job.
  * job["fresh"]  -> files this job really created just now (used for watchlist counting/alerts).
"""

import hashlib
import hmac
import json
import logging
import math
import os
import queue
import re
import socket
import html
import secrets
import shutil
import signal
import subprocess
import sys
import struct
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from contextlib import contextmanager, nullcontext
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from flask import Flask, jsonify, redirect, render_template, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadCancelled

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE_DIR = Path(os.path.expanduser("~/media-downloader/downloads")).resolve()
DATA_DIR = BASE_DIR.parent                       # jobs.json, watches.json, settings.json, archive.txt, cookies.txt
CATEGORIES = ("youtube", "tiktok", "snapchat", "instagram", "general", "folders")   # folders = your own (e.g. "Good")
for _c in CATEGORIES:
    (BASE_DIR / _c).mkdir(parents=True, exist_ok=True)

MAX_WORKERS = 3             # downloads running at once; extras wait in the queue
PREVIEW_TIMEOUT = 25        # seconds before a preview lookup is abandoned
YTDLP_TIMEOUT = 60 * 45     # hard cap for one yt-dlp job
SNAP_HARD_TIMEOUT = 60 * 60 # hard cap for one snapchat-dl job (big accounts take a while)
SNAP_IDLE_TIMEOUT = 30      # quiet this long (no output, no new data) => finished
SNAP_START_TIMEOUT = 45     # before the first output/file (slow Snapchat page load)
SNAP_FULL_QUIET = 5         # all counted stories saved: finish this many seconds after the LAST new file
SNAP_STALL_LIMIT = 35       # fewer saved than counted: give up this many seconds after the last new file
SNAP_AFTER_DONE_IDLE = 8    # quiet this long AFTER it printed 'stories downloaded' => finished
IG_HARD_TIMEOUT = 60 * 40   # hard cap for one gallery-dl (Instagram) job
IG_IDLE_TIMEOUT = 300       # gallery-dl silent this long => stuck (it pauses 6-12 s between requests on purpose)
IG_DEFAULT_CAP = 30         # items taken from a whole Instagram profile unless you set a range
JOB_TTL = 60 * 60 * 24      # keep finished jobs 24h so you can come back to the result
PARTIAL_EXT = (".part", ".ytdl", ".temp")
COOKIE_MAX_BYTES = 1024 * 1024

NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SNAP_DONE_RE = re.compile(r"stories? downloaded|download(ed)? complete", re.I)
SNAP_ERR_RE = re.compile(r"error|not found|private|does not exist|no stor|invalid", re.I)
SNAP_USER_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")

VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov", ".m4v")
AUDIO_EXT = (".mp3", ".m4a", ".opus", ".flac", ".wav", ".ogg")
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")
MEDIA_EXT = (".mp4", ".mov", ".jpg", ".jpeg", ".png", ".webp", ".gif")
SIDE_EXT = (".srt", ".vtt", ".ass", ".lrc", ".jpg", ".jpeg", ".png", ".webp", ".description", ".json")

app = Flask(__name__, template_folder='views', static_folder='assets', static_url_path='/assets')
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024        # requests are tiny; cookie uploads are < 1 MB
pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="dl")
preview_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pv")
jobs, cancels = {}, {}
lock = threading.Lock()
JOBS_FILE = DATA_DIR / "jobs.json"


def save_jobs():
    """Write job state to disk so results survive a server restart."""
    try:
        with lock:
            data = json.dumps(list(jobs.values()))
        tmp = JOBS_FILE.with_suffix(f".{threading.get_ident()}.tmp")      # unique per thread
        tmp.write_text(data)
        tmp.replace(JOBS_FILE)
    except OSError:
        app.logger.warning("Could not write %s", JOBS_FILE)


def load_jobs():
    try:
        items = json.loads(JOBS_FILE.read_text())
    except (OSError, ValueError):
        return
    for j in items if isinstance(items, list) else []:
        if not isinstance(j, dict) or "id" not in j:
            continue
        if j.get("status") in ("queued", "running", "paused"):
            j.update(status="error", message="Server restarted before this finished.",
                     finished=time.time())
        j.setdefault("fresh", [])
        jobs[j["id"]] = j
        cancels[j["id"]] = threading.Event()


load_jobs()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def host_is(host: str, *domains) -> bool:
    """True for 'example.com' and 'www.example.com' but NOT 'notexample.com'."""
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def detect_category(url: str) -> str:
    """Pick the subfolder from the URL's host."""
    host = urlparse(url).hostname or ""
    if host_is(host, "youtube.com", "youtu.be", "youtube-nocookie.com"):
        return "youtube"
    if host_is(host, "tiktok.com"):
        return "tiktok"
    if host_is(host, "instagram.com"):
        return "instagram"
    return "general"


def is_instagram(url: str) -> bool:
    return host_is(urlparse(url).hostname or "", "instagram.com")


def valid_http_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.hostname)


def json_body() -> dict:
    """Request JSON as a dict - anything else (list, string, garbage) becomes {}."""
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def is_partial(name: str) -> bool:
    return name.endswith(PARTIAL_EXT) or ".fxtmp." in name


def last_line(exc, default="Download failed") -> str:
    """Last non-empty line of an exception, without yt-dlp's 'ERROR: ' prefix."""
    lines = [l.strip() for l in str(exc).replace("ERROR: ", "").splitlines() if l.strip()]
    return (lines[-1] if lines else default)[:500]


def safe_path(category: str, name: str):
    """Resolve a saved file; return None if it would escape its folder."""
    if category not in CATEGORIES:
        return None
    try:
        root = (BASE_DIR / category).resolve()
        target = (root / name).resolve()
        return target if root in target.parents and target.is_file() else None
    except (ValueError, OSError):                    # e.g. a NUL byte in the name
        return None


def mtime_of(folder: Path, rel: str) -> float:
    try:
        return (folder / rel).stat().st_mtime
    except OSError:
        return 0.0


def update(job, **kw):
    with lock:
        job.update(kw)


def job_cancel(job):
    return cancels.get(job["id"])


def prune_jobs():
    cutoff = time.time() - JOB_TTL
    with lock:
        for jid in [k for k, j in jobs.items() if j["finished"] and j["finished"] < cutoff]:
            jobs.pop(jid, None)
            cancels.pop(jid, None)


def move_to_folder(job, files):
    """Move a finished job's files into folders/<name>/ (the "Save to" choice). Returns {old rel: new rel}."""
    name = clean_folder(job.get("folder"))
    if not name or job.get("category") not in CATEGORIES:
        return {}
    src_root, dest = BASE_DIR / job["category"], BASE_DIR / "folders" / name
    dest.mkdir(parents=True, exist_ok=True)
    moved, emptied = {}, set()
    for rel in files:
        p = src_root / rel
        if not p.is_file():
            continue
        if p.parent.resolve() == dest.resolve():             # already there (downloaded into the same folder again)
            moved[rel] = f"{name}/{p.name}"
            continue
        target, k = dest / p.name, 2
        while target.exists():
            target, k = dest / f"{p.stem} ({k}){p.suffix}", k + 1
        try:
            shutil.move(str(p), str(target))
        except OSError:
            continue
        _retag(fs_rel(p), fs_rel(target))
        moved[rel] = f"{name}/{target.name}"
        emptied.add(p.parent)
    for d in emptied:                                          # tidy up folders the move left empty
        while d != src_root and src_root in d.parents:
            try:
                if any(d.iterdir()):
                    break
                d.rmdir()
            except OSError:
                break
            d = d.parent
    fs_dirty()
    return moved


def finish_ok(job, files, message=None, fresh=None):
    """fresh = the files this job really created now (defaults to all of them)."""
    if job.get("folder") and files:                           # "Save to" a folder of your own
        moved = move_to_folder(job, files)
        if moved:
            files = [moved.get(f, f) for f in files if f in moved]
            fresh = [moved[f] for f in (files if fresh is None else fresh) if f in moved] if fresh is not None else None
            update(job, category="folders")
            message = (message or f"Saved {len(files)} file(s)") + f" · 📂 in {clean_folder(job['folder'])}"
    update(job, status="done", progress=100, files=list(files), speed="", eta="",
           fresh=list(files if fresh is None else fresh),
           message=message or f"Saved {len(files)} file(s)", finished=time.time())
    save_jobs()
    _after_finish(job, True)


def finish_err(job, msg):
    if job.get("pausing"):                               # stopped by "Pause", not by a failure: keep it for Resume
        update(job, status="paused", pausing=False, message="Paused - press Resume to continue", speed="", eta="")
        save_jobs()
        return
    update(job, status="error", message=str(msg)[:500], finished=time.time())
    save_jobs()
    _after_finish(job, False)


@contextmanager
def named_lock(key, cancel, job=None):
    """
    One holder per key. Used so two jobs for the SAME link / Snapchat user / Instagram URL
    never fight over the same files: the second waits, then sees the files already exist.
    """
    with _named_guard:
        ent = _named.setdefault(key, [threading.Lock(), 0])
        ent[1] += 1
    got = False
    try:
        if not ent[0].acquire(blocking=False):
            if job is not None:
                update(job, message="Waiting for the same item to finish in another job…")
            while not ent[0].acquire(timeout=1):
                if cancel.is_set():
                    raise DownloadCancelled("Cancelled by user")
        got = True
        yield
    finally:
        if got:
            ent[0].release()
        with _named_guard:
            ent[1] -= 1
            if ent[1] <= 0 and _named.get(key) is ent:
                del _named[key]


_named, _named_guard = {}, threading.Lock()


# --------------------------------------------------------------------------- #
# Instagram login (cookies.txt) - validated, private (chmod 600), per-job copies
# --------------------------------------------------------------------------- #
COOKIES_FILE = DATA_DIR / "cookies.txt"
_COOKIE_DOMAIN_RE = re.compile(r"^\.?[A-Za-z0-9.\-]+$")
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-%]+$")


def parse_cookie_text(text: str, site: str = "instagram.com", label: str = "Instagram"):
    """
    Check a pasted/uploaded cookies.txt (Netscape format) and keep ONLY that site's cookies (instagram.com by default)
    (a browser export can hold cookies of every site - those must never be stored here).
    Returns (cleaned file text, kept_count, ignored_count) or raises ValueError(simple message).
    """
    if len(text.encode("utf-8", "replace")) > COOKIE_MAX_BYTES:
        raise ValueError("That file is too big to be a cookies.txt (limit 1 MB).")
    if "\x00" in text:
        raise ValueError("That does not look like a text file.")
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if text.lstrip()[:1] in ("[", "{"):
        raise ValueError("This looks like a JSON export. In the extension choose the "
                         "“Netscape” / “cookies.txt” format instead.")
    kept, ignored = [], 0
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("#HttpOnly_"):                 # curl-style: a real cookie line
            line = line[len("#HttpOnly_"):]
        elif line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 6:                               # tabs got turned into spaces while pasting
            fields = line.split(None, 6)
        if len(fields) == 6:
            fields.append("")                             # empty cookie value
        if len(fields) != 7:
            continue
        domain, flag, path, secure, expiry, name, value = [f.strip() for f in fields]
        if not (_COOKIE_DOMAIN_RE.match(domain) and flag.upper() in ("TRUE", "FALSE") and path.startswith("/")
                and secure.upper() in ("TRUE", "FALSE") and _COOKIE_NAME_RE.match(name)
                and re.fullmatch(r"-?\d+(\.\d+)?", expiry)):
            continue
        if not host_is(domain.lstrip("."), site):
            ignored += 1
            continue
        exp = max(0, int(float(expiry)))
        kept.append("\t".join([domain, flag.upper(), path, secure.upper(), str(exp), name, value]))
    if not kept:
        raise ValueError(f"No {label} cookies found. Open {site} while logged in, export again "
                         f"(Netscape / cookies.txt format) and make sure the file is the {label} one.")
    return "# Netscape HTTP Cookie File\n" + "\n".join(kept) + "\n", len(kept), ignored


def write_private(path: Path, data: str):
    """Write a file that only this Linux user can read (chmod 600), atomically."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def cookie_status() -> dict:
    """What the Settings card shows. Never returns cookie values."""
    st = {"saved": False, "count": 0, "has_session": False, "session_expired": False,
          "expires": None, "saved_at": None}
    try:
        text = COOKIES_FILE.read_text()
        st["saved_at"] = COOKIES_FILE.stat().st_mtime
    except OSError:
        return st
    now = time.time()
    for line in text.splitlines():
        f = line.split("\t")
        if line.startswith("#") or len(f) != 7:
            continue
        st["count"] += 1
        if f[5] == "sessionid" and f[6]:
            st["has_session"] = True
            exp = int(f[4]) if f[4].isdigit() else 0
            if exp and exp < now:
                st["session_expired"] = True
            elif exp:
                st["expires"] = exp
    st["saved"] = st["count"] > 0
    return st


@contextmanager
def cookie_copy():
    """
    A private temporary copy of cookies.txt for ONE job/preview, deleted afterwards.
    (yt-dlp rewrites its cookie file on exit, so jobs must never share the real file.)
    Yields the path, or None when no login is saved.
    """
    if not COOKIES_FILE.is_file():
        yield None
        return
    d = tempfile.mkdtemp(prefix="hub-ck-")               # mode 0700
    try:
        dst = os.path.join(d, "cookies.txt")
        try:
            shutil.copyfile(COOKIES_FILE, dst)
            os.chmod(dst, 0o600)
        except OSError:
            dst = None
        yield dst
    finally:
        shutil.rmtree(d, ignore_errors=True)


def cookie_ctx(use: bool):
    return cookie_copy() if use else nullcontext(None)


# ---- Instagram link types ----------------------------------------------------
_IG_RESERVED = {"explore", "accounts", "direct", "about", "legal", "developer", "web", "challenge", "emails",
                "privacy", "api", "oauth", "static", "nametag", "session", "reel", "reels", "p", "tv",
                "stories", "share", "directory", "lite"}


def instagram_info(url: str) -> dict:
    """
    kind: reel (yt-dlp) | post | stories | highlight | profile (gallery-dl) | unknown
    """
    parts = [x for x in urlparse(url).path.split("/") if x]
    out = {"kind": "unknown", "user": "", "code": ""}
    if not parts:
        return out
    a = parts[0]
    if a == "share" and len(parts) >= 2:
        out.update(kind="reel", code=parts[-1])             # share links: yt-dlp follows the redirect
    elif a in ("reel", "reels", "tv") and len(parts) > 1:
        out.update(kind="reel", code=parts[1])
    elif a == "p" and len(parts) > 1:
        out.update(kind="post", code=parts[1])
    elif len(parts) >= 3 and parts[1] in ("reel", "reels", "tv"):
        out.update(kind="reel", user=a, code=parts[2])
    elif len(parts) >= 3 and parts[1] == "p":
        out.update(kind="post", user=a, code=parts[2])
    elif a == "stories" and len(parts) >= 3 and parts[1] == "highlights":
        out.update(kind="highlight", code=parts[2])
    elif a == "stories" and len(parts) >= 2:
        out.update(kind="stories", user=parts[1], code=parts[2] if len(parts) > 2 else "")
    elif a.lower() not in _IG_RESERVED and re.fullmatch(r"[A-Za-z0-9._]{1,30}", a):
        out.update(kind="profile", user=a)
    return out


# gallery-dl folder layout: instagram/<username>/<post date>/<type>/file  (date = when it was POSTED, so re-runs find the same path)
_IG_DATE = "{date:%Y-%m-%d}"
IG_LAYOUT = {"extractor": {"instagram": {"directory": {
    "subcategory in ('post', 'posts')": ["instagram", "{username}", _IG_DATE, "posts"],
    "subcategory in ('reel', 'reels')": ["instagram", "{username}", _IG_DATE, "reels"],
    "subcategory == 'stories'": ["instagram", "{username}", _IG_DATE, "stories"],
    "subcategory == 'highlights'": ["instagram", "{username}", _IG_DATE, "highlights"],
    "subcategory == 'tagged'": ["instagram", "{username}", _IG_DATE, "tagged"],
    "subcategory == 'avatar'": ["instagram", "{username}", "profile-picture"],
    "": ["instagram", "{username}", _IG_DATE, "{subcategory}"]}}}}

GALLERY_MISSING = ("gallery-dl is not installed on the server, and Instagram photos, carousels, stories, "
                   "highlights and profiles need it. Install it with:  pip install -U gallery-dl "
                   "--break-system-packages   (or: pipx install gallery-dl), then press Restart in Settings.")
IG_NEEDS_LOGIN = ("Instagram needs a login for this. Open Settings → “Instagram login”, follow the steps "
                  "and save your cookies.txt first.")
IG_LOGIN_BAD = ("Your saved Instagram login is not working (expired, logged out, or Instagram blocked it). "
                "Export a fresh cookies.txt from the browser and upload it again in Settings → Instagram login. "
                "If you just downloaded a lot, also wait 20-30 minutes.")


def friendly_ig_error(msg: str, gallery_missing=False) -> str:
    """Turn yt-dlp / gallery-dl error text into something a non-programmer can act on."""
    m = (msg or "").lower()
    if re.search(r"no video in this post|no video formats|there is no video", m):
        return ("This post is photos only. Photos and carousels need gallery-dl, which is not installed "
                "on the server (see Settings → Instagram login)." if gallery_missing
                else "This link has no video in it. If it is a photo post, paste its normal post link "
                     "(instagram.com/p/…) so gallery-dl can save the photos.")
    if re.search(r"login|log in|logged in|cookies|authenticat|checkpoint|challenge|unauthori|forbidden|"
                 r"\b40[13]\b|rate-limit|rate limit|empty media response|not granting access", m):
        return IG_LOGIN_BAD
    if re.search(r"\b429\b|too many requests", m):
        return "Instagram says “too many requests”. Wait 20-30 minutes, then try again."
    if re.search(r"\b404\b|not\s*found|could not be found|unavailable|does not exist|private", m):
        return "Instagram could not find that. It may be deleted, or the account is private and you do not follow it."
    return msg or "Instagram download failed."


# --------------------------------------------------------------------------- #
# Download options (everything the UI can ask for)
# --------------------------------------------------------------------------- #
DEFAULT_OPTS = {
    "kind": "video",            # video | audio | image (cover/thumbnail as JPG) | gif | sticker
    "quality": "best", "container": "mp4", "compat": False,     # compat = H.264, plays everywhere
    "audio_fmt": "mp3", "abr": "192",
    "subs": False, "autosubs": False, "sub_langs": "en", "sub_fmt": "srt", "embed_subs": False,
    "thumb": False, "embed_thumb": False, "meta": False, "sponsor": False, "desc": False,
    "playlist": "single", "items": "",                          # single | all, "1-5"
    "clip_start": "", "clip_end": "",                           # "1:30" or seconds
    "date_after": "", "date_before": "",                        # YYYY-MM-DD
    "min_dur": "", "max_dur": "", "min_views": "", "max_mb": "",
    "title_has": "", "title_not": "",
    "name": "",                                                 # file name typed on the page (empty = automatic)
    "folder": "",                                               # "" = the normal site/creator folder, "Good" = folders/Good/
    "ig_what": "posts",                                         # profile link: posts | stories | highlights | all
    "force": False,                                             # re-download even if the file exists
    # effects (ffmpeg, applied after the download - like the Telegram bots)
    "fx_look": "none", "fx_blur": "none", "fx_sharpen": False, "fx_mirror": "none",
    "fx_rotate": "0", "fx_speed": "1", "fx_crop": "none", "fx_scale": "none",
    "fx_mute": False, "fx_keep": True,                           # fx_keep = keep the original too
}
_ENUMS = {"kind": {"video", "audio", "image", "gif", "sticker", "subs"},
          "quality": {"best", "2160", "1440", "1080", "720", "480", "360"},
          "container": {"mp4", "mkv", "webm"},
          "audio_fmt": {"mp3", "m4a", "opus", "flac", "wav"}, "sub_fmt": {"srt", "vtt", "ass"},
          "abr": {"96", "128", "192", "256", "320"}, "playlist": {"single", "all"},
          "ig_what": {"posts", "stories", "highlights", "all"},
          "fx_look": {"none", "bw", "sepia", "negative", "vivid", "warm", "cool", "vintage", "cinematic"},
          "fx_blur": {"none", "light", "strong"}, "fx_mirror": {"none", "h", "v"},
          "fx_rotate": {"0", "90", "180", "270"}, "fx_speed": {"1", "0.5", "1.5", "2"},
          "fx_crop": {"none", "square", "vertical"}, "fx_scale": {"none", "1080", "720", "480"}}
_WORDS_RE = re.compile(r"[^\w\s.\-]", re.UNICODE)


def _num(v, lo=0, hi=10**9):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n)) if math.isfinite(n) else None


def _secs(v):
    """'90' | '1:30' | '1:02:03' -> seconds (or None). Rejects negatives, NaN, junk."""
    v = str(v or "").strip()
    if not v:
        return None
    try:
        parts = [float(x) for x in v.split(":")]
    except ValueError:
        return None
    if len(parts) > 3 or any(not math.isfinite(p) or p < 0 for p in parts):
        return None
    t = 0.0
    for p in parts:
        t = t * 60 + p
    return t


def clean_folder(v):
    """A folder name typed by the user ("Good") -> one safe folder level (no slashes, max 40 chars)."""
    s = re.sub(r"\s+", " ", re.sub(r'[\x00-\x1f<>:"/\\|?*]', "", str(v or ""))).strip().strip(".")[:40].strip()
    return "" if s in ("", ".", "..") else s


def clean_filename(v):
    """A file name typed by the user -> safe for every system (no slashes, no control characters, max 100 chars)."""
    s = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "", str(v or ""))
    return re.sub(r"\s+", " ", s).strip().strip(".")[:100]


_name_lock = threading.Lock()


def _taken(d, base):
    """Is this name already used in folder d (base.mp4, base.en.srt, base [bw].mp4 ...)?"""
    try:
        return any(f.name == base or f.name.startswith(base + ".") or f.name.startswith(base + " [") for f in d.iterdir())
    except OSError:
        return False


def rename_files(folder, files, o, auto=False):
    """
    Give THIS job's files the name the user typed - or, with auto=True and nothing typed, today's date + a number.
    Several files get _001, _002 ...; a name that is already used gets the next free number, so nothing is overwritten.
    Covers/subtitles/effect copies keep matching their video. Returns {old relative path: new relative path}.
    """
    name = o.get("name") or ""
    if not files or not (name or auto):
        return {}
    today, mapping, by_dir = time.strftime("%Y-%m-%d"), {}, {}
    for rel in files:
        pr = Path(rel)
        by_dir.setdefault(pr.parent.as_posix(), []).append(pr.name)
    with _name_lock:
        for parent, names in by_dir.items():
            d = folder if parent == "." else folder / parent
            names = sorted(names)
            order, bases, group = {n: i for i, n in enumerate(names)}, [], {}
            for n in sorted(names, key=lambda x: (len(x), x)):
                base = next((b for b in bases if n.startswith(b + ".") or n.startswith(b + " [")), None)
                if base is None:
                    base = n.rsplit(".", 1)[0] if "." in n else n
                    bases.append(base)
                group.setdefault(base, []).append(n)
            bases.sort(key=lambda b: min(order[n] for n in group[b]))
            counter = 0
            for b in bases:
                if name and len(bases) == 1:
                    cand, k = name, 1
                    while _taken(d, cand):
                        k += 1
                        cand = f"{name}_{k:03d}"
                else:
                    stem = name or today
                    while True:
                        counter += 1
                        cand = f"{stem}_{counter:03d}"
                        if not _taken(d, cand):
                            break
                for n in group[b]:
                    new_n = cand + n[len(b):]
                    try:
                        os.rename(d / n, d / new_n)
                    except OSError:
                        continue
                    mapping[n if parent == "." else f"{parent}/{n}"] = new_n if parent == "." else f"{parent}/{new_n}"
    return mapping


def sanitize_options(raw):
    """Never trust the browser: only known keys, validated values."""
    raw = raw if isinstance(raw, dict) else {}
    o = dict(DEFAULT_OPTS)
    for k, default in DEFAULT_OPTS.items():
        v = raw.get(k, default)
        if k in _ENUMS:
            o[k] = str(v) if str(v) in _ENUMS[k] else default
        elif isinstance(default, bool):
            o[k] = bool(v)
        elif k == "sub_langs":
            o[k] = re.sub(r"[^A-Za-z0-9,\-_.*]", "", str(v))[:40] or "en"
        elif k == "items":
            o[k] = str(v) if re.fullmatch(r"[0-9,\-: ]{0,30}", str(v)) else ""
        elif k == "name":
            o[k] = clean_filename(v)
        elif k == "folder":
            o[k] = clean_folder(v)
        elif k in ("title_has", "title_not"):
            o[k] = _WORDS_RE.sub("", str(v)).strip()[:60]
        elif k in ("date_after", "date_before"):
            o[k] = str(v) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v)) else ""
        else:                                          # clip times + numeric filters
            o[k] = str(v).strip()[:12]
    return o


def has_filters(o):
    return any(o.get(k) for k in ("date_after", "date_before", "min_dur", "max_dur",
                                  "min_views", "max_mb", "title_has", "title_not"))


# ---- Arroxy-style controls: speed limit, parallel parts, request pacing, retries, filename template ----
_RATE = {"500K": 500 * 1024, "1M": 1024 ** 2, "2M": 2 * 1024 ** 2, "5M": 5 * 1024 ** 2, "10M": 10 * 1024 ** 2}
_TPL = {"title": "%(title).120B", "uploader": "%(uploader,channel,creator|Unknown).60B", "id": "%(id)s",
        "date": "%(upload_date>%Y-%m-%d|unknown-date)s", "resolution": "%(height&{}p|)s", "playlist_index": "%(playlist_index|)s"}
_TPL_RE = re.compile(r"\{(" + "|".join(_TPL) + r")\}")


def clean_template(v):
    """What the filename box accepts: the known {placeholders}, plain text, '/' for sub-folders - nothing that can leave the folder."""
    s = str(v or "").strip()[:150]
    parts = _TPL_RE.split(s)
    s = "".join("{" + p + "}" if i % 2 else re.sub(r'[\x00-\x1f<>:"\\|?*{}%]', "", p) for i, p in enumerate(parts))
    segs = [x.strip() for x in s.split("/")]
    if s.startswith("/") or any(x in ("..", ".") for x in segs):
        return ""
    return "/".join(x for x in segs if x)


def _to_outtmpl(v):
    s = clean_template(v)
    if not s:
        return None
    return "".join(_TPL[p] if i % 2 else p for i, p in enumerate(_TPL_RE.split(s))) + ".%(ext)s"


def _apply_speed_settings(opts):
    rate = _RATE.get(str(settings.get("speed_limit", "0")))
    if rate:
        opts["ratelimit"] = rate
    parts = settings.get("parallel_parts", 1)
    if isinstance(parts, int) and 2 <= parts <= 8:
        opts["concurrent_fragment_downloads"] = parts
    pace = settings.get("pace", "off")
    if pace == "balanced":
        opts.update(sleep_interval_requests=1, sleep_interval=1, max_sleep_interval=3)
    elif pace == "careful":
        opts.update(sleep_interval_requests=2, sleep_interval=3, max_sleep_interval=8)
    rc = settings.get("retry_count", 3)
    if isinstance(rc, int) and 0 <= rc <= 10:
        opts["retries"] = opts["fragment_retries"] = rc
        if rc:                                              # wait longer before each new attempt (2 s, 4 s, 8 s ... max 30 s)
            opts["retry_sleep_functions"] = {k: (lambda n: min(2 ** n, 30)) for k in ("http", "fragment", "file_access", "extractor")}


_TRACK_PARAM = re.compile(r"^(utm_[a-z_]+|fbclid|gclid|dclid|msclkid|igshid|igsh|si|pp|feature|mc_cid|mc_eid|ref_src|"
                          r"is_from_webapp|sender_device|sender_web_id|share_app_id|share_item_id|share_link_id|tt_from|u_code)$", re.I)


def clean_url(url, _depth=0):
    """Strip tracking junk (si, pp, utm_*, fbclid, gclid ...) and unwrap youtube.com/redirect links. Anything else is left alone."""
    try:
        if not settings.get("clean_urls", True) or not isinstance(url, str):
            return url
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.netloc:
            return url
        if _depth < 3 and (u.hostname or "").lower() in ("youtube.com", "www.youtube.com", "m.youtube.com") and u.path == "/redirect":
            q = parse_qs(u.query).get("q", [""])[0]
            if valid_http_url(q):
                return clean_url(q, _depth + 1)
        if not u.query:
            return url
        pieces = u.query.split("&")
        kept = [p for p in pieces if not _TRACK_PARAM.match(p.split("=", 1)[0])]
        if len(kept) == len(pieces):
            return url
        return u._replace(query="&".join(kept)).geturl()
    except ValueError:
        return url


def build_ydl_opts(options, folder, hook, tmpl=None, cookiefile=None, ig=False, ig_sub="posts"):
    """Translate UI options into a yt-dlp options dict."""
    from yt_dlp.utils import DateRange, download_range_func, match_filter_func
    o = sanitize_options(options)
    if o["kind"] in ("gif", "sticker"):               # these only need the picture, no subs/tags/etc.
        for k in ("subs", "autosubs", "embed_subs", "thumb", "embed_thumb", "meta", "sponsor", "desc"):
            o[k] = False
    if o["kind"] == "subs":                           # subtitles only: no video is fetched, so nothing to embed / tag / cut
        for k in ("embed_subs", "thumb", "embed_thumb", "meta", "sponsor"):
            o[k] = False
    if tmpl is None:
        who = "%(channel,uploader_id,uploader|Unknown).60B/" if ig else "%(uploader,channel,creator|Unknown).60B/"
        creator = who if settings.get("creator_folders", True) else ""
        nt = None if ig else _to_outtmpl(settings.get("name_template", ""))      # your own file-name template (Settings -> Downloads)
        tmpl = creator + (nt or "%(title).120B [%(id)s].%(ext)s")
        if ig and settings.get("ig_organize", True):          # instagram/<user>/<post date>/<type>/title [id].ext
            tmpl = who + f"%(upload_date>%Y-%m-%d|unknown-date)s/{ig_sub}/" + "%(title).120B [%(id)s].%(ext)s"
        if o["playlist"] == "all":
            tmpl = "%(playlist_title|).80B/" + (nt or "%(playlist_index|)s %(title).100B [%(id)s].%(ext)s")
    opts = {"outtmpl": str(folder / tmpl), "quiet": True, "no_warnings": True, "noprogress": True,
            "socket_timeout": 20, "retries": 3, "fragment_retries": 3, "progress_hooks": [hook],
            "noplaylist": o["playlist"] != "all"}
    if cookiefile:
        opts["cookiefile"] = cookiefile
    _apply_speed_settings(opts)
    pps = []

    # --- what to get ---------------------------------------------------------
    if o["kind"] == "audio":
        opts["format"] = "bestaudio/best"
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": o["audio_fmt"]}
        if o["audio_fmt"] in ("mp3", "m4a", "opus"):
            pp["preferredquality"] = o["abr"]
        pps.append(pp)
    elif o["kind"] == "subs":
        opts["skip_download"] = True
    elif o["kind"] in ("gif", "sticker"):
        opts["format"] = "bv*[height<=720]/b[height<=720]/bv*/b"      # video only, small is fine
        opts["merge_output_format"] = "mp4"
    elif o["kind"] == "image":
        opts.update(skip_download=True, writethumbnail=True)
        pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
    else:
        q = o["quality"]
        # "res" = the SHORTER side, so a vertical 720x1280 TikTok counts as 720p (a height cap missed it and fell back to 1080p)
        res = "res" if q == "best" else f"res:{q}"
        opts["format"] = "bv*+ba/b"
        opts["merge_output_format"] = o["container"]
        if q != "best":
            opts["format_sort"] = [res]
        if o["compat"] or settings.get("prefer_h264", True):
            # iPhone / Photos play H.264 and HEVC, not VP9/AV1. Only those two codecs, sharpest first:
            # TikTok's 1080p is HEVC while its H.264 copy is 720p, so "H.264 first" gave blurry TikToks.
            ok = "[vcodec~='^(avc|h264|hevc|h265|hvc1)']"
            opts["format"] = f"bv*{ok}+ba/b{ok}/bv*+ba/b"
            opts["format_sort"] = [res, "fps", "vcodec:h264", "acodec:m4a", "tbr"]     # m4a: Opus in MP4 does not play on iPhone

    # --- extras ----------------------------------------------------------------
    if o["sponsor"]:
        cats = ["sponsor", "selfpromo", "interaction"]
        pps += [{"key": "SponsorBlock", "categories": cats, "when": "after_filter"},
                {"key": "ModifyChapters", "remove_sponsor_segments": cats, "remove_chapters_patterns": [],
                 "remove_ranges": [], "sponsorblock_chapter_title": "[SponsorBlock]: %(category_names)l",
                 "force_keyframes": False}]
    if o["subs"] or o["autosubs"] or o["kind"] == "subs":
        embed = o["embed_subs"] and o["kind"] == "video"
        fmt = "srt" if embed else o["sub_fmt"]                      # embedding needs srt; otherwise your chosen format
        opts.update(writesubtitles=o["subs"] or o["kind"] == "subs", writeautomaticsub=o["autosubs"],
                    subtitleslangs=[x for x in o["sub_langs"].split(",") if x],
                    subtitlesformat=f"{fmt}/best")
        pps.append({"key": "FFmpegSubtitlesConvertor", "format": fmt})
        if embed:
            pps.append({"key": "FFmpegEmbedSubtitle"})
    if o["meta"]:
        pps.append({"key": "FFmpegMetadata", "add_chapters": True, "add_metadata": True})
    if (o["thumb"] or o["embed_thumb"]) and o["kind"] not in ("image", "subs"):
        opts["writethumbnail"] = True
        pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
        if o["embed_thumb"]:
            pps.append({"key": "EmbedThumbnail"})
    if o["desc"]:
        opts["writedescription"] = True
    if o["force"]:
        opts["overwrites"] = True
    opts["postprocessors"] = pps

    # --- playlist + clip + filters ------------------------------------------
    if o["playlist"] == "all" and o["items"]:
        opts["playlist_items"] = o["items"]
    start, end = _secs(o["clip_start"]), _secs(o["clip_end"])
    if (start is not None or end is not None) and o["kind"] != "image":
        start = start or 0.0
        if end is None or end > start:
            opts["download_ranges"] = download_range_func(None, [(start, end if end is not None else float("inf"))])
            opts["force_keyframes_at_cuts"] = True
    if o["date_after"] or o["date_before"]:
        opts["daterange"] = DateRange(o["date_after"].replace("-", "") or None,
                                      o["date_before"].replace("-", "") or None)
    f = []
    # ">=?" / "<=?" = "pass if the site did not report this field" (instead of rejecting every video)
    for key, field, mult in (("min_dur", "duration >=?", 60), ("max_dur", "duration <=?", 60),
                             ("min_views", "view_count >=?", 1)):
        n = _num(o[key])
        if n:
            f.append(f"{field} {int(n * mult)}")
    if o["title_has"]:
        f.append(f"title ~= (?i){re.escape(o['title_has'])}")
    if o["title_not"]:
        f.append(f"title !~= (?i){re.escape(o['title_not'])}")
    if f:
        opts["match_filter"] = match_filter_func(" & ".join(f))
    mb = _num(o["max_mb"])
    if mb:
        opts["max_filesize"] = int(mb * 1024 * 1024)
    return opts


# --------------------------------------------------------------------------- #
# Effects (ffmpeg)
# --------------------------------------------------------------------------- #
_LOOKS = {
    "bw": "hue=s=0",
    "sepia": "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131",
    "negative": "negate",
    "vivid": "eq=saturation=1.6:contrast=1.08",
    "warm": "colorbalance=rs=.12:gs=.02:bs=-.12:rm=.1:bm=-.1",
    "cool": "colorbalance=rs=-.1:bs=.15:rm=-.08:bm=.12",
    "vintage": "curves=preset=vintage",
    "cinematic": "eq=contrast=1.12:saturation=1.1,vignette=PI/5",
}


def has_fx(o):
    return (o["fx_look"] != "none" or o["fx_blur"] != "none" or o["fx_sharpen"] or o["fx_mirror"] != "none"
            or o["fx_rotate"] != "0" or o["fx_speed"] != "1" or o["fx_crop"] != "none"
            or o["fx_scale"] != "none" or o["fx_mute"])


def fx_plan(o):
    """-> (video filters, audio filter, file-name tag). Only whitelisted values get in."""
    vf, tag = [], []
    if o["fx_crop"] == "square":
        vf.append("crop='min(iw,ih)':'min(iw,ih)'"); tag.append("square")
    elif o["fx_crop"] == "vertical":
        vf.append("crop='min(iw,ih*9/16)':'min(ih,iw*16/9)'"); tag.append("9x16")
    if o["fx_scale"] != "none":
        vf.append(f"scale=-2:{o['fx_scale']}"); tag.append(o["fx_scale"] + "p")
    rot = {"90": ["transpose=1"], "180": ["hflip", "vflip"], "270": ["transpose=2"]}.get(o["fx_rotate"])
    if rot:
        vf += rot; tag.append("r" + o["fx_rotate"])
    if o["fx_mirror"] != "none":
        vf.append("hflip" if o["fx_mirror"] == "h" else "vflip"); tag.append("mirror" + o["fx_mirror"])
    if o["fx_look"] != "none":
        vf.append(_LOOKS[o["fx_look"]]); tag.append(o["fx_look"])
    if o["fx_blur"] != "none":
        vf.append("gblur=sigma=2" if o["fx_blur"] == "light" else "gblur=sigma=8"); tag.append("blur")
    if o["fx_sharpen"]:
        vf.append("unsharp=5:5:1.0:5:5:0.0"); tag.append("sharp")
    af = None
    if o["fx_speed"] != "1":
        af = f"atempo={o['fx_speed']}"; tag.append("x" + o["fx_speed"])
    if o["fx_mute"]:
        tag.append("mute")
    return vf, af, "-".join(tag) or "fx"


def _stop_process(proc, grace=4):
    """Stop a child process AND anything it started (whole process group)."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.wait(4)
        except subprocess.TimeoutExpired:
            pass


def _ffmpeg(cmd, timeout=1800, cancel=None):
    """Run ffmpeg; stops quickly when the job is cancelled."""
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            errors="replace", start_new_session=True)
    started = time.time()
    while True:
        try:
            _, err = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                _stop_process(proc, grace=1)
                raise DownloadCancelled("Cancelled by user")
            if time.time() - started > timeout:
                _stop_process(proc, grace=1)
                raise subprocess.TimeoutExpired(cmd, timeout)
    if proc.returncode != 0:
        raise RuntimeError(((err or "").strip().splitlines() or ["ffmpeg failed"])[-1][:200])


def _need_ffmpeg():
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg is not installed on the server (sudo apt install ffmpeg).")
    return ff


def apply_effects(folder, rels, o, job):
    """Run the chosen effects over the given files (only THIS job's files). Returns the resulting file list."""
    ff, cancel = _need_ffmpeg(), job_cancel(job)
    vf, af, tag = fx_plan(o)
    out = []
    media = [r for r in rels if r.lower().endswith(VIDEO_EXT + IMG_EXT)]
    for i, rel in enumerate(rels):
        src = folder / rel
        ext = src.suffix.lower()
        if rel not in media:
            out.append(rel); continue                        # subtitles, audio, covers: leave alone
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled("Cancelled by user")
        is_vid = ext in VIDEO_EXT
        if not is_vid and not vf:                            # nothing to do to a photo (e.g. only "speed")
            out.append(rel); continue
        update(job, message=f"Applying effects {media.index(rel) + 1}/{len(media)}…", progress=min(90 + i, 98))
        suffix = ".mp4" if is_vid else ext
        dst = src.with_name(f"{src.stem} [{tag}]{suffix}") if o["fx_keep"] else src.with_suffix(suffix)
        tmp = dst.with_name(f"{dst.stem}.{job['id']}.fxtmp{suffix}")      # unique per job
        filters = list(vf)
        if is_vid:
            if o["fx_speed"] != "1":
                filters.append(f"setpts=PTS/{o['fx_speed']}")
            filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")   # even size for H.264
        base = [ff, "-y", "-nostdin", "-loglevel", "error", "-i", str(src)]
        if filters:
            base += ["-vf", ",".join(filters)]
        if is_vid:
            base += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                     "-movflags", "+faststart"]
            tails = [["-an"]] if o["fx_mute"] else [["-af", af, "-c:a", "aac", "-b:a", "160k"] if af else
                                                      ["-c:a", "aac", "-b:a", "160k"]]
            if af and not o["fx_mute"]:
                tails.append(["-c:a", "aac", "-b:a", "160k"])   # retry for clips without audio
        else:
            tails = [["-frames:v", "1"] + (["-q:v", "2"] if ext in (".jpg", ".jpeg") else [])]
        err = None
        for tail in tails:
            try:
                _ffmpeg(base + tail + [str(tmp)], cancel=cancel); err = None; break
            except DownloadCancelled:
                tmp.unlink(missing_ok=True)
                raise
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                err = exc
        if err:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(str(err))
        os.replace(tmp, dst)
        if not o["fx_keep"] and dst != src:
            src.unlink(missing_ok=True)
        out.append(dst.relative_to(folder).as_posix())
    return sorted(set(out))


def make_animation(folder, rels, o, job):
    """Turn videos into GIFs or animated WebP stickers (512x512, WhatsApp size). -> (files, warnings)"""
    ff, cancel = _need_ffmpeg(), job_cancel(job)
    vf, _af, tag = fx_plan(o)
    gif = o["kind"] == "gif"
    cap = 15 if (o["clip_start"] or o["clip_end"]) else (8 if gif else 6)        # seconds
    out, warns = [], []
    media = [r for r in rels if r.lower().endswith(VIDEO_EXT + IMG_EXT)]
    for i, rel in enumerate(rels):
        src, ext = folder / rel, Path(rel).suffix.lower()
        is_vid = ext in VIDEO_EXT
        if rel not in media or (gif and not is_vid):
            out.append(rel); continue                        # photos can't become GIFs; leave other files
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled("Cancelled by user")
        update(job, message=f"Making {o['kind']} {media.index(rel) + 1}/{len(media)}…", progress=min(90 + i, 98))
        suffix = ".gif" if gif else ".webp"
        dst = src.with_name(f"{src.stem} [{o['kind']}{'-' + tag if has_fx(o) else ''}]{suffix}")
        tmp = dst.with_name(f"{dst.stem}.{job['id']}.fxtmp{suffix}")
        pre = list(vf) + ([f"setpts=PTS/{o['fx_speed']}"] if is_vid and o["fx_speed"] != "1" else [])
        if gif:
            attempts = [(12, 480, 8), (10, 360, 8), (8, 280, 8)]         # fps, width, max MB
        elif is_vid:
            attempts = [(15, 55, 0.5), (12, 40, 0.5), (10, 28, 0.5), (8, 18, 0.5)]   # fps, quality, max MB
        else:
            attempts = [(1, 80, 0.1), (1, 60, 0.1), (1, 40, 0.1), (1, 25, 0.1)]        # static sticker < 100 KB
        ok = False
        for fps, size_or_q, limit_mb in attempts:
            if gif:
                graph = ",".join(pre + [f"fps={fps}", f"scale={size_or_q}:-2:flags=lanczos"]) + \
                        ",split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer:bayer_scale=4"
                cmd = [ff, "-y", "-nostdin", "-loglevel", "error", "-t", str(cap), "-i", str(src), "-vf", graph,
                       "-an", "-loop", "0", str(tmp)]
            else:
                graph = ",".join(pre + ([f"fps={fps}"] if is_vid else []) +
                                 ["scale=512:512:force_original_aspect_ratio=decrease", "format=yuva420p",
                                  "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black@0"])
                cmd = [ff, "-y", "-nostdin", "-loglevel", "error"] + (["-t", str(cap)] if is_vid else []) + \
                      ["-i", str(src), "-vf", graph, "-an", "-c:v", "libwebp_anim", "-lossless", "0",
                       "-q:v", str(size_or_q), "-loop", "0"] + ([] if is_vid else ["-frames:v", "1"]) + [str(tmp)]
            try:
                _ffmpeg(cmd, cancel=cancel)
            except DownloadCancelled:
                tmp.unlink(missing_ok=True)
                raise
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(str(exc))
            if tmp.stat().st_size <= limit_mb * 1024 * 1024:
                ok = True; break
        if not ok:
            warns.append("could not get it under the size limit - try a shorter clip")
        os.replace(tmp, dst)
        if not o["fx_keep"]:
            src.unlink(missing_ok=True)
        out.append(dst.relative_to(folder).as_posix())
    return sorted(set(out)), warns


def postprocess_media(folder, files, o, job):
    """GIF/sticker conversion or effects after a download. Returns (files, note-or-None)."""
    try:
        if o["kind"] in ("gif", "sticker"):
            out, warns = make_animation(folder, files, o, job)
            note = f"{'GIF' if o['kind'] == 'gif' else 'Sticker'} ready · {len(out)} file(s)"
            return out, note + ("".join(f" ({w})" for w in warns))
        if o["kind"] != "audio" and has_fx(o):
            out = apply_effects(folder, files, o, job)
            return out, "Effects applied" + ("" if o["fx_keep"] else " (original replaced)") + f" · {len(out)} file(s)"
    except DownloadCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("Post-processing failed: %s", exc)
        return files, f"Downloaded, but the {'conversion' if o['kind'] in ('gif', 'sticker') else 'effects'} failed: {exc}"
    return files, None


# --------------------------------------------------------------------------- #
# Which files belong to a yt-dlp job (NO folder diffing - safe with concurrent jobs)
# --------------------------------------------------------------------------- #
def job_files(info, folder):
    """
    From the info dict yt-dlp returned for THIS job:
      main = the media files yt-dlp says it produced (already on disk)
      side = covers, subtitles, descriptions: files yt-dlp reported, plus files that share the
             exact name prefix of a main file ("Title [id].en.srt" next to "Title [id].mp4").
    """
    root = folder.resolve()
    mains, side, prefixes = set(), set(), set()

    def rel(p):
        try:
            return Path(p).resolve().relative_to(root)
        except (ValueError, OSError):
            return None

    def add(p, bucket):
        r = rel(p) if p else None
        if r is not None and (root / r).is_file() and not is_partial(r.name):
            bucket.add(r.as_posix())

    def walk(i):
        if not isinstance(i, dict):
            return
        for e in i.get("entries") or []:
            walk(e)
        for rd in i.get("requested_downloads") or []:
            fp = rd.get("filepath") or rd.get("_filename") or rd.get("filename")
            r = rel(fp) if fp else None
            if r is not None:
                prefixes.add((r.parent.as_posix(), r.stem))
                add(fp, mains)
            for s in (rd.get("requested_subtitles") or {}).values():
                add((s or {}).get("filepath"), side)
            for t in rd.get("thumbnails") or []:
                add((t or {}).get("filepath"), side)
        for s in (i.get("requested_subtitles") or {}).values():
            add((s or {}).get("filepath"), side)
        for t in i.get("thumbnails") or []:
            add((t or {}).get("filepath"), side)

    walk(info)
    for parent, stem in prefixes:
        d = root / parent
        try:
            for f in d.iterdir():
                if (f.is_file() and f.name.startswith(stem + ".") and f.suffix.lower() in SIDE_EXT
                        and not is_partial(f.name)):
                    side.add(f.relative_to(root).as_posix())
        except OSError:
            pass
    return sorted(mains), sorted(side - mains)


def fresh_since(folder, files, started):
    """The subset of files written at/after `started` (i.e. really created by this job)."""
    return [f for f in files if mtime_of(folder, f) >= started - 1]


# --------------------------------------------------------------------------- #
# Workers (run in the thread pool, never in a request thread)
# --------------------------------------------------------------------------- #
def ytdlp_worker(job, url, category, options, cancel):
    folder = BASE_DIR / category
    clock = {"start": time.time()}                      # reset once we really begin (after any waiting)
    fetched = {"n": 0}                                  # how many real download-progress events we saw
    ig = category == "instagram"
    update(job, status="running", message="Starting…")

    def hook(d):
        # Called by yt-dlp many times per second: enforce cancel + timeout here.
        if cancel.is_set():
            raise DownloadCancelled("Cancelled by user")
        if time.time() > clock["start"] + YTDLP_TIMEOUT:
            raise DownloadCancelled("Timed out and was stopped")
        if d["status"] == "downloading":
            fetched["n"] += 1                          # yt-dlp only reports this when it really fetches bytes
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            pct = (d.get("downloaded_bytes", 0) / total * 100) if total else 0
            update(job, progress=round(min(pct, 99.0), 1), message="Downloading…",
                   speed=(d.get("_speed_str") or "").strip(),
                   eta=(d.get("_eta_str") or "").strip())
        elif d["status"] == "finished":
            update(job, message="Processing file…", progress=99)

    o = sanitize_options(options)
    try:
        with named_lock(f"url:{url}", cancel, job):
            started = clock["start"] = time.time()     # "fresh" = written after THIS moment, not while we waited
            with cookie_ctx(ig) as cf:                 # private cookie copy, deleted right after
                with YoutubeDL(build_ydl_opts(o, folder, hook, cookiefile=cf, ig=ig,
                                                  ig_sub="reels" if ig and instagram_info(url)["kind"] == "reel" else "posts")) as ydl:
                    info = ydl.extract_info(url, download=True)
            main, side = job_files(info, folder)
            files = sorted(set(main) | set(side))
            if not files:
                if o["kind"] == "subs":
                    return finish_err(job, "No subtitles found for the languages you chose. Try another language, or tick “Auto-captions”.")
                return finish_err(job, "No video matched your filters." if has_filters(o)
                                  else "Nothing was saved - the site returned no downloadable media.")
            note = None
            # "real" = this job fetched something (or wrote a cover). Otherwise yt-dlp found it already on disk.
            real = fetched["n"] > 0 or o["kind"] in ("image", "subs")
            fresh = fresh_since(folder, files, started) if real else []
            if not fresh:
                note = "Already downloaded - the file is ready (tick “Re-download if it exists” to fetch it again)."
            # effects / GIF / sticker only touch THIS job's own media files
            targets = main if o["kind"] not in ("image", "subs") else ([f for f in side if f.lower().endswith(IMG_EXT)] if o["kind"] == "image" else [])
            others = [f for f in files if f not in targets]
            out, fxnote = postprocess_media(folder, targets, o, job) if targets else ([], None)
            made_now = set(out) - set(targets)             # files created by the effects step
            files = sorted(set(others) | set(out))
            note = fxnote or note
            fresh_now = (set(fresh) & set(files)) | made_now
            auto = not o["name"] and settings.get("auto_name", "date") == "date" and o["kind"] != "subs"     # nothing typed: date + number (subtitles keep ".en")
            m = rename_files(folder, sorted(set(files) & fresh_now), o, auto)
            if m:
                files = sorted({m.get(f, f) for f in files}); fresh_now = {m.get(f, f) for f in fresh_now}
                note = (note + " · " if note else "") + "saved as " + Path(sorted(m.values())[0]).name + (f" (+{len(m) - 1} more)" if len(m) > 1 else "")
            finish_ok(job, files, note, fresh=sorted(fresh_now))
    except DownloadCancelled as exc:
        finish_err(job, last_line(exc, "Cancelled by user"))
    except Exception as exc:  # noqa: BLE001
        msg = last_line(exc)

        # TikTok photo slideshows are not videos: save the pictures with gallery-dl - but only when that is really the case
        # (a /photo/ link, or yt-dlp says there is no video), not for network errors or filter misses.
        if category == "tiktok" and ("/photo/" in url or re.search(r"no video|unsupported url|not be comprised of video|photo", msg, re.I)):
            exe = shutil.which("gallery-dl")
            if not exe:
                return finish_err(job, "This TikTok post is a photo slideshow. Saving photos needs gallery-dl, which is not installed on the server "
                                       "(pip install -U gallery-dl --break-system-packages).")
            app.logger.info("yt-dlp cannot read %s - it looks like a photo slideshow, using gallery-dl", url)
            update(job, message="Photo slideshow - downloading the pictures…")
            real = resolve_tiktok(url)
            user, pid = tiktok_ids(real)
            scratch = Path(tempfile.mkdtemp(prefix="hub-tt-", dir=str(DATA_DIR)))
            try:
                return _gallery_run(job, [exe, "-d", str(scratch), real], folder, o, cancel, False, "", None,
                                    collect_root=scratch, relocate=_tiktok_relocator(scratch, folder, user, pid))
            finally:
                shutil.rmtree(scratch, ignore_errors=True)

        if type(exc).__name__ == "DownloadError":
            app.logger.warning("yt-dlp could not download %s: %s", url, msg)
        else:
            app.logger.exception("Unexpected error while downloading %s", url)
        if ig:
            msg = friendly_ig_error(msg, gallery_missing=not shutil.which("gallery-dl"))
        finish_err(job, msg)


# ---- Snapchat --------------------------------------------------------------
def fetch_story_info(username):
    """
    Best effort: read the public profile page and count the snaps in it.
    Returns {"count": n, "ids": [snap ids]} or None if the page can't be read
    (the download still works, it just falls back to the quiet-time rule).
    """
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
               "Accept-Language": "en-US,en;q=0.9"}
    for url in (f"https://story.snapchat.com/@{username}",
                f"https://www.snapchat.com/add/{username}"):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as r:
                html = r.read().decode("utf-8", "replace")
            m = NEXT_DATA_RE.search(html)
            if not m:
                continue
            data = json.loads(m.group(1))
        except (OSError, ValueError):
            continue
        # Only the MAIN story (what snapchat-dl downloads). Highlights, spotlight and
        # "related" sections also contain snap lists, so we must not count those.
        try:
            snaps = data["props"]["pageProps"]["story"]["snapList"]
        except (KeyError, TypeError):
            continue
        if not isinstance(snaps, list):
            continue
        vals = [(sn.get("snapId") or {}).get("value") for sn in snaps if isinstance(sn, dict)]
        if vals:
            return {"count": len(vals), "ids": [v for v in vals if isinstance(v, str) and v],
                    "stories": _story_details(snaps)}
    return None


def _story_details(snaps):
    """
    What the 'choose stories' picker shows for each snap. Every field is optional: Snapchat can change
    its page any time, so a missing field just means a plainer tile (never an error).
    """
    out = []
    for sn in snaps:
        if not isinstance(sn, dict):
            continue
        sid = (sn.get("snapId") or {}).get("value") if isinstance(sn.get("snapId"), dict) else None
        if not isinstance(sid, str) or not sid:
            continue
        urls = sn.get("snapUrls") if isinstance(sn.get("snapUrls"), dict) else {}
        prev = urls.get("mediaPreviewUrl")
        prev = prev.get("value") if isinstance(prev, dict) else prev
        media = urls.get("mediaUrl")
        media = media.get("value") if isinstance(media, dict) else media
        mtype = sn.get("snapMediaType")
        kind = "video" if mtype == 1 else "image" if mtype == 0 else "unknown"
        if not (isinstance(prev, str) and prev.startswith("https://")):
            prev = media if kind == "image" and isinstance(media, str) and media.startswith("https://") else None
        ts = sn.get("timestampInSec")
        ts = ts.get("value") if isinstance(ts, dict) else ts
        out.append({"id": sid, "index": len(out) + 1, "kind": kind, "preview": prev,
                    "media": media if isinstance(media, str) and media.startswith("https://") else None,   # server side only: used to fetch JUST the picked stories
                    "time": int(ts) if str(ts).isdigit() else None})
    return out


def count_snapchat_stories(username):
    info = fetch_story_info(username)
    return info["count"] if info else None


def user_dir(folder: Path, username: str):
    """snapchat/<username>/ (snapchat-dl may differ in letter case)."""
    try:
        for d in folder.iterdir():
            if d.is_dir() and d.name.lower() == username.lower():
                return d
    except OSError:
        pass
    return None


def scan_user(folder: Path, username: str) -> dict:
    """{relative path: (mtime, size)} of everything in THIS user's folder only."""
    d, out = user_dir(folder, username), {}
    if d:
        for f in d.rglob("*"):
            try:
                if f.is_file():
                    st = f.stat()
                    out[f.relative_to(folder).as_posix()] = (st.st_mtime, st.st_size)
            except OSError:
                pass
    return out


def media_names(scan: dict):
    return sorted(n for n in scan if n.lower().endswith(MEDIA_EXT) and not is_partial(n))


def new_in_scan(scan: dict, before: dict):
    return [n for n in media_names(scan) if n not in before or scan[n][0] > before[n][0]]


def downloaded_ids(ids, names):
    """Which story ids already appear in saved file names (None-safe)."""
    blob = "\n".join(names)
    return {i for i in ids if i in blob}


_SNAP_EXT = {"video/mp4": "mp4", "video/quicktime": "mov", "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
SNAP_MAX_BYTES = 800 * 1024 * 1024


def _snap_media_ok(url):
    """Story files are only fetched from Snapchat's own hosts, over https."""
    try:
        u = urlparse(url)
    except ValueError:
        return False
    return u.scheme == "https" and host_is(u.hostname or "", *THUMB_HOSTS)


def _snap_fetch_story(dest, story, cancel, tick=None):
    """Save ONE story from its own media link into dest. Returns the final Path."""
    url = story["media"]
    if not _snap_media_ok(url):
        raise ValueError("the story link is not on a Snapchat host")
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        if not _snap_media_ok(r.geturl()):
            raise ValueError("the story link redirected away from Snapchat")
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not (ctype.startswith(("video/", "image/")) or ctype == "application/octet-stream"):
            raise ValueError("that link is not a picture or video")
        ext = _SNAP_EXT.get(ctype) or {"video": "mp4", "image": "jpg"}.get(story.get("kind"))
        if not ext:
            raise ValueError("unknown media type")
        total = int(r.headers.get("Content-Length") or 0)
        sid = re.sub(r"[^A-Za-z0-9._=\-]", "_", story["id"])[:120]     # the id stays in the name: that is how already-saved stories are recognised
        name = f"{time.strftime('%Y-%m-%d_%H-%M-%S', time.localtime(story.get('time') or time.time()))}_{sid}.{ext}"
        final, part, got = dest / name, dest / (name + ".part"), 0
        try:
            with open(part, "wb") as f:
                while True:
                    if cancel.is_set():
                        raise DownloadCancelled("Cancelled by user")
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > SNAP_MAX_BYTES:
                        raise ValueError("the file is too large")
                    f.write(chunk)
                    if tick and total:
                        tick(got / total)
            if got < 512:
                raise ValueError("the download was empty")
            os.replace(part, final)
        finally:
            part.unlink(missing_ok=True)
    return final


def _snapchat_direct(job, username, cancel, options, picks, folder, done0, log, total_online):
    """
    Download ONLY the ticked stories, each straight from its own link. No other story of this user is fetched
    (the older snapchat-dl route fetches everything in order and deletes the extras afterwards).
    """
    dest = user_dir(folder, username) or (folder / username)
    dest.mkdir(parents=True, exist_ok=True)
    todo = [p for p in picks if p["id"] not in done0]
    new, failed = [], []
    log.append(f"[hub] fetching exactly {len(todo)} picked stories - nothing else is downloaded")
    for n, st in enumerate(todo, 1):
        if cancel.is_set():
            break
        base = 5 + (n - 1) / len(todo) * 88
        update(job, message=f"Downloading story {n} of {len(todo)}…", progress=round(base, 1),
               speed="", eta="")
        try:
            p = _snap_fetch_story(dest, st, cancel, lambda fr, b=base, w=88 / len(todo): update(job, progress=round(b + fr * w, 1)))
            new.append(f"{dest.name}/{p.name}")
        except DownloadCancelled:
            break
        except (OSError, ValueError) as exc:
            failed.append(st["id"])
            log.append(f"[hub] story {st['id']} failed: {exc}")
    update(job, log="\n".join(log[-60:]))
    if cancel.is_set():
        return finish_err(job, "Cancelled by user" + (f" - {len(new)} stories had already been saved." if new else ""))
    if not new:
        return finish_err(job, "None of the picked stories could be downloaded (they may have just expired). Open the list again.")
    note = f"Downloaded {len(new)} picked stor{'y' if len(new) == 1 else 'ies'}"
    if done0:
        note += f" · {len(done0)} already saved"
    if failed:
        note += f" · {len(failed)} could not be fetched"
    others = total_online - len(picks)
    if others > 0:
        note += f" · the other {others} were not touched"
    o = dict(sanitize_options(options), kind="video")
    if has_fx(o):
        update(job, message="Applying effects…", progress=94)
        new, fxnote = postprocess_media(folder, new, o, job)
        note += f" · {fxnote}" if fxnote else ""
    if o["name"]:
        m = rename_files(folder, new, o)
        if m:
            new = sorted({m.get(f, f) for f in new})
            note += " · saved as " + Path(sorted(m.values())[0]).name
    return finish_ok(job, new, note, fresh=new)


def snapchat_worker(job, username, cancel, options=None, only=None):
    """One snapchat job per username at a time; the rest is in _snapchat_run. only = set of picked story ids."""
    try:
        with named_lock(f"snap:{username.lower()}", cancel, job):
            _snapchat_run(job, username, cancel, options, only)
    except DownloadCancelled as exc:
        finish_err(job, last_line(exc, "Cancelled by user"))


def _snapchat_run(job, username, cancel, options=None, only=None):
    """
    1. Count the user's stories (and their ids) from the public profile page.
    2. Stories whose id is already in a saved file name are SKIPPED. If all of them
       are saved, snapchat-dl is not even started.
    3. Otherwise run `snapchat-dl -u <username>` (it skips existing files itself) and
       follow progress by counting stories on disk (ONLY in snapchat/<username>/).
       Finish ~5s after the last new file, or 35s after the last new file if some never arrive.
       snapchat-dl's own output is NOT treated as progress (its update loop keeps chattering).
    4. only = the story ids you ticked in the picker: we wait only for those, stop snapchat-dl as soon as they
       are saved, and remove the extra stories it fetched on the way (never anything that was already on disk).
    """
    folder = BASE_DIR / "snapchat"
    before = scan_user(folder, username)
    update(job, status="running", message=f"Counting @{username}'s stories…", progress=2)
    info = fetch_story_info(username)
    if cancel.is_set():
        return finish_err(job, "Cancelled by user")
    expected = info["count"] if info else None
    ids = info["ids"] if info else []
    log = [f"[hub] counted {expected} stories on the profile page"] if expected else []
    all_ids, total = list(ids), expected
    if only is not None:                                # "download only the stories I ticked"
        if not info or not ids:
            return finish_err(job, "I could not read the story list right now, so single stories cannot be picked. "
                                   "Try again in a moment, or download all.")
        ids = [i for i in ids if i in only]
        if not ids:
            return finish_err(job, "The stories you picked are no longer online (Snapchat stories disappear after "
                                   "24 hours). Open the list again.")
        expected = len(ids)
        log.append(f"[hub] only {expected} picked stories wanted (of {total} online)")

    # --- skip what we already have -----------------------------------------
    names0 = media_names(before)
    done0 = downloaded_ids(ids, names0) if ids else set()
    if ids and len(done0) == len(ids):
        mine = [n for n in names0 if any(i in n for i in ids)]
        log.append(f"[hub] all {len(ids)} stories are already on disk - download skipped")
        update(job, log="\n".join(log))
        return finish_ok(job, mine, f"All {len(ids)} stories were already downloaded - nothing new", fresh=[])
    if expected:
        extra = f" ({len(done0)} already saved, getting the rest…)" if done0 else " - downloading…"
        what = f"{expected} picked stor{'y' if expected == 1 else 'ies'}" if only is not None else f"{expected} stories"
        update(job, expected=expected, message=f"Found {what}{extra}", progress=5)
        log.append(f"[hub] {len(done0)} of {expected} already on disk")
    else:
        update(job, message=f"Fetching stories for @{username}…", progress=5)
    if only is not None and info:                       # preferred route: fetch ONLY the picked stories from their own links
        by_id = {s_["id"]: s_ for s_ in (info.get("stories") or [])}
        picks = [by_id.get(i) for i in ids]
        if picks and all(p and p.get("media") for p in picks):
            return _snapchat_direct(job, username, cancel, options, picks, folder, done0, log, total or len(all_ids))
        log.append("[hub] no direct links for these stories - using snapchat-dl instead")
    hard_limit = min(SNAP_HARD_TIMEOUT, max(300, (total or expected) * 30)) if expected else SNAP_HARD_TIMEOUT

    env = {**os.environ, "PYTHONUNBUFFERED": "1", "NO_COLOR": "1"}
    folder.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(["snapchat-dl", "-u", username], cwd=str(folder), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", bufsize=1,
                                start_new_session=True)
    except FileNotFoundError:
        return finish_err(job, "snapchat-dl is not installed or not in PATH on the server.")
    except OSError as exc:
        return finish_err(job, f"Could not start snapchat-dl: {exc}")

    lines = queue.Queue()

    def reader():
        for raw in proc.stdout:
            lines.put(ANSI_RE.sub("", raw).strip())
        lines.put(None)                              # output closed = process ended
    threading.Thread(target=reader, daemon=True).start()

    start = last = last_new = last_any = time.time()  # last_new = when a NEW story last appeared; last_any = any file change
    last_sel = None
    done_at, reason, saved, use_ids = None, None, len(done0), False
    last_sig, active = (len(before), sum(s for _, s in before.values())), False
    cur = before
    try:
        while True:
            try:
                line = lines.get(timeout=1)
                if line is None:
                    break
                if line and line != (log[-1] if log else None):       # an identical repeat is chatter, not progress
                    log.append(line); last = time.time(); active = True
                    if not expected:
                        update(job, message=line.split(" - ", 1)[-1][:120])
                    if SNAP_DONE_RE.search(line) and not done_at:
                        done_at = time.time()
            except queue.Empty:
                pass
            now = time.time()

            cur = scan_user(folder, username)             # one scan of THIS user's folder per tick
            sig = (len(cur), sum(s for _, s in cur.values()))
            if sig != last_sig:
                last_sig, last, active, last_any = sig, now, True, now

            # Stories on disk: by id when file names contain the ids, else new files only.
            matched = downloaded_ids(ids, media_names(cur)) if ids else set()
            use_ids = bool(matched) or only is not None
            n = len(matched) if use_ids else len(new_in_scan(cur, before))
            if only is not None:                     # a picked file that is still growing is not finished yet
                sel = tuple(sorted((k, v[1]) for k, v in cur.items() if any(i in k for i in ids)))
                if sel != last_sel:
                    last_sel, last_new = sel, now
            if n != saved:
                saved, last, last_new = n, now, now
                if expected:
                    got = max(n - len(done0), 0)
                    eta = max(0, int((expected - n) * (now - start) / got)) if got else 0
                    update(job, message=f"Saved {n} of {expected} stories…",
                           progress=min(5 + n / expected * 90, 95),
                           eta=f"{eta}s" if n < expected else "")
                else:
                    update(job, message=f"Saved {n} stor{'y' if n == 1 else 'ies'}…",
                           progress=min(5 + n * 0.7, 95))

            if cancel.is_set():
                reason = "Cancelled by user"; break
            if now - start > hard_limit:
                reason = "Timed out and was stopped"; break
            if expected and saved >= expected and now - last_new > SNAP_FULL_QUIET:
                break                                    # every story is on disk: finish
            quiet = last_any if only is not None else last_new      # picking: snapchat-dl may still be fetching OTHER stories
            if expected and now - quiet > (SNAP_STALL_LIMIT if saved > len(done0) else SNAP_START_TIMEOUT):
                break                                    # nothing new for a while: finish
            if done_at and now - last > SNAP_AFTER_DONE_IDLE:
                break                                    # said done and nothing more is arriving
            if not expected and now - last > (SNAP_IDLE_TIMEOUT if active else SNAP_START_TIMEOUT):
                break                                    # no count available: go by quiet time
    finally:
        _stop_process(proc)                              # ALWAYS: snapchat-dl -u never exits by itself
    update(job, log="\n".join(log[-60:]))                # shown in the popup for troubleshooting

    cur = scan_user(folder, username)
    new = new_in_scan(cur, before)
    dropped, unfiltered = 0, False
    if only is not None:
        if any(i in n for n in media_names(cur) for i in all_ids):       # only filter when file names really carry the ids
            for n in [n for n in new if not any(i in n for i in ids)]:
                (folder / n).unlink(missing_ok=True)                     # an extra story snapchat-dl fetched before reaching your picks
                dropped += 1
            cur = scan_user(folder, username)
            new = new_in_scan(cur, before)
        else:
            unfiltered = True                                            # cannot tell stories apart: keep everything, say so
    if reason == "Cancelled by user":
        return finish_err(job, "Cancelled by user" + (f" - {len(new)} stories had already been saved." if new else ""))
    if new:
        skipped = max(0, saved - len(new)) if use_ids else 0
        note = f"Downloaded {len(new)} {'picked ' if only is not None and not unfiltered else 'new '}stor{'y' if len(new) == 1 else 'ies'}"
        if skipped:
            note += f" · {skipped} already saved"
        if dropped:
            note += f" · the {dropped} other{'s' if dropped != 1 else ''} snapchat-dl fetched on the way were not kept"
        if unfiltered:
            note += " · could not match file names to stories, so everything was kept"
        if reason:
            note += f" · stopped early: {reason}"
        o = dict(sanitize_options(options), kind="video")     # stories: only the effects apply
        if has_fx(o):
            update(job, message="Applying effects…", progress=92)
            new, fxnote = postprocess_media(folder, new, o, job)
            note += f" · {fxnote}" if fxnote else ""
        if o["name"]:
            m = rename_files(folder, new, o)
            if m:
                new = sorted({m.get(f, f) for f in new})
                note += " · saved as " + Path(sorted(m.values())[0]).name
        return finish_ok(job, new, note, fresh=new)
    if reason:
        return finish_err(job, reason)
    hint = next((l for l in reversed(log) if SNAP_ERR_RE.search(l) and not l.startswith("[hub]")), None)
    if hint and not done_at:
        return finish_err(job, hint.split(" - ", 1)[-1])
    mine = media_names(cur)
    if only is not None and not unfiltered:
        mine = [n for n in mine if any(i in n for i in ids)]
        if not mine:
            return finish_err(job, "None of the picked stories could be downloaded (they may have just expired).")
    if not mine and not done_at:
        return finish_err(job, "No new stories found (user may have none).")
    finish_ok(job, mine, "Nothing new - these stories were already downloaded", fresh=[])


# ---- Instagram via gallery-dl (photos, carousels, stories, highlights, profiles) ----
def _gallery_range(o, ig):
    items = (o.get("items") or "").replace(" ", "")
    if items and re.fullmatch(r"\d[\d,\-:]*", items):
        return items, False
    if ig["kind"] == "profile" and o.get("ig_what", "posts") in ("posts", "all"):
        return f"1-{ig_cap()}", True      # stories/highlights are few: no cap
    return "", False


def ig_cap():
    try:
        return max(1, min(500, int(settings.get("ig_cap", IG_DEFAULT_CAP))))
    except (TypeError, ValueError):
        return IG_DEFAULT_CAP


def _gallery_path(line, root: Path):
    """gallery-dl prints '<path>' for new files and '# <path>' for ones it skipped (already on disk)."""
    old = line.startswith("# ")
    text = line[2:] if old else line
    if not text.startswith("/"):
        return None, False
    try:
        p = Path(text)
        rel = p.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None, False
    return (rel.as_posix(), old) if p.is_file() and not is_partial(p.name) else (None, False)


def instagram_gallery_worker(job, url, category, options, cancel):
    folder = BASE_DIR / "instagram"
    folder.mkdir(parents=True, exist_ok=True)
    o = sanitize_options(options)
    ig = instagram_info(url)
    update(job, status="running", message="Starting…", progress=2)
    exe = shutil.which("gallery-dl")
    if not exe:
        if ig["kind"] == "post":                       # a video post can still be fetched by yt-dlp
            return ytdlp_worker(job, url, category, options, cancel)
        return finish_err(job, GALLERY_MISSING)
    st = cookie_status()
    if not st["saved"]:
        return finish_err(job, IG_NEEDS_LOGIN)
    if st["session_expired"] or not st["has_session"]:
        return finish_err(job, IG_LOGIN_BAD)
    rng, capped = _gallery_range(o, ig)
    try:
        with named_lock(f"url:{url}", cancel, job), cookie_copy() as cf:
            if not cf:
                return finish_err(job, IG_NEEDS_LOGIN)
            cmd = [exe, "--cookies", cf, "-d", str(BASE_DIR)]
            if settings.get("ig_organize", True):                         # username / date / type folders
                cfg = os.path.join(os.path.dirname(cf), "gallery-dl.json")  # lives in the private temp dir
                Path(cfg).write_text(json.dumps(IG_LAYOUT))
                cmd += ["-c", cfg]
            if ig["kind"] == "stories" and ig["user"]:
                url = f"https://www.instagram.com/stories/{ig['user']}/"      # ALL current stories, not only the pasted one
            all_mode = ig["kind"] == "profile" and o["ig_what"] == "all"
            if ig["kind"] == "profile" and not all_mode:
                cmd += ["-o", f"include={o['ig_what']}"]
            if rng and not all_mode:
                cmd += ["--range", rng]
            if o["force"]:
                cmd.append("--no-skip")
            if all_mode:
                # one run per type: the item cap only limits posts + reels, never stories or highlights
                passes = [(label, cmd + ["-o", f"include={label}"] + (["--range", rng] if ranged and rng else []) + [url], ranged)
                          for label, ranged in (("stories", False), ("highlights", False), ("posts", True), ("reels", True))]
                _gallery_run(job, None, folder, o, cancel, capped, rng, ig, passes=passes)
            else:
                cmd.append(url)
                _gallery_run(job, cmd, folder, o, cancel, capped, rng, ig)
    except DownloadCancelled as exc:
        finish_err(job, last_line(exc, "Cancelled by user"))


def _gallery_collect(job, cmd, folder, cancel, cap_n, ig, label=""):
    """Run ONE gallery-dl process. Returns (new, old, log, reason, rc)."""
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "NO_COLOR": "1"}
    pre = f"{label}: " if label else ""
    try:
        proc = subprocess.Popen(cmd, cwd=str(folder), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", bufsize=1, start_new_session=True)
    except OSError as exc:
        return [], [], [f"Could not start gallery-dl: {exc}"], f"Could not start gallery-dl: {exc}", -1
    lines = queue.Queue()

    def reader():
        for raw in proc.stdout:
            lines.put(ANSI_RE.sub("", raw).rstrip("\n").rstrip())
        lines.put(None)
    threading.Thread(target=reader, daemon=True).start()

    started = last = time.time()
    new, old, log, reason, eof = [], [], [], None, False
    try:
        while True:
            try:
                line = lines.get(timeout=1)
                if line is None:
                    eof = True
                    break
            except queue.Empty:
                line = ""
            if line:
                last = time.time()
                log.append(line.replace(str(BASE_DIR), "~"))
                rel, was_old = _gallery_path(line, folder)
                if rel:
                    (old if was_old else new).append(rel)
                    n = len(set(new) | set(old))
                    update(job, message=f"{pre}saved {n} item{'s' if n != 1 else ''}…",
                           progress=min(5 + (n / cap_n if cap_n else n / (n + 8)) * 90, 95))
            now = time.time()
            if cancel.is_set():
                reason = "Cancelled by user"; break
            if now - started > IG_HARD_TIMEOUT:
                reason = "Timed out and was stopped"; break
            if now - last > IG_IDLE_TIMEOUT:
                reason = "Instagram stopped answering - try again later" if ig else "gallery-dl stopped answering - try again later"
                break
    finally:
        if not eof:
            _stop_process(proc)
    try:
        rc = proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        _stop_process(proc)
        rc = -1
    return sorted(set(new)), sorted(set(old) - set(new)), log, reason, rc


def _gallery_run(job, cmd, folder, o, cancel, capped, rng, ig, passes=None, collect_root=None, relocate=None):
    """
    ig = Instagram link info, or None for the TikTok slideshow fallback.
    passes = [(label, full command, capped?)]: one gallery-dl run per content type, so the item cap
             only limits posts/reels and never cuts off stories or highlights.
    collect_root + relocate = (TikTok photos) gallery-dl works in a scratch folder, then relocate() moves the files to their
             final place and returns (new, old, "where it is").
    """
    cap_n = int(rng.split("-")[1]) if capped and "-" in rng else 0
    if passes:
        new, old, log, reason, rc = [], [], [], None, 0
        for label, pcmd, ranged in passes:
            update(job, message=f"Taking {label}…")
            n_, o_, l_, r_, rc_ = _gallery_collect(job, pcmd, folder, cancel, cap_n if ranged else 0, ig, label)
            new += n_; old += o_; log += [f"── {label} ──"] + l_
            if rc_ not in (0, None) and not (n_ or o_):
                rc = rc_                                  # a pass with nothing + an error code (e.g. no highlights)
            if r_:
                reason = r_
                if r_ == "Cancelled by user":
                    break
        new, old = sorted(set(new)), sorted(set(old) - set(new))
        if new or old:
            rc = 0                                        # something was saved: an empty pass is not an error
    else:
        new, old, log, reason, rc = _gallery_collect(job, cmd, collect_root or folder, cancel, cap_n, ig)
    where = ""
    if relocate and (new or old):
        new, old, where = relocate(new, old)
    update(job, log="\n".join(log[-80:]))

    if reason == "Cancelled by user":
        return finish_err(job, reason)
    if not new and not old:
        if reason:
            return finish_err(job, reason)
        if rc not in (0, None):
            errs = [l for l in log if re.search(r"error|warning|http|login|auth|forbidden|unauthori|not found|private",
                                                l, re.I)]
            _m = last_line("\n".join(errs[-3:]) or (log[-1] if log else ""), "gallery-dl failed")
            return finish_err(job, friendly_ig_error(_m) if ig else _m)      # Instagram wording only for Instagram
        return finish_err(job, "Nothing found there (no posts, or the account is private).")
    note = f"Downloaded {len(new)} new item{'s' if len(new) != 1 else ''}" if new else "Already downloaded - nothing new"
    if new and old:
        note += f" · {len(old)} already saved"
    if where:
        note += f" · saved in {where}"
    if capped:
        note += f" · {'posts and reels' if passes else 'profile'} capped at {cap_n} (set a range under Customize → More options → Items to change)"
    if reason:
        note += f" · stopped early: {reason}"
    elif rc not in (0, None):
        note += " · finished with some errors (see log)"
    fresh = list(new)
    o2 = dict(o, kind=("video" if o["kind"] in ("audio", "image") else o["kind"]))
    if new and (has_fx(o2) or o2["kind"] in ("gif", "sticker")):
        update(job, message="Applying effects…", progress=92)
        out, fxnote = postprocess_media(folder, new, o2, job)
        fresh = list(out)
        new = out
        note += f" · {fxnote}" if fxnote else ""
    if o.get("name") and new:
        m = rename_files(folder, new, o)
        if m:
            new = sorted({m.get(f, f) for f in new}); fresh = sorted({m.get(f, f) for f in fresh})
            note += " · saved as " + Path(sorted(m.values())[0]).name
    finish_ok(job, sorted(set(new) | set(old)), note, fresh=fresh)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


def _fetch_info(url):
    with cookie_ctx(is_instagram(url)) as cf:
        opts = {"skip_download": True, "noplaylist": True, "quiet": True, "no_warnings": True, "socket_timeout": 15}
        if cf:
            opts["cookiefile"] = cf
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)


# ---- TikTok: short links, photo slideshows ----
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIKTOK_SHORT = re.compile(r"^https?://((vm|vt)\.tiktok\.com/|(www\.)?tiktok\.com/t/)", re.I)
TIKTOK_POST = re.compile(r"/(?:photo|video)/\d+")


def resolve_tiktok(url, timeout=6):
    """Share links (vm.tiktok.com/…, tiktok.com/t/…) -> the real address, which says whether it is a /photo/ or a /video/."""
    if not TIKTOK_SHORT.match(url or ""):
        return url
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": _UA}), timeout=timeout) as r:
            return r.geturl() or url
    except Exception:  # noqa: BLE001
        return url


def tiktok_ids(url):
    """(creator, post id) from a TikTok address; '' when unknown."""
    u = re.search(r"/@([^/?#]+)", url or "")
    i = re.search(r"/(?:photo|video)/(\d+)", url or "")
    return (u.group(1) if u else ""), (i.group(1) if i else "")


def tiktok_oembed(url, timeout=6):
    """TikTok's public 'embed' info (title, creator, cover picture) - works for photo posts too. {} if it cannot be read."""
    try:
        req = urllib.request.Request("https://www.tiktok.com/oembed?url=" + quote(url, safe=""), headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
            return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _tiktok_slideshow_preview(url):
    user, _pid = tiktok_ids(url)
    oe = tiktok_oembed(url)
    return jsonify(success=True, title=oe.get("title") or "TikTok photo post", thumbnail=oe.get("thumbnail_url") or "",
                   uploader=oe.get("author_unique_id") or user or oe.get("author_name") or "", duration=None, views=None, upload_date=None,
                   site="TikTok", category="tiktok", channel_url="", is_slideshow=True)


def _tiktok_relocator(tmp, dest_root, user, pid):
    """
    gallery-dl saves wherever ITS layout says. We download into a scratch folder instead and put the photos where the
    file browser expects them:  tiktok/<creator>/slideshow <post id>/001.jpg, 002.jpg …  (so the folder always exists).
    """
    who = re.sub(r"[^\w.\-]", "_", user or "")[:60].strip("._") or "unknown"
    post = f"slideshow {pid}" if pid else "slideshow " + time.strftime("%Y%m%d-%H%M%S")

    def run(new, old):
        folder, moved, kept = dest_root / who, [], []
        for i, rel in enumerate(sorted(set(new) | set(old)), 1):
            src = tmp / rel
            if not src.is_file():
                continue
            name = f"{pid or post}_{i:03d}{src.suffix.lower()}" if src.suffix.lower() in IMG_EXT else src.name
            dst = folder / name
            rel_dst = dst.relative_to(dest_root).as_posix()
            if dst.exists():
                kept.append(rel_dst)                      # saved before: keep it, do not duplicate
                continue
            try:
                folder.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved.append(rel_dst)
            except OSError:
                continue
        return moved, kept, f"tiktok/{who}/{post}/"
    return run


def _ig_preview(url, ig):
    """Preview for Instagram links yt-dlp cannot read (photos, stories, profiles): no network call, no noise."""
    user = ig["user"]
    title = {"post": f"Instagram post {ig['code']}" + (f" by @{user}" if user else ""),
             "stories": f"Stories of @{user}", "highlight": "Instagram highlight",
             "profile": f"Instagram profile @{user}"}.get(ig["kind"], "Instagram link")
    return jsonify(success=True, gallery=True, title=title, thumbnail="", uploader=user, duration=None,
                   views=None, upload_date=None, site="Instagram", category="instagram", channel_url="",
                   ig={"kind": ig["kind"], "gallery_dl": bool(shutil.which("gallery-dl")),
                       "login": cookie_status(), "cap": ig_cap() if ig["kind"] == "profile" else 0})


@app.route("/preview", methods=["POST"])
def preview():
    """Title + thumbnail without downloading (time-limited)."""
    url = clean_url(str(json_body().get("url") or "").strip())
    if not url:
        return jsonify(success=False, error="No URL provided"), 400
    if not valid_http_url(url):
        return jsonify(success=False, error="That doesn't look like a valid link."), 400

    lu = live_user_from(url)
    if lu:                                                       # TikTok LIVE link: say whether they are live now
        try:
            st = preview_pool.submit(tt_live_status, lu).result(timeout=PREVIEW_TIMEOUT)
        except FutTimeout:
            return jsonify(success=False, error="TikTok did not answer in time - try again."), 504
        except LiveError as exc:
            return jsonify(success=False, error=str(exc)), 502
        with live_lock:
            rec = live_now.get(lu.lower())
        return jsonify(success=True, title=(st["title"] or f"@{lu}'s LIVE") if st["live"] else f"@{lu} is not live right now",
                       thumbnail=st["cover"] if st["live"] else "", uploader="@" + lu, duration=None, views=None,
                       upload_date=None, site="TikTok LIVE", category="tiktok", channel_url="",
                       live={"user": lu, "is_live": st["live"], "recording": rec})

    # --- TikTok: share links -> real address; photo posts are not videos, so yt-dlp cannot read them (the download handles them) ---
    tiktok = detect_category(url) == "tiktok"
    if tiktok:
        url = resolve_tiktok(url)
        if "/photo/" in url:
            return _tiktok_slideshow_preview(url)

    igx = None
    if is_instagram(url):
        igx = instagram_info(url)
        if igx["kind"] == "unknown":
            return jsonify(success=False, error="That Instagram link type is not supported. Use a reel, post, "
                                                "story, highlight or profile link."), 400
        if igx["kind"] != "reel":
            return _ig_preview(url, igx)
    try:
        info = preview_pool.submit(_fetch_info, url).result(timeout=PREVIEW_TIMEOUT)
        extra = {}
        if igx:
            extra = {"ig": {"kind": "reel", "gallery_dl": bool(shutil.which("gallery-dl")), "login": cookie_status(), "cap": 0}}
        return jsonify(success=True, title=info.get("title", "Unknown Title"),
                       thumbnail=info.get("thumbnail", ""),
                       uploader=info.get("uploader") or info.get("channel") or "",
                       duration=info.get("duration"), views=info.get("view_count"),
                       upload_date=info.get("upload_date"), quality=_quality_summary(info),
                       site=info.get("extractor_key", ""), category=detect_category(url),
                       channel_url="" if igx else (info.get("channel_url") or info.get("uploader_url") or ""),
                       **extra)
    except FutTimeout:
        return jsonify(success=False, error="Preview timed out - you can still download."), 504
    except Exception as exc:  # noqa: BLE001
        msg = last_line(exc, "Preview failed")
        if igx:
            msg = friendly_ig_error(msg)
        app.logger.info("Preview failed for %s: %s", url, msg)
        if tiktok and re.search(r"unsupported url|no video|not be comprised of video|photo", msg, re.I):
            if TIKTOK_POST.search(url):
                return _tiktok_slideshow_preview(url)     # a photo slideshow: say so instead of "unsupported"
            if TIKTOK_SHORT.match(url):                   # a share link we could not open: the download works out what it is
                return jsonify(success=True, title="TikTok link", thumbnail="", uploader="", duration=None, views=None, upload_date=None,
                               site="TikTok", category="tiktok", channel_url="")
        return jsonify(success=False, error=msg[:300])


def _quality_summary(info):
    """Best picture this link offers: {height, fps, hdr} (shown as '4K · 60 fps · HDR' on the preview card)."""
    try:
        fm = [f for f in (info.get("formats") or []) if isinstance(f, dict) and f.get("vcodec") not in (None, "none")]
        hs = [f["height"] for f in fm if isinstance(f.get("height"), int)]
        if not hs:
            return None
        top = max(hs)
        fps = max([f.get("fps") or 0 for f in fm if f.get("height") == top] or [0])
        return {"height": top, "fps": int(round(fps)),
                "hdr": any(str(f.get("dynamic_range") or "").upper().startswith("HDR") for f in fm)}
    except Exception:  # noqa: BLE001 - a missing badge is better than a failed preview
        return None


PL_CACHE = {}
_CHANNEL_RE = re.compile(r"^(https?://(?:www\.|m\.)?youtube\.com/(?:@[\w.\-]+|channel/[\w\-]+|c/[\w\-]+|user/[\w\-]+))/?$", re.I)


def fetch_playlist(url, limit=300):
    """The videos of a playlist / channel / profile WITHOUT downloading anything (fast 'flat' listing)."""
    m = _CHANNEL_RE.match(url)
    if m:
        url = m.group(1) + "/videos"                    # a bare channel link lists its tabs; we want the videos
    with cookie_ctx(False) as cf:
        opts = {"skip_download": True, "quiet": True, "no_warnings": True, "socket_timeout": 20,
                "extract_flat": "in_playlist", "playlistend": limit, "noplaylist": False}
        if cf:
            opts["cookiefile"] = cf
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    entries = []
    for e in (info.get("entries") or []):
        if not isinstance(e, dict):
            continue
        u = e.get("url") or e.get("webpage_url")
        if isinstance(u, str) and not u.startswith("http") and str(e.get("ie_key") or "").lower() == "youtube" and e.get("id"):
            u = f"https://www.youtube.com/watch?v={e['id']}"
        if not isinstance(u, str) or not valid_http_url(u):
            continue
        th = e.get("thumbnail") or next((t.get("url") for t in reversed(e.get("thumbnails") or []) if isinstance(t, dict) and t.get("url")), None)
        entries.append({"index": len(entries) + 1, "id": str(e.get("id") or len(entries) + 1)[:60],
                        "title": str(e.get("title") or f"Video {len(entries) + 1}")[:160], "duration": e.get("duration"),
                        "url": u, "thumb": th if isinstance(th, str) and th.startswith("https://") else ""})
    return {"title": str(info.get("title") or "Playlist")[:120], "uploader": str(info.get("uploader") or info.get("channel") or "")[:80],
            "total": info.get("playlist_count"), "entries": entries}


@app.route("/playlist-info", methods=["POST"])
def playlist_info():
    """List a playlist / channel for the 'Choose videos' picker."""
    url = clean_url(str(json_body().get("url") or "").strip())
    if not valid_http_url(url):
        return jsonify(success=False, error="That doesn't look like a valid link."), 400
    hit = PL_CACHE.get(url)
    if hit and time.time() - hit[0] < 600:
        return jsonify(success=True, **hit[1])
    try:
        data = preview_pool.submit(fetch_playlist, url).result(timeout=70)
    except FutTimeout:
        return jsonify(success=False, error="Reading the list took too long. Try again, or download without choosing."), 504
    except Exception as exc:  # noqa: BLE001
        return jsonify(success=False, error=last_line(exc, "Could not read this list")[:300])
    if len(data["entries"]) < 1:
        return jsonify(success=False, error="No videos were found in this link.")
    if len(PL_CACHE) > 30:
        PL_CACHE.clear()
    PL_CACHE[url] = (time.time(), data)
    return jsonify(success=True, **data)


@app.route("/snap-info")
def snap_info():
    """Story count for the Snapchat preview card (best effort, time-limited)."""
    user = (request.args.get("user") or "").lstrip("@").strip()
    if not SNAP_USER_RE.match(user):
        return jsonify(success=False, error="Invalid Snapchat username."), 400
    try:
        count = preview_pool.submit(count_snapchat_stories, user).result(timeout=20)
    except Exception:  # noqa: BLE001
        count = None
    return jsonify(success=True, user=user, count=count)


STORY_CACHE, THUMB_CACHE = {}, {}
THUMB_HOSTS = ("sc-cdn.net", "snapchat.com", "snap.com", "snapcdn.net", "sc-gw.com")     # thumbnails are only fetched from Snapchat's own hosts
THUMB_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}                     # never SVG/HTML: nothing scriptable gets through


def _fetch_bytes(url, limit=4 * 1024 * 1024, timeout=10):
    """Small download helper (kept separate so tests can replace it). Returns (content-type, bytes)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(limit + 1)
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if len(data) > limit:
        raise OSError("thumbnail too large")
    return ctype, data


def story_list(user):
    """The user's current stories (cached 10 min) or None."""
    key, now = user.lower(), time.time()
    hit = STORY_CACHE.get(key)
    if hit and now - hit[0] < 600:
        return hit[1]
    info = preview_pool.submit(fetch_story_info, user).result(timeout=25)
    if info:
        if len(STORY_CACHE) > 50:
            STORY_CACHE.clear()
        STORY_CACHE[key] = (now, info)
    return info


@app.route("/snap-stories")
def snap_stories():
    """The tiles of the 'Choose stories' picker (no Snapchat URLs are handed to the browser)."""
    user = (request.args.get("user") or "").lstrip("@").strip()
    if not SNAP_USER_RE.match(user):
        return jsonify(success=False, error="Invalid Snapchat username."), 400
    try:
        info = story_list(user)
    except Exception:  # noqa: BLE001
        info = None
    if not info or not info.get("ids"):
        return jsonify(success=False, error="Could not read this user's stories (none right now, a private account, "
                                            "or Snapchat blocked the request).")
    stories = info.get("stories") or [{"id": i, "index": n + 1, "kind": "unknown", "time": None, "preview": None}
                                      for n, i in enumerate(info["ids"])]
    saved = downloaded_ids([s_["id"] for s_ in stories], media_names(scan_user(BASE_DIR / "snapchat", user)))
    return jsonify(success=True, user=user, count=len(stories), stories=[
        {"id": s_["id"], "index": s_["index"], "kind": s_.get("kind", "unknown"), "time": s_.get("time"),
         "thumb": bool(s_.get("preview")), "saved": s_["id"] in saved} for s_ in stories])


@app.route("/snap-thumb")
def snap_thumb():
    """A story's preview picture, fetched by the server (only from Snapchat's hosts, only real images)."""
    user = (request.args.get("user") or "").lstrip("@").strip()
    sid = request.args.get("id") or ""
    hit = STORY_CACHE.get(user.lower())
    story = next((s_ for s_ in (hit[1].get("stories") or []) if s_["id"] == sid), None) if hit and SNAP_USER_RE.match(user) else None
    url = story.get("preview") if story else None
    if not url or not host_is(urlparse(url).hostname or "", *THUMB_HOSTS):
        return jsonify(success=False, message="No preview."), 404
    cached = THUMB_CACHE.get((user.lower(), sid))
    if not cached:
        try:
            ctype, data = _fetch_bytes(url)
        except Exception:  # noqa: BLE001
            return jsonify(success=False, message="No preview."), 404
        if ctype not in THUMB_TYPES or not data:
            return jsonify(success=False, message="No preview."), 404
        if len(THUMB_CACHE) > 300:
            THUMB_CACHE.clear()
        cached = THUMB_CACHE[(user.lower(), sid)] = (ctype, data)
    resp = app.response_class(cached[1], mimetype=cached[0])
    resp.headers["Cache-Control"] = "private, max-age=900"
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return resp


def enqueue(action, target, quality="best", batch=None, options=None, only=None):
    """Validate and queue ONE download. Returns (job, None) or (None, error message)."""
    target = str(target or "").strip()
    action = str(action or "")
    if not target:
        return None, "No target provided."
    prune_jobs()
    cancel = threading.Event()
    if action == "snapchat":
        username = target.lstrip("@").strip()
        if not SNAP_USER_RE.match(username):
            return None, "Enter a valid Snapchat username."
        picked = None
        if only is not None:                                     # the stories ticked in the picker
            if not isinstance(only, list) or not only:
                return None, "Pick at least one story first."
            picked = set()
            for x in only[:500]:
                if not isinstance(x, str) or not 1 <= len(x) <= 300 or any(c.isspace() or ord(c) < 32 for c in x):
                    return None, "That story selection is not valid."
                picked.add(x)
        fn, args, category = snapchat_worker, (username, cancel, options, picked), "snapchat"
        label = f"@{username}" + (f" · {len(picked)} picked" if picked else "")
    elif action == "url":
        target = clean_url(target)
        if not valid_http_url(target):
            return None, "Please enter a valid http(s) link."
        lu = live_user_from(target)
        if lu:                                                   # a TikTok LIVE link: record it now, outside the queue
            return start_live(lu)
        category = detect_category(target)
        opts = sanitize_options(options if options else {"quality": quality})
        fn, args, label = ytdlp_worker, (target, category, opts, cancel), target
        if category == "instagram":
            kind = instagram_info(target)["kind"]
            if kind == "unknown":
                return None, "That Instagram link type is not supported (use a reel, post, story, highlight or profile link)."
            if kind != "reel":
                fn = instagram_gallery_worker
    else:
        return None, "Unknown action."
    job = {"id": uuid.uuid4().hex[:12], "category": category, "label": label,
           "status": "queued", "progress": 0, "message": "Waiting in queue…",
           "speed": "", "eta": "", "files": [], "fresh": [], "created": time.time(),
           "finished": None, "batch": batch, "folder": sanitize_options(options)["folder"]}
    with lock:
        jobs[job["id"]], cancels[job["id"]] = job, cancel
    queue_add(job, fn, args)
    return job, None


@app.route("/download-action", methods=["POST"])
def download_action():
    """Queue a download; returns a job_id immediately. Accepts form or JSON."""
    src = json_body() or request.form
    job, err = enqueue(src.get("action_type", ""), src.get("target"), src.get("quality", "best"),
                       options=src.get("options") if isinstance(src.get("options"), dict) else None,
                       only=src.get("only") if "only" in src else None)
    if err:
        return jsonify(success=False, message=err), 400
    resp = jsonify(success=True, job_id=job["id"], folder=str(BASE_DIR / job["category"]))
    resp.status_code = 202
    return _remember_jobs(resp, job["id"])


@app.route("/batch", methods=["POST"])
def batch_download():
    """Queue many downloads at once; the page shows ONE popup when the whole batch ends."""
    items = json_body().get("items")
    items = items[:50] if isinstance(items, list) else []
    bid, made, errors = uuid.uuid4().hex[:8], [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        job, err = enqueue(it.get("action_type"), it.get("target"), it.get("quality", "best"), batch=bid,
                           options=it.get("options") if isinstance(it.get("options"), dict) else None)
        (made.append(job["id"]) if job else errors.append(f"{it.get('target')}: {err}"))
    with lock:
        batches[bid] = made
    check_batch(bid)
    resp = jsonify(success=bool(made), job_ids=made, errors=errors, batch_id=bid)
    resp.status_code = 202
    return _remember_jobs(resp, *made)


def _run_safely(fn, job, *args):
    """Final safety net: a crashing worker still reports an error to the UI (and to the log)."""
    ev = job_cancel(job)
    if ev is not None and ev.is_set():                  # cancelled while it was waiting in the queue
        return finish_err(job, "Cancelled by user")
    try:
        fn(job, *args)
    except DownloadCancelled as exc:
        finish_err(job, last_line(exc, "Cancelled by user"))
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("Worker crashed for job %s", job.get("id"))
        finish_err(job, f"Unexpected error: {exc}")
    if job["status"] in ("queued", "running"):          # a worker returned without finishing: never leave it hanging
        finish_err(job, "The download ended without a result.")


# ---- download queue: MAX_WORKERS run at once, the rest wait in an order you can change ----
# A queued job can be moved or held; a running job can be paused (stopped now, resumed later -
# yt-dlp continues its .part file and Snapchat/Instagram skip what is already saved).
qcond = threading.Condition()
pending = []                 # job ids waiting, in the order they will start
tasks = {}                   # job id -> (worker fn, args) - kept until the job really ends, so Resume can rerun it


def queue_add(job, fn, args, front=False):
    with qcond:
        tasks[job["id"]] = (fn, args)
        if job["id"] not in pending:
            pending.insert(0, job["id"]) if front else pending.append(job["id"])
        qcond.notify()


def _queue_worker():
    while True:
        with qcond:
            while True:
                with lock:
                    jid = next((i for i in pending if (jobs.get(i) or {}).get("status") == "queued"), None)
                if jid:
                    pending.remove(jid)
                    fn, args = tasks.get(jid, (None, ()))
                    break
                qcond.wait(timeout=30)
        job = jobs.get(jid)
        if not job or fn is None:
            continue
        _run_safely(fn, job, *args)
        if job.get("status") != "paused":
            with qcond:
                tasks.pop(jid, None)


for _n in range(MAX_WORKERS):
    threading.Thread(target=_queue_worker, daemon=True, name=f"dlq{_n}").start()


def queue_view(ids):
    """The jobs you know about (by id), with their place in the line."""
    with qcond, lock:
        order = {jid: n + 1 for n, jid in enumerate(i for i in pending if (jobs.get(i) or {}).get("status") in ("queued", "paused"))}
        out = []
        for i in ids:
            j = jobs.get(i)
            if j and j["status"] in ("queued", "running", "paused"):
                out.append(dict(j, position=order.get(i), can_pause=i in tasks))
    rank = {"running": 0, "queued": 1, "paused": 2}
    return sorted(out, key=lambda j: (rank[j["status"]], j.get("position") or 0, j["created"]))


@app.route("/queue")
def queue_list():
    ids = [i for i in (request.args.get("ids") or "").split(",") if i][:200]
    return jsonify(success=True, jobs=queue_view(ids), slots=MAX_WORKERS)


@app.route("/queue/<job_id>/<action>", methods=["POST"])
def queue_action(job_id, action):
    with lock:
        job = jobs.get(job_id)
    if not job or action not in ("pause", "resume", "up", "down", "top", "cancel"):
        return jsonify(success=False, message="Not found."), 404
    st = job["status"]
    if action == "cancel":
        if st in ("queued", "paused"):                       # not running: end it now instead of waiting for a free slot
            with qcond:
                if job_id in pending:
                    pending.remove(job_id)
                tasks.pop(job_id, None)
            update(job, pausing=False)
            finish_err(job, "Cancelled by user")
        else:
            ev = cancels.get(job_id)
            ev and ev.set()
        return jsonify(success=True)
    if action == "pause":
        if st == "queued":
            update(job, status="paused", message="On hold - press Resume to put it back in line")
        elif st == "running":
            if job_id not in tasks:
                return jsonify(success=False, message="This kind of job cannot be paused - cancel it instead."), 400
            update(job, pausing=True, message="Pausing…")
            ev = cancels.get(job_id)
            ev and ev.set()
        save_jobs()
        return jsonify(success=True)
    if action == "resume":
        if st != "paused":
            return jsonify(success=False, message="It is not paused."), 400
        ev = cancels.get(job_id)
        ev and ev.clear()
        update(job, status="queued", message="Waiting in queue…")
        with qcond:
            if job_id not in pending:
                pending.append(job_id)
            qcond.notify()
        save_jobs()
        return jsonify(success=True)
    with qcond:                                              # up / down / top: only among the jobs still waiting
        if job_id not in pending:
            return jsonify(success=False, message="Only waiting downloads can be moved."), 400
        i = pending.index(job_id)
        pending.pop(i)
        j = 0 if action == "top" else max(0, i - 1) if action == "up" else min(len(pending), i + 1)
        pending.insert(j, job_id)
    return jsonify(success=True)


# --------------------------------------------------------------------------- #
# TikTok LIVE recording
# Method adapted from Michele0303/tiktok-live-recorder (MIT License, (c) 2023 Michele):
# username -> room id -> "is it live?" -> best FLV/HLS stream URL -> save the raw stream -> remux to MP4.
# --------------------------------------------------------------------------- #
try:
    from curl_cffi import requests as _cffi            # looks like a real browser to TikTok (also used by yt-dlp)
except ImportError:                                     # still works without it, but TikTok blocks plain clients more often
    _cffi = None

TT_WEB, TT_CAST, TT_HELPER = "https://www.tiktok.com", "https://webcast.tiktok.com", "https://tikrec.com"
TT_LIVE_RE = re.compile(r"^https?://(?:www\.|m\.)?tiktok\.com/@([A-Za-z0-9._-]{2,24})/live\b", re.I)
TT_USER_RE = re.compile(r"^[A-Za-z0-9._-]{2,24}$")
TT_HEADERS = {"Accept-Language": "en-US", "Referer": "https://www.tiktok.com/", "Origin": "https://www.tiktok.com",
              "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/136.0.0.0 Safari/537.36"}
LIVE_INTERVALS = (2, 5, 10)                             # live watches can check more often than post watches
live_now = {}                                           # lower-case username -> job id of the running recording
live_lock = threading.Lock()
_tt_local = threading.local()


class LiveError(Exception):
    pass


TT_COOKIES_FILE = DATA_DIR / "tiktok_cookies.txt"
TT_SESSION_NAMES = ("sessionid", "sessionid_ss")
_tt_who = {"mtime": None, "user": ""}


def tt_cookies():
    """[(domain, path, name, value, expiry)] from the saved TikTok login, or []."""
    try:
        text = TT_COOKIES_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) == 7 and not line.startswith("#"):
            out.append((f[0], f[2], f[5], f[6], int(f[4] or 0)))
    return out


def _tt_cookie_mtime():
    try:
        return TT_COOKIES_FILE.stat().st_mtime
    except OSError:
        return None


def _tt_session():
    """One browser-like session per thread; it picks up a new/removed TikTok login by itself."""
    if _cffi is None:
        return None
    mt = _tt_cookie_mtime()
    s = getattr(_tt_local, "s", None)
    if s is None or getattr(_tt_local, "mt", "x") != mt:
        s = _tt_local.s = _cffi.Session(impersonate="chrome136")
        s.headers.update(TT_HEADERS)
        for dom, path, name, value, _exp in tt_cookies():
            try:
                s.cookies.set(name, value, domain=dom, path=path or "/")
            except Exception:  # noqa: BLE001
                pass
        _tt_local.mt = mt
    return s


def _tt_cookie_header():
    """Cookie header for the plain (no curl_cffi) fallback."""
    return "; ".join(f"{n}={v}" for _d, _p, n, v, _e in tt_cookies())


def tt_login_user(force=False):
    """The logged-in TikTok username ('' = not logged in / not working). Asked once per saved login."""
    mt = _tt_cookie_mtime()
    if mt is None:
        return ""
    if not force and _tt_who["mtime"] == mt:
        return _tt_who["user"]
    user = ""
    try:
        d = _tt_json(f"{TT_WEB}/passport/web/account/info/", {"aid": 1459})
        data = d.get("data") or {}
        user = str(data.get("username") or data.get("unique_id") or data.get("screen_name") or "")
    except LiveError:
        user = _tt_who["user"] if _tt_who["mtime"] == mt else ""
    _tt_who.update(mtime=mt, user=user)
    return user


def tt_login_status(check=False):
    """What the Options panel shows. Never returns cookie values."""
    ck = tt_cookies()
    sess = [c for c in ck if c[2] in TT_SESSION_NAMES]
    exp = max((c[4] for c in sess), default=0) or None
    st = {"saved": bool(ck), "count": len(ck), "has_session": bool(sess), "expires": exp,
          "session_expired": bool(exp and exp < time.time()), "saved_at": _tt_cookie_mtime(), "user": ""}
    if ck and check:
        st["user"] = tt_login_user()
    return st


def _tt_json(url, params=None, timeout=15):
    """GET a TikTok JSON endpoint; returns {} when TikTok answers with its 'please wait' wall or anything not JSON."""
    if params:
        url += ("&" if "?" in url else "?") + "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    try:
        s = _tt_session()
        if s is not None:
            text = s.get(url, timeout=timeout).text
        else:
            hdr = dict(TT_HEADERS)
            if tt_cookies():
                hdr["Cookie"] = _tt_cookie_header()
            req = urllib.request.Request(url, headers=hdr)
            text = urllib.request.urlopen(req, timeout=timeout).read(4 * 1024 * 1024).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        raise LiveError(f"Could not reach TikTok: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _tt_room_direct(user):
    for attempt in range(3):                             # TikTok sometimes answers with its "please wait" wall: try again
        d = _tt_json(f"{TT_WEB}/api-live/user/room/", {"aid": 1988, "sourceType": 54, "uniqueId": user})
        if d:
            break
        time.sleep(1.5 * (attempt + 1))
    data = d.get("data") or {}
    u, lr = data.get("user") or {}, data.get("liveRoom") or {}
    if not u and d.get("statusCode") not in (None, 0):
        raise LiveError("TikTok account not found." if d.get("statusCode") == 19881007 else "TikTok refused the lookup.")
    return u.get("roomId") or "", lr.get("title") or "", (lr.get("coverUrl") or u.get("avatarLarger") or "")


def _tt_room_helper(user):
    """Fallback used by Michele0303's recorder: tikrec.com signs the lookup URL. It sees the username - can be switched off."""
    try:
        signed = _tt_json(f"{TT_HELPER}/tiktok/room/api/sign", {"unique_id": user}).get("signed_path")
    except LiveError:
        signed = None
    if not signed or not str(signed).startswith("/"):
        return ""
    d = _tt_json(TT_WEB + signed)
    return str(((d.get("data") or {}).get("user") or {}).get("roomId") or "")


def tt_room(user):
    """(room_id, title, cover) for a username. room_id is '' when TikTok gives none."""
    rid, title, cover = "", "", ""
    try:
        rid, title, cover = _tt_room_direct(user)
    except LiveError as exc:
        if "not found" in str(exc) or not settings.get("live_helper", True):
            raise                                        # a missing account is final - never ask the helper about it
    if not rid and settings.get("live_helper", True):
        rid = _tt_room_helper(user)
    return str(rid or ""), title, cover


def tt_room_info(rid):
    return _tt_json(f"{TT_CAST}/webcast/room/info/", {"aid": 1988, "room_id": rid})


def tt_is_live(rid):
    if not rid:
        return False
    alive = _tt_json(f"{TT_CAST}/webcast/room/check_alive/", {"aid": 1988, "region": "CH", "room_ids": rid, "user_is_login": "true"})
    lst = alive.get("data")
    if not (isinstance(lst, list) and lst and isinstance(lst[0], dict) and lst[0].get("alive")):
        return False
    info = tt_room_info(rid)
    if info.get("status_code") == 4003110:               # live but restricted for anonymous viewers: still try
        return True
    room = info.get("data") or {}
    return info.get("status_code", 0) == 0 and str(room.get("status", 2)) == "2"


LIVE_QUALITY_ORDER = {"best": ("origin", "uhd", "hd", "sd", "ld"), "hd": ("hd", "uhd", "origin", "sd", "ld"),
                      "sd": ("sd", "ld", "hd", "uhd", "origin")}


def _quality_label(key, main, names):
    try:
        p = json.loads(main.get("sdk_params") or "{}")
    except ValueError:
        p = {}
    res = str(p.get("resolution") or "")
    h = min((int(x) for x in res.split("x") if x.isdigit()), default=0)
    name = names.get(key) or {"origin": "Original", "uhd": "Ultra HD", "hd": "HD", "sd": "SD", "ld": "Low"}.get(key, key)
    return f"{name} · {h}p" if h else name


def tt_streams(rid):
    """[(url, quality label)] - the quality you chose first (Original by default), FLV before HLS like the original recorder."""
    info = tt_room_info(rid)
    if "This account is private" in json.dumps(info)[:20000]:
        raise LiveError("This account is private.")
    if info.get("status_code") == 4003110:
        raise LiveError("TikTok only shows this live to logged-in or following viewers.")
    room = info.get("data") or {}
    su = room.get("stream_url") or {}
    out = []
    add = lambda u, lab="": u and isinstance(u, str) and all(u != x for x, _ in out) and out.append((u, lab))
    sdk = (su.get("live_core_sdk_data") or {}).get("pull_data") or {}
    try:
        streams = json.loads(sdk.get("stream_data") or "{}").get("data") or {}
    except ValueError:
        streams = {}
    quals = [q for q in (sdk.get("options") or {}).get("qualities") or [] if isinstance(q, dict)]
    level = {q.get("sdk_key"): q.get("level", -1) for q in quals}
    names = {q.get("sdk_key"): str(q.get("name") or "") for q in quals if q.get("name")}
    pref = LIVE_QUALITY_ORDER.get(settings.get("live_quality", "best"), LIVE_QUALITY_ORDER["best"])
    rank = lambda k: (pref.index(k) if k in pref else len(pref), -level.get(k, -1))
    for key in sorted(streams, key=rank):
        main = (streams[key] or {}).get("main") or {}
        lab = _quality_label(key, main, names)
        add(main.get("flv"), lab)
        add(main.get("hls") or main.get("m3u8"), lab)
    flv = su.get("flv_pull_url") or {}
    for key, lab in (("FULL_HD1", "Full HD"), ("HD1", "HD"), ("SD2", "SD"), ("SD1", "SD")):
        add(flv.get(key), lab)
    add(su.get("hls_pull_url"), "HLS")
    return out


def tt_stream_urls(rid):
    return [u for u, _l in tt_streams(rid)]


def tt_live_status(user):
    """What the page and the watchlist need: {'user', 'room_id', 'live', 'title', 'cover'}."""
    rid, title, cover = tt_room(user)
    return {"user": user, "room_id": rid, "live": tt_is_live(rid), "title": title, "cover": cover}


def _stream_to_file(url, fh, job, cancel, started, deadline):
    """Copy one stream URL into the open file until it ends, we are stopped, time is up or the disk is nearly full.
    Returns (bytes written, reason)."""
    req = urllib.request.Request(url, headers=TT_HEADERS)
    n, tick = 0, 0.0
    with urllib.request.urlopen(req, timeout=20) as r:
        while True:
            if cancel.is_set():
                return n, "stopped"
            if time.time() >= deadline:
                return n, "time"
            chunk = r.read(256 * 1024)
            if not chunk:
                return n, "ended"
            fh.write(chunk)
            n += len(chunk)
            now = time.time()
            if now - tick > 2:
                tick = now
                if now - job.get("stats_t", 0) > 45:              # viewers + title, at most once every 45 s
                    job["stats_t"] = now
                    threading.Thread(target=_live_stats, args=(job,), daemon=True).start()
                if shutil.disk_usage(BASE_DIR).free < live_min_free():
                    return n, "disk"
                size = fh.tell()
                el = int(now - started)
                update(job, message=f"🔴 Recording · {el // 3600}:{el // 60 % 60:02d}:{el % 60:02d} · {human(size)}",
                       speed=human(size / max(1, now - started)) + "/s", rec_size=size, rec_secs=el)


def _live_stats(job):
    try:
        room = tt_room_info(job.get("room_id") or "").get("data") or {}
    except Exception:  # noqa: BLE001
        return
    n = room.get("user_count")
    if isinstance(n, int):
        update(job, viewers=n, peak=max(n, job.get("peak") or 0))
    if room.get("title"):
        update(job, live_title=str(room["title"])[:200])


def live_max_minutes():
    m = settings.get("live_max_minutes")
    return int(m) if m else int(float(settings.get("live_max_hours", 6)) * 60)


def live_min_free():
    return float(settings.get("live_min_free_gb", 2)) * 1024 ** 3


def _hms(t):
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}"


def _chat_load(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def _chat_srt(comments, total):
    """Each comment shows for 4 s (players stack overlapping lines, like a chat)."""
    def ts(x):
        x = max(0.0, x)
        return f"{int(x // 3600):02d}:{int(x // 60 % 60):02d}:{int(x % 60):02d},{int(x * 1000 % 1000):03d}"
    rows = []
    for i, e in enumerate(comments, 1):
        a = float(e.get("t") or 0)
        b = min(a + 4, float(total) + 4)
        text = re.sub(r"[\r\n]+", " ", f"{e.get('u') or '?'}: {e.get('x') or ''}")[:200]
        rows.append(f"{i}\n{ts(a)} --> {ts(b)}\n{text}\n")
    return "\n".join(rows)


def _chat_text(chat, user):
    lines = [f"Chat of @{user}'s TikTok LIVE", ""]
    for e in chat:
        k, who, x = e.get("k"), e.get("u") or "?", e.get("x") or ""
        if k == "c":
            lines.append(f"[{_hms(e.get('t') or 0)}] {who}: {x}")
        elif k == "g":
            lines.append(f"[{_hms(e.get('t') or 0)}] 🎁 {who} {x}")
    return "\n".join(lines) + "\n"


def _chat_thread(job, user, rid, started, stop, path):
    """Read the live chat with TikTokLive (optional package) until the recording stops. Reconnects a few times."""
    try:
        import asyncio
        from TikTokLive import TikTokLiveClient
        from TikTokLive.events import CommentEvent, GiftEvent, LikeEvent, JoinEvent
        from TikTokLive.client.web.web_settings import WebDefaults
    except ImportError:
        update(job, chat_state="Chat needs the TikTokLive package - rebuild the Docker image.")
        return
    key = (settings.get("live_chat_key") or "").strip()
    if key:
        WebDefaults.tiktok_sign_api_key = key
    update(job, chat=[], chat_n=0, gifts_n=0, diamonds=0, likes=0, joins=0, chat_state="Connecting to chat…")
    fh = open(path, "a", encoding="utf-8")
    nick = lambda u: (getattr(u, "nickname", "") or getattr(u, "unique_id", "") or "?") if u else "?"

    def emit(kind, who, text, **extra):
        ev = {"t": round(time.time() - started, 1), "k": kind, "u": str(who)[:60], "x": str(text)[:300], **extra}
        try:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            fh.flush()
        except OSError:
            pass
        with lock:
            buf = job.setdefault("chat", [])
            buf.append(ev)
            del buf[:-80]

    async def session():
        client = TikTokLiveClient(unique_id=user)

        @client.on(CommentEvent)
        async def _c(e):
            emit("c", nick(e.user), e.comment)
            with lock:
                job["chat_n"] = job.get("chat_n", 0) + 1

        @client.on(GiftEvent)
        async def _g(e):
            g = e.gift
            if g is None or (g.type == 1 and e.streaking):     # streak gifts: count once, when the streak ends
                return
            n = e.repeat_count or 1
            emit("g", nick(e.user), f"sent {n}× {g.name}", d=(g.diamond_count or 0) * n)
            with lock:
                job["gifts_n"] = job.get("gifts_n", 0) + n
                job["diamonds"] = job.get("diamonds", 0) + (g.diamond_count or 0) * n

        @client.on(LikeEvent)
        async def _l(e):
            if e.total:
                update(job, likes=int(e.total))

        @client.on(JoinEvent)
        async def _j(e):
            with lock:
                job["joins"] = job.get("joins", 0) + 1

        task = await client.start(room_id=int(rid), fetch_live_check=False)
        update(job, chat_state="connected")
        while not stop.is_set() and not task.done():
            await asyncio.sleep(0.5)
        if not task.done():
            await client.disconnect()
        elif task.exception() is not None:
            raise task.exception()

    tries = 0
    try:
        while not stop.is_set() and tries < 5:
            try:
                asyncio.run(session())
                if stop.is_set():
                    break
            except Exception as exc:  # noqa: BLE001
                app.logger.info("Chat for @%s: %s", user, exc)
                update(job, chat_state="Chat unavailable: " + last_line(exc, "could not connect")[:160])
            tries += 1
            if stop.wait(min(60, 5 * tries)):
                break
    finally:
        fh.close()


def el_now(started):
    return int(time.time() - started)


def tiktok_live_worker(job, user, cancel):
    """Record one TikTok LIVE until it ends or you press Stop - stopping always SAVES what was recorded."""
    update(job, status="running", message=f"Looking for @{user}'s live…", progress=0)
    try:
        st = tt_live_status(user)
    except LiveError as exc:
        return finish_err(job, str(exc))
    if not st["room_id"]:
        return finish_err(job, f"Could not find @{user}'s live room (TikTok may be blocking the check - try again in a minute).")
    if not st["live"]:
        return finish_err(job, f"@{user} is not live right now.")
    _need_ffmpeg()
    folder = BASE_DIR / "tiktok"
    out_dir = folder / user / "lives"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{user} live {time.strftime('%Y-%m-%d %H-%M')}"
    raw = out_dir / f"{stem}.flv.part"
    started = time.time()
    deadline = started + live_max_minutes() * 60
    update(job, live=True, rec_started=started, label=f"🔴 @{user} LIVE", message="🔴 Recording…",
           live_user=user, room_id=st["room_id"], live_title=st["title"], viewers=None, peak=0, cover=st["cover"], marks=[])
    chat_log = out_dir / f".{stem}.chat.jsonl"
    chat_stop, chat_thread = threading.Event(), None
    if settings.get("live_chat"):                         # chat is optional: it goes through a signing service
        chat_thread = threading.Thread(target=_chat_thread, args=(job, user, st["room_id"], started, chat_stop, chat_log),
                                       daemon=True, name=f"chat-{user}")
        chat_thread.start()
    reason, fails, total, errmsg = "ended", 0, 0, ""
    with open(raw, "ab") as fh:
        while True:
            try:
                urls = tt_streams(st["room_id"])
            except LiveError as exc:
                errmsg = str(exc)
                break
            got = 0
            for item in urls:
                url, qlab = item if isinstance(item, tuple) else (item, "")
                if qlab and job.get("quality") != qlab:
                    update(job, quality=qlab)
                try:
                    n, reason = _stream_to_file(url, fh, job, cancel, started, deadline)
                except Exception as exc:  # noqa: BLE001   a dropped connection: try the next URL / reconnect below
                    n, reason = 0, f"network: {exc}"
                got += n
                if n >= 4096 or reason in ("stopped", "time", "disk"):
                    break
            total += got
            fh.flush()
            if reason in ("stopped", "time", "disk"):
                break
            fails = 0 if got else fails + 1
            if fails >= 6 or not tt_is_live(st["room_id"]):      # the live is over (or TikTok keeps refusing)
                break
            if cancel.wait(2):
                reason = "stopped"
                break
    chat_stop.set()
    if chat_thread is not None:
        chat_thread.join(timeout=15)
    el = el_now(started)
    keep_min = int(settings.get("live_min_keep", 0) or 0)
    if total < 4096 or (keep_min and el < keep_min * 60 and reason != "stopped"):
        raw.unlink(missing_ok=True)
        chat_log.unlink(missing_ok=True)
        if total >= 4096:
            return finish_err(job, f"@{user}'s live lasted only {el // 60}m {el % 60:02d}s - not kept (shorter than {keep_min} min, see ⚙ Options).")
        return finish_err(job, errmsg or "Could not record anything - TikTok gave no working stream for this live.")
    update(job, message="Saving the recording…", speed="")
    chat = _chat_load(chat_log)
    comments = [e for e in chat if e.get("k") == "c"]
    srt = None
    if comments and settings.get("live_chat_subs", True):   # chat as a subtitle track you can switch on in any player
        srt = out_dir / f".{stem}.chat.srt"
        srt.write_text(_chat_srt(comments, el), encoding="utf-8")
    marks = sorted(m["t"] for m in job.get("marks") or [] if 0 < m["t"] < el_now(started))
    meta_in, meta_map, metafile = [], [], None
    if marks:                                            # your ⭐ moments become chapters you can jump to in any player
        pts = [0] + marks + [int(el_now(started))]
        notes = {m["t"]: m.get("note") for m in job.get("marks") or []}
        lines = [";FFMETADATA1"]
        for i in range(len(pts) - 1):
            title = "Start" if i == 0 else (notes.get(pts[i]) or f"⭐ Moment {i}")
            lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={pts[i] * 1000}", f"END={max(pts[i] + 1, pts[i + 1]) * 1000}",
                      "title=" + re.sub(r"[=;#\\\n]", " ", str(title))[:80]]
        metafile = out_dir / f".{stem}.chapters.txt"
        metafile.write_text("\n".join(lines) + "\n", encoding="utf-8")
        meta_in, meta_map = ["-i", str(metafile)], ["-map_chapters", "1"]
    mp4 = out_dir / f"{stem}.mp4"
    k = 2
    while mp4.exists():
        mp4, k = out_dir / f"{stem} ({k}).mp4", k + 1
    hevc = _vcodec(raw) in ("hevc", "h265")
    base = ["ffmpeg", "-y", "-v", "error", "-f", "flv", "-i", str(raw), *meta_in]
    tail = [*(["-tag:v", "hvc1"] if hevc else []), "-movflags", "+faststart", str(mp4)]
    sub_idx = 1 + (1 if meta_in else 0)
    tries = []
    if srt is not None:
        tries.append(base + ["-i", str(srt), "-map", "0", "-map", f"{sub_idx}:0", *meta_map, "-c", "copy", "-c:s", "mov_text",
                             "-metadata:s:s:0", "title=Live chat", "-metadata:s:s:0", "handler_name=Live chat",
                             "-metadata:s:s:0", "language=und"] + tail)
    tries.append(base + ["-map", "0", *meta_map, "-c", "copy"] + tail)
    saved = None
    for cmd in tries:
        try:
            _ffmpeg(cmd, timeout=3 * 3600)
            saved = mp4
            break
        except Exception:  # noqa: BLE001   try again without the chat track
            app.logger.exception("Live remux failed for @%s", user)
            mp4.unlink(missing_ok=True)
    if saved is None:                                    # could not convert: keep the raw recording rather than lose it
        saved = raw.with_name(f"{stem}.flv")
        raw.replace(saved)
    else:
        raw.unlink(missing_ok=True)
    for tmp in (metafile, srt):
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    if chat:                                             # a readable transcript next to the video
        saved.with_name(saved.stem + ".chat.txt").write_text(_chat_text(chat, user), encoding="utf-8")
    chat_log.unlink(missing_ok=True)
    fs_dirty()
    el = int(time.time() - started)
    moments = f" · {len(marks)} ⭐ moment{'s' if len(marks) != 1 else ''}" if marks else ""
    moments += f" · 💬 {len(comments)} chat message{'s' if len(comments) != 1 else ''}" if chat else ""
    why = {"stopped": "you stopped it", "time": "time limit reached", "disk": "the disk is almost full"}.get(reason, "the live ended")
    rel = saved.relative_to(folder).as_posix()
    took = f"{el // 3600}h {el // 60 % 60:02d}m" if el >= 3600 else f"{el // 60}m {el % 60:02d}s"
    finish_ok(job, [rel], message=f"Saved {took} of @{user}'s live ({human(saved.stat().st_size)}){moments} - {why}.")
    return None


def start_live(user, silent=False):
    """Start recording @user now (in its own thread - a live cannot wait in the download queue). Returns (job, error)."""
    key = user.lower()
    with live_lock:
        jid = live_now.get(key)
        if jid and (jobs.get(jid) or {}).get("status") in ("queued", "running"):
            return jobs[jid], None                         # already recording: hand back the same job
        job = {"id": uuid.uuid4().hex[:12], "category": "tiktok", "label": f"🔴 @{user} LIVE", "status": "queued",
               "progress": 0, "message": "Starting…", "speed": "", "eta": "", "files": [], "fresh": [], "created": time.time(),
               "finished": None, "batch": None, "silent": silent, "live": True}
        cancel = threading.Event()
        with lock:
            jobs[job["id"]], cancels[job["id"]] = job, cancel
        live_now[key] = job["id"]
    with wlock:                                           # remember when they go live -> "usually live around …"
        for w in watches.values():
            if w["kind"] == "tiktok_live" and w["target"].lower() == key:
                w["live_starts"] = (w.get("live_starts") or [])[-39:] + [int(time.time())]
                _save(WATCH_FILE, watches)

    def run():
        try:
            _run_safely(tiktok_live_worker, job, user, cancel)
        finally:
            with live_lock:
                if live_now.get(key) == job["id"]:
                    live_now.pop(key, None)
        if silent and job["status"] == "done" and settings.get("live_notify", True):   # started by the watchlist: say it was saved
            notify(f"⏺ Saved @{user}'s live", str(job.get("message") or "")[:200], ["movie_camera"])
    threading.Thread(target=run, daemon=True, name=f"live-{key}").start()
    return job, None


def _has_tiktoklive():
    if "TikTokLive" in sys.modules:
        return True
    try:
        import importlib.util
        return importlib.util.find_spec("TikTokLive") is not None
    except (ImportError, ValueError):
        return False


def live_user_from(target):
    m = TT_LIVE_RE.match(target or "")
    return m.group(1) if m else None


def _live_rec_view(j):
    return {"id": j["id"], "user": j.get("live_user") or "", "title": j.get("live_title") or "", "started": j.get("rec_started"),
            "secs": j.get("rec_secs", 0), "size": j.get("rec_size", 0), "size_h": human(j.get("rec_size", 0)),
            "speed": j.get("speed", ""), "viewers": j.get("viewers"), "peak": j.get("peak") or 0,
            "status": j["status"], "message": j.get("message", ""), "cover": j.get("cover") or "",
            "quality": j.get("quality") or "", "marks": len(j.get("marks") or []),
            "chat": list(j.get("chat") or [])[-40:], "chat_n": j.get("chat_n", 0), "gifts_n": j.get("gifts_n", 0),
            "diamonds": j.get("diamonds", 0), "likes": j.get("likes", 0), "joins": j.get("joins", 0),
            "chat_state": j.get("chat_state", "")}


def live_habit(starts):
    """'Usually live around 21:00 · Fri, Sat' once we have seen 3+ lives (server's local time)."""
    if len(starts or []) < 3:
        return ""
    lt = [time.localtime(t) for t in starts]
    hours = [0] * 24
    for t in lt:
        for d, wgt in ((-1, 1), (0, 2), (1, 1)):          # smooth: 20:55 and 21:05 count as the same habit
            hours[(t.tm_hour + d) % 24] += wgt
    h = max(range(24), key=lambda i: hours[i])
    days = {}
    for t in lt:
        days[t.tm_wday] = days.get(t.tm_wday, 0) + 1
    top = [d for d, n in sorted(days.items(), key=lambda x: -x[1]) if n >= 2][:3]
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    return f"Usually live around {h:02d}:00" + (" · " + ", ".join(names[d] for d in sorted(top)) if top else "")


@app.route("/live/overview")
def live_overview():
    """Everything the Live page shows except the saved files (those need the file password: /fs/lives)."""
    with live_lock:
        ids = list(live_now.values())
    with lock:
        recs = [_live_rec_view(jobs[i]) for i in ids if i in jobs and jobs[i]["status"] in ("queued", "running")]
    by_user = {r["user"].lower(): r for r in recs}
    with wlock:
        ws = [dict(w) for w in watches.values() if w["kind"] == "tiktok_live"]
    accounts = [{"id": w["id"], "user": w["target"], "enabled": w["enabled"], "interval": w.get("interval", 5),
                 "last_check": w.get("last_check"), "next_check": w.get("next_check"), "busy": w.get("busy", False),
                 "last_status": w.get("last_status", ""), "total": w.get("total_new", 0),
                 "recording": by_user.get(w["target"].lower()), "habit": live_habit(w.get("live_starts")),
                 "lives_seen": len(w.get("live_starts") or [])} for w in sorted(ws, key=lambda w: w["created"])]
    return jsonify(success=True, accounts=accounts, recording=recs, intervals=LIVE_INTERVALS + INTERVALS[:2],
                   quality=settings.get("live_quality", "best"),
                   chat_on=bool(settings.get("live_chat")), login=tt_login_status()["has_session"],
                   helper=settings.get("live_helper", True), max_hours=settings.get("live_max_hours", 6),
                   ready=_cffi is not None, now=time.time())


@app.route("/live/<job_id>/mark", methods=["POST"])
def live_mark(job_id):
    """⭐ Mark this moment of a running recording - it becomes a chapter in the saved MP4."""
    with lock:
        j = jobs.get(job_id)
        if not j or not j.get("live") or j["status"] != "running" or not j.get("rec_started"):
            return jsonify(success=False, message="That recording is not running."), 404
        if len(j.setdefault("marks", [])) >= 200:
            return jsonify(success=False, message="That is a lot of moments - 200 is the limit."), 400
        t = int(time.time() - j["rec_started"])
        note = re.sub(r"\s+", " ", str(json_body().get("note") or "")).strip()[:80]
        j["marks"].append({"t": t, "note": note})
        n = len(j["marks"])
    return jsonify(success=True, t=t, count=n)


@app.route("/fs/lives")
def fs_lives():
    """Saved live recordings, newest first (needs the file password like the rest of /fs/)."""
    root, out = BASE_DIR / "tiktok", []
    if root.is_dir():
        for f in root.glob("*/lives/*"):
            if not f.is_file() or is_partial(f.name) or f.suffix.lower() not in (".mp4", ".flv"):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            rel = fs_rel(f)
            with mlock:
                dur = (media_meta.get(rel) or {}).get("d") or 0
            tr = f.with_name(f.stem + ".chat.txt")
            out.append({"path": rel, "user": f.parent.parent.name, "name": f.name, "size": st.st_size, "size_h": human(st.st_size),
                        "modified": st.st_mtime, "duration": dur, "transcript": fs_rel(tr) if tr.is_file() else ""})
    out.sort(key=lambda x: -x["modified"])
    total = sum(x["size"] for x in out)
    return jsonify(success=True, items=out[:300], count=len(out), total_h=human(total),
                   hours=round(sum(x["duration"] for x in out) / 3600, 1))


@app.route("/tiktok-cookies", methods=["GET", "POST", "DELETE"])
def tiktok_cookies():
    """Status / save / remove the TikTok login (cookies.txt). Cookie values are never sent back."""
    if request.method == "DELETE":
        TT_COOKIES_FILE.unlink(missing_ok=True)
        _tt_who.update(mtime=None, user="")
        return jsonify(success=True, message="TikTok login removed.", **tt_login_status())
    if request.method == "POST":
        up = request.files.get("file")
        text = up.read(COOKIE_MAX_BYTES + 1).decode("utf-8", "replace") if up else json_body().get("text")
        if not isinstance(text, str) or not text.strip():
            return jsonify(success=False, message="Nothing to save - choose the cookies.txt file or paste its text."), 400
        try:
            clean, kept, ignored = parse_cookie_text(text, "tiktok.com", "TikTok")
        except ValueError as exc:
            return jsonify(success=False, message=str(exc)), 400
        try:
            write_private(TT_COOKIES_FILE, clean)
        except OSError as exc:
            return jsonify(success=False, message=f"Could not save the file: {exc.strerror}"), 500
        st = tt_login_status(check=True)
        if not st["has_session"]:
            msg = f"Saved {kept} TikTok cookies, but there is no “sessionid” - you were probably not logged in in that browser."
        elif st["user"]:
            msg = f"Logged in to TikTok as @{st['user']}."
        else:
            msg = f"Saved {kept} TikTok cookies. TikTok did not confirm the account right now - recordings will still use it."
        if ignored:
            msg += f" ({ignored} cookies from other sites were ignored on purpose.)"
        return jsonify(success=True, message=msg, **st)
    return jsonify(success=True, **tt_login_status(check=request.args.get("check") == "1"))


@app.route("/folders")
def folders_list():
    """Your own folders (names only) for the "Save to" picker."""
    root = BASE_DIR / "folders"
    try:
        names = sorted((d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")), key=str.lower)
    except OSError:
        names = []
    return jsonify(success=True, folders=names[:200])


@app.route("/live/status")
def live_status_api():
    user = (request.args.get("user") or "").lstrip("@")
    if not TT_USER_RE.match(user):
        return jsonify(success=False, message="Enter a TikTok username."), 400
    try:
        st = tt_live_status(user)
    except LiveError as exc:
        return jsonify(success=False, message=str(exc)), 502
    with live_lock:
        st["recording"] = live_now.get(user.lower())
    return jsonify(success=True, **st)


@app.route("/job/<job_id>")
def job_status(job_id):
    with lock:
        job = jobs.get(job_id)
        return jsonify(success=True, **job) if job else (jsonify(success=False, message="Job not found."), 404)


@app.route("/jobs")
def recent_jobs():
    """
    /jobs?ids=a,b,c  -> exactly those jobs (what the page uses, so nothing falls out of a 'recent' window)
    /jobs            -> nothing (job ids are private to the browser that started them)
    """
    ids = [i for i in (request.args.get("ids") or "").split(",") if i][:200]
    with lock:
        items = [jobs[i] for i in ids if i in jobs]          # only the jobs you ask for by id - there is no list of everything
        return jsonify(success=True, jobs=items)


@app.route("/zip/<job_id>")
def zip_job(job_id):
    """Bundle everything a job saved into one ZIP (handy for 100+ stories on a phone)."""
    with lock:
        job = jobs.get(job_id)
        names = list(job["files"]) if job else []
        category = job["category"] if job else ""
    if not names:
        return jsonify(success=False, message="Nothing to zip."), 404
    tmp = tempfile.TemporaryFile()                   # real temp file, deleted on close
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:   # media is already compressed
        for name in names:
            p = safe_path(category, name)
            if p:
                z.write(p, arcname=name)
    tmp.seek(0)
    return send_file(tmp, mimetype="application/zip", as_attachment=True,
                     download_name=f"{category}-{job_id}.zip")


@app.route("/zip-multi")
def zip_multi():
    """One ZIP with everything saved by several jobs (batch results)."""
    ids = [i for i in (request.args.get("ids") or "").split(",") if i][:60]
    tmp, count = tempfile.TemporaryFile(), 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for i in ids:
            with lock:
                job = jobs.get(i)
                names, category = (list(job["files"]), job["category"]) if job else ([], "")
            for name in names:
                p = safe_path(category, name)
                if p:
                    z.write(p, arcname=f"{category}/{name}"); count += 1
    if not count:
        return jsonify(success=False, message="Nothing to zip."), 404
    tmp.seek(0)
    return send_file(tmp, mimetype="application/zip", as_attachment=True, download_name="batch.zip")


@app.route("/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id):
    ev = cancels.get(job_id)
    if not ev:
        return jsonify(success=False, message="Job not found."), 404
    ev.set()
    return jsonify(success=True)


@app.route("/list-files")
def list_files():
    """All saved files (newest first) + storage totals for the explorer."""
    wanted = request.args.get("category")
    cats = [wanted] if wanted in CATEGORIES else CATEGORIES
    out = []
    for cat in cats:
        root = BASE_DIR / cat
        for f in root.rglob("*"):
            if f.is_file() and not is_partial(f.name):
                st, rel = f.stat(), f.relative_to(root).as_posix()
                out.append({"category": cat, "name": rel, "size": st.st_size,
                            "size_h": human(st.st_size), "modified": st.st_mtime,
                            "path": f"{cat}/{rel}"})
    out.sort(key=lambda x: x["modified"], reverse=True)
    return jsonify(success=True, files=out, total_h=human(sum(x["size"] for x in out)),
                   free_h=human(shutil.disk_usage(BASE_DIR).free))


@app.route("/files/<category>/<path:name>", methods=["GET", "DELETE"])
def file_item(category, name):
    """GET streams the file (?dl=1 forces download); DELETE removes it."""
    path = safe_path(category, name)
    if not path:
        return jsonify(success=False, message="File not found."), 404
    if request.method == "DELETE":
        try:
            path.unlink()
            parent = path.parent
            if parent != (BASE_DIR / category).resolve() and not any(parent.iterdir()):
                parent.rmdir()                       # remove emptied <username>/ folder
        except OSError as exc:
            return jsonify(success=False, message=f"Could not delete: {exc.strerror}"), 500
        return jsonify(success=True)
    resp = send_from_directory(path.parent, path.name, as_attachment=request.args.get("dl") == "1")
    # saved files are untrusted content: never let one run scripts inside the Hub's origin
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'; media-src 'self'"
    return resp


# --------------------------------------------------------------------------- #
# Optional access lock (set HUB_TOKEN to turn it on)
# --------------------------------------------------------------------------- #
HUB_TOKEN = os.environ.get("HUB_TOKEN", "").strip()      # empty = no lock (same as before)


def _token_ok(v):
    return bool(v) and hmac.compare_digest(str(v), HUB_TOKEN)


@app.before_request
def require_token():
    """If HUB_TOKEN is set, every request needs it. Open the site once with /?token=YOUR_TOKEN - it is then kept in a cookie."""
    if not HUB_TOKEN or request.path.startswith(("/assets/", "/phone/", "/icon/", "/manifest.webmanifest", "/sw.js")):      # /phone/ has its own key; the rest is needed to install the app
        return None
    if _token_ok(request.cookies.get("hub_token")) or _token_ok(request.headers.get("X-Hub-Token")):
        return None
    if _token_ok(request.args.get("token")):
        return _remember_token(redirect(request.path))
    if request.path == "/unlock" and request.method == "POST":           # the token box on the lock page
        ip, now = request.remote_addr or "?", time.time()
        st = _tok_fail.get(ip, [0, 0.0])
        if st[1] > now:
            return _lock_page(f"Too many wrong tries. Wait {int(st[1] - now) + 1} seconds."), 429
        if _token_ok((request.form.get("token") or "").strip()):
            _tok_fail.pop(ip, None)
            return _remember_token(redirect("/"))
        st[0] += 1
        if st[0] >= 5:
            st = [0, now + 60]
        _tok_fail[ip] = st
        time.sleep(1)                                                   # makes guessing slow
        return _lock_page("That token is not right. Copy it again from HUB_TOKEN in your .env file."), 401
    if request.method == "GET" and "text/html" in (request.headers.get("Accept") or ""):
        return _lock_page(), 401                                        # a browser / the home-screen app: show the token box
    return jsonify(success=False, message="Locked. Open the site once with ?token=YOUR_TOKEN."), 401


_tok_fail = {}


def _remember_token(resp):
    resp.set_cookie("hub_token", HUB_TOKEN, httponly=True, samesite="Lax", max_age=60 * 60 * 24 * 365)
    return resp


def _lock_page(error=""):
    """A small page with one box for the access token (works inside the phone home-screen app too)."""
    err = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    return app.response_class(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><meta name="robots" content="noindex">
<title>Media Hub - locked</title><link rel="apple-touch-icon" href="/icon/180.png"><link rel="manifest" href="/manifest.webmanifest">
<style>
:root{{--bg:#090b14;--card:#141729;--text:#eef0fb;--mute:#a0a6c6;--line:rgba(255,255,255,.1);--field:#0b0e1c}}
@media (prefers-color-scheme:light){{:root{{--bg:#f1f4ff;--card:#fff;--text:#14172e;--mute:#535d84;--line:rgba(30,36,90,.14);--field:#fff}}}}
*{{box-sizing:border-box}} body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:16px;background:var(--bg);color:var(--text);
font-family:'Segoe UI',system-ui,-apple-system,Roboto,sans-serif}}
.card{{width:100%;max-width:380px;padding:28px 24px;border:1px solid var(--line);border-radius:24px;background:var(--card);text-align:center;box-shadow:0 30px 80px rgba(0,0,0,.35)}}
img{{width:64px;height:64px;border-radius:16px}} h1{{margin:14px 0 4px;font-size:1.3rem}} p{{margin:0 0 16px;color:var(--mute);font-size:.9rem;line-height:1.45}}
input{{width:100%;padding:13px 14px;border:1px solid var(--line);border-radius:14px;background:var(--field);color:var(--text);font:inherit;font-size:16px}}
input:focus{{outline:none;border-color:#7c5cf0;box-shadow:0 0 0 3px rgba(124,92,240,.25)}}
button{{width:100%;margin-top:12px;padding:13px;border:0;border-radius:14px;background:linear-gradient(135deg,#7c3aed,#2f6fe8);color:#fff;font:inherit;font-weight:700;font-size:1rem;cursor:pointer}}
.err{{margin:0 0 12px;padding:9px 12px;border-radius:12px;background:rgba(251,113,133,.14);color:#fb7185;font-size:.86rem}}
small{{display:block;margin-top:14px;color:var(--mute);font-size:.76rem}}
</style></head><body><main class="card">
<img src="/icon/180.png" alt=""><h1>🔒 Media Hub is locked</h1>
<p>Enter your access token (<b>HUB_TOKEN</b> from the server's <b>.env</b> file). This device remembers it for a year.</p>
{err}<form method="post" action="/unlock" autocomplete="on">
<input type="text" name="username" value="media-hub" autocomplete="username" hidden>
<input type="password" name="token" placeholder="Access token" autocomplete="current-password" autocapitalize="off" spellcheck="false" required autofocus>
<button type="submit">Unlock</button></form>
<small>Each phone, computer and home-screen app asks once.</small>
</main></body></html>""", mimetype="text/html")


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    return resp


@app.errorhandler(Exception)
def handle_error(exc):
    """Always answer JSON. Real crashes are written to the log WITH the traceback."""
    if isinstance(exc, HTTPException):
        if exc.code and exc.code < 400:                  # redirects must stay redirects
            return exc
        return jsonify(success=False, message=exc.description or exc.name), exc.code or 500
    app.logger.exception("Unhandled error on %s %s", request.method, request.path)
    return jsonify(success=False, message="Internal server error (details are in the server log)."), 500


# --------------------------------------------------------------------------- #
# Notifications (ntfy), batches, watchlist + scheduler
# --------------------------------------------------------------------------- #
WATCH_FILE, SETTINGS_FILE = DATA_DIR / "watches.json", DATA_DIR / "settings.json"
ARCHIVE_FILE = DATA_DIR / "archive.txt"          # yt-dlp's "already downloaded" list
WATCH_MAX = 10          # newest N videos looked at on each check
BASELINE_MAX = 100      # existing videos ignored when a watch is created
INTERVALS = (15, 30, 60, 180, 360, 720, 1440)   # minutes: 15 min ... 1 day (each watched account has its OWN)
watches, settings = {}, {"ntfy": ""}
batches, batch_notified = {}, {}
wlock = threading.RLock()


def _load(path, default):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _save(path, data):
    try:
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")           # unique per thread
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except OSError:
        app.logger.warning("Could not write %s", path)


def _ntfy_send(title, message, tags=None):
    """Publish to ntfy. Returns None on success or an error string."""
    target = (settings.get("ntfy") or "").strip()
    if not target:
        return "No ntfy topic set."
    url = target if "://" in target else "https://ntfy.sh/" + target
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return "Invalid ntfy address."
    body = json.dumps({"topic": p.path.strip("/").split("/")[-1] or "media-hub",
                       "title": title, "message": message,
                       "tags": tags or [], "priority": 4}).encode()
    try:
        req = urllib.request.Request(f"{p.scheme}://{p.netloc}", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        return None
    except Exception as exc:  # noqa: BLE001
        return f"Could not reach ntfy: {exc}"


def notify(title, message, tags=None):
    """Fire-and-forget phone notification (no-op when ntfy isn't configured)."""
    if (settings.get("ntfy") or "").strip():
        threading.Thread(target=_ntfy_send, args=(title, message[:400], tags), daemon=True).start()


def check_batch(bid):
    with lock:
        ids = batches.get(bid) or []
        js = [jobs.get(i) for i in ids]
        if not ids or batch_notified.get(bid) or any(j is None or j["status"] not in ("done", "error") for j in js):
            return
        batch_notified[bid] = True


def _after_finish(job, ok):
    """A phone alert when a download YOU started finishes - only if you switched that on (Settings → Notifications).
    Watchlist checks alert on their own; batch items stay quiet so 50 links do not send 50 alerts."""
    if not settings.get("notify_downloads") or job.get("silent") or job.get("batch"):
        return
    label = re.sub(r"^https?://(www\.)?", "", str(job.get("label") or ""))[:50]
    notify(("✅ Done: " if ok else "⚠️ Failed: ") + label, str(job.get("message") or "")[:200], ["white_check_mark" if ok else "warning"])


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def describe_new(w, new):
    """(title, body) for the phone: who posted, how many, and what."""
    low = [n.lower() for n in new]
    vids = [n for n, l in zip(new, low) if l.endswith(VIDEO_EXT)]
    auds = [n for n, l in zip(new, low) if l.endswith(AUDIO_EXT)]
    imgs = [n for n, l in zip(new, low) if l.endswith(IMG_EXT) and not l.endswith((".gif", ".webp"))]
    anim = [n for n, l in zip(new, low) if l.endswith((".gif", ".webp"))]
    if w["kind"] == "channel" and (vids or auds):
        imgs = []                                    # thumbnails saved next to videos
    total = len(vids) + len(auds) + len(imgs) + len(anim) or len(new)
    parts = [p for p in (_plural(len(vids), "video") if vids else "",
                         _plural(len(imgs), "photo") if imgs else "",
                         _plural(len(auds), "audio file") if auds else "",
                         _plural(len(anim), "GIF/sticker") if anim else "") if p]
    title = f"👁 {w['label']} posted {total} new"
    lines = [", ".join(parts) or _plural(len(new), "file")]
    if w["kind"] == "channel":                       # show the real titles
        for n in (vids + auds + imgs + anim)[:4]:
            t = re.sub(r"\s*\[[^\]]+\]$", "", Path(n).stem)
            t = t.split(" - ", 1)[-1] if " - " in t else t
            lines.append("• " + t[:70])
        if total > 4:
            lines.append(f"+ {total - 4} more")
    return title, "\n".join(lines)


def normalize_channel_url(url):
    p = urlparse(url)
    path = p.path.rstrip("/")
    if host_is(p.hostname or "", "youtube.com") and \
            re.match(r"^/(channel/[^/]+|@[^/]+|c/[^/]+|user/[^/]+)$", path):
        return f"{p.scheme}://{p.netloc}{path}/videos"      # the uploads tab only
    return url


def _flat_entries(info, default_key):
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        if e.get("entries"):
            yield from _flat_entries(e, default_key)
        elif e.get("id"):
            yield (e.get("ie_key") or e.get("extractor_key") or default_key or "generic").lower(), e["id"]


def channel_worker(job, w, cancel):
    """
    YouTube channel / TikTok profile watcher.
    First run: record the existing videos in the archive WITHOUT downloading them.
    Later runs: download only videos that are not in the archive (yt-dlp's download_archive).
    The new files are the ones yt-dlp reports for this run - never a folder diff.
    """
    url, folder = w["target"], BASE_DIR / w["category"]
    started = time.time()
    deadline = started + YTDLP_TIMEOUT
    update(job, status="running", message="Checking for new videos…", progress=5)

    if not w.get("baselined"):
        opts = {"extract_flat": "in_playlist", "playlistend": BASELINE_MAX, "quiet": True,
                "no_warnings": True, "socket_timeout": 20, "ignoreerrors": True}
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return finish_err(job, "Could not read this profile (private, blocked, or unsupported).")
        pairs = sorted(set(_flat_entries(info, info.get("extractor_key"))))
        with wlock:
            with open(ARCHIVE_FILE, "a") as f:
                f.writelines(f"{k} {i}\n" for k, i in pairs)
            w["baselined"] = True
        return finish_ok(job, [], f"Now watching - {len(pairs)} existing videos ignored")

    def hook(d):
        if cancel.is_set() or time.time() > deadline:
            raise DownloadCancelled("Stopped")
        if d["status"] == "downloading":
            update(job, message="Downloading new video…", progress=50)

    o = sanitize_options(w.get("options"))
    opts = build_ydl_opts(o, folder, hook)
    opts.update(noplaylist=False, playlistend=WATCH_MAX, download_archive=str(ARCHIVE_FILE),
                ignoreerrors=True)
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadCancelled as exc:
        return finish_err(job, last_line(exc, "Stopped"))
    main, side = job_files(info, folder)
    # a video already on disk but missing from the archive is "recorded", not "new"
    main = fresh_since(folder, main, started)
    side = fresh_since(folder, side, started)
    files = sorted(set(main) | set(side))
    fxnote = None
    if files:
        targets = main if o["kind"] != "image" else [f for f in side if f.lower().endswith(IMG_EXT)]
        out, fxnote = postprocess_media(folder, targets, o, job)
        files = sorted((set(files) - set(targets)) | set(out))
    finish_ok(job, files, fxnote or (f"{len(files)} new video(s)" if files else "Nothing new"), fresh=files)


def run_live_watch(w):
    """A TikTok LIVE watch: a quick 'are they live?' check; when they are, recording starts by itself."""
    user, status, started = w["target"], "", False
    try:
        with live_lock:
            jid = live_now.get(user.lower())
        if jid and (jobs.get(jid) or {}).get("status") in ("queued", "running"):
            status = "🔴 Recording now"
        elif tt_live_status(user)["live"]:
            start_live(user, silent=True)
            started, status = True, "🔴 Went live - recording"
        else:
            status = "Not live"
    except LiveError as exc:
        status = "⚠️ " + str(exc)[:200]
    except Exception:  # noqa: BLE001
        app.logger.exception("Live check crashed for @%s", user)
        status = "⚠️ The live check failed - it will try again."
    finally:
        with wlock:
            w["busy"] = False
            w["last_check"] = time.time()
            w["next_check"] = time.time() + w.get("interval", 5) * 60
            w["last_status"] = status
            w["last_new"] = 1 if started else 0
            w["total_new"] = w.get("total_new", 0) + (1 if started else 0)
            _save(WATCH_FILE, watches)
    if started and settings.get("live_notify", True):
        notify(f"🔴 @{user} is live", "Media Hub started recording it.", ["red_circle"])


def run_watch(wid):
    """One scheduled check of one watched account (runs in the thread pool). Never leaves a watch 'busy'."""
    with wlock:
        w = watches.get(wid)
    if not w:
        return
    if w["kind"] == "tiktok_live":
        return run_live_watch(w)
    new = []
    job = {"status": "error", "message": "The check could not start.", "fresh": []}     # placeholder until the real job exists
    cancel = threading.Event()
    try:
        job = {"id": uuid.uuid4().hex[:12], "category": w["category"], "label": f"👁 {w['label']}",
               "status": "running", "progress": 0, "message": "Checking…", "speed": "", "eta": "",
               "files": [], "fresh": [], "created": time.time(), "finished": None, "silent": True, "batch": None,
               "folder": clean_folder((w.get("options") or {}).get("folder"))}
        with lock:
            jobs[job["id"]], cancels[job["id"]] = job, cancel
        try:
            if w["kind"] == "snapchat":
                snapchat_worker(job, w["target"], cancel, w.get("options"))
            else:
                channel_worker(job, w, cancel)
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("Watch check crashed for %s", w.get("label"))
            finish_err(job, f"Unexpected error: {exc}")
        if job["status"] in ("queued", "running"):
            finish_err(job, "The check ended without a result.")
        new = list(job.get("fresh") or []) if job["status"] == "done" else []
    except Exception:  # noqa: BLE001
        app.logger.exception("Watch bookkeeping failed for %s", w.get("label"))
    finally:
        with wlock:                                       # ALWAYS release the watch, whatever happened above
            w["busy"] = False
            w["last_check"] = time.time()
            w["next_check"] = time.time() + w.get("interval", 60) * 60
            w["total_new"] = w.get("total_new", 0) + len(new)
            w["last_new"] = len(new)
            if job["status"] == "error" and not job["message"].startswith("No new stories"):
                w["last_status"] = "⚠️ " + job["message"][:300]
            else:
                w["last_status"] = f"{len(new)} new" if new else "Nothing new"
            _save(WATCH_FILE, watches)
    if new:
        try:
            if settings.get("digest") == "daily":
                digest_add(w, new)
            else:
                title, body = describe_new(w, new)
                notify(title, body, ["eyes"])
        except Exception:  # noqa: BLE001
            app.logger.exception("Could not build the notification")


# ---- daily digest: one alert a day instead of one per account ----
DIGEST_FILE = DATA_DIR / "digest.json"
dlock = threading.Lock()


def digest_add(w, new):
    low = [n.lower() for n in new]
    kinds = {"video": sum(l.endswith(VIDEO_EXT) for l in low), "photo": sum(l.endswith(IMG_EXT) for l in low)}
    with dlock:
        d = _load(DIGEST_FILE, {})
        e = d.setdefault("pending", {}).setdefault(w["id"], {"label": w["label"], "count": 0, "video": 0, "photo": 0})
        e["label"] = w["label"]
        e["count"] += len(new)
        for k, v in kinds.items():
            e[k] += v
        _save(DIGEST_FILE, d)


def digest_text(pending):
    rows = sorted(pending.values(), key=lambda e: -e["count"])
    total = sum(e["count"] for e in rows)
    title = f"👁 Daily digest: {_plural(total, 'new post')} from {_plural(len(rows), 'account')}"
    lines = []
    for e in rows[:8]:
        bits = [p for p in (_plural(e["video"], "video") if e["video"] else "", _plural(e["photo"], "photo") if e["photo"] else "") if p]
        lines.append(f"• {e['label']}: {e['count']}" + (f" ({', '.join(bits)})" if bits else ""))
    if len(rows) > 8:
        lines.append(f"+ {len(rows) - 8} more accounts")
    return title, "\n".join(lines)


def digest_tick(force=False):
    """Send the digest once a day at the chosen hour (server's local time). Returns what was sent, or None."""
    if settings.get("digest") != "daily" and not force:
        return None
    now = time.localtime()
    today = time.strftime("%Y-%m-%d", now)
    with dlock:
        d = _load(DIGEST_FILE, {})
        due = force or (now.tm_hour >= int(settings.get("digest_hour", 20)) and d.get("sent_on") != today)
        if not due or not d.get("pending"):
            return None
        title, body = digest_text(d["pending"])
        d.update(pending={}, sent_on=today)
        _save(DIGEST_FILE, d)
    notify(title, body, ["eyes"])
    return title


def scheduler_loop():
    while True:
        time.sleep(15)
        try:
            digest_tick()
        except Exception:  # noqa: BLE001
            app.logger.exception("Digest error")
        try:
            now = time.time()
            with wlock:
                due = [w for w in watches.values()
                       if w["enabled"] and not w.get("busy") and w["next_check"] <= now]
                for w in due:
                    w["busy"] = True
            for w in due:
                try:
                    pool.submit(run_watch, w["id"])
                except RuntimeError:                       # the program is shutting down
                    w["busy"] = False
                    return
                except Exception:  # noqa: BLE001
                    w["busy"] = False
                    app.logger.exception("Could not queue the watch check")
        except Exception:  # noqa: BLE001
            app.logger.exception("Scheduler error")


@app.route("/watches", methods=["GET", "POST"])
def watches_api():
    if request.method == "GET":
        with wlock:
            out = sorted((dict(w) for w in watches.values()), key=lambda w: w["created"])
        for w in out:
            if w["kind"] == "tiktok_live":
                with live_lock:
                    j = jobs.get(live_now.get(w["target"].lower()) or "")
                if j and j["status"] in ("queued", "running"):
                    w["rec"] = {"id": j["id"], "message": j.get("message", ""), "secs": j.get("rec_secs", 0),
                                "size_h": human(j.get("rec_size", 0))}
        return jsonify(success=True, intervals=INTERVALS, live_intervals=LIVE_INTERVALS + INTERVALS[:2], watches=out)
    d = json_body()
    kind, target = d.get("kind"), str(d.get("target") or "").strip()
    interval = d.get("interval") if d.get("interval") in INTERVALS else 60
    if kind == "tiktok_live":
        target = live_user_from(target) or target.lstrip("@")
        if not TT_USER_RE.match(target):
            return jsonify(success=False, message="Enter a TikTok username or a tiktok.com/@user/live link."), 400
        key, label, category = f"tiktok_live:{target.lower()}", f"🔴 @{target}", "tiktok"
        interval = d.get("interval") if d.get("interval") in LIVE_INTERVALS + INTERVALS else 5
    elif kind == "snapchat":
        target = target.lstrip("@")
        if not SNAP_USER_RE.match(target):
            return jsonify(success=False, message="Invalid Snapchat username."), 400
        key, label, category = f"snapchat:{target.lower()}", f"@{target}", "snapchat"
    elif kind == "channel":
        if not valid_http_url(target):
            return jsonify(success=False, message="Invalid profile/channel link."), 400
        if is_instagram(target):
            return jsonify(success=False, message="Watching Instagram accounts is not supported yet - Instagram "
                                                  "is strict about automatic checks and could lock your account. "
                                                  "Download Instagram posts by hand for now."), 400
        target = normalize_channel_url(target)
        key, category = target, detect_category(target)
        label = (str(d.get("label") or "") or target.rstrip("/").split("/")[-2 if target.endswith("/videos") else -1])[:60]
    else:
        return jsonify(success=False, message="Unknown watch type."), 400
    with wlock:
        if any(w["key"] == key for w in watches.values()):
            return jsonify(success=False, message="You are already watching this."), 409
        w = {"id": uuid.uuid4().hex[:10], "key": key, "kind": kind, "target": target, "label": label,
             "category": category, "interval": interval, "enabled": True, "created": time.time(),
             "next_check": time.time(), "last_check": None, "last_status": "Waiting for first check…",
             "last_new": 0, "total_new": 0, "baselined": kind == "snapchat", "busy": False,
             "options": dict(sanitize_options(d.get("options")), name="")}
        watches[w["id"]] = w
        _save(WATCH_FILE, watches)
    return jsonify(success=True, watch=w), 201


@app.route("/watches/<wid>", methods=["DELETE"])
def watch_delete(wid):
    with wlock:
        ok = watches.pop(wid, None) is not None
        _save(WATCH_FILE, watches)
    return jsonify(success=ok), (200 if ok else 404)


@app.route("/watches/<wid>/<action>", methods=["POST"])
def watch_action(wid, action):
    with wlock:
        w = watches.get(wid)
        if not w or action not in ("toggle", "check", "interval"):
            return jsonify(success=False, message="Not found."), 404
        if action == "interval":                             # every account has its own timer
            try:
                iv = int(json_body().get("interval"))
            except (TypeError, ValueError):
                iv = None
            if iv not in (LIVE_INTERVALS + INTERVALS if w["kind"] == "tiktok_live" else INTERVALS):
                return jsonify(success=False, message="Choose one of: " + ", ".join(map(str, INTERVALS)) + " minutes."), 400
            w["interval"] = iv
            if w.get("last_check"):                          # count from the last check (due now if that is already past)
                w["next_check"] = w["last_check"] + iv * 60
        elif action == "toggle":
            w["enabled"] = not w["enabled"]
        else:
            w["next_check"], w["enabled"] = 0, True          # picked up within ~15s
        _save(WATCH_FILE, watches)
    return jsonify(success=True)


@app.route("/settings", methods=["GET", "POST"])
def settings_api():
    if request.method == "POST":
        d = json_body()
        if "ntfy" in d:
            settings["ntfy"] = str(d["ntfy"] or "").strip()[:200]
        if "creator_folders" in d:
            settings["creator_folders"] = bool(d["creator_folders"])
        if "ig_organize" in d:
            settings["ig_organize"] = bool(d["ig_organize"])
        if d.get("auto_name") in ("date", "keep"):
            settings["auto_name"] = d["auto_name"]
        if "notify_downloads" in d:
            settings["notify_downloads"] = bool(d["notify_downloads"])
        if "prefer_h264" in d:
            settings["prefer_h264"] = bool(d["prefer_h264"])
        if "name_template" in d:
            settings["name_template"] = clean_template(d["name_template"])
        if d.get("speed_limit") in ("0", "500K", "1M", "2M", "5M", "10M"):
            settings["speed_limit"] = d["speed_limit"]
        if "parallel_parts" in d:
            try:
                settings["parallel_parts"] = max(1, min(8, int(d["parallel_parts"])))
            except (TypeError, ValueError):
                return jsonify(success=False, message="Parallel parts must be a number from 1 to 8."), 400
        if d.get("pace") in ("off", "balanced", "careful"):
            settings["pace"] = d["pace"]
        if "retry_count" in d:
            try:
                settings["retry_count"] = max(0, min(10, int(d["retry_count"])))
            except (TypeError, ValueError):
                return jsonify(success=False, message="Retries must be a number from 0 to 10."), 400
        if "clean_urls" in d:
            settings["clean_urls"] = bool(d["clean_urls"])
        if "live_helper" in d:
            settings["live_helper"] = bool(d["live_helper"])
        for k in ("live_chat", "live_chat_subs", "live_notify"):
            if k in d:
                settings[k] = bool(d[k])
        if "live_chat_key" in d:
            settings["live_chat_key"] = str(d["live_chat_key"] or "").strip()[:200]
        if "live_max_minutes" in d:
            try:
                settings["live_max_minutes"] = max(5, min(1440, int(d["live_max_minutes"])))
            except (TypeError, ValueError):
                return jsonify(success=False, message="Max length must be 5-1440 minutes."), 400
        if "live_min_free_gb" in d:
            try:
                settings["live_min_free_gb"] = max(1, min(100, int(d["live_min_free_gb"])))
            except (TypeError, ValueError):
                return jsonify(success=False, message="Free space must be 1-100 GB."), 400
        if "live_min_keep" in d:
            try:
                settings["live_min_keep"] = max(0, min(60, int(d["live_min_keep"])))
            except (TypeError, ValueError):
                return jsonify(success=False, message="Minimum length must be 0-60 minutes."), 400
        if d.get("live_quality") in LIVE_QUALITY_ORDER:
            settings["live_quality"] = d["live_quality"]
        if "live_max_hours" in d:
            try:
                settings["live_max_hours"] = max(1, min(24, int(d["live_max_hours"])))
            except (TypeError, ValueError):
                return jsonify(success=False, message="Live time limit must be 1-24 hours."), 400
        if d.get("digest") in ("off", "daily"):
            settings["digest"] = d["digest"]
            if d["digest"] == "off":                      # going back to instant alerts: don't keep a stale backlog
                with dlock:
                    _save(DIGEST_FILE, {"pending": {}})
        if "digest_hour" in d:
            try:
                settings["digest_hour"] = max(0, min(23, int(d["digest_hour"])))
            except (TypeError, ValueError):
                return jsonify(success=False, message="Digest hour must be 0-23."), 400
        if "ig_cap" in d:
            try:
                settings["ig_cap"] = max(1, min(500, int(d["ig_cap"])))
            except (TypeError, ValueError):
                return jsonify(success=False, message="The profile limit must be a number (1-500)."), 400
        _save(SETTINGS_FILE, settings)
    return jsonify(success=True, ntfy=settings.get("ntfy", ""),
                   creator_folders=settings.get("creator_folders", True), ig_organize=settings.get("ig_organize", True), auto_name=settings.get("auto_name", "date"), prefer_h264=settings.get("prefer_h264", True),
                   notify_downloads=settings.get("notify_downloads", False),
                   name_template=settings.get("name_template", ""), speed_limit=settings.get("speed_limit", "0"),
                   parallel_parts=settings.get("parallel_parts", 1), pace=settings.get("pace", "off"),
                   retry_count=settings.get("retry_count", 3), clean_urls=settings.get("clean_urls", True),
                   live_helper=settings.get("live_helper", True), live_max_hours=settings.get("live_max_hours", 6),
                   live_ready=_cffi is not None, live_quality=settings.get("live_quality", "best"),
                   live_chat=settings.get("live_chat", False), live_chat_subs=settings.get("live_chat_subs", True),
                   live_chat_key_set=bool(settings.get("live_chat_key")), live_notify=settings.get("live_notify", True),
                   live_max_minutes=live_max_minutes(), live_min_free_gb=settings.get("live_min_free_gb", 2),
                   live_min_keep=settings.get("live_min_keep", 0), live_chat_ready=_has_tiktoklive(),
                   digest=settings.get("digest", "off"), digest_hour=settings.get("digest_hour", 20),
                   digest_waiting=sum(e.get("count", 0) for e in _load(DIGEST_FILE, {}).get("pending", {}).values()),
                   ig_cap=ig_cap())


@app.route("/digest/send", methods=["POST"])
def digest_send_now():
    if not (settings.get("ntfy") or "").strip():
        return jsonify(success=False, message="Set an ntfy topic first."), 400
    sent = digest_tick(force=True)
    return jsonify(success=True, message=sent or "Nothing new is waiting for the digest.")


# ---- Instagram login card ------------------------------------------------------
@app.route("/instagram-cookies", methods=["GET", "POST", "DELETE"])
def instagram_cookies():
    """Status / save / remove the Instagram cookies.txt. Cookie values are never sent back."""
    if request.method == "DELETE":
        COOKIES_FILE.unlink(missing_ok=True)
        return jsonify(success=True, message="Instagram login removed.", **cookie_status())
    if request.method == "POST":
        text = None
        up = request.files.get("file")
        if up:
            text = up.read(COOKIE_MAX_BYTES + 1).decode("utf-8", "replace")
        else:
            text = json_body().get("text")
        if not isinstance(text, str) or not text.strip():
            return jsonify(success=False, message="Nothing to save - choose the cookies.txt file or paste its text."), 400
        try:
            clean, kept, ignored = parse_cookie_text(text)
        except ValueError as exc:
            return jsonify(success=False, message=str(exc)), 400
        try:
            write_private(COOKIES_FILE, clean)
        except OSError as exc:
            return jsonify(success=False, message=f"Could not save the file: {exc.strerror}"), 500
        st = cookie_status()
        msg = f"Saved {kept} Instagram cookies."
        if ignored:
            msg += f" ({ignored} cookies from other sites were ignored on purpose.)"
        if not st["has_session"]:
            msg += " Warning: no “sessionid” cookie - you are probably not logged in in that browser."
        return jsonify(success=True, message=msg, **st)
    return jsonify(success=True, gallery_dl=bool(shutil.which("gallery-dl")), **cookie_status())


# ---- maintenance: versions, one-click yt-dlp update, restart -----------------
def _ytdlp_loaded_version():
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:  # noqa: BLE001
        return "unknown"


def _pip(args):
    return subprocess.run([sys.executable, "-m", "pip", *args], capture_output=True, text=True, timeout=300)


def _fresh_ytdlp_version():
    """Version on disk right now (the running process keeps the old one until restart)."""
    r = subprocess.run([sys.executable, "-c", "import yt_dlp.version as v;print(v.__version__)"],
                       capture_output=True, text=True, timeout=60)
    return r.stdout.strip() or "unknown"


def do_update_ytdlp():
    loaded = _ytdlp_loaded_version()
    cmd = ["install", "-U", "--disable-pip-version-check", "yt-dlp"]
    try:
        r = _pip(cmd)
        if r.returncode != 0 and "externally-managed" in (r.stderr + r.stdout):
            r = _pip(cmd + ["--break-system-packages"])     # Ubuntu 23.04+ system Python
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"success": False, "message": f"Update failed: {exc}"}
    if r.returncode != 0:
        msg = (r.stderr.strip().splitlines() or r.stdout.strip().splitlines() or ["pip failed"])[-1]
        return {"success": False, "message": f"Update failed: {msg[:250]}"}
    fresh = _fresh_ytdlp_version()
    return {"success": True, "old": loaded, "new": fresh, "needs_restart": fresh != loaded,
            "message": "Already on the latest version." if fresh == loaded and "already satisfied" in r.stdout.lower()
            else f"Updated {loaded} → {fresh}. Restart the app to use it."}


@app.route("/version")
def version_info():
    return jsonify(success=True, ytdlp=_ytdlp_loaded_version(), ffmpeg=bool(shutil.which("ffmpeg")),
                   snapchat_dl=bool(shutil.which("snapchat-dl")), gallery_dl=bool(shutil.which("gallery-dl")))


@app.route("/update-ytdlp", methods=["POST"])
def update_ytdlp():
    return jsonify(do_update_ytdlp())


@app.route("/restart", methods=["POST"])
def restart_app():
    """Restart this process (gunicorn: reload via SIGHUP; plain `python app.py`: re-exec)."""
    def _do():
        time.sleep(1)
        if os.environ.get("SERVER_SOFTWARE", "").startswith("gunicorn"):
            os.kill(os.getppid(), signal.SIGHUP)
        else:
            os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=_do, daemon=True).start()
    return jsonify(success=True, message="Restarting… running downloads will be stopped.")


@app.route("/settings/test", methods=["POST"])
def settings_test():
    err = _ntfy_send("🔔 Media Hub", "Watchlist alerts will look like this.\n👁 @example posted 3 new\n2 videos, 1 photo", ["eyes"])
    return jsonify(success=err is None, message=err or "Sent - check your phone.")


# --------------------------------------------------------------------------- #
# File explorer: browse / search / tags / rename / move / delete / zip / thumbnails
# --------------------------------------------------------------------------- #
TAGS_FILE = DATA_DIR / "tags.json"
THUMB_DIR = DATA_DIR / "thumbs"
THUMB_DIR.mkdir(parents=True, exist_ok=True)
TYPE_FOLDERS = {"posts", "reels", "stories", "highlights", "tagged", "profile-picture"}
KIND_EXT = {"video": VIDEO_EXT, "photo": IMG_EXT, "audio": AUDIO_EXT}
manual_tags = {}                       # "category/folder/file" -> ["tag", ...]  (tags YOU added; automatic ones are computed)
tlock = threading.RLock()
_idx = {"t": 0.0, "files": []}         # cached list of every saved file (refreshed every 10 s or after a change)
_thumb_sem = threading.Semaphore(3)    # at most 3 thumbnails are generated at once


def _tagname(s):
    s = re.sub(r"\s+", "-", str(s or "").strip().lower())
    return re.sub(r"[^\w@.\-]", "", s, flags=re.UNICODE)[:30].strip("-")


def file_kind(name):
    low = name.lower()
    for kind, exts in KIND_EXT.items():
        if low.endswith(exts):
            return kind
    return "other"


def fs_rel(p):
    return "" if p == BASE_DIR else p.relative_to(BASE_DIR).as_posix()


def fs_abs(rel):
    """Virtual path ('instagram/user/…') -> absolute path inside the downloads folder, or None if it is not allowed."""
    parts = [x for x in str(rel or "").strip("/").split("/") if x]
    if any(x in (".", "..") or "\x00" in x for x in parts) or (parts and parts[0] not in CATEGORIES):
        return None
    try:
        p = BASE_DIR.joinpath(*parts).resolve()
        p.relative_to(BASE_DIR)
    except (ValueError, OSError):
        return None
    return p


def auto_tags(rel, is_dir=False):
    """Tags every file gets by itself, from where it is saved: site, @user, type, month, video/photo/audio."""
    parts = rel.split("/")
    t = set()
    if parts[0] in CATEGORIES:
        t.add(parts[0])
    if parts[0] == "folders":
        if len(parts) >= 3 or (is_dir and len(parts) >= 2):
            t.add(_tagname(parts[1]))
    elif len(parts) >= 3 or (is_dir and len(parts) >= 2):
        u = _tagname(parts[1]).lstrip("@")
        if u:
            t.add("@" + u)
    for seg in parts[2:]:
        if seg in TYPE_FOLDERS:
            t.add(seg)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", seg):
            t.add(seg[:7])
    if not is_dir and file_kind(parts[-1]) != "other":
        t.add(file_kind(parts[-1]))
        t.update(smart_tags(rel))
    return t


# ---- smart tags: read from the media itself (ffprobe), cached in meta.json ----
META_FILE = DATA_DIR / "meta.json"
SMART_TAGS = ("vertical", "horizontal", "square", "4k", "hd", "short", "long")
media_meta = {}                        # "category/…/file" -> {"s": size, "m": mtime, "w": px, "h": px, "d": seconds}
mlock = threading.Lock()


def smart_tags(rel):
    with mlock:
        m = media_meta.get(rel)
    if not m:
        return set()
    t, w, h, d = set(), m.get("w") or 0, m.get("h") or 0, m.get("d") or 0
    if w and h:
        r = w / h
        t.add("square" if 0.9 <= r <= 1.1 else "vertical" if r < 1 else "horizontal")
        if min(w, h) >= 2000:
            t.add("4k")
        elif min(w, h) >= 720:
            t.add("hd")
    if d and file_kind(rel) in ("video", "audio"):
        if d < 60:
            t.add("short")
        elif d >= 20 * 60:
            t.add("long")
    return t


def _probe(path):
    ff = shutil.which("ffprobe")
    if not ff:
        return None
    try:
        r = subprocess.run([ff, "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                            "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    try:
        dur = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0
    return {"w": int(v.get("width") or 0), "h": int(v.get("height") or 0), "d": round(dur, 1)}


def meta_loop():
    """Quietly probe new files in the background (a few per pass) so tags appear without slowing anything down."""
    with mlock:
        media_meta.update(_load(META_FILE, {}))
    while True:
        time.sleep(20)
        if not shutil.which("ffprobe"):
            continue
        try:
            files = fs_index()
            live = {rel for rel, _s, _m in files}
            changed, done = False, 0
            for rel, size, mt in files:
                if file_kind(rel) == "other":
                    continue
                with mlock:
                    m = media_meta.get(rel)
                if m and m.get("s") == size and m.get("m") == int(mt):
                    continue
                info = _probe(BASE_DIR / rel)
                if info is None:
                    break
                with mlock:
                    media_meta[rel] = dict(info, s=size, m=int(mt))
                changed, done = True, done + 1
                if done >= 40:
                    break
            with mlock:
                for gone in [k for k in media_meta if k not in live]:      # forget deleted/moved files
                    media_meta.pop(gone)
                    changed = True
                snap = dict(media_meta) if changed else None
            if snap is not None:
                _save(META_FILE, snap)
        except Exception:  # noqa: BLE001
            app.logger.exception("Media probe pass failed")


def manual_for(rel):
    """Your own tags on this path AND on every folder above it (tag a user folder = tag everything inside)."""
    out, parts = set(), rel.split("/")
    with tlock:
        for i in range(1, len(parts) + 1):
            out.update(manual_tags.get("/".join(parts[:i]), ()))
    return out


def tags_of(rel, is_dir=False):
    return sorted(auto_tags(rel, is_dir) | manual_for(rel))


def tag_group(t):
    if t.startswith("@"):
        return "users"
    if t in CATEGORIES:
        return "sites"
    if t in ("video", "photo", "audio") or t in TYPE_FOLDERS:
        return "types"
    if t in SMART_TAGS:
        return "smart"
    return "dates" if re.fullmatch(r"\d{4}-\d{2}", t) else "custom"


def _item(rel, size, mtime, isd, count=None):
    name = rel.rsplit("/", 1)[-1]
    with tlock:
        direct = list(manual_tags.get(rel, ()))
    it = {"name": name, "path": rel, "dir": isd, "modified": mtime, "tags": tags_of(rel, isd), "mtags": direct}
    if isd:
        it["count"] = count
    else:
        it.update(size=size, size_h=human(size), kind=file_kind(name))
    return it


def fs_index():
    now = time.time()
    if now - _idx["t"] < 10:
        return _idx["files"]
    out = []
    for cat in CATEGORIES:
        for dp, _dn, fns in os.walk(BASE_DIR / cat):
            for fn in fns:
                if fn.startswith(".") or is_partial(fn):
                    continue
                fp = os.path.join(dp, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                out.append((Path(fp).relative_to(BASE_DIR).as_posix(), st.st_size, st.st_mtime))
    _idx.update(t=now, files=out)
    return out


def fs_dirty():
    _idx["t"] = 0.0


def _retag(old, new=None):
    """Keep your tags attached when something is renamed/moved (new) or forget them when deleted (None)."""
    with tlock:
        for k in [k for k in manual_tags if k == old or k.startswith(old + "/")]:
            v = manual_tags.pop(k)
            if new is not None:
                manual_tags[new + k[len(old):]] = v
        _save(TAGS_FILE, manual_tags)


def _name_ok(name):
    return bool(name) and name not in (".", "..") and not re.search(r"[/\\\x00]", name) and len(name) <= 200


@app.route("/fs/list")
def fs_list():
    p = fs_abs(request.args.get("path"))
    if p is None or not p.is_dir():
        return jsonify(success=False, message="Folder not found."), 404
    dirs_only, base, items = request.args.get("dirs") == "1", fs_rel(p), []
    try:
        with os.scandir(p) as scan:
            for e in scan:
                if e.name.startswith(".") or is_partial(e.name) or (p == BASE_DIR and e.name not in CATEGORIES):
                    continue
                try:
                    isd, st = e.is_dir(follow_symlinks=False), e.stat(follow_symlinks=False)
                    if not isd and not e.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if dirs_only and not isd:
                    continue
                count = None
                if isd:
                    try:
                        count = sum(1 for n in os.listdir(e.path) if not n.startswith(".") and not is_partial(n))
                    except OSError:
                        pass
                items.append(_item((base + "/" if base else "") + e.name, st.st_size, st.st_mtime, isd, count))
    except OSError:
        return jsonify(success=False, message="Could not read that folder."), 500
    return jsonify(success=True, path=base, items=items)


@app.route("/fs/search")
def fs_search():
    words = (request.args.get("q") or "").lower().split()
    want = [x for x in (_tagname(t) for t in request.args.getlist("tag")) if x]
    recent = request.args.get("recent") == "1"
    groups = {}
    for tg_ in want:
        groups.setdefault(tag_group(tg_), []).append(tg_)
    if not (words or want or recent):
        return jsonify(success=True, items=[])
    out = []
    for rel, size, mt in sorted(fs_index(), key=lambda f: f[2], reverse=True):
        tg = tags_of(rel)
        if groups and not all(any(t in tg for t in ts) for ts in groups.values()):
            continue          # inside one group (tiktok + youtube) = ANY of them; different groups must all match
        if words:
            hay = rel.lower() + " " + " ".join(tg)
            if not all(w in hay for w in words):
                continue
        out.append(_item(rel, size, mt, False))
        if len(out) >= (200 if recent else 600):
            break
    return jsonify(success=True, items=out)


@app.route("/fs/tags")
def fs_tags():
    counts, total = {}, 0
    for rel, size, _mt in fs_index():
        total += size
        for t in tags_of(rel):
            counts[t] = counts.get(t, 0) + 1
    tags = sorted(({"name": k, "count": v, "group": tag_group(k)} for k, v in counts.items()),
                  key=lambda x: (-x["count"], x["name"]))
    return jsonify(success=True, tags=tags, files=len(fs_index()), total_h=human(total),
                   free_h=human(shutil.disk_usage(BASE_DIR).free))


# ---- duplicates, storage dashboard, cleanup ----
_hash_cache = {}                       # (rel, size, mtime) -> quick content hash
_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{6,})\]$")
SIDECAR_EXT = (".description", ".info.json", ".live_chat.json")
PARTIAL_MIN_AGE = 3 * 3600             # leftovers older than this are not an active download any more


def _quick_hash(path, size):
    """Size + first, middle and last MB: identical for identical files, fast even for 4K videos."""
    h = hashlib.blake2b(str(size).encode(), digest_size=16)
    with open(path, "rb") as f:
        if size <= 3 << 20:
            h.update(f.read())
        else:
            for pos in (0, size // 2, size - (1 << 20)):
                f.seek(pos)
                h.update(f.read(1 << 20))
    return h.hexdigest()


def find_duplicates():
    """Groups of the same media saved more than once. Oldest copy first (that is the one cleanup keeps)."""
    by_size, by_id = {}, {}
    for rel, size, mt in fs_index():
        kind = file_kind(rel)
        if kind == "other" or size < 1024:
            continue
        by_size.setdefault(size, []).append((rel, size, mt))
        m = _ID_RE.search(Path(rel).stem)
        if m and kind in ("video", "audio"):
            by_id.setdefault((m.group(1), kind), []).append((rel, size, mt))
    groups, seen = [], set()
    for same in by_size.values():
        if len(same) < 2:
            continue
        by_hash = {}
        for rel, size, mt in same:
            key = (rel, size, int(mt))
            if key not in _hash_cache:
                try:
                    _hash_cache[key] = _quick_hash(BASE_DIR / rel, size)
                except OSError:
                    continue
            by_hash.setdefault(_hash_cache[key], []).append((rel, size, mt))
        for g in by_hash.values():
            if len(g) > 1:
                groups.append(("identical", g))
                seen.update(r for r, _s, _m in g)
    for (vid, _k), g in by_id.items():                 # same video id, different file (e.g. 720p and 1080p)
        g = [x for x in g if x[0] not in seen]
        if len(g) > 1:
            groups.append(("same video", g))
    out = []
    for reason, g in groups:
        g = sorted(g, key=lambda x: x[2])
        wasted = sum(s for _r, s, _m in g[1:])
        out.append({"reason": reason, "wasted": wasted, "wasted_h": human(wasted),
                    "items": [_item(r, s, m, False) for r, s, m in g]})
    return sorted(out, key=lambda x: -x["wasted"])


def _leftovers():
    """Files nobody needs: unfinished downloads (old .part files) and yt-dlp's info/description side files."""
    now, parts, sides = time.time(), [], []
    for cat in CATEGORIES:
        for dp, _dn, fns in os.walk(BASE_DIR / cat):
            for fn in fns:
                fp = os.path.join(dp, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                rel = Path(fp).relative_to(BASE_DIR).as_posix()
                if is_partial(fn) and now - st.st_mtime > PARTIAL_MIN_AGE:
                    parts.append((rel, st.st_size))
                elif fn.lower().endswith(SIDECAR_EXT):
                    sides.append((rel, st.st_size))
    return parts, sides


@app.route("/fs/dupes")
def fs_dupes():
    groups = find_duplicates()
    wasted = sum(g["wasted"] for g in groups)
    return jsonify(success=True, groups=groups, wasted_h=human(wasted), extra=sum(len(g["items"]) - 1 for g in groups))


@app.route("/fs/storage")
def fs_storage():
    sites, users, kinds, months, total = {}, {}, {}, {}, 0
    files = fs_index()
    for rel, size, mt in files:
        total += size
        parts = rel.split("/")
        sites[parts[0]] = sites.get(parts[0], 0) + size
        if len(parts) >= 3:
            k = parts[0] + "/" + parts[1]
            users[k] = users.get(k, 0) + size
        kd = file_kind(rel)
        kinds[kd] = kinds.get(kd, 0) + size
        mo = time.strftime("%Y-%m", time.localtime(mt))
        months[mo] = months.get(mo, 0) + size
    du = shutil.disk_usage(BASE_DIR)
    parts, sides = _leftovers()
    ident = [g for g in find_duplicates() if g["reason"] == "identical"]       # what "Clean up" removes
    rows = lambda d, n=None: [{"name": k, "size": v, "size_h": human(v)} for k, v in sorted(d.items(), key=lambda x: -x[1])[:n]]
    return jsonify(success=True, total=total, total_h=human(total), files=len(files),
                   disk={"total": du.total, "used": du.used, "free": du.free, "total_h": human(du.total), "free_h": human(du.free)},
                   sites=rows(sites), users=rows(users, 12), kinds=rows(kinds),
                   months=[{"name": k, "size": v, "size_h": human(v)} for k, v in sorted(months.items())[-12:]],
                   cleanup={"partials": {"count": len(parts), "size_h": human(sum(s for _r, s in parts))},
                            "sidecars": {"count": len(sides), "size_h": human(sum(s for _r, s in sides))},
                            "dupes": {"count": sum(len(g["items"]) - 1 for g in ident), "size_h": human(sum(g["wasted"] for g in ident))}})


@app.route("/fs/cleanup", methods=["POST"])
def fs_cleanup():
    what = json_body().get("what")
    if what == "partials":
        rels = [r for r, _s in _leftovers()[0]]
    elif what == "sidecars":
        rels = [r for r, _s in _leftovers()[1]]
    elif what == "dupes":                                  # identical copies only - "same video" groups need you to choose
        rels = [it["path"] for g in find_duplicates() if g["reason"] == "identical" for it in g["items"][1:]]
    else:
        return jsonify(success=False, message="Unknown cleanup."), 400
    done, freed = 0, 0
    for rel in rels:
        ap = fs_abs(rel)
        if ap is None or not ap.is_file():
            continue
        try:
            size = ap.stat().st_size
            ap.unlink()
        except OSError:
            continue
        _retag(rel, None)
        done, freed = done + 1, freed + size
    fs_dirty()
    return jsonify(success=True, deleted=done, freed_h=human(freed))


def _paths(d, key="paths"):
    v = d.get(key)
    return [x for x in (v if isinstance(v, list) else [])[:500] if isinstance(x, str)]


@app.route("/fs/tag", methods=["POST"])
def fs_tag():
    d = json_body()
    add = {x for x in (_tagname(t) for t in (d.get("add") or [])[:30]) if x}
    rem = {x for x in (_tagname(t) for t in (d.get("remove") or [])[:30]) if x}
    with tlock:
        for rel in _paths(d):
            ap = fs_abs(rel)
            if ap is None or ap == BASE_DIR or not ap.exists():
                continue
            key = fs_rel(ap)
            cur = (set(manual_tags.get(key, ())) | add) - rem
            if cur:
                manual_tags[key] = sorted(cur)[:30]
            else:
                manual_tags.pop(key, None)
        _save(TAGS_FILE, manual_tags)
    fs_dirty()
    return jsonify(success=True)


@app.route("/fs/rename", methods=["POST"])
def fs_rename():
    d = json_body()
    ap, name = fs_abs(d.get("path")), str(d.get("name") or "").strip()
    if ap is None or ap == BASE_DIR or not ap.exists():
        return jsonify(success=False, message="Not found."), 404
    if ap.parent == BASE_DIR:
        return jsonify(success=False, message="The site folders cannot be renamed."), 400
    if not _name_ok(name):
        return jsonify(success=False, message="That name is not allowed."), 400
    dst = ap.with_name(name)
    if dst.exists():
        return jsonify(success=False, message="Something with that name already exists."), 409
    try:
        ap.rename(dst)
    except OSError as exc:
        return jsonify(success=False, message=f"Could not rename: {exc.strerror}"), 500
    _retag(fs_rel(ap), fs_rel(dst))
    fs_dirty()
    return jsonify(success=True, path=fs_rel(dst))


@app.route("/fs/mkdir", methods=["POST"])
def fs_mkdir():
    d = json_body()
    parent, name = fs_abs(d.get("path")), str(d.get("name") or "").strip()
    if parent is None or parent == BASE_DIR or not parent.is_dir():
        return jsonify(success=False, message="Open a folder inside a site folder first."), 400
    if not _name_ok(name):
        return jsonify(success=False, message="That name is not allowed."), 400
    try:
        (parent / name).mkdir()
    except FileExistsError:
        return jsonify(success=False, message="That folder already exists."), 409
    except OSError as exc:
        return jsonify(success=False, message=f"Could not create it: {exc.strerror}"), 500
    fs_dirty()
    return jsonify(success=True, path=fs_rel(parent / name))


@app.route("/fs/move", methods=["POST"])
def fs_move():
    d = json_body()
    dest = fs_abs(d.get("dest"))
    if dest is None or dest == BASE_DIR or not dest.is_dir():
        return jsonify(success=False, message="Choose a folder inside a site folder."), 400
    moved = skipped = 0
    for rel in _paths(d):
        src = fs_abs(rel)
        if src is None or src == BASE_DIR or src.parent == BASE_DIR or not src.exists():
            skipped += 1; continue
        target = dest / src.name
        if dest == src or src in dest.parents or target.exists():
            skipped += 1; continue
        try:
            shutil.move(str(src), str(target))
        except OSError:
            skipped += 1; continue
        _retag(fs_rel(src), fs_rel(target))
        moved += 1
    fs_dirty()
    return jsonify(success=True, moved=moved, skipped=skipped)


@app.route("/fs/delete", methods=["POST"])
def fs_delete():
    done = 0
    for rel in _paths(json_body()):
        ap = fs_abs(rel)
        if ap is None or ap == BASE_DIR or ap.parent == BASE_DIR or not ap.exists():
            continue
        try:
            shutil.rmtree(ap) if ap.is_dir() and not ap.is_symlink() else ap.unlink()
        except OSError:
            continue
        _retag(fs_rel(ap), None)
        done += 1
        parent, root = ap.parent, (BASE_DIR / rel.split("/")[0]).resolve()
        while parent != root and parent != BASE_DIR:                  # tidy up emptied folders
            try:
                if any(parent.iterdir()):
                    break
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    fs_dirty()
    return jsonify(success=True, deleted=done)


@app.route("/fs/zip")
def fs_zip():
    tmp, count = tempfile.TemporaryFile(), 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for rel in request.args.getlist("p")[:200]:
            ap = fs_abs(rel)
            if ap is None or ap == BASE_DIR:
                continue
            files = [ap] if ap.is_file() else [f for f in ap.rglob("*") if f.is_file()] if ap.is_dir() else []
            for f in files:
                if not is_partial(f.name):
                    z.write(f, arcname=f.relative_to(BASE_DIR).as_posix()); count += 1
    if not count:
        return jsonify(success=False, message="Nothing to zip."), 404
    tmp.seek(0)
    return send_file(tmp, mimetype="application/zip", as_attachment=True, download_name="media-hub.zip")


@app.route("/fs/thumb")
def fs_thumb():
    """Small preview picture for a photo or video (made with ffmpeg once, then cached)."""
    ap = fs_abs(request.args.get("path"))
    if ap is None or not ap.is_file() or file_kind(ap.name) not in ("video", "photo"):
        return "", 404
    st = ap.stat()
    out = THUMB_DIR / (hashlib.sha1(f"{fs_rel(ap)}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest() + ".jpg")
    if not out.is_file():
        ff = shutil.which("ffmpeg")
        if not ff:
            return "", 404
        with _thumb_sem:
            for seek in ((["-ss", "1"], []) if file_kind(ap.name) == "video" else ([],)):
                if out.is_file():
                    break
                tmp = out.with_name(f"{out.stem}.{threading.get_ident()}.tmp.jpg")
                try:
                    subprocess.run([ff, "-y", "-nostdin", "-loglevel", "error", *seek, "-i", str(ap), "-frames:v", "1",
                                    "-vf", "scale=360:-2", "-q:v", "4", str(tmp)],
                                   timeout=25, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    os.replace(tmp, out)
                except (subprocess.SubprocessError, OSError):
                    tmp.unlink(missing_ok=True)
    if not out.is_file():
        return "", 404
    return send_file(out, mimetype="image/jpeg", max_age=86400)


# ---- password for the file browser (checked on the SERVER, so the files really are hidden) ----
FILES_PIN = os.environ.get("HUB_FILES_PIN", "145234").strip()
_FILES_SECRET = os.urandom(32)          # a restart locks everything again
_pin_fail = {}                          # ip -> [wrong tries, blocked until]


def _files_sig(exp):
    return hmac.new(_FILES_SECRET, str(exp).encode(), hashlib.sha256).hexdigest()


def files_unlocked():
    try:
        exp, sig = request.cookies.get("hub_files", "").split(".", 1)
        return int(exp) > time.time() and hmac.compare_digest(sig, _files_sig(exp))
    except ValueError:
        return False


def _job_allows(path, jid):
    """A finished download may be saved by the browser that started it (needs the private job id), even while locked."""
    cat, _, name = path[len("/files/"):].partition("/")
    mine = set(request.cookies.get("hub_jobs", "").split(",")) | {str(jid or "")}      # jobs THIS browser started
    with lock:
        for i in mine:
            job = jobs.get(i) if i else None
            if job and job.get("category") == cat and name in (job.get("files") or []):
                return True
    return False


def _remember_jobs(resp, *ids):
    """Remember which downloads this browser started, so it can always save them - no password needed."""
    cur = [x for x in request.cookies.get("hub_jobs", "").split(",") if re.fullmatch(r"[0-9a-f]{8,12}", x)]
    resp.set_cookie("hub_jobs", ",".join((cur + [i for i in ids if i])[-60:]), httponly=True, samesite="Lax", max_age=7 * 24 * 3600)
    return resp


@app.before_request
def require_files_pin():
    p = request.path
    if p in ("/fs/unlock", "/fs/lock"):
        return None
    if p.startswith("/files/"):
        if request.method == "GET" and _job_allows(p, request.args.get("j")):
            return None
    elif not (p.startswith("/fs/") or p == "/list-files"):
        return None
    if files_unlocked():
        return None
    return jsonify(success=False, locked=True, message="The files are locked. Enter the password first."), 403


@app.route("/fs/unlock", methods=["POST"])
def fs_unlock():
    ip, now = request.remote_addr or "?", time.time()
    st = _pin_fail.get(ip, [0, 0.0])
    if st[1] > now:
        return jsonify(success=False, message=f"Too many wrong tries. Wait {int(st[1] - now) + 1} seconds."), 429
    if hmac.compare_digest(str(json_body().get("pin") or "").encode(), FILES_PIN.encode()):
        _pin_fail.pop(ip, None)
        exp = int(now) + 6 * 3600
        resp = jsonify(success=True)
        resp.set_cookie("hub_files", f"{exp}.{_files_sig(exp)}", httponly=True, samesite="Lax", max_age=6 * 3600)
        return resp
    st[0] += 1
    if st[0] >= 5:
        st = [0, now + 60]
    _pin_fail[ip] = st
    return jsonify(success=False, message="Wrong password."), 403


@app.route("/fs/lock", methods=["POST"])
def fs_lock():
    resp = jsonify(success=True)
    resp.delete_cookie("hub_files")
    return resp


# ---- sending files to the phone: share-sheet Shortcut, convert, install as an app ----
PHOTO_EXT = (".mp4", ".m4v", ".mov", ".jpg", ".jpeg", ".png", ".gif", ".heic")       # what the Photos app accepts
PHOTO_MIME = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".png": "image/png", ".gif": "image/gif", ".heic": "image/heic"}


def phone_key():
    k = settings.get("phone_key")
    if not k:
        k = settings["phone_key"] = secrets.token_urlsafe(18)
        _save(SETTINGS_FILE, settings)
    return k


def _phone_ok():
    return hmac.compare_digest(str(request.args.get("key") or ""), phone_key())


def phone_files(job, limit=200):
    """Photos-ready files: videos and photos, but not the cover picture that sits next to a video."""
    names = [f for f in (job.get("files") or []) if f.lower().endswith(PHOTO_EXT)]
    vids = {os.path.splitext(f)[0] for f in names if f.lower().endswith((".mp4", ".m4v", ".mov"))}
    return [f for f in names if f.lower().endswith((".mp4", ".m4v", ".mov")) or os.path.splitext(f)[0] not in vids][:limit]


def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))             # nothing is sent: this only asks which network address would be used
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def lan_base():
    """The address the PHONE must use. A browser on 'localhost' would otherwise put 'localhost' into the QR code."""
    u = urlparse(request.host_url)
    host = _lan_ip() if (u.hostname or "") in ("localhost", "127.0.0.1", "::1") else u.hostname
    return f"{u.scheme}://{host}" + (f":{u.port}" if u.port else "")


def _zip_pairs(pairs, first_no=1):
    tmp, n = tempfile.TemporaryFile(), 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for i, (cat, rel) in enumerate(pairs, first_no):
            p = safe_path(cat, rel)
            if p:
                z.write(p, arcname=f"{i:03d}_{p.name}"); n += 1
    if not n:
        tmp.close()
        return None
    tmp.seek(0)
    return send_file(tmp, mimetype="application/zip", as_attachment=True, download_name="photos.zip")


@app.route("/phone/grab", methods=["POST"])
def phone_grab():
    """Share-sheet Shortcut: start a download from a link and wait (up to 50 s) so it can go straight into Photos."""
    if not _phone_ok():
        return jsonify(success=False, message="Wrong key."), 403
    raw = request.get_data(as_text=True) or ""
    m = re.search(r"https?://[^\s\"']+", str(json_body().get("url") or "") or raw)
    if not m:
        return jsonify(success=False, message="No link found."), 400
    job, err = enqueue("url", m.group(0), options={"kind": "video"})
    if err:
        return jsonify(success=False, message=err), 400
    end = time.time() + 50
    while time.time() < end and job["status"] in ("queued", "running"):
        time.sleep(0.5)
    if job["status"] == "error":
        return jsonify(success=False, message=job["message"]), 500
    if job["status"] != "done":
        return jsonify(success=False, working=True, job=job["id"], message="Still downloading - it will be in Media Hub when it finishes."), 202
    names = phone_files(job, 40)
    resp = _zip_pairs([(job["category"], n) for n in names]) if names else None
    return resp or (jsonify(success=False, message="Downloaded, but Photos cannot take this format. Turn on 'iPhone-friendly (H.264)' in Settings."), 415)


@app.route("/fs/phonekey", methods=["GET", "POST"])            # under /fs/ = needs the file password
def fs_phonekey():
    if request.method == "POST":
        settings["phone_key"] = secrets.token_urlsafe(18)
        _save(SETTINGS_FILE, settings)
    return jsonify(success=True, key=phone_key(), base=lan_base())


# ---- make old videos iPhone-friendly (H.264 / .mp4) ----
def _vcodec(path):
    ff = shutil.which("ffprobe")
    if not ff:
        return ""
    try:
        r = subprocess.run([ff, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip().lower()
    except (OSError, subprocess.SubprocessError):
        return ""


def convert_worker(job, rels):
    cancel = job_cancel(job)
    try:
        ff = _need_ffmpeg()
    except RuntimeError as exc:
        return finish_err(job, str(exc))
    todo, fine = [], 0
    for rel in rels:
        ap = fs_abs(rel)
        if ap is None or not ap.is_file():
            continue
        if _vcodec(ap) in ("h264", "hevc") and ap.suffix.lower() in (".mp4", ".mov", ".m4v"):
            fine += 1
        else:
            todo.append(ap)
    made = 0
    for i, ap in enumerate(todo):
        if cancel.is_set():
            return finish_err(job, "Cancelled by user")
        update(job, message=f"Converting {i + 1}/{len(todo)}: {ap.name[:40]}", progress=int(i / len(todo) * 100))
        dst, k = ap.with_name(f"{ap.stem} [iphone].mp4"), 1
        while dst.exists():
            k += 1
            dst = ap.with_name(f"{ap.stem} [iphone {k}].mp4")
        tmp = dst.with_name(f"{dst.stem}.{job['id']}.fxtmp.mp4")
        try:
            _ffmpeg([ff, "-y", "-nostdin", "-loglevel", "error", "-i", str(ap), "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
                     "-crf", "21", "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(tmp)],
                    timeout=3600, cancel=cancel)
            os.replace(tmp, dst); made += 1
        except DownloadCancelled:
            tmp.unlink(missing_ok=True)
            return finish_err(job, "Cancelled by user")
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            tmp.unlink(missing_ok=True)
            app.logger.warning("convert failed for %s: %s", ap, exc)
    fs_dirty()
    finish_ok(job, [], f"Made {made} iPhone-friendly cop{'y' if made == 1 else 'ies'} ([iphone].mp4)" + (f" · {fine} already fine" if fine else ""), fresh=[])


@app.route("/fs/convert", methods=["POST"])
def fs_convert():
    rels = []
    for rel in _paths(json_body()):
        ap = fs_abs(rel)
        if ap is None or ap == BASE_DIR:
            continue
        if ap.is_file() and file_kind(ap.name) == "video":
            rels.append(fs_rel(ap))
        elif ap.is_dir():
            rels += [fs_rel(f) for f in sorted(ap.rglob("*")) if f.is_file() and file_kind(f.name) == "video" and " [iphone" not in f.name][:100]
    if not rels:
        return jsonify(success=False, message="No videos in your selection."), 400
    if not shutil.which("ffmpeg"):
        return jsonify(success=False, message="ffmpeg is not installed on the server."), 400
    job = {"id": uuid.uuid4().hex[:12], "category": rels[0].split("/")[0], "label": f"Converting {len(rels)} video(s)", "status": "queued", "progress": 0,
           "message": "Waiting…", "speed": "", "eta": "", "files": [], "fresh": [], "created": time.time(), "finished": None, "silent": True, "batch": None}
    cancel = threading.Event()
    with lock:
        jobs[job["id"]], cancels[job["id"]] = job, cancel
    pool.submit(_run_safely, convert_worker, job, rels)
    return jsonify(success=True, job_id=job["id"], count=len(rels))


# ---- install as an app (home-screen icon, full screen) ----
_icons = {}


def _icon_png(n):
    if n in _icons:
        return _icons[n]
    c0, c1, ss = (139, 92, 246), (34, 211, 238), 2

    def white(u, v):                                   # is this point part of the white "download" arrow?
        return ((0.43 <= u <= 0.57 and 0.24 <= v <= 0.54) or (0.54 <= v <= 0.74 and abs(u - 0.5) <= 0.74 - v)
                or (0.26 <= u <= 0.74 and 0.79 <= v <= 0.84))
    raw = bytearray()
    for y in range(n):
        raw.append(0)
        for x in range(n):
            w = sum(white((x + (a + .5) / ss) / n, (y + (b + .5) / ss) / n) for a in range(ss) for b in range(ss)) / (ss * ss)
            t = (x + y) / (2 * n)
            for i in range(3):
                raw.append(int((c0[i] + (c1[i] - c0[i]) * t) * (1 - w) + 255 * w))

    def chunk(kind, d):
        return struct.pack(">I", len(d)) + kind + d + struct.pack(">I", zlib.crc32(kind + d) & 0xffffffff)
    _icons[n] = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))
    return _icons[n]


@app.route("/icon/<int:size>.png")
def pwa_icon(size):
    return app.response_class(_icon_png(max(64, min(512, size))), mimetype="image/png", headers={"Cache-Control": "public, max-age=604800"})


@app.route("/manifest.webmanifest")
def pwa_manifest():
    icons = [{"src": f"/icon/{s}.png", "sizes": f"{s}x{s}", "type": "image/png", "purpose": "any maskable"} for s in (192, 512)]
    return app.response_class(json.dumps({"name": "Media Hub", "short_name": "Media Hub", "start_url": "/", "scope": "/", "display": "standalone",
                                          "background_color": "#07080f", "theme_color": "#07080f", "icons": icons}), mimetype="application/manifest+json")


@app.route("/sw.js")
def pwa_sw():
    js = "self.addEventListener('install',()=>self.skipWaiting());self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));self.addEventListener('fetch',()=>{});"
    return app.response_class(js, mimetype="application/javascript", headers={"Cache-Control": "no-cache"})


manual_tags.update(_load(TAGS_FILE, {}))


watches.update(_load(WATCH_FILE, {}))
settings.update(_load(SETTINGS_FILE, {}))
for _w in watches.values():
    _w["busy"] = False
threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()
threading.Thread(target=meta_loop, daemon=True, name="meta").start()


# --------------------------------------------------------------------------- #
# Security check: warn (or refuse to start) when the site is left with the default passwords
# --------------------------------------------------------------------------- #
DEFAULT_FILES_PIN = "145234"


def security_problems():
    """List of things that make private files easy to reach. Empty list = fine."""
    out = []
    if not HUB_TOKEN:
        out.append({"id": "token", "text": "No access token is set (HUB_TOKEN). Anyone on your network - or your Tailscale network - can open this page and your files screen."})
    elif len(HUB_TOKEN) < 12:
        out.append({"id": "token_short", "text": "HUB_TOKEN is short. Use at least 12 random characters."})
    if FILES_PIN == DEFAULT_FILES_PIN:
        out.append({"id": "pin_default", "text": "The file password is still the default (145234). Set your own with HUB_FILES_PIN."})
    elif len(FILES_PIN) < 8:
        out.append({"id": "pin_short", "text": "The file password is short. Use 8 or more characters (HUB_FILES_PIN)."})
    return out


@app.route("/security/status")
def security_status():
    return jsonify(success=True, problems=security_problems(), strict=os.environ.get("HUB_STRICT", "") == "1")


def _security_startup():
    probs = security_problems()
    if not probs:
        return
    bar = "!" * 70
    print("\n" + bar + "\n  SECURITY WARNING - your private files are not well protected:")
    for p in probs:
        print("   - " + p["text"])
    print("  Fix: set HUB_TOKEN and HUB_FILES_PIN as environment variables (docker-compose 'environment:'), then restart.")
    print("  Add HUB_STRICT=1 to make the server refuse to start until this is fixed.\n" + bar + "\n", flush=True)
    if os.environ.get("HUB_STRICT", "") == "1":
        raise SystemExit("HUB_STRICT=1: refusing to start with weak or missing passwords.")


_security_startup()


if __name__ == "__main__":
    # debug=False: Werkzeug's debugger on 0.0.0.0 allows remote code execution,
    # and the reloader would duplicate the job queue.
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)