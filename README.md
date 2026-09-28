# 视频下载

在局域网浏览器里解析链接，让 NAS 在后台保存视频。支持 YouTube、哔哩哔哩、抖音以及其他 yt-dlp 可解析的站点。

## 功能

- **解析链接**：粘贴链接后展示可选格式（MP4 / MKV / WebM / MP3）、清晰度和字幕
- **后台下载**：任务在服务端运行，关闭浏览器不会中断；支持继续、取消、删除临时文件
- **下载历史**：共享记录，局域网内所有设备可见，支持一键重新下载
- **代理设置**：自动检测常见本地代理端口，也可手动填写；哔哩哔哩和抖音在代理失败时自动改用直连
- **Cookies 导入**：支持 Netscape 格式的 `cookies.txt`，多站点 Cookies 自动合并

## 部署到飞牛 NAS

```bash
cd /vol1/1000/docker
git clone https://github.com/yiyu12138/VD.git
cd VD
sudo docker compose up -d --build
```

部署完成后访问 `http://飞牛设备IP:8000`。

数据卷路径不是 `/vol1/1000` 时，改 `docker-compose.yml` 里的下载目录挂载。

### 更新

```bash
cd /vol1/1000/docker/VD
git pull
sudo docker compose up -d --build
```

### 常用命令

```bash
sudo docker compose ps
sudo docker compose logs -f
sudo docker compose down
```

## 使用说明

1. 粘贴视频链接，点击**解析**。
2. 选择格式、清晰度和可选字幕，点击**保存到 NAS**。
3. 任务在后台运行，可关闭页面。
4. 文件保存到 `/vol1/1000/下载/视频下载/<视频标题>/`。
5. 失败任务显示为「可继续」，可恢复或删除临时文件。

### 代理

设置 → 自动检测代理，会扫描本机常见端口（20171、7890 等）。也可手动填写。哔哩哔哩和抖音代理失败时自动改用直连。

### Cookies

在已登录的电脑浏览器里用「Get cookies.txt LOCALLY」等扩展导出 Netscape 格式的 `cookies.txt`，再到设置 → Cookies 导入上传。不同站点可分开导入，会自动合并。Cookies 相当于登录凭据，请只在受信任的设备和网络中使用。

## 技术架构

| 层 | 技术 |
|----|------|
| 后端 | Python 3.12 · FastAPI · yt-dlp · FFmpeg |
| 前端 | 原生 HTML / CSS / JS |
| 存储 | SQLite（任务、历史、设置） |
| 容器 | Docker Compose · host 网络 · 非 root 运行 |

## 测试

```bash
pip install -r requirements.txt
python -m pytest -v
python -m compileall app
docker compose build
```

## 注意事项

- 仅适用于受信任的局域网，没有公网认证，不要直接暴露到公网。
- 只下载你有权保存的内容，并遵守各平台规则。
- 受 DRM 或付费保护的内容无法下载。
