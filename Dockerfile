FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 appuser

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY assets ./assets
COPY docker-entrypoint.sh /docker-entrypoint.sh
# 飞牛新建文件常是 mode 0，不 chmod 的话非 root 用户读不了源码
RUN chmod -R a+rX /app \
    && chmod +x /docker-entrypoint.sh \
    && chown -R appuser:appuser /app

EXPOSE 8000
ENTRYPOINT ["/docker-entrypoint.sh"]
