"""Media Hub Telegram bot: send a link (or a Snapchat username), pick what you want with buttons,
the Hub downloads it, and the bot asks whether to send the files here too."""
import asyncio, json, os, re, time, uuid
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from telegram import BotCommand, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, InputMediaPhoto, Update
from telegram.error import TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

BOT_TOKEN = os.environ["BOT_TOKEN"]
HUB = os.environ.get("HUB_URL", "http://127.0.0.1:5000").rstrip("/")
HUB_TOKEN = os.environ.get("HUB_TOKEN", "")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x.isdigit()}
DATA = Path(os.environ.get("BOT_DATA", "/root/media-downloader/bot_settings.json"))   # inside your ./hub-data volume
HEADERS = {"X-Hub-Token": HUB_TOKEN} if HUB_TOKEN else {}

URL_RE = re.compile(r"https?://\S+")
SNAP_RE = re.compile(r"(?:snapchat\.com/add/|story\.snapchat\.com/@|snapchat\.com/@)([A-Za-z0-9._-]{3,32})", re.I)
USER_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
IMG, VID, AUD = (".jpg", ".jpeg", ".png"), (".mp4", ".mov", ".m4v"), (".mp3", ".m4a")
LIMIT = 49 * 1024 * 1024            # Telegram bots can upload up to 50 MB
PHOTO_LIMIT = 10 * 1024 * 1024      # bigger pictures are sent as documents

# ---------------------------------------------------------------- settings
CHOICES = {
    "mode":    ("🎬 Type", [("video", "🎬 Video"), ("audio", "🎵 Audio (MP3)"), ("image", "🖼 Cover photo")]),
    "quality": ("📺 Quality", [("best", "Best"), ("1080", "1080p"), ("720", "720p"), ("480", "480p")]),
    "look":    ("✨ Filter", [("none", "None"), ("bw", "⚫ B&W"), ("sepia", "🟤 Sepia"), ("vivid", "🌈 Vivid"),
                              ("warm", "🔥 Warm"), ("cool", "❄️ Cool"), ("vintage", "📼 Vintage"), ("cinematic", "🎞 Cinematic")]),
    "send":    ("📤 Send to Telegram", [("ask", "Ask me"), ("always", "Always"), ("never", "Never")]),
}
LABEL = {k: dict(v[1]) for k, v in CHOICES.items()}
DEFAULTS = {"mode": "video", "quality": "best", "look": "none", "send": "ask"}
_store = {}
try:
    _store = json.loads(DATA.read_text())
except (OSError, ValueError):
    pass


def get_set(uid):
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (_store.get(str(uid)) or {}).items() if k in LABEL and v in LABEL[k]})
    return s


def put_set(uid, key, val):
    if key in LABEL and val in LABEL[key]:
        _store.setdefault(str(uid), {})[key] = val
        try:
            DATA.parent.mkdir(parents=True, exist_ok=True)
            DATA.write_text(json.dumps(_store))
        except OSError:
            pass


def settings_text(s):
    return "⚙️ Settings\n\n" + "\n".join(f"{CHOICES[k][0]}: {LABEL[k][s[k]]}" for k in CHOICES) + "\n\nTap a line to change it."


def settings_kb(s):
    rows = [[Btn(f"{CHOICES[k][0]}: {LABEL[k][s[k]]}", callback_data=f"st:open:{k}")] for k in CHOICES]
    return Kb(rows + [[Btn("✅ Done", callback_data="st:close")]])


def choice_kb(cur, items, cb, back):
    btns = [Btn(("✅ " if v == cur else "") + lab, callback_data=cb(v)) for v, lab in items]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    return Kb(rows + [[Btn("⬅️ Back", callback_data=back)]])


# ---------------------------------------------------------------- link cards
P = {}      # pending cards: id -> {kind, target, mode, quality, look}


def make_pending(uid, url=None, snap=None):
    s = get_set(uid)
    pid = uuid.uuid4().hex[:8]
    if len(P) > 200:
        P.clear()
    P[pid] = {"kind": "snap" if snap else "url", "target": snap or url,
              "mode": s["mode"], "quality": s["quality"], "look": s["look"]}
    return pid, P[pid]


def card_text(p):
    look = LABEL["look"][p["look"]]
    if p["kind"] == "snap":
        return f"👻 Snapchat stories of @{p['target']}\n✨ Filter: {look}"
    host = (urlparse(p["target"]).hostname or p["target"]).removeprefix("www.")
    return f"🔗 {host}\nDefault: {LABEL['mode'][p['mode']]} · 📺 {LABEL['quality'][p['quality']]} · ✨ {look}\n\nWhat do you want?"


