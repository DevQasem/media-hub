console.log("🚀 Media Hub JS Loading...");

const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

/* ---------- State Management ---------- */
const AppState = {
  timer: null,
  seq: 0,
  mode: null,
  snapUser: '',
  videoUrl: '',
  chanUrl: '',
  chanName: '',
  hadLink: false,
  noPreview: false,
  isSlideshow: false,
  clipPick: null,
  clipBusy: false,
  clipState: 'unknown',
  clipSeen: (() => { try { return sessionStorage.getItem('hub_clip_seen') || ''; } catch { return ''; } })(),
  lastRun: null,
  viewing: null,
  results: [],
  resultOpen: false,
  wt: null,
  watching: false,
  lastRunning: []
};

/* one dot on the Settings buttons: shows when the Instagram login expired or a server tool is missing */
const SX = {igExp:false, miss:false, pane:'look'};
function updDots() { document.querySelectorAll('[data-sdot]').forEach(el => el.classList.toggle('show', SX.igExp || SX.miss)); }

/* offline banner */
let netFails = 0;
function netState(ok) {
  const bar = $('netbar');
  if (ok) { if (netFails >= 2) toast('Back online ✓'); netFails = 0; bar.classList.remove('show'); return; }
  netFails++;
  if (netFails >= 2 || navigator.onLine === false) {
    bar.textContent = navigator.onLine === false ? '📡 You are offline - waiting for a connection…' : '⚠ Can’t reach Media Hub (is the server running or restarting?) - retrying…';
    bar.classList.add('show');
  }
}
async function api(url, opts) {
  try { const r = await (await fetch(url, opts)).json(); netState(true); return r; }
  catch { netState(false); return { success:false, message:"Can't reach the server.", error:"Can't reach the server." }; }
}
const post = (u, b) => api(u, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(b)});
function toast(t) { const el = $('toast'); el.textContent = t; el.classList.add('show'); clearTimeout(toast.t); toast.t = setTimeout(() => el.classList.remove('show'), 2600); }

/* ---------- sites: name, colour, button icon ---------- */
const SVG = d => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
const BRANDS = {
  tiktok:   {name:'TikTok',      color:'#25f4ee', re:/tiktok/i,                        icon:SVG('<path d="M14 3v11.2a3.4 3.4 0 1 1-3.4-3.4"/><path d="M14 3c.4 2.6 2.1 4.2 4.8 4.5"/>')},
  youtube:  {name:'YouTube',     color:'#ff4d5e', re:/youtu\.?be|youtube/i,            icon:SVG('<rect x="2.5" y="5.5" width="19" height="13" rx="4" fill="currentColor" stroke="none"/><path d="M10 9.2l5 2.8-5 2.8z" fill="#ff0033" stroke="none"/>')},
  instagram:{name:'Instagram',   color:'#f472b6', re:/instagram/i,                     icon:SVG('<rect x="3.5" y="3.5" width="17" height="17" rx="5"/><circle cx="12" cy="12" r="3.8"/><circle cx="17" cy="7" r=".6" fill="currentColor"/>')},
  snapchat: {name:'Snapchat',    color:'#fffc00', re:/snapchat/i,                      icon:'👻'},
  x:        {name:'X / Twitter', color:'#e7e9ea', re:/(^|[./])(twitter|x)\.com/i,      icon:SVG('<path d="M5 5l14 14M19 5L5 19"/>')},
  facebook: {name:'Facebook',    color:'#60a5fa', re:/facebook|fb\.watch/i,            icon:'f'},
  reddit:   {name:'Reddit',      color:'#fb923c', re:/reddit/i,                        icon:'👽'},
  twitch:   {name:'Twitch',      color:'#a78bfa', re:/twitch/i,                        icon:'🎮'},
  vimeo:    {name:'Vimeo',       color:'#38bdf8', re:/vimeo/i,                         icon:'▶'},
  other:    {name:'Other site',  color:'#9aa0c3', re:/$^/,                             icon:'⬇'},
};
const detect = url => { const k = Object.keys(BRANDS).find(k => BRANDS[k].re.test(url)) || 'other'; return {key:k, name:BRANDS[k].name, color:BRANDS[k].color}; };
const setColor = c => document.documentElement.style.setProperty('--pc', c);

/* ---------- appearance (theme, accent, text size, compact, confetti) ---------- */
const UI_KEY = 'hub_ui', UI_DEF = {theme:'dark', accent:'auto', size:'m', density:'comfy', confetti:'1', palette:'classic'};
const ACCENTS = {violet:'#8b5cf6', cyan:'#22d3ee', pink:'#f472b6', green:'#34d399', orange:'#fb923c', red:'#fb7185'};
let ui = Object.assign({}, UI_DEF);
try { Object.assign(ui, JSON.parse(localStorage.getItem(UI_KEY) || '{}')); } catch {}
const accentOr = c => ui.accent !== 'auto' && ACCENTS[ui.accent] ? ACCENTS[ui.accent] : c;

function applyUi() {
  const d = document.documentElement; d.dataset.theme = ui.theme; d.dataset.size = ui.size; d.dataset.density = ui.density; d.dataset.palette = ui.palette || 'classic';
  const light = ui.theme === 'light' || (ui.theme === 'auto' && window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches);
  d.dataset.scheme = light ? 'light' : 'dark';
  if($('themeMeta')) $('themeMeta').content = (getComputedStyle(d).getPropertyValue('--bg') || '').trim() || (light ? '#f1f4ff' : '#07080f');
  document.querySelectorAll('[data-look]').forEach(b => b.classList.toggle('on', (ui.palette || 'classic') === b.dataset.look));
  document.querySelectorAll('[data-ui]').forEach(b => { const [k, v] = b.dataset.ui.split(':'); b.classList.toggle('on', ui[k] === v); });
  if($('confettiOn')) $('confettiOn').checked = ui.confetti === '1';
  setColor(accentOr((BRANDS[document.body.dataset.site] || BRANDS.other).color));
}
try { matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => { if (ui.theme === 'auto') applyUi(); }); } catch {}
function setUi(k, v) { ui[k] = v; try { localStorage.setItem(UI_KEY, JSON.stringify(ui)); } catch {} applyUi(); }
function setBrand(key) {
  const b = BRANDS[key] || BRANDS.other; document.body.dataset.site = key; setColor(accentOr(b.color));
  $('goIcon').innerHTML = b.icon; $('badge').textContent = key === 'other' ? 'Any link' : b.name;
  $('badge').classList.remove('pop'); void $('badge').offsetWidth; $('badge').classList.add('pop');
}

/* Initialization routines for dynamic DOM parts */
function initDomInjects() {
    // 1. Inject the accent colors
    if($('accents')) {
        $('accents').insertAdjacentHTML('beforeend', Object.entries(ACCENTS).map(([k, c]) => `<button class="sw" style="--c:${c}" data-ui="accent:${k}" aria-label="Accent ${k}"></button>`).join(''));
    }

    // 2. Inject the supported platforms
    if($('platforms')) {
        $('platforms').innerHTML = ['tiktok','youtube','instagram','snapchat','x','facebook','reddit','twitch','vimeo'].map((k, i) => `<span class="plat" style="--c:${BRANDS[k].color};--i:${i}">${esc(BRANDS[k].name)}</span>`).join('');
    }

    // 3. Bind click events to ALL settings buttons (Theme, Accent, Size, Layout)
    document.querySelectorAll('button[data-ui]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const [k, v] = e.currentTarget.dataset.ui.split(':');
            if (k && v) {
                setUi(k, v);
            }
        });
    });
}

/* ---------- smart box: video links AND Snapchat ---------- */
const SNAP_RE = /(?:story\.snapchat\.com\/(?:s\/|@)|snapchat\.com\/(?:add\/|@|s\/))@?([A-Za-z0-9._-]{3,32})/i;
const KNOWN = /youtu|tiktok|instagram|twitter|(?:^|[./])x\.com|facebook|fb\.watch|reddit|twitch|snapchat|vimeo|dailymotion/i;
const USER_RE = /^[A-Za-z0-9._-]{3,32}$/;
const isUrl = u => /^https?:\/\/\S+\.\S+/i.test(u);
const isInsta = u => /^https?:\/\/([a-z0-9-]+\.)*instagram\.com(\/|$|\?)/i.test(u);

function classify(v) {
  if (!v) return null;

  // Auto-repair broken TikTok links missing the domain
  if (v.startsWith('@') && (v.includes('/video/') || v.includes('/photo/'))) {
    v = 'https://www.tiktok.com/' + v;
  }

  if (v[0] === '@') return USER_RE.test(v.slice(1)) ? {kind:'snap', user:v.slice(1)} : {kind:'bad'};
  if (/^https?:\/\//i.test(v) || v.includes('/') || KNOWN.test(v)) {
    const url = /^https?:\/\//i.test(v) ? v : 'https://' + v, m = url.match(SNAP_RE);
    if (m) return {kind:'snap', user:m[1]};
    return isUrl(url) ? {kind:'video', url} : {kind:'bad'};
  }
  return USER_RE.test(v) ? {kind:'snap', user:v} : {kind:'bad'};
}

function setHint(t, err) { $('hint').textContent = t; $('hint').classList.remove('err'); if (err) { void $('hint').offsetWidth; $('hint').classList.add('err'); } }

function onUrlInput() {
  clearTimeout(AppState.timer); AppState.seq++;
  const v = $('url').value.trim();

  AppState.isSlideshow = false;

  if (v && !AppState.hadLink) setOpts({});
  AppState.hadLink = !!v;
  $('clearBtn').style.display = v ? '' : 'none'; $('pasteBtn').style.display = v ? 'none' : '';
  ['preview', 'snapbox', 'igbox'].forEach(id => $(id).classList.remove('show'));
  ['actions', 'watchrow'].forEach(id => $(id).classList.remove('show'));
  AppState.chanUrl = ''; setHint(''); AppState.mode = null; AppState.noPreview = false; AppState.liveUser = null; liveWint(false); $('platforms').style.display = v ? 'none' : '';
  if (v && document.body.dataset.tab === 'watch') setTab('home');
  if (v) hideClip(); else { closeCustom(); setBrand('other'); setOpts({}); }
  const c = classify(v);
  if (!c || c.kind === 'bad') { if (c) setHint('Paste a full link, or a Snapchat username'); refreshOpts(); return; }
  c.kind === 'snap' ? snapPreview(c.user) : videoPreview(c.url);
  refreshOpts();
}

function snapPreview(user) {
  AppState.mode = 'snap'; AppState.snapUser = user; setBrand('snapchat'); $('snapname').textContent = '@' + user;
  const chips = extra => $('snapchips').innerHTML = ['👻 Public stories', '📁 snapchat/', extra].filter(Boolean).map(c => `<span class="chip">${esc(c)}</span>`).join('');
  chips('⏳ Counting stories…'); $('snapbox').classList.add('show'); $('actions').classList.add('show');
  $('watchTxt').textContent = '👁 Auto-download new stories'; $('watchrow').classList.add('show'); refreshOpts();
  const mine = AppState.seq;
  AppState.timer = setTimeout(async () => {
    const r = await api('/snap-info?user=' + encodeURIComponent(user));
    if (mine !== AppState.seq) return;
    chips(r.success && r.count ? `📖 ${r.count} ${r.count === 1 ? 'story' : 'stories'} found` : 'Story count unavailable');
  }, 600);
}

const loginChip = l => !l || !l.saved ? ['🔑 No login saved', 'bad'] : l.session_expired ? ['🔑 Login expired', 'bad'] : !l.has_session ? ['🔑 Login has no sessionid', 'bad'] : ['🔑 Login saved ✔', 'ok'];
const chipHtml = c => { const t = Array.isArray(c) ? c : [c, '']; return `<span class="chip ${t[1]}">${esc(t[0])}</span>`; };

function sxPane(n) {
  SX.pane = n;
  document.querySelectorAll('#sxnav [data-sec]').forEach(b => b.classList.toggle('on', b.dataset.sec === n));
  document.querySelectorAll('.sxpane').forEach(s => s.classList.toggle('on', s.dataset.pane === n));
  if (n === 'tools') loadVersion(); if (n === 'ig') loadIg();
}
function openSettings(sec) { $('smodal').classList.add('open'); sxPane(typeof sec === 'string' ? sec : SX.pane); }
function setTab(t) {
  document.body.dataset.tab = t; $('tabbar').style.setProperty('--ti', ({home:0, live:1, watch:2})[t] || 0);
  if (t === 'live') { lvOpen(); setColor('#25f4ee'); } else { lvClose(); if (document.body.dataset.wasLive) applyUi(); }
  document.body.dataset.wasLive = t === 'live' ? '1' : '';
  document.querySelectorAll('.tabbar [data-tab]').forEach(b => b.classList.toggle('on', b.dataset.tab === t));
  document.querySelectorAll('#rail [data-nav]').forEach(b => b.classList.toggle('on', b.dataset.nav === t));
  if (window.scrollTo) window.scrollTo({top:0, behavior:'smooth'});
}
function closeKeys() { $('kmodal').classList.remove('open'); }
function focusUrl() { setTab('home'); $('url').focus(); if ($('url').select) $('url').select(); }
function closeSettings() { $('smodal').classList.remove('open'); }
function openIgSettings() { openSettings('ig'); }

function igCard(r, d) {
  AppState.mode = 'ig'; $('preview').classList.remove('show'); $('watchrow').classList.remove('show'); setHint(''); setBrand('instagram');
  const kinds = {post:'📸 Post (photos / carousel)', stories:'📖 All stories of this user', highlight:'⭐ Highlight', profile:'👤 Whole profile'}, ig = r.ig || {};
  $('igtitle').textContent = r.title;
  $('igwhatrow').style.display = ig.kind === 'profile' ? '' : 'none';
  $('igchips').innerHTML = [kinds[ig.kind] || 'Instagram', '📁 instagram/', ig.gallery_dl ? ['🧰 gallery-dl ✔', 'ok'] : ['🧰 gallery-dl missing ✖', 'bad'], loginChip(ig.login)].map(chipHtml).join('');
  const bad = [];
  if (!ig.gallery_dl) bad.push('gallery-dl is not installed on the server (see the 🔧 Maintenance button).');
  if (!ig.login || !ig.login.saved || ig.login.session_expired || !ig.login.has_session) bad.push('Instagram needs your login: open Settings → Instagram login.');
  $('ignote').innerHTML = (bad.length ? esc(bad.join(' ')) + ' <button class="linkbtn" style="padding:0" id="ignoteSettingsBtn">Open Settings</button><br>' : '')
    + (ig.kind === 'profile' ? esc(`Takes the first ${ig.cap || 30} items. For a different amount, open Customize → More options → Items and type a range such as 1-60.`) : '');

  if ($('ignoteSettingsBtn')) $('ignoteSettingsBtn').addEventListener('click', openIgSettings);

  $('igbox').classList.add('show'); $('actions').classList.add('show'); refreshOpts();
}

