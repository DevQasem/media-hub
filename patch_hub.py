#!/usr/bin/env python3
"""
Media Hub patcher.  Usage:  python3 patch_hub.py /path/to/project
Expects:  app.py, views/index.html, assets/style.css, assets/script.js
Each original file is copied to <name>.bak first. Every fix prints OK or SKIPPED.
"""
import re
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
FILES = {"py": root / "app.py", "html": root / "views/index.html",
         "css": root / "assets/style.css", "js": root / "assets/script.js"}
for p in FILES.values():
    if not p.is_file():
        sys.exit(f"Missing file: {p}")
    shutil.copyfile(p, p.with_name(p.name + ".bak"))
T = {k: p.read_text(encoding="utf-8") for k, p in FILES.items()}
bad = 0


def rep(k, label, old, new, count=1):
    """Plain replace. count=0 means 'all occurrences'."""
    global bad
    n = T[k].count(old)
    if n == 0 or (count and n != count):
        print(f"SKIPPED  {label}  (found {n} matches)"); bad += 1; return
    T[k] = T[k].replace(old, new)
    print(f"OK       {label}")


def rex(k, label, pattern, new, flags=0):
    global bad
    T[k], n = re.subn(pattern, new, T[k], count=1, flags=flags)
    if n == 0:
        print(f"SKIPPED  {label}  (pattern not found)"); bad += 1
    else:
        print(f"OK       {label}")


# =========================================================================== #
# CSS
# =========================================================================== #
rep("css", "CSS: real dark-theme values for --field/--field2/--toast/--top-bg",
    "--field:var(--field); --field2:var(--field2); --modal:#13152b; --toast:var(--toast); --top-bg:var(--top-bg);",
    "--field:rgba(10,12,28,.7); --field2:rgba(255,255,255,.04); --modal:#13152b; --toast:#1b1e3a; --top-bg:rgba(7,8,15,.72);")
rex("css", "CSS: remove body[data-site] --pc overrides (JS sets the colour on <html>)",
    r'\n\s*body\[data-site="(?:youtube|tiktok|snapchat|instagram|facebook|reddit|twitch|x)"\]\s*\{\s*--pc:[^}]*\}[^\n]*', "")
# the helper above removes only one; loop for the rest
for _ in range(7):
    T["css"] = re.sub(r'\n\s*body\[data-site="(?:youtube|tiktok|snapchat|instagram|facebook|reddit|twitch|x)"\]\s*\{\s*--pc:[^}]*\}[^\n]*', "", T["css"], count=1)
rep("css", "CSS: .tg position:relative", ".tgs { display:flex; flex-wrap:wrap; gap:6px; } .tg input {",
    ".tgs { display:flex; flex-wrap:wrap; gap:6px; } .tg { position:relative; } .tg input {")
T["css"] += r'''

/* ---------- story picker + file browser ---------- */
.pickbar { display:flex; gap:6px; flex-wrap:wrap; }
.pgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(92px,1fr)); gap:8px; margin:12px 0 0; }
.ptile { position:relative; aspect-ratio:9/16; padding:0; border:2px solid var(--line); border-radius:14px; overflow:hidden; background:var(--card2); color:var(--text); font:inherit; cursor:pointer; transition:.2s var(--ease); }
.ptile img { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; transition:opacity .2s; }
.ptile .pph { position:absolute; inset:0; display:grid; place-items:center; font-size:1.8rem; }
.ptile i { position:absolute; font-style:normal; font-size:.66rem; font-weight:800; padding:1px 7px; border-radius:99px; background:rgba(0,0,0,.68); color:#fff; }
.ptile .pn { left:6px; top:6px; } .ptile .pk { right:6px; top:6px; } .ptile .pt { right:6px; bottom:6px; }
.ptile .ps { left:6px; bottom:6px; background:var(--ok); color:#06281c; }
.ptile.on { border-color:var(--pc); box-shadow:0 0 0 3px color-mix(in srgb,var(--pc) 35%,transparent); }
.ptile:not(.on) img, .ptile:not(.on) .pph { opacity:.4; }
.ptile:hover { transform:translateY(-2px); }
#fstat { margin:8px 2px 0; }
#flist .rimg { width:46px; height:46px; }
'''

