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
  clipPick: null,
  clipBusy: false,
  clipState: 'unknown',
  clipSeen: sessionStorage.getItem('hub_clip_seen') || '',
  lastRun: null,
  viewing: null,
  results: [],
  resultOpen: false,
  wt: null,
  lastRunning: []
};

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
const UI_KEY = 'hub_ui', UI_DEF = {theme:'dark', accent:'auto', size:'m', density:'comfy', confetti:'1'};
const ACCENTS = {violet:'#8b5cf6', cyan:'#22d3ee', pink:'#f472b6', green:'#34d399', orange:'#fb923c', red:'#fb7185'};
let ui = Object.assign({}, UI_DEF);
try { Object.assign(ui, JSON.parse(localStorage.getItem(UI_KEY) || '{}')); } catch {}
const accentOr = c => ui.accent !== 'auto' && ACCENTS[ui.accent] ? ACCENTS[ui.accent] : c;

function applyUi() {
  const d = document.documentElement; d.dataset.theme = ui.theme; d.dataset.size = ui.size; d.dataset.density = ui.density;
  const light = ui.theme === 'light' || (ui.theme === 'auto' && window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches);
  $('themeMeta').content = light ? '#f1f4ff' : '#07080f';
  document.querySelectorAll('[data-ui]').forEach(b => { const [k, v] = b.dataset.ui.split(':'); b.classList.toggle('on', ui[k] === v); });
  $('confettiOn').checked = ui.confetti === '1';
  setColor(accentOr((BRANDS[document.body.dataset.site] || BRANDS.other).color));
}
function setUi(k, v) { ui[k] = v; try { localStorage.setItem(UI_KEY, JSON.stringify(ui)); } catch {} applyUi(); }
function setBrand(key) {
  const b = BRANDS[key] || BRANDS.other; document.body.dataset.site = key; setColor(accentOr(b.color));
  $('goIcon').innerHTML = b.icon; $('badge').textContent = key === 'other' ? 'Any link' : b.name;
  $('badge').classList.remove('pop'); void $('badge').offsetWidth; $('badge').classList.add('pop');
}

/* Initialization routines for dynamic DOM parts */
function initDomInjects() {
    $('accents').insertAdjacentHTML('beforeend', Object.entries(ACCENTS).map(([k, c]) => `<button class="sw" style="--c:${c}" data-ui="accent:${k}" data-setui-type="accent" data-setui-val="${k}" aria-label="Accent ${k}"></button>`).join(''));
    $('platforms').innerHTML = ['tiktok','youtube','instagram','snapchat','x','facebook','reddit','twitch','vimeo'].map((k, i) => `<span class="plat" style="--c:${BRANDS[k].color};--i:${i}">${esc(BRANDS[k].name)}</span>`).join('');
    
    // Attach dynamically created accent UI handlers
    document.querySelectorAll('[data-setui-type]').forEach(btn => {
        btn.addEventListener('click', (e) => setUi(e.currentTarget.dataset.setuiType, e.currentTarget.dataset.setuiVal));
    });
}

/* ---------- smart box: video links AND Snapchat ---------- */
const SNAP_RE = /(?:story\.snapchat\.com\/(?:s\/|@)|snapchat\.com\/(?:add\/|@|s\/))@?([A-Za-z0-9._-]{3,32})/i;
const KNOWN = /youtu|tiktok|instagram|twitter|x\.com|facebook|fb\.watch|reddit|twitch|snapchat|vimeo|dailymotion/i;
const USER_RE = /^[A-Za-z0-9._-]{3,32}$/;
const isUrl = u => /^https?:\/\/\S+\.\S+/i.test(u);
const isInsta = u => /^https?:\/\/([a-z0-9-]+\.)*instagram\.com(\/|$|\?)/i.test(u);

