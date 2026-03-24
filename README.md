# XiaoAi Music

小爱音箱免费播放本地歌曲，免登录小米账号。

## 致谢

感谢 [idootop/open-xiaoai](https://github.com/idootop/open-xiaoai) 项目。

## 功能
![img.png](img.png)

- 曲库索引
  - 启动时自动为曲库建立索引
  - 支持配置定时刷新索引（默认不定时刷新）
  - 支持通过语音命令主动触发刷洗（命令关键词支持配置）
- 通过播放关键词搜索播放歌曲
  - 搜索关键词匹配的歌曲，打乱顺序，提取前 20 首播放
- 通过停止关键词停止当前播放
- 通过随便听听关键词，随机播放 20 首歌曲
- 播放上一首和下一首，调整音量后自动重播
- 通过关键词限定歌手或专辑名

> 关键词、歌曲数目等均可在 config.py 中配置

示例：
- 小爱同学，播放许嵩
    - 逻辑：搜索歌名、歌手、专辑名、文件名、路径名中包含许嵩的歌曲，打乱顺序，提取前 20 首播放。
- 小爱同学，播放许嵩的歌（庐州月）
    - 逻辑：搜索歌手名中包含许嵩的歌曲（庐州月），打乱顺序，提取前 20 首播放。
- 小爱同学，播放范特西专辑歌曲
    - 逻辑：搜索专辑名中包含范特西的歌曲，打乱顺序，提取前 20 首播放。
- 小爱同学，播放范特西中的晴天
    - 逻辑：搜索专辑名中包含范特西的歌曲晴天，打乱顺序，提取前 20 首播放。
- 小爱同学，停止播放
    - 逻辑：停止当前播放
- 小爱同学，随便听听
    - 逻辑：从曲库中随机播放 20 首歌曲。

## 安装 ffprobe（必需）

项目使用 `ffprobe` 读取音乐元数据（歌名/歌手/专辑）和精确时长。

- macOS（Homebrew）：

```bash
brew install ffmpeg
```

- Ubuntu / Debian：

```bash
sudo apt update
sudo apt install -y ffmpeg
```

验证：

```bash
ffprobe -version
```

如果未安装 `ffprobe`，程序会在启动时直接报错退出。

## 运行

1. 使用本项目前，请先根据 [idootop/open-xiaoai](https://github.com/idootop/open-xiaoai) 完成小爱音箱刷机并安装 client。

2. 确保小爱端 client 已运行并连接到本机 `4399` 端口，**或在 config.json 中自定义端口**，多个音箱请使用不同端口。

3. 运行前请先将 `config.json.example` 重命名为 `config.json`，并至少指定一个音乐目录：

- 必选 `music_dirs`：配置多个本地音乐目录
- 可选 `search.max_results`：播放队列取前 N 首
- 可选 `search.refresh_interval_sec`：曲库索引刷新间隔（秒）
- 可选 `search.index_file`：索引文件保存路径（保存歌曲元信息）
- 可选 `commands.play_keywords` / `commands.stop_keywords`：语音命令关键词
- 可选 `http.base_url`：api 服务地址（例如 `http://192.168.11.18:18080`，可选）
- 可选 `xiaoai.port`：本地监听小爱的端口（例如 `4399`，可选）

4. 执行命令启动服务

```bash
uv run main.py
```

## HTTP API

服务启动后会在配置的端口（默认 `18080`）上提供 HTTP 接口，可用于外部程序集成或调试。

### 文件路由

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/file/{hex}/{filename}` | 播放音频文件 |
| `HEAD` | `/file/{hex}/{filename}` | 获取音频文件元信息（不传输文件内容） |

- `{hex}` 是文件绝对路径的 UTF-8 hex 编码，由系统在搜索时自动生成
- `{filename}` 是 URL 编码后的原始文件名，仅用于可读性
- 支持 HTTP `Range` 请求（`bytes=start-end`），用于音频跳转/续播
- 仅允许访问已通过搜索注册的文件，未注册文件返回 `403`

### 控制 API

所有控制接口均为 `GET /api/{command}?param=value`，返回 JSON：

```json
{ "ok": true, "result": "..." }
```

| 接口 | 参数 | 说明 |
|------|------|------|
| `/api/say?payload=文本` | `payload`（必填） | 让小爱朗读指定文本（TTS） |
| `/api/ask?payload=文本` | `payload`（必填） | 向小爱提问并获取回复 |
| `/api/music?url=地址` | `url`（必填） | 播放指定 URL 的音频 |
| `/api/local?keyword=关键词` | `keyword`（必填） | 搜索本地曲库并播放匹配的歌曲 |
| `/api/random` | 无 | 从曲库中随机播放歌曲 |
| `/api/prev` | 无 | 播放上一首 |
| `/api/next` | 无 | 播放下一首 |
| `/api/stop` | 无 | 停止播放并清空队列 |
| `/api/refresh` | 无 | 刷新曲库索引 |
| `/api/status` | 无 | 当前播放曲目及播放列表 |

#### 示例

```bash
# 让小爱说一句话
curl "http://192.168.11.18:18080/api/say?payload=你好"

# 搜索并播放周杰伦的歌曲
curl "http://192.168.11.18:18080/api/local?keyword=周杰伦"

# 随机播放
curl "http://192.168.11.18:18080/api/random"

# 下一首
curl "http://192.168.11.18:18080/api/next"

# 停止播放
curl "http://192.168.11.18:18080/api/stop"
```

> 请将 `192.168.11.18:18080` 替换为你实际的 `http.base_url` 配置。

## 后台运行

前一步的“运行”测试没问题之后，再配置后台运行。systemd/PM2/nohup 任选一即可。

### system service 后台运行（支持多实例，推荐）

1. 多个配置文件（可选）

配置文件命名为 config*.json，如 config-mi1.json config-mi2.json。注意各配置之间的 xiaoai.port 与 http.port 不能相同。

2. 新建 `/etc/systemd/system/mimusic@.service`，根据实际情况修改。

```TOML
[Unit]
Description=XiaoAI local music - Instance %i
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/miMusic

ExecStart=/root/.local/bin/uv run main.py --config %i.json
RestartSec=10
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

3. 启动：

```
sudo systemctl start mimusic@config-mi1
sudo systemctl start mimusic@config-mi2

```

4. 开机自启：

```
sudo systemctl enable mimusic@config-mi1
sudo systemctl enable mimusic@config-mi2
```

5. 查看日志：

```
journalctl -u mimusic@config-mi1 -f
journalctl -u mimusic@config-mi2 -f
```

### PM2 后台运行


1. 安装 PM2（如未安装）：

```bash
npm i -g pm2
```

2. 启动：

```bash
mkdir -p logs
pm2 start ecosystem.config.cjs
```

3. 查看状态和日志：

```bash
pm2 status
pm2 logs XiaoAiMusic
```

4. 重启 / 停止：

```bash
pm2 restart XiaoAiMusic
pm2 stop XiaoAiMusic
```

5. 开机自启（可选）：

```bash
pm2 save
pm2 startup
```

### nohup 后台运行（免安装）

后台运行：

```bash
mkdir -p logs
nohup uv run main.py > logs/app.log 2>&1 &
echo $! > logs/app.pid
```

监听日志：

```bash
tail -f logs/app.log
```

停止进程（按 PID）：

```bash
kill "$(cat logs/app.pid)"
```

重启（先停再起）：

```bash
kill "$(cat logs/app.pid)" 2>/dev/null || true
nohup uv run main.py > logs/app.log 2>&1 &
echo $! > logs/app.pid
```