/* ---------- playlist / channel picker (the Arroxy-style "choose videos" list) ---------- */
const isListUrl = u => /[?&]list=|youtube\.com\/(?:playlist|@[\w.-]+|channel\/|c\/|user\/)|tiktok\.com\/@[\w.-]+\/?(?:[?#]|$)|\/(?:playlists?|channel|channels|user|users|series|collections?)\//i.test(u);
const pl = {url:'', items:[], sel:new Set(), q:''};
function closePl() { $('plmodal').classList.remove('open'); }
async function openPl() {
  pl.url = AppState.videoUrl; if (!pl.url) return;
  $('plmodal').classList.add('open'); $('pltitle').textContent = '☑ Choose videos'; $('plFilter').value = ''; pl.q = '';
  $('plist').innerHTML = '<div class="empty">Reading the list… (can take up to a minute for big channels)</div>'; $('plGoBtn').disabled = true;
  const r = await post('/playlist-info', {url: pl.url});
  if (!r.success) { $('plist').innerHTML = `<div class="empty"><b>😕</b>${esc(r.error || r.message || 'Could not read the list.')}</div>`; return; }
  pl.items = r.entries; pl.sel = new Set(); $('pltitle').textContent = '☑ ' + (r.title || 'Choose videos'); renderPl();
}
function plShown() { const q = pl.q.toLowerCase(); return q ? pl.items.filter(e => e.title.toLowerCase().includes(q)) : pl.items; }
function renderPl() {
  const shown = plShown();
  $('plist').innerHTML = shown.length ? shown.map(e => `<button class="prow${pl.sel.has(e.index) ? ' on' : ''}" data-i="${e.index}" aria-pressed="${pl.sel.has(e.index)}">`
    + `<span class="pcheck" aria-hidden="true"></span>` + (e.thumb ? `<img loading="lazy" alt="" referrerpolicy="no-referrer" src="${esc(e.thumb)}">` : '<span class="pth">🎬</span>')
    + `<span class="pmain"><b>${esc(e.title)}</b><small>#${e.index}${e.duration ? ' · ' + fmtDur(e.duration) : ''}</small></span></button>`).join('')
    : '<div class="empty">No video matches that filter.</div>';
  const n = pl.sel.size; $('plGoBtn').disabled = !n; $('plGoBtn').textContent = n ? `⬇ Download ${n} ${n === 1 ? 'video' : 'videos'}` : 'Pick at least one video';
}
async function plGo() {
  if (!pl.sel.size) return;
  const options = Object.assign(getOpts(), {name:'', playlist:'single', items:''}), items = pl.items.filter(e => pl.sel.has(e.index)).map(e => ({action_type:'url', target:e.url, options}));
  closePl(); const r = await post('/batch', {items});
  if (!r.success) return toast('Could not start the downloads');
  setTracked([...tracked(), ...r.job_ids]); setGroups([...groups(), r.job_ids]);
  toast(`${r.job_ids.length} queued` + (r.errors && r.errors.length ? ` · ${r.errors.length} skipped` : '')); watch();
}

const LIVE_RE = /^https?:\/\/(?:www\.|m\.)?tiktok\.com\/@([A-Za-z0-9._-]{2,24})\/live\b/i;
function videoPreview(url) {
  AppState.mode = 'video'; AppState.videoUrl = url;
  const lm = url.match(LIVE_RE); AppState.liveUser = lm ? lm[1] : null; AppState.liveOn = null; liveWint(!!lm);
  const d = detect(url); setBrand(d.key); $('actions').classList.add('show');
  AppState.timer = setTimeout(async () => {
    const mine = ++AppState.seq;
    setHint(`Reading ${d.name} link…`); $('preview').classList.add('show'); $('plnote').style.display = 'none';
    $('thumb').className = 'thumb loading'; setThumb('');
    $('ptitle').textContent = 'Loading details…'; $('chips').innerHTML = ''; $('dur').style.display = 'none';
    $('siteBadge').textContent = d.name; $('siteBadge').style.background = d.color;

    const r = await post('/preview', {url});
    if (mine !== AppState.seq) return;
    if (!r.success) { $('preview').classList.remove('show'); AppState.noPreview = true; refreshOpts(); return setHint((r.error || 'No preview for this link.') + ' You can still try the download.', true); }
    if (r.gallery) return igCard(r, d);
    if (r.live) return liveCard(r);

    setHint(''); $('siteBadge').textContent = r.site || d.name;
    AppState.chanUrl = r.channel_url || ''; AppState.chanName = r.uploader || '';
    if (AppState.chanUrl) { $('watchTxt').textContent = '👁 Auto-download new posts from ' + (AppState.chanName || 'this creator'); $('watchrow').classList.add('show'); }

    // --- TIKTOK SLIDESHOW & THUMBNAIL FIX ---
    if (!r.thumbnail) {
      $('preview').classList.add('no-thumb');
      $('thumb').style.display = 'none';
    } else {
      $('preview').classList.remove('no-thumb');
      $('thumb').className = 'thumb';
      $('thumb').style.display = '';
      setThumb(r.thumbnail);
    }

    if (url.includes('/photo/') || r.type === 'slideshow' || r.type === 'image' || r.is_slideshow) {
      AppState.isSlideshow = true;
    }
    // ----------------------------------------

    paintPreview(getOpts());
    $('ptitle').textContent = r.title;
    if (r.duration) { $('dur').textContent = fmtDur(r.duration); $('dur').style.display = ''; }

    const chips = [];
    if (r.quality && r.quality.height) { const q = r.quality; chips.push([`🎞 ${q.height >= 2160 ? '4K' : q.height + 'p'}${q.fps >= 50 ? ' · ' + q.fps + ' fps' : ''}${q.hdr ? ' · HDR' : ''}`, 'ok']); }
    $('plnote').style.display = isListUrl(url) ? '' : 'none';
    if (r.uploader) chips.push('👤 ' + r.uploader);
    if (r.duration) chips.push(`⏱ ${r.duration}s`);
    if (r.views != null) chips.push('👁 ' + new Intl.NumberFormat('en', {notation:'compact'}).format(r.views));
    if (r.upload_date && r.upload_date.length === 8) chips.push('📅 ' + new Date(`${r.upload_date.slice(0,4)}-${r.upload_date.slice(4,6)}-${r.upload_date.slice(6)}`).toLocaleDateString());
    chips.push('📁 ' + r.category + '/');
    if (r.ig) { chips.push(loginChip(r.ig.login)); chips.push('👁 Watching Instagram: not yet'); }
    $('chips').innerHTML = chips.map(chipHtml).join('');

    refreshOpts();
  }, 450);
}

function go() { if (AppState.mode === 'snap') startSnap(); else if (AppState.mode === 'video' || AppState.mode === 'ig') startVideo(false); }
function fmtDur(s) { const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = Math.floor(s % 60); return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0'); }

async function pasteLink() {
  try { $('url').value = (await navigator.clipboard.readText()).trim(); onUrlInput(); }
  catch { toast('Your browser blocks the Paste button here - long-press the box and paste instead'); $('url').focus(); }
}

function clearLink() { $('url').value = ''; onUrlInput(); $('url').focus(); }

/* ---------- clipboard auto-detect ---------- */
const CLIP_KEY = 'hub_clip_on';
const clipEnabled = () => { try { return localStorage.getItem(CLIP_KEY) !== '0'; } catch { return true; } };

function pickClipLink(text) {
  text = String(text || '').trim(); if (!text || text.length > 2000) return null;
  const m = text.match(/https?:\/\/[^\s<>"']+/i);
  if (m) { const url = m[0].replace(/[)\].,;!?]+$/, ''); return KNOWN.test(url) && isUrl(url) ? url : null; }
  return /^@[A-Za-z0-9._-]{3,32}$/.test(text) ? text : null;
}
function hideClip() { if($('clipbar')) $('clipbar').classList.remove('show'); AppState.clipPick = null; }
function rememberClip(v) { AppState.clipSeen = v; try { sessionStorage.setItem('hub_clip_seen', v); } catch {} }
function dismissClip() { if (AppState.clipPick) rememberClip(AppState.clipPick); hideClip(); }
function useClip() { if (!AppState.clipPick) return; const v = AppState.clipPick; rememberClip(v); hideClip(); $('url').value = v; onUrlInput(); toast('Link ready - press the big button'); }

function showClip(v) {
  AppState.clipPick = v; let label;
  if (v[0] === '@') label = '👻 Snapchat user ' + v;
  else { let h = v; try { h = new URL(v).hostname.replace(/^www\./, ''); } catch {} label = '📋 Link in your clipboard: ' + h; }
  $('cliptxt').textContent = label; $('clipbar').classList.add('show');
}
function setClipStat(state) {
  AppState.clipState = state; const el = $('clipstat'); if (!el) return;
  el.textContent = state === 'ok' ? '✔ Your browser allows it - copy a link, then open or return to this page.'
    : state === 'insecure' ? '⚠ Your browser blocks clipboard reading on plain http:// pages.'
    : state === 'denied' ? '⚠ The browser did not allow reading the clipboard.'
    : state === 'off' ? 'Turned off.' : '';
}
async function checkClipboard() {
  if (AppState.clipBusy) return; AppState.clipBusy = true;
  try {
    if (!clipEnabled()) { hideClip(); return setClipStat('off'); }
    if (!(window.isSecureContext && navigator.clipboard && navigator.clipboard.readText)) { hideClip(); return setClipStat('insecure'); }
    if (document.hidden || $('url').value.trim() || $('modal').classList.contains('open')) return;
    let text; try { text = await navigator.clipboard.readText(); } catch { hideClip(); return setClipStat('denied'); }
    setClipStat('ok');
    const v = pickClipLink(text);
    if (!v || v === AppState.clipSeen || $('url').value.trim()) return hideClip();
    showClip(v);
  } finally { AppState.clipBusy = false; }
}
function setClipOn() { try { localStorage.setItem(CLIP_KEY, $('clipOn').checked ? '1' : '0'); } catch {} $('clipOn').checked ? checkClipboard() : (hideClip(), setClipStat('off')); }
document.addEventListener('visibilitychange', () => { if (!document.hidden) setTimeout(checkClipboard, 300); });
window.addEventListener('focus', () => setTimeout(checkClipboard, 300));

/* ---------- options (Customize) ---------- */
const OPT_DEFAULTS = {kind:'video', quality:'best', container:'mp4', compat:false, audio_fmt:'mp3', abr:'192', subs:false, autosubs:false, sub_langs:'en', sub_fmt:'srt',
  embed_subs:false, thumb:false, embed_thumb:false, meta:false, sponsor:false, desc:false, playlist:'single', items:'', clip_start:'', clip_end:'',
  date_after:'', date_before:'', min_dur:'', max_dur:'', min_views:'', max_mb:'', title_has:'', title_not:'',
  force:false, name:'', ig_what:'posts', fx_look:'none', fx_blur:'none', fx_sharpen:false, fx_mirror:'none', fx_rotate:'0', fx_speed:'1', fx_crop:'none', fx_scale:'none', fx_mute:false, fx_keep:true};
const PRESETS = {
  best:  ['🎬 Best video', {}],
  phone: ['📱 Phone friendly', {quality:'1080', compat:true}],
  music: ['🎵 Music MP3', {kind:'audio', abr:'320', embed_thumb:true, meta:true}],
  study: ['📚 Lecture + subs', {quality:'720', subs:true, autosubs:true, embed_subs:true, meta:true}],
  small: ['🪶 Small file', {quality:'480', max_mb:'200'}],
  photo: ['🖼 Photo / cover', {kind:'image'}],
  gif:   ['🎞 Quick GIF', {kind:'gif'}],
  sticker: ['🧩 Sticker', {kind:'sticker'}],
  c15:   ['✂️ First 15 s', {clip_start:'0', clip_end:'15'}],
  c30:   ['✂️ First 30 s', {clip_start:'0', clip_end:'30'}],
  vert:  ['📱 Vertical 9:16', {quality:'1080', fx_crop:'vertical', compat:true}],
};
let curKind = 'video';

function getOpts() {
  const o = {kind: curKind}, seen = {};
  document.querySelectorAll('[data-o]').forEach(el => {
    const v = el.type === 'checkbox' ? el.checked : el.value.trim();
    o[el.dataset.o] = el.type === 'checkbox' ? (seen[el.dataset.o] || v) : v; if (el.type === 'checkbox') seen[el.dataset.o] = o[el.dataset.o];
  });
  o.folder = saveFolder();
  return o;
}
function setOpts(p) {
  const o = Object.assign({}, OPT_DEFAULTS, p); curKind = o.kind;
  document.querySelectorAll('[data-o]').forEach(el => { const v = o[el.dataset.o]; if (el.type === 'checkbox') el.checked = !!v; else el.value = v ?? ''; });
  refreshOpts();
}
function applyPreset(name) {
  const cur = getOpts(), keep = {}; Object.keys(cur).filter(k => k.startsWith('fx_')).forEach(k => keep[k] = cur[k]);
  setOpts(name === 'reset' ? {} : Object.assign(keep, PRESETS[name][1])); toast(name === 'reset' ? 'Back to the normal download' : 'Preset: ' + PRESETS[name][0]); }
const isCustom = o => Object.keys(OPT_DEFAULTS).some(k => k !== 'name' && String(o[k]) !== String(OPT_DEFAULTS[k]));

function updateGo(o, lk) {
  if (!AppState.mode) return;
  let label, sub, icon = '';

  if (AppState.mode === 'snap') {
    label = 'Download all stories'; sub = 'Public stories · saved in snapchat/';
  }
  else if (AppState.liveUser) {
    label = AppState.liveOn === false ? 'Not live right now' : 'Record live';
    sub = AppState.liveOn === false ? 'Press Watch below to record when they go live' : AppState.liveOn ? `Until the live ends or you stop it · tiktok/${AppState.liveUser}/lives/` : 'Checking if they are live…';
    icon = '🔴';
  }
  else if (AppState.mode === 'ig') {
    label = 'Download from Instagram'; sub = 'Original quality · saved in instagram/';
  }
  else if (AppState.isSlideshow) {
    label = 'Download Photos';
    sub = 'Slideshow Archive (ZIP)';
    icon = '🖼️';
  }
  else {
    label = o.kind === 'subs' ? 'Download subtitles' : o.kind === 'audio' ? 'Download ' + o.audio_fmt.toUpperCase() : o.kind === 'image' ? 'Save the photo' : o.kind === 'gif' ? 'Make a GIF' : o.kind === 'sticker' ? 'Make a sticker' : 'Download';
    sub = o.kind === 'subs' ? `Subtitles only · ${o.sub_fmt.toUpperCase()} · ${o.sub_langs || 'en'}` : o.kind === 'audio' ? `Audio only${['flac','wav'].includes(o.audio_fmt) ? '' : ' · ' + o.abr + ' kbps'}` : o.kind === 'image' ? 'Cover / photo as JPG'
        : o.kind === 'gif' ? 'Looping GIF, 480 px' : o.kind === 'sticker' ? 'WhatsApp-size WebP' : `${o.quality === 'best' ? 'Best quality' : o.quality + 'p'} · ${o.container.toUpperCase()}`;

    if (o.kind === 'audio') icon = '🎵';
    if (o.kind === 'subs') icon = '💬';
    if (o.kind === 'image') icon = '🖼️';
  }

  if (o.fx_look !== 'none' && lk) sub += ' · ✨ ' + lk[1];
  if (AppState.noPreview && AppState.mode === 'video') sub = 'No preview - will try anyway · ' + sub;

  if (saveFolder()) sub += ' · 📂 ' + saveFolder();
  $('goLabel').textContent = label;
  $('goSub').textContent = sub;

  if (icon) {
      $('goIcon').textContent = icon;
  } else {
      $('goIcon').innerHTML = (BRANDS[document.body.dataset.site] || BRANDS.other).icon;
  }
}

function refreshOpts() {
  document.querySelectorAll('#kindseg button').forEach(b => b.classList.toggle('on', b.dataset.kind === curKind));
  const ctx = AppState.mode === 'snap' ? 'snap' : AppState.mode === 'ig' ? 'ig' : curKind;
  document.querySelectorAll('[data-show]').forEach(el => el.style.display = el.dataset.show.split(',').includes(ctx) ? '' : 'none');
  const o = getOpts(), lk = LOOKS.find(x => x[0] === o.fx_look);
  document.querySelectorAll('.look').forEach(b => b.classList.toggle('on', b.dataset.look === o.fx_look)); paintPreview(o);
  const fx = ['fx_look','fx_blur','fx_mirror','fx_crop','fx_scale'].filter(k => o[k] !== 'none').length + (o.fx_rotate !== '0') + (o.fx_speed !== '1');
  const extras = ['subs','autosubs','embed_subs','thumb','embed_thumb','meta','sponsor','desc','compat','force','fx_sharpen','fx_mute'].filter(k => o[k]).length
                 + (o.playlist === 'all') + !!(o.clip_start || o.clip_end);
  const filters = ['date_after','date_before','min_dur','max_dur','min_views','max_mb','title_has','title_not'].filter(k => o[k]).length;
  const plain = AppState.mode === 'snap' || AppState.mode === 'ig', changed = isCustom(o);
  $('custBtn').classList.toggle('changed', changed);
  $('optsum').textContent = changed ? '· ' + [o.kind !== 'video' && !plain ? o.kind : '', fx ? `${fx} effect${fx > 1 ? 's' : ''}` : '', !plain && extras ? `${extras} extra${extras > 1 ? 's' : ''}` : '', !plain && filters ? `${filters} filter${filters > 1 ? 's' : ''}` : ''].filter(Boolean).join(', ') : '';
  updateGo(o, lk);
}

function toggleCustom() { const c = $('custom'); c.classList.toggle('open'); $('custBtn').classList.toggle('on', c.classList.contains('open')); if (c.classList.contains('open')) setTimeout(() => c.scrollIntoView({behavior:'smooth', block:'nearest'}), 60); }
function closeCustom() { $('custom').classList.remove('open'); $('custBtn').classList.remove('on'); }

const LOOKS = [
  ['none','Original','linear-gradient(135deg,#f472b6,#fbbf24,#22d3ee)',''],
  ['bw','B&W','linear-gradient(135deg,#fff,#000)','grayscale(1)'],
  ['sepia','Sepia','linear-gradient(135deg,#f3dcb0,#6b4423)','sepia(1)'],
  ['vivid','Vivid','linear-gradient(135deg,#ff2d95,#ffe600,#00e5ff)','saturate(1.7) contrast(1.1)'],
  ['warm','Warm','linear-gradient(135deg,#ffd166,#ef476f)','sepia(.35) saturate(1.3) hue-rotate(-10deg)'],
  ['cool','Cool','linear-gradient(135deg,#a5f3fc,#3b82f6)','saturate(1.1) hue-rotate(15deg) brightness(1.02)'],
  ['vintage','Vintage','linear-gradient(135deg,#e9d8a6,#9b6a3c)','sepia(.5) contrast(.9) saturate(.9)'],
  ['cinematic','Cinema','linear-gradient(135deg,#0f2027,#2c5364)','contrast(1.15) saturate(1.1) brightness(.92)'],
  ['negative','Negative','linear-gradient(135deg,#0ff,#f0f)','invert(1)']];

function initLooksAndPresets() {
    if($('looks')) $('looks').innerHTML = LOOKS.map(l => `<button class="look" data-look="${l[0]}"><i style="background:${l[2]}"></i>${l[1]}</button>`).join('');
    document.querySelectorAll('.look').forEach(b => b.addEventListener('click', () => { document.querySelector('[data-o="fx_look"]').value = b.dataset.look; refreshOpts(); }));
    if($('presets')) $('presets').innerHTML = Object.entries(PRESETS).map(([k, v]) => `<button class="preset" data-preset="${k}">${v[0]}</button>`).join('');
    document.querySelectorAll('.preset').forEach(b => b.addEventListener('click', (e) => applyPreset(e.currentTarget.dataset.preset)));
    document.querySelectorAll('#kindseg button').forEach(b => b.addEventListener('click', () => { curKind = b.dataset.kind; refreshOpts(); }));
}

function setThumb(url) { const v = url ? `url("${url.replace(/"/g, '%22')}")` : ''; $('timg').style.backgroundImage = v; $('timg0').style.backgroundImage = v; }
function onCmp(v) { $('thumb').style.setProperty('--cmp', v + '%'); }
function paintPreview(o) {
  const l = LOOKS.find(x => x[0] === o.fx_look);
  const active = !!(l && l[3]) || o.fx_blur !== 'none' || o.fx_mirror !== 'none';
  if (active && !$('thumb').classList.contains('cmp-on')) { $('thumb').style.setProperty('--cmp', '50%'); $('cmp').value = 50; }
  $('thumb').classList.toggle('cmp-on', active);
  $('timg').style.filter = [l && l[3], o.fx_blur === 'light' ? 'blur(2px)' : o.fx_blur === 'strong' ? 'blur(6px)' : ''].filter(Boolean).join(' ');
  $('timg').style.transform = [o.fx_mirror === 'h' ? 'scaleX(-1)' : '', o.fx_mirror === 'v' ? 'scaleY(-1)' : ''].filter(Boolean).join(' ');
}

/* ---------- background jobs ---------- */
const KEY = 'hub_tracked', GKEY = 'hub_groups';
const jget = (k) => { try { return JSON.parse(localStorage.getItem(k) || '[]'); } catch { return []; } };
const jset = (k, a) => { try { localStorage.setItem(k, JSON.stringify(a)); } catch {} };
const tracked = () => jget(KEY), setTracked = a => jset(KEY, a), groups = () => jget(GKEY), setGroups = a => jset(GKEY, a);

function startVideo(audio) {
  if (AppState.mode !== 'video' && AppState.mode !== 'ig') return toast('Paste a valid video link first');
  const options = getOpts();
  if (AppState.mode === 'ig') options.kind = 'video';
  else if (audio) options.kind = 'audio';
  if (AppState.liveUser && AppState.liveOn === false) return toast(`@${AppState.liveUser} is not live - press Watch to record automatically`);
  run({action_type:'url', target:AppState.videoUrl, options}, AppState.liveUser ? `🔴 Recording @${AppState.liveUser}'s live…` : AppState.mode === 'ig' ? 'Fetching from Instagram…' : options.kind === 'audio' ? 'Extracting audio…' : 'Downloading…');
}

function startSnap() {
  if (AppState.mode !== 'snap') return toast('Enter a Snapchat username or link');
  run({action_type:'snapchat', target:AppState.snapUser, options:getOpts()}, 'Fetching Snapchat stories…');
}

async function run(payload, title) {
  AppState.lastRun = {payload, title}; openProgress(title);
  const r = await post('/download-action', payload);
  if (!r.success) return showResult({status:'error', message:r.message || 'Could not start the download.', files:[]});
  AppState.viewing = r.job_id; setTracked([...tracked(), r.job_id]); watch();
  if ($('fname')) $('fname').value = '';          // the name was for this download only
}
function openProgress(title) {
  AppState.resultOpen = false; $('modal').classList.add('open'); $('m-load').style.display = ''; $('m-res').style.display = 'none';
  $('ltitle').textContent = title; $('bar').style.width = '0'; setRing(0); $('lmsg').textContent = 'Starting…'; $('lmeta').textContent = ''; $('cancelBtn').disabled = false;
  $('cancelBtn').textContent = /Recording .*live/.test(title) ? '⏹ Stop & save' : 'Cancel';
}
function setRing(p) { p = Math.max(0, Math.min(100, +p || 0)); $('mring').style.setProperty('--p', p); $('mpct').textContent = Math.round(p) + '%'; $('mring').classList.toggle('ind', p < 1); }
function updateProgress(j) { setRing(j.progress); $('bar').style.width = j.progress + '%'; $('lmsg').textContent = j.message; $('lmeta').textContent = [j.speed, j.eta && 'ETA ' + j.eta].filter(Boolean).join('  ·  '); }
function hideProgress() { $('modal').classList.remove('open'); AppState.viewing = null; toast('Running in the background - a popup will tell you when it is done'); }
async function cancelJob() { if (!AppState.viewing) return; $('cancelBtn').disabled = true; $('lmsg').textContent = $('cancelBtn').textContent.includes('Stop') ? 'Stopping and saving…' : 'Cancelling…'; await api('/cancel/' + AppState.viewing, {method:'POST'}); }

/* One polling loop only: watch() is guarded, watchBody() does one poll. */
async function watch() {
  if (AppState.watching) return;
  AppState.watching = true;
  clearTimeout(AppState.wt);
  try { await watchBody(); } finally { AppState.watching = false; AppState.wt = setTimeout(watch, 1500); }
}
async function watchBody() {
  const ids = tracked(), gids = groups(), all = [...new Set([...ids, ...gids.flat()])];
  if (!all.length) { renderStrip([]); return; }
  const d = await api('/jobs?ids=' + all.map(encodeURIComponent).join(','));
  if (d.success) {
    const byId = Object.fromEntries(d.jobs.map(j => [j.id, j])), keep = [], running = [], gset = new Set(gids.flat());
    for (const id of ids) {
      const j = byId[id]; if (!j) continue;
      if (j.status === 'done' || j.status === 'error') { if (!gset.has(id) && !AppState.results.some(x => x.id === j.id)) AppState.results.push(j); }
      else { keep.push(id); running.push(j); if (id === AppState.viewing) updateProgress(j); }
    }
    const keepG = [];
    for (const g of gids) {
      const js = g.map(id => byId[id]).filter(Boolean); if (!js.length) continue;
      if (js.every(j => j.status === 'done' || j.status === 'error')) AppState.results.push({batch:true, jobs:js}); else keepG.push(g);
    }
    const addedT = tracked().filter(id => !ids.includes(id)), addedG = groups().filter(g => !gids.some(h => h.join() === g.join()));
    setGroups([...keepG, ...addedG]); setTracked([...keep, ...addedT]); renderStrip(running); showNextResult();
  }
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) watch(); });

function renderStrip(running) {
  AppState.lastRunning = running;
  $('tbRun').textContent = running.length || ''; $('pillRun').textContent = running.length ? `⏳ ${running.length} running` : '⏳ idle'; $('pillRun').classList.toggle('on', running.length > 0);
  const act = running.filter(j => j.status === 'running').length, wait = running.filter(j => j.status === 'queued').length, held = running.filter(j => j.status === 'paused').length;
  if (running.length) $('pillRun').textContent = `⏳ ${act} running` + (wait ? ` · ${wait} waiting` : '') + (held ? ` · ${held} paused` : '');
  const more = (running.length > 4 ? `<div class="run" data-openq="1"><span>+ ${running.length - 4} more in the queue…</span></div>` : '')
    + (running.length ? `<button class="qlink" data-openq="1">☰ Manage queue <em>reorder · pause · cancel</em></button>` : '');
  $('strip').innerHTML = running.slice(0, 4).map(j => `<div class="run" data-run-id="${esc(j.id)}"><div class="pring${j.progress < 1 ? ' ind' : ''}" style="--p:${Math.round(j.progress)}"><b>${Math.round(j.progress)}</b></div><span>${esc(j.label)} · ${esc(j.message)}</span></div>`).join('') + more;
  document.querySelectorAll('.run[data-run-id]').forEach(el => el.addEventListener('click', (e) => peek(e.currentTarget.dataset.runId)));
}

function redo() {
  if (!AppState.lastRun) return; closeResult();
  const p = JSON.parse(JSON.stringify(AppState.lastRun.payload)); p.options = Object.assign({}, p.options, {force:true}); run(p, AppState.lastRun.title);
}
function peek(id) { const j = AppState.lastRunning.find(x => x.id === id); if (!j) return; AppState.viewing = id; openProgress(j.live ? `🔴 Recording ${j.label.replace(/^🔴 /, '')} live…` : j.label); updateProgress(j); }
function showNextResult() { if (AppState.resultOpen || !AppState.results.length) return; const j = AppState.results.shift(); if (j.id === AppState.viewing) AppState.viewing = null; showResult(j); }
function confetti() {
  if (ui.confetti !== '1' || (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches)) return;
  const cols = ['var(--pc)', '#22d3ee', '#f472b6', '#fbbf24', '#34d399', '#8b5cf6'], r = (a, b) => (a + Math.random() * (b - a)).toFixed(2);
  $('confetti').innerHTML = Array.from({length: 44}, (_, i) => `<i style="left:${r(2, 98)}%;--x:${r(-18, 18)}vw;--r:${r(0, 360)}deg;--d:${r(1.6, 2.8)}s;--c:${cols[i % cols.length]};animation-delay:${r(0, .35)}s"></i>`).join('');
  clearTimeout(confetti.t); confetti.t = setTimeout(() => { $('confetti').innerHTML = ''; }, 3400);
}

/* phones: send the finished files to the share sheet ("Save Video" / "Save Image" = Photos). Needs https. */
const PMIME = {mp4:'video/mp4', m4v:'video/mp4', mov:'video/quicktime', webm:'video/webm', jpg:'image/jpeg', jpeg:'image/jpeg', png:'image/png', webp:'image/webp', gif:'image/gif'};
function canSharePhotos() {
  try { return matchMedia('(pointer:coarse)').matches && !!navigator.canShare && navigator.canShare({files:[new File(['x'], 'a.mp4', {type:'video/mp4'})]}); } catch { return false; }
}
async function preparePhotos(j, files, url) {
  const btn = $('rbtnPhotos'), list = files.filter(f => /\.(mp4|m4v|mov|webm|jpe?g|png|webp|gif)$/i.test(f)).slice(0, 20), out = []; let total = 0;
  AppState.photoFor = j.id;
  if (!list.length) return btn.remove();
  try {                                    // fetch first: the share sheet must be opened by a tap, and a big download would make that tap expire
    for (const f of list) {
      const r = await fetch(url(f)); if (!r.ok) throw new Error('fetch');
      total += +r.headers.get('content-length') || 0; if (total > 300 * 1024 * 1024) throw new Error('big');
      const b = await r.blob(), ext = f.split('.').pop().toLowerCase();
      out.push(new File([b], f.split('/').pop(), {type: PMIME[ext] || b.type}));
      if ($('rbtnPhotos')) $('rbtnPhotos').textContent = `📲 Preparing ${out.length}/${list.length}…`;
    }
  } catch { if ($('rbtnPhotos')) $('rbtnPhotos').remove(); return; }
  const b2 = $('rbtnPhotos'); if (!b2 || AppState.photoFor !== j.id) return;
  b2.disabled = false; b2.textContent = '📲 Save to Photos';
  b2.onclick = async () => { try { await navigator.share({files: out}); } catch (e) { if (e.name !== 'AbortError') toast('Could not open the share sheet'); } };
}

const photoCount = files => {                 // same rule as the server: videos + photos, but not the cover that belongs to a video
  const vid = /\.(mp4|m4v|mov)$/i, img = /\.(jpe?g|png|gif|heic)$/i, stem = f => f.replace(/\.[^.]+$/, '');
  const vs = new Set(files.filter(f => vid.test(f)).map(stem));
  return files.filter(f => vid.test(f) || (img.test(f) && !vs.has(stem(f)))).length;
};
const isIos = () => /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
const CHECK = '<svg class="chk" viewBox="0 0 52 52"><circle class="chk-c" cx="26" cy="26" r="24" fill="none"/><path class="chk-p" fill="none" d="M14 27l8 8 16-17"/></svg>';
function showResult(j) {
  if (j.batch) return showBatchResult(j.jobs);
  AppState.resultOpen = true;
  const ok = j.status === 'done', files = j.files || [], n = files.length, already = ok && /^already downloaded/i.test(j.message || '');
  $('modal').classList.add('open'); $('m-load').style.display = 'none'; $('m-res').style.display = '';
  $('rico').innerHTML = ok ? CHECK : '<span class="warn">⚠️</span>'; if (ok && n && !already) confetti();
  $('rtitle').textContent = ok ? (already ? 'Already downloaded ✓' : n ? `${n} file${n > 1 ? 's' : ''} ready` : 'Already up to date') : 'Something went wrong';
  $('rtitle').style.color = ok ? 'var(--ok)' : 'var(--err)';
  $('rdesc').textContent = ok ? `${j.label} · ${j.message}` : j.message;
  const url = f => `/files/${encodeURIComponent(j.category)}/${f.split('/').map(encodeURIComponent).join('/')}?dl=1&j=${encodeURIComponent(j.id)}`;
  $('rlog').innerHTML = j.log ? `<details style="text-align:left;margin-top:10px"><summary style="cursor:pointer;color:var(--mute);font-size:.8rem">Show log</summary><pre style="white-space:pre-wrap;word-break:break-word;font-size:.7rem;max-height:140px;overflow:auto;color:var(--mute)">${esc(j.log)}</pre></details>` : '';
  $('rfiles').style.display = n ? '' : 'none';
  const isImg = f => /\.(gif|webp|jpe?g|png)$/i.test(f);
  $('rfiles').innerHTML = files.map(f => `<div class="rf">${isImg(f) ? `<img class="rimg" loading="lazy" alt="" src="${esc(url(f).replace('dl=1&', ''))}">` : ''}<span title="${esc(f)}">${esc(f)}</span><a class="mini" href="${esc(url(f))}">Save</a></div>`).join('');
  let btns = '';
  if (n > 1) btns += `<a class="btn primary" href="/zip/${encodeURIComponent(j.id)}">⬇ Download all (ZIP)</a>`;
  if (n === 1) btns += `<a class="btn primary" href="${esc(url(files[0]))}">⬇ Save to this device</a>`;
  if (already && AppState.lastRun && AppState.lastRun.payload.action_type === 'url') btns += `<button class="btn ghost" id="rbtnRedo">🔁 Download again</button>`;
  if (!ok && /instagram login|cookies\.txt/i.test(j.message || '')) btns += `<button class="btn ghost" id="rbtnIg">📸 Open Instagram login</button>`;
  const photos = ok && n && canSharePhotos();
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const pcount = photoCount(files);
  if (photos) btns = '<button class="btn primary" id="rbtnPhotos" disabled>📲 Preparing…</button>' + btns;
  if (n) btns += '<button class="btn ghost" id="rbtnShow">📂 Show in folder</button>';
  $('rbtns').innerHTML = btns + `<button class="btn ghost" id="rbtnClose">Close</button>`;

  if ($('rbtnRedo')) $('rbtnRedo').addEventListener('click', redo);
  if ($('rbtnIg')) $('rbtnIg').addEventListener('click', () => { closeResult(); openIgSettings(); });
  if ($('rbtnClose')) $('rbtnClose').addEventListener('click', closeResult);
  if ($('rbtnShow')) $('rbtnShow').addEventListener('click', () => { closeResult(); xRevealJob(j); });
  if (photos) preparePhotos(j, files, url);
  else if (ok && n && matchMedia('(pointer:coarse)').matches) $('rbtns').insertAdjacentHTML('beforeend', '<p class="hint" style="text-align:center;margin:2px 0 0">📲 To save straight into Photos, open Media Hub over https - see Settings → Phone.</p>');

  if (navigator.vibrate) navigator.vibrate(150);
}

function showBatchResult(js) {
  AppState.resultOpen = true;
  const ok = js.filter(j => j.status === 'done').length, bad = js.length - ok, ids = js.filter(j => (j.files || []).length).map(j => j.id);
  $('modal').classList.add('open'); $('m-load').style.display = 'none'; $('m-res').style.display = '';
  $('rico').innerHTML = bad ? '<span class="warn">📦</span>' : CHECK; if (!bad) confetti(); $('rtitle').textContent = `Batch finished: ${ok} done` + (bad ? `, ${bad} failed` : '');
  $('rtitle').style.color = bad ? 'var(--text)' : 'var(--ok)'; $('rdesc').textContent = ''; $('rlog').innerHTML = ''; $('rfiles').style.display = '';
  $('rfiles').innerHTML = js.map(j => `<div class="rf" style="flex-wrap:wrap"><span title="${esc(j.message)}">${j.status === 'done' ? '✔' : '✖'} ${esc(j.label)}</span><small style="color:var(--mute);flex-basis:100%">${esc(j.message)}</small></div>`).join('');
  $('rbtns').innerHTML = (ids.length ? `<a class="btn primary" href="/zip-multi?ids=${ids.map(encodeURIComponent).join(',')}">⬇ Download everything (ZIP)</a>` : '') + (ids.length ? '<button class="btn ghost" id="rbtnBatchShow">📂 Show in folder</button>' : '') + `<button class="btn ghost" id="rbtnBatchClose">Close</button>`;
  if ($('rbtnBatchClose')) $('rbtnBatchClose').addEventListener('click', closeResult);
  if ($('rbtnBatchShow')) $('rbtnBatchShow').addEventListener('click', () => { closeResult(); xRevealJob(js.find(j => (j.files || []).length)); });
  if (navigator.vibrate) navigator.vibrate(150);
}
function closeResult() { AppState.resultOpen = false; $('modal').classList.remove('open'); showNextResult(); }

/* ---------- batch ---------- */
function toggleBatch() { $('batchbox').classList.toggle('open'); }
async function startBatch() {
  const items = [], bad = [], options = Object.assign(getOpts(), {name:''});
  for (const l of $('batchtxt').value.split('\n').map(x => x.trim()).filter(Boolean)) {
    const c = classify(l);
    if (c && c.kind === 'video') items.push({action_type:'url', target:c.url, options: isInsta(c.url) ? Object.assign({}, options, {kind:'video'}) : options});
    else if (c && c.kind === 'snap') items.push({action_type:'snapchat', target:c.user, options});
    else bad.push(l);
  }
  if (!items.length) return toast('No valid links found');
  const r = await post('/batch', {items});
  if (!r.success) return toast('Batch could not start');
  setTracked([...tracked(), ...r.job_ids]); setGroups([...groups(), r.job_ids]);
  $('batchtxt').value = ''; toggleBatch();
  toast(`${r.job_ids.length} queued` + (bad.length ? ` · ${bad.length} lines skipped` : '')); watch();
}

/* ---------- watchlist ---------- */
const ago = ts => { if (!ts) return 'never'; const m = Math.round((Date.now() / 1000 - ts) / 60); return m < 1 ? 'just now' : m < 60 ? m + ' min ago' : Math.round(m / 60) + ' h ago'; };
const ivLabel = m => m < 60 ? `${m} min` : m === 60 ? '1 hour' : m < 1440 ? `${m / 60} hours` : 'day';
let lastWatchHtml = '';
async function loadWatches() {
  const d = await api('/watches'); if (!d.success || !Array.isArray(d.watches)) return;
  const on = d.watches.filter(w => w.enabled).length;
  $('tbWatch').textContent = on || ''; $('rnWatch').textContent = on || ''; $('pillWatch').textContent = `👁 ${on} watching`; $('pillWatch').classList.toggle('on', on > 0);
  const ivs = d.intervals && d.intervals.length ? d.intervals : [15, 30, 60, 180, 360];
  const html = d.watches.length ? d.watches.map((w, i) => `<div class="file" style="--i:${i}">
      <span class="fav${w.enabled ? ' live' : ''}${w.rec ? ' rec' : ''}">${w.kind === 'snapchat' ? '👻' : w.kind === 'tiktok_live' ? '🔴' : '🎬'}</span>
      <div class="fmain"><div class="fname" title="${esc(w.target)}">${esc(w.label)}</div>
        <div class="fmeta">${w.rec ? `<b class="recnow">⏺ REC ${fmtDur(w.rec.secs || 0)} · ${esc(w.rec.size_h)}</b>` : w.busy ? '⏳ checking now…' : esc(w.last_status)} · checked ${ago(w.last_check)}${w.enabled ? '' : ' · paused'} · ${w.total_new} ${w.kind === 'tiktok_live' ? 'recorded' : 'downloaded'}</div></div>
      <div class="fctl">
        ${w.rec ? `<button class="mini del wact-stop" data-jid="${esc(w.rec.id)}" title="Stop recording and save it">⏹ Stop &amp; save</button>` : ''}
        <select class="wsel" data-wid="${esc(w.id)}" aria-label="Check how often">${(w.kind === 'tiktok_live' && d.live_intervals ? d.live_intervals : ivs).map(m => `<option value="${m}"${m === w.interval ? ' selected' : ''}>every ${ivLabel(m)}</option>`).join('')}</select>
        <button class="mini wact-check" data-wid="${esc(w.id)}" title="Check this account now">Check</button>
        <button class="mini wact-toggle" data-wid="${esc(w.id)}">${w.enabled ? 'Pause' : 'Resume'}</button>
        <button class="mini del wact-del" data-wid="${esc(w.id)}" aria-label="Stop watching">✕</button></div></div>`).join('')
    : '<div class="empty"><b>👀</b>Nothing watched yet.<br>Paste a creator\'s video or a Snapchat username above, then press <b style="display:inline;font-size:inherit">Watch</b>.</div>';
  if (html === lastWatchHtml) return;
  const a = document.activeElement; if (a && a.classList && a.classList.contains('wsel') && $('wlist').contains(a)) return;
  lastWatchHtml = html; $('wlist').innerHTML = html;

  document.querySelectorAll('.wsel').forEach(el => el.addEventListener('change', e => setWInt(e.currentTarget.dataset.wid, e.currentTarget.value)));
  document.querySelectorAll('.wact-check').forEach(el => el.addEventListener('click', e => wact(e.currentTarget.dataset.wid, 'check')));
  document.querySelectorAll('.wact-toggle').forEach(el => el.addEventListener('click', e => wact(e.currentTarget.dataset.wid, 'toggle')));
  document.querySelectorAll('.wact-del').forEach(el => el.addEventListener('click', e => wdel(e.currentTarget.dataset.wid)));
  document.querySelectorAll('.wact-stop').forEach(el => el.addEventListener('click', async e => { const b = e.currentTarget; b.disabled = true; await api('/cancel/' + encodeURIComponent(b.dataset.jid), {method:'POST'}); toast('Stopping - the recording is being saved'); lastWatchHtml = ''; setTimeout(loadWatches, 1500); }));
}

async function addWatch(kind, target, label) {
  const r = await post('/watches', {kind, target, label, interval: +$('wint').value, options: Object.assign(getOpts(), {name:''})});
  toast(r.success ? `Watching ✓ - checks ${$('wint').options[$('wint').selectedIndex].text}` : (r.message || 'Could not add')); loadWatches();
}
const watchSnap = () => addWatch('snapchat', AppState.snapUser);
function watchCreator() { AppState.chanUrl ? addWatch('channel', AppState.chanUrl, AppState.chanName) : toast('No creator link found for this video'); }
function watchGo() { return AppState.liveUser ? addWatch('tiktok_live', AppState.liveUser) : AppState.mode === 'snap' ? watchSnap() : watchCreator(); }
async function setWInt(id, v) { const r = await post('/watches/' + encodeURIComponent(id) + '/interval', {interval: +v}); toast(r.success ? `Now checks every ${ivLabel(+v)}` : (r.message || 'Could not change')); lastWatchHtml = ''; loadWatches(); }
async function wact(id, a) { await api(`/watches/${encodeURIComponent(id)}/${a}`, {method:'POST'}); lastWatchHtml = ''; loadWatches(); }
async function wdel(id) { if (confirm('Stop watching this account?')) { await api('/watches/' + encodeURIComponent(id), {method:'DELETE'}); lastWatchHtml = ''; loadWatches(); } }
setInterval(() => { if (!document.hidden) loadWatches(); }, 8000); loadWatches();

/* ---------- 🔧 maintenance ---------- */
function openMaint() { openSettings('tools'); }
function closeMaint() {}
const isTyping = t => !!t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable);
function linkFromText(text) {
  text = String(text || '').trim(); const m = text.match(/https?:\/\/[^\s<>"']+/i);
  if (m) { const u = m[0].replace(/[)\].,;!?]+$/, ''); return isUrl(u) ? u : null; }
  return /^@[A-Za-z0-9._-]{3,32}$/.test(text) ? text : null;
}
function takeLink(v) { setTab('home'); $('url').value = v; onUrlInput(); toast('Link ready - press the big button'); }

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeMaint(); closeSettings(); closeKeys(); closePicker(); closeFiles(); closeWatch(); closePin(); closeQueue();
    if ($('modal').classList.contains('open')) { AppState.resultOpen ? closeResult() : hideProgress(); }
    return;
  }
  if ((e.ctrlKey || e.metaKey) && String(e.key).toLowerCase() === 'k') { e.preventDefault(); focusUrl(); return; }
  if (isTyping(e.target) || e.ctrlKey || e.metaKey || e.altKey || document.querySelectorAll('.overlay.open').length) return;
  const k = String(e.key).toLowerCase();
  if (k === '/') { e.preventDefault(); focusUrl(); }
  else if (k === 'c') AppState.mode ? toggleCustom() : toast('Paste a link first, then press C to customize');
  else if (k === 's') openSettings(); else if (k === 't') openMaint(); else if (k === 'h') setTab('home');
  else if (k === 'w') openWatch();
  else if (k === 'f') openFiles();
  else if (k === 'q') openQueue();
  else if (e.key === '?') $('kmodal').classList.add('open');
});

