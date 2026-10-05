'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = async (path, opts = {}) => {
  const init = { ...opts, headers: { ...(opts.headers || {}) } };
  if (opts.json !== undefined) {
    init.body = JSON.stringify(opts.json);
    init.headers['Content-Type'] = 'application/json';
    delete init.json;
  }
  const res = await fetch('/api/' + path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `请求失败（${res.status}）`);
  return data;
};

const fmtBytes = (n) => {
  if (!n) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n >= 100 || i === 0 ? n.toFixed(0) : n.toFixed(1)} ${u[i]}`;
};
const fmtShort = (n) => fmtBytes(n).replace(' ', '').replace('B', '');
const fmtDur = (s) => {
  if (!s) return '';
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return (h ? `${h}:${String(m).padStart(2, '0')}` : m) + ':' + String(x).padStart(2, '0');
};
const fmtCount = (n) => !n ? '' : n >= 1e8 ? (n / 1e8).toFixed(1) + ' 亿' : n >= 1e4 ? (n / 1e4).toFixed(1) + ' 万' : String(n);
const fmtTime = (iso) => {
  const d = new Date(typeof iso === 'number' ? iso * 1000 : iso);
  const now = new Date();
  const hm = d.toTimeString().slice(0, 5);
  if (d.toDateString() === now.toDateString()) return '今天 ' + hm;
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return '昨天 ' + hm;
  return `${d.getMonth() + 1}月${d.getDate()}日`;
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let toastTimer;
function toast(msg, err = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show' + (err ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.className = 'toast', 2600);
}
async function busy(btn, fn) {
  btn.classList.add('loading'); btn.disabled = true;
  try { return await fn(); } catch (e) { toast(e.message, true); } finally { btn.classList.remove('loading'); btn.disabled = false; }
}
const bind = (key, val) => $$(`[data-bind="${key}"]`).forEach((el) => el.textContent = val);

/* ---------------- 路由 ---------------- */
const views = ['download', 'tasks', 'library', 'settings'];
function route() {
  const v = views.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'download';
  views.forEach((x) => $('#view-' + x).hidden = x !== v);
  $$('[data-view]').forEach((a) => a.classList.toggle('on', a.dataset.view === v));
  if (v === 'library') loadLibrary();
  if (v === 'settings') loadSettings();
  window.scrollTo({ top: 0 });
}
window.addEventListener('hashchange', route);

/* ---------------- 系统信息 ---------------- */
let sys = {};
async function loadSystem() {
  try { sys = await api('系统'); } catch { return; }
  const s = sys['存储'] || {};
  bind('dir', sys['下载目录'].split('/').filter(Boolean).slice(-2).join('/'));
  bind('dir-full', sys['下载目录']);
  bind('free', fmtBytes(s['可用']) || '—');
  bind('total', s['总量'] ? `可用 / ${fmtBytes(s['总量'])}` : '');
  $$('[data-bind="used-bar"]').forEach((i) => i.style.width = s['总量'] ? (s['已用'] / s['总量'] * 100).toFixed(1) + '%' : 0);
  bind('ver', 'v' + sys['版本']); bind('ytdlp', sys.yt_dlp); bind('ffmpeg', sys.ffmpeg); bind('py', sys.python);
  const pill = $('[data-bind="status-pill"]');
  pill.className = 'pill glass hide-m ' + (sys['代理'] ? 'ok' : 'warn');
  pill.querySelector('span').textContent = `${sys['代理'] ? '代理已设置' : '未设置代理'} · yt-dlp ${sys.yt_dlp}`;
  $('#conc').textContent = sys['并发'];
  $('#site-folders').checked = sys['按站点分类'];
  if ($('#sub-mode')) $('#sub-mode').value = sys['字幕方式'] || 'both';
  updateSaveHint();
}

/* ---------------- 解析 ---------------- */
const state = { media: null, url: '', kind: 'video', height: null, container: 'mp4', audio: 'mp3', subs: new Set() };

$('#paste-btn').addEventListener('click', async () => {
  try {
    const text = (await navigator.clipboard.readText()).trim();
    const m = text.match(/https?:\/\/\S+/);
    if (!m) return toast('剪贴板里没有链接', true);
    $('#url-input').value = m[0];
    $('#parse-form').requestSubmit();
  } catch { toast('无法读取剪贴板，请手动粘贴', true); }
});
$('#url-input').addEventListener('paste', (e) => {
  const text = e.clipboardData.getData('text');
  const m = text.match(/https?:\/\/\S+/);
  if (m && m[0] !== text.trim()) { e.preventDefault(); $('#url-input').value = m[0]; }
});

$('#parse-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const raw = $('#url-input').value.trim();
  const url = (raw.match(/https?:\/\/\S+/) || [raw])[0];
  $('#url-input').value = url;
  $('#result-empty').hidden = true; $('#result').hidden = true; $('#result-loading').hidden = false;
  await busy($('#parse-btn'), async () => {
    try {
      const media = await api('解析', { method: 'POST', json: { url } });
      state.media = media; state.url = url; state.subs.clear();
      renderResult();
    } catch (err) {
      $('#result-empty').hidden = false;
      throw err;
    } finally { $('#result-loading').hidden = true; }
  });
});

function heightsOf(formats) {
  const map = new Map();
  for (const f of formats) {
    if (!f.has_video || !f.height) continue;
    const size = f.filesize || f.filesize_approx || 0;
    const prev = map.get(f.height);
    if (!prev || size > prev.size) map.set(f.height, { size, exts: new Set([...(prev?.exts || []), f.ext]) });
    else prev.exts.add(f.ext);
  }
  const audio = Math.max(0, ...formats.filter((f) => f.has_audio && !f.has_video).map((f) => f.filesize || f.filesize_approx || 0));
  return [...map.entries()].sort((a, b) => b[0] - a[0]).map(([h, v]) => ({ h, size: v.size ? v.size + audio : 0, exts: v.exts }));
}
const resLabel = (h) => h >= 4320 ? '8K' : h >= 2160 ? '4K' : h >= 1440 ? '2K' : h + 'P';

function renderResult() {
  const m = state.media;
  $('#thumb').src = m.thumbnail || '';
  $('#duration').textContent = fmtDur(m.duration);
  $('#media-title').textContent = m.title;
  const date = m.upload_date ? `${m.upload_date.slice(0, 4)}-${m.upload_date.slice(4, 6)}-${m.upload_date.slice(6)}` : '';
  $('#media-meta').textContent = [m.uploader, m.site, m.view_count ? fmtCount(m.view_count) + ' 播放' : '', date].filter(Boolean).join(' · ');

  const hs = heightsOf(m.formats);
  const hasAudio = m.formats.some((f) => f.has_audio);
  state.kind = hs.length ? 'video' : hasAudio ? 'audio' : 'cover';
  state.height = hs[0]?.h ?? null;
  $('[data-kind="video"]').disabled = !hs.length;
  $('[data-kind="audio"]').disabled = !hasAudio;
  $('[data-kind="cover"]').disabled = !m.thumbnail;

  $('#res-opts').innerHTML = hs.map((x) =>
    `<button type="button" data-h="${x.h}">${resLabel(x.h)}${x.size ? `<small>${fmtShort(x.size)}</small>` : ''}</button>`).join('');
  $('#afmt-opts').innerHTML = ['mp3', 'm4a'].map((x) => `<button type="button" data-a="${x}">${x.toUpperCase()}</button>`).join('');
  $('#sub-wrap').hidden = !m.subtitles.length;
  $('#sub-opts').innerHTML = m.subtitles.slice(0, 30).map((s) => `<button type="button" data-s="${esc(s)}">${esc(langName(s))}</button>`).join('');
  $('#result').hidden = false;
  syncOptions();
}
function langName(code) {
  try { return new Intl.DisplayNames(['zh-CN'], { type: 'language' }).of(code.replace('_', '-')) || code; } catch { return code; }
}
function containersFor(h) {
  const x = heightsOf(state.media.formats).find((v) => v.h === h);
  const list = ['mp4', 'mkv', 'webm'].filter((c) => c === 'mkv' || x?.exts.has(c));
  return list.length ? list : ['mkv'];
}
function syncOptions() {
  $$('#kind-seg button').forEach((b) => b.classList.toggle('on', b.dataset.kind === state.kind));
  $('#video-opts').hidden = state.kind !== 'video';
  $('#audio-opts').hidden = state.kind !== 'audio';
  $('#sub-wrap').hidden = state.kind !== 'video' || !state.media.subtitles.length;
  $$('#res-opts button').forEach((b) => b.classList.toggle('on', +b.dataset.h === state.height));
  if (state.kind === 'video') {
    const cs = containersFor(state.height);
    if (!cs.includes(state.container)) state.container = cs[0];
    $('#fmt-opts').innerHTML = cs.map((c) => `<button type="button" data-c="${c}" class="${c === state.container ? 'on' : ''}">${c.toUpperCase()}</button>`).join('');
  }
  $$('#afmt-opts button').forEach((b) => b.classList.toggle('on', b.dataset.a === state.audio));
  $$('#sub-opts button').forEach((b) => b.classList.toggle('on', state.subs.has(b.dataset.s)));
  updateSaveHint();
}
function updateSaveHint() {
  if (!state.media) return;
  const ext = state.kind === 'video' ? state.container : state.kind === 'audio' ? state.audio : 'jpg';
  const site = sys['按站点分类'] && state.media.site ? '/' + state.media.site : '';
  $('#save-hint').textContent = `保存至 ${sys['下载目录'] || 'NAS'}${site} · ${ext.toUpperCase()}`;
}
$('#result').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (!b || b.disabled) return;
  if (b.dataset.kind) state.kind = b.dataset.kind;
  else if (b.dataset.h) state.height = +b.dataset.h;
  else if (b.dataset.c) state.container = b.dataset.c;
  else if (b.dataset.a) state.audio = b.dataset.a;
  else if (b.dataset.s) state.subs.has(b.dataset.s) ? state.subs.delete(b.dataset.s) : state.subs.add(b.dataset.s);
  else return;
  syncOptions();
});
$('#download-btn').addEventListener('click', (e) => busy(e.currentTarget, async () => {
  const body = { url: state.url, container: 'jpg', height: null, subtitles: [] };
  if (state.kind === 'video') Object.assign(body, { container: state.container, height: state.height, subtitles: [...state.subs] });
  if (state.kind === 'audio') body.container = state.audio;
  await api('下载', { method: 'POST', json: body });
  toast('已加入下载队列');
  pollSoon();
}));

/* ---------------- 任务 ---------------- */
let jobs = [];
const STATUS = {
  '排队中': ['wt', '等待'], '准备中': ['run', '准备中'], '下载中': ['run', ''], '合并中': ['run', '处理中'],
  '暂停中': ['wt', '暂停中'], '取消中': ['wt', '取消中'], '已完成': ['ok', '完成'], '已取消': ['', '已取消'], '可继续': ['er', '需处理'],
};
const isActive = (j) => ['排队中', '准备中', '下载中', '合并中', '暂停中', '取消中'].includes(j.status);
let jobFilter = 'all';

function jobNode(j) {
  const n = $('#job-tpl').content.firstElementChild.cloneNode(true);
  const img = $('img', n);
  if (j.thumbnail) img.src = j.thumbnail; else img.removeAttribute('src');
  $('.j-title', n).textContent = j.title;
  const [cls, label] = STATUS[j.status] || ['', j.status];
  const pct = j.total ? Math.min(100, Math.round(j.downloaded / j.total * 100)) : 0;
  const st = $('.st', n);
  st.className = 'st ' + cls;
  st.textContent = j.status === '下载中' ? (j.total ? pct + '%' : '下载中') : (j.status === '可继续' && j.error === '已暂停' ? '已暂停' : label);
  const fmt = j.container === 'jpg' ? '封面' : j.height ? `${resLabel(j.height)} · ${j.container.toUpperCase()}` : `音频 · ${j.container.toUpperCase()}`;
  let left = fmt, right = '';
  if (j.status === '下载中') {
    left = j.total ? `${fmtBytes(j.downloaded)} / ${fmtBytes(j.total)}` : fmtBytes(j.downloaded);
    if (j.speed) {
      right = fmtBytes(j.speed) + '/s';
      if (j.total) right += ' · 剩 ' + fmtDur((j.total - j.downloaded) / j.speed);
    }
  } else if (j.status === '已完成') { left = `${fmt} · ${fmtBytes(j.total)}`; right = fmtTime(j.updated_at); }
  else if (j.status === '排队中') right = '排队中';
  else right = fmtTime(j.created_at);
  $('.j-left', n).textContent = left;
  $('.j-right', n).textContent = right;
  const track = $('.track', n);
  if (['下载中', '合并中'].includes(j.status) && j.total) $('i', track).style.width = (j.status === '合并中' ? 100 : pct) + '%';
  else track.remove();
  $('.j-err', n).textContent = j.status === '可继续' && j.error !== '已暂停' ? j.error || '' : '';
  const acts = $('.j-acts', n);
  const btn = (act, icon, title, cls = '') => `<button class="icon-btn ${cls}" data-act="${act}" data-id="${j.id}" title="${title}" aria-label="${title}"><svg><use href="#i-${icon}"/></svg></button>`;
  let html = '';
  if (['排队中', '准备中', '下载中'].includes(j.status)) html += btn('暂停', 'pause', '暂停');
  if (['可继续', '已取消'].includes(j.status)) html += btn('继续', j.status === '可继续' && j.error === '已暂停' ? 'play' : 'retry', '继续');
  if (isActive(j) && j.status !== '取消中') html += btn('取消', 'x', '取消');
  if (j.status === '已完成' && j.saved_path) html += btn('打开', 'play', '预览');
  if (!isActive(j)) html += btn('删除', 'trash', '删除记录', 'danger');
  acts.innerHTML = html;
  return n;
}
function renderJobs() {
  const recent = jobs.slice(0, 6);
  const r = $('#recent-jobs');
  r.replaceChildren(...recent.map(jobNode));
  if (!recent.length) r.innerHTML = '<div class="list-empty">还没有任务</div>';
  const f = {
    all: () => true, active: isActive, done: (j) => j.status === '已完成', failed: (j) => j.status === '可继续',
  }[jobFilter];
  const list = jobs.filter(f);
  const a = $('#all-jobs');
  a.replaceChildren(...list.map(jobNode));
  if (!list.length) a.innerHTML = '<div class="list-empty">这里空空如也</div>';
  const active = jobs.filter(isActive).length;
  $$('[data-count="active"]').forEach((el) => el.textContent = active || '');
}
async function loadJobs() {
  try { jobs = await api('下载/任务'); renderJobs(); } catch { /* 静默 */ }
}
document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-act]');
  if (!b) return;
  const { act, id } = b.dataset;
  const j = jobs.find((x) => x.id === id);
  try {
    if (act === '打开') return openViewer(libPath(j.saved_path), j.title);
    if (act === '删除') await api(`下载/任务/${id}`, { method: 'DELETE' });
    else await api(`下载/任务/${id}/${act}`, { method: 'POST' });
    pollSoon();
  } catch (err) { toast(err.message, true); }
});
const libPath = (p) => {
  const root = (sys['下载目录'] || '').replace(/\/$/, '') + '/';
  return p.startsWith(root) ? p.slice(root.length) : p;
};
$('#job-filter').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  jobFilter = b.dataset.f;
  $$('#job-filter button').forEach((x) => x.classList.toggle('on', x === b));
  renderJobs();
});
$('#pause-all').addEventListener('click', (e) => busy(e.currentTarget, async () => { const r = await api('下载/任务/全部暂停', { method: 'POST' }); toast(`已暂停 ${r['数量']} 个任务`); pollSoon(); }));
$('#resume-all').addEventListener('click', (e) => busy(e.currentTarget, async () => { const r = await api('下载/任务/全部继续', { method: 'POST' }); toast(`已继续 ${r['数量']} 个任务`); pollSoon(); }));
$('#clear-done').addEventListener('click', (e) => busy(e.currentTarget, async () => { const r = await api('下载/任务', { method: 'DELETE' }); toast(`已清除 ${r['数量']} 条记录`); pollSoon(); }));

// 自适应轮询：有活动任务 1s，空闲 8s，页面隐藏时暂停
let pollTimer;
function schedule() {
  clearTimeout(pollTimer);
  if (document.hidden) return;
  pollTimer = setTimeout(tick, jobs.some(isActive) ? 1000 : 8000);
}
let lastActive = 0;
async function tick() {
  await loadJobs();
  const active = jobs.filter(isActive).length;
  if (active < lastActive) { loadSystem(); if (!$('#view-library').hidden) loadLibrary(); }
  lastActive = active;
  schedule();
}
function pollSoon() { clearTimeout(pollTimer); pollTimer = setTimeout(tick, 200); }
document.addEventListener('visibilitychange', () => document.hidden ? clearTimeout(pollTimer) : pollSoon());

/* ---------------- 媒体库 ---------------- */
let libTimer;
async function loadLibrary() {
  let items;
  try { items = await api('媒体库?q=' + encodeURIComponent($('#lib-search').value.trim())); } catch (e) { return toast(e.message, true); }
  const total = items.reduce((a, b) => a + b.size, 0);
  bind('lib-summary', `${items.length} 个文件 · ${fmtBytes(total) || '0 B'}`);
  $$('[data-count="library"]').forEach((el) => el.textContent = items.length || '');
  const grid = $('#lib-grid');
  if (!items.length) { grid.innerHTML = '<div class="card glass list-empty" style="grid-column:1/-1">还没有下载内容</div>'; return; }
  libItems = new Map(items.map((it) => [it.path, it]));
  grid.innerHTML = items.map((it) => `
    <article class="tile glass" data-path="${esc(it.path)}" data-name="${esc(it.name)}" tabindex="0">
      <div class="cv">${it.cover ? `<img loading="lazy" alt="" src="/api/媒体库/文件?path=${encodeURIComponent(it.cover)}">` : ''}<span>${esc(it.ext)}</span>${(it.subtitles || []).length ? `<span class="cc" title="${esc(it.subtitles.map((s) => langName(s.lang)).join('、'))}">CC</span>` : ''}</div>
      <div class="ti"><b>${esc(it.name)}</b><p>${fmtBytes(it.size)} · ${fmtTime(it.mtime)}${(it.subtitles || []).length ? ' · ' + it.subtitles.length + ' 个字幕' : ''}</p></div>
    </article>`).join('');
}
let libItems = new Map();
$('#lib-search').addEventListener('input', () => { clearTimeout(libTimer); libTimer = setTimeout(loadLibrary, 250); });
$('#lib-grid').addEventListener('click', (e) => {
  const t = e.target.closest('.tile'); if (t) openViewer(t.dataset.path, t.dataset.name);
});
$('#lib-grid').addEventListener('keydown', (e) => {
  const t = e.target.closest('.tile'); if (t && e.key === 'Enter') openViewer(t.dataset.path, t.dataset.name);
});

function openViewer(path, name) {
  const url = '/api/媒体库/文件?path=' + encodeURIComponent(path);
  const ext = path.split('.').pop().toLowerCase();
  const media = ['mp3', 'm4a', 'opus', 'flac', 'wav'].includes(ext) ? `<audio src="${url}" controls autoplay></audio>`
    : ['jpg', 'jpeg', 'png', 'webp'].includes(ext) ? `<img src="${url}" alt="">`
    : `<video src="${url}" controls autoplay playsinline crossorigin="anonymous">${((libItems.get(path) || {}).subtitles || []).map((s, i) =>
        `<track kind="subtitles" src="/api/媒体库/字幕?path=${encodeURIComponent(s.path)}" srclang="${esc(s.lang)}" label="${esc(langName(s.lang))}"${i === 0 ? ' default' : ''}>`).join('')}</video>`;
  $('#viewer-body').innerHTML = `${media}<h3>${esc(name)}</h3><p class="muted small mono">${esc(path)}</p>
    <div class="row-btns"><a class="btn ghost" href="${url}&download=true">下载到本机</a><button class="btn ghost danger" id="lib-del">删除文件</button></div>`;
  $('#lib-del').onclick = async () => {
    if (!confirm(`确定删除「${name}」？此操作不可恢复。`)) return;
    try { await api('媒体库/文件?path=' + encodeURIComponent(path), { method: 'DELETE' }); closeViewer(); toast('已删除'); loadLibrary(); loadSystem(); } catch (e) { toast(e.message, true); }
  };
  $('#viewer').showModal();
}
function closeViewer() { $('#viewer-body').innerHTML = ''; $('#viewer').close(); }
$('#viewer').addEventListener('click', (e) => { if (e.target === e.currentTarget || e.target.closest('[data-close]')) closeViewer(); });
$('#viewer').addEventListener('close', () => $('#viewer-body').innerHTML = '');

/* ---------------- 设置 ---------------- */
async function loadSettings() {
  loadSystem();
  try { const p = await api('代理'); $('#proxy-input').value = p['地址']; } catch { /* */ }
  loadCookies();
}
async function loadCookies() {
  try {
    const c = await api('Cookies');
    $('#cookie-sites').innerHTML = c['站点'].length ? c['站点'].map((s) => `<span>${esc(s)}</span>`).join('') : '<p class="muted small">尚未导入</p>';
  } catch { /* */ }
}
const proxyMsg = (t) => $('#proxy-msg').textContent = t;
$('#proxy-form').addEventListener('submit', (e) => {
  e.preventDefault();
  busy($('button', e.currentTarget), async () => {
    const r = await api('代理', { method: 'PUT', json: { 地址: $('#proxy-input').value.trim() } });
    $('#proxy-input').value = r['地址']; toast(r['已设置'] ? '代理已保存' : '代理已清除'); loadSystem();
  });
});
$('#proxy-detect').addEventListener('click', (e) => busy(e.currentTarget, async () => {
  proxyMsg('正在扫描本机常见端口…');
  const r = await api('代理/自动检测', { method: 'POST' });
  proxyMsg(r['消息']); if (r['已设置']) $('#proxy-input').value = r['地址']; loadSystem();
}));
$('#proxy-test').addEventListener('click', (e) => busy(e.currentTarget, async () => {
  const r = await api('代理/测试', { method: 'POST' });
  proxyMsg(r['可用'] ? `可用 · ${r['延迟']} ms` : r['消息']);
}));
$('#proxy-clear').addEventListener('click', (e) => busy(e.currentTarget, async () => {
  await api('代理', { method: 'DELETE' }); $('#proxy-input').value = ''; proxyMsg(''); toast('代理已清除'); loadSystem();
}));
$('#cookie-file').addEventListener('change', async (e) => {
  const f = e.target.files[0]; if (!f) return;
  try {
    await api('Cookies', { method: 'PUT', body: await f.arrayBuffer(), headers: { 'Content-Type': 'text/plain' } });
    toast('Cookies 已导入'); loadCookies();
  } catch (err) { toast(err.message, true); }
  e.target.value = '';
});
$('#cookie-clear').addEventListener('click', (e) => {
  if (!confirm('确定清除全部 Cookies？')) return;
  busy(e.currentTarget, async () => { await api('Cookies', { method: 'DELETE' }); toast('已清除'); loadCookies(); });
});
$$('.stepper button').forEach((b) => b.addEventListener('click', async () => {
  const v = Math.max(1, Math.min(6, (+$('#conc').textContent) + (+b.dataset.step)));
  try { sys = await api('设置', { method: 'PUT', json: { concurrency: v } }); $('#conc').textContent = sys['并发']; } catch (e) { toast(e.message, true); }
}));
$('#sub-mode').addEventListener('change', async (e) => {
  try { sys = await api('设置', { method: 'PUT', json: { subtitle_mode: e.target.value } }); toast('字幕方式已保存'); } catch (err) { toast(err.message, true); }
});
$('#site-folders').addEventListener('change', async (e) => {
  try { sys = await api('设置', { method: 'PUT', json: { site_folders: e.target.checked } }); updateSaveHint(); } catch (err) { toast(err.message, true); }
});
$('#update-ytdlp').addEventListener('click', async (e) => {
  const b = e.currentTarget; b.textContent = '更新中…'; b.disabled = true;
  try {
    const r = await api('系统/更新解析器', { method: 'POST' });
    if (r['需要重启']) {
      toast(`已更新到 ${r['更新后']}，正在重启服务…`);
      await api('系统/重启', { method: 'POST' }).catch(() => {});
      setTimeout(() => location.reload(), 4000);
    } else toast('已是最新版本');
  } catch (err) { toast(err.message, true); }
  b.textContent = '检查更新'; b.disabled = false;
});

/* ---------------- 启动 ---------------- */
bind('today', new Date().toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }));
route();
loadSystem();
tick();
api('媒体库').then((x) => $$('[data-count="library"]').forEach((el) => el.textContent = x.length || '')).catch(() => {});
const shared = new URLSearchParams(location.search).get('url') || new URLSearchParams(location.search).get('text');
if (shared) {
  const m = shared.match(/https?:\/\/\S+/);
  if (m) { $('#url-input').value = m[0]; history.replaceState(null, '', '/'); $('#parse-form').requestSubmit(); }
}