def card_kb(pid, p):
    if p["kind"] == "snap":
        rows = [[Btn("⬇️ Download stories", callback_data=f"ln:{pid}:go:snap")]]
    else:
        rows = [[Btn("🎬 Video", callback_data=f"ln:{pid}:go:video"), Btn("🎵 Audio", callback_data=f"ln:{pid}:go:audio"),
                 Btn("🖼 Cover", callback_data=f"ln:{pid}:go:image")]]
    rows.append([Btn(f"✨ Filter: {LABEL['look'][p['look']]}", callback_data=f"ln:{pid}:filt"), Btn("✖", callback_data=f"ln:{pid}:x")])
    return Kb(rows)


async def safe_edit(msg, text, kb=None):
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramError:
        pass


def ok(update):
    return update.effective_user and update.effective_user.id in ALLOWED


# ---------------------------------------------------------------- commands
HELP = ("Send me a link (TikTok, YouTube, Instagram, Snapchat…) and pick what you want with the buttons.\n"
        "👻 Snapchat stories: /snap username\n"
        "⚙️ Filters and preferences: /settings")


async def on_start(update: Update, ctx):
    if not ok(update):
        return await update.message.reply_text("⛔ Not allowed.")
    await update.message.reply_text("👋 Welcome to your Media Hub!\n\n" + HELP,
                                    reply_markup=Kb([[Btn("⚙️ Settings", callback_data="st:menu")]]))


async def on_settings(update: Update, ctx):
    if not ok(update):
        return await update.message.reply_text("⛔ Not allowed.")
    s = get_set(update.effective_user.id)
    await update.message.reply_text(settings_text(s), reply_markup=settings_kb(s))


async def on_snap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update):
        return await update.message.reply_text("⛔ Not allowed.")
    name = (ctx.args[0] if ctx.args else "").lstrip("@")
    if not USER_RE.match(name):
        return await update.message.reply_text("Use it like this:  /snap username")
    pid, p = make_pending(update.effective_user.id, snap=name)
    await update.message.reply_text(card_text(p), reply_markup=card_kb(pid, p))


async def on_text(update: Update, ctx):
    if not ok(update):
        return await update.message.reply_text("⛔ Not allowed.")
    m = URL_RE.search(update.message.text or "")
    if not m:
        return await update.message.reply_text("Send me a link, or use /snap username 👻\n\n" + HELP)
    url = m.group(0).rstrip(").,")
    sm = SNAP_RE.search(url)
    pid, p = make_pending(update.effective_user.id, url=None if sm else url, snap=sm.group(1) if sm else None)
    await update.message.reply_text(card_text(p), reply_markup=card_kb(pid, p))


# ---------------------------------------------------------------- jobs
async def start_job(q, ctx, p, mode):
    fx = {"fx_look": p["look"], "fx_keep": False}              # keep only the filtered file
    if p["kind"] == "snap":
        body = {"action_type": "snapchat", "target": p["target"], "options": fx}
    else:
        body = {"action_type": "url", "target": p["target"],
                "options": {**fx, "kind": mode, "quality": p["quality"]}}
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=30) as c:
            d = (await c.post(f"{HUB}/download-action", json=body)).json()
    except Exception as exc:  # noqa: BLE001
        return await safe_edit(q.message, f"⚠️ Could not reach Media Hub: {exc}")
    if not d.get("success"):
        return await safe_edit(q.message, "⚠️ " + str(d.get("message", "Failed")))
    await safe_edit(q.message, "⏳ Sent to Media Hub…", cancel_kb(d["job_id"]))
    ctx.application.create_task(watch(q.message, d["job_id"], q.from_user.id))


def cancel_kb(jid):
    return Kb([[Btn("✖ Cancel", callback_data=f"cx:{jid}")]])


async def watch(msg, jid, uid):
    end, last = time.time() + 45 * 60, ""
    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as c:
        while time.time() < end:
            await asyncio.sleep(3)
            try:
                j = (await c.get(f"{HUB}/job/{jid}")).json()
            except Exception:  # noqa: BLE001
                continue
            if j.get("status") == "error":
                return await safe_edit(msg, "⚠️ " + str(j.get("message", "Failed")))
            if j.get("status") == "done":
                n = len(j.get("files") or [])
                if not n:
                    return await safe_edit(msg, "✅ Done: " + str(j.get("message") or "nothing new to send."))
                mode = get_set(uid)["send"]
                if mode == "always":
                    return await deliver(msg, msg.get_bot(), msg.chat_id, jid)
                if mode == "never":
                    return await safe_edit(msg, f"✅ Saved {n} file(s) in Media Hub.")
                return await safe_edit(msg, f"✅ Saved {n} file(s) in Media Hub.\nSend them here too?",
                                       Kb([[Btn("📤 Yes, send here", callback_data=f"yes:{jid}"), Btn("No", callback_data=f"no:{jid}")]]))
            text = f"⏳ {j.get('message', '')} {int(j.get('progress') or 0)}%"
            if text != last:
                last = text
                await safe_edit(msg, text, cancel_kb(jid))
    await safe_edit(msg, "⌛ Still running. Check Media Hub later.")


