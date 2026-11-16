# SJTU-Skipper / 课堂总 Agent

SJTU-Skipper 将音视频输入、STT、VLM、LLM、行为插件和 Web UI 分层组织．行为层通过追加流、事件和服务接口访问底层能力．

设计与接口契约见：

- [`docs/architecture.md`](docs/architecture.md)
- [`docs/plugin-api.md`](docs/plugin-api.md)

## 快速开始

### 1. 环境

环境需要 Python 3.13、Bun >=1.4.4、`ffmpeg`/`ffprobe` 和 `uv`．

在仓库根目录创建 `.venv`：

```bash
nix develop
uv sync --locked --extra dev
bun install --frozen-lockfile
```

devShell 提供 Python、Bun、uv、ffmpeg 和动态库路径．非 Nix 系统需安装这些工具，再执行上述依赖安装命令．生产运行从项目 `node_modules` 加载 OMP SDK，版本由 `package.json` 和 `bun.lock` 固定．

### 2. 真实运行

```bash
./start.sh check   # 检查插件拓扑加载（直接运行最新源码，不需要重新安装）
./start.sh         # 启动 Web UI（等价于 serve）
./start.sh --port 9000
```

`start.sh` 检查 Bun 和项目依赖，再设置 `PYTHONPATH=src` 并启动 Python．修改源码后重启服务即可．

服务必须在前台运行才能接收 Ctrl+C：直接在终端执行上面的命令，不要加 `&`、`nohup` 或 `setsid`．后台 shell 启动的进程可能无法接收终端的 Ctrl+C 信号．前台运行时，Ctrl+C 会在 5 秒内退出（`SHUTDOWN_TIMEOUT`，见 `src/config.py`）；再次按 Ctrl+C 可强制退出，也可使用 `pkill -f "sjtu-skipper serve"`．

也可以继续使用安装后的命令行：

```bash
.venv/bin/sjtu-skipper check
.venv/bin/sjtu-skipper serve
```

打开页面后：

1. 首次启动会生成 `data/agent/auth.json`（用户名 `admin`，随机密码只打印一次在启动日志里），浏览器会先跳转到登录页；改成自己的密码后重启即可。登录态是签名 cookie，有效期由 `session_ttl_hours` 控制（默认 7 天）。
2. 在「设置」填写 Canvas Token．在「STT」分组填写兼容服务地址、语言和课程上下文．
3. 在「LLM」编辑并保存 `models.yml` 和 `roles.yml`．为提供方保存 API key，或点击「关联 OpenCode 套餐」完成设备授权．
4. 左侧课程树中展开课程（可点「刷新」重新拉取）。
5. 选择录播或直播，开始一节课。
6. 左侧「课堂」查看转写、板书、笔记。

左右两侧面板都带标题栏（右上角 × 收起，拖动或双击内侧分隔条调整宽度）；右侧顶部会标明当前面板对应哪门课、哪一节课。没有选中课程且没有历史时会显示占位提示。

一节课开始处理后可以继续在左侧选别的课：处理在后台继续，切换选中不会中断它。再点「开始处理」会进入「处理队列」，按先后顺序自动开始；直播课优先排队。队列是串行的：当前媒体管线一次只处理一节课。

直播与录播都连续发送最多 200 ms 的音频帧．直播显示 partial，只有 final 写入转写记录；录播使用 offline 模式全速传输，默认并发 1 路．STT 服务负责声学切分、上下文重叠及文本合并．`stt.context` 提供课程主题或术语，默认留空．板书识别按顺序提交，`vlm.prepare_ahead` 控制是否提前准备下一个窗口的图像．录播页显示下载和处理进度及「打开录像」链接，本地录像只保留最近 `input.keep_recordings`（默认 4）节．

多节录播共用连续时间轴，进度按各节时长之和计算．处理本节时预下载下一节，节间不等待语音或板书分析清空．全部节次结束后才完成输入尾段．

录播下载与音视频解码共用本地字节缓存．支持 Range 的来源可在完整下载前开始处理．不支持 Range 且索引位于文件尾时，解码等待下载．完整录像下载成功后才显示本地播放链接．

LLM 配置保存在 `data/agent/llm_runtime/models.yml` 和 `roles.yml`．设置页同时检查两份 YAML，检查通过后保存．已有会话保留创建时的配置，新会话使用新配置．

- `models.yml` 使用 OMP `providers` 结构．提供方配置 `baseUrl`、`api`、鉴权和 headers，模型配置输入能力、上下文限制与思考能力．`api` 接受已安装 OMP SDK 支持的协议，包括 `openai-completions` 和 `openai-responses`．
- `roles.yml` 将职责映射到 `provider/model:level` 或 `@role`．角色涵盖板书、分段、整课写作、两项审阅、修订、问答、交互助手与上下文压缩．未填写的角色继承 `default`．
- 板书角色 `board_recognition` 必须选择支持图像输入的模型．无效选择器、角色循环或不支持的思考级别会阻止保存．
- 默认模型为 `deepseek-flash`．省略 `maxTokens` 时继承 OMP 模型能力信息；显式填写时使用配置值．
- 提供方凭据由项目 OMP `AuthStorage` 管理，保存在 `data/agent/llm_runtime/credentials.db`．API key 与 OpenCode OAuth 凭据分别保存，设置页只显示配置状态．无鉴权服务设置 `auth: none`．