# =========================================================================== #
# HTML
# =========================================================================== #
rep("html", "HTML: 'Choose stories' button in the Snapchat card",
    '<div class="chips" id="snapchips"></div></div>',
    '<div class="chips" id="snapchips"></div></div>\n      <p class="pnote"><button class="linkbtn" id="pickStoriesBtn" style="padding:0">☑ Choose which stories…</button></p>')
rep("html", "HTML: 'Browse saved files' button",
    '<button class="linkbtn" id="toggleBatchBtn">📋 Paste many links at once</button>',
    '<button class="linkbtn" id="toggleBatchBtn">📋 Paste many links at once</button>\n    <button class="linkbtn" id="openFilesBtn" style="margin-left:12px">📁 Browse saved files</button>')
rep("html", "HTML: shortcut row for F",
    '<div class="krow"><span>Close any popup</span>',
    '<div class="krow"><span>Saved files</span><span><kbd>F</kbd></span></div>\n  <div class="krow"><span>Close any popup</span>')
rep("html", "HTML: picker + files modals", '<div id="confetti" aria-hidden="true"></div>',
    '''<!-- story picker -->
<div class="overlay" id="pmodal" role="dialog" aria-modal="true" aria-label="Choose stories"><div class="mcard sheet">
  <div class="shead"><h3 id="pktitle">☑ Choose stories</h3><button class="mini" id="closePickBtn" aria-label="Close">✕</button></div>
  <div class="pickbar"><button class="mini" id="pickAllBtn">All</button><button class="mini" id="pickNewBtn">Only not saved</button><button class="mini" id="pickNoneBtn">None</button></div>
  <div class="pgrid" id="pgrid"></div>
  <button class="btn primary wide" id="pickGoBtn">Download</button>
</div></div>
<!-- saved files -->
<div class="overlay" id="fmodal" role="dialog" aria-modal="true" aria-label="Saved files"><div class="mcard sheet">
  <div class="shead"><h3>📁 Saved files</h3><button class="mini" id="closeFilesBtn" aria-label="Close">✕</button></div>
  <div class="opt" id="fcats"></div>
  <p class="hint" id="fstat"></p>
  <div id="flist"></div>
</div></div>
<div id="confetti" aria-hidden="true"></div>''')
rep("html", "HTML: remove leftover comments", "<!-- Notice the /assets/ path here -->\n", "", count=0)

# =========================================================================== #
# JS
# =========================================================================== #
rep("js", "JS: guard sessionStorage read",
    "clipSeen: sessionStorage.getItem('hub_clip_seen') || '',",
    "clipSeen: (() => { try { return sessionStorage.getItem('hub_clip_seen') || ''; } catch { return ''; } })(),")
rep("js", "JS: missing tools shown red", "class=\"${ok ? 'vok' : 'vok'}\"", "class=\"${ok ? 'vok' : 'vbad'}\"")
rep("js", "JS: KNOWN regex no longer matches box.com etc.", "twitter|x\\.com|", "twitter|(?:^|[./])x\\.com|")
rep("js", "JS: watch() single-loop guard",
    "async function watch() {\n  clearTimeout(AppState.wt);",
    "async function watch() {\n  if (AppState.watching) return;\n  AppState.watching = true;\n  clearTimeout(AppState.wt);\n"
    "  try { await watchBody(); } finally { AppState.watching = false; AppState.wt = setTimeout(watch, 1500); }\n}\n"
    "async function watchBody() {")
rex("js", "JS: remove inner re-schedule",
    r"\n  AppState\.wt = setTimeout\(watch, 1500\);\n\}\ndocument\.addEventListener\('visibilitychange'", "\n}\ndocument.addEventListener('visibilitychange'")
