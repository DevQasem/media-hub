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
from urllib.parse import quote, urlparse

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
CATEGORIES = ("youtube", "tiktok", "snapchat", "instagram", "general")
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
        if j.get("status") in ("queued", "running"):
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


def finish_ok(job, files, message=None, fresh=None):
    """fresh = the files this job really created now (defaults to all of them)."""
    update(job, status="done", progress=100, files=list(files), speed="", eta="",
           fresh=list(files if fresh is None else fresh),
           message=message or f"Saved {len(files)} file(s)", finished=time.time())
    save_jobs()
    _after_finish(job, True)


def finish_err(job, msg):
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


def parse_cookie_text(text: str):
    """
    Check a pasted/uploaded cookies.txt (Netscape format) and keep ONLY instagram.com cookies
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
        if not host_is(domain.lstrip("."), "instagram.com"):
            ignored += 1
            continue
        exp = max(0, int(float(expiry)))
        kept.append("\t".join([domain, flag.upper(), path, secure.upper(), str(exp), name, value]))
    if not kept:
        raise ValueError("No Instagram cookies found. Open instagram.com while logged in, export again "
                         "(Netscape / cookies.txt format) and make sure the file is the Instagram one.")
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
    "subs": False, "autosubs": False, "sub_langs": "en", "embed_subs": False,
    "thumb": False, "embed_thumb": False, "meta": False, "sponsor": False, "desc": False,
    "playlist": "single", "items": "",                          # single | all, "1-5"
    "clip_start": "", "clip_end": "",                           # "1:30" or seconds
    "date_after": "", "date_before": "",                        # YYYY-MM-DD
    "min_dur": "", "max_dur": "", "min_views": "", "max_mb": "",
    "title_has": "", "title_not": "",
    "name": "",                                                 # file name typed on the page (empty = automatic)
    "ig_what": "posts",                                         # profile link: posts | stories | highlights | all
    "force": False,                                             # re-download even if the file exists
    # effects (ffmpeg, applied after the download - like the Telegram bots)
    "fx_look": "none", "fx_blur": "none", "fx_sharpen": False, "fx_mirror": "none",
    "fx_rotate": "0", "fx_speed": "1", "fx_crop": "none", "fx_scale": "none",
    "fx_mute": False, "fx_keep": True,                           # fx_keep = keep the original too
}
_ENUMS = {"kind": {"video", "audio", "image", "gif", "sticker"},
          "quality": {"best", "2160", "1440", "1080", "720", "480", "360"},
          "container": {"mp4", "mkv", "webm"},
          "audio_fmt": {"mp3", "m4a", "opus", "flac", "wav"},
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


def build_ydl_opts(options, folder, hook, tmpl=None, cookiefile=None, ig=False, ig_sub="posts"):
    """Translate UI options into a yt-dlp options dict."""
    from yt_dlp.utils import DateRange, download_range_func, match_filter_func
    o = sanitize_options(options)
    if o["kind"] in ("gif", "sticker"):               # these only need the picture, no subs/tags/etc.
        for k in ("subs", "autosubs", "embed_subs", "thumb", "embed_thumb", "meta", "sponsor", "desc"):
            o[k] = False
    if tmpl is None:
        who = "%(channel,uploader_id,uploader|Unknown).60B/" if ig else "%(uploader,channel,creator|Unknown).60B/"
        creator = who if settings.get("creator_folders", True) else ""
        tmpl = creator + "%(title).120B [%(id)s].%(ext)s"
        if ig and settings.get("ig_organize", True):          # instagram/<user>/<post date>/<type>/title [id].ext
            tmpl = who + f"%(upload_date>%Y-%m-%d|unknown-date)s/{ig_sub}/" + "%(title).120B [%(id)s].%(ext)s"
        if o["playlist"] == "all":
            tmpl = "%(playlist_title|).80B/%(playlist_index|)s %(title).100B [%(id)s].%(ext)s"
    opts = {"outtmpl": str(folder / tmpl), "quiet": True, "no_warnings": True, "noprogress": True,
            "socket_timeout": 20, "retries": 3, "fragment_retries": 3, "progress_hooks": [hook],
            "noplaylist": o["playlist"] != "all"}
    if cookiefile:
        opts["cookiefile"] = cookiefile
    pps = []

    # --- what to get ---------------------------------------------------------
    if o["kind"] == "audio":
        opts["format"] = "bestaudio/best"
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": o["audio_fmt"]}
        if o["audio_fmt"] in ("mp3", "m4a", "opus"):
            pp["preferredquality"] = o["abr"]
        pps.append(pp)
    elif o["kind"] in ("gif", "sticker"):
        opts["format"] = "bv*[height<=720]/b[height<=720]/bv*/b"      # video only, small is fine
        opts["merge_output_format"] = "mp4"
    elif o["kind"] == "image":
        opts.update(skip_download=True, writethumbnail=True)
        pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
    else:
        q = o["quality"]
        opts["format"] = "bv*+ba/b" if q == "best" else f"bv*[height<={q}]+ba/b[height<={q}]/b"
        opts["merge_output_format"] = o["container"]
        if o["compat"] or settings.get("prefer_h264", True):        # H.264 plays everywhere, incl. iPhone Photos (VP9/AV1 do not)
            opts["format_sort"] = ["vcodec:h264", "res", "acodec:m4a"]

    # --- extras ----------------------------------------------------------------
    if o["sponsor"]:
        cats = ["sponsor", "selfpromo", "interaction"]
        pps += [{"key": "SponsorBlock", "categories": cats, "when": "after_filter"},
                {"key": "ModifyChapters", "remove_sponsor_segments": cats, "remove_chapters_patterns": [],
                 "remove_ranges": [], "sponsorblock_chapter_title": "[SponsorBlock]: %(category_names)l",
                 "force_keyframes": False}]
    if o["subs"] or o["autosubs"]:
        opts.update(writesubtitles=o["subs"], writeautomaticsub=o["autosubs"],
                    subtitleslangs=[x for x in o["sub_langs"].split(",") if x],
                    subtitlesformat="srt/best")
        if o["embed_subs"] and o["kind"] == "video":
            pps += [{"key": "FFmpegSubtitlesConvertor", "format": "srt"}, {"key": "FFmpegEmbedSubtitle"}]
    if o["meta"]:
        pps.append({"key": "FFmpegMetadata", "add_chapters": True, "add_metadata": True})
    if (o["thumb"] or o["embed_thumb"]) and o["kind"] != "image":
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
                return finish_err(job, "No video matched your filters." if has_filters(o)
                                  else "Nothing was saved - the site returned no downloadable media.")
            note = None
            # "real" = this job fetched something (or wrote a cover). Otherwise yt-dlp found it already on disk.
            real = fetched["n"] > 0 or o["kind"] == "image"
            fresh = fresh_since(folder, files, started) if real else []
            if not fresh:
                note = "Already downloaded - the file is ready (tick “Re-download if it exists” to fetch it again)."
            # effects / GIF / sticker only touch THIS job's own media files
            targets = main if o["kind"] != "image" else [f for f in side if f.lower().endswith(IMG_EXT)]
            others = [f for f in files if f not in targets]
            out, fxnote = postprocess_media(folder, targets, o, job)
            made_now = set(out) - set(targets)             # files created by the effects step
            files = sorted(set(others) | set(out))
            note = fxnote or note
            fresh_now = (set(fresh) & set(files)) | made_now
            auto = not o["name"] and settings.get("auto_name", "date") == "date"     # nothing typed: date + number
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
        mtype = sn.get("snapMediaType")
        kind = "video" if mtype == 1 else "image" if mtype == 0 else "unknown"
        if not (isinstance(prev, str) and prev.startswith("https://")):
            prev = media if kind == "image" and isinstance(media, str) and media.startswith("https://") else None
        ts = sn.get("timestampInSec")
        ts = ts.get("value") if isinstance(ts, dict) else ts
        out.append({"id": sid, "index": len(out) + 1, "kind": kind, "preview": prev,
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
    url = str(json_body().get("url") or "").strip()
    if not url:
        return jsonify(success=False, error="No URL provided"), 400
    if not valid_http_url(url):
        return jsonify(success=False, error="That doesn't look like a valid link."), 400

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
                       upload_date=info.get("upload_date"),
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
        if not valid_http_url(target):
            return None, "Please enter a valid http(s) link."
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
           "finished": None, "batch": batch}
    with lock:
        jobs[job["id"]], cancels[job["id"]] = job, cancel
    pool.submit(_run_safely, fn, job, *args)
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
        resp = redirect(request.path)
        resp.set_cookie("hub_token", HUB_TOKEN, httponly=True, samesite="Lax", max_age=60 * 60 * 24 * 365)
        return resp
    return jsonify(success=False, message="Locked. Open the site once with ?token=YOUR_TOKEN."), 401


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


def run_watch(wid):
    """One scheduled check of one watched account (runs in the thread pool). Never leaves a watch 'busy'."""
    with wlock:
        w = watches.get(wid)
    if not w:
        return
    new = []
    job = {"status": "error", "message": "The check could not start.", "fresh": []}     # placeholder until the real job exists
    cancel = threading.Event()
    try:
        job = {"id": uuid.uuid4().hex[:12], "category": w["category"], "label": f"👁 {w['label']}",
               "status": "running", "progress": 0, "message": "Checking…", "speed": "", "eta": "",
               "files": [], "fresh": [], "created": time.time(), "finished": None, "silent": True, "batch": None}
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
            title, body = describe_new(w, new)
            notify(title, body, ["eyes"])
        except Exception:  # noqa: BLE001
            app.logger.exception("Could not build the notification")


def scheduler_loop():
    while True:
        time.sleep(15)
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
            return jsonify(success=True, intervals=INTERVALS,
                           watches=sorted((dict(w) for w in watches.values()), key=lambda w: w["created"]))
    d = json_body()
    kind, target = d.get("kind"), str(d.get("target") or "").strip()
    interval = d.get("interval") if d.get("interval") in INTERVALS else 60
    if kind == "snapchat":
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
            if iv not in INTERVALS:
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
        if "ig_cap" in d:
            try:
                settings["ig_cap"] = max(1, min(500, int(d["ig_cap"])))
            except (TypeError, ValueError):
                return jsonify(success=False, message="The profile limit must be a number (1-500)."), 400
        _save(SETTINGS_FILE, settings)
    return jsonify(success=True, ntfy=settings.get("ntfy", ""),
                   creator_folders=settings.get("creator_folders", True), ig_organize=settings.get("ig_organize", True), auto_name=settings.get("auto_name", "date"), prefer_h264=settings.get("prefer_h264", True),
                   notify_downloads=settings.get("notify_downloads", False),
                   ig_cap=ig_cap())


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
    if len(parts) >= 3 or (is_dir and len(parts) >= 2):
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
    return t


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