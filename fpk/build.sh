#!/bin/bash
# 构建飞牛 fnOS 原生 fpk（不使用 Docker），需在飞牛设备上运行：
#   bash fpk/build.sh
# 产物：<仓库根目录>/dist/vd_<版本>_x86.fpk
set -eu

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
APPNAME="vd"
VERSION="$(sed -n "s/^__version__ *= *'\([^']*\)'.*/\1/p" "${ROOT}/app/__init__.py")"
PY="${FNOS_PYTHON:-/var/apps/python312/target/bin/python3}"
PIP_INDEX="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"

[ -n "${VERSION}" ] || { echo "无法读取版本号" >&2; exit 1; }
[ -x "${PY}" ] || { echo "找不到 ${PY}，请先安装飞牛 python312" >&2; exit 1; }
command -v fnpack >/dev/null || { echo "未找到 fnpack" >&2; exit 1; }
echo "==> 版本 ${VERSION}"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
APP="${WORK}/${APPNAME}"
mkdir -p "${APP}/app/server" "${APP}/app/pylib"

cp -a "${HERE}/manifest" "${HERE}/ICON.PNG" "${HERE}/ICON_256.PNG" \
      "${HERE}/config" "${HERE}/cmd" "${HERE}/wizard" "${APP}/"
cp -a "${HERE}/app/ui" "${APP}/app/ui"
cp -a "${ROOT}/app" "${APP}/app/server/app"
find "${APP}/app/server" -name '__pycache__' -prune -exec rm -rf {} +

echo "==> 安装依赖到 pylib"
"${PY}" -m pip install -q --no-warn-script-location --disable-pip-version-check \
    --target "${APP}/app/pylib" --index-url "${PIP_INDEX}" -r "${ROOT}/requirements.txt"
find "${APP}/app/pylib" -name '__pycache__' -prune -exec rm -rf {} +
du -sh "${APP}/app/pylib"

sed -i "s/^version .*/version               = ${VERSION}/" "${APP}/manifest"
find "${APP}" -type d -exec chmod 755 {} +
find "${APP}" -type f -exec chmod 644 {} +
chmod +x "${APP}"/cmd/*

echo "==> 自检"
( cd "${APP}/app/server" && PYTHONPATH=".:../pylib" DATA_DIR="${WORK}/d" DOWNLOAD_DIR="${WORK}/dl" \
  "${PY}" -c "import app.main, yt_dlp, uvicorn; print('导入正常, yt-dlp', yt_dlp.version.__version__)" )

( cd "${APP}" && fnpack build )
mkdir -p "${ROOT}/dist"
OUT="${ROOT}/dist/${APPNAME}_${VERSION}_x86.fpk"
cp "${APP}/${APPNAME}.fpk" "${OUT}"
echo "==> 完成: ${OUT}"
ls -la "${OUT}"
