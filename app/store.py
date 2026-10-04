import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                'CREATE TABLE IF NOT EXISTS settings ('
                'key TEXT PRIMARY KEY, value TEXT)'
            )
            connection.execute(
                'CREATE TABLE IF NOT EXISTS history ('
                'id INTEGER PRIMARY KEY, url TEXT, title TEXT, thumbnail TEXT, '
                'container TEXT, height INTEGER, created_at TEXT)'
            )
            connection.execute(
                'CREATE TABLE IF NOT EXISTS jobs ('
                'id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL, '
                "thumbnail TEXT NOT NULL DEFAULT '', container TEXT NOT NULL, "
                "height INTEGER, subtitles TEXT NOT NULL DEFAULT '[]', "
                'status TEXT NOT NULL, downloaded INTEGER NOT NULL DEFAULT 0, '
                'total INTEGER, speed REAL NOT NULL DEFAULT 0, temporary_dir TEXT, '
                'saved_path TEXT, error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0, '
                'created_at TEXT NOT NULL, updated_at TEXT NOT NULL)'
            )
            columns = {row[1] for row in connection.execute('PRAGMA table_info(jobs)')}
            if 'site' not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN site TEXT NOT NULL DEFAULT ''")
            if 'cancel_requested' not in columns:
                connection.execute(
                    'ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0'
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5, check_same_thread=False)

    def get_proxy(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = 'proxy'"
            ).fetchone()
        return row[0] if row else None

    def get_proxy_display(self) -> str | None:
        value = self.get_proxy()
        if not value:
            return None
        parts = urlsplit(value)
        if '@' not in parts.netloc:
            return value
        host = parts.hostname or ''
        try:
            port = f':{parts.port}' if parts.port else ''
        except ValueError:
            port = ''
        username = parts.username or ''
        userinfo = f'{username}:***' if username else '***'
        return f'{parts.scheme}://{userinfo}@{host}{port}{parts.path}'

    def set_proxy(self, value: str | None) -> None:
        with self._connect() as connection:
            if value:
                connection.execute(
                    "INSERT INTO settings(key, value) VALUES ('proxy', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (value,),
                )
            else:
                connection.execute("DELETE FROM settings WHERE key = 'proxy'")

    def add_history(self, item: dict) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                'INSERT INTO history(url, title, thumbnail, container, height, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (item['url'], item['title'], item['thumbnail'], item['container'],
                 item['height'], item['created_at']),
            )
        return cursor.lastrowid

    def list_history(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT id, url, title, thumbnail, container, height, created_at '
                'FROM history ORDER BY created_at DESC, id DESC'
            ).fetchall()
        fields = ('id', 'url', 'title', 'thumbnail', 'container', 'height', 'created_at')
        return [dict(zip(fields, row)) for row in rows]

    def delete_history(self, item_id: int | None) -> None:
        with self._connect() as connection:
            if item_id is None:
                connection.execute('DELETE FROM history')
            else:
                connection.execute('DELETE FROM history WHERE id = ?', (item_id,))

    def add_job(self, item: dict) -> None:
        with self._connect() as connection:
            connection.execute(
                'INSERT INTO jobs(id, url, title, thumbnail, container, height, subtitles, '
                'status, downloaded, total, speed, temporary_dir, saved_path, error, '
                'cancel_requested, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    item['id'], item['url'], item['title'], item.get('thumbnail', ''),
                    item['container'], item.get('height'), json.dumps(item.get('subtitles', [])),
                    item['status'], item.get('downloaded', 0), item.get('total'),
                    item.get('speed', 0), item.get('temporary_dir'), item.get('saved_path'),
                    item.get('error'), int(bool(item.get('cancel_requested'))), item['created_at'],
                    item.get('updated_at', item['created_at']),
                ),
            )

    def update_job(self, job_id: str, values: dict) -> None:
        allowed = {
            'title', 'thumbnail', 'status', 'downloaded', 'total', 'speed',
            'temporary_dir', 'saved_path', 'error', 'subtitles', 'cancel_requested', 'site',
        }
        values = {key: value for key, value in values.items() if key in allowed}
        if not values:
            return
        if 'subtitles' in values:
            values['subtitles'] = json.dumps(values['subtitles'])
        if 'cancel_requested' in values:
            values['cancel_requested'] = int(bool(values['cancel_requested']))
        values['updated_at'] = datetime.now(UTC).isoformat()
        fields = ', '.join(f'{key} = ?' for key in values)
        with self._connect() as connection:
            connection.execute(
                f'UPDATE jobs SET {fields} WHERE id = ?',
                (*values.values(), job_id),
            )

    @staticmethod
    def _job(row: tuple) -> dict:
        fields = (
            'id', 'url', 'title', 'thumbnail', 'container', 'height', 'subtitles',
            'status', 'downloaded', 'total', 'speed', 'temporary_dir', 'saved_path',
            'error', 'cancel_requested', 'created_at', 'updated_at', 'site',
        )
        item = dict(zip(fields, row))
        item['subtitles'] = json.loads(item['subtitles'])
        item['cancel_requested'] = bool(item['cancel_requested'])
        return item

    def get_job(self, job_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT id, url, title, thumbnail, container, height, subtitles, status, '
                'downloaded, total, speed, temporary_dir, saved_path, error, cancel_requested, created_at, updated_at, site '
                'FROM jobs WHERE id = ?', (job_id,),
            ).fetchone()
        return self._job(row) if row else None

    def list_jobs(self, status: str | None = None, limit: int = 100) -> list[dict]:
        where = 'WHERE status = ? ' if status else ''
        params = ((status,) if status else ()) + (limit,)
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT id, url, title, thumbnail, container, height, subtitles, status, '
                'downloaded, total, speed, temporary_dir, saved_path, error, cancel_requested, created_at, updated_at, site '
                f'FROM jobs {where}ORDER BY created_at DESC LIMIT ?', params
            ).fetchall()
        return [self._job(row) for row in rows]

    def clear_jobs(self, statuses: tuple[str, ...]) -> int:
        marks = ','.join('?' * len(statuses))
        with self._connect() as connection:
            return connection.execute(f'DELETE FROM jobs WHERE status IN ({marks})', statuses).rowcount

    def get_setting(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                'INSERT INTO settings(key, value) VALUES (?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))

    def get_int(self, key: str, default: int) -> int:
        try:
            return int(self.get_setting(key) or default)
        except ValueError:
            return default

    def get_bool(self, key: str, default: bool) -> bool:
        value = self.get_setting(key)
        return default if value is None else value == '1'

    def delete_job(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute('DELETE FROM jobs WHERE id = ?', (job_id,))

    def recover_interrupted_jobs(self) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = '已取消', speed = 0, updated_at = ? "
                "WHERE status = '取消中'",
                (now,),
            )
            connection.execute(
                "UPDATE jobs SET status = '可继续', error = '服务重启，可继续下载', "
                "updated_at = ? WHERE status IN ('准备中', '下载中', '合并中', '暂停中')",
                (now,),
            )