模型配置由设置页保存到 YAML，凭据写入项目 `AuthStorage`．普通插件设置保存在 `data/agent/settings.json`．首次使用时在设置页配置提供方、角色与密钥．

### 3. 命令行

```bash
sjtu-skipper serve [--host 0.0.0.0] [--port 8765] [--data-dir PATH]
sjtu-skipper check
```

## 架构一览

```text
输入插件（Canvas/SJTU 录播 + 直播 + 手动 URL）
        │  AudioStream / VideoStream（append-only）
        ├──────────────► STT 插件 ──► TextStream(transcript)
        └──────────────► VLM 插件 ──► TextStream(visual)
                                      │
行为层（llm-agent / summarize / instant-summarize / 自定义插件）
  事件总线、时钟、服务注册、UI、项目 OMP SDK 运行时
                                      │
Web UI（左右多 tab 边栏 + toolbox + 中间 pages）
```

- **流**：音频是 PCM s16le；视频是 JPEG 帧；文字是不断增长的 `TextItem` 列表。`len(stream)` 是整秒/片段数，精确时长用 `stream.duration`。
- **事件**：`clock.tick`、`session.*`、`stream.appended`、`settings.changed`、`agent.*`、`stt.*`、`vlm.*`、`summarize.*`、`instant-summarize.*`、`input.*`。
- **插件**：`id` + `requires` 组成 DAG，拓扑序加载；插件之间只通过 `ctx.services` 调用。
- **LLM 层**：Python 通过私有 JSON Lines IPC 调用一个常驻 Bun 进程．OMP SDK 管理模型请求、工具执行、会话与上下文压缩．取消会终止实际模型流和工具执行．
- **UI**：声明式 block + action；插件不写前端代码。

内置插件：

| 插件 | id | 作用 |
| --- | --- | --- |
| settings | `settings` | 设置注册、持久化、设置页 |
| llm | `llm` | 项目 Bun 运行时、模型角色配置与提供方鉴权 |
| input | `input` | Canvas/SJTU 录播、直播、ffmpeg 源 |
| stt | `stt` | 兼容 HTTP/WebSocket STT 服务，支持流式临时文字、定稿及课程上下文 |
| vlm | `vlm` | 视频流 -> 融合板书 -> 文字流 |
| llm-agent | `llm-agent` | SDK 多会话助手，原生 read/grep/glob/edit/write，流式事件与文件授权 |
| notes-pipeline | `notes-pipeline` | 按来源完成位置整理片段，整课统稿后审阅，通过后发布笔记 |
| summarize | `summarize` | 笔记页面与课后 direct/fill/skip 策略 |
| ask | `ask` | 笔记内问答：选中提问，快速 / 研究两种强度，流式回答，答案随笔记保存 |
| reader | `reader` | 通用 Markdown 阅读页（每门课一页），持有选中菜单与内联问答块 |
| instant-summarize | `instant-summarize` | 分段笔记页面与预览开关，直播和录播均可使用 |

分段核心区间由 `notes.chunk_seconds` 设置，`notes.overlap_seconds` 提供左侧原文上下文．重叠原文不重复计入正文．`notes.concurrency` 限制所有课程同时运行的笔记 agent 数量．分段预览标为未审阅；完整笔记须经过整课写作和来源覆盖、正确性两项审阅．来源缺口与审阅结果分别显示．生成失败时保留已发布笔记和用户问答．

写作者每次 `edit` 或 `write` 成功后，应用在发布锁内更新预览．浏览器可在生成期间阅读已保存文稿．审阅者读取锁保护的固定快照．来源始终只读，完整来源在上下文压缩后仍可重新读取．

## 目录

```text
src/                              # 课堂 Agent
├── app.py / cli.py / config.py
├── applog.py
├── core/                         # 流、事件、插件、服务、UI、runtime
├── agent_runtime/                # Bun OMP SDK、IPC、配置、鉴权与授权工作区
├── llm/                          # Python 角色请求与用量适配
├── plugins/                      # settings/llm/input/stt/vlm/agent/summarize/instant_summarize
├── integrations/                 # vendored SJTU Canvas/视频客户端
└── ui/                           # FastAPI + 静态前端
tests/
docs/
data/                             # 运行产物（gitignore）
```

## 格式化

flake 提供 Biome 2.6.5．`nix fmt` 通过 treefmt 调用 Biome 格式化 TypeScript，Prettier 不处理 TypeScript．配置见 `biome.json`：

- 仅匹配 `.ts`、`.tsx`、`.mts`、`.cts`．
- 使用 2 个空格缩进、LF 换行、100 列和文件末尾换行．
- 使用双引号、分号、尾逗号和箭头函数参数括号．
- 运算符位于换行前一行，保留对象括号内空格．

只格式化运行时代码时执行 `nix develop -c biome format --write src/agent_runtime`．

## 测试

```bash
.venv/bin/python -m pytest -q
bun run check:types
```

确定性测试使用脚本化运行时与本地请求记录，覆盖配置、IPC、授权和应用行为．真实模型验收单独执行并保存请求与文稿记录．

TypeScript 由项目 devShell 提供；`check:types` 使用根目录 `tsconfig.json`．

## 说明

- 直播通道使用 SJTU 门户 `v1/vod_live` 和 `v1/course_vod_videoinfos`；也可在 UI 中手动填入 HLS/RTMP/HTTP URL．
- 详细接口与扩展方式见 `docs/plugin-api.md`。
