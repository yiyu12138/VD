import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import yt_dlp

DIRECT_FALLBACK_DOMAINS: frozenset[str] = frozenset({
    'bilibili.com',
    'douyin.com',
})

QUALITY_KEYS: tuple[str, ...] = ('tbr', 'vbr', 'abr', 'filesize', 'filesize_approx')

# 各容器的首选编码（顺序即优先级）：mp4 优先 H.264 —— YouTube 的 1080p 里
# 有 VP9 封进 mp4 的流（itag 613/614/615），码率往往最高，被选中后虽然桌面
# Chrome 能放，但 iPhone/iPad、Safari、Firefox 和多数电视/播放器都放不了。
CODEC_FAMILIES: dict[str, str] = {
    'avc1': 'h264', 'avc3': 'h264', 'h264': 'h264',
    'hev1': 'hevc', 'hvc1': 'hevc', 'h265': 'hevc',
    'av01': 'av1',
    'vp09': 'vp9', 'vp9': 'vp9', 'vp08': 'vp8', 'vp8': 'vp8',
    'mp4a': 'aac', 'aac': 'aac', 'mp3': 'mp3',
    'opus': 'opus', 'vorbis': 'vorbis', 'flac': 'flac', 'ac-3': 'ac3', 'ec-3': 'eac3',
}
VIDEO_CODECS: dict[str, tuple[str, ...]] = {
    'mp4': ('h264', 'hevc', 'av1'),
    'webm': ('vp9', 'vp8', 'av1'),
    'mov': ('h264', 'hevc'),
}
AUDIO_CODECS: dict[str, tuple[str, ...]] = {
    'mp4': ('aac', 'mp3', 'ac3', 'eac3'),
    'webm': ('opus', 'vorbis'),
    'mov': ('aac', 'mp3', 'ac3', 'eac3'),
}
AUDIO_EXT: dict[str, set[str]] = {
    'mp4': {'m4a', 'mp4', 'aac'},
    'mov': {'m4a', 'mp4', 'aac'},
}


def codec_family(value: object) -> str:
    """把 yt-dlp 的 vcodec/acodec（如 avc1.640028、vp09.00.10.08）归成编码族名。"""
    return CODEC_FAMILIES.get(str(value or '').strip().lower().split('.')[0], str(value or '').strip().lower().split('.')[0])

class StopDownload(Exception):
    """用户主动取消/暂停，不应触发直连重试。"""


_proxy_cache: dict[str, tuple[bool, float]] = {}
_PROXY_CACHE_TTL = 30.0


def ydl_options() -> dict:
    return {
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': False,
        'noplaylist': True,
        'extract_flat': False,
    }


def cookie_file() -> Path | None:
    path = Path(os.environ.get('DATA_DIR', '/data')) / 'cookies.txt'
    return path if path.exists() and path.stat().st_size > 0 else None


def validate_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in {'http', 'https'} and bool(parts.hostname)


def validate_cookie_file(content: bytes) -> None:
    text = content.decode('utf-8', errors='replace')
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith('#')]
    if not lines:
        raise ValueError('文件不包含有效的 Cookie 条目')
    for line in lines[:10]:
        if len(line.split('\t')) < 7:
            raise ValueError('文件格式不符合 Netscape cookies.txt 标准')


def merge_cookie_files(existing: bytes, incoming: bytes) -> bytes:
    def parse_entries(raw: bytes) -> dict[tuple, str]:
        entries: dict[tuple, str] = {}
        for line in raw.decode('utf-8', errors='replace').splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            parts = stripped.split('\t')
            if len(parts) >= 7:
                key = (parts[0], parts[2], parts[5])
                entries[key] = line
        return entries

    merged = parse_entries(existing)
    merged.update(parse_entries(incoming))
    header = '# Netscape HTTP Cookie File\n'
    return (header + '\n'.join(merged.values()) + '\n').encode('utf-8')


