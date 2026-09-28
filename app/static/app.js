(() => {
  'use strict';

  const VIDEO_CONTAINERS = ['mp4', 'mkv', 'webm'];
  const POLL_ACTIVE_MS = 1000;
  const POLL_IDLE_MS = 10000;
  const ACTIVE_STATUSES = new Set(['准备中', '下载中', '合并中', '取消中']);

  const state = {
    parsed: null, container: null, height: null,
    subtitles: [], previousFocus: null, pollTimer: null,
  };

  const $ = (id) => document.getElementById(id);

  const setStatus = (el, message = '', kind = '') => {
    el.textContent = message;
    el.dataset.kind = kind;
  };

  const formatsFor = (p) => Array.isArray(p?.formats) ? p.formats : [];
  const audioFormats = (p) => formatsFor(p).filter((f) => f.has_audio);
  const videoFormats = (p, container = null) => formatsFor(p).filter((f) =>
    f.has_video && Number.isFinite(f.height) &&
    (!container || container === 'mkv' || f.ext === container));

  const downloadableVideos = (p, container) => {
    const audio = audioFormats(p).filter((f) => !f.has_video);
    const hasCompatibleAudio = audio.some((f) =>
      container === 'mkv' ||
      (container === 'mp4' ? ['m4a', 'mp4'].includes(f.ext) : f.ext === container));
    return videoFormats(p, container).filter((f) => f.has_audio || hasCompatibleAudio);
  };

  const containersFor = (p) => {
    const out = VIDEO_CONTAINERS.filter((c) => downloadableVideos(p, c).length > 0);
    if (audioFormats(p).length) out.push('mp3');
    return out;
  };

  const heightsFor = (p, container) =>
    [...new Set(downloadableVideos(p, container).map((f) => f.height))].sort((a, b) => b - a);

  const allHeightsFor = (p) =>
    [...new Set(VIDEO_CONTAINERS.flatMap((c) => heightsFor(p, c)))].sort((a, b) => b - a);

  window.buildDownloadPayload = (p, container, height, subtitles = state.subtitles) => {
    if (!p?.url || !containersFor(p).includes(container)) return null;
    if (container === 'mp3')
      return audioFormats(p).length ? { url: p.url, container, height: null, subtitles } : null;
    return downloadableVideos(p, container).some((f) => f.height === height)
      ? { url: p.url, container, height, subtitles } : null;
  };

  window.matchHistoryChoice = (p, item) => {
    const container = String(item?.container || '').toLowerCase();
    if (!containersFor(p).includes(container)) return null;
    if (container === 'mp3') return { container, height: null };
    return heightsFor(p, container).includes(item?.height)
      ? { container, height: item.height } : null;
  };

  const request = async (url, options) => {
    const res = await fetch(url, options);
    if (!res.ok) {
      let msg = '请求失败，请稍后再试';
      try { msg = (await res.json()).detail || msg; } catch (_) { /* default */ }
      throw new Error(msg);
    }
    return res;
  };

  const scheduleNextPoll = (hasActive) => {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(pollJobs, hasActive ? POLL_ACTIVE_MS : POLL_IDLE_MS);
  };

  const pollJobs = async () => {
    try {
      const jobs = await (await request('/api/下载/任务')).json();
      const active = jobs.filter((j) => ACTIVE_STATUSES.has(j.status));
      $('jobs-section').hidden = active.length === 0;
      $('jobs-list').replaceChildren(...active.map(jobItem));
      if (jobs.some((j) => j.status === '已完成')) await loadHistory();
      scheduleNextPoll(active.length > 0);
    } catch (_) {
      setStatus($('parse-status'), '无法读取下载任务。', 'error');
      scheduleNextPoll(false);
    }
  };

  const startPolling = () => {
    clearTimeout(state.pollTimer);
    pollJobs();
  };

  const durationText = (seconds) => {
    if (!Number.isFinite(seconds)) return '';
    const total = Math.max(0, Math.round(seconds));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return `时长：${h ? `${h}:` : ''}${String(m).padStart(h ? 2 : 1, '0')}:${String(s).padStart(2, '0')}`;
  };

  const chip = (label, pressed, onClick) => {
    const btn = document.createElement('button');
    btn.className = 'opt-chip';
    btn.type = 'button';
    btn.textContent = label;
    btn.setAttribute('aria-pressed', String(pressed));
    btn.addEventListener('click', onClick);
    return btn;
  };

  const renderOptions = () => {
    const p = state.parsed;
    const containers = containersFor(p);
    if (!containers.includes(state.container)) state.container = containers[0] ?? null;
    const heights = allHeightsFor(p);
    if (state.container === 'mp3') state.height = null;
    else if (!heights.includes(state.height)) state.height = heights[0] ?? null;

    $('fmt-opts').replaceChildren(...containers.map((c) =>
      chip(c.toUpperCase(), state.container === c, () => { state.container = c; renderOptions(); })));

    $('res-opts').replaceChildren(...(state.container === 'mp3'
      ? [chip('仅音频', true, () => {})]
      : heights.map((h) => chip(`${h}P`, state.height === h, () => {
          state.height = h;
          if (!heightsFor(p, state.container).includes(h)) {
            state.container = containers.find((c) => heightsFor(p, c).includes(h));
            setStatus($('parse-status'), `已切换为 ${state.container.toUpperCase()}，以支持 ${h}P。`, 'success');
          }
          renderOptions();
        }))));

    $('res-fieldset').hidden = state.container === 'mp3';
    $('download-btn').disabled = !window.buildDownloadPayload(p, state.container, state.height);
  };

  const renderSubtitles = (p) => {
    const available = Array.isArray(p.subtitles) ? p.subtitles : [];
    state.subtitles = state.subtitles.filter((l) => available.includes(l));
    $('sub-block').hidden = available.length === 0;
    $('sub-opts').replaceChildren(...available.map((lang) =>
      chip(lang, state.subtitles.includes(lang), () => {
        state.subtitles = state.subtitles.includes(lang)
          ? state.subtitles.filter((l) => l !== lang)
          : [...state.subtitles, lang];
        renderSubtitles(p);
      })));
  };

  const renderParsed = (p) => {
    state.parsed = p;
    state.subtitles = [];
    $('media-title').textContent = p.title || '未命名视频';
    $('media-duration').textContent = durationText(p.duration);
    const img = $('thumb');
    img.hidden = !p.thumbnail;
    img.src = p.thumbnail || '';
    $('result-area').hidden = false;
    renderOptions();
    renderSubtitles(p);
  };

  const parseLink = async (url, msg = '正在解析链接…') => {
    setStatus($('parse-status'), msg);
    const res = await request('/api/解析', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    data.url = url;
    renderParsed(data);
    setStatus($('parse-status'), '解析完成，请选择可用格式与清晰度。', 'success');
    return data;
  };

  const download = async () => {
    const payload = window.buildDownloadPayload(state.parsed, state.container, state.height);
    if (!payload) {
      setStatus($('parse-status'), '当前选择已不可用，请重新解析链接。', 'error');
      return;
    }
    const btn = $('download-btn');
    btn.disabled = true;
    setStatus($('parse-status'), '正在提交下载任务…', 'success');
    try {
      await request('/api/下载', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      setStatus($('parse-status'), '已加入后台下载任务，可关闭页面。', 'success');
      startPolling();
    } catch (err) {
      setStatus($('parse-status'), err.message, 'error');
    } finally {
      btn.disabled = !window.buildDownloadPayload(state.parsed, state.container, state.height);
    }
  };

  const bytes = (v) => {
    if (!Number.isFinite(v) || v < 1) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(Math.floor(Math.log(v) / Math.log(1024)), units.length - 1);
    return `${(v / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
  };

  const jobMeta = (j) =>
    `${String(j.container || '').toUpperCase()}` +
    `${j.height ? ` · ${j.height}P` : ' · 音频'}` +
    `${j.subtitles?.length ? ` · 字幕 ${j.subtitles.join('、')}` : ''}`;

  const actionBtn = (label, onClick, className = 'btn-secondary') => {
    const btn = document.createElement('button');
    btn.className = className;
    btn.type = 'button';
    btn.textContent = label;
    btn.addEventListener('click', onClick);
    return btn;
  };

  const jobItem = (j) => {
    const tpl = $('job-tpl').content.firstElementChild.cloneNode(true);
    const dl = (sel) => tpl.querySelector(sel);
    dl('.job-name').textContent = j.title || '正在解析…';
    const badge = dl('.badge');
    badge.textContent = j.status;
    badge.dataset.status = j.status;
    dl('.job-meta-line').textContent = jobMeta(j);
    const downloaded = Number(j.downloaded) || 0;
    const total = Number(j.total) || 0;
    const pct = total ? Math.min(100, Math.round(downloaded / total * 100)) : 0;
    dl('.progress-track').setAttribute('aria-valuenow', String(pct));
    dl('.progress-fill').style.width = `${pct}%`;
    dl('.job-progress').textContent =
      j.status === '已完成' ? `已完成 · ${bytes(total || downloaded)}` :
      j.status === '取消中' ? '正在取消并清理临时文件…' :
      j.status === '已取消' ? '已取消，临时文件已清理。' :
      total
        ? `${pct}% · ${bytes(downloaded)} / ${bytes(total)} · ${j.speed ? `${bytes(Number(j.speed))}/秒` : '等待中'}`
        : `${bytes(downloaded)} 已下载 · ${j.speed ? `${bytes(Number(j.speed))}/秒` : '等待中'}`;
    const errEl = dl('.job-error');
    errEl.hidden = !j.error;
    errEl.textContent = j.error || '';
    const actions = dl('.job-actions');
    if (j.status === '可继续') {
      actions.append(actionBtn('继续下载', async () => {
        try { await request(`/api/下载/任务/${j.id}/继续`, { method: 'POST' }); startPolling(); }
        catch (err) { setStatus($('parse-status'), err.message, 'error'); }
      }));
    }
    if (ACTIVE_STATUSES.has(j.status) && j.status !== '取消中') {
      actions.append(actionBtn('取消下载', async () => {
        if (!window.confirm('确定取消下载并删除当前临时片段吗？')) return;
        try { await request(`/api/下载/任务/${j.id}/取消`, { method: 'POST' }); startPolling(); }
        catch (err) { setStatus($('parse-status'), err.message, 'error'); }
      }, 'btn-text'));
    }
    if (j.status === '可继续') {
      actions.append(actionBtn('删除临时文件', async () => {
        if (!window.confirm('确定删除该任务及其临时文件吗？')) return;
        try { await request(`/api/下载/任务/${j.id}`, { method: 'DELETE' }); startPolling(); }
        catch (err) { setStatus($('parse-status'), err.message, 'error'); }
      }, 'btn-text'));
    }
    return tpl;
  };

  const historyMeta = (item) =>
    `${String(item.container || '').toUpperCase()}` +
    `${item.height ? ` · ${item.height}P` : ' · 音频'}` +
    ` · ${new Date(item.created_at).toLocaleString('zh-CN')}`;

  const historyItem = (item) => {
    const li = document.createElement('li');
    li.className = 'hist-item';
    const audio = item.container === 'mp3';
    const ic = document.createElement('div');
    ic.className = `hist-icon${audio ? ' hist-icon--audio' : ''}`;
    ic.innerHTML = audio
      ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>'
      : '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 3v12m0 0-4-4m4 4 4-4M5 19h14"/></svg>';
    const body = document.createElement('div');
    body.className = 'hist-body';
    const title = document.createElement('div');
    title.className = 'hist-title';
    title.textContent = item.title || '未命名视频';
    const meta = document.createElement('div');
    meta.className = 'hist-meta';
    meta.textContent = historyMeta(item);
    body.append(title, meta);
    const actions = document.createElement('div');
    actions.className = 'hist-actions';
    const repeatBtn = document.createElement('button');
    repeatBtn.className = 'btn-secondary';
    repeatBtn.type = 'button';
    repeatBtn.textContent = '重新下载';
    repeatBtn.addEventListener('click', async () => {
      $('url-input').value = item.url;
      try {
        const parsed = await parseLink(item.url, '正在重新解析链接…');
        const match = window.matchHistoryChoice(parsed, item);
        if (!match) throw new Error('当前链接不再提供该格式或清晰度，请重新选择。');
        state.container = match.container;
        state.height = match.height;
        renderOptions();
        download();
      } catch (err) { setStatus($('parse-status'), err.message, 'error'); }
    });
    const removeBtn = document.createElement('button');
    removeBtn.className = 'btn-text';
    removeBtn.type = 'button';
    removeBtn.textContent = '删除';
    removeBtn.addEventListener('click', async () => {
      if (!window.confirm('确定删除这条下载记录吗？')) return;
      try { await request(`/api/历史/${item.id}`, { method: 'DELETE' }); await loadHistory(); }
      catch (err) { setStatus($('parse-status'), err.message, 'error'); }
    });
    actions.append(repeatBtn, removeBtn);
    li.append(ic, body, actions);
    return li;
  };

  const loadHistory = async () => {
    try {
      const items = await (await request('/api/历史')).json();
      $('history-list').replaceChildren(...items.map(historyItem));
      $('history-empty').hidden = items.length > 0;
      $('clear-history').disabled = items.length === 0;
    } catch (_) { setStatus($('parse-status'), '无法读取下载历史。', 'error'); }
  };

  const modal = $('settings-modal');
  const focusable = () => [...modal.querySelectorAll('button:not([disabled]),input:not([disabled])')];
  const closeModal = () => { modal.hidden = true; state.previousFocus?.focus(); };

  const loadProxy = async () => {
    const st = $('proxy-status');
    try {
      const data = await (await request('/api/代理')).json();
      $('proxy-input').value = '';
      $('proxy-input').placeholder = data.已设置 ? '输入完整地址以替换当前代理' : 'http://127.0.0.1:7890';
      setStatus(st, data.已设置 ? `当前代理：${data.地址}` : '当前未设置代理。', data.已设置 ? 'success' : '');
    } catch (_) { setStatus(st, '无法读取代理设置。', 'error'); }
  };

  const loadCookies = async () => {
    const st = $('cookies-status');
    try {
      const data = await (await request('/api/Cookies')).json();
      setStatus(st, data.已设置 ? '已导入 Cookies。' : '当前未导入 Cookies。', data.已设置 ? 'success' : '');
    } catch (_) { setStatus(st, '无法读取 Cookies 状态。', 'error'); }
  };

  const openModal = async () => {
    state.previousFocus = document.activeElement;
    modal.hidden = false;
    await Promise.all([loadProxy(), loadCookies()]);
    $('proxy-input').focus();
  };

  $('parse-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const url = $('url-input').value.trim();
    if (!url) { setStatus($('parse-status'), '请输入视频链接。', 'error'); return; }
    const btn = $('parse-btn');
    btn.disabled = true;
    try { await parseLink(url); }
    catch (err) { setStatus($('parse-status'), err.message, 'error'); }
    finally { btn.disabled = false; }
  });
  $('download-btn').addEventListener('click', download);
  $('clear-history').addEventListener('click', async () => {
    if (!window.confirm('确定清空所有共享下载记录吗？')) return;
    await request('/api/历史', { method: 'DELETE' });
    await loadHistory();
  });
  $('open-settings').addEventListener('click', openModal);
  $('close-settings').addEventListener('click', closeModal);
  modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });

  $('proxy-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const addr = $('proxy-input').value.trim();
    const st = $('proxy-status');
    if (!addr) { setStatus(st, '请输入完整代理地址；清除请使用「清除」按钮。', 'error'); return; }
    try {
      const data = await (await request('/api/代理', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 地址: addr }),
      })).json();
      $('proxy-input').value = '';
      setStatus(st, `已保存代理：${data.地址}`, 'success');
    } catch (err) { setStatus(st, err.message, 'error'); }
  });
  $('auto-detect-proxy').addEventListener('click', async () => {
    const st = $('proxy-status');
    setStatus(st, '正在检测代理…');
    try {
      const data = await (await request('/api/代理/自动检测', { method: 'POST' })).json();
      setStatus(st, data.消息, data.已设置 ? 'success' : 'error');
      $('proxy-input').value = '';
    } catch (err) { setStatus(st, err.message, 'error'); }
  });
  $('clear-proxy').addEventListener('click', async () => {
    const st = $('proxy-status');
    try {
      await request('/api/代理', { method: 'DELETE' });
      $('proxy-input').value = '';
      setStatus(st, '已清除代理。', 'success');
    } catch (err) { setStatus(st, err.message, 'error'); }
  });
  $('import-cookies').addEventListener('click', async () => {
    const st = $('cookies-status');
    const file = $('cookies-input').files[0];
    if (!file) { setStatus(st, '请选择 cookies.txt 文件。', 'error'); return; }
    const btn = $('import-cookies');
    btn.disabled = true;
    setStatus(st, '正在导入 Cookies…');
    try {
      await request('/api/Cookies', {
        method: 'PUT',
        headers: { 'Content-Type': 'text/plain;charset=UTF-8' },
        body: file,
      });
      $('cookies-input').value = '';
      await loadCookies();
    } catch (err) { setStatus(st, err.message, 'error'); }
    finally { btn.disabled = false; }
  });
  $('clear-cookies').addEventListener('click', async () => {
    const st = $('cookies-status');
    if (!window.confirm('确定清除已导入的 Cookies 吗？')) return;
    try {
      await request('/api/Cookies', { method: 'DELETE' });
      $('cookies-input').value = '';
      await loadCookies();
    } catch (err) { setStatus(st, err.message, 'error'); }
  });

  document.addEventListener('keydown', (e) => {
    if (modal.hidden) return;
    if (e.key === 'Escape') { e.preventDefault(); closeModal(); return; }
    if (e.key === 'Tab') {
      const targets = focusable();
      const first = targets[0];
      const last = targets.at(-1);
      if (!first) return;
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  loadHistory();
  startPolling();
})();