async def deliver(msg, bot, chat, jid):
    await safe_edit(msg, "📤 Sending…")
    skipped, sent = [], 0
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=120) as c:
            j = (await c.get(f"{HUB}/job/{jid}")).json()
            cat, files = j["category"], sorted(j.get("files") or [])

            async def get(f):
                r = await c.get(f"{HUB}/files/{quote(cat)}/{quote(f)}", params={"j": jid})
                r.raise_for_status()
                return r.content

            vids = [f for f in files if f.lower().endswith(VID)]
            imgs = [] if vids else [f for f in files if f.lower().endswith(IMG)]     # covers next to a video are skipped
            rest = vids or [f for f in files if f not in imgs]
            for i in range(0, len(imgs), 10):                                         # photos: albums of up to 10
                media = []
                for f in imgs[i:i + 10]:
                    data = await get(f)
                    if len(data) > PHOTO_LIMIT:
                        await bot.send_document(chat, data, filename=f.rsplit("/", 1)[-1]); sent += 1
                    else:
                        media.append(InputMediaPhoto(data))
                if len(media) == 1:
                    await bot.send_photo(chat, media[0].media)
                elif media:
                    await bot.send_media_group(chat, media)
                sent += len(media)
            for f in rest:
                name, data = f.rsplit("/", 1)[-1], await get(f)
                if len(data) > LIMIT:
                    skipped.append(name); continue
                if f.lower().endswith(VID):
                    await bot.send_video(chat, data, filename=name, supports_streaming=True)
                elif f.lower().endswith(AUD):
                    await bot.send_audio(chat, data, filename=name)
                else:
                    await bot.send_document(chat, data, filename=name)
                sent += 1
    except Exception as exc:  # noqa: BLE001
        return await safe_edit(msg, f"⚠️ Sending failed after {sent} file(s): {exc}")
    note = f"✅ Sent {sent} file(s)."
    if skipped:
        note += "\n⚠️ Too big for Telegram, only in Media Hub: " + ", ".join(skipped[:5])
    await safe_edit(msg, note)


# ---------------------------------------------------------------- buttons
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ALLOWED:
        return await q.answer("Not allowed", show_alert=True)
    await q.answer()
    uid, parts = q.from_user.id, q.data.split(":")
    kind = parts[0]

    if kind == "st":                                           # settings screens
        act = parts[1]
        if act == "set":
            put_set(uid, parts[2], parts[3])
        if act in ("menu", "set"):
            s = get_set(uid)
            return await safe_edit(q.message, settings_text(s), settings_kb(s))
        if act == "open" and parts[2] in CHOICES:
            k, s = parts[2], get_set(uid)
            return await safe_edit(q.message, f"{CHOICES[k][0]}\nChoose one:",
                                   choice_kb(s[k], CHOICES[k][1], lambda v: f"st:set:{k}:{v}", "st:menu"))
        return await safe_edit(q.message, "✅ Settings saved.")

    if kind in ("ln", "lf"):                                   # link card
        p = P.get(parts[1])
        if not p:
            return await safe_edit(q.message, "⌛ This card expired. Send the link again.")
        act = parts[2]
        if kind == "lf":                                       # a filter was picked on the card
            if act in LABEL["look"]:
                p["look"] = act
            return await safe_edit(q.message, card_text(p), card_kb(parts[1], p))
        if act == "x":
            P.pop(parts[1], None)
            return await safe_edit(q.message, "Cancelled.")
        if act == "filt":
            return await safe_edit(q.message, "✨ Pick a filter:",
                                   choice_kb(p["look"], CHOICES["look"][1], lambda v: f"lf:{parts[1]}:{v}", f"lf:{parts[1]}:back"))
        if act == "go":
            P.pop(parts[1], None)
            return await start_job(q, ctx, p, parts[3] if len(parts) > 3 else p["mode"])

    if kind == "cx":                                           # cancel a running download
        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=15) as c:
                await c.post(f"{HUB}/cancel/{parts[1]}")
        except Exception:  # noqa: BLE001
            pass
        return await safe_edit(q.message, "🛑 Cancelling…")

    if kind == "no":
        return await safe_edit(q.message, "👍 Saved in Media Hub only.")
    if kind == "yes":
        await deliver(q.message, ctx.bot, q.message.chat_id, parts[1])


async def post_init(app):
    await app.bot.set_my_commands([BotCommand("settings", "⚙️ Filters & preferences"),
                                   BotCommand("snap", "👻 Download Snapchat stories"),
                                   BotCommand("help", "How to use")])


def main():
    if not ALLOWED:
        raise SystemExit("Set ALLOWED_IDS (your Telegram numeric id).")
    app = (Application.builder().token(BOT_TOKEN).post_init(post_init)
           .read_timeout(120).write_timeout(120).connect_timeout(30).build())
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("help", on_start))
    app.add_handler(CommandHandler("settings", on_settings))
    app.add_handler(CommandHandler("snap", on_snap))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_button))
    app.run_polling()


if __name__ == "__main__":
    main()