def needs_direct_fallback(url: str, proxy: str | None) -> bool:
    if not proxy:
        return False
    host = (urlsplit(url).hostname or '').lower()
    return any(host == domain or host.endswith('.' + domain) for domain in DIRECT_FALLBACK_DOMAINS)


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    if (parts.hostname or '').endswith('douyin.com') and parts.path.rstrip('/') == '/jingxuan':
        video_id = parse_qs(parts.query).get('modal_id', [''])[0]
        if video_id.isdigit():
            return f'https://www.douyin.com/video/{video_id}'
    return url


def extract_info(url: str, proxy: str | None, download: bool) -> dict:
    options = ydl_options()
    if cookies := cookie_file():
        options['cookiefile'] = str(cookies)
    if proxy:
        options['proxy'] = proxy
    with yt_dlp.YoutubeDL(options) as downloader:
        return downloader.extract_info(url, download=download)


def parse_media(url: str, proxy: str | None) -> dict:
    url = normalize_url(url)
    try:
        info = extract_info(url, proxy, download=False)
    except Exception:
        if not needs_direct_fallback(url, proxy):
            raise
        info = extract_info(url, None, download=False)

    formats = []
    for item in info.get('formats', []):
        has_video = item.get('vcodec') not in {None, 'none'}
        has_audio = item.get('acodec') not in {None, 'none'}
        if not item.get('format_id') or not (has_video or has_audio):
            continue
        parsed = {
            'id': str(item['format_id']),
            'height': item.get('height') if has_video else None,
            'ext': str(item.get('ext') or '').lower(),
            'has_video': has_video,
            'has_audio': has_audio,
            'vcodec': item.get('vcodec'),
            'acodec': item.get('acodec'),
        }
        parsed.update({key: item.get(key) for key in QUALITY_KEYS})
        formats.append(parsed)

    subtitles = sorted({
        language
        for source in (info.get('subtitles', {}), info.get('automatic_captions', {}))
        for language, tracks in source.items()
        if tracks
    })
    return {
        'title': info.get('title') or '未命名视频',
        'site': info.get('extractor_key') or info.get('extractor') or '',
        'uploader': info.get('uploader') or info.get('channel') or '',
        'view_count': info.get('view_count'),
        'upload_date': info.get('upload_date') or '',
        'thumbnail': info.get('thumbnail') or '',
        'duration': info.get('duration'),
        'formats': formats,
        'subtitles': subtitles,
    }


def select_format(formats: list[dict], container: str, height: int | None) -> str:
    def quality(item: dict) -> tuple:
        return tuple(item.get(key) or 0 for key in QUALITY_KEYS)

    def compatible(item: dict) -> bool:
        if container == 'mkv':
            return True
        if item.get('ext') == container:
            return True
        # 有些站点的 m4a 音频 ext 标成 mp4，mp4 容器下当作同族
        return container in AUDIO_EXT and item.get('ext') in AUDIO_EXT[container]

    def codec_of(item: dict, kind: str) -> str:
        return codec_family(item.get('vcodec') if kind == 'video' else item.get('acodec'))

    def codec_ok(item: dict, kind: str) -> bool:
        table = VIDEO_CODECS if kind == 'video' else AUDIO_CODECS
        if container == 'mkv' or container not in table:
            return True
        family = codec_of(item, kind)
        return not family or family in table[container]

    def rank(item: dict, kind: str) -> tuple:
        """同清晰度下先看编码通用性（mp4 优先 H.264），再看码率。"""
        table = VIDEO_CODECS if kind == 'video' else AUDIO_CODECS
        priority = 0
        if container != 'mkv' and container in table:
            family = codec_of(item, kind)
            if family in table[container]:
                priority = len(table[container]) - table[container].index(family)
        return (priority, quality(item))

    if container in {'mp3', 'm4a'}:
        audio = (
            [item for item in formats if item.get('has_audio') and not item.get('has_video')]
            or [item for item in formats if item.get('has_audio')]
        )
        if not audio:
            raise ValueError('当前没有可用音频')
        return max(audio, key=quality)['id']

    video = [
        item for item in formats
        if item.get('has_video') and item.get('height') == height and compatible(item)
    ]
    if not video:
        raise ValueError('所选清晰度当前不可用')
    friendly = [item for item in video if codec_ok(item, 'video')] or video
    selected_video = max(friendly, key=lambda item: rank(item, 'video'))
    if selected_video.get('has_audio'):
        return selected_video['id']

    audio = [
        item for item in formats
        if item.get('has_audio') and not item.get('has_video') and compatible(item)
    ]
    if not audio:
        raise ValueError('当前没有可用音频')
    friendly_audio = [item for item in audio if codec_ok(item, 'audio')] or audio
    return f"{selected_video['id']}+{max(friendly_audio, key=lambda item: rank(item, 'audio'))['id']}"