document.addEventListener('paste', e => {
  if (isTyping(e.target)) return;
  const v = linkFromText(e.clipboardData && e.clipboardData.getData('text')); if (!v) return;
  e.preventDefault(); takeLink(v);
});

document.addEventListener('dragover', e => { if (X.drag) return; e.preventDefault(); $('smart').classList.add('drop'); });
document.addEventListener('dragleave', e => { if (!e.relatedTarget) $('smart').classList.remove('drop'); });
document.addEventListener('drop', e => {
  if (X.drag) return;
  e.preventDefault(); $('smart').classList.remove('drop'); const dt = e.dataTransfer; if (!dt) return;
  const v = linkFromText(dt.getData('text/uri-list') || dt.getData('text/plain') || dt.getData('text')); v ? takeLink(v) : toast('That is not a link');
});

window.addEventListener('offline', () => netState(false)); window.addEventListener('online', () => api('/version'));

async function loadVersion() {
  const v = await api('/version'); if (!v.success) return;
  const row = (name, ok, okText, badText) => `<div class="vrow"><b>${name}<small>${ok ? '' : esc(badText)}</small></b><span class="${ok ? 'vok' : 'vbad'}">${ok ? okText : '✖ missing'}</span></div>`;
  $('verlist').innerHTML = row('yt-dlp', true, '✔ ' + esc(v.ytdlp), '')
    + row('ffmpeg', v.ffmpeg, '✔ installed', 'Needed for effects, MP3, GIFs and stickers:  sudo apt install ffmpeg')
    + row('snapchat-dl', v.snapchat_dl, '✔ installed', 'Needed for Snapchat stories.')
    + row('gallery-dl', v.gallery_dl, '✔ installed', 'Needed for Instagram photos, stories and profiles:  pip install -U gallery-dl --break-system-packages');
  SX.miss = !(v.ffmpeg && v.snapchat_dl && v.gallery_dl); updDots();
  if (!v.ffmpeg && !loadVersion.warned) { loadVersion.warned = true; toast('ffmpeg is not installed - effects, GIFs and MP3 will not work'); }
}

async function updateYtdlp() {
  const b = $('updBtn'); b.disabled = true; b.textContent = 'Updating… (up to a minute)'; $('updmsg').textContent = '';
  const r = await api('/update-ytdlp', {method:'POST'});
  b.disabled = false; b.textContent = '⬆ Update yt-dlp'; $('updmsg').textContent = (r.message || '') + (r.needs_restart ? ' Press Restart app to use it.' : ''); loadVersion();
}
async function restartApp() {
  if (!confirm('Restart the app now? Downloads that are running will be stopped.')) return;
  await api('/restart', {method:'POST'}); toast('Restarting…'); $('updmsg').textContent = 'Restarting…';
  setTimeout(() => { const t = setInterval(async () => { const v = await api('/version'); if (v.success) { clearInterval(t); toast('Back online ✓'); $('updmsg').textContent = 'Back online ✓'; loadVersion(); } }, 2000); }, 3000);
}

/* ---------- settings ---------- */
const TPL_SAMPLE = {title:'My cool video', uploader:'Creator', id:'a1B2c3', date:'2026-10-31', resolution:'1080p', playlist_index:'07'};
function tplPreview() { const t = $('nameTpl').value.trim(); $('tplprev').textContent = t ? 'Example: ' + t.replace(/\{(\w+)\}/g, (m, k) => TPL_SAMPLE[k] ?? m) + '.mp4' : 'Now: the title and the video id, like “My cool video [a1B2c3].mp4”.'; }
async function saveTpl() { const r = await post('/settings', {name_template: $('nameTpl').value.trim()}); $('nameTpl').value = r.name_template || ''; tplPreview(); toast(r.name_template ? 'Name template saved ✓' : 'Back to the normal name'); }
async function loadSettings() {
  const d = await api('/settings');
  if (d.success) { $('ntfy').value = d.ntfy || ''; $('creatorFolders').checked = d.creator_folders !== false; $('igOrganize').checked = d.ig_organize !== false; $('autoName').value = d.auto_name || 'creator'; $('preferH264').checked = d.prefer_h264 !== false; $('notifyDl').checked = !!d.notify_downloads; $('igcap').value = d.ig_cap || 30;
    $('nameTpl').value = d.name_template || ''; $('speedLimit').value = d.speed_limit || '0'; $('parallelParts').value = String(d.parallel_parts || 1); $('paceSel').value = d.pace || 'off'; $('retryCount').value = String(d.retry_count ?? 3); $('cleanUrls').checked = d.clean_urls !== false; tplPreview();
    if ($('liveQuality')) $('liveQuality').value = d.live_quality || 'best';
    if ($('liveHelper')) { $('liveHelper').checked = d.live_helper !== false; $('liveMax').value = String(d.live_max_hours || 6); $('liveReady').textContent = d.live_ready === false ? '⚠ curl_cffi is not installed on the server - TikTok will block live checks more often. Rebuild the Docker image.' : ''; }
    if ($('digestSel')) { $('digestSel').value = d.digest || 'off'; $('digestHour').value = String(d.digest_hour ?? 20); digestNote(d.digest_waiting || 0); } }
  $('clipOn').checked = clipEnabled(); loadVersion(); loadIg();
}
async function setCreatorFolders() { await post('/settings', {creator_folders: $('creatorFolders').checked}); toast($('creatorFolders').checked ? 'New downloads go into creator folders' : 'Creator folders off'); }
async function saveSettings() { await post('/settings', {ntfy: $('ntfy').value.trim()}); toast('Saved'); }
async function testSettings() { await saveSettings(); const r = await post('/settings/test', {}); toast(r.message); }
const fmtDate = ts => new Date(ts * 1000).toLocaleDateString(undefined, {year:'numeric', month:'short', day:'numeric'});

async function loadIg() {
  const s = await api('/instagram-cookies'); if (!s.success) return;
  const el = $('igstat');
  if (!s.saved) el.innerHTML = '✖ <b>No login saved.</b> Reels may work without one, but photos, stories and profiles need it.';
  else if (s.session_expired) el.innerHTML = '⚠ <b>Login saved, but it has expired.</b> Export a fresh cookies.txt and save it again.';
  else el.innerHTML = `✔ <b>Login saved</b> · ${s.count} cookies · sessionid ${s.has_session ? '✔' : '<b style="color:var(--err)">✖ missing - you were not logged in</b>'}`
    + (s.expires ? ` · valid until about ${esc(fmtDate(s.expires))}` : '') + (s.saved_at ? ` · saved ${esc(fmtDate(s.saved_at))}` : '');
  $('igRemove').disabled = !s.saved; SX.igExp = !!(s.saved && s.session_expired); updDots();
}

async function igPickFile() {
  const f = $('igfile').files[0]; if (!f) return;
  if (f.size > 1024 * 1024) { toast('That file is too big to be a cookies.txt'); $('igfile').value = ''; return; }
  try { $('igtext').value = await f.text(); toast('File loaded - now press “Save login”'); } catch { toast('Could not read that file'); }
}
async function saveIg() {
  const text = $('igtext').value.trim(); if (!text) return toast('Choose the cookies.txt file or paste its text first');
  $('igSave').disabled = true; const r = await post('/instagram-cookies', {text}); $('igSave').disabled = false;
  toast(r.message || (r.success ? 'Saved' : 'Could not save'));
  if (r.success) { $('igtext').value = ''; $('igfile').value = ''; } else $('igstat').innerHTML = '⚠ ' + esc(r.message || 'Could not save');
  if (r.success) loadIg();
}
async function removeIg() {
  if (!confirm('Remove the saved Instagram login from the server?')) return;
  const r = await api('/instagram-cookies', {method:'DELETE'}); toast(r.message || 'Removed'); loadIg();
}
async function saveIgCap() {
  const r = await post('/settings', {ig_cap: $('igcap').value.trim()});
  toast(r.success ? 'Saved' : (r.message || 'Could not save')); if (r.success) $('igcap').value = r.ig_cap;
}

/* ---------- Setup Event Bindings Safely ---------- */
function safeBind(id, event, handler) {
    const el = document.getElementById(id);
    if (el) el.addEventListener(event, handler);
}

function initStaticEventBindings() {
  safeBind('gearBtn', 'click', openSettings);
  safeBind('wrenchBtn', 'click', openMaint);

  safeBind('url', 'input', onUrlInput);
  const urlEl = document.getElementById('url');
  if(urlEl) urlEl.addEventListener('keydown', (e) => { if(e.key === 'Enter') go(); });

  safeBind('pasteBtn', 'click', pasteLink);
  safeBind('clearBtn', 'click', clearLink);
  safeBind('useClipBtn', 'click', useClip);
  safeBind('dismissClipBtn', 'click', dismissClip);
  safeBind('goBtn', 'click', go);
  safeBind('custBtn', 'click', toggleCustom);
  safeBind('watchBtn', 'click', watchGo);

  const customEl = document.getElementById('custom');
  if(customEl) customEl.addEventListener('input', e => {
      const t = e.target; if (t && t.type === 'checkbox' && t.dataset.o) document.querySelectorAll(`input[type=checkbox][data-o="${t.dataset.o}"]`).forEach(x => x.checked = t.checked);
      refreshOpts();
  });

  safeBind('cmp', 'input', (e) => onCmp(e.target.value));
  safeBind('ptitle', 'click', (e) => e.currentTarget.classList.toggle('open'));
  safeBind('resetPresetBtn', 'click', () => applyPreset('reset'));

  safeBind('toggleBatchBtn', 'click', toggleBatch);
  safeBind('startBatchBtn', 'click', startBatch);
  safeBind('cancelBtn', 'click', cancelJob);
  safeBind('runBackgroundBtn', 'click', hideProgress);
  safeBind('closeSettingsBtn', 'click', closeSettings);
  safeBind('smodalDoneBtn', 'click', closeSettings);

  const smodalEl = document.getElementById('smodal');
  if(smodalEl) smodalEl.addEventListener('click', (e) => { if(e.target === smodalEl) closeSettings(); });

  safeBind('confettiOn', 'change', (e) => setUi('confetti', e.target.checked ? '1' : '0'));
  safeBind('saveNtfyBtn', 'click', saveSettings);
  safeBind('testNtfyBtn', 'click', testSettings);
  safeBind('creatorFolders', 'change', setCreatorFolders);
  safeBind('notifyDl', 'change', async () => { await post('/settings', {notify_downloads: $('notifyDl').checked}); toast($('notifyDl').checked ? 'You will get an alert when your downloads finish' : 'Download alerts off'); });
  safeBind('nameTpl', 'input', tplPreview); safeBind('saveTplBtn', 'click', saveTpl);
  document.querySelectorAll('#tplChips [data-tpl]').forEach(b => b.addEventListener('click', () => { const i = $('nameTpl'); i.value = (i.value + b.dataset.tpl).slice(0, 150); tplPreview(); i.focus(); }));
  [['speedLimit', 'speed_limit', 'Speed limit saved'], ['parallelParts', 'parallel_parts', 'Parallel parts saved'], ['paceSel', 'pace', 'Pause setting saved'], ['retryCount', 'retry_count', 'Retries saved']]
    .forEach(([id, key, msg]) => safeBind(id, 'change', async () => { await post('/settings', {[key]: $(id).value}); toast(msg + ' ✓'); }));
  safeBind('cleanUrls', 'change', async () => { await post('/settings', {clean_urls: $('cleanUrls').checked}); toast($('cleanUrls').checked ? 'Links are cleaned' : 'Links are kept as pasted'); });
  safeBind('pickVideosBtn', 'click', openPl); safeBind('closePlBtn', 'click', closePl); safeBind('plGoBtn', 'click', plGo);
  safeBind('plAllBtn', 'click', () => { plShown().forEach(e => pl.sel.add(e.index)); renderPl(); });
  safeBind('plNoneBtn', 'click', () => { pl.sel.clear(); renderPl(); });
  safeBind('plFilter', 'input', () => { pl.q = $('plFilter').value.trim(); renderPl(); });
  $('plist').addEventListener('click', e => { const t = e.target.closest('.prow'); if (!t) return; const i = +t.dataset.i; pl.sel.has(i) ? pl.sel.delete(i) : pl.sel.add(i); renderPl(); });
  const plm = $('plmodal'); plm.addEventListener('click', e => { if (e.target === plm) closePl(); });
  safeBind('preferH264', 'change', async () => { await post('/settings', {prefer_h264: $('preferH264').checked}); toast($('preferH264').checked ? 'New videos: iPhone-friendly (H.264)' : 'New videos: best quality, any format'); });
  safeBind('phoneSetupBtn', 'click', () => showPhoneSetup(false));
  safeBind('autoName', 'change', async () => { await post('/settings', {auto_name: $('autoName').value}); toast(({creator:'Empty name = creator + date (nba_2026-10-31_001)', date:'Empty name = date + number'})[$('autoName').value] || 'Empty name = keep the original title'); });
  safeBind('igOrganize', 'change', async () => { await post('/settings', {ig_organize: $('igOrganize').checked}); toast($('igOrganize').checked ? 'Instagram: username / date / type folders' : 'Instagram: flat folders'); });
  safeBind('igfile', 'change', igPickFile);
  safeBind('igSave', 'click', saveIg);
  safeBind('igRemove', 'click', removeIg);
  safeBind('igcap', 'change', saveIgCap);
  safeBind('clipOn', 'change', setClipOn);

  const mmodalEl = document.getElementById('mmodal');
  if(mmodalEl) mmodalEl.addEventListener('click', (e) => { if(e.target === mmodalEl) closeMaint(); });

  safeBind('updBtn', 'click', updateYtdlp);
  safeBind('rstBtn', 'click', restartApp);
  safeBind('mmodalCloseBtn', 'click', closeMaint);

  const tabHome = document.querySelector('nav.tabbar [data-tab="home"]');
  const tabWatch = document.querySelector('nav.tabbar [data-tab="watch"]');
  if(tabHome) tabHome.addEventListener('click', () => setTab('home'));
  const tabLive = document.querySelector('nav.tabbar [data-tab="live"]');
  if(tabLive) tabLive.addEventListener('click', () => setTab('live'));
  if(tabWatch) tabWatch.addEventListener('click', openWatch);

  safeBind('navSettingsBtn', 'click', openSettings);
  safeBind('navMaintBtn', 'click', openMaint);

  const kmodalEl = document.getElementById('kmodal');
  if(kmodalEl) kmodalEl.addEventListener('click', (e) => { if(e.target === kmodalEl) closeKeys(); });
  safeBind('closeKeysBtn', 'click', closeKeys);
}