function classify(v) {
  if (!v) return null;
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
  if (v && !AppState.hadLink) setOpts({});
  AppState.hadLink = !!v;
  $('clearBtn').style.display = v ? '' : 'none'; $('pasteBtn').style.display = v ? 'none' : '';
  ['preview', 'snapbox', 'igbox'].forEach(id => $(id).classList.remove('show'));
  ['actions', 'watchrow'].forEach(id => $(id).classList.remove('show'));
  AppState.chanUrl = ''; setHint(''); AppState.mode = null; AppState.noPreview = false; $('platforms').style.display = v ? 'none' : '';
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

function openSettings() { $('smodal').classList.add('open'); loadIg(); }
function setTab(t) {
  document.body.dataset.tab = t; $('tabbar').style.setProperty('--ti', t === 'watch' ? 1 : 0);
  document.querySelectorAll('.tabbar [data-tab]').forEach(b => b.classList.toggle('on', b.dataset.tab === t));
  if (window.scrollTo) window.scrollTo({top:0, behavior:'smooth'});
}
function closeKeys() { $('kmodal').classList.remove('open'); }
function focusUrl() { setTab('home'); $('url').focus(); if ($('url').select) $('url').select(); }
function closeSettings() { $('smodal').classList.remove('open'); }
function openIgSettings() { openSettings(); setTimeout(() => $('igSection').scrollIntoView({behavior:'smooth', block:'start'}), 120); }

function igCard(r, d) {
  AppState.mode = 'ig'; $('preview').classList.remove('show'); $('watchrow').classList.remove('show'); setHint(''); setBrand('instagram');
  const kinds = {post:'📸 Post (photos / carousel)', stories:'📖 Stories', highlight:'⭐ Highlight', profile:'👤 Whole profile'}, ig = r.ig || {};
  $('igtitle').textContent = r.title;
  $('igchips').innerHTML = [kinds[ig.kind] || 'Instagram', '📁 instagram/', ig.gallery_dl ? ['🧰 gallery-dl ✔', 'ok'] : ['🧰 gallery-dl missing ✖', 'bad'], loginChip(ig.login)].map(chipHtml).join('');
  const bad = [];
  if (!ig.gallery_dl) bad.push('gallery-dl is not installed on the server (see the 🔧 Maintenance button).');
  if (!ig.login || !ig.login.saved || ig.login.session_expired || !ig.login.has_session) bad.push('Instagram needs your login: open Settings → Instagram login.');
  $('ignote').innerHTML = (bad.length ? esc(bad.join(' ')) + ' <button class="linkbtn" style="padding:0" id="ignoteSettingsBtn">Open Settings</button><br>' : '')
    + (ig.kind === 'profile' ? esc(`Takes the first ${ig.cap || 30} items. For a different amount, open Customize → More options → Items and type a range such as 1-60.`) : '');
  
  if ($('ignoteSettingsBtn')) $('ignoteSettingsBtn').addEventListener('click', openIgSettings);

  $('igbox').classList.add('show'); $('actions').classList.add('show'); refreshOpts();
}

function videoPreview(url) {
  AppState.mode = 'video'; AppState.videoUrl = url;
  const d = detect(url); setBrand(d.key); $('actions').classList.add('show');
  AppState.timer = setTimeout(async () => {
    const mine = ++AppState.seq;
    setHint(`Reading ${d.name} link…`); $('preview').classList.add('show');
    $('thumb').className = 'thumb loading'; setThumb('');
    $('ptitle').textContent = 'Loading details…'; $('chips').innerHTML = ''; $('dur').style.display = 'none';
    $('siteBadge').textContent = d.name; $('siteBadge').style.background = d.color;
    const r = await post('/preview', {url});
    if (mine !== AppState.seq) return;
    if (!r.success) { $('preview').classList.remove('show'); AppState.noPreview = true; refreshOpts(); return setHint((r.error || 'No preview for this link.') + ' You can still try the download.', true); }
    if (r.gallery) return igCard(r, d);
    setHint(''); $('siteBadge').textContent = r.site || d.name;
    AppState.chanUrl = r.channel_url || ''; AppState.chanName = r.uploader || '';
    if (AppState.chanUrl) { $('watchTxt').textContent = '👁 Auto-download new posts from ' + (AppState.chanName || 'this creator'); $('watchrow').classList.add('show'); }
    $('thumb').className = 'thumb';
    if (r.thumbnail) setThumb(r.thumbnail);
    paintPreview(getOpts());
    $('ptitle').textContent = r.title;
    if (r.duration) { $('dur').textContent = fmtDur(r.duration); $('dur').style.display = ''; }
    const chips = [];
    if (r.uploader) chips.push('👤 ' + r.uploader);
    if (r.duration) chips.push(`⏱ ${r.duration}s`);
    if (r.views != null) chips.push('👁 ' + new Intl.NumberFormat('en', {notation:'compact'}).format(r.views));
    if (r.upload_date && r.upload_date.length === 8) chips.push('📅 ' + new Date(`${r.upload_date.slice(0,4)}-${r.upload_date.slice(4,6)}-${r.upload_date.slice(6)}`).toLocaleDateString());
    chips.push('📁 ' + r.category + '/');
    if (r.ig) { chips.push(loginChip(r.ig.login)); chips.push('👁 Watching Instagram: not yet'); }
    $('chips').innerHTML = chips.map(chipHtml).join('');
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
function hideClip() { $('clipbar').classList.remove('show'); AppState.clipPick = null; }
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
const OPT_DEFAULTS = {kind:'video', quality:'best', container:'mp4', compat:false, audio_fmt:'mp3', abr:'192', subs:false, autosubs:false, sub_langs:'en',
  embed_subs:false, thumb:false, embed_thumb:false, meta:false, sponsor:false, desc:false, playlist:'single', items:'', clip_start:'', clip_end:'',
  date_after:'', date_before:'', min_dur:'', max_dur:'', min_views:'', max_mb:'', title_has:'', title_not:'',
  force:false, fx_look:'none', fx_blur:'none', fx_sharpen:false, fx_mirror:'none', fx_rotate:'0', fx_speed:'1', fx_crop:'none', fx_scale:'none', fx_mute:false, fx_keep:true};
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
const isCustom = o => Object.keys(OPT_DEFAULTS).some(k => String(o[k]) !== String(OPT_DEFAULTS[k]));

function updateGo(o, lk) {
  if (!AppState.mode) return;
  let label, sub;
  if (AppState.mode === 'snap') { label = 'Download all stories'; sub = 'Public stories · saved in snapchat/'; }
  else if (AppState.mode === 'ig') { label = 'Download from Instagram'; sub = 'Original quality · saved in instagram/'; }
  else {
    label = o.kind === 'audio' ? 'Download ' + o.audio_fmt.toUpperCase() : o.kind === 'image' ? 'Save the photo' : o.kind === 'gif' ? 'Make a GIF' : o.kind === 'sticker' ? 'Make a sticker' : 'Download';
    sub = o.kind === 'audio' ? `Audio only${['flac','wav'].includes(o.audio_fmt) ? '' : ' · ' + o.abr + ' kbps'}` : o.kind === 'image' ? 'Cover / photo as JPG'
        : o.kind === 'gif' ? 'Looping GIF, 480 px' : o.kind === 'sticker' ? 'WhatsApp-size WebP' : `${o.quality === 'best' ? 'Best quality' : o.quality + 'p'} · ${o.container.toUpperCase()}`;
  }
  if (o.fx_look !== 'none' && lk) sub += ' · ✨ ' + lk[1];
  if (AppState.noPreview && AppState.mode === 'video') sub = 'No preview - will try anyway · ' + sub;
  $('goLabel').textContent = label; $('goSub').textContent = sub;
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
    $('looks').innerHTML = LOOKS.map(l => `<button class="look" data-look="${l[0]}"><i style="background:${l[2]}"></i>${l[1]}</button>`).join('');
    document.querySelectorAll('.look').forEach(b => b.addEventListener('click', () => { document.querySelector('[data-o="fx_look"]').value = b.dataset.look; refreshOpts(); }));
    $('presets').innerHTML = Object.entries(PRESETS).map(([k, v]) => `<button class="preset" data-preset="${k}">${v[0]}</button>`).join('');
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
  run({action_type:'url', target:AppState.videoUrl, options}, AppState.mode === 'ig' ? 'Fetching from Instagram…' : options.kind === 'audio' ? 'Extracting audio…' : 'Downloading…');
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
}
function openProgress(title) {
  AppState.resultOpen = false; $('modal').classList.add('open'); $('m-load').style.display = ''; $('m-res').style.display = 'none';
  $('ltitle').textContent = title; $('bar').style.width = '0'; setRing(0); $('lmsg').textContent = 'Starting…'; $('lmeta').textContent = ''; $('cancelBtn').disabled = false;
}
function setRing(p) { p = Math.max(0, Math.min(100, +p || 0)); $('mring').style.setProperty('--p', p); $('mpct').textContent = Math.round(p) + '%'; $('mring').classList.toggle('ind', p < 1); }
function updateProgress(j) { setRing(j.progress); $('bar').style.width = j.progress + '%'; $('lmsg').textContent = j.message; $('lmeta').textContent = [j.speed, j.eta && 'ETA ' + j.eta].filter(Boolean).join('  ·  '); }
function hideProgress() { $('modal').classList.remove('open'); AppState.viewing = null; toast('Running in the background - a popup will tell you when it is done'); }
async function cancelJob() { if (!AppState.viewing) return; $('cancelBtn').disabled = true; $('lmsg').textContent = 'Cancelling…'; await api('/cancel/' + AppState.viewing, {method:'POST'}); }

async function watch() {
  clearTimeout(AppState.wt);
  const ids = tracked(), gids = groups(), all = [...new Set([...ids, ...gids.flat()])];
  if (!all.length) { renderStrip([]); return; }
  const d = await api('/jobs?ids=' + all.map(encodeURIComponent).join(','));
  if (d.success) {
    const byId = Object.fromEntries(d.jobs.map(j => [j.id, j])), keep = [], running = [], gset = new Set(gids.flat());
    for (const id of ids) {
      const j = byId[id]; if (!j) continue;
      if (j.status === 'done' || j.status === 'error') { if (!gset.has(id)) AppState.results.push(j); }
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
  AppState.wt = setTimeout(watch, 1500);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) watch(); });

function renderStrip(running) {
  AppState.lastRunning = running;
  $('tbRun').textContent = running.length || ''; $('pillRun').textContent = running.length ? `⏳ ${running.length} running` : '⏳ idle'; $('pillRun').classList.toggle('on', running.length > 0);
  const more = running.length > 4 ? `<div class="run"><span>+ ${running.length - 4} more running…</span></div>` : '';
  $('strip').innerHTML = running.slice(0, 4).map(j => `<div class="run" data-run-id="${esc(j.id)}"><div class="pring${j.progress < 1 ? ' ind' : ''}" style="--p:${Math.round(j.progress)}"><b>${Math.round(j.progress)}</b></div><span>${esc(j.label)} · ${esc(j.message)}</span></div>`).join('') + more;
  document.querySelectorAll('.run[data-run-id]').forEach(el => el.addEventListener('click', (e) => peek(e.currentTarget.dataset.runId)));
}

function redo() {
  if (!AppState.lastRun) return; closeResult();
  const p = JSON.parse(JSON.stringify(AppState.lastRun.payload)); p.options = Object.assign({}, p.options, {force:true}); run(p, AppState.lastRun.title);
}
function peek(id) { const j = AppState.lastRunning.find(x => x.id === id); if (!j) return; AppState.viewing = id; openProgress(j.label); updateProgress(j); }
function showNextResult() { if (AppState.resultOpen || !AppState.results.length) return; const j = AppState.results.shift(); if (j.id === AppState.viewing) AppState.viewing = null; showResult(j); }
function confetti() {
  if (ui.confetti !== '1' || (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches)) return;
  const cols = ['var(--pc)', '#22d3ee', '#f472b6', '#fbbf24', '#34d399', '#8b5cf6'], r = (a, b) => (a + Math.random() * (b - a)).toFixed(2);
  $('confetti').innerHTML = Array.from({length: 44}, (_, i) => `<i style="left:${r(2, 98)}%;--x:${r(-18, 18)}vw;--r:${r(0, 360)}deg;--d:${r(1.6, 2.8)}s;--c:${cols[i % cols.length]};animation-delay:${r(0, .35)}s"></i>`).join('');
  clearTimeout(confetti.t); confetti.t = setTimeout(() => { $('confetti').innerHTML = ''; }, 3400);
}

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
  const url = f => `/files/${encodeURIComponent(j.category)}/${f.split('/').map(encodeURIComponent).join('/')}?dl=1`;
  $('rlog').innerHTML = j.log ? `<details style="text-align:left;margin-top:10px"><summary style="cursor:pointer;color:var(--mute);font-size:.8rem">Show log</summary><pre style="white-space:pre-wrap;word-break:break-word;font-size:.7rem;max-height:140px;overflow:auto;color:var(--mute)">${esc(j.log)}</pre></details>` : '';
  $('rfiles').style.display = n ? '' : 'none';
  const isImg = f => /\.(gif|webp|jpe?g|png)$/i.test(f);
  $('rfiles').innerHTML = files.map(f => `<div class="rf">${isImg(f) ? `<img class="rimg" loading="lazy" alt="" src="${esc(url(f).replace('?dl=1', ''))}">` : ''}<span title="${esc(f)}">${esc(f)}</span><a class="mini" href="${esc(url(f))}">Save</a></div>`).join('');
  let btns = '';
  if (n > 1) btns += `<a class="btn primary" href="/zip/${encodeURIComponent(j.id)}">⬇ Download all (ZIP)</a>`;
  if (n === 1) btns += `<a class="btn primary" href="${esc(url(files[0]))}">⬇ Save to this device</a>`;
  if (already && AppState.lastRun && AppState.lastRun.payload.action_type === 'url') btns += `<button class="btn ghost" id="rbtnRedo">🔁 Download again</button>`;
  if (!ok && /instagram login|cookies\.txt/i.test(j.message || '')) btns += `<button class="btn ghost" id="rbtnIg">📸 Open Instagram login</button>`;
  $('rbtns').innerHTML = btns + `<button class="btn ghost" id="rbtnClose">Close</button>`;
  
  if ($('rbtnRedo')) $('rbtnRedo').addEventListener('click', redo);
  if ($('rbtnIg')) $('rbtnIg').addEventListener('click', () => { closeResult(); openIgSettings(); });
  if ($('rbtnClose')) $('rbtnClose').addEventListener('click', closeResult);
  
  if (navigator.vibrate) navigator.vibrate(150);
}

function showBatchResult(js) {
  AppState.resultOpen = true;
  const ok = js.filter(j => j.status === 'done').length, bad = js.length - ok, ids = js.filter(j => (j.files || []).length).map(j => j.id);
  $('modal').classList.add('open'); $('m-load').style.display = 'none'; $('m-res').style.display = '';
  $('rico').innerHTML = bad ? '<span class="warn">📦</span>' : CHECK; if (!bad) confetti(); $('rtitle').textContent = `Batch finished: ${ok} done` + (bad ? `, ${bad} failed` : '');
  $('rtitle').style.color = bad ? 'var(--text)' : 'var(--ok)'; $('rdesc').textContent = ''; $('rlog').innerHTML = ''; $('rfiles').style.display = '';
  $('rfiles').innerHTML = js.map(j => `<div class="rf" style="flex-wrap:wrap"><span title="${esc(j.message)}">${j.status === 'done' ? '✔' : '✖'} ${esc(j.label)}</span><small style="color:var(--mute);flex-basis:100%">${esc(j.message)}</small></div>`).join('');
  $('rbtns').innerHTML = (ids.length ? `<a class="btn primary" href="/zip-multi?ids=${ids.map(encodeURIComponent).join(',')}">⬇ Download everything (ZIP)</a>` : '') + `<button class="btn ghost" id="rbtnBatchClose">Close</button>`;
  if ($('rbtnBatchClose')) $('rbtnBatchClose').addEventListener('click', closeResult);
  if (navigator.vibrate) navigator.vibrate(150);
}
function closeResult() { AppState.resultOpen = false; $('modal').classList.remove('open'); showNextResult(); }

/* ---------- batch ---------- */
function toggleBatch() { $('batchbox').classList.toggle('open'); }
async function startBatch() {
  const items = [], bad = [], options = getOpts();
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
  $('tbWatch').textContent = on || ''; $('pillWatch').textContent = `👁 ${on} watching`; $('pillWatch').classList.toggle('on', on > 0);
  const ivs = d.intervals && d.intervals.length ? d.intervals : [15, 30, 60, 180, 360];
  const html = d.watches.length ? d.watches.map((w, i) => `<div class="file" style="--i:${i}">
      <span class="fav${w.enabled ? ' live' : ''}">${w.kind === 'snapchat' ? '👻' : '🎬'}</span>
      <div class="fmain"><div class="fname" title="${esc(w.target)}">${esc(w.label)}</div>
        <div class="fmeta">${w.busy ? '⏳ checking now…' : esc(w.last_status)} · checked ${ago(w.last_check)}${w.enabled ? '' : ' · paused'} · ${w.total_new} downloaded</div></div>
      <div class="fctl">
        <select class="wsel" data-wid="${esc(w.id)}" aria-label="Check how often">${ivs.map(m => `<option value="${m}"${m === w.interval ? ' selected' : ''}>every ${ivLabel(m)}</option>`).join('')}</select>
        <button class="mini wact-check" data-wid="${esc(w.id)}">Check now</button>
        <button class="mini wact-toggle" data-wid="${esc(w.id)}">${w.enabled ? 'Pause' : 'Resume'}</button>
        <button class="mini del wact-del" data-wid="${esc(w.id)}" aria-label="Stop watching">✕</button></div></div>`).join('')
    : '<div class="empty"><b>👀</b>Nothing watched yet.<br>Paste a creator\'s video or a Snapchat username above, then press <b style="display:inline;font-size:inherit">Watch</b>.</div>';
  if (html === lastWatchHtml) return;
  const a = document.activeElement; if (a && a.classList && a.classList.contains('wsel') && $('wlist').contains(a)) return;
  lastWatchHtml = html; $('wlist').innerHTML = html;
  
  // Attach dynamic handlers
  document.querySelectorAll('.wsel').forEach(el => el.addEventListener('change', e => setWInt(e.currentTarget.dataset.wid, e.currentTarget.value)));
  document.querySelectorAll('.wact-check').forEach(el => el.addEventListener('click', e => wact(e.currentTarget.dataset.wid, 'check')));
  document.querySelectorAll('.wact-toggle').forEach(el => el.addEventListener('click', e => wact(e.currentTarget.dataset.wid, 'toggle')));
  document.querySelectorAll('.wact-del').forEach(el => el.addEventListener('click', e => wdel(e.currentTarget.dataset.wid)));
}

async function addWatch(kind, target, label) {
  const r = await post('/watches', {kind, target, label, interval: +$('wint').value, options: getOpts()});
  toast(r.success ? `Watching ✓ - checks ${$('wint').options[$('wint').selectedIndex].text}` : (r.message || 'Could not add')); loadWatches();
}
const watchSnap = () => addWatch('snapchat', AppState.snapUser);
function watchCreator() { AppState.chanUrl ? addWatch('channel', AppState.chanUrl, AppState.chanName) : toast('No creator link found for this video'); }
function watchGo() { return AppState.mode === 'snap' ? watchSnap() : watchCreator(); }
async function setWInt(id, v) { const r = await post('/watches/' + encodeURIComponent(id) + '/interval', {interval: +v}); toast(r.success ? `Now checks every ${ivLabel(+v)}` : (r.message || 'Could not change')); lastWatchHtml = ''; loadWatches(); }
async function wact(id, a) { await api(`/watches/${encodeURIComponent(id)}/${a}`, {method:'POST'}); lastWatchHtml = ''; loadWatches(); }
async function wdel(id) { if (confirm('Stop watching this account?')) { await api('/watches/' + encodeURIComponent(id), {method:'DELETE'}); lastWatchHtml = ''; loadWatches(); } }
setInterval(() => { if (!document.hidden) loadWatches(); }, 8000); loadWatches();

/* ---------- 🔧 maintenance ---------- */
function openMaint() { $('mmodal').classList.add('open'); loadVersion(); }
function closeMaint() { $('mmodal').classList.remove('open'); }
const isTyping = t => !!t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable);
function linkFromText(text) {
  text = String(text || '').trim(); const m = text.match(/https?:\/\/[^\s<>"']+/i);
  if (m) { const u = m[0].replace(/[)\].,;!?]+$/, ''); return isUrl(u) ? u : null; }
  return /^@[A-Za-z0-9._-]{3,32}$/.test(text) ? text : null;
}
function takeLink(v) { setTab('home'); $('url').value = v; onUrlInput(); toast('Link ready - press the big button'); }

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeMaint(); closeSettings(); closeKeys(); return; }
  if ((e.ctrlKey || e.metaKey) && String(e.key).toLowerCase() === 'k') { e.preventDefault(); focusUrl(); return; }
  if (isTyping(e.target) || e.ctrlKey || e.metaKey || e.altKey || document.querySelectorAll('.overlay.open').length) return;
  const k = String(e.key).toLowerCase();
  if (k === '/') { e.preventDefault(); focusUrl(); }
  else if (k === 'c') AppState.mode ? toggleCustom() : toast('Paste a link first, then press C to customize');
  else if (k === 's') openSettings(); else if (k === 't') openMaint(); else if (k === 'h') setTab('home');
  else if (k === 'w') { setTab('watch'); $('watchcard').scrollIntoView({behavior:'smooth', block:'start'}); }
  else if (e.key === '?') $('kmodal').classList.add('open');
});

