import os
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.responses import FileResponse

from app.media import (
    download_media,
    merge_cookie_files,
    parse_media,
    proxy_candidates,
    proxy_works,
    select_format,
    validate_cookie_file,
    validate_url,
)
from app.store import Store


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.store = _make_store()
    application.state.store.recover_interrupted_jobs()
    yield


app = FastAPI(docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')
app.mount('/assets', StaticFiles(directory=Path(__file__).parents[1] / 'assets'), name='assets')


def _make_store() -> Store:
    return Store(Path(os.environ.get('DATA_DIR', '/data')) / 'app.db')


def get_store() -> Store:
    try:
        return app.state.store
    except AttributeError:
        return _make_store()


def download_directory() -> Path:
    return Path(os.environ.get('DOWNLOAD_DIR', '/downloads'))


def jobs_directory() -> Path:
    return Path(os.environ.get('DATA_DIR', '/data')) / 'jobs'


def cookies_file() -> Path:
    return Path(os.environ.get('DATA_DIR', '/data')) / 'cookies.txt'


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
    return '解析失败，请检查链接、权限或代理设置'


class ParseRequest(BaseModel):
    url: str


class ProxyRequest(BaseModel):
    地址: str | None = None


class DownloadRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str
    container: str
    height: int | None = None
    subtitles: list[str] = []


class DownloadCancelled(Exception):
    pass


def validate_download_request(request: DownloadRequest) -> None:
    if not validate_url(request.url):
        raise HTTPException(400, '请输入有效的 HTTP 或 HTTPS 链接')
    if request.container not in {'mp4', 'mkv', 'webm', 'mp3'}:
        raise HTTPException(400, '请选择支持的文件格式')
    if request.container == 'mp3':
        if request.height is not None:
            raise HTTPException(400, 'MP3 下载不应指定清晰度')
    elif request.height is None or request.height <= 0:
        raise HTTPException(400, '请选择可用清晰度')


def enqueue_job(request: DownloadRequest, background_tasks: BackgroundTasks) -> dict:
    validate_download_request(request)
    store = get_store()
    job_id = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    item = {
        'id': job_id, 'url': request.url, 'title': '正在解析…',
        'container': request.container, 'height': request.height,
        'subtitles': request.subtitles, 'status': '准备中',
        'temporary_dir': str(jobs_directory() / job_id),
        'created_at': now, 'updated_at': now,
    }
    store.add_job(item)
    background_tasks.add_task(run_download_job, job_id)
    return item


def destination_directory(title: str) -> Path:
    base = download_directory() / safe_name(title)
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
        destination = target / f'{safe_name(title)}.{language}.srt'
        shutil.move(str(item), destination)


def cancel_job(store: Store, job_id: str, temporary_dir: Path) -> None:
    shutil.rmtree(temporary_dir, ignore_errors=True)
    store.update_job(job_id, {'status': '已取消', 'speed': 0, 'temporary_dir': None, 'error': None})


def run_download_job(job_id: str) -> None:
    store = get_store()
    job = store.get_job(job_id)
    if not job:
        return
    if job['cancel_requested']:
        cancel_job(store, job_id, Path(job['temporary_dir']))
        return
    request = DownloadRequest(
        url=job['url'], container=job['container'],
        height=job['height'], subtitles=job['subtitles'],
    )
    temp = Path(job['temporary_dir'])
    previous_report = 0.0

    def progress(status: str, downloaded: int, total: int | None, speed: float) -> None:
        nonlocal previous_report
        if store.get_job(job_id)['cancel_requested']:
            raise DownloadCancelled
        now = time.monotonic()
        if status == '下载中' and now - previous_report < .5:
            return
        previous_report = now
        store.update_job(job_id, {'status': status, 'downloaded': downloaded, 'total': total, 'speed': speed})

    try:
        store.update_job(job_id, {'status': '准备中', 'error': None})
        proxy = store.get_proxy()
        parsed = parse_media(request.url, proxy)
        if store.get_job(job_id)['cancel_requested']:
            raise DownloadCancelled
        if not set(request.subtitles).issubset(parsed['subtitles']):
            raise ValueError('所选字幕当前不可用，请重新解析链接')
        format_id = select_format(parsed['formats'], request.container, request.height)
        store.update_job(job_id, {'title': parsed['title'], 'thumbnail': parsed['thumbnail'], 'status': '下载中'})
        output = download_media(
            request.url, format_id, request.container, proxy, temp,
            progress=progress, subtitles=request.subtitles,
        )
        if store.get_job(job_id)['cancel_requested']:
            raise DownloadCancelled
        if not output.is_file() or output.parent.resolve() != temp.resolve():
            raise RuntimeError('下载未生成目标文件')
        store.update_job(job_id, {'status': '合并中', 'speed': 0})
        target = destination_directory(parsed['title'])
        final = target / f'{safe_name(parsed["title"])}.{request.container}'
        shutil.move(str(output), final)
        if request.subtitles:
            move_subtitles(temp, target, parsed['title'])
        shutil.rmtree(temp, ignore_errors=True)
        store.update_job(job_id, {
            'status': '已完成', 'downloaded': final.stat().st_size,
            'total': final.stat().st_size, 'speed': 0, 'temporary_dir': None,
            'saved_path': f'/vol1/1000/下载/视频下载/{target.name}', 'error': None,
        })
        store.add_history({
            'url': request.url, 'title': parsed['title'],
            'thumbnail': parsed.get('thumbnail', ''),
            'container': request.container, 'height': request.height,
            'created_at': datetime.now(UTC).isoformat(),
        })
    except DownloadCancelled:
        cancel_job(store, job_id, temp)
    except Exception as error:
        shutil.rmtree(temp, ignore_errors=True)
        store.update_job(job_id, {'status': '可继续', 'speed': 0, 'error': str(error)})


@app.get('/api/健康检查')
def health():
    return {'状态': '正常'}


@app.get('/', include_in_schema=False)
def homepage():
    return FileResponse(Path(__file__).parent / 'static' / 'index.html')


@app.post('/api/解析')
def parse(request: ParseRequest):
    if not validate_url(request.url):
        raise HTTPException(400, '请输入有效的 HTTP 或 HTTPS 链接')
    try:
        return parse_media(request.url, get_store().get_proxy())
    except Exception as error:
        raise HTTPException(422, parse_error_message(error)) from None


@app.post('/api/下载')
def download(request: DownloadRequest, background_tasks: BackgroundTasks):
    return enqueue_job(request, background_tasks)


@app.get('/api/下载/任务')
def list_jobs():
    return get_store().list_jobs()


@app.post('/api/下载/任务/{job_id}/继续')
def resume_job(job_id: str, background_tasks: BackgroundTasks):
    store = get_store()
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, '任务不存在')
    if job['status'] != '可继续':
        raise HTTPException(400, '任务当前状态不可继续')
    store.update_job(job_id, {'status': '准备中', 'error': None, 'cancel_requested': False})
    background_tasks.add_task(run_download_job, job_id)
    return {'id': job_id}