/* ---------- story picker (Snapchat) ---------- */
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

/* ---------- iPhone: save straight into Photos (share sheet "Save Video" / "Save Image") - no ZIP, no Shortcut ---------- */
const PH_TYPES = /\.(mp4|m4v|mov|jpe?g|png|gif|heic)$/i;          // what the Photos app accepts
const PH_MAX_FILES = 20, PH_MAX_BYTES = 250 * 1024 * 1024;         // per tap: the phone keeps the files in memory
const phCapable = () => { try { return window.isSecureContext && !!navigator.canShare && navigator.canShare({files:[new File(['x'], 'a.mp4', {type:'video/mp4'})]}); } catch { return false; } };
let PH = {abort: null};
function phEl() {
  let el = $('phs'); if (el) return el;
  el = document.createElement('div'); el.id = 'phs'; el.className = 'phs';
  el.innerHTML = '<div class="phs-card"><div class="phs-grab"></div><h3 id="phsT"></h3><div id="phsB"></div><div class="phs-btns" id="phsX"></div></div>';
  el.addEventListener('click', e => { if (e.target === el) phClose(); });
  document.body.appendChild(el); return el;
}
function phClose() { if (PH.abort) PH.abort.abort(); PH.abort = null; const el = $('phs'); if (el) el.classList.remove('open'); }
function phShow(title, body, btns) {
  phEl().classList.add('open'); $('phsT').textContent = title; $('phsB').innerHTML = body; $('phsX').innerHTML = btns || '';
  $('phsX').querySelectorAll('[data-x]').forEach(b => b.addEventListener('click', () => b.dataset.x === 'close' && phClose()));
}
async function xSaveToPhotos(items) {
  const all = (items || []).filter(i => !i.dir), ok = all.filter(i => PH_TYPES.test(i.name)), skipped = all.length - ok.length;
  if (!ok.length) return toast(all.length ? 'Photos only takes mp4, mov, jpg, png, gif, heic - use “Make iPhone-friendly” first' : 'Select a video or photo first');
  if (!phCapable()) {
    return phShow('Open Media Hub over https', `<p class="phs-p">The iPhone only lets a website save into Photos over a secure <b>https</b> address. You are on a plain <b>http</b> one.</p>
      <p class="phs-p">Easiest fix: on the server run <code>tailscale serve --bg 5000</code> and open the <b>https://…ts.net</b> address it shows. After that, this button saves straight into Photos.</p>
      <p class="phs-p"><small>Meanwhile: for a photo, press and hold it → <b>Add to Photos</b>.</small></p>`, '<button class="btn ghost" data-x="close">Close</button>');
  }
  const queue = [...ok]; let round = 0, rounds = 0;                 // split into parts if it is a lot
  { let n = 0, b = 0, k = 1; for (const it of queue) { if (n && (n >= PH_MAX_FILES || b + it.size > PH_MAX_BYTES)) { k++; n = 0; b = 0; } n++; b += it.size; } rounds = k; }
  const next = async () => {
    const part = []; let b = 0;
    while (queue.length && part.length < PH_MAX_FILES && (!part.length || b + queue[0].size <= PH_MAX_BYTES)) { const it = queue.shift(); part.push(it); b += it.size; }
    round++; const tag = rounds > 1 ? ` (part ${round}/${rounds})` : '';
    const ctl = PH.abort = new AbortController(), files = []; let done = 0;
    phShow(`Preparing ${part.length} file${part.length === 1 ? '' : 's'}${tag}…`, '<div class="phs-bar"><i id="phsBar"></i></div><p class="phs-p" id="phsMsg">Loading…</p>', '<button class="btn ghost" data-x="close">Cancel</button>');
    try {
      for (const it of part) {
        const r = await fetch(xFileUrl(it), {signal: ctl.signal}); if (!r.ok) throw new Error('fetch');
        const total = +r.headers.get('content-length') || it.size || 0, rd = r.body && r.body.getReader(), chunks = []; let got = 0;
        if (rd) for (;;) { const {done: d, value} = await rd.read(); if (d) break; chunks.push(value); got += value.length;
          if (total) { $('phsBar').style.width = Math.min(100, ((done + got / total) / part.length) * 100) + '%'; $('phsMsg').textContent = `${done + 1} of ${part.length} · ${it.name.slice(0, 38)}`; } }
        const blob = rd ? new Blob(chunks) : await r.blob(), ext = it.name.split('.').pop().toLowerCase();
        files.push(new File([blob], it.name.split('/').pop(), {type: PMIME[ext] || blob.type})); done++;
      }
    } catch (e) { if (e.name !== 'AbortError') phShow('Could not load the files', '<p class="phs-p">Check the connection and try again.</p>', '<button class="btn ghost" data-x="close">Close</button>'); return; }
    PH.abort = null;
    const label = files.length === 1 ? (/^video/.test(files[0].type) ? 'Save video to Photos' : 'Save photo to Photos') : `Save ${files.length} to Photos`;
    phShow('Ready', `<p class="phs-p">Tap the button, then choose <b>${files.length === 1 ? (/^video/.test(files[0].type) ? 'Save Video' : 'Save Image') : 'Save ' + files.length + ' Items'}</b> in the sheet that opens.${skipped ? `<br><small>${skipped} file(s) skipped: Photos cannot take that format.</small>` : ''}</p>`,
      `<button class="btn primary" id="phsGo">📲 ${label}</button><button class="btn ghost" data-x="close">Close</button>`);
    $('phsGo').addEventListener('click', async () => {
      try { await navigator.share({files}); } catch (e) { if (e.name === 'AbortError') return; toast('Could not open the share sheet'); return; }
      if (queue.length) next(); else { phClose(); toast('Done ✓'); }
    });
  };
  next();
}

