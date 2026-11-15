# SJTU-Skipper / 课堂总 Agent

SJTU-Skipper 仓库包含两条相关但独立的能力：

1. **旧的板书批处理流水线**：`legacy/blackboard_lecture_agent.py`，从课堂录像采样、配准、融合黑板帧，经 VLM 转录后合成 LaTeX 板书笔记。
2. **新的课堂总 Agent 架构**：`src/`，把音频/视频输入、STT、VLM、LLM、行为插件和 Web UI 拆成可独立演进的层；行为层只通过 append-only 流、事件和服务接口与底层交互。

新架构的设计与接口契约见：

- [`docs/architecture.md`](docs/architecture.md)
- [`docs/implementation-plan.md`](docs/implementation-plan.md)
- [`docs/plugin-api.md`](docs/plugin-api.md)

## 新架构快速开始

### 1. 环境

系统需要 Python 3.12、`ffmpeg`/`ffprobe`；`aria2c` 可选。

本项目使用仓库根目录的 `.venv`（Python 3.12）。旧 `.venv` 已删除并重建：

```bash
nix develop
uv sync --locked --extra dev
```

devShell 提供 Python、uv、ffmpeg 和动态库路径，不打包应用．在非 Nix 系统上，安装 Python 3.13、uv 和 ffmpeg 后执行同一条 `uv sync` 命令．

### 2. 真实运行

```bash
./start.sh check   # 检查插件拓扑加载（直接运行最新源码，不需要重新安装）
./start.sh         # 启动 Web UI（等价于 serve）
./start.sh --port 9000
```

`start.sh` 只是设置 `PYTHONPATH=src` 并调用 `.venv/bin/python -m src`，所以改完源码直接重启即可，不需要 `pip install -e` 或任何构建步骤。

服务必须**在前台跑**才收得到 Ctrl+C：直接在终端里执行上面的命令，不要加 `&`、`nohup`、`setsid`，也不要丢进后台 shell——退到后台的进程已经脱离终端，按 Ctrl+C 不会有任何信号发过去。前台运行时 Ctrl+C 会在 5 秒内退出（`SHUTDOWN_TIMEOUT`，见 `src/config.py`）；真卡住了再按一次是强制退出，或者用 `pkill -f "sjtu-skipper serve"`。

也可以继续使用安装后的命令行：

```bash
.venv/bin/sjtu-skipper check
.venv/bin/sjtu-skipper serve
```

打开页面后：

1. 首次启动会生成 `data/agent/auth.json`（用户名 `admin`，随机密码只打印一次在启动日志里），浏览器会先跳转到登录页；改成自己的密码后重启即可。登录态是签名 cookie，有效期由 `session_ttl_hours` 控制（默认 7 天）。
2. 右侧「设置」填入 Canvas Token、LLM API Key / Base URL．在「STT」分组填写兼容服务地址、语言和课程上下文．
3. LLM 连接方式二选一：OpenAI 兼容接口（Base URL + API Key），或在设置里点「关联 OpenCode 套餐」完成设备授权。OpenCode 凭据独立保存在本应用，不依赖本机 OpenCode。
4. 左侧课程树中展开课程（可点「刷新」重新拉取）。
5. 选择录播或直播，开始一节课。
6. 左侧「课堂」查看转写、板书、笔记。

左右两侧面板都带标题栏（右上角 × 收起，拖动或双击内侧分隔条调整宽度）；右侧顶部会标明当前面板对应哪门课、哪一节课。没有选中课程且没有历史时会显示占位提示。

一节课开始处理后可以继续在左侧选别的课：处理在后台继续，切换选中不会中断它。再点「开始处理」会进入「处理队列」，按先后顺序自动开始；直播课优先排队。队列是串行的：当前媒体管线一次只处理一节课。

直播与录播都连续发送最多 200 ms 的音频帧．直播显示 partial，只有 final 写入转写记录；录播使用 offline 模式全速传输，默认并发 1 路．STT 服务负责声学切分、上下文重叠及文本合并．`stt.context` 提供课程主题或术语，默认留空．VLM 并发保持独立设置．录播页显示下载和处理进度及「打开录像」链接，本地录像只保留最近 `input.keep_recordings`（默认 4）节．

多节录播共用连续时间轴，进度按各节时长之和计算．处理本节时预下载下一节，节间不等待语音或板书分析清空．全部节次结束后才完成输入尾段．

Canvas token 与 OpenAI 兼容 key 可以放在根 `.env`（git-ignored；OpenCode 套餐则走设置页授权）：

```dotenv
DEEPSEEK_API_KEY=...
# 或使用任意 OpenAI 兼容服务
LLM_BASE_URL=http://127.0.0.1:3000/v1
LLM_API_KEY=...
LLM_MODEL=...
# STT service endpoint
STT_SERVER_URL=http://127.0.0.1:8766
```

设置会持久化到 `data/agent/settings.json`（权限 0600）。

### 3. 命令行

