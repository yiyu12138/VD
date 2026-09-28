#!/bin/sh
set -e
mkdir -p /data /downloads
chown -R appuser:appuser /data /downloads || true
exec runuser -u appuser -- uvicorn app.main:app --host 0.0.0.0 --port 8000