def has_audio_track(path: Path) -> bool:
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
         'stream=codec_type', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
        check=False, capture_output=True, text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == 'audio'


def download_media(
    url: str,
    format_id: str,
    container: str,
    proxy: str | None,
    target: Path,
    progress: Callable[[str, int, int | None, float], None] | None = None,
    subtitles: list[str] | None = None,
) -> Path:
    url = normalize_url(url)
    target.mkdir(parents=True, exist_ok=True)
    audio_only = container in {'mp3', 'm4a'}

    def download_once(active_proxy: str | None) -> None:
        options = {
            **ydl_options(), 'format': format_id,
            'outtmpl': str(target / '%(title).200B.%(ext)s'),
            'merge_output_format': container, 'continuedl': True,
        }
        if cookies := cookie_file():
            options['cookiefile'] = str(cookies)
        if active_proxy:
            options['proxy'] = active_proxy
        postprocessors = [{
            'key': 'FFmpegExtractAudio' if audio_only else 'FFmpegVideoRemuxer',
            **({'preferredcodec': container} if audio_only else {'preferedformat': container}),
        }]
        if subtitles:
            options.update({
                'writesubtitles': True, 'writeautomaticsub': True,
                'subtitleslangs': subtitles, 'subtitlesformat': 'srt/best',
            })
            postprocessors.append({'key': 'FFmpegSubtitlesConvertor', 'format': 'srt'})
        options['postprocessors'] = postprocessors
        if progress:
            def report(item: dict) -> None:
                state = item.get('status')
                downloaded = int(item.get('downloaded_bytes') or 0)
                total = item.get('total_bytes') or item.get('total_bytes_estimate')
                total = int(total) if total else None
                speed = float(item.get('speed') or 0)
                if state == 'downloading':
                    progress('下载中', downloaded, total, speed)
                elif state == 'finished':
                    progress('合并中', total or downloaded, total or downloaded, 0)
            options['progress_hooks'] = [report]
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.extract_info(url, download=True)

    try:
        download_once(proxy)
    except StopDownload:
        raise
    except Exception as error:
        if isinstance(error.__context__, StopDownload) or not needs_direct_fallback(url, proxy):
            raise
        download_once(None)

    outputs = [
        item for item in target.iterdir()
        if item.is_file() and item.suffix.lower() == f'.{container}'
    ]
    if not outputs:
        raise RuntimeError('下载未生成目标文件')
    outputs.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    if audio_only:
        return outputs[0]
    output = next((item for item in outputs if has_audio_track(item)), None)
    if not output:
        raise RuntimeError('音视频合并未完成，未生成包含声音的文件')
    return output


SUBTITLE_MODES = ('both', 'embed', 'external')

# 常见语言代码 → ISO 639-2（mp4/mkv 字幕轨的语言标签）
_LANG3 = {
    'zh': 'chi', 'zh-hans': 'chi', 'zh-hant': 'chi', 'zh-cn': 'chi', 'zh-tw': 'chi', 'zh-hk': 'chi',
    'en': 'eng', 'ja': 'jpn', 'ko': 'kor', 'fr': 'fre', 'de': 'ger', 'es': 'spa', 'ru': 'rus',
    'pt': 'por', 'it': 'ita', 'ar': 'ara', 'th': 'tha', 'vi': 'vie', 'id': 'ind', 'hi': 'hin',
}