/* ---------- pro video player (used by the file preview) ---------- */
const VPI = {   // 24x24 icons
  play:'<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>', pause:'<svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></svg>',
  back:'<svg viewBox="0 0 24 24"><path d="M12 5V2L7 6l5 4V7a5 5 0 1 1-5 5H5a7 7 0 1 0 7-7z"/><text x="12" y="16.5" font-size="6.5" text-anchor="middle" font-weight="700">10</text></svg>',
  fwd:'<svg viewBox="0 0 24 24"><path d="M12 5V2l5 4-5 4V7a5 5 0 1 0 5 5h2a7 7 0 1 1-7-7z"/><text x="12" y="16.5" font-size="6.5" text-anchor="middle" font-weight="700">10</text></svg>',
  prev:'<svg viewBox="0 0 24 24"><path d="M6 6h2v12H6zm3.5 6 8.5 6V6z"/></svg>', next:'<svg viewBox="0 0 24 24"><path d="M16 6h2v12h-2zM6 18l8.5-6L6 6z"/></svg>',
  vol:'<svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9zm13.5 3A4.5 4.5 0 0 0 14 8v8a4.5 4.5 0 0 0 2.5-4z"/></svg>', mute:'<svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9zm13 .6 1.4-1.4 2.1 2.1 2.1-2.1 1.4 1.4-2.1 2.1 2.1 2.1-1.4 1.4-2.1-2.1-2.1 2.1-1.4-1.4 2.1-2.1z"/></svg>',
  loop:'<svg viewBox="0 0 24 24"><path d="M7 7h10v3l4-4-4-4v3H5v6h2zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2z"/></svg>',
  pip:'<svg viewBox="0 0 24 24"><path d="M19 7h-8v6h8zm4-4H1v18h22zm-2 16H3V5h18z"/></svg>',
  fs:'<svg viewBox="0 0 24 24"><path d="M5 5h5v2H7v3H5zm9 0h5v5h-2V7h-3zM5 14h2v3h3v2H5zm12 0h2v5h-5v-2h3z"/></svg>', fsx:'<svg viewBox="0 0 24 24"><path d="M8 5h2v5H5V8h3zm6 0h2v3h3v2h-5zM5 14h5v5H8v-3H5zm9 0h5v2h-3v3h-2z"/></svg>'
};
const VP_RATES = [0.5, 0.75, 1, 1.25, 1.5, 2];
const vpGet = (k, d) => { try { const v = localStorage.getItem('vp.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } };
const vpSet = (k, v) => { try { localStorage.setItem('vp.' + k, JSON.stringify(v)); } catch {} };
const vpFmt = t => { if (!isFinite(t)) return '0:00'; t = Math.max(0, Math.floor(t)); const h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60), s = t % 60; return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0'); };
function vpDestroy() { if (LB.vp) { try { LB.vp.destroy(); } catch {} LB.vp = null; } }
function vpKey(e) { return !!(LB.vp && LB.vp.key(e)); }
function vpMount(box, src, it) {
  const wrap = document.createElement('div'); wrap.className = 'vp'; wrap.tabIndex = 0;
  const multi = LB.list.length > 1, canPip = !!(document.pictureInPictureEnabled || document.createElement('video').webkitSetPresentationMode);
  wrap.innerHTML = `<video playsinline webkit-playsinline preload="metadata"></video><div class="vp-spin"></div><div class="vp-flash"></div><div class="vp-err" hidden></div>
    <div class="vp-ctrl"><div class="vp-bar"><div class="vp-buf"></div><div class="vp-fill"></div><div class="vp-knob"></div><div class="vp-tip">0:00</div></div>
    <div class="vp-row">${multi ? `<button data-a="prev" title="Previous (PageUp)">${VPI.prev}</button>` : ''}<button data-a="play" title="Play / pause (Space)">${VPI.play}</button>
      <button data-a="back" title="Back 10 s (J)">${VPI.back}</button><button data-a="fwd" title="Forward 10 s (L)">${VPI.fwd}</button>${multi ? `<button data-a="next" title="Next (PageDown)">${VPI.next}</button>` : ''}
      <span class="vp-vol"><button data-a="mute" title="Mute (M)">${VPI.vol}</button><input class="vp-vr" type="range" min="0" max="1" step="0.02" aria-label="Volume"></span>
      <span class="vp-time">0:00 / 0:00</span><span class="vp-sp"></span>
      <button data-a="loop" title="Loop" class="vp-t">${VPI.loop}</button><button data-a="rate" class="vp-txt" title="Speed (&lt; &gt;)">1×</button>
      ${canPip ? `<button data-a="pip" title="Picture in picture">${VPI.pip}</button>` : ''}<button data-a="fs" title="Fullscreen (F)">${VPI.fs}</button></div></div>`;
  box.appendChild(wrap);
  const v = wrap.querySelector('video'), q = s => wrap.querySelector(s), bar = q('.vp-bar'), fill = q('.vp-fill'), buf = q('.vp-buf'), knob = q('.vp-knob'), tip = q('.vp-tip');
  const timeEl = q('.vp-time'), vr = q('.vp-vr'), flash = q('.vp-flash'), errEl = q('.vp-err'), btn = a => q(`[data-a="${a}"]`);
  const posKey = 'pos:' + it.path; let hideT = 0, scrub = false, lastSave = 0, tapT = 0, lastTap = 0, flashT = 0;
  v.src = src; v.volume = vpGet('vol', 1); v.muted = vpGet('muted', false); v.playbackRate = vpGet('rate', 1); vr.value = v.muted ? 0 : v.volume;

  const dur = () => isFinite(v.duration) ? v.duration : 0;
  const say = html => { flash.innerHTML = html; flash.classList.remove('go'); void flash.offsetWidth; flash.classList.add('go'); };
  const toggle = () => { if (v.paused) { v.play().catch(() => {}); } else v.pause(); };
  const seek = d => { v.currentTime = Math.max(0, Math.min(dur() || 1e9, v.currentTime + d)); say(`<b>${d > 0 ? '+' : '−'}${Math.abs(d)} s</b>`); };
  const setVol = x => { v.muted = false; v.volume = Math.max(0, Math.min(1, x)); say(`<b>${Math.round(v.volume * 100)}%</b>`); };
  const poke = () => { wrap.classList.remove('hide'); clearTimeout(hideT); if (!v.paused && !scrub) hideT = setTimeout(() => wrap.classList.add('hide'), 2800); };
  const ui = () => {
    const d = dur(), t = v.currentTime, p = d ? t / d * 100 : 0;
    fill.style.width = p + '%'; knob.style.left = p + '%'; timeEl.textContent = vpFmt(t) + ' / ' + vpFmt(d);
    if (d && v.buffered.length) { let e = 0; for (let i = 0; i < v.buffered.length; i++) if (v.buffered.start(i) <= t + .5) e = Math.max(e, v.buffered.end(i)); buf.style.width = e / d * 100 + '%'; }
  };
  const fsEl = () => document.fullscreenElement || document.webkitFullscreenElement;
  const fsToggle = () => {
    if (fsEl()) return (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    if (wrap.requestFullscreen) wrap.requestFullscreen().catch(() => {}); else if (wrap.webkitRequestFullscreen) wrap.webkitRequestFullscreen(); else if (v.webkitEnterFullscreen) v.webkitEnterFullscreen();   // iPhone: only the video itself can go fullscreen
  };
  const pipToggle = async () => { try { if (document.pictureInPictureElement) await document.exitPictureInPicture(); else if (v.requestPictureInPicture) await v.requestPictureInPicture(); else if (v.webkitSetPresentationMode) v.webkitSetPresentationMode(v.webkitPresentationMode === 'picture-in-picture' ? 'inline' : 'picture-in-picture'); } catch {} };
  const setRate = r => { v.playbackRate = r; vpSet('rate', r); btn('rate').textContent = (r === 1 ? '1' : r) + '×'; say(`<b>${r}×</b>`); };
  const stepRate = d => setRate(VP_RATES[Math.max(0, Math.min(VP_RATES.length - 1, VP_RATES.indexOf(v.playbackRate) + d))] || 1);

  const act = a => ({
    play: toggle, back: () => seek(-10), fwd: () => seek(10), prev: () => xLbStep(-1), next: () => xLbStep(1),
    mute: () => { v.muted = !v.muted; },
    loop: () => { v.loop = !v.loop; btn('loop').classList.toggle('on', v.loop); say(`<b>Loop ${v.loop ? 'on' : 'off'}</b>`); },
    rate: () => setRate(VP_RATES[(VP_RATES.indexOf(v.playbackRate) + 1) % VP_RATES.length] || 1), pip: pipToggle, fs: fsToggle
  }[a] || (() => {}))();
  wrap.querySelector('.vp-row').addEventListener('click', e => { const b = e.target.closest('[data-a]'); if (b) { act(b.dataset.a); poke(); } });
  vr.addEventListener('input', () => { v.muted = false; v.volume = +vr.value; });

  // progress bar: tap / drag, with a time tooltip
  const at = e => { const r = bar.getBoundingClientRect(); return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)); };
  bar.addEventListener('pointerdown', e => { scrub = true; bar.setPointerCapture(e.pointerId); wrap.classList.add('scrub'); v.currentTime = at(e) * dur(); ui(); });
  bar.addEventListener('pointermove', e => { const f = at(e); tip.style.left = f * 100 + '%'; tip.textContent = vpFmt(f * dur()); if (scrub) { v.currentTime = f * dur(); ui(); } });
  const endScrub = () => { scrub = false; wrap.classList.remove('scrub'); poke(); };
  bar.addEventListener('pointerup', endScrub); bar.addEventListener('pointercancel', endScrub);

  // picture area: mouse click = play/pause, double click = fullscreen; touch: tap = show controls, double tap left/right = -/+10 s, middle = play/pause
  wrap.addEventListener('pointerup', e => {
    if (e.target.closest('.vp-ctrl') || e.target.closest('.vp-err')) return;
    if (e.pointerType === 'mouse') { if (e.button !== 0) return; clearTimeout(tapT); const now = Date.now(); if (now - lastTap < 280) { fsToggle(); lastTap = 0; } else { lastTap = now; tapT = setTimeout(toggle, 220); } wrap.focus({preventScroll: true}); return; }
    const now = Date.now(), r = wrap.getBoundingClientRect(), f = (e.clientX - r.left) / r.width;
    if (now - lastTap < 300) { clearTimeout(tapT); lastTap = 0; if (f < .35) seek(-10); else if (f > .65) seek(10); else toggle(); }
    else { lastTap = now; tapT = setTimeout(() => { wrap.classList.contains('hide') || v.paused ? poke() : wrap.classList.add('hide'); }, 300); }
  });
  wrap.addEventListener('pointermove', e => { if (e.pointerType === 'mouse') poke(); });
  wrap.addEventListener('mouseleave', () => { if (!v.paused) wrap.classList.add('hide'); });

  // video events
  v.addEventListener('play', () => { btn('play').innerHTML = VPI.pause; wrap.classList.remove('ended'); poke(); });
  v.addEventListener('pause', () => { btn('play').innerHTML = VPI.play; wrap.classList.remove('hide'); clearTimeout(hideT); });
  v.addEventListener('ended', () => { wrap.classList.add('ended'); wrap.classList.remove('hide'); vpSet(posKey, 0); });
  v.addEventListener('timeupdate', () => { if (!scrub) ui(); const n = Date.now(); if (n - lastSave > 3000 && dur() > 40) { lastSave = n; vpSet(posKey, v.ended ? 0 : v.currentTime); } });
  v.addEventListener('progress', ui);
  v.addEventListener('durationchange', ui);
  v.addEventListener('volumechange', () => { btn('mute').innerHTML = v.muted || v.volume === 0 ? VPI.mute : VPI.vol; vr.value = v.muted ? 0 : v.volume; vpSet('vol', v.volume); vpSet('muted', v.muted); });
  v.addEventListener('waiting', () => wrap.classList.add('busy')); v.addEventListener('seeking', () => wrap.classList.add('busy'));
  for (const n of ['playing', 'canplay', 'seeked']) v.addEventListener(n, () => wrap.classList.remove('busy'));
  v.addEventListener('loadedmetadata', () => {
    ui(); btn('rate').textContent = (v.playbackRate === 1 ? '1' : v.playbackRate) + '×';
    const p = vpGet(posKey, 0); if (p > 5 && dur() > 40 && p < dur() - 8) { v.currentTime = p; say(`<b>Resumed at ${vpFmt(p)}</b>`); }
  });
  v.addEventListener('error', () => {
    wrap.classList.remove('busy'); errEl.hidden = false;
    errEl.innerHTML = `<div style="font-size:2.2rem">⚠️</div><b>This video can’t be played here</b><span>Its format is not supported by this browser (often WebM / VP9 on an iPhone). Use ⬇ Download, or select it and choose <b>Make iPhone-friendly</b>.</span>`;
  });
  const fsChange = () => { btn('fs').innerHTML = fsEl() ? VPI.fsx : VPI.fs; };
  document.addEventListener('fullscreenchange', fsChange); document.addEventListener('webkitfullscreenchange', fsChange);

  v.play().catch(() => {}); wrap.focus({preventScroll: true}); poke();
  return {
    destroy() { clearTimeout(hideT); clearTimeout(tapT); document.removeEventListener('fullscreenchange', fsChange); document.removeEventListener('webkitfullscreenchange', fsChange);
      if (fsEl() === wrap) (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      if (dur() > 40 && !v.ended) vpSet(posKey, v.currentTime); v.pause(); v.removeAttribute('src'); v.load(); wrap.remove(); },
    key(e) {
      if (e.ctrlKey || e.metaKey || e.altKey) return false;
      const k = e.key; poke();
      if (k === ' ' || k === 'k' || k === 'K') toggle();
      else if (k === 'ArrowLeft') seek(-5); else if (k === 'ArrowRight') seek(5);
      else if (k === 'j' || k === 'J') seek(-10); else if (k === 'l' || k === 'L') seek(10);
      else if (k === 'ArrowUp') setVol(v.volume + .05); else if (k === 'ArrowDown') setVol(v.volume - .05);
      else if (k === 'm' || k === 'M') act('mute'); else if (k === 'f' || k === 'F') fsToggle();
      else if (k === '<') stepRate(-1); else if (k === '>') stepRate(1);
      else if (k === ',' && v.paused) v.currentTime = Math.max(0, v.currentTime - 1 / 30); else if (k === '.' && v.paused) v.currentTime += 1 / 30;
      else if (k === 'Home') v.currentTime = 0; else if (k === 'End') v.currentTime = dur();
      else if (/^[0-9]$/.test(k)) v.currentTime = dur() * (+k / 10);
      else return false;
      return true;
    }
  };
}

/* ---------- file explorer (browse · search · tags · right-click menu) ---------- */
const X = {path:'', q:'', tags:[], rows:[], sel:new Set(), last:-1, view:'grid', sort:'modified', desc:true, size:150,
           seq:0, tagsAll:null, allUsers:false, drag:null, qt:null, lp:false, lpt:null, dlgCancel:null, pop:null, bandMoved:false, selMode:false};
try { const s = JSON.parse(localStorage.getItem('hub_x') || '{}'); ['view','sort','desc','size'].forEach(k => { if (s[k] !== undefined) X[k] = s[k]; }); } catch {}
try { if (!localStorage.getItem('hub_x') && innerWidth < 700) X.size = 104; } catch {}          // smaller tiles on a phone: 3 per row
const xSave = () => { try { localStorage.setItem('hub_x', JSON.stringify({view:X.view, sort:X.sort, desc:X.desc, size:X.size})); } catch {} };
const xSearching = () => !!(X.q || X.tags.length);
const xParent = p => p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '';
const xFileUrl = it => { const [c, ...r] = it.path.split('/'); return `/files/${encodeURIComponent(c)}/${r.map(encodeURIComponent).join('/')}`; };
const xThumb = it => '/fs/thumb?path=' + encodeURIComponent(it.path);
const XICON = {video:'🎬', photo:'🖼️', audio:'🎵', other:'📄'};
const XFOLD = '<svg class="xfold" viewBox="0 0 64 52" aria-hidden="true"><path d="M4 8a4 4 0 0 1 4-4h14l6 6h28a4 4 0 0 1 4 4v30a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4z" fill="currentColor" opacity=".5"/><path d="M4 18a4 4 0 0 1 4-4h48a4 4 0 0 1 4 4v26a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4z" fill="currentColor"/></svg>';
const xHuman = n => { for (const u of ['B','KB','MB','GB','TB']) { if (n < 1024 || u === 'TB') return (u === 'B' ? Math.round(n) : n.toFixed(1)) + ' ' + u; n /= 1024; } };
const XSMART = ['vertical','horizontal','square','4k','hd','short','long'];
const xGroup = t => t[0] === '@' ? 'users' : ['youtube','tiktok','snapchat','instagram','general'].includes(t) ? 'sites'
  : ['video','photo','audio','posts','reels','stories','highlights','tagged','profile-picture'].includes(t) ? 'types' : XSMART.includes(t) ? 'smart' : /^\d{4}-\d{2}$/.test(t) ? 'dates' : 'custom';
const xChip = t => `<b class="tgc tg-${xGroup(t)}">${esc(t)}</b>`;
const xSel = () => X.rows.filter(r => X.sel.has(r.path));
const xDate = ts => new Date(ts * 1000).toLocaleString(undefined, {dateStyle:'medium', timeStyle:'short'});
const xPost = async (u, b) => { const r = await post(u, b); if (!r.success) toast(r.message || 'That did not work'); return r; };
const xAfter = () => { xLoad(true); xSide(); };

/* ----- loading + sorting ----- */
async function xLoad(keepSel) {
  const my = ++X.seq; let r;
  if (xSearching()) { const qs = new URLSearchParams(); if (X.q) qs.set('q', X.q); X.tags.forEach(t => qs.append('tag', t)); r = await api('/fs/search?' + qs); }
  else if (X.path === '@recent') r = await api('/fs/search?recent=1');
  else if (X.path === '@dupes') { r = await api('/fs/dupes'); if (r.success) { X.dupes = r; r.items = r.groups.flatMap((g, gi) => g.items.map((it, k) => Object.assign(it, {_g:gi, _k:k}))); } }
  else if (X.path === '@storage') { r = await api('/fs/storage'); if (r.success) { X.storage = r; r.items = []; } }
  else r = await api('/fs/list?path=' + encodeURIComponent(X.path));
  if (my !== X.seq) return;
  if (!r.success && r.locked) { closeFiles(); return toast('The files are locked again - open them again'); }
  if (!r.success) { toast(r.message || 'Could not open that'); if (X.path) { X.path = ''; return xLoad(); } r = {items:[]}; }
  X.rows = r.items || [];
  const have = new Set(X.rows.map(i => i.path));
  X.sel = keepSel ? new Set([...X.sel].filter(p => have.has(p))) : new Set();
  if (!keepSel) X.last = -1;
  xRender();
}
function xSorted(items) {
  const d = X.desc ? -1 : 1, k = X.sort;
  const cmp = (a, b) => k === 'name' ? a.name.localeCompare(b.name, undefined, {numeric:true}) : k === 'size' ? (a.size || 0) - (b.size || 0)
    : k === 'type' ? (a.kind || '').localeCompare(b.kind || '') || a.name.localeCompare(b.name) : a.modified - b.modified;
  return [...items].sort((a, b) => (b.dir - a.dir) || cmp(a, b) * d);
}
async function xSide() { const r = await api('/fs/tags'); if (r.success) { X.tagsAll = r; xNav(); } }

/* ----- drawing ----- */
const xTile = (it, i) => {
  const th = it.dir ? XFOLD : `<span class="xic">${XICON[it.kind]}</span>`
    + (it.kind === 'video' || it.kind === 'photo' ? `<img loading="lazy" alt="" draggable="false" data-k="${it.kind}" src="${xThumb(it)}">` : '')
    + (it.kind === 'video' ? '<i class="xplay">▶</i>' : '');
  const sub = it.dir ? (it.count != null ? `${it.count} item${it.count === 1 ? '' : 's'}` : 'Folder') : it.size_h;
  const tg = it.tags.filter(t => xGroup(t) !== 'dates').slice(0, 4).map(xChip).join('');
  return `<div class="xi${X.sel.has(it.path) ? ' sel' : ''}" data-i="${i}" data-dir="${it.dir ? 1 : 0}" draggable="true" title="${esc(it.path)}">`
    + `<div class="xth">${th}</div><div class="xnm">${esc(it.name)}</div><div class="xtg">${tg}</div><div class="xsub">${esc(sub)}</div>`
    + `<div class="xdt">${esc(xDate(it.modified))}</div><button class="xmore" aria-label="More">⋯</button></div>`;
};
function xRender() {
  const special = !xSearching() && (X.path === '@dupes' || X.path === '@storage');
  if (!special) X.rows = xSorted(X.rows);
  const g = $('xgrid'); g.className = 'xgrid ' + X.view + (X.selMode ? ' selmode' : ''); g.style.setProperty('--xs', X.size + 'px');
  g.innerHTML = special ? (X.path === '@dupes' ? xDupesHtml() : xStorageHtml()) : X.rows.length ? X.rows.map(xTile).join('')
    : `<div class="empty" style="grid-column:1/-1"><b>${xSearching() ? '🔍' : '📭'}</b>${xSearching() ? 'Nothing matches your search.' : X.path ? 'This folder is empty.' : 'No downloads yet.'}</div>`;
  $('xGrid').classList.toggle('on', X.view === 'grid'); $('xList').classList.toggle('on', X.view === 'list');
  $('xsort').value = X.sort; $('xdesc').classList.toggle('asc', !X.desc); $('xsize').value = X.size;
  xCrumbs(); xFilters(); xStatus(); xNav();
  if ($('xlb').classList.contains('open')) xLbSync();
}
function xCrumbs() {
  let h;
  if (xSearching()) h = '<button data-go="">🏠</button><span>›</span><b>Search results</b>';
  else if (X.path === '@recent') h = '<button data-go="">🏠</button><span>›</span><b>Recent</b>';
  else if (X.path === '@dupes') h = '<button data-go="">🏠</button><span>›</span><b>Duplicates</b>';
  else if (X.path === '@storage') h = '<button data-go="">🏠</button><span>›</span><b>Storage</b>';
  else { const ps = X.path ? X.path.split('/') : []; h = '<button data-go="">🏠 Home</button>' + ps.map((p, i) => `<span>›</span><button data-go="${esc(ps.slice(0, i + 1).join('/'))}">${esc(p)}</button>`).join(''); }
  $('xcrumbs').innerHTML = h;
}
const xFilters = () => { $('xfilters').innerHTML = X.tags.map(t => `<b class="tgc tg-${xGroup(t)}">${esc(t)} <u data-rmtag="${esc(t)}">✕</u></b>`).join(''); };
function xStatus() {
  const s = xSel(), size = s.reduce((a, b) => a + (b.size || 0), 0);
  $('xstat').textContent = `${X.rows.length} item${X.rows.length === 1 ? '' : 's'}` + (s.length ? ` · ${s.length} selected${size ? ' (' + xHuman(size) + ')' : ''}` : '') + (xSearching() ? ' · search results' : '');
  xActBar();
}
const XGROUPS = [['sites','🌐','Sites'], ['users','👤','Users'], ['types','🎞️','Types'], ['smart','✨','Shape & length'], ['custom','🏷️','My tags'], ['dates','📅','Months']];
function xNav() {
  const T = (X.tagsAll && X.tagsAll.tags) || [];
  const btns = XGROUPS.map(([g, ico, label]) => {
    const all = T.filter(t => t.group === g); if (!all.length) return '';
    const act = all.filter(t => X.tags.includes(t.name)).length;
    return `<button data-pop="${g}" class="${act || X.pop === g ? 'on' : ''}"><span>${ico} ${label}</span><em>${act ? '✓ ' + act : all.length + ' ›'}</em></button>`;
  }).join('');
  $('xnav').innerHTML = `<button data-go="" class="${!X.path && !xSearching() ? 'on' : ''}"><span>🏠 All files</span></button>`
    + `<button data-go="@recent" class="${X.path === '@recent' && !xSearching() ? 'on' : ''}"><span>🕒 Recent</span></button>`
    + `<button data-go="@dupes" class="${X.path === '@dupes' && !xSearching() ? 'on' : ''}"><span>👯 Duplicates</span></button>`
    + `<button data-go="@storage" class="${X.path === '@storage' && !xSearching() ? 'on' : ''}"><span>📊 Storage</span></button>`
    + `<h5>Filter by</h5>` + btns
    + (X.tags.length ? `<button data-clear="1"><span style="color:var(--err)">✕ Clear filters</span><em>${X.tags.length}</em></button>` : '');
  if (X.tagsAll) $('xdisk').textContent = `${X.tagsAll.files} files · ${X.tagsAll.total_h} used · ${X.tagsAll.free_h} free`;
}
function xPopClose() { $('xpop').classList.remove('open'); if (X.pop) { X.pop = null; xNav(); } }
function xPopList() {
  const q = ($('xpq').value || '').trim().toLowerCase();
  const rows = ((X.tagsAll && X.tagsAll.tags) || []).filter(t => t.group === X.pop && (!q || t.name.includes(q)));
  $('xpl').innerHTML = rows.map(t => `<button data-tag="${esc(t.name)}" class="${X.tags.includes(t.name) ? 'on' : ''}"><span>${X.tags.includes(t.name) ? '✓' : ''}</span><b>${esc(t.name)}</b><em>${t.count}</em></button>`).join('') || '<div class="xhint">Nothing here yet.</div>';
}
function xPop(g, btn) {
  if ($('xpop').classList.contains('open') && X.pop === g) return xPopClose();
  X.pop = g; const label = XGROUPS.find(x => x[0] === g)[2], pop = $('xpop');
  pop.innerHTML = `<h5>${label}</h5><div class="xhint" style="margin:-4px 0 8px">Tick several: files matching <b>any</b> of them are shown.</div><input class="xin" id="xpq" placeholder="Search ${label.toLowerCase()}…" autocomplete="off" spellcheck="false"><div id="xpl"></div>` + (X.tags.length ? '<button class="xclr" data-clear="1"><span></span><b>Clear all filters</b></button>' : '');
  pop.classList.add('open'); xPopList(); xNav();
  const r = btn.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(r.right + 8, innerWidth - pop.offsetWidth - 8)) + 'px';
  pop.style.top = Math.max(8, Math.min(r.top, innerHeight - pop.offsetHeight - 8)) + 'px';
  $('xpq').addEventListener('input', xPopList);
  if (matchMedia('(hover:hover)').matches) $('xpq').focus();
}
function xActBar() {                            // the bar with big buttons that appears as soon as something is selected
  const s = xSel(), bar = $('xact'); bar.classList.toggle('show', s.length > 0);
  if (!s.length) return;
  bar.innerHTML = `<b>${s.length} selected</b><span class="xsp"></span>` + [...(isIos() ? [['📲','Photos','phone']] : []), ['📦','ZIP','zip'], ['🏷️','Tags','tags'], ['📂','Move','move'], ['🗑️','Delete','del','danger'], ['✕','','clear']]
    .map(([i, l, a, c]) => `<button data-a="${a}" class="${c || ''}"${l ? '' : ' aria-label="Clear selection"'}><span>${i}</span>${l ? `<em>${l}</em>` : ''}</button>`).join('');
}
function xMarkSel() { document.querySelectorAll('#xgrid .xi').forEach(el => el.classList.toggle('sel', X.sel.has(X.rows[+el.dataset.i].path))); xStatus(); }
function xGo(p) { xPopClose(); X.q = ''; X.tags = []; $('xq').value = ''; X.path = p; X.sel.clear(); X.last = -1; $('xscroll').scrollTop = 0; $('xside').classList.remove('show'); xLoad(); }
function xSetView(v) { X.view = v; xSave(); xRender(); }

/* ----- selection ----- */
function xClick(e, i) {
  const p = X.rows[i].path;
  if (e.shiftKey && X.last >= 0) { const a = Math.min(X.last, i), b = Math.max(X.last, i); X.sel = new Set(X.rows.slice(a, b + 1).map(r => r.path)); }
  else if (e.ctrlKey || e.metaKey) { X.sel.has(p) ? X.sel.delete(p) : X.sel.add(p); X.last = i; }
  else { X.sel = new Set([p]); X.last = i; }
  xMarkSel();
}
function xArrow(k) {
  if (!X.rows.length) return;
  const cols = X.view === 'list' ? 1 : Math.max(1, getComputedStyle($('xgrid')).gridTemplateColumns.split(' ').length);
  const d = k === 'ArrowRight' ? 1 : k === 'ArrowLeft' ? -1 : k === 'ArrowDown' ? cols : -cols;
  const i = Math.max(0, Math.min(X.rows.length - 1, X.last < 0 ? 0 : X.last + d));
  X.last = i; X.sel = new Set([X.rows[i].path]); xMarkSel();
  const el = $('xgrid').children[i]; if (el && el.scrollIntoView) el.scrollIntoView({block:'nearest'});
}

/* ----- right-click menu ----- */
function xCtxClose() { $('xctx').classList.remove('open'); }
function xMenu(e, i) {
  e.preventDefault();
  if (i != null) { const p = X.rows[i].path; if (!X.sel.has(p)) { X.sel = new Set([p]); X.last = i; xMarkSel(); } } else { X.sel.clear(); xMarkSel(); }
  const s = xSel(), one = s.length === 1 ? s[0] : null; let m;
  if (!s.length) m = [['📁','New folder','mkdir'], ['🔄','Refresh','refresh'], ['☑️','Select all','all'], '-', [X.view === 'grid' ? '☰' : '▦', X.view === 'grid' ? 'List view' : 'Grid view', 'toggleview']];
  else if (one) m = [['↗️', one.dir ? 'Open folder' : 'Open / preview', 'open'], one.dir ? ['📦','Download as ZIP','zip'] : ['⬇️','Download','dl'],
    !one.dir && ['🔗','Copy link','link'], isIos() && ['📲','Save to Photos','phone'], (one.dir || one.kind === 'video') && ['📱','Make iPhone-friendly','convert'], xSearching() && ['📍','Show in folder','where'], '-', ['✏️','Rename','rename'], ['🏷️','Tags…','tags'], ['📂','Move to…','move'], '-', ['🗑️','Delete','del','danger']];
  else m = [['📦', `Download ${s.length} as ZIP`, 'zip'], isIos() && ['📲','Save to Photos','phone'], ['📱','Make iPhone-friendly','convert'], ['🏷️','Tags…','tags'], ['📂','Move to…','move'], '-', ['🗑️', `Delete ${s.length} items`, 'del', 'danger']];
  const c = $('xctx');
  c.innerHTML = m.filter(Boolean).map(x => x === '-' ? '<hr>' : `<button data-a="${x[2]}" class="${x[3] || ''}"><span>${x[0]}</span>${esc(x[1])}</button>`).join('');
  c.classList.add('open');
  c.style.left = Math.max(8, Math.min(e.clientX, innerWidth - c.offsetWidth - 8)) + 'px';
  c.style.top = Math.max(8, Math.min(e.clientY, innerHeight - c.offsetHeight - 8)) + 'px';
}
function xDownload(url) { const a = document.createElement('a'); a.href = url; a.download = ''; document.body.appendChild(a); a.click(); a.remove(); }
function xOpen(it) { it.dir ? xGo(it.path) : xView(it); }
async function xAct(a) {
  const s = xSel(), one = s[0];
  switch (a) {
    case 'open': if (one) xOpen(one); break;
    case 'dl': if (one) xDownload(xFileUrl(one) + '?dl=1'); break;
    case 'zip': if (s.length) xDownload('/fs/zip?' + s.map(x => 'p=' + encodeURIComponent(x.path)).join('&')); break;
    case 'link': { const u = location.origin + xFileUrl(one); try { await navigator.clipboard.writeText(u); toast('Link copied'); } catch { prompt('Copy this link:', u); } break; }
    case 'where': X.q = ''; X.tags = []; $('xq').value = ''; X.path = xParent(one.path); X.sel = new Set([one.path]); xLoad(true); break;
    case 'rename': xRename(one); break;
    case 'tags': xTags(); break;
    case 'move': xMove(); break;
    case 'del': xDelete(); break;
    case 'mkdir': xMkdir(); break;
    case 'refresh': xAfter(); break;
    case 'all': X.sel = new Set(X.rows.map(r => r.path)); xMarkSel(); break;
    case 'toggleview': xSetView(X.view === 'grid' ? 'list' : 'grid'); break;
    case 'phone': xSaveToPhotos(xSel()); break;
    case 'convert': xConvert(); break;
    case 'clear': X.sel.clear(); xMarkSel(); break;
  }
}

/* ----- dialogs ----- */
function xDialog(o) {
  return new Promise(resolve => {
    const d = $('xdlg');
    d.innerHTML = `<div class="xdbox"><h4>${esc(o.title)}</h4>${o.body || ''}`
      + (o.input != null ? `<input class="xin" id="xdin" value="${esc(o.input)}" placeholder="${esc(o.placeholder || '')}" autocomplete="off" spellcheck="false">` : '') + (o.after || '')
      + `<div class="xdbtns"><button class="btn ghost" id="xdno" style="padding:9px 16px">Cancel</button><button class="btn ${o.danger ? 'ghost' : 'primary'}" id="xdok" style="padding:9px 16px${o.danger ? ';color:var(--err)' : ''}">${esc(o.ok || 'OK')}</button></div></div>`;
    d.classList.add('open');
    const done = v => { d.classList.remove('open'); d.innerHTML = ''; X.dlgCancel = null; resolve(v); };
    X.dlgCancel = () => done(null);
    const inp = $('xdin'), box = d.firstElementChild;
    if (o.mount) o.mount(box);
    $('xdok').addEventListener('click', () => done(o.collect ? o.collect() : (inp ? inp.value.trim() : true)));
    $('xdno').addEventListener('click', () => done(null));
    d.onmousedown = e => { if (e.target === d) done(null); };
    if (inp) { inp.focus(); if (o.select) inp.setSelectionRange(0, o.select); if (!o.noEnter) inp.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); $('xdok').click(); } }); }
  });
}
async function xRename(it) {
  it = it || xSel()[0]; if (!it) return;
  const dot = it.dir ? -1 : it.name.lastIndexOf('.');
  const v = await xDialog({title:'Rename', input:it.name, select: dot > 0 ? dot : it.name.length, ok:'Rename'});
  if (!v || v === it.name) return;
  const r = await xPost('/fs/rename', {path:it.path, name:v});
  if (r.success) { X.sel = new Set([r.path]); xAfter(); }
}
async function xMkdir() {
  if (!X.path || X.path === '@recent' || xSearching()) return toast('Open a folder inside a site first');
  const v = await xDialog({title:'New folder', input:'New folder', select:20, ok:'Create'}); if (!v) return;
  const r = await xPost('/fs/mkdir', {path:X.path, name:v}); if (r.success) { X.sel = new Set([r.path]); xAfter(); }
}
async function xDelete(items) {
  items = items || xSel(); if (!items.length) return;
  const folders = items.filter(i => i.dir).length;
  const ok = await xDialog({title:'Delete', ok:'Delete', danger:true, body:`<p style="margin:0;color:var(--mute);font-size:.9rem">Delete <b>${items.length === 1 ? esc(items[0].name) : items.length + ' items'}</b>${folders ? ' and everything inside' : ''}? This cannot be undone.</p>`});
  if (!ok) return;
  const r = await xPost('/fs/delete', {paths:items.map(i => i.path)}); if (r.success) { toast(`Deleted ${r.deleted}`); xAfter(); }
}
async function xMove(items) {
  items = items || xSel(); if (!items.length) return;
  const skip = new Set(items.map(i => i.path)); let cur = xParent(items[0].path);
  const dest = await xDialog({title:`Move ${items.length} item${items.length === 1 ? '' : 's'} to…`, ok:'Move here', body:'<div class="xcrumbs" id="xmb" style="max-width:none"></div><div class="xpl" id="xml"></div>',
    collect: () => cur,
    mount: box => {
      const nav = async p => {
        cur = p; $('xdok').disabled = !p;
        const ps = p ? p.split('/') : [];
        $('xmb').innerHTML = '<button data-p="">🏠</button>' + ps.map((x, i) => `<span>›</span><button data-p="${esc(ps.slice(0, i + 1).join('/'))}">${esc(x)}</button>`).join('');
        const r = await api('/fs/list?dirs=1&path=' + encodeURIComponent(p));
        const dirs = (r.items || []).filter(i => !skip.has(i.path));
        $('xml').innerHTML = dirs.map(i => `<button data-p="${esc(i.path)}">📁 ${esc(i.name)}</button>`).join('') || '<div class="xhint">No folders here.</div>';
      };
      box.addEventListener('click', e => { const b = e.target.closest('[data-p]'); if (b) nav(b.dataset.p); });
      nav(cur);
    }});
  if (!dest) return;
  const r = await xPost('/fs/move', {paths:[...skip], dest});
  if (r.success) { toast(`Moved ${r.moved}` + (r.skipped ? ` · ${r.skipped} skipped (already exists)` : '')); xAfter(); }
}
async function xTags(items) {
  items = items || xSel(); if (!items.length) return;
  const orig = new Set(items.flatMap(i => i.mtags || [])), cur = new Set(orig);
  const sugg = ((X.tagsAll && X.tagsAll.tags) || []).filter(t => t.group === 'custom').slice(0, 8).map(t => t.name);
  const auto = items.length === 1 ? items[0].tags.filter(t => !(items[0].mtags || []).includes(t)) : [];
  const draw = () => {
    $('xtc').innerHTML = [...cur].map(t => `<b class="tgc tg-custom" data-rm="${esc(t)}">${esc(t)} <u>✕</u></b>`).join('') || '<span class="xhint">No tags of your own yet</span>';
    $('xts').innerHTML = sugg.filter(t => !cur.has(t)).map(t => `<b class="tgc tg-custom" data-add="${esc(t)}">+ ${esc(t)}</b>`).join('');
  };
  const add = raw => { String(raw).split(',').map(s => s.trim().toLowerCase().replace(/\s+/g, '-').replace(/[^\w@.\-]/g, '').slice(0, 30)).filter(Boolean).forEach(t => cur.add(t)); draw(); };
  const r = await xDialog({title: items.length === 1 ? 'Tags · ' + items[0].name : `Tags · ${items.length} items`, ok:'Save', input:'', placeholder:'Type a tag, press Enter…', noEnter:true,
    body:'<div class="xchips" id="xtc"></div>',
    after:'<div class="xchips" id="xts"></div>' + (auto.length ? `<div class="xhint">Automatic tags (can’t be removed)</div><div class="xchips">${auto.map(xChip).join('')}</div>` : ''),
    collect: () => { const v = $('xdin').value; if (v.trim()) add(v); return {add:[...cur].filter(t => !orig.has(t)), remove:[...orig].filter(t => !cur.has(t))}; },
    mount: box => {
      draw(); const inp = $('xdin');
      inp.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); if (inp.value.trim()) { add(inp.value); inp.value = ''; } else $('xdok').click(); }
        else if (e.key === ',') { e.preventDefault(); add(inp.value); inp.value = ''; }
      });
      box.addEventListener('click', e => { const rm = e.target.closest('[data-rm]'), ad = e.target.closest('[data-add]'); if (rm) { cur.delete(rm.dataset.rm); draw(); } else if (ad) { cur.add(ad.dataset.add); draw(); } });
    }});
  if (!r || (!r.add.length && !r.remove.length)) return;
  const res = await xPost('/fs/tag', {paths:items.map(i => i.path), add:r.add, remove:r.remove}); if (res.success) { toast('Tags saved'); xAfter(); }
}

/* ----- preview viewer ----- */
const LB = {list:[], i:0};
function xView(it) { LB.list = X.rows.filter(r => !r.dir); LB.i = Math.max(0, LB.list.findIndex(r => r.path === it.path)); $('xlb').classList.add('open'); xLb(); }
function xLbInfo() {
  const it = LB.list[LB.i]; if (!it) return;
  $('xlbt').textContent = it.name;
  $('xlbi').innerHTML = `<span>${LB.i + 1} / ${LB.list.length}</span><span>${esc(it.size_h)}</span><span>${esc(xDate(it.modified))}</span>` + it.tags.map(xChip).join('');
}
function xLb() {
  const it = LB.list[LB.i]; if (!it) return xLbClose();
  vpDestroy();
  const u = esc(xFileUrl(it));
  if (it.kind === 'video') { $('xlbm').innerHTML = ''; LB.vp = vpMount($('xlbm'), xFileUrl(it), it); }
  else $('xlbm').innerHTML = it.kind === 'photo' ? `<img src="${u}" alt="">`
    : it.kind === 'audio' ? `<div class="xaud"><div style="font-size:4rem">🎵</div><audio src="${u}" controls autoplay></audio></div>`
    : '<div class="xaud"><div style="font-size:4rem">📄</div><p>No preview for this file type - use Download.</p></div>';
  $('xlbdl').href = u + '?dl=1'; xLbInfo();
}
function xLbSync() {                       // the list was reloaded while the viewer is open: keep it in step without restarting the video
  const cp = LB.list[LB.i] && LB.list[LB.i].path;
  LB.list = X.rows.filter(r => !r.dir); const j = LB.list.findIndex(r => r.path === cp);
  if (j >= 0) { LB.i = j; xLbInfo(); } else if (!LB.list.length) xLbClose(); else { LB.i = Math.min(LB.i, LB.list.length - 1); xLb(); }
}
function xLbClose() { vpDestroy(); $('xlb').classList.remove('open'); $('xlbm').innerHTML = ''; }
function xLbStep(d) { if (LB.list.length) { LB.i = (LB.i + d + LB.list.length) % LB.list.length; xLb(); } }

