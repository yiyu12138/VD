import os
import re
import shutil
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.responses import FileResponse, HTMLResponse

from app import __version__
from app.media import (
    download_cover,
    download_media,
    merge_cookie_files,
    parse_media,
    proxy_candidates,
    StopDownload,
    proxy_works,
    select_format,
    validate_cookie_file,
    validate_url,
)
from app.store import Store
from app.system import ffmpeg_version, storage_info, update_ytdlp, ytdlp_version
from app.worker import JobQueue

ROOT = Path(__file__).parent
ACTIVE = {'排队中', '准备中', '下载中', '合并中'}
VIDEO_EXT = {'.mp4', '.mkv', '.webm', '.mov', '.m4v'}
AUDIO_EXT = {'.mp3', '.m4a', '.opus', '.flac', '.wav'}
IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.webp'}


def data_dir() -> Path:
    return Path(os.environ.get('DATA_DIR', '/data'))


def download_directory() -> Path:
    return Path(os.environ.get('DOWNLOAD_DIR', '/downloads'))


def jobs_directory() -> Path:
    return data_dir() / 'jobs'


def cookies_file() -> Path:
    return data_dir() / 'cookies.txt'


@asynccontextmanager
async def lifespan(application: FastAPI):
    store = Store(data_dir() / 'app.db')
    store.recover_interrupted_jobs()
    application.state.store = store
    application.state.queue = JobQueue(run_download_job, store.get_int('concurrency', 2))
    for job in store.list_jobs(status='排队中'):
        application.state.queue.submit(job['id'])
    yield
    application.state.queue.shutdown()