```bash
sjtu-skipper serve [--host 0.0.0.0] [--port 8765] [--data-dir PATH]
sjtu-skipper check

# 兼容旧命令：lecture-agent <同上>
```

## 新架构一览

```text
输入插件（Canvas/SJTU 录播 + 直播 + 手动 URL）
        │  AudioStream / VideoStream（append-only）
        ├──────────────► STT 插件 ──► TextStream(transcript)
        └──────────────► VLM 插件 ──► TextStream(visual)
                                      │
行为层（llm-agent / summarize / instant-summarize / 自定义插件）
  事件总线 · 时钟 · 服务注册 · UI · LLM 统一接口
                                      │
Web UI（左右多 tab 边栏 + toolbox + 中间 pages）
```

- **流**：音频是 PCM s16le；视频是 JPEG 帧；文字是不断增长的 `TextItem` 列表。`len(stream)` 是整秒/片段数，精确时长用 `stream.duration`。
- **事件**：`clock.tick`、`session.*`、`stream.appended`、`settings.changed`、`agent.*`、`stt.*`、`vlm.*`、`summarize.*`、`instant-summarize.*`、`input.*`。
- **插件**：`id` + `requires` 组成 DAG，拓扑序加载；插件之间只通过 `ctx.services` 调用。
- **LLM 层**：统一 `ChatRequest/ChatResponse/StreamEvent`，OpenAI 兼容 provider，流式/非流式、工具调用、图片、重试/超时。
- **UI**：声明式 block + action；插件不写前端代码。

内置插件：

| 插件 | id | 作用 |
|---|---|---|
| settings | `settings` | 设置注册、持久化、设置页 |
| llm | `llm` | Provider 注册、统一 LLM 调用 |
| input | `input` | Canvas/SJTU 录播、直播、ffmpeg 源 |
| stt | `stt` | 兼容 HTTP/WebSocket STT 服务，支持流式临时文字、定稿及课程上下文 |
| vlm | `vlm` | 视频流 -> 融合板书 -> 文字流 |
| llm-agent | `llm-agent` | 多会话 REPL Agent；具名 agent profile + 工具参数声明；info/read/search/replace/append（+ 可选 write）；支持流式 |
| summarize | `summarize` | 非即时：课后/手动一次性整理整堂课，写 note_*.md 结构完整讲义 |
| ask | `ask` | 笔记内问答：选中提问，快速 / 研究两种强度，流式回答，答案随笔记保存 |
| reader | `reader` | 通用 Markdown 阅读页（每门课一页），持有选中菜单与内联问答块 |
| instant-summarize | `instant-summarize` | 即时：按 N 字符/秒增量合并，写 instant_note_*.md；由笔记面板的开关控制累积与否 |

## 旧板书流水线

旧脚本保持原样，仍可单独使用：

```bash
cp .env.example .env   # 填 DEEPSEEK_API_KEY
source .venv/bin/activate
python legacy/blackboard_lecture_agent.py --session topo_1022
```

产物在 `data/outputs/<session>/` 与 `data/notes/`。新 VLM 插件复用其配准/融合算法与提示词；旧脚本已移入 `legacy/`（本地保留、git-ignored）。

## 目录

```text
src/                              # 新总 Agent（直接位于 src 下）
├── app.py / cli.py / config.py
├── applog.py
├── core/                         # 流、事件、插件、服务、UI、runtime
├── llm/                          # 协议无关 LLM 层
├── plugins/                      # settings/llm/input/stt/vlm/agent/summarize/instant_summarize
├── integrations/                 # vendored SJTU Canvas/视频客户端
└── ui/                           # FastAPI + 静态前端
legacy/                           # 旧脚本与旧子模块，git-ignored，仅本地保留
├── blackboard_lecture_agent.py
├── topo_lecture_agent.py
├── test_placeholder_experiment.py
└── sjtu-canvas-assistant/
tests/
docs/
data/                             # 运行产物（gitignore）
```

`sjtu-canvas-assistant` 的必要客户端代码已经 vendor 到 `src/integrations/sjtu_canvas_assistant/`，运行时不依赖 `legacy/`。

## 测试

```bash
.venv/bin/python -m pytest -q
```

测试不访问网络：LLM 用 `httpx.MockTransport` 或测试内的脚本化 provider，Canvas/SJTU 用 fake client。生产代码不包含 mock provider / mock STT 后端。

## 说明

- 旧 `sjtu-canvas-assistant` 子模块已移入 `legacy/`，不再参与构建；必要的客户端代码已 vendor 到 `src/integrations/sjtu_canvas_assistant/`。
- 不修改旧板书脚本（同上）。
- 新版 SJTU 门户（`jy-application-resourcemanage`）的录播/直播适配已随 vendored 客户端放入 `src/integrations/sjtu_canvas_assistant/`，旧子模块不再需要。
- 直播通道优先使用 SJTU 新版门户 `v1/vod_live` + `v1/course_vod_videoinfos`；若接口或鉴权变化，仍可在 UI 中手动填入 HLS/RTMP/HTTP URL。
- 详细接口与扩展方式见 `docs/plugin-api.md`。