def _lang3(code: str) -> str:
    c = code.lower()
    return _LANG3.get(c) or _LANG3.get(c.split('-')[0]) or 'und'


def embed_subtitles(video: Path, subtitles: list[tuple[Path, str]]) -> bool:
    """把字幕作为软字幕轨封装进视频（不重新编码，几秒完成）。

    mp4 用 mov_text，mkv 直接放 srt；webm 只支持 webvtt。成功返回 True，失败保留原文件返回 False。
    """
    if not subtitles:
        return False
    ext = video.suffix.lower()
    codec = {'.mp4': 'mov_text', '.m4v': 'mov_text', '.mov': 'mov_text', '.mkv': 'srt', '.webm': 'webvtt'}.get(ext)
    if not codec:
        return False
    tmp = video.with_name(video.stem + '.subs-tmp' + ext)
    cmd = ['ffmpeg', '-y', '-v', 'error', '-i', str(video)]
    for sub, _ in subtitles:
        cmd += ['-i', str(sub)]
    cmd += ['-map', '0:v?', '-map', '0:a?']
    for i in range(len(subtitles)):
        cmd += ['-map', f'{i + 1}:0']
    cmd += ['-c:v', 'copy', '-c:a', 'copy', '-c:s', codec]
    for i, (_, lang) in enumerate(subtitles):
        cmd += [f'-metadata:s:s:{i}', f'language={_lang3(lang)}', f'-metadata:s:s:{i}', f'title={lang}']
    cmd += ['-disposition:s:0', 'default']
    if ext in ('.mp4', '.m4v', '.mov'):
        cmd += ['-movflags', '+faststart']
    cmd.append(str(tmp))
    result = subprocess.run(cmd, capture_output=True, check=False, timeout=1800)
    if result.returncode != 0 or not tmp.exists() or tmp.stat().st_size < video.stat().st_size * 0.9:
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(video)
    return True


def proxy_candidates() -> list[str]:
    return [
        'http://127.0.0.1:20171',
        'socks5://127.0.0.1:20170',
        'http://127.0.0.1:20172',
    ] + [
        f'{scheme}://127.0.0.1:{port}'
        for port in (7890, 7891, 1080, 10808, 2080)
        for scheme in ('http', 'socks5')
    ]


def proxy_works(proxy: str, use_cache: bool = True) -> bool:
    now = time.monotonic()
    if use_cache and proxy in _proxy_cache:
        result, expires_at = _proxy_cache[proxy]
        if now < expires_at:
            return result

    options = {
        'proxy': proxy,
        'quiet': True,
        'no_warnings': True,
        'ignoreconfig': True,
        'socket_timeout': 3,
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            response = downloader.urlopen(Request('https://www.youtube.com/generate_204'))
            response.read(1)
        result = True
    except Exception:
        result = False

    _proxy_cache[proxy] = (result, now + _PROXY_CACHE_TTL)
    return result


def download_cover(url: str, proxy: str | None, target: Path, name: str = 'cover') -> Path:
    """下载封面并统一转成 JPG。"""
    if not url:
        raise ValueError('该视频没有封面')
    target.mkdir(parents=True, exist_ok=True)
    options = {'quiet': True, 'no_warnings': True, 'socket_timeout': 15}
    if proxy:
        options['proxy'] = proxy
    raw = target / f'{name}.src'
    with yt_dlp.YoutubeDL(options) as downloader:
        raw.write_bytes(downloader.urlopen(Request(url)).read())
    output = target / f'{name}.jpg'
    result = subprocess.run(
        ['ffmpeg', '-y', '-loglevel', 'error', '-i', str(raw), '-frames:v', '1', '-q:v', '2', str(output)],
        capture_output=True, check=False,
    )
    if result.returncode != 0 or not output.exists():
        raw.replace(output)
    else:
        raw.unlink(missing_ok=True)
    return output