async function xConvert() {
  const s = xSel(); if (!s.length) return;
  const ok = await xDialog({title:'Make iPhone-friendly', ok:'Convert', body:'<p style="margin:0;color:var(--mute);font-size:.9rem">Makes a copy of each video as <b>name [iphone].mp4</b> (H.264) that the iPhone Photos app accepts. Your originals stay where they are. Long videos take a while.</p>'});
  if (!ok) return;
  const r = await xPost('/fs/convert', {paths: s.map(i => i.path)}); if (!r.success) return;
  toast(`Converting ${r.count} video${r.count === 1 ? '' : 's'}…`); xWatchJob(r.job_id);
}
async function xWatchJob(id) {                          // progress in the status bar, then refresh the list
  while ($('fmodal').classList.contains('open')) {
    const d = await api('/jobs?ids=' + id), j = d.success && d.jobs[0]; if (!j) return;
    $('xstat').textContent = '⏳ ' + j.message;
    if (j.status === 'done' || j.status === 'error') { toast((j.status === 'done' ? '✅ ' : '⚠️ ') + j.message); return xAfter(); }
    await new Promise(r => setTimeout(r, 1500));
  }
}

/* ----- jump to a finished download ----- */
async function xReveal(paths) {
  if (!paths || !paths.length) return;
  if (!(await xUnlock())) return;
  $('fmodal').classList.add('open');
  X.q = ''; X.tags = []; $('xq').value = ''; X.path = xParent(paths[0]); X.sel = new Set(paths); X.last = -1;
  await xLoad(true); xSide();
  const i = X.rows.findIndex(r => X.sel.has(r.path)), el = i >= 0 && $('xgrid').children[i];
  if (el && el.scrollIntoView) el.scrollIntoView({block:'center'});
}
const xRevealJob = j => j && xReveal((j.files || []).map(f => j.category + '/' + f));

/* ----- open / close + wiring ----- */
function closeFiles() {
  const was = $('fmodal').classList.contains('open');
  xCtxClose(); xPopClose(); xLbClose(); if (X.dlgCancel) X.dlgCancel();
  $('fmodal').classList.remove('open'); $('xside').classList.remove('show');
  if (was) { X.rows = []; X.sel.clear(); X.tagsAll = null; $('xgrid').innerHTML = ''; fetch('/fs/lock', {method:'POST'}).catch(() => {}); }   // lock again + forget what was shown
}
async function openFiles() { if (!(await xUnlock())) return; $('fmodal').classList.add('open'); xLoad(); xSide(); }

function initExplorer() {
  const g = $('xgrid'), sc = $('xscroll'), tile = e => e.target.closest('.xi');
  g.addEventListener('click', e => {
    const t = tile(e); if (!t) return; const i = +t.dataset.i;
    if (X.lp) { X.lp = false; return; }
    if (e.target.closest('.xmore')) { const r = e.target.getBoundingClientRect(); xMenu({preventDefault() {}, clientX:r.left, clientY:r.bottom}, i); e.stopPropagation(); return; }
    if (X.selMode) { const p = X.rows[i].path; X.sel.has(p) ? X.sel.delete(p) : X.sel.add(p); X.last = i; xMarkSel(); return; }
    if (matchMedia('(hover:none)').matches && !e.shiftKey) { X.sel = new Set([X.rows[i].path]); xOpen(X.rows[i]); return; }
    xClick(e, i);
  });
  g.addEventListener('dblclick', e => { const t = tile(e); if (t && !X.selMode) xOpen(X.rows[+t.dataset.i]); });
  sc.addEventListener('contextmenu', e => { const t = tile(e); xMenu(e, t ? +t.dataset.i : null); });
  sc.addEventListener('click', e => { if (X.bandMoved) { X.bandMoved = false; return; } if (!tile(e)) { X.sel.clear(); xMarkSel(); } });
  g.addEventListener('touchstart', e => {
    const t = tile(e); if (!t) return; const tp = e.touches[0];
    X.lpt = setTimeout(() => { X.lp = true; setTimeout(() => { X.lp = false; }, 900); xMenu({preventDefault() {}, clientX:tp.clientX, clientY:tp.clientY}, +t.dataset.i); }, 550);
  }, {passive:true});
  ['touchend', 'touchmove', 'touchcancel'].forEach(ev => g.addEventListener(ev, () => clearTimeout(X.lpt), {passive:true}));
  g.addEventListener('error', e => {                         // thumbnail failed: try the original photo once, else show the icon
    const im = e.target; if (im.tagName !== 'IMG') return;
    if (im.dataset.k === 'photo' && !im.dataset.t) { im.dataset.t = '1'; im.src = xFileUrl(X.rows[+im.closest('.xi').dataset.i]); } else im.classList.add('bad');
  }, true);
  /* drag files onto a folder to move them */
  g.addEventListener('dragstart', e => {
    const t = tile(e); if (!t) return; const p = X.rows[+t.dataset.i].path;
    if (!X.sel.has(p)) { X.sel = new Set([p]); xMarkSel(); }
    X.drag = [...X.sel]; e.dataTransfer.setData('text/plain', 'hub-move'); e.dataTransfer.effectAllowed = 'move';
  });
  g.addEventListener('dragover', e => {
    const t = tile(e), r = t && X.rows[+t.dataset.i];
    document.querySelectorAll('.xi.drop').forEach(x => x.classList.remove('drop'));
    if (X.drag && r && r.dir && !X.drag.includes(r.path)) { e.preventDefault(); e.stopPropagation(); t.classList.add('drop'); }
  });
  g.addEventListener('drop', async e => {
    const t = tile(e), r = t && X.rows[+t.dataset.i]; if (!X.drag || !r || !r.dir) return;
    e.preventDefault(); e.stopPropagation(); t.classList.remove('drop');
    const paths = X.drag; X.drag = null;
    const res = await xPost('/fs/move', {paths, dest:r.path}); if (res.success) { toast(`Moved ${res.moved}` + (res.skipped ? ` · ${res.skipped} skipped` : '')); xAfter(); }
  });
  document.addEventListener('dragend', () => { X.drag = null; document.querySelectorAll('.xi.drop').forEach(x => x.classList.remove('drop')); });

  /* rubber-band selection: hold the left button on empty space and drag a rectangle (like Windows) */
  const band = $('xband'); let bs = null;
  sc.addEventListener('mousedown', e => {
    if (e.button !== 0 || e.target.closest('.xi, .xpanel')) return;
    const r = sc.getBoundingClientRect();
    if (e.clientX - r.left >= sc.clientWidth || e.clientY - r.top >= sc.clientHeight) return;      // that was the scrollbar
    e.preventDefault(); sc.focus({preventScroll:true});
    bs = {x:e.clientX - r.left + sc.scrollLeft, y:e.clientY - r.top + sc.scrollTop, cx:e.clientX, cy:e.clientY, moved:false, base:(e.ctrlKey || e.metaKey || e.shiftKey) ? new Set(X.sel) : new Set()};
  });
  document.addEventListener('mousemove', e => {
    if (!bs) return;
    if (!bs.moved && Math.hypot(e.clientX - bs.cx, e.clientY - bs.cy) < 5) return;
    bs.moved = true;
    const r = sc.getBoundingClientRect();
    if (e.clientY < r.top + 28) sc.scrollTop -= 18; else if (e.clientY > r.bottom - 28) sc.scrollTop += 18;
    const x = Math.max(0, Math.min(sc.scrollWidth, e.clientX - r.left + sc.scrollLeft)), y = Math.max(0, Math.min(sc.scrollHeight, e.clientY - r.top + sc.scrollTop));
    const L = Math.min(bs.x, x), T = Math.min(bs.y, y), W = Math.abs(x - bs.x), H = Math.abs(y - bs.y);
    band.style.cssText = `display:block;left:${L}px;top:${T}px;width:${W}px;height:${H}px`;
    const sel = new Set(bs.base);
    document.querySelectorAll('#xgrid .xi').forEach(el => {
      const p = X.rows[+el.dataset.i].path;
      if (el.offsetLeft < L + W && el.offsetLeft + el.offsetWidth > L && el.offsetTop < T + H && el.offsetTop + el.offsetHeight > T) sel.add(p);
      el.classList.toggle('sel', sel.has(p));
    });
    X.sel = sel; X.last = -1; xStatus();
  });
  document.addEventListener('mouseup', () => {
    if (!bs) return; band.style.display = 'none'; X.bandMoved = bs.moved; bs = null;
    setTimeout(() => { X.bandMoved = false; }, 60);
  });

  $('xact').addEventListener('click', e => { const b = e.target.closest('[data-a]'); if (b) xAct(b.dataset.a); });
  $('xSelBtn').addEventListener('click', () => { X.selMode = !X.selMode; $('xSelBtn').classList.toggle('on', X.selMode); $('xgrid').classList.toggle('selmode', X.selMode); });
  if (isIos()) $('xlbphone').title = 'Save to Photos'; else $('xlbphone').style.display = 'none';
  $('xlbphone').addEventListener('click', () => { const it = LB.list[LB.i]; if (it) xSaveToPhotos([it]); });
  let sw = null;                                       // preview: swipe down = close, swipe left/right = next / previous
  $('xlbm').addEventListener('touchstart', e => { const tp = e.touches[0]; sw = e.target.closest('.vp-ctrl') ? null : {x:tp.clientX, y:tp.clientY, v:!!e.target.closest('.vp')}; }, {passive:true});
  $('xlbm').addEventListener('touchend', e => {
    if (!sw) return; const tp = e.changedTouches[0], dx = tp.clientX - sw.x, dy = tp.clientY - sw.y, v = sw.v; sw = null;
    if (Math.abs(dy) > 90 && Math.abs(dy) > Math.abs(dx) * 1.5) xLbClose();
    else if (!v && Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) xLbStep(dx < 0 ? 1 : -1);
  }, {passive:true});
  $('xctx').addEventListener('click', e => { const b = e.target.closest('button[data-a]'); if (b) { xCtxClose(); xAct(b.dataset.a); } });
  document.addEventListener('mousedown', e => { if (!e.target.closest('#xctx')) xCtxClose(); });
  sc.addEventListener('scroll', xCtxClose, {passive:true});
  $('xnav').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.go !== undefined) xGo(b.dataset.go);
    else if (b.dataset.pop) xPop(b.dataset.pop, b);
    else if (b.dataset.clear) { X.tags = []; X.sel.clear(); xPopClose(); xLoad(); }
  });
  $('xpop').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.clear) { X.tags = []; X.sel.clear(); xPopClose(); xLoad(); return; }
    if (b.dataset.tag) { const k = X.tags.indexOf(b.dataset.tag); k >= 0 ? X.tags.splice(k, 1) : X.tags.push(b.dataset.tag); X.sel.clear(); xPopList(); xLoad(); }
  });
  document.addEventListener('mousedown', e => { if (!e.target.closest('#xpop') && !e.target.closest('[data-pop]')) xPopClose(); });
  $('xcrumbs').addEventListener('click', e => { const b = e.target.closest('[data-go]'); if (b) xGo(b.dataset.go); });
  $('xfilters').addEventListener('click', e => { const b = e.target.closest('[data-rmtag]'); if (b) { X.tags = X.tags.filter(t => t !== b.dataset.rmtag); xLoad(); } });
  $('xq').addEventListener('input', () => { clearTimeout(X.qt); X.qt = setTimeout(() => { X.q = $('xq').value.trim(); X.sel.clear(); xLoad(); }, 250); });
  $('xUpBtn').addEventListener('click', () => { if (xSearching() || X.path === '@recent') xGo(''); else if (X.path) xGo(xParent(X.path)); });
  $('xGrid').addEventListener('click', () => xSetView('grid')); $('xList').addEventListener('click', () => xSetView('list'));
  $('xsort').addEventListener('change', e => { X.sort = e.target.value; xSave(); xRender(); });
  $('xdesc').addEventListener('click', () => { X.desc = !X.desc; xSave(); xRender(); });
  $('xsize').addEventListener('input', e => { X.size = +e.target.value; $('xgrid').style.setProperty('--xs', X.size + 'px'); xSave(); });
  $('xMenuBtn').addEventListener('click', () => $('xside').classList.toggle('show'));
  $('xlbx').addEventListener('click', xLbClose); $('xlbp').addEventListener('click', () => xLbStep(-1)); $('xlbn').addEventListener('click', () => xLbStep(1));
  $('xlbtag').addEventListener('click', () => { const it = LB.list[LB.i]; if (it) xTags([it]); });
  $('xlbdel').addEventListener('click', () => { const it = LB.list[LB.i]; if (it) xDelete([it]); });
  $('xlb').addEventListener('click', e => { if (e.target.id === 'xlbm' || e.target.id === 'xlb') xLbClose(); });
  $('xpop').addEventListener('contextmenu', async e => {      // right-click a tag = jump to the folder where its files are saved
    const b = e.target.closest('button[data-tag]'); if (!b) return;
    e.preventDefault();
    const r = await api('/fs/search?' + new URLSearchParams({tag: b.dataset.tag}));
    const f = r.success && r.items && r.items[0];
    if (!f) return toast('No files with that tag');
    xPopClose(); X.q = ''; X.tags = []; $('xq').value = '';
    X.path = xParent(f.path); X.sel = new Set([f.path]); X.last = -1;
    await xLoad(true); xSide();
    toast('📍 ' + X.path);
  });
}

/* keyboard: runs before the page-wide shortcuts, and only while the explorer is open */
document.addEventListener('keydown', e => {
  if (!$('fmodal').classList.contains('open')) return;
  const k = e.key, stop = () => e.stopPropagation();
  if ($('xdlg').classList.contains('open')) { if (k === 'Escape') { stop(); X.dlgCancel && X.dlgCancel(); } return; }
  if ($('xctx').classList.contains('open')) { if (k === 'Escape') { stop(); xCtxClose(); } return; }
  if ($('xpop').classList.contains('open')) { if (k === 'Escape') { stop(); xPopClose(); } return; }
  if ($('phs') && $('phs').classList.contains('open')) { if (k === 'Escape') { stop(); phClose(); } return; }
  if ($('xlb').classList.contains('open')) {
    if (LB.vp && vpKey(e)) { e.preventDefault(); stop(); return; }
    if (k === 'PageDown') xLbStep(1); else if (k === 'PageUp') xLbStep(-1);
    else if (k === 'Escape') { stop(); xLbClose(); } else if (k === 'ArrowRight') xLbStep(1); else if (k === 'ArrowLeft') xLbStep(-1);
    else if (k === 'Delete' && LB.list[LB.i]) xDelete([LB.list[LB.i]]);
    return;
  }
  if (isTyping(e.target)) {
    if (e.target.id === 'xq') {
      if (k === 'Escape') { stop(); if (e.target.value) { e.target.value = ''; X.q = ''; xLoad(); } else e.target.blur(); }
      else if (k === 'Enter') { clearTimeout(X.qt); X.q = e.target.value.trim(); xLoad(); }
    }
    return;
  }
  if (k === 'Escape') { if (X.sel.size) { stop(); X.sel.clear(); xMarkSel(); } return; }
  if (e.ctrlKey || e.metaKey) { if (k.toLowerCase() === 'a') { e.preventDefault(); xAct('all'); } return; }
  if (k === '/') { e.preventDefault(); $('xq').focus(); }
  else if (k === 'Delete') xDelete();
  else if (k === 'F2') { e.preventDefault(); xRename(); }
  else if (k === 'Enter') xAct('open');
  else if (k === 'Backspace') { e.preventDefault(); $('xUpBtn').click(); }
  else if (k.startsWith('Arrow')) { e.preventDefault(); xArrow(k); }
}, true);

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
  const pm = $('pmodal'); pm.addEventListener('click', e => { if (e.target === pm) closePicker(); });
  safeBind('openFilesBtn', 'click', openFiles);
  safeBind('closeFilesBtn', 'click', closeFiles);
  initExplorer();
}

/* ---------- shell: left menu, watchlist panel, password for the files ---------- */
function openWatch() { const d = $('wdr'); d.classList.remove('closing'); d.classList.add('open'); loadWatches(); }
function closeWatch() { const d = $('wdr'); if (!d.classList.contains('open') || d.classList.contains('closing')) return; d.classList.add('closing'); setTimeout(() => d.classList.remove('open', 'closing'), 330); }

let pinResolve = null;
function pinDone(ok) { $('pinmodal').classList.remove('open'); const r = pinResolve; pinResolve = null; if (r) r(ok); }
function closePin() { if (pinResolve) pinDone(false); }
function xUnlock() {                                   // resolves true only when the password was right
  return new Promise(res => {
    closePin(); pinResolve = res;
    $('pinerr').textContent = ''; $('pinin').value = ''; $('pinlock').textContent = '🔒'; $('pinlock').classList.remove('open');
    $('pinmodal').classList.add('open'); setTimeout(() => $('pinin').focus(), 60);
  });
}
async function pinSubmit() {
  const pin = $('pinin').value.trim(); if (!pin) return;
  $('pinOk').disabled = true; const r = await post('/fs/unlock', {pin}); $('pinOk').disabled = false;
  if (r.success) { $('pinlock').textContent = '🔓'; $('pinlock').classList.add('open'); setTimeout(() => pinDone(true), 450); return; }
  $('pinerr').textContent = r.message || 'Wrong password.'; $('pinin').value = '';
  $('pinin').classList.remove('bad'); void $('pinin').offsetWidth; $('pinin').classList.add('bad'); $('pinin').focus();
}
async function showPhoneSetup(fresh) {
  if (!(await xUnlock())) return;                                             // the key is secret: password first
  const r = fresh ? await api('/fs/phonekey', {method:'POST'}) : await api('/fs/phonekey');
  fetch('/fs/lock', {method:'POST'}).catch(() => {});                          // and lock again right away
  if (!r.success) return toast('Could not read the key');
  const g = `${r.base}/phone/grab?key=${r.key}`;
  const sf = (typeof saveFolder === 'function' && saveFolder()) || 'Good';
  const g3 = `${g}&wait=0&folder=${encodeURIComponent(sf)}`;
  $('phoneSetup').innerHTML = `<div class="sxh">① Save to Photos <small>- no Shortcut, no ZIP</small></div><ol class="phsteps">
    <li>iPhone only allows websites to save into Photos over a secure <b>https</b> address.</li>
    <li>Easiest way (Tailscale): on the server run <code>tailscale serve --bg 5000</code>, then open the <b>https://…ts.net</b> address it prints on your iPhone (Safari → Share → Add to Home Screen).</li>
    <li>In the file browser tap a video or photo → <b>📲</b> → <b>Save to Photos</b> (it also works for several selected files).</li></ol>
    <div class="sxh">② Optional: Shortcut “Media Hub Share” <small>- share a link from TikTok, Instagram, YouTube…</small></div><ol class="phsteps">
    <li><b>+</b> → name it <b>Media Hub Share</b> → tap ⓘ → turn on <b>Show in Share Sheet</b> → accepted types: <b>URLs</b> and <b>Text</b>.</li>
    <li>Add <b>Get Contents of URL</b> with this URL: <code>${esc(g)}</code> <button class="mini" data-cp="${esc(g)}">Copy</button><br>Tap <b>Show More</b>: Method <b>POST</b>, Request Body <b>JSON</b>, add a <b>Text</b> field named <b>url</b> with the value <b>Shortcut Input</b>.</li>
    <li>Add <b>Extract Archive</b>, then <b>Save to Photo Album</b> (input <b>Extracted Files</b>).</li>
    <li>Now in any app: <b>Share → Media Hub Share</b>. The video lands in Photos (it waits up to about 50 seconds; longer ones finish inside Media Hub - turn on alerts in Settings → Notifications).</li></ol>
    <div class="sxh">③ Shortcut “Save to <span class="scf">${esc(sf)}</span>” <small>- downloads on the server only, nothing opens, nothing is saved on the phone</small></div>
    <div class="scsetup">
      <div class="fld scfld"><label for="scFolder">Folder on the server</label><input id="scFolder" value="${esc(sf)}" maxlength="40" autocomplete="off" spellcheck="false" placeholder="Good"></div>
      <div class="sclink"><span>Your link</span><code id="scUrl">${esc(g3)}</code><button class="mini" id="scCopy" data-cp="${esc(g3)}">📋 Copy</button></div>
    </div>
    <div class="scg" id="scg" data-folder="${esc(sf)}"></div>
    <p class="hint">Type any folder name above - the link updates. Make one Shortcut per folder if you like (Good, Funny…).</p>
  <p class="hint">Keep these links private. <button class="linkbtn" style="padding:0" data-newkey="1">Make a new key</button> if one leaks. Away from home? Use your Tailscale address instead of the local one.</p>`;
  scgInit();
}
function initShell() {
  $('phoneSetup').addEventListener('input', e => {                    // ③: the folder box rewrites the Shortcut link
    if (e.target.id !== 'scFolder') return;
    const f = cleanFolder(e.target.value) || 'Good', u = $('scUrl').textContent.replace(/&folder=[^&]*/, '&folder=' + encodeURIComponent(f));
    $('scUrl').textContent = u; $('scCopy').dataset.cp = u;
    document.querySelectorAll('#phoneSetup .scf').forEach(el => { el.textContent = f; });
    if ($('scg')) { $('scg').dataset.folder = f; scgGo(SCG.i); }               // the mock phone follows the new name
  });
  $('phoneSetup').addEventListener('click', async e => {
    const c = e.target.closest('[data-cp]'), n = e.target.closest('[data-newkey]');
    if (c) { try { await navigator.clipboard.writeText(c.dataset.cp); toast('Link copied'); } catch { prompt('Copy this link:', c.dataset.cp); } }
    else if (n && confirm('Make a new key? The old Shortcut link stops working.')) showPhoneSetup(true);
  });
  document.querySelectorAll('#rail [data-nav]').forEach(b => b.addEventListener('click', () => {
    const n = b.dataset.nav;
    if (n === 'home') setTab('home'); else if (n === 'live') setTab('live'); else if (n === 'watch') openWatch(); else if (n === 'files') openFiles(); else if (n === 'queue') openQueue();
    else if (n === 'settings') openSettings(); else if (n === 'keys') $('kmodal').classList.add('open');
  }));
  safeBind('navFilesBtn', 'click', openFiles);
  safeBind('wdrClose', 'click', closeWatch); safeBind('wdrBack', 'click', closeWatch);
  safeBind('pinOk', 'click', pinSubmit); safeBind('pinNo', 'click', () => pinDone(false));
  $('pinin').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); pinSubmit(); } });
  const pm = $('pinmodal'); pm.addEventListener('click', e => { if (e.target === pm) pinDone(false); });
  $('sxnav').addEventListener('click', e => { const b = e.target.closest('[data-sec]'); if (b) sxPane(b.dataset.sec); });
}

/* ---------- Safe Bootup ---------- */
/* ---------- polish: greeting, header on scroll, tap ripple, security banner ---------- */
function initPolish() {
  const g = $('greet'); if (g) { const h = new Date().getHours(); g.innerHTML = `${h < 5 ? '🌙' : h < 12 ? '☀️' : h < 18 ? '👋' : '🌆'} <b>${h < 5 ? 'Still up?' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening'}</b> - what are we saving?`; }
  const top = document.querySelector('.top'); if (top) addEventListener('scroll', () => top.classList.toggle('scrolled', scrollY > 8), {passive: true});
  document.addEventListener('pointerdown', e => {
    const t = e.target.closest('.brandbtn, .btn'); if (!t || t.disabled) return;
    const r = t.getBoundingClientRect(), d = document.createElement('i'); d.className = 'rip'; d.style.left = e.clientX - r.left + 'px'; d.style.top = e.clientY - r.top + 'px';
    t.appendChild(d); setTimeout(() => d.remove(), 650);
    if (e.pointerType === 'touch' && navigator.vibrate) navigator.vibrate(8);
  }, {passive: true});
  checkSecurity();
}
async function checkSecurity() {
  try {
    const d = await (await fetch('/security/status', {cache: 'no-store'})).json(); if (!d.success || !d.problems.length) return;
    let until = 0; try { until = +localStorage.getItem('sec.hide') || 0; } catch {}
    if (Date.now() < until) return;
    const b = $('secBanner'); if (!b) return;
    b.innerHTML = `<div class="sec-h"><b>🔐 Your private files need a stronger lock</b><button class="sec-x" aria-label="Hide for a day">✕</button></div><ul>${d.problems.map(p => `<li>${esc(p.text)}</li>`).join('')}</ul>`
      + `<details><summary>How to fix</summary><p>Add this to your docker-compose file and restart. Then open the site once with <code>/?token=YOUR_TOKEN</code>.</p><pre>environment:\n  - HUB_TOKEN=a-long-random-secret\n  - HUB_FILES_PIN=your-long-file-password\n  - HUB_STRICT=1</pre></details>`;
    const bh = b.querySelector('.sec-h b'); if (bh) bh.addEventListener('click', () => b.classList.toggle('open'));      // phone: tap to read the details
    b.hidden = false; b.querySelector('.sec-x').addEventListener('click', () => { b.hidden = true; try { localStorage.setItem('sec.hide', String(Date.now() + 864e5)); } catch {} });
  } catch {}
}