document.addEventListener('paste', e => {
  if (isTyping(e.target)) return;
  const v = linkFromText(e.clipboardData && e.clipboardData.getData('text')); if (!v) return;
  e.preventDefault(); takeLink(v);
});

document.addEventListener('dragover', e => { e.preventDefault(); $('smart').classList.add('drop'); });
document.addEventListener('dragleave', e => { if (!e.relatedTarget) $('smart').classList.remove('drop'); });
document.addEventListener('drop', e => {
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
  const miss = !(v.ffmpeg && v.snapchat_dl && v.gallery_dl); $('wdot').classList.toggle('show', miss); $('wdot2').classList.toggle('show', miss);
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
async function loadSettings() {
  const d = await api('/settings');
  if (d.success) { $('ntfy').value = d.ntfy || ''; $('creatorFolders').checked = d.creator_folders !== false; $('igcap').value = d.ig_cap || 30; }
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
  $('igRemove').disabled = !s.saved; const exp = !!(s.saved && s.session_expired); $('gdot').classList.toggle('show', exp); $('gdot2').classList.toggle('show', exp);
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

/* ---------- Setup Event Bindings ---------- */
function initStaticEventBindings() {
  // Header / Top
  $('gearBtn').addEventListener('click', openSettings);
  $('wrenchBtn').addEventListener('click', openMaint);
  
  // Smart Input
  $('url').addEventListener('input', onUrlInput);
  $('url').addEventListener('keydown', (e) => { if(e.key === 'Enter') go(); });
  $('pasteBtn').addEventListener('click', pasteLink);
  $('clearBtn').addEventListener('click', clearLink);
  $('useClipBtn').addEventListener('click', useClip);
  $('dismissClipBtn').addEventListener('click', dismissClip);

  // Actions
  $('goBtn').addEventListener('click', go);
  $('custBtn').addEventListener('click', toggleCustom);
  $('watchBtn').addEventListener('click', watchGo);
  
  // Customization
  $('custom').addEventListener('input', e => {
      const t = e.target; if (t && t.type === 'checkbox' && t.dataset.o) document.querySelectorAll(`input[type=checkbox][data-o="${t.dataset.o}"]`).forEach(x => x.checked = t.checked);
      refreshOpts();
  });
  $('cmp').addEventListener('input', (e) => onCmp(e.target.value));
  $('ptitle').addEventListener('click', (e) => e.currentTarget.classList.toggle('open'));
  $('resetPresetBtn').addEventListener('click', () => applyPreset('reset'));
  
  // Batch
  $('toggleBatchBtn').addEventListener('click', toggleBatch);
  $('startBatchBtn').addEventListener('click', startBatch);

  // Modals & Settings
  $('cancelBtn').addEventListener('click', cancelJob);
  $('runBackgroundBtn').addEventListener('click', hideProgress);
  $('closeSettingsBtn').addEventListener('click', closeSettings);
  $('smodalDoneBtn').addEventListener('click', closeSettings);
  $('smodal').addEventListener('click', (e) => { if(e.target === $('smodal')) closeSettings(); });
  
  $('confettiOn').addEventListener('change', (e) => setUi('confetti', e.target.checked ? '1' : '0'));
  $('saveNtfyBtn').addEventListener('click', saveSettings);
  $('testNtfyBtn').addEventListener('click', testSettings);
  $('creatorFolders').addEventListener('change', setCreatorFolders);
  $('igfile').addEventListener('change', igPickFile);
  $('igSave').addEventListener('click', saveIg);
  $('igRemove').addEventListener('click', removeIg);
  $('igcap').addEventListener('change', saveIgCap);
  $('clipOn').addEventListener('change', setClipOn);

  // Maintenance Modal
  $('mmodal').addEventListener('click', (e) => { if(e.target === $('mmodal')) closeMaint(); });
  $('updBtn').addEventListener('click', updateYtdlp);
  $('rstBtn').addEventListener('click', restartApp);
  $('mmodalCloseBtn').addEventListener('click', closeMaint);

  // Bottom Tabs
  document.querySelector('[data-tab="home"]').addEventListener('click', () => setTab('home'));
  document.querySelector('[data-tab="watch"]').addEventListener('click', () => setTab('watch'));
  $('navSettingsBtn').addEventListener('click', openSettings);
  $('navMaintBtn').addEventListener('click', openMaint);

  // Keyboard Modal
  $('kmodal').addEventListener('click', (e) => { if(e.target === $('kmodal')) closeKeys(); });
  $('closeKeysBtn').addEventListener('click', closeKeys);
}

/* ---------- Bootup ---------- */
applyUi();
initDomInjects();
initLooksAndPresets();
initStaticEventBindings();
loadSettings();
setOpts({});
watch(); 
setTimeout(checkClipboard, 700);