rep("js", "JS: dedupe results by job id",
    "if (!gset.has(id)) AppState.results.push(j);",
    "if (!gset.has(id) && !AppState.results.some(x => x.id === j.id)) AppState.results.push(j);")
rep("js", "JS: Esc closes every popup",
    "closeMaint(); closeSettings(); closeKeys(); return; }",
    "closeMaint(); closeSettings(); closeKeys(); closePicker(); closeFiles();\n"
    "    if ($('modal').classList.contains('open')) { AppState.resultOpen ? closeResult() : hideProgress(); } return; }")
rep("js", "JS: F shortcut", "else if (e.key === '?') $('kmodal').classList.add('open');",
    "else if (k === 'f') openFiles();\n  else if (e.key === '?') $('kmodal').classList.add('open');")
rex("js", "JS: applyUi() again after swatches exist",
    r"initDomInjects\(\);\s*initLooksAndPresets\(\);", "initDomInjects();\n    applyUi();\n    initLooksAndPresets();")
rex("js", "JS: bind picker/files", r"initStaticEventBindings\(\);\s*loadSettings\(\);",
    "initStaticEventBindings();\n    initPickFilesBindings();\n    loadSettings();")

NEW_JS = r'''/* ---------- story picker (Snapchat) ---------- */
const pick = {user:'', stories:[], sel:new Set()};
function closePicker() { $('pmodal').classList.remove('open'); }
async function openPicker() {
  const user = AppState.snapUser; if (!user) return;
  $('pmodal').classList.add('open'); $('pktitle').textContent = '☑ @' + user;
  $('pgrid').innerHTML = '<div class="empty" style="grid-column:1/-1">Loading stories…</div>'; $('pickGoBtn').disabled = true;
  const r = await api('/snap-stories?user=' + encodeURIComponent(user));
  if (!r.success) { $('pgrid').innerHTML = `<div class="empty" style="grid-column:1/-1"><b>😕</b>${esc(r.error || r.message || 'Could not load stories.')}</div>`; return; }
  pick.user = user; pick.stories = r.stories; pick.sel = new Set(r.stories.filter(s => !s.saved).map(s => s.id)); renderPicker();
}
function renderPicker() {
  $('pgrid').innerHTML = pick.stories.map(s => {
    const ico = s.kind === 'video' ? '🎬' : '🖼', t = s.time ? new Date(s.time * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) : '';
    return `<button class="ptile${pick.sel.has(s.id) ? ' on' : ''}" data-sid="${esc(s.id)}" aria-pressed="${pick.sel.has(s.id)}">`
      + (s.thumb ? `<img loading="lazy" alt="" src="/snap-thumb?user=${encodeURIComponent(pick.user)}&id=${encodeURIComponent(s.id)}">` : `<span class="pph">${ico}</span>`)
      + `<i class="pn">${s.index}</i><i class="pk">${ico}</i>${s.saved ? '<i class="ps">saved</i>' : ''}${t ? `<i class="pt">${esc(t)}</i>` : ''}</button>`;
  }).join('');
  const n = pick.sel.size; $('pickGoBtn').disabled = !n; $('pickGoBtn').textContent = n ? `⬇ Download ${n} ${n === 1 ? 'story' : 'stories'}` : 'Pick at least one';
}
function pickGo() {
  if (!pick.sel.size) return; closePicker();
  run({action_type:'snapchat', target:pick.user, options:getOpts(), only:[...pick.sel]}, 'Fetching picked stories…');
}

/* ---------- saved files browser ---------- */
let fcat = '', fRows = [];
const CATS = [['', 'All'], ['youtube', 'YouTube'], ['tiktok', 'TikTok'], ['snapchat', 'Snapchat'], ['instagram', 'Instagram'], ['general', 'Other']];
const fileUrl = f => `/files/${encodeURIComponent(f.category)}/${f.name.split('/').map(encodeURIComponent).join('/')}`;
function closeFiles() { $('fmodal').classList.remove('open'); }
function openFiles() { $('fmodal').classList.add('open'); renderCats(); loadFiles(); }
function renderCats() { $('fcats').innerHTML = CATS.map(([k, n]) => `<button data-cat="${k}" class="${k === fcat ? 'on' : ''}">${n}</button>`).join(''); }
async function loadFiles() {
  $('flist').innerHTML = '<div class="empty">Loading…</div>';
  const r = await api('/list-files' + (fcat ? '?category=' + fcat : ''));
  if (!r.success) { $('flist').innerHTML = '<div class="empty"><b>😕</b>Could not load the file list.</div>'; return; }
  fRows = r.files; $('fstat').textContent = `${r.files.length} files · ${r.total_h} used · ${r.free_h} free on the server`;
  if (!fRows.length) { $('flist').innerHTML = '<div class="empty"><b>📭</b>Nothing saved here yet.</div>'; return; }
  const img = f => /\.(gif|webp|jpe?g|png)$/i.test(f), shown = fRows.slice(0, 300);
  $('flist').innerHTML = shown.map((f, i) => {
    const u = fileUrl(f), ico = /\.(mp4|mkv|webm|mov|m4v)$/i.test(f.name) ? '🎬' : /\.(mp3|m4a|opus|flac|wav|ogg)$/i.test(f.name) ? '🎵' : '📄';
    return `<div class="file" style="--i:${Math.min(i, 8)}">${img(f.name) ? `<img class="rimg" loading="lazy" alt="" src="${esc(u)}">` : `<span class="fav">${ico}</span>`}`
      + `<div class="fmain"><div class="fname">${esc(f.name)}</div><div class="fmeta">${esc(f.category)} · ${esc(f.size_h)} · ${esc(new Date(f.modified * 1000).toLocaleString())}</div></div>`
      + `<div class="fctl"><a class="mini" href="${esc(u)}" target="_blank" rel="noopener">Open</a><a class="mini" href="${esc(u)}?dl=1">Save</a><button class="mini del" data-fi="${i}" aria-label="Delete">✕</button></div></div>`;
  }).join('') + (fRows.length > 300 ? `<div class="empty">Showing the newest 300 of ${fRows.length}. Pick a category to narrow it down.</div>` : '');
}
async function delFile(i) {
  const f = fRows[i]; if (!f || !confirm('Delete this file from the server?\n\n' + f.name)) return;
  const r = await api(fileUrl(f), {method:'DELETE'}); toast(r.success ? 'Deleted' : (r.message || 'Could not delete')); loadFiles();
}

function initPickFilesBindings() {
  safeBind('pickStoriesBtn', 'click', openPicker);
  safeBind('closePickBtn', 'click', closePicker);
  safeBind('pickGoBtn', 'click', pickGo);
  safeBind('pickAllBtn', 'click', () => { pick.sel = new Set(pick.stories.map(s => s.id)); renderPicker(); });
  safeBind('pickNewBtn', 'click', () => { pick.sel = new Set(pick.stories.filter(s => !s.saved).map(s => s.id)); renderPicker(); });
  safeBind('pickNoneBtn', 'click', () => { pick.sel.clear(); renderPicker(); });
  $('pgrid').addEventListener('click', e => {
    const t = e.target.closest('.ptile'); if (!t) return;
    const id = t.dataset.sid; pick.sel.has(id) ? pick.sel.delete(id) : pick.sel.add(id); renderPicker();
  });
  safeBind('openFilesBtn', 'click', openFiles);
  safeBind('closeFilesBtn', 'click', closeFiles);
  $('fcats').addEventListener('click', e => { const b = e.target.closest('button[data-cat]'); if (b) { fcat = b.dataset.cat; renderCats(); loadFiles(); } });
  $('flist').addEventListener('click', e => { const b = e.target.closest('button[data-fi]'); if (b) delFile(+b.dataset.fi); });
  [['pmodal', closePicker], ['fmodal', closeFiles]].forEach(([id, fn]) => { const el = $(id); el.addEventListener('click', e => { if (e.target === el) fn(); }); });
}

'''
rep("js", "JS: story picker + file browser code", "/* ---------- Safe Bootup ---------- */", NEW_JS + "/* ---------- Safe Bootup ---------- */")

