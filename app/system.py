import importlib
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path


def ytdlp_version() -> str:
    try:
        import yt_dlp.version
        return yt_dlp.version.__version__
    except Exception:
        return '未知'


@lru_cache(maxsize=1)
def ffmpeg_version() -> str:
    binary = shutil.which('ffmpeg')
    if not binary:
        return '未安装'
    try:
        out = subprocess.run([binary, '-version'], capture_output=True, text=True, timeout=5).stdout
        return out.split()[2] if out.startswith('ffmpeg version') else '已安装'
    except Exception:
        return '已安装'


def storage_info(path: Path) -> dict:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return {'总量': 0, '已用': 0, '可用': 0}
    return {'总量': usage.total, '已用': usage.used, '可用': usage.free}


def update_ytdlp(data_dir: Path) -> tuple[str, str]:
    """把最新 yt-dlp 装进数据目录下的 pylib-override，启动时优先加载，升级应用也不会丢。"""
    before = ytdlp_version()
    target = data_dir / 'pylib-override'
    target.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, '-m', 'pip', 'install', '-q', '--upgrade', '--no-deps',
         '--disable-pip-version-check', '--target', str(target), 'yt-dlp'],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr else 'pip 失败')
    version_file = next(target.glob('yt_dlp/version.py'), None)
    after = before
    if version_file:
        for line in version_file.read_text().splitlines():
            if line.startswith('__version__'):
                after = line.split('=', 1)[1].strip().strip('\'"')
    importlib.invalidate_caches()
    return before, after