function phoneTweaks() {                       // only on touch screens
  if (!(window.matchMedia && matchMedia('(pointer:coarse)').matches)) return;
  const ph = {url:'Paste a link or @user', fname:'Name it (optional)', xq:'Search files…'};
  for (const k in ph) { const el = $(k); if (el) el.placeholder = ph[k]; }
  const h = document.querySelector('.wdr-head'); let y0 = null;           // swipe the watchlist header down to close it
  if (h) {
    h.addEventListener('touchstart', e => { y0 = e.touches[0].clientY; }, {passive:true});
    h.addEventListener('touchend', e => { if (y0 !== null && e.changedTouches[0].clientY - y0 > 50) closeWatch(); y0 = null; }, {passive:true});
  }
}
function bootApp() {
    applyUi();
    phoneTweaks();
    initDomInjects();
    applyUi();                 // again: the accent swatches only exist after initDomInjects()
    initLooksAndPresets();
    initStaticEventBindings();
    initPickFilesBindings();
    initShell();
    initPolish();
    loadSettings();
    setOpts({});
    watch();
    setTimeout(checkClipboard, 700);
    if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('/sw.js').catch(() => {});      // lets Android install it as an app
    console.log("🚀 Media Hub Boot Sequence Complete!");
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootApp);
} else {
    bootApp();
}

/* ================= download queue: reorder, hold, pause, cancel ================= */
const Q = {timer:null, busy:false};
const qIds = () => [...new Set([...tracked(), ...groups().flat()])];
function openQueue() { $('qmodal').classList.add('open'); qLoad(); clearInterval(Q.timer); Q.timer = setInterval(qLoad, 1500); }
function closeQueue() { if (!$('qmodal')) return; $('qmodal').classList.remove('open'); clearInterval(Q.timer); Q.timer = null; }
async function qLoad() {
  if (Q.busy) return; Q.busy = true;
  try {
    const ids = qIds();
    const d = ids.length ? await api('/queue?ids=' + ids.map(encodeURIComponent).join(',')) : {success:true, jobs:[], slots:3};
    if (d.success) qRender(d.jobs || [], d.slots || 3);
  } finally { Q.busy = false; }
}
function qRender(jobs, slots) {
  const run = jobs.filter(j => j.status === 'running'), wait = jobs.filter(j => j.status === 'queued'), held = jobs.filter(j => j.status === 'paused');
  $('qsum').textContent = jobs.length ? `${run.length} of ${slots} slots busy · ${wait.length} waiting${held.length ? ' · ' + held.length + ' paused' : ''}` : '';
  $('qPauseAll').disabled = !(run.some(j => j.can_pause) || wait.length); $('qResumeAll').disabled = !held.length;
  const row = (j, i, list) => {
    const st = j.status, p = Math.round(j.progress || 0);
    const btn = (a, ico, lab, dis) => `<button class="qb${a === 'cancel' ? ' del' : ''}" data-qa="${a}" data-id="${esc(j.id)}" title="${lab}" aria-label="${lab}"${dis ? ' disabled' : ''}>${ico}</button>`;
    const move = st === 'queued' ? btn('top', '⤒', 'Start next', i === 0) + btn('up', '↑', 'Move up', i === 0) + btn('down', '↓', 'Move down', i === list.length - 1) : '';
    const pr = st === 'paused' ? btn('resume', '▶', 'Resume') : (st === 'queued' || j.can_pause) ? btn('pause', '⏸', 'Pause') : '';
    return `<div class="qrow q-${st}${j.live ? ' q-live' : ''}"><div class="qpos">${j.live && st === 'running' ? '<b class="recdot">REC</b>' : st === 'running' ? `<div class="pring${p < 1 ? ' ind' : ''}" style="--p:${p};--ringbg:var(--modal)"><b>${p}</b></div>` : st === 'queued' ? `<b>#${j.position || i + 1}</b>` : '⏸'}</div>`
      + `<div class="qmain"><b>${esc(j.label)}</b><small>${esc(j.message || '')}${j.speed ? ' · ' + esc(j.speed) : ''}${j.eta ? ' · ETA ' + esc(j.eta) : ''}</small>${st === 'running' && !j.live ? `<div class="qbar"><i style="width:${p}%"></i></div>` : ''}</div>`
      + `<div class="qctl">${move}${pr}${j.live ? btn('cancel', '⏹', 'Stop &amp; save') : btn('cancel', '✕', 'Cancel')}</div></div>`;
  };
  const sec = (t, list) => list.length ? `<div class="qh">${t}<em>${list.length}</em></div>` + list.map(row).join('') : '';
  $('qlist').innerHTML = jobs.length ? sec('⚡ Downloading now', run) + sec('⏳ Waiting in line', wait) + sec('⏸ Paused', held)
    : '<div class="empty"><b>🧘</b>Nothing in the queue. Paste a few links (or use “Paste many links at once”) and they line up here.</div>';
}
async function qAct(id, a) {
  const r = await post(`/queue/${encodeURIComponent(id)}/${a}`, {});
  if (!r.success) toast(r.message || 'That did not work'); else if (a === 'pause') toast('Paused - it continues where it stopped');
  qLoad(); watch();
}
async function qAll(a) {
  const d = await api('/queue?ids=' + qIds().map(encodeURIComponent).join(',')); if (!d.success) return;
  const list = (d.jobs || []).filter(j => a === 'pause' ? (j.status === 'queued' || (j.status === 'running' && j.can_pause)) : j.status === 'paused');
  for (const j of list) await post(`/queue/${encodeURIComponent(j.id)}/${a}`, {});
  toast(list.length ? (a === 'pause' ? `Paused ${list.length}` : `Resumed ${list.length}`) : 'Nothing to change'); qLoad(); watch();
}

/* ================= watchlist digest ================= */
function digestNote(n) {
  const on = $('digestSel').value === 'daily';
  $('digestHourBox').hidden = !on;
  $('digestMsg').textContent = on ? (n ? `${n} new post${n === 1 ? '' : 's'} waiting for the next digest.` : 'Nothing waiting yet.') : 'You get one alert per account, right when it posts.';
}
async function saveDigest() {
  const r = await post('/settings', {digest: $('digestSel').value, digest_hour: $('digestHour').value});
  if (!r.success) return toast(r.message || 'Could not save');
  digestNote(r.digest_waiting || 0); toast(r.digest === 'daily' ? `One digest a day at ${String(r.digest_hour).padStart(2, '0')}:00 ✓` : 'Instant watchlist alerts ✓');
}

/* ================= theme presets ================= */
const LOOKS_THEME = {classic:'dark', midnight:'dark', neon:'dark', sunset:'dark', paper:'light'};
function applyLook(p) {
  if (!LOOKS_THEME[p]) return;
  ui.palette = p; ui.theme = LOOKS_THEME[p];
  try { localStorage.setItem(UI_KEY, JSON.stringify(ui)); } catch {}
  applyUi(); toast(`${p[0].toUpperCase() + p.slice(1)} look ✓`);
}

/* ================= files: duplicates + storage ================= */
function xDupesHtml() {
  const d = X.dupes; if (!d) return '';
  if (!d.groups.length) return '<div class="xpanel empty" style="grid-column:1/-1"><b>✨</b>No duplicates - every file is saved only once.</div>';
  let i = 0;
  const head = `<div class="xpanel xbanner" style="grid-column:1/-1"><div><b>${d.extra} extra cop${d.extra === 1 ? 'y' : 'ies'}</b> in ${d.groups.length} group${d.groups.length === 1 ? '' : 's'} · ${esc(d.wasted_h)} you could free</div>`
    + `<div class="xbtns"><button class="btn ghost" data-dsel="1">☑ Select the extra copies</button></div>`
    + `<p class="xhint">The oldest file in each group is the one kept. “Same video” groups are different files of one video (e.g. 720p and 1080p) - look before you delete.</p></div>`;
  return head + d.groups.map(g => `<div class="xpanel xgh" style="grid-column:1/-1"><span class="tgc ${g.reason === 'identical' ? 'tg-sites' : 'tg-dates'}">${g.reason === 'identical' ? '🟰 identical' : '🎬 same video'}</span><b>${g.items.length} copies</b><em>${esc(g.wasted_h)} extra</em></div>`
    + g.items.map(it => { const html = xTile(it, i); i++; return it._k === 0 ? html.replace('<div class="xth">', '<div class="xth"><i class="xkeep">KEEP</i>') : html; }).join('')).join('');
}
const xBar = (rows, max, cls) => rows.map(r => `<div class="sbar ${cls || ''}"${r.go !== undefined ? ` data-sgo="${esc(r.go)}" role="button" tabindex="0"` : ''}><span>${esc(r.label || r.name)}</span><i><u style="width:${max ? Math.max(2, r.size / max * 100) : 0}%;${r.c ? '--c:' + r.c : ''}"></u></i><em>${esc(r.size_h)}</em></div>`).join('');
function xStorageHtml() {
  const s = X.storage; if (!s) return '';
  const SITE_C = {youtube:'#ff4d5e', tiktok:'#25f4ee', instagram:'#f472b6', snapchat:'#fffc00', general:'#a78bfa'};
  const KIND = {video:['🎬 Videos','#8b5cf6'], photo:['🖼️ Photos','#22d3ee'], audio:['🎵 Audio','#34d399'], other:['📄 Other','#9aa0c3']};
  const dk = s.disk, other = Math.max(0, dk.used - s.total), pc = v => (v / dk.total * 100).toFixed(1);
  const maxOf = a => Math.max(0, ...a.map(r => r.size));
  const sites = s.sites.map(r => Object.assign({}, r, {label:r.name, c:SITE_C[r.name], go:r.name}));
  const users = s.users.map(r => Object.assign({}, r, {label:r.name.split('/')[1] + '  ·  ' + r.name.split('/')[0], go:r.name}));
  const kinds = s.kinds.map(r => Object.assign({}, r, {label:(KIND[r.name] || [r.name])[0], c:(KIND[r.name] || [])[1]}));
  const mmax = maxOf(s.months);
  const months = s.months.map(m => `<div class="mcol" title="${esc(m.name)}: ${esc(m.size_h)}"><i style="height:${mmax ? Math.max(3, m.size / mmax * 100) : 0}%"></i><span>${esc(m.name.slice(5))}</span></div>`).join('');
  const clean = (k, ico, t, desc) => { const c = s.cleanup[k]; return `<div class="cl"><b>${ico} ${t}</b><small>${desc}</small><div class="clrow"><em>${c.count} file${c.count === 1 ? '' : 's'} · ${esc(c.size_h)}</em><button class="btn ghost" data-clean="${k}" data-n="${c.count}"${c.count ? '' : ' disabled'}>Clean up</button></div></div>`; };
  return `<div class="xpanel stg" style="grid-column:1/-1">
    <section class="scard wide"><h4>💾 Disk</h4>
      <div class="disk"><i class="d1" style="width:${pc(s.total)}%" title="Media Hub"></i><i class="d2" style="width:${pc(other)}%" title="Other files"></i></div>
      <div class="dleg"><span><i class="d1"></i>Media Hub <b>${esc(s.total_h)}</b></span><span><i class="d2"></i>Other files <b>${esc(xHuman(other))}</b></span><span><i></i>Free <b>${esc(dk.free_h)}</b></span><span class="xhint">${s.files} files · disk ${esc(dk.total_h)}</span></div></section>
    <section class="scard"><h4>🌐 By site</h4>${xBar(sites, maxOf(sites)) || '<p class="xhint">Nothing yet.</p>'}</section>
    <section class="scard"><h4>🎞️ By type</h4>${xBar(kinds, maxOf(kinds)) || '<p class="xhint">Nothing yet.</p>'}</section>
    <section class="scard"><h4>👤 Biggest creators <small>tap to open</small></h4>${xBar(users, maxOf(users)) || '<p class="xhint">No creator folders yet.</p>'}</section>
    <section class="scard"><h4>📅 Added per month</h4><div class="months">${months || '<p class="xhint">Nothing yet.</p>'}</div></section>
    <section class="scard wide"><h4>🧹 Cleanup <small>deleting cannot be undone</small></h4><div class="cls">
      ${clean('dupes', '👯', 'Extra identical copies', 'Keeps the oldest copy of each file.')}
      ${clean('partials', '🧩', 'Unfinished downloads', 'Leftover .part files older than 3 hours.')}
      ${clean('sidecars', '📝', 'Info side files', 'yt-dlp .description and .info.json files.')}
    </div></section></div>`;
}
async function xClean(what, n) {
  const names = {dupes:'extra identical copies', partials:'unfinished download leftovers', sidecars:'info side files'};
  if (!confirm(`Delete ${n} ${names[what]}?\n\nThis cannot be undone.`)) return;
  const r = await xPost('/fs/cleanup', {what}); if (r.success) toast(`Deleted ${r.deleted} file${r.deleted === 1 ? '' : 's'} · freed ${r.freed_h}`); xAfter();
}

(function initExtras() {
  safeBind('closeQueueBtn', 'click', closeQueue); safeBind('qPauseAll', 'click', () => qAll('pause')); safeBind('qResumeAll', 'click', () => qAll('resume'));
  const qm = $('qmodal'); if (qm) qm.addEventListener('click', e => { if (e.target === qm) closeQueue(); const b = e.target.closest('[data-qa]'); if (b) qAct(b.dataset.id, b.dataset.qa); });
  $('strip').addEventListener('click', e => { if (e.target.closest('[data-openq]')) { e.stopPropagation(); openQueue(); } });
  safeBind('pillRun', 'click', openQueue);
  safeBind('digestSel', 'change', saveDigest); safeBind('digestHour', 'change', saveDigest);
  safeBind('digestNowBtn', 'click', async () => { const r = await post('/digest/send', {}); toast(r.message || 'Done'); loadSettings(); });
  document.querySelectorAll('[data-look]').forEach(b => b.addEventListener('click', () => applyLook(b.dataset.look)));
  applyUi();
  $('xgrid').addEventListener('click', e => {
    const c = e.target.closest('[data-clean]'), g = e.target.closest('[data-sgo]'), s = e.target.closest('[data-dsel]');
    if (c) { e.stopPropagation(); xClean(c.dataset.clean, c.dataset.n); }
    else if (g) { e.stopPropagation(); xGo(g.dataset.sgo); }
    else if (s) { e.stopPropagation(); X.selMode = true; X.sel = new Set(X.rows.filter(r => r._k > 0).map(r => r.path)); xRender(); }
  }, true);
  $('xgrid').addEventListener('keydown', e => { const g = e.target.closest('[data-sgo]'); if (g && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); xGo(g.dataset.sgo); } });
})();


/* ================= TikTok LIVE ================= */
function liveWint(on) {
  const w = $('wint'); if (!w) return;
  if (AppState.wintHtml === undefined) AppState.wintHtml = w.innerHTML;
  if (on && !w.dataset.live) { w.innerHTML = [2, 5, 10, 15, 30].map(m => `<option value="${m}"${m === 5 ? ' selected' : ''}>every ${m} min</option>`).join(''); w.dataset.live = '1'; }
  else if (!on && w.dataset.live) { w.innerHTML = AppState.wintHtml; delete w.dataset.live; }
}
function liveCard(r) {
  const L = r.live; setHint(''); AppState.liveOn = !!L.is_live;
  $('siteBadge').textContent = L.is_live ? '🔴 LIVE' : 'TikTok LIVE';
  if (r.thumbnail) { $('preview').classList.remove('no-thumb'); $('thumb').className = 'thumb'; $('thumb').style.display = ''; setThumb(r.thumbnail); }
  else { $('preview').classList.add('no-thumb'); $('thumb').style.display = 'none'; }
  $('ptitle').textContent = r.title; $('plnote').style.display = 'none';
  const chips = [L.is_live ? ['🔴 Live now', 'ok'] : '⚫ Not live', '👤 @' + L.user, `📁 tiktok/${L.user}/lives/`];
  if (L.recording) chips.push(['⏺ Already recording', 'ok']);
  $('chips').innerHTML = chips.map(chipHtml).join('');
  $('watchTxt').textContent = `🔴 Record automatically whenever @${L.user} goes live`; $('watchrow').classList.add('show');
  refreshOpts();
}
(function initLive() {
  safeBind('liveHelper', 'change', async () => { await post('/settings', {live_helper: $('liveHelper').checked}); toast($('liveHelper').checked ? 'Helper on - used only when TikTok blocks the check' : 'Helper off - only TikTok itself is asked'); });
  safeBind('liveQuality', 'change', async () => { const r = await post('/settings', {live_quality: $('liveQuality').value}); toast(r.success ? 'New recordings: ' + $('liveQuality').selectedOptions[0].text : 'Could not save'); });
  safeBind('liveMax', 'change', async () => { const r = await post('/settings', {live_max_hours: $('liveMax').value}); toast(r.success ? `Recordings stop after ${r.live_max_hours} h at most` : (r.message || 'Could not save')); });
})();


/* ================= Live page ================= */
var LV = {timer:null, tick:null, skew:0, html:{}, saved:null, filter:''};   // var: setTab() may run before this line
const lvUser = v => { v = String(v || '').trim(); const m = v.match(LIVE_RE); if (m) return m[1]; v = v.replace(/^@/, ''); return /^[A-Za-z0-9._-]{2,24}$/.test(v) ? v : ''; };
const lvLink = u => `https://www.tiktok.com/@${encodeURIComponent(u)}/live`;
const lvAgo = ts => ts ? ago(ts) : 'not yet';
const lvIn = ts => { if (!ts) return 'soon'; const m = Math.round((ts - Date.now() / 1000 + LV.skew) / 60); return m <= 0 ? 'any moment' : `in ${m} min`; };
const lvSet = (id, html) => { if (LV.html[id] !== html) { LV.html[id] = html; $(id).innerHTML = html; } };
const lvAv = (u, live) => `<span class="lvav${live ? ' on' : ''}" style="--h:${[...u].reduce((a, c) => a + c.charCodeAt(0), 0) % 360}"><span>${esc(u[0].toUpperCase())}</span>${live ? '<i>LIVE</i>' : ''}</span>`;

function lvOpen() { if (!LV) return; lvLoad(); lvSaved(); clearInterval(LV.timer); LV.timer = setInterval(lvLoad, 3000); clearInterval(LV.tick); LV.tick = setInterval(lvTick, 1000); }
function lvClose() { if (!LV) return; clearInterval(LV.timer); clearInterval(LV.tick); LV.timer = LV.tick = null; }
async function lvLoad() {
  const d = await api('/live/overview'); if (!d.success) return;
  LV.skew = Date.now() / 1000 - d.now; lvBadge(d.recording.length);
  if (document.body.dataset.tab === 'live') lvRender(d);
}
function lvBadge(n) { ['rnLive', 'tbLive'].forEach(id => { if ($(id)) $(id).textContent = n || ''; }); }
setInterval(() => { if (!document.hidden && document.body.dataset.tab !== 'live') lvLoad(); }, 15000); setTimeout(lvLoad, 1500);

function lvRender(d) {
  const recU = new Set(d.recording.map(r => r.user.toLowerCase()));
  const liveN = new Set([...recU, ...d.accounts.filter(a => /^🔴/.test(a.last_status)).map(a => a.user.toLowerCase())]).size;
  lvSet('lvStats', [[d.accounts.length, 'watched'], [liveN, 'live now'], [d.recording.length, 'recording'], LV.saved ? [LV.saved.count, 'saved'] : null]
    .filter(Boolean).map(([n, l]) => `<span class="lvstat${l === 'recording' && n ? ' hot' : ''}"><b>${n}</b>${l}</span>`).join('')
    + `<span class="lvstat lvqpill" title="Change in Settings → Downloads">✦ ${({best:'Original quality', hd:'HD quality', sd:'SD quality'})[d.quality] || 'Original quality'}</span>`);
  $('lvRecCount').textContent = d.recording.length || ''; $('lvAccCount').textContent = d.accounts.length || '';
  lvSet('lvRec', d.recording.length ? d.recording.map((r, i) => `<div class="lvcard lvrecc" style="--i:${i}">
      ${r.cover ? `<div class="lvcover" style="background-image:url('${esc(r.cover).replace(/'/g, '%27')}')"></div>` : ''}
      <div class="lvtop">${lvAv(r.user, true)}<div class="lvwho"><b>@${esc(r.user)}</b><small>${esc(r.title || 'Live on TikTok')}</small></div><span class="lveq" aria-hidden="true"><i></i><i></i><i></i><i></i></span><b class="recdot">● REC</b></div>
      ${r.quality ? `<div class="lvq">✦ ${esc(r.quality)}</div>` : ''}
      <div class="lvnums">
        <div><em>Time</em><b class="lvtime" data-start="${r.started || 0}">${fmtDur(r.secs || 0)}</b></div>
        <div><em>Size</em><b>${esc(r.size_h)}</b></div>
        <div><em>Speed</em><b>${esc(r.speed || '–')}</b></div>
        <div><em>Viewers</em><b>${r.viewers == null ? '–' : new Intl.NumberFormat('en', {notation:'compact'}).format(r.viewers)}</b><small>${r.peak ? 'peak ' + new Intl.NumberFormat('en', {notation:'compact'}).format(r.peak) : ''}</small></div>
      </div>
      ${lvChatHtml(r, d.chat_on)}
      <div class="lvbtns"><button class="btn ghost lvmark" data-lvmark="${esc(r.id)}" title="Mark this moment - it becomes a chapter in the video">⭐ Mark moment${r.marks ? ` <em>${r.marks}</em>` : ''}</button><a class="btn ghost" href="${lvLink(r.user)}" target="_blank" rel="noopener noreferrer">↗ TikTok</a><button class="btn ghost lvdanger" data-lvstop="${esc(r.id)}">⏹ Stop &amp; save</button></div></div>`).join('')
    : '<div class="lvempty">Nothing is recording. When a watched account goes live, it shows up here by itself.</div>');
  const ivs = d.intervals || [2, 5, 10, 15, 30];
  lvSet('lvAcc', d.accounts.length ? d.accounts.map(a => {
    const rec = recU.has(a.user.toLowerCase()), warn = /^⚠/.test(a.last_status);
    const st = rec ? ['rec', '● Recording'] : !a.enabled ? ['off', 'Paused'] : a.busy ? ['chk', 'Checking…'] : warn ? ['warn', '⚠ Check failed'] : /^🔴/.test(a.last_status) ? ['rec', 'Live'] : ['idle', 'Not live'];
    return `<div class="lvcard lvaccc s-${st[0]}" style="--i:${d.accounts.indexOf(a)}">
      <div class="lvtop">${lvAv(a.user, st[0] === 'rec')}<div class="lvwho"><b>@${esc(a.user)}</b><small>${a.total} recorded</small></div><span class="lvchip c-${st[0]}">${st[1]}</span></div>
      <div class="lvmeta">${warn ? esc(a.last_status) + '<br>' : ''}Checked ${lvAgo(a.last_check)}${a.enabled ? ` · next ${lvIn(a.next_check)}` : ''}${a.habit ? `<br><span class="lvhabit">🕘 ${esc(a.habit)}</span>` : a.lives_seen ? `<br><span class="lvhabit">🕘 Learning their schedule (${a.lives_seen}/3 lives)</span>` : ''}</div>
      <div class="lvctl">
        <select class="wsel" data-lvint="${esc(a.id)}" aria-label="Check how often">${ivs.map(m => `<option value="${m}"${m === a.interval ? ' selected' : ''}>every ${ivLabel(m)}</option>`).join('')}</select>
        <button class="mini" data-lvact="check" data-id="${esc(a.id)}">Check now</button>
        ${rec ? '' : `<button class="mini" data-lvrec="${esc(a.user)}">🔴 Record</button>`}
        <button class="mini" data-lvact="toggle" data-id="${esc(a.id)}">${a.enabled ? 'Pause' : 'Resume'}</button>
        <a class="mini" href="${lvLink(a.user)}" target="_blank" rel="noopener noreferrer" aria-label="Open @${esc(a.user)} on TikTok">↗</a>
        <button class="mini del" data-lvdel="${esc(a.id)}" aria-label="Stop watching @${esc(a.user)}">✕</button>
      </div></div>`;
  }).join('') : '<div class="lvempty">No accounts yet. Type a username above and press <b>Watch</b>.</div>');
  document.querySelectorAll('.lvfeed').forEach(f => { if (!f.matches(':hover')) f.scrollTop = f.scrollHeight; });
  if (!d.ready) setHintLv('⚠ curl_cffi is missing on the server, so TikTok blocks checks more often. Rebuild the Docker image.');
}
function setHintLv(t, err) { $('lvHint').textContent = t || ''; $('lvHint').classList.toggle('err', !!err); }
function lvTick() {
  document.querySelectorAll('.lvtime[data-start]').forEach(el => { const s = +el.dataset.start; if (s) el.textContent = fmtDur(Math.max(0, Date.now() / 1000 - LV.skew - s)); });
}
async function lvSaved() {
  const r = await api('/fs/lives');
  if (!r.success) {
    LV.saved = null; $('lvSavedSum').textContent = '';
    LV.html.lvSaved = ''; $('lvSaved').innerHTML = r.locked ? '<div class="lvempty lvlock">🔒 Saved lives are behind the file password. <button class="btn ghost" data-lvunlock="1">Unlock</button></div>' : `<div class="lvempty">${esc(r.message || 'Could not load the saved lives.')}</div>`;
    return;
  }
  LV.saved = r; $('lvSavedSum').textContent = r.count ? `${r.count} · ${r.total_h}${r.hours ? ' · ' + r.hours + ' h' : ''}` : '';
  lvSavedRender();
}
function lvSavedRender() {
  const r = LV.saved; if (!r) return;
  const users = [...new Set(r.items.map(x => x.user))];
  const items = r.items.filter(x => !LV.filter || x.user === LV.filter);
  const chips = users.length > 1 ? `<div class="lvfilter"><button data-lvf="" class="${LV.filter ? '' : 'on'}">All</button>${users.map(u => `<button data-lvf="${esc(u)}" class="${LV.filter === u ? 'on' : ''}">@${esc(u)}</button>`).join('')}</div>` : '';
  LV.html.lvSaved = '';
  $('lvSaved').innerHTML = r.items.length ? chips + `<div class="lvlist">` + items.map(x => `<div class="lvrow">
      <span class="lvthumb"><img loading="lazy" alt="" src="${xThumb(x)}" onerror="this.remove()"><span>▶</span></span><div class="lvwho"><b>@${esc(x.user)}</b><small>${esc(new Date(x.modified * 1000).toLocaleString(undefined, {dateStyle:'medium', timeStyle:'short'}))}</small></div>
      <span class="lvdur">${x.duration ? fmtDur(x.duration) : '–'}</span><span class="lvsize">${esc(x.size_h)}</span>
      <div class="lvctl"><a class="mini" href="${xFileUrl(x)}" target="_blank" rel="noopener">▶ Play</a>${x.transcript ? `<a class="mini" href="${xFileUrl({path:x.transcript})}" target="_blank" rel="noopener" title="Chat transcript">💬</a>` : ''}<a class="mini" href="${xFileUrl(x)}?dl=1" download>⬇</a><button class="mini" data-lvfolder="${esc(x.path.split('/').slice(0, -1).join('/'))}" aria-label="Show in Files">📂</button></div></div>`).join('') + '</div>'
    : '<div class="lvempty">No saved lives yet.</div>';
}
async function lvAddWatch() {
  const u = lvUser($('lvUser').value); if (!u) return setHintLv('Type a TikTok username, like @name, or paste a tiktok.com/@name/live link.', true);
  const r = await post('/watches', {kind:'tiktok_live', target:u, interval:+$('lvInt').value});
  setHintLv(r.success ? `Watching @${u} ✓ - it records by itself when they go live.` : (r.message || 'Could not add'), !r.success);
  if (r.success) { $('lvUser').value = ''; await api(`/watches/${encodeURIComponent(r.watch.id)}/check`, {method:'POST'}); }
  lvLoad(); loadWatches();
}
async function lvRecord(u) {
  u = u || lvUser($('lvUser').value); if (!u) return setHintLv('Type a TikTok username first.', true);
  setHintLv(`Checking if @${u} is live…`);
  const r = await post('/download-action', {action_type:'url', target:lvLink(u)});
  if (!r.success) return setHintLv(r.message || 'Could not start.', true);
  setTracked([...tracked(), r.job_id]); watch();
  setTimeout(async () => {
    const j = (await api('/jobs?ids=' + r.job_id)).jobs || [];
    const job = j[0]; if (job && job.status === 'error') setHintLv(job.message, true); else setHintLv(`🔴 Recording @${u} ✓`);
    lvLoad();
  }, 3500);
}