# =========================================================================== #
# PYTHON
# =========================================================================== #
rex("py", "PY: delete dead run_snapchat_watchlist()",
    r"def run_snapchat_watchlist\(username\):.*?return total_new_stories\n\n", "", flags=re.S)
rep("py", "PY: unique temp file per thread (jobs)", 'tmp = JOBS_FILE.with_suffix(".tmp")',
    'tmp = JOBS_FILE.with_suffix(f".{threading.get_ident()}.tmp")')
rep("py", "PY: unique temp file per thread (state files)", 'tmp = path.with_suffix(".tmp")\n        tmp.write_text(json.dumps(data))',
    'tmp = path.with_suffix(f".{threading.get_ident()}.tmp")\n        tmp.write_text(json.dumps(data))')
rep("py", "PY: filters pass videos with missing metadata (>=? / <=?)", '"duration >="', '"duration >=?"')
rep("py", "PY: duration max", '"duration <="', '"duration <=?"')
rep("py", "PY: views", '"view_count >="', '"view_count >=?"')
rep("py", "PY: TikTok fallback only for photo posts / 'no video' errors",
    'if category == "tiktok" and shutil.which("gallery-dl"):',
    'if (category == "tiktok" and shutil.which("gallery-dl")\n                and ("/photo/" in url or re.search(r"no video|unsupported url", msg, re.I))):')