app = FastAPI(docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')


def get_store() -> Store:
    try:
        return app.state.store
    except AttributeError:
        app.state.store = Store(data_dir() / 'app.db')
        return app.state.store


def get_queue() -> JobQueue:
    try:
        return app.state.queue
    except AttributeError:
        app.state.queue = JobQueue(run_download_job, 2)
        return app.state.queue


def safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')
    return cleaned[:120] or '未命名视频'


def valid_proxy(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return (
        parts.scheme in {'http', 'https', 'socks4', 'socks4a', 'socks5', 'socks5h'}
        and bool(parts.hostname)
        and parts.password != '***'
    )


def parse_error_message(error: Exception) -> str:
    text = str(error).lower()
    if 'fresh cookies' in text or ('cookies' in text and 'douyin' in text):
        return '抖音要求最新登录 Cookies；请使用「分享」复制的视频链接，若仍失败则该内容需要登录 Cookies。'
    if 'unsupported url' in text and 'douyin' in text:
        return '请输入抖音视频分享链接，而不是精选页或频道页链接。'
    if 'sign in' in text or 'login' in text or 'cookies' in text:
        return '该内容需要登录，请在设置中导入对应站点的 Cookies。'
    if 'unsupported url' in text:
        return '暂不支持这个链接。'
    if 'timed out' in text or 'network' in text or 'connection' in text:
        return '网络连接失败，请检查代理设置。'
    return '解析失败，请检查链接、权限或代理设置'


def friendly_error(error: Exception) -> str:
    message = str(error).strip() or error.__class__.__name__
    message = re.sub(r'\x1b\[[0-9;]*m', '', message)
    return message.removeprefix('ERROR: ')[:300]


class ParseRequest(BaseModel):
    url: str


class ProxyRequest(BaseModel):
    地址: str | None = None


class SettingsRequest(BaseModel):
    concurrency: int | None = None
    site_folders: bool | None = None


class DownloadRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str
    container: str
    height: int | None = None
    subtitles: list[str] = []


class DownloadCancelled(StopDownload):
    pass


class DownloadPaused(StopDownload):
    pass


def validate_download_request(request: DownloadRequest) -> None:
    if not validate_url(request.url):
        raise HTTPException(400, '请输入有效的 HTTP 或 HTTPS 链接')
    if request.container not in {'mp4', 'mkv', 'webm', 'mp3', 'm4a', 'jpg'}:
        raise HTTPException(400, '请选择支持的文件格式')
    if request.container in {'mp3', 'm4a', 'jpg'}:
        if request.height is not None:
            raise HTTPException(400, '音频或封面下载不应指定清晰度')
    elif request.height is None or request.height <= 0:
        raise HTTPException(400, '请选择可用清晰度')


def enqueue_job(request: DownloadRequest) -> dict:
    validate_download_request(request)
    store = get_store()
    job_id = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    item = {
        'id': job_id, 'url': request.url, 'title': '正在解析…',
        'container': request.container, 'height': request.height,
        'subtitles': request.subtitles, 'status': '排队中',
        'temporary_dir': str(jobs_directory() / job_id),
        'created_at': now, 'updated_at': now,
    }
    store.add_job(item)
    get_queue().submit(job_id)
    return item


def destination_directory(title: str, site: str | None) -> Path:
    root = download_directory()
    if site and get_store().get_bool('site_folders', True):
        root = root / safe_name(site)
    base = root / safe_name(title)
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = base.with_name(f'{base.name} ({suffix})')
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def move_subtitles(source: Path, target: Path, title: str) -> None:
    for item in source.glob('*.srt'):
        language = item.stem.rsplit('.', 1)[-1]
        shutil.move(str(item), target / f'{safe_name(title)}.{language}.srt')


def run_download_job(job_id: str) -> None:
    store = get_store()
    job = store.get_job(job_id)
    if not job or job['status'] not in {'排队中', '准备中'}:
        return
    temp = Path(job['temporary_dir'] or jobs_directory() / job_id)
    if job['cancel_requested']:
        shutil.rmtree(temp, ignore_errors=True)
        store.update_job(job_id, {'status': '已取消', 'speed': 0, 'temporary_dir': None})
        return
    request = DownloadRequest(
        url=job['url'], container=job['container'],
        height=job['height'], subtitles=job['subtitles'],
    )
    last_report = 0.0
    last_check = 0.0

    def check_flags() -> None:
        current = store.get_job(job_id) or {}
        if current.get('cancel_requested'):
            raise DownloadCancelled
        if current.get('status') == '暂停中':
            raise DownloadPaused

    def progress(status: str, downloaded: int, total: int | None, speed: float) -> None:
        nonlocal last_report, last_check
        now = time.monotonic()
        if now - last_check >= 1:
            last_check = now
            check_flags()
        if status == '下载中' and now - last_report < .5:
            return
        last_report = now
        store.update_job(job_id, {'status': status, 'downloaded': downloaded, 'total': total, 'speed': speed})

    try:
        store.update_job(job_id, {'status': '准备中', 'error': None, 'temporary_dir': str(temp)})
        proxy = store.get_proxy()
        parsed = parse_media(request.url, proxy)
        check_flags()
        if not set(request.subtitles).issubset(parsed['subtitles']):
            raise ValueError('所选字幕当前不可用，请重新解析链接')
        store.update_job(job_id, {
            'title': parsed['title'], 'thumbnail': parsed['thumbnail'],
            'site': parsed.get('site') or '', 'status': '下载中',
        })
        if request.container == 'jpg':
            output = download_cover(parsed['thumbnail'], proxy, temp)
        else:
            format_id = select_format(parsed['formats'], request.container, request.height)
            output = download_media(
                request.url, format_id, request.container, proxy, temp,
                progress=progress, subtitles=request.subtitles,
            )
        check_flags()
        if not output.is_file() or output.parent.resolve() != temp.resolve():
            raise RuntimeError('下载未生成目标文件')
        store.update_job(job_id, {'status': '合并中', 'speed': 0})
        target = destination_directory(parsed['title'], parsed.get('site'))
        final = target / f'{safe_name(parsed["title"])}.{request.container}'
        shutil.move(str(output), final)
        if request.subtitles:
            move_subtitles(temp, target, parsed['title'])
        if request.container != 'jpg' and parsed['thumbnail']:
            try:
                download_cover(parsed['thumbnail'], proxy, target, name='cover')
            except Exception:
                pass
        shutil.rmtree(temp, ignore_errors=True)
        size = final.stat().st_size
        store.update_job(job_id, {
            'status': '已完成', 'downloaded': size, 'total': size, 'speed': 0,
            'temporary_dir': None, 'saved_path': str(final), 'error': None,
        })
        store.add_history({
            'url': request.url, 'title': parsed['title'],
            'thumbnail': parsed.get('thumbnail', ''),
            'container': request.container, 'height': request.height,
            'created_at': datetime.now(UTC).isoformat(),
        })
    except DownloadCancelled:
        shutil.rmtree(temp, ignore_errors=True)
        store.update_job(job_id, {'status': '已取消', 'speed': 0, 'temporary_dir': None, 'error': None})
    except DownloadPaused:
        # 保留临时文件，继续时 yt-dlp 会断点续传
        store.update_job(job_id, {'status': '可继续', 'speed': 0, 'error': '已暂停'})
    except Exception as error:
        # 失败同样保留临时文件，便于续传；删除任务时才清理
        store.update_job(job_id, {'status': '可继续', 'speed': 0, 'error': friendly_error(error)})


# ---------- 页面 ----------

def _asset_version() -> str:
    static = ROOT / 'static'
    stamp = max(int(p.stat().st_mtime) for p in static.iterdir() if p.is_file())
    return f'{__version__}-{stamp}'


@app.get('/', include_in_schema=False)
def homepage():
    html = (ROOT / 'static' / 'index.html').read_text('utf-8')
    html = html.replace('?v=dev', '?v=' + _asset_version())
    return HTMLResponse(html, headers={'Cache-Control': 'no-cache'})


@app.get('/manifest.webmanifest', include_in_schema=False)
def manifest():
    return FileResponse(ROOT / 'static' / 'manifest.webmanifest', media_type='application/manifest+json')


@app.get('/api/健康检查')
def health():
    return {'状态': '正常'}


# ---------- 解析与下载 ----------

@app.post('/api/解析')
def parse(request: ParseRequest):
    if not validate_url(request.url):
        raise HTTPException(400, '请输入有效的 HTTP 或 HTTPS 链接')
    try:
        return parse_media(request.url, get_store().get_proxy())
    except Exception as error:
        raise HTTPException(422, parse_error_message(error)) from None


@app.post('/api/下载')
def download(request: DownloadRequest):
    return enqueue_job(request)


@app.get('/api/下载/任务')
def list_jobs():
    return get_store().list_jobs(limit=100)


def _job_or_404(job_id: str) -> dict:
    job = get_store().get_job(job_id)
    if not job:
        raise HTTPException(404, '任务不存在')
    return job


@app.post('/api/下载/任务/{job_id}/继续')
def resume_job(job_id: str):
    job = _job_or_404(job_id)
    if job['status'] not in {'可继续', '已取消'}:
        raise HTTPException(400, '任务当前状态不可继续')
    get_store().update_job(job_id, {
        'status': '排队中', 'error': None, 'cancel_requested': False,
        'temporary_dir': job.get('temporary_dir') or str(jobs_directory() / job_id),
    })
    get_queue().submit(job_id)
    return {'id': job_id}


@app.post('/api/下载/任务/{job_id}/暂停')
def pause_job(job_id: str):
    job = _job_or_404(job_id)
    if job['status'] == '排队中':
        get_store().update_job(job_id, {'status': '可继续', 'error': '已暂停'})
    elif job['status'] in {'准备中', '下载中'}:
        get_store().update_job(job_id, {'status': '暂停中'})
    else:
        raise HTTPException(400, '任务当前状态不可暂停')
    return {'id': job_id}


@app.post('/api/下载/任务/全部暂停')
def pause_all():
    count = 0
    for job in get_store().list_jobs(limit=500):
        if job['status'] in {'排队中', '准备中', '下载中'}:
            pause_job(job['id'])
            count += 1
    return {'数量': count}


@app.post('/api/下载/任务/全部继续')
def resume_all():
    count = 0
    for job in get_store().list_jobs(limit=500):
        if job['status'] == '可继续':
            resume_job(job['id'])
            count += 1
    return {'数量': count}


@app.post('/api/下载/任务/{job_id}/取消')
def cancel_job_route(job_id: str):
    job = _job_or_404(job_id)
    store = get_store()
    if job['status'] in {'排队中', '可继续'}:
        if job.get('temporary_dir'):
            shutil.rmtree(job['temporary_dir'], ignore_errors=True)
        store.update_job(job_id, {'status': '已取消', 'temporary_dir': None, 'error': None})
    elif job['status'] in {'准备中', '下载中', '合并中', '暂停中'}:
        store.update_job(job_id, {'status': '取消中', 'cancel_requested': True})
    else:
        raise HTTPException(400, '任务当前状态不可取消')
    return {'id': job_id}


@app.delete('/api/下载/任务/{job_id}')
def delete_job(job_id: str):
    job = _job_or_404(job_id)
    if job['status'] in ACTIVE - {'排队中'}:
        raise HTTPException(400, '请先取消正在进行的任务')
    if job.get('temporary_dir'):
        shutil.rmtree(job['temporary_dir'], ignore_errors=True)
    get_store().delete_job(job_id)
    return {'id': job_id}


@app.delete('/api/下载/任务')
def clear_finished_jobs():
    return {'数量': get_store().clear_jobs(('已完成', '已取消'))}


# ---------- 历史 ----------

@app.get('/api/历史')
def list_history():
    return get_store().list_history()


@app.delete('/api/历史')
def clear_history():
    get_store().delete_history(None)
    return {'ok': True}


@app.delete('/api/历史/{item_id}')
def delete_history_item(item_id: int):
    get_store().delete_history(item_id)
    return {'id': item_id}


# ---------- 媒体库 ----------

def _resolve_media(rel: str) -> Path:
    root = download_directory().resolve()
    path = (root / rel).resolve()
    if root != path and root not in path.parents:
        raise HTTPException(400, '路径无效')
    if not path.exists():
        raise HTTPException(404, '文件不存在')
    return path


def _kind(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in VIDEO_EXT:
        return 'video'
    if ext in AUDIO_EXT:
        return 'audio'
    if ext in IMAGE_EXT and path.stem != 'cover':
        return 'image'
    return None


@app.get('/api/媒体库')
def library(q: str = ''):
    root = download_directory()
    items = []
    if root.exists():
        for path in root.rglob('*'):
            if not path.is_file() or path.name.startswith('.'):
                continue
            kind = _kind(path)
            if not kind:
                continue
            if q and q.lower() not in path.name.lower():
                continue
            stat = path.stat()
            cover = next((c for c in (path.parent / 'cover.jpg', path.parent / 'cover.webp') if c.exists()), None)
            rel = path.relative_to(root).as_posix()
            items.append({
                'path': rel, 'name': path.stem, 'ext': path.suffix.lower().lstrip('.'),
                'kind': kind, 'size': stat.st_size, 'mtime': stat.st_mtime,
                'folder': path.parent.relative_to(root).as_posix(),
                'cover': cover.relative_to(root).as_posix() if cover else (rel if kind == 'image' else None),
            })
    items.sort(key=lambda item: item['mtime'], reverse=True)
    return items[:500]


@app.get('/api/媒体库/文件')
def library_file(path: str, download: bool = False):
    target = _resolve_media(path)
    if not target.is_file():
        raise HTTPException(400, '不是文件')
    return FileResponse(target, filename=target.name if download else None)


@app.delete('/api/媒体库/文件')
def library_delete(path: str):
    target = _resolve_media(path)
    root = download_directory().resolve()
    folder = target.parent
    target.unlink()
    # 目录里只剩封面/字幕时一并清理
    if folder != root and not any(_kind(p) for p in folder.iterdir()):
        shutil.rmtree(folder, ignore_errors=True)
    return {'ok': True}


# ---------- 系统 ----------

@app.get('/api/系统')
def system_info():
    store = get_store()
    return {
        '版本': __version__,
        'python': sys.version.split()[0],
        'yt_dlp': ytdlp_version(),
        'ffmpeg': ffmpeg_version(),
        '下载目录': str(download_directory()),
        '存储': storage_info(download_directory()),
        '代理': bool(store.get_proxy()),
        'cookies': cookies_file().exists() and cookies_file().stat().st_size > 0,
        '并发': store.get_int('concurrency', 2),
        '按站点分类': store.get_bool('site_folders', True),
    }


@app.put('/api/设置')
def update_settings(request: SettingsRequest):
    store = get_store()
    if request.concurrency is not None:
        if not 1 <= request.concurrency <= 6:
            raise HTTPException(400, '同时下载数需在 1 到 6 之间')
        store.set_setting('concurrency', str(request.concurrency))
        get_queue().resize(request.concurrency)
    if request.site_folders is not None:
        store.set_setting('site_folders', '1' if request.site_folders else '0')
    return system_info()


@app.post('/api/系统/更新解析器')
def update_parser():
    try:
        before, after = update_ytdlp(data_dir())
    except Exception as error:
        raise HTTPException(500, f'更新失败：{friendly_error(error)}') from None
    return {'更新前': before, '更新后': after, '需要重启': before != after}


@app.post('/api/系统/重启')
def restart():
    import threading

    def later():
        time.sleep(.5)
        os.execv(sys.executable, [sys.executable, '-m', 'uvicorn', *sys.argv[1:]])

    threading.Thread(target=later, daemon=True).start()
    return {'ok': True}


# ---------- 代理 ----------

@app.get('/api/代理')
def get_proxy():
    address = get_store().get_proxy_display()
    return {'已设置': bool(address), '地址': address or ''}


@app.put('/api/代理')
def set_proxy(request: ProxyRequest):
    address = (request.地址 or '').strip()
    if address and not valid_proxy(address):
        raise HTTPException(400, '代理地址格式无效')
    store = get_store()
    store.set_proxy(address or None)
    display = store.get_proxy_display()
    return {'已设置': bool(display), '地址': display or ''}


@app.delete('/api/代理')
def delete_proxy():
    get_store().set_proxy(None)
    return {'已设置': False, '地址': ''}


@app.post('/api/代理/自动检测')
def auto_detect_proxy():
    for candidate in proxy_candidates():
        if proxy_works(candidate):
            get_store().set_proxy(candidate)
            return {'已设置': True, '地址': candidate, '消息': f'已自动设置代理：{candidate}'}
    return {'已设置': False, '地址': '', '消息': '未检测到可用代理'}


@app.post('/api/代理/测试')
def test_proxy():
    proxy = get_store().get_proxy()
    if not proxy:
        return {'可用': False, '消息': '尚未设置代理'}
    started = time.monotonic()
    ok = proxy_works(proxy, use_cache=False)
    return {'可用': ok, '延迟': round((time.monotonic() - started) * 1000), '消息': '代理可用' if ok else '代理不可用'}


# ---------- Cookies ----------

def cookie_sites(path: Path) -> list[str]:
    sites = set()
    for line in path.read_text('utf-8', errors='replace').splitlines():
        parts = line.split('\t')
        if len(parts) >= 7 and not line.startswith('#'):
            domain = parts[0].lstrip('.').removeprefix('www.')
            sites.add('.'.join(domain.split('.')[-2:]))
    return sorted(sites)


@app.get('/api/Cookies')
def get_cookies_status():
    path = cookies_file()
    ok = path.exists() and path.stat().st_size > 0
    return {'已设置': ok, '站点': cookie_sites(path) if ok else []}


@app.put('/api/Cookies')
async def import_cookies(req: Request):
    content = await req.body()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(413, 'Cookies 文件过大')
    try:
        validate_cookie_file(content)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    path = cookies_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        content = merge_cookie_files(path.read_bytes(), content)
    path.write_bytes(content)
    path.chmod(0o600)
    return get_cookies_status()


@app.delete('/api/Cookies')
def delete_cookies():
    cookies_file().unlink(missing_ok=True)
    return {'已设置': False, '站点': []}