(function initLivePage() {
  const pg = $('livePage'); if (!pg) return;
  safeBind('lvWatchBtn', 'click', lvAddWatch); safeBind('lvRecBtn', 'click', () => lvRecord());
  $('lvUser').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); lvAddWatch(); } });
  pg.addEventListener('change', e => { const s = e.target.closest('[data-lvint]'); if (s) { setWInt(s.dataset.lvint, s.value).then(lvLoad); } });
  pg.addEventListener('click', async e => {
    const t = e.target.closest('[data-lvmark],[data-lvstop],[data-lvact],[data-lvdel],[data-lvrec],[data-lvunlock],[data-lvfolder],[data-lvf]'); if (!t) return;
    if (t.dataset.lvmark) { const r = await post(`/live/${encodeURIComponent(t.dataset.lvmark)}/mark`, {}); if (r.success) { toast(`⭐ Moment marked at ${fmtDur(r.t)}`); t.classList.remove('pop'); void t.offsetWidth; t.classList.add('pop'); } else toast(r.message || 'Could not mark'); lvLoad(); return; }
    if (t.dataset.lvstop) { t.disabled = true; t.textContent = 'Saving…'; await api('/cancel/' + encodeURIComponent(t.dataset.lvstop), {method:'POST'}); toast('Stopping - the recording is being saved'); setTimeout(() => { lvLoad(); lvSaved(); }, 4000); }
    else if (t.dataset.lvact) { await wact(t.dataset.id, t.dataset.lvact); if (t.dataset.lvact === 'check') toast('Checking now…'); lvLoad(); setTimeout(lvLoad, 4000); }
    else if (t.dataset.lvdel) { await wdel(t.dataset.lvdel); lvLoad(); }
    else if (t.dataset.lvrec) lvRecord(t.dataset.lvrec);
    else if (t.dataset.lvunlock) { if (await xUnlock()) lvSaved(); }
    else if (t.dataset.lvfolder) { await openFiles(); xGo(t.dataset.lvfolder); }
    else if (t.dataset.lvf !== undefined) { LV.filter = t.dataset.lvf; lvSavedRender(); }
  });
})();


/* ================= Live: chat feed + options ================= */
const lvNum = n => new Intl.NumberFormat('en', {notation:'compact'}).format(n || 0);
function lvChatHtml(r, on) {
  if (!on) return '';
  const st = r.chat_state && r.chat_state !== 'connected' ? `<div class="lvchatstate">${esc(r.chat_state)}</div>` : '';
  const rows = (r.chat || []).map(e => e.k === 'g'
    ? `<div class="cg"><b>🎁 ${esc(e.u)}</b> ${esc(e.x)}${e.d ? ` <em>💎 ${lvNum(e.d)}</em>` : ''}</div>`
    : `<div class="cc"><b style="--h:${[...String(e.u)].reduce((a, c) => a + c.charCodeAt(0), 0) % 360}">${esc(e.u)}</b> ${esc(e.x)}</div>`).join('');
  return `<div class="lvchat">
    <div class="lvchatstats"><span>💬 ${lvNum(r.chat_n)}</span><span>🎁 ${lvNum(r.gifts_n)}${r.diamonds ? ` · 💎 ${lvNum(r.diamonds)}` : ''}</span><span>❤ ${lvNum(r.likes)}</span><span>👋 ${lvNum(r.joins)}</span></div>
    ${st}<div class="lvfeed" data-feed="${esc(r.id)}">${rows || '<div class="lvchatwait">Waiting for the first message…</div>'}</div></div>`;
}
async function lvOptsLoad() {
  const d = await api('/settings'); if (!d.success) return;
  const set = (id, v) => { if ($(id)) { if ($(id).type === 'checkbox') $(id).checked = !!v; else $(id).value = String(v); } };
  set('loQuality', d.live_quality || 'best'); set('loMax', d.live_max_minutes || 360); set('loKeep', d.live_min_keep || 0); set('loFree', d.live_min_free_gb || 2);
  set('loChat', d.live_chat); set('loChatSubs', d.live_chat_subs !== false); set('loNotify', d.live_notify !== false); set('loHelper', d.live_helper !== false);
  if ($('loMax') && !$('loMax').value) { $('loMax').insertAdjacentHTML('beforeend', `<option value="${d.live_max_minutes}">${d.live_max_minutes} minutes</option>`); $('loMax').value = String(d.live_max_minutes); }
  $('loChatKey').value = ''; $('loChatKey').placeholder = d.live_chat_key_set ? '•••••••• saved - type to replace' : 'not set';
  $('loChatReady').textContent = d.live_chat_ready === false ? '⚠ The TikTokLive package is not installed on the server - rebuild the Docker image to use chat.' : '';
  $('loReady').textContent = d.live_ready === false ? '⚠ curl_cffi is not installed on the server - TikTok blocks checks more often. Rebuild the Docker image.' : '';
}
async function lvOpt(key, val, msg) {
  const r = await post('/settings', {[key]: val});
  toast(r.success ? msg + ' ✓' : (r.message || 'Could not save')); if (r.success) lvLoad();
}
(function initLiveOpts() {
  const b = $('lvOptBtn'); if (!b) return;
  b.addEventListener('click', () => { const o = $('lvOpts'), open = o.hidden; o.hidden = !open; b.setAttribute('aria-expanded', String(open)); b.classList.toggle('on', open); if (open) lvOptsLoad(); });
  const sel = (id, key, msg) => safeBind(id, 'change', () => lvOpt(key, $(id).value, msg($(id))));
  sel('loQuality', 'live_quality', el => 'New recordings: ' + el.selectedOptions[0].text.split(' - ')[0]);
  sel('loMax', 'live_max_minutes', el => 'Recordings stop after ' + el.selectedOptions[0].text);
  sel('loKeep', 'live_min_keep', el => +el.value ? 'Lives shorter than ' + el.selectedOptions[0].text + ' are not kept' : 'Every live is kept');
  sel('loFree', 'live_min_free_gb', el => 'Stops when less than ' + el.value + ' GB is free');
  const tog = (id, key, on, off) => safeBind(id, 'change', () => lvOpt(key, $(id).checked, $(id).checked ? on : off));
  tog('loChat', 'live_chat', 'Chat is recorded from the next recording', 'Chat off');
  tog('loChatSubs', 'live_chat_subs', 'Chat is added as subtitles', 'No chat subtitles');
  tog('loNotify', 'live_notify', 'Live alerts on', 'Live alerts off');
  tog('loHelper', 'live_helper', 'Helper on', 'Helper off - only TikTok itself is asked');
  safeBind('loChatKeySave', 'click', async () => { await lvOpt('live_chat_key', $('loChatKey').value.trim(), $('loChatKey').value.trim() ? 'API key saved' : 'API key removed'); lvOptsLoad(); });
})();


/* ================= Live: TikTok login ================= */
function ttLoginPaint(st) {
  const el = $('ttLoginStatus'); if (!el || !st) return;
  let t, cls;
  if (!st.saved) { t = '⚪ Not logged in - recording as a logged-out viewer'; cls = ''; }
  else if (!st.has_session) { t = '⚠ Saved, but there is no login in it (no “sessionid”). Export again while logged in.'; cls = 'warn'; }
  else if (st.session_expired) { t = '⚠ The login has expired - export a fresh cookies.txt.'; cls = 'warn'; }
  else if (st.user) { t = `🟢 Logged in as @${st.user}`; cls = 'ok'; }
  else { t = '🟡 Login saved - TikTok did not confirm the account right now (press Check later)'; cls = 'mid'; }
  if (st.expires && !st.session_expired && st.has_session) t += ` · valid until ${new Date(st.expires * 1000).toLocaleDateString()}`;
  el.textContent = t; el.className = 'lvloginst ' + cls;
  $('ttRemove').disabled = !st.saved;
}
async function ttLoginLoad(check) { const r = await api('/tiktok-cookies' + (check ? '?check=1' : '')); if (r.success) ttLoginPaint(r); }
async function ttLoginSave() {
  const f = $('ttFile').files[0]; let r;
  if (f) { const fd = new FormData(); fd.append('file', f); r = await api('/tiktok-cookies', {method:'POST', body:fd}); }
  else r = await post('/tiktok-cookies', {text: $('ttText').value});
  toast(r.message || (r.success ? 'Saved' : 'Could not save'));
  if (r.success) { $('ttText').value = ''; $('ttFile').value = ''; ttLoginPaint(r); lvLoad(); }
}
(function initTtLogin() {
  if (!$('ttSave')) return;
  safeBind('ttSave', 'click', ttLoginSave);
  safeBind('ttCheck', 'click', async () => { $('ttLoginStatus').textContent = 'Checking with TikTok…'; await ttLoginLoad(true); });
  safeBind('ttRemove', 'click', async () => { if (!confirm('Remove the TikTok login from Media Hub?')) return; const r = await api('/tiktok-cookies', {method:'DELETE'}); toast(r.message || 'Removed'); ttLoginPaint(r); lvLoad(); });
  $('lvOptBtn').addEventListener('click', () => { if (!$('lvOpts').hidden) ttLoginLoad(true); });
})();


/* ================= Save to: default folder or one of your own ================= */
const FOLDER_KEY = 'hub_folder';
function saveFolder() { const s = $('fdest'); return s && s.value !== '+new' ? s.value : ''; }
const cleanFolder = v => String(v || '').replace(/[\x00-\x1f<>:"/\\|?*]/g, '').replace(/\s+/g, ' ').trim().replace(/^\.+|\.+$/g, '').slice(0, 40).trim();
function folderOptions(names, pick) {
  const s = $('fdest'); if (!s) return;
  const all = [...new Set(names.filter(Boolean))].sort((a, b) => a.localeCompare(b));
  s.innerHTML = '<option value="">Save to: default folder (site / creator)</option>'
    + all.map(n => `<option value="${esc(n)}">Save to: ${esc(n)}</option>`).join('')
    + '<option value="+new">＋ New folder…</option>';
  s.value = all.includes(pick) ? pick : '';
  s.closest('.folderrow').classList.toggle('on', !!s.value);
}
async function loadFolders() {
  let pick = ''; try { pick = localStorage.getItem(FOLDER_KEY) || ''; } catch {}
  const r = await api('/folders');
  folderOptions([...(r.success ? r.folders : []), pick], pick);
}
(function initFolders() {
  const s = $('fdest'); if (!s) return;
  let prev = '';
  s.addEventListener('focus', () => { prev = s.value; });
  s.addEventListener('change', () => {
    let v = s.value;
    if (v === '+new') {
      v = cleanFolder(prompt('Name of the new folder (for example: Good)') || '');
      if (!v) { s.value = prev; return; }
      folderOptions([...[...s.options].map(o => o.value).filter(x => x && x !== '+new'), v], v);
      toast(`📂 New downloads go to “${v}”`);
    } else toast(v ? `📂 New downloads go to “${v}”` : 'New downloads go to the normal site / creator folder');
    try { localStorage.setItem(FOLDER_KEY, v); } catch {}
    s.closest('.folderrow').classList.toggle('on', !!v); prev = v;
    refreshOpts();
  });
  loadFolders();
})();


/* ================= Shortcut walkthrough: a little Shortcuts editor that builds itself ================= */
const SCG_STEPS = [
  {t:'Create a new Shortcut', h:'Open the <b>Shortcuts</b> app on your iPhone and tap <b>+</b> in the top-right corner.', k:'plus', show:0, lib:true},
  {t:'Give it a name', h:'Tap the name at the top and type <b>Save to <span class="scf">F</span></b>.', k:'name', show:0, type:true},
  {t:'Put it in the Share menu', h:'Tap <b>ⓘ</b> → turn on <b>Show in Share Sheet</b> → tap <b>Share Sheet Types</b> and keep only <b>URLs</b> and <b>Text</b>.', k:'info', show:1},
  {t:'Add “Get Contents of URL”', h:'Tap <b>Search for apps and actions</b>, type <b>Get Contents of URL</b> and tap it. Tap the blue <b>URL</b> and paste <b>your link</b> (📋 Copy it above).', k:'url', show:2},
  {t:'Send the link to Media Hub', h:'Tap <b>›</b> to show more: <b>Method → POST</b>, <b>Request Body → JSON</b>, then <b>Add new field → Text</b>: key <code>url</code>, value <b>Shortcut Input</b> (tap the value and pick it).', k:'more', show:2, more:true},
  {t:'Read the answer', h:'Add <b>Get Dictionary Value</b>. Make it say: Get <b>Value</b> for <code>message</code> in <b>Contents of URL</b>.', k:'dict', show:3, more:true},
  {t:'Show a notification', h:'Add <b>Show Notification</b> and put <b>Dictionary Value</b> in it. Then tap <b>Done</b>. That’s the whole Shortcut!', k:'done', show:4, more:true},
  {t:'Use it 🎉', h:'In TikTok, Instagram, YouTube…: <b>Share → More (···) → Save to <span class="scf">F</span></b>. The first time, tap <b>Always Allow</b>. The video lands in <i>folders/<span class="scf">F</span>/</i> on your server - nothing is saved on the phone.', k:'', show:4, more:true, banner:true},
];
const SCG = {i:0, typer:null, tap:null};

function scgInit() {
  const box = $('scg'); if (!box) return;
  const f = esc(box.dataset.folder || 'Good'), F = `<span class="scf">${f}</span>`;
  box.innerHTML = `
    <div class="scg-stage">
      <div class="scg-phone" aria-hidden="true">
        <div class="scg-island"></div>
        <div class="scg-banner"><i class="scg-appico">⬇</i><div><b>SHORTCUTS · now</b><span>⬇️ Downloading to 📂 ${F}</span></div></div>
        <div class="scg-lib">
          <div class="scg-libtop"><span>Shortcuts</span><b data-k="plus">+</b></div>
          <h4>All Shortcuts</h4>
          <div class="scg-tiles"><i style="--c:#ff9f0a">Timer</i><i style="--c:#30d158">Home</i><i style="--c:#bf5af2">Music</i></div>
        </div>
        <div class="scg-ed">
          <div class="scg-top"><span class="scg-chev">‹</span><span class="scg-name" data-k="name"><i class="scg-appico">⬇</i><b class="scg-typed">New Shortcut</b></span><span class="scg-tr"><b data-k="info">ⓘ</b><b data-k="done">Done</b></span></div>
          <div class="scg-body">
            <div class="scg-act" data-n="1"><i class="scg-ai" style="--c:#8e8e93">↗</i><div>Receive <em class="tk">URLs</em> and <em class="tk">Text</em> input from <em class="tk">Share Sheet</em></div></div>
            <div class="scg-act" data-n="2"><i class="scg-ai" style="--c:#0a84ff">🌐</i><div>Get contents of <em class="tk" data-k="url">…/phone/grab?…folder=${F}</em><span class="scg-mtog" data-k="more">›</span>
              <div class="scg-more"><div><span>Method</span><em class="tk">POST</em></div><div><span>Request Body</span><em class="tk">JSON</em></div><div><span>url</span><em class="tk var">⧉ Shortcut Input</em></div></div></div></div>
            <div class="scg-act" data-n="3" data-k="dict"><i class="scg-ai" style="--c:#ff9f0a">{ }</i><div>Get <em class="tk">Value</em> for <em class="tk">message</em> in <em class="tk var">🌐 Contents of URL</em></div></div>
            <div class="scg-act" data-n="4"><i class="scg-ai" style="--c:#ff453a">🔔</i><div>Show notification <em class="tk var">{ } Dictionary Value</em></div></div>
          </div>
          <div class="scg-search">🔍 Search for apps and actions</div>
        </div>
        <i class="scg-tap"></i>
      </div>
      <div class="scg-panel">
        <div class="scg-count"></div>
        <div class="scg-bar"><i></i></div>
        <div class="scg-text"><h5></h5><p></p></div>
        <div class="scg-nav"><button class="btn ghost" data-scg="back">‹ Back</button><button class="btn primary" data-scg="next">Next ›</button></div>
        <div class="scg-dots">${SCG_STEPS.map((_, n) => `<button data-scg="${n}" aria-label="Step ${n + 1}"></button>`).join('')}</div>
      </div>
    </div>`;
  box.querySelector('.scg-panel').addEventListener('click', e => {
    const b = e.target.closest('[data-scg]'); if (!b) return;
    const v = b.dataset.scg;
    scgGo(v === 'next' ? (SCG.i >= SCG_STEPS.length - 1 ? 0 : SCG.i + 1) : v === 'back' ? Math.max(0, SCG.i - 1) : +v);
  });
  let x0 = null; const ph = box.querySelector('.scg-phone');                     // swipe the phone left / right
  ph.addEventListener('pointerdown', e => { x0 = e.clientX; });
  ph.addEventListener('pointerup', e => { if (x0 === null) return; const dx = e.clientX - x0; x0 = null; if (Math.abs(dx) > 40) scgGo(Math.min(SCG_STEPS.length - 1, Math.max(0, SCG.i + (dx < 0 ? 1 : -1)))); });
  scgGo(0);
}

function scgGo(i) {
  const box = $('scg'); if (!box) return;
  const st = SCG_STEPS[i], ph = box.querySelector('.scg-phone'), folder = (document.querySelector('#phoneSetup .scf') || {}).textContent || box.dataset.folder || 'Good';
  SCG.i = i;
  ph.classList.toggle('lib', !!st.lib);
  ph.classList.toggle('more', !!st.more);
  ph.classList.toggle('banner', !!st.banner);
  box.querySelectorAll('.scg-act').forEach(a => a.classList.toggle('on', +a.dataset.n <= st.show));
  box.querySelectorAll('[data-k]').forEach(el => el.classList.toggle('focus', el.dataset.k === st.k));
  const typed = box.querySelector('.scg-typed'); clearInterval(SCG.typer);
  const goal = 'Save to ' + folder;
  if (st.type) {                                                              // type the name letter by letter
    let n = 0; typed.textContent = ''; typed.classList.add('caret');
    SCG.typer = setInterval(() => { typed.textContent = goal.slice(0, ++n); if (n >= goal.length) { clearInterval(SCG.typer); typed.classList.remove('caret'); } }, 70);
  } else { typed.textContent = i === 0 ? 'New Shortcut' : goal; typed.classList.remove('caret'); }
  const pane = box.querySelector('.scg-text');
  pane.classList.remove('in'); void pane.offsetWidth; pane.classList.add('in');
  pane.querySelector('h5').textContent = st.t;
  pane.querySelector('p').innerHTML = st.h.replace(/<span class="scf">F<\/span>/g, `<span class="scf">${esc(folder)}</span>`);
  box.querySelector('.scg-count').textContent = `Step ${i + 1} of ${SCG_STEPS.length}`;
  box.querySelector('.scg-bar i').style.width = ((i + 1) / SCG_STEPS.length * 100) + '%';
  box.querySelectorAll('.scg-dots button').forEach((d, n) => { d.classList.toggle('on', n === i); d.classList.toggle('past', n < i); });
  box.querySelector('[data-scg="back"]').disabled = i === 0;
  box.querySelector('[data-scg="next"]').textContent = i === SCG_STEPS.length - 1 ? '↺ Start over' : 'Next ›';
  clearTimeout(SCG.tap);                                                      // move the "finger" once blocks have slid in
  const tap = box.querySelector('.scg-tap'); tap.classList.remove('show');
  SCG.tap = setTimeout(() => {
    const el = st.k && box.querySelector(`[data-k="${st.k}"]`);
    if (!el || !el.offsetParent) return;
    const a = ph.getBoundingClientRect(), b = el.getBoundingClientRect();
    tap.style.left = (b.left - a.left + b.width / 2) + 'px'; tap.style.top = (b.top - a.top + b.height / 2) + 'px';
    tap.classList.remove('show'); void tap.offsetWidth; tap.classList.add('show');
  }, 420);
}