rex("py", "PY: Instagram wording only for Instagram jobs",
    r'return finish_err\(job, friendly_ig_error\(last_line\((.*?)"gallery-dl failed"\)\)\)',
    r'_m = last_line(\1"gallery-dl failed")\n            return finish_err(job, friendly_ig_error(_m) if ig else _m)', flags=re.S)
rep("py", "PY: fix 'Advanced → Items' wording", "set a range under Advanced → Items to change", "set a range under Customize → More options → Items to change")
rep("py", "PY: effects counter (i/N)", "{i + 1}/{len(media)}", "{media.index(rel) + 1}/{len(media)}", count=0)
rep("py", "PY: import hmac", "import json\nimport logging", "import hmac\nimport json\nimport logging")
rep("py", "PY: flask imports",
    "from flask import Flask, jsonify, render_template, request, send_file, send_from_directory",
    "from flask import Flask, jsonify, redirect, render_template, request, send_file, send_from_directory")
rep("py", "PY: optional access token (set HUB_TOKEN)", "@app.after_request\ndef security_headers(resp):", '''HUB_TOKEN = os.environ.get("HUB_TOKEN", "").strip()      # empty = no lock (same as before)


def _token_ok(v):
    return bool(v) and hmac.compare_digest(str(v), HUB_TOKEN)


@app.before_request
def require_token():
    """If HUB_TOKEN is set, every request needs it. Open the site once with /?token=YOUR_TOKEN - it is then kept in a cookie."""
    if not HUB_TOKEN or request.path.startswith("/assets/"):
        return None
    if _token_ok(request.cookies.get("hub_token")) or _token_ok(request.headers.get("X-Hub-Token")):
        return None
    if _token_ok(request.args.get("token")):
        resp = redirect(request.path)
        resp.set_cookie("hub_token", HUB_TOKEN, httponly=True, samesite="Lax", max_age=60 * 60 * 24 * 365)
        return resp
    return jsonify(success=False, message="Locked. Open the site once with ?token=YOUR_TOKEN."), 401


@app.after_request
def security_headers(resp):''')

for k, p in FILES.items():
    p.write_text(T[k], encoding="utf-8")
print(f"\nDone. {bad} fix(es) skipped." + (" Send me the SKIPPED lines and I'll adjust them." if bad else " Restart the app and hard-refresh the browser."))