@app.post('/api/下载/任务/{job_id}/取消')
def cancel_job_route(job_id: str):
    store = get_store()
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, '任务不存在')
    if job['status'] not in {'准备中', '下载中', '合并中'}:
        raise HTTPException(400, '任务当前状态不可取消')
    store.update_job(job_id, {'status': '取消中', 'cancel_requested': True})
    return {'id': job_id}


@app.delete('/api/下载/任务/{job_id}')
def delete_job(job_id: str):
    store = get_store()
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, '任务不存在')
    if job['status'] == '可继续' and job.get('temporary_dir'):
        shutil.rmtree(job['temporary_dir'], ignore_errors=True)
    store.delete_job(job_id)
    return {'id': job_id}


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


@app.get('/api/代理')
def get_proxy():
    store = get_store()
    address = store.get_proxy_display()
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


@app.get('/api/Cookies')
def get_cookies_status():
    path = cookies_file()
    return {'已设置': path.exists() and path.stat().st_size > 0}


@app.put('/api/Cookies')
async def import_cookies(req: Request):
    content = await req.body()
    try:
        validate_cookie_file(content)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    path = cookies_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        content = merge_cookie_files(path.read_bytes(), content)
    path.write_bytes(content)
    return {'已设置': True}


@app.delete('/api/Cookies')
def delete_cookies():
    cookies_file().unlink(missing_ok=True)
    return {'已设置': False}
