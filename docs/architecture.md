# 课堂总 Agent 架构设计

> 状态：课堂插件架构与 OMP SDK 运行时契约
>
> 目标：把课堂的音频、视频、语音识别、板书识别、LLM 调用、行为插件和 UI 拆成可独立演进的分层，行为层只通过「流 + 事件 + 服务接口」与底层交互。

## 1. 总体分层

```text
┌──────────────────────────────────────────────────────────────────────┐
│                            UI（Web）                                  │
│  左侧多 tab 边栏 / 右侧多 tab 边栏 / 每个 tab 下有 toolbox           │
│  中间 pages 区域 / 页面内 block 渲染 / action 回传                    │
└───────────────────────────────▲──────────────────────────────────────┘
                                │ UIManager（状态 + action）
┌───────────────────────────────┴──────────────────────────────────────┐
│                         行为层（Behavior）                            │
│  插件：llm-agent / summarize / 自定义行为插件                          │
│  能力：事件总线、流读取、LLM、UI、服务注册与调用                       │
│  依赖：只依赖「流接口 + 事件 + 服务接口 + LLM 接口」                   │
└───────────────▲───────────────────────────────▲──────────────────────┘
                │ TextStream(transcript)         │ TextStream(visual)
┌───────────────┴───────────┐     ┌─────────────┴──────────────────────┐
│  STT 插件                 │     │  VLM 插件（picture_parser 管线）    │
│  AudioStream -> 文字流    │     │  VideoStream -> 文字流              │
└───────────────▲───────────┘     └─────────────▲──────────────────────┘
                │ AudioStream                    │ VideoStream
┌───────────────┴────────────────────────────────┴──────────────────────┐
│ 输入插件（Input）                                                     │
│  录播通道：Canvas + SJTU 视频门户 VOD                                 │
│  直播通道：SJTU 视频门户 vod_live + FLV 设备流 / 手动 HLS/RTMP URL     │
│  ffmpeg -> PCM 音频块 + JPEG 视频帧                                    │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
┌───────────────────────────────┴──────────────────────────────────────┐
│ 项目 OMP SDK 运行时（Bun >=1.4.4）                                    │
│  Python 私有 IPC -> createAgentSession / core 执行循环                 │
│  models.yml / roles.yml / 提供方 AuthStorage                           │
│  原生文件工具、模型请求、会话存储与上下文压缩                          │
└──────────────────────────────────────────────────────────────────────┘
```

核心原则：

1. **数据平面与控制平面分离**。音视频/文字只通过 append-only 流传递；行为层不接触 Canvas、ffmpeg、SJTU 门户、STT/VLM 实现细节。
2. **插件只通过 ctx 暴露的能力交互**。`PluginContext` 是行为接口的唯一入口。
3. **流前缀不可变**。任何消费者只能追加、读取，不能修改已有片段。
4. **服务按名字发现**。插件之间不互相 import 实现，只通过 `services` 注册/调用。
5. **UI 是声明式的**。插件返回 block 列表和 action id，前端只负责渲染与回传，不执行插件代码。
6. **所有 LLM 请求走 LLM 层**．STT 使用独立 HTTP/WebSocket 协议，不经过 LLM 服务．

## 2. 流数据平面

### 2.1 通用语义

- 所有流都是 **append-only**：已有片段永不被修改或删除。
- 时间轴使用秒（float），从当前会话开始计为 0。
- 每个流属于一个 `Session`；新会话创建全新的四个流，旧流保留只读（用于历史回放/导出）。
- 消费者使用自己的游标读取；流本身不维护「当前读者」状态。
- 读取超出当前末尾时：`read()` 截断到当前末尾；`wait_for()` 可等待到达指定长度。

### 2.2 AudioStream

```python
class AudioStream:
    sample_rate: int          # 默认 16000
    channels: int             # 默认 1
    sample_width: int         # 默认 2（s16le）
    session_id: str

    def __len__(self) -> int: ...            # 当前时长（整秒；len() 协议限制）
    @property
    def duration(self) -> float: ...         # 当前时长（精确到秒的小数）
    def append(self, pcm: bytes, duration: float | None = None) -> None: ...
    def read(self, start: float, end: float) -> bytes: ...
    def wait_for(self, end: float, timeout: float | None = None) -> bool: ...
```

- `read(l, r)` 返回该时间段的 **PCM 原始字节**（s16le，单声道）。
- Python 的 `len()` 必须返回 `int`，因此 `len(stream)` 是整秒时长；需要亚秒精度时用 `stream.duration`。
- `append` 的时长默认由字节数推算：`len(pcm) / (sample_rate * channels * sample_width)`。
- 输入插件按约 100 ms 一块追加。

### 2.3 VideoStream

```python
class VideoStream:
    codec: str = "mjpeg"
    width: int
    height: int
    fps: float
    session_id: str

    def __len__(self) -> int: ...            # 整秒；精确时长用 .duration
    @property
    def duration(self) -> float: ...
    def append_frame(self, jpeg: bytes, duration: float | None = None) -> None: ...
    def read(self, start: float, end: float) -> bytes: ...   # 拼接的 JPEG 帧字节
    def read_frames(self, start: float, end: float) -> list[VideoFrame]: ...
    def wait_for(self, end: float, timeout: float | None = None) -> bool: ...
```

- 视频流以 **JPEG 帧** 为存储单位。`read(l, r)` 返回区间内所有帧的 JPEG 字节拼接；需要逐帧时使用 `read_frames`。
- 每帧有 `start`、`end`、`data`、`index`。默认帧时长 `1 / fps`。
- 选择 JPEG 而不是 MP4 的原因：无需容器复用/解复用，VLM 管线可直接解码；代价是体积较大；默认 4 fps 以匹配原管线，可按需降低。

### 2.4 TextStream

```python
@dataclass(frozen=True)
class TextItem:
    index: int
    text: str
    start: float | None
    end: float | None
    source: str
    meta: Mapping[str, Any]

class TextStream(Sequence[TextItem]):
    session_id: str
    name: str                 # "transcript" | "visual" | 其他

    def __len__(self) -> int: ...             # 片段数
    def __getitem__(self, i) -> TextItem: ...
    def append(self, text, *, start=None, end=None, source="", meta=None) -> TextItem: ...
    def text_since(self, index: int) -> str: ...
    def char_count_since(self, index: int) -> int: ...
    def snapshot_index(self) -> int: ...
    @property
    def char_count(self) -> int: ...
```

- 文字流是「不断增长的列表」，列表项是 `TextItem`；`len()` 是片段数。
- 片段不可变；`index` 单调递增。
- `text_since(i)` 返回从第 i 项到末尾的拼接文本，便于行为层做增量总结。

### 2.5 StreamHub / StreamSet

```python
@dataclass
class StreamSet:
    session_id: str
    started_at: float
    meta: dict
    audio: AudioStream
    video: VideoStream
    transcript: TextStream
    visual: TextStream

class StreamHub:
    @property
    def current(self) -> StreamSet: ...
    def start_session(self, *, meta=None) -> StreamSet: ...
    def end_session(self, reason="") -> None: ...
    def list_sessions(self) -> list[SessionInfo]: ...
```

- 输入、STT、VLM 都从 `hub.current` 取当前流。
- 行为层通过 `ctx.streams.current` 读取，不调用 `start_session`。
- `start_session` 会发送 `session.started` 事件；`end_session` 发送 `session.stopped`。

### 2.6 流事件

为避免 100 ms 一次的音频追加把事件总线打爆，音视频流做 **合并通知**：

- `audio`：默认每新增 0.5 s 发一次 `stream.appended`。
- `video`：默认每新增 1.0 s 发一次。
- `transcript` / `visual`：每次 `append` 立即发一次。

事件 payload：

```python
@dataclass(frozen=True)
class StreamAppended:
    stream: str          # "audio" | "video" | "transcript" | "visual"
    session_id: str
    old_len: float | int
    new_len: float | int
    delta: float | int
    start: float | int | None
    end: float | int | None
    chars: int = 0       # 仅文字流
    first_index: int = 0 # 仅文字流
```

## 3. 事件总线与时钟

```python
class EventBus:
    def define(self, name: str, description: str = "") -> EventType: ...
    def on(self, type: str, handler: Callable[[Event], None], *, plugin_id="", priority=0) -> Subscription: ...
    def once(...)
    def off(self, subscription) -> None: ...
    def emit(self, type: str, **payload) -> None: ...
    def flush(self, timeout: float = 5.0) -> bool: ...
    def start(self) / stop(self): ...
```

- 事件在独立 dispatcher 线程顺序分发；`emit` 只入队，不阻塞生产者。
- handler 异常被捕获、记录日志，并发出 `event.error`；不会中断其他 handler。
- 支持 `"*"` 通配监听。
- 同一事件、同一插件内按 `priority` 从高到低执行；不同插件按注册顺序。
- `flush()` 用于测试和优雅退出。

内置事件：

| 事件 | 触发 |
| --- | --- |
| `clock.tick` | 每秒一次，payload: `ts`, `tick`, `interval` |
| `session.started` | 新会话 |
| `session.stopped` | 会话结束 |
| `stream.appended` | 流追加（见上） |
| `settings.changed` | 设置项变化 |
| `ui.changed` | UI 状态变化（节流） |
| `llm.request` / `llm.response` / `llm.error` | LLM 层观测 |
| `agent.run.started` / `agent.message` / `agent.tool_call` / `agent.tool_result` / `agent.run.finished` / `agent.error` | Agent 运行 |
| `vlm.segment` / `vlm.error` | VLM |
| `summarize.started` / `summarize.finished` / `summarize.error` | 整堂课总结插件 |
| `instant-summarize.triggered` / `instant-summarize.updated` / `instant-summarize.error` | 增量总结插件 |
| `input.status` / `input.error` | 输入源 |

插件可以 `define()` 自己的事件类型（仅用于文档/校验），然后 `emit`。

## 4. 插件系统

### 4.1 插件协议

```python
class Plugin:
    id: str
    requires: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()

    def setup(self, ctx: PluginContext) -> None: ...
    def teardown(self) -> None: ...
```

- `requires` 是插件 id 列表，形成 DAG。
- `PluginManager` 拓扑排序后依次 `setup`；循环依赖、缺失依赖在加载时报错。
- `teardown` 逆序执行。
- 插件可声明 `provides` 作为文档；实际服务注册用 `ctx.services.register()`。

### 4.2 PluginContext（行为接口）

```python
@dataclass
class PluginContext:
    plugin_id: str
    data_dir: Path
    events: EventBus
    streams: StreamHub          # 只读约定；写入仅限输入/STT/VLM
    llm: LLMService
    ui: UIManager
    services: ServiceRegistry
    meta: MetaRegistry
    logger: logging.Logger

    def service(self, name: str) -> Any: ...
    def register_service(self, name: str, obj: Any) -> None: ...
    def require(self, name: str) -> Any: ...
    def log(self, level, msg, **fields): ...
```

行为插件能看到的能力就是这些；不允许 `ctx.runtime` 之类的后门。

### 4.3 服务注册与插件互调

```python
class ServiceRegistry:
    def register(self, name: str, obj: Any, *, plugin_id: str) -> None: ...
    def get(self, name: str) -> Any | None: ...
    def require(self, name: str) -> Any: ...
    def call(self, name: str, method: str, *args, **kwargs) -> Any: ...
    def names(self) -> list[str]: ...
```

- 服务名用点号命名空间：`settings`, `agent`, `input.catalog`, `stt`, `vlm`, `summarize`, `instant-summarize`。
- 插件声明 `requires` 保证加载顺序；运行时调用前仍应处理服务不存在的情况。
- 服务对象的方法就是插件公开的 API；不要跨插件访问私有字段。

### 4.4 内置插件与加载顺序（示例）

```text
settings
llm
input
stt
vlm
llm-agent
summarize
```

拓扑序由 `requires` 决定，不依赖注册顺序．`llm-agent` 依赖 `llm` 和 `settings`；`summarize` 与 `instant-summarize` 依赖 `llm-agent`；`vlm` 依赖 `llm` 和 `settings`；`stt` 依赖 `settings`．

## 5. OMP SDK 运行时

### 5.1 进程与依赖

Python 保留课堂处理、任务队列和 Web 服务．应用管理一个常驻 Bun 子进程，多个 SDK 会话在同一进程内异步执行．

依赖从项目 `node_modules` 加载．`package.json` 与 `bun.lock` 固定同组 OMP 包版本：

- `@oh-my-pi/pi-coding-agent`
- `@oh-my-pi/pi-agent-core`
- `@oh-my-pi/pi-ai`
- `@oh-my-pi/pi-natives`

运行环境要求 Bun >=1.4.4．安装依赖执行 `bun install --frozen-lockfile`．生产运行不依赖临时目录中的 SDK 源码．

Python 与 Bun 使用带请求 ID 的 JSON Lines 通信．stdout 只传 IPC 消息，日志写入 stderr．IPC 支持创建、运行、重置、关闭、取消会话和单次生成．返回流包含正文、思考、工具调用、工具结果、文件变更、用量和终态．

`createAgentSession()` 管理文件工具、会话存储和上下文压缩．core 的 `Agent` 执行模型与工具循环．Python 只保留显示记录和任务状态，不维护第二套模型历史或工具输出截断．

### 5.2 隔离与文件工具

子进程在导入 SDK 前设置项目 agent 根目录，并移除继承的 OMP 配置覆盖变量．会话显式使用项目配置：

- `Settings.isolated()`、项目 `AuthStorage`、`ModelRegistry` 和会话目录．
- 明确的工作目录、角色模型、思考级别和工具名单．
- `restrictToolNames: true`、空 skills、rules、contextFiles、promptTemplates 和 slashCommands．
- 禁用全局配置发现、扩展发现、MCP、LSP 和 IRC．

文件工具直接使用 OMP 的 `read`、`grep`、`glob`、`edit` 和 `write`．工具保留上游 schema、动态描述、错误和输出．Python 不定义 `info`、`search`、`replace`、`append` 工具或编辑 JSON 方言．

每个会话持有独立的 `ToolConversationState` 和 `EditStore`．默认编辑模式为 hashline，模型策略可选择 replace 模式．实际模型同时决定工具描述、工具协议和提示模板．`tools.format=auto` 保留原生协议或 OMP dialect 的自动选择．

模型模板由 OMP 的 `systemPromptTemplates` 规则选择．项目只追加课堂职责提示，不传绕过模板选择的 `customSystemPrompt`．模板与职责文本来自固定项目路径．

工作区使用逻辑文件名映射．来源和参考底稿只读，指定文稿可写．应用检查每次调用涉及的全部路径，包括多文件编辑和移动目标．未授权绝对路径、父目录逃逸、符号链接逃逸和未开放 URI 均会报错．`grep/glob` 只能遍历授权文件．只读审阅者不注册写工具．

同一模型消息中，连续只读调用按 shared 规则并行执行．写入调用按 exclusive 规则等待前面的调用完成．后面的读取等待写入完成，多次写入保持调用顺序．单个工具失败作为对应调用的错误结果返回．

### 5.3 模型、角色与鉴权

设置页提供 `models.yml` 和 `roles.yml` 两个编辑区．项目文件是唯一配置来源，路径为 `data_dir/llm_runtime/`．保存前同时检查两份 YAML．保存失败保留原配置，已运行会话保留创建时配置．

`models.yml` 使用 OMP 的 `providers` 结构．提供方配置地址、API、鉴权与共享 headers．模型配置 ID、输入能力、上下文限制、思考能力与兼容参数．API 协议由已安装 OMP SDK 校验和执行．可调用模型仅限项目显式配置的提供方与模型．

`roles.yml` 使用 `provider/model:level` 和 `@role` 选择器．未填写的角色继承 `default`．省略思考级别时使用 OMP 模型默认策略．无效选择器、角色循环、不支持的思考级别和无图像能力的板书模型均会阻止保存．

以下配置展示两份编辑区的结构．模型 ID、地址与能力须使用提供方的实际值．示例模型声明图像输入，以满足板书角色要求：

```yaml
# models.yml
providers:
  lecture:
    baseUrl: https://provider.example/v1
    api: openai-completions
    auth: apiKey
    models:
      - id: lecture-vision
        name: Lecture Vision
        input: [text, image]
```

```yaml
# roles.yml
default: lecture/lecture-vision
board_recognition: '@default'
note_chunk: '@default'
note_writer: '@default'
note_review_coverage: '@default'
note_review_correctness: '@default'
ask_instant: '@default'
ask_research: '@default'
agent_chat: '@default'
compaction: '@default'
```

提供方密钥通过设置页单独保存．需要独立思考级别时，角色可指定 `lecture/lecture-vision:high`，模型必须声明支持该级别．

| 角色                      | 职责                         |
| ------------------------- | ---------------------------- |
| `board_recognition`       | 板书图像识别与增量提取       |
| `note_chunk`              | 片段来源整理                 |
| `note_writer`             | 整课写作与审阅后修订         |
| `note_review_coverage`    | 独立审阅来源覆盖             |
| `note_review_correctness` | 独立审阅公式、推导与一致性   |
| `ask_instant`             | 选区即时回答，不注册文件工具 |
| `ask_research`            | 读取授权材料后回答           |
| `agent_chat`              | 交互式文件助手               |
| `compaction`              | SDK 上下文压缩               |

项目共享一个提供方 `AuthStorage`，存储于 `data_dir/llm_runtime/credentials.db`．API key 在设置页按提供方保存．OpenCode 设备登录、刷新和登出使用同一凭据存储，OAuth 凭据保持 OAuth 类型．同一服务的独立账号使用不同 provider ID．无鉴权服务配置 `auth: none`．

`models.yml` 的显式 `apiKey` 按 OMP 规则覆盖存储凭据．设置页只显示凭据状态和来源，不回显密钥．Python 不随笔记任务传输提供方密钥．并发刷新共用提供方刷新锁，失败不会改用其他提供方凭据．

首次使用时在设置页填写 YAML 和提供方凭据．SDK 历史保存在项目数据目录，任务从已保存文稿和来源位置恢复．

### 5.4 取消、预览与恢复

Python 取消任务时，Bun 终止对应会话的模型流和工具执行．子进程意外退出会使受影响请求失败，未完成任务不得记为成功．SDK 会话存储保留模型消息，任务队列保留阶段与来源位置．显式停止输入时，应用取消该课程的 VLM SDK 请求和笔记会话．已保存文稿保留．

每次成功 `edit/write` 后，运行时发送文件变更事件．Python 在 publication lock 内更新可预览文稿并刷新浏览器．生成失败保留已保存文稿和用户问答．

上下文管理沿用 SDK 的令牌估算、缓存、重复工具结果处理与压缩．完整来源始终保存在授权文件中，压缩后仍可读取．分页输出保留继续读取入口，不把截断内容标为完整来源．

整课写作与修订共用一个 SDK 会话．`review_notes` 为两位只读审阅者创建相同来源和文稿快照．报告包含 P0-P3 findings 与 `overall_correctness`、`explanation`、`confidence`．两项整体判断均为 `correct` 才通过；非阻断建议不要求清空．宿主将通过结果绑定到文稿摘要，修改后必须复审．会话提前停止时，宿主拒绝完成并要求继续；取消、错误或达到配置上限不得发布．Python 负责输入调度、预览和发布，不再维护独立修订循环．

笔记操作分为生成、停止、删除．没开始时仅可生成，正在生成时仅可停止，生成结束时仅可删除．停止保留输出，删除后回到没开始．全局任务队列汇总内存中的排队与运行任务，支持停止笔记任务；重启不恢复任务．

笔记执行视图顶部为紧凑状态栏，下拉选择主写作、覆盖审阅或正确性审阅．右侧显示模型与思考深度、SDK 上下文用量、LLM 耗时、工具耗时及输出速率．下方按顺序显示可折叠的模型思考、正文、工具调用与结构化审阅报告．工具参数生成期间同步显示已有参数，write/edit 折叠预览修改内容，grep/glob 折叠预览结果，展开后显示完整内容；read 只显示路径与行范围，不显示读取正文．模型思考默认压缩为一行，展开显示服务商提供的完整思考内容．

日志记录 role、provider、model 与思考级别．用量按 `provider/model` 统计，避免相同模型 ID 在不同提供方之间混合．日志与 UI 隐藏密钥、敏感 headers、Authorization 和 OAuth token．

## 6. UI 层

### 6.1 布局模型

- 左侧边栏：多 tab；每个 tab 下有 toolbox（工具项列表）。
- 右侧边栏：多 tab；每个 tab 下有 toolbox。
- 两侧边栏都可以**整栏收起/展开**（顶栏的 ◧ / ◨ 按钮，状态存 localStorage）；收起后中央区域占满宽度。
- 中间：pages 区域，顶部 page tabs，下面是当前 page 内容。
- 页面内容由 block 列表组成，前端渲染。

```python
class UIManager:
    def add_tab(self, side: Literal["left","right"], id, title, icon=None, order=0) -> TabHandle
    def add_toolbox_item(self, tab_id, id, title, *, render=None, order=0, description="") -> ItemHandle
    def add_page(self, id, title, *, render, icon=None, order=0, closable=True) -> PageHandle
    def remove_page / remove_item / remove_tab
    def open_page(self, id) -> None
    def close_page(self, id) -> None
    def refresh(self, *, reason="") -> None       # 节流 + ui.changed
    def register_action(self, action_id, handler, *, plugin_id) -> None
    def dispatch_action(self, action_id, payload) -> ActionResult
    def state(self) -> dict
```

### 6.2 Block 类型

```python
{"type": "markdown", "text": "..."}
{"type": "html", "html": "..."}                  # 仅可信插件
{"type": "text", "text": "..."}
{"type": "keyvalue", "items": [[k, v], ...]}
{"type": "table", "columns": [...], "rows": [[...]], "actions": [...]}
{"type": "list", "items": [...]}
{"type": "buttons", "buttons": [{"label","action","value","style"}]}
{"type": "form", "fields": [{"name","label","type","value","options","placeholder","secret"}], "submit": {"label","action"}}
{"type": "log", "lines": [...]}
{"type": "image", "src": "/api/artifacts/...", "alt": "..."}
{"type": "video", "src": "..."}
{"type": "tabs", "tabs": [{"title","blocks": [...], "meta": {...}}]}
{"type": "progress", "value": 0.5, "label": "..."}
{"type": "switch", "label": "实时总结", "checked": true, "action": "instant-summarize.toggle", "hint": "..."}
{"type": "qa", "id": "qa-1a2b3c4d", "ts": "...", "status": "done|pending|error",
 "question": "...", "answer": "...", "question_html": "...", "answer_html": "...",
 "path": "...", "delete_action": "reader.qa_delete"}
```

- `switch`：布尔开关，切换后以 `{"enabled": <bool>}` 调用它的 `action`。
- `qa`：笔记里的内联问答对，渲染成可折叠的独立组件（提问 / 回答两侧配色不同），带一键删除按钮。
- `tabs[*].meta`：reader 用它告诉前端当前文档属于谁、选中菜单有哪些项（见 7.10）。

- `render` 回调返回 block 列表（或 `PageContent`）。
- Markdown 在服务端用 `markdown-it-py` 渲染；`$...$` / `$$...$$` 保留给前端 KaTeX。
- 前端不执行插件代码；只处理 `action`。

### 6.3 Action 协议

```json
POST /api/ui/action
{
  "action": "input.play_recording",
  "page_id": "input:course:93578",
  "item_id": null,
  "values": {"course_id": 93578, "video_id": "211449"}
}
```

响应：

```json
{"ok": true, "message": "已开始播放", "refresh": true, "open_page": "input:session"}
```

- 长任务（LLM、下载、网络请求）在后台线程执行；action 立即返回，通过 `ui.changed` 刷新。
- 插件 action 处理器必须捕获异常并返回 `{"ok": false, "message": ...}`。

### 6.4 流控与并发处理

- `core/streams.py` 的 `BacklogGate` 给 `audio`/`video` 各挂一道闸门：reader 在 append 前 `wait(已摄录时长)`，消费者提交窗口后 `release(窗口结束时间)`。录播全速解码时，未处理媒体被限制在 `窗口 × (并发 + 4)` 以内（1.5h 课程不再把 5GB 帧塞进内存）；直播闸门 limit=0，不生效。
- 闸门有 stall 看门狗（默认 300 s）：消费者挂死时自动放行并告警，避免 reader 永久阻塞。
- 结束屏障：两个 ffmpeg reader 都 EOF（`session.input_done`）→ input 等待 `stt.busy()`/`vlm.busy()` 变 False → 才 `end_session`；`summarize` 在 `session.stopped` 上请求完整笔记．直播与录播均支持分段笔记．

### 6.5 Web 服务

- `GET /`：静态前端。
- `GET /api/ui/state`：完整 UI 状态 + 运行时状态。
- `POST /api/ui/action`：执行 action。
- `GET /api/artifacts/{path}`：受限于 data_dir 的静态文件（图片、笔记）。
- `GET /api/media?src=...`：视频转发代理。只允许 `*.sjtu.edu.cn` 的 URL，透传 `Range`/`Content-Range`，浏览器不必能直连 `live.sjtu.edu.cn`（VPN 场景）；HLS 播放列表不走代理。
- `WS /ws`：服务端推送 `{"type":"refresh","reason":...}` 与 `{"type":"notify","topic","payload"}`（流式增量用，见 7.10）。
- 前端每 3 秒兜底轮询 state；WebSocket 断开自动重连。
- **登录**：所有 HTTP 路由与 `/ws` 都在签名 cookie 之后。`GET/POST /login` 是唯一公开路由，`POST /logout` 清 cookie；未登录时 `/api/*` 返回 401、页面 303 到 `/login`，WebSocket 直接 4401 关闭。
- 账号在 `data_dir/auth.json`：`users`（`password` 明文或 `password_hash` = `scrypt$salt$hash`）、`session_secret`、`session_ttl_hours`（默认 168）。文件不存在或没有用户时自动创建 `admin` + 随机密码，并把密码打印到日志一次；连续 5 次密码错误锁定该用户名 5 分钟。cookie 为 HttpOnly + SameSite=Lax，`X-Forwarded-Proto: https` 或 HTTPS 下自动加 Secure。

## 7. 内置插件

### 7.1 settings

- 服务名：`settings`。
- 设置项注册：

```python
SettingsSpec(
    key="my.api_key",
    label="API Key",
    type="secret",
    default="",
    group="My Plugin",
    description="...",
)
```

- 类型：`string`, `text`, `int`, `float`, `bool`, `select`, `secret`, `json`。
- 持久化到 `data_dir/settings.json`（原子写，权限 0600）。
- `get(key)` 返回真实值；前端表单不会收到 secret 真实值，只显示「已配置（N 字符）」；清除走 `settings.clear_secret`。`public_all()` 对 secret 返回 `"******"` 并带 `is_set`。
- 变更时发 `settings.changed`。
- UI：没有侧边栏 tab，只有一个设置 page（`settings:main`），从菜单栏 / 命令面板（Ctrl+P）打开；page 内按 group 分 tab 渲染表单，action `settings.save`。只有满足 `visible_when` 的字段才会出现。

### 7.2 llm（运行时宿主插件）

- 服务名为 `llm`，Python `LLMService` 将角色请求转发给项目 Bun 运行时．
- 设置页显示两份 YAML、有效角色配置和提供方凭据状态．
- 负责运行时生命周期、配置保存、凭据保存、OpenCode 登录与登出．
- Python 不注册 provider，也不执行模型工具循环．单次生成与会话共用角色解析和鉴权．

### 7.3 input（输入插件）

- 服务名：`input`。
- 设置项：Canvas token/base URL、SJTU 视频门户、音频采样率、视频 fps/宽度、本地保留录像节数（`input.keep_recordings`）、直播设备选择。
- 处理模式由课程类型决定：**直播 = 1:1 实时**，**录播 = 全速**（无 `-re`，无变速）；`session.meta["mode"]` 记录 `live`/`fast`。
- 依赖 `settings`；使用 vendored `integrations/sjtu_canvas_assistant` 的 Canvas/VOD 能力；直播使用新版门户端点：
  - `GET {portal}/v1/vod_live`（时间范围直播列表）
  - `GET {portal}/v1/course_vod_videoinfos?courseId=...&playType=2`（FLV 设备流）
- 录播通道：
  - Canvas 课程列表 -> SJTU `list_course_videos` -> `get_video_info` -> `get_stream_url`。
  - 本机文件 / HTTP URL 也可作为录播源。
- 直播通道：
  - 今日/近期直播列表 -> 选择设备（老师/课件）-> FLV URL。
  - 也支持手动 HLS/RTMP/HTTP URL。
- 录播先**下载到本地**（`sessions/<sid>/media/<name>.mp4`，`.part` → 原子重命名）再交给 ffmpeg，避免音频/视频两个进程各拉一遍远端；下载失败自动回退为直读远端。本地文件同时用于回看（`/api/artifacts/<sid>/media/<name>`，Starlette `FileResponse` 原生支持 Range）。
- `FFmpegSource` 同时起两个 ffmpeg 子进程：
  - 音频：`-vn -ac 1 -ar 16000 -f s16le pipe:1`
  - 视频：`-an -vf fps=4 -f image2pipe -vcodec mjpeg pipe:1`（默认保持原分辨率）
  - 录播全速解码（无 `-re`）；直播加低延迟参数（天然 1:1）。
  - 解析 JPEG SOI/EOI 边界，逐帧 `append_frame`。
  - 音频、视频两个 reader **都 EOF** 才发 `input.ended` 并置 `session.input_done`；否则全速模式下音频先结束会截断视频与 VLM 结果。
- 一个 `SourceManager` 保证同一时刻最多一个源；支持 pause/resume/stop/seek（seek 仅录播/本地文件）。
- UI：
  - 左侧只有一个可折叠的**课程树**（`tree` block）：课程 → 某次课（录播与直播同一条路径）。
    - 相邻的连续 2–3 节录播自动合并成一节，节点上标注「2 节」；同一门课连排的多节直播也按同样规则合并（间隔不超过 `input.max_gap_minutes`、同日、最多 3 节），展开时跳到「正在直播 / 下一节 / 最后一节」那一节的地址；
    - 正在直播的课程/节次带 `直播中` 徽章并默认展开，一键进入直播；每节课的状态由 begin/end 推导，树里记录最近的边界时刻，到点在时钟 tick 上自动重取课程树（不用手点「刷新」）；
    - 展开状态保存在插件内存，`input.toggle_course` 切换。
    - 课程名先显示，各课程回放列表完成后分别更新；直播查询复用回放授权．同次课的多节视频地址并行解析．
  - 右侧 tab「本节课」：toolbox「处理控制」。
  - 页面 `input:session`（标题「课堂」）：标题带 `直播`/`录播` 徽章。
    - **直播**：`player` block 并排播放两路视频——「课堂」（`viewNum=1`，黑板/讲台）与「屏幕」（`viewNum=2`，大屏共享）；没有第二路时显示占位。
    - **录播**：不渲染播放器，只显示下载/处理进度卡片（`progress` block）与「打开录像」链接（本地 mp4，新窗口播放），避免每秒进度刷新重建 `<video>` 打断播放。
    - 下载进度按整次课汇总各段字节数，包含预下载．全部下载完成后保留完成状态．输入段索引用于顺序连接媒体时间轴．
    - 前端按 URL 后缀选择播放器：`.m3u8` → hls.js（Safari 用原生 HLS），`.flv` → mpegts.js，其余交给原生 `<video>`。
    - 除 `.m3u8` 外的媒体地址一律改写成 `/api/media?src=...`，由服务端转发；浏览器直连失败时 pane 里会显示红色提示。
    - 屏幕流默认静音，避免两路音频叠加。
  - 点击「课程 → 某次课」只**选中并加载**视频地址（`input.open_class`），不创建会话、不启动 ffmpeg、不自动播放。
  - 「开始处理」按钮（`input.start_processing`）才开始摄取与会话；默认暂停。启动在后台线程里完成，下载几分钟也不会阻塞 HTTP 请求。
  - 「暂停处理」（`input.pause_processing`）：录播暂停 reader（管道反压让 ffmpeg 自动停下）；直播停止摄取但页面继续播放直播画面，恢复时从直播边缘继续。
  - 录播自然播完由 `input.ended` 驱动：先等待 STT/VLM 把队列排空（`_wait_for_consumers`），再切下一节或 `end_session("playback finished")`，保证总结时所有窗口都已提交。
  - 处理完成后按 `input.keep_recordings` 只保留最近 N 节的 `media/` 目录，其余删除（笔记不受影响）．
  - 页面 `input:manual`：手动 URL（调试，仅从命令面板打开）。
- 行为层只看到 `hub.current.audio/video`，看不到 Canvas/SJTU/ffmpeg 对象。

### 7.4 stt（语音识别插件）

- 服务名：`stt`；依赖 `settings`；公开 `status()`、`transcribe()`、`enabled()`、`busy()`、`flushing()`．
- 协议：自动转写统一使用 WebSocket `/v1/stream`，录播选择 offline 模式，直播选择 live 模式．手动范围转写可使用 HTTP．服务端独占声学切分、上下文和文本合并．
- 设置：`enabled=true`、`server_url=http://127.0.0.1:8766`、`language=Chinese`、`context=''`、`timeout=60`、`batch_seconds=120`、`concurrency=1`．地址可由 `STT_SERVER_URL` 初始化．
- 直播持续发送最多 200 ms PCM，发送与接收独立运行．partial 替换临时文字，final 才写入永久转写．结束和 reset 完成不足 1 秒的尾音．
- 录播连续发送 PCM．服务端有界并发识别并按时间顺序提交；应用限制未确认的发送量，并负责覆盖记录与持久化．
- accepted 允许继续发送音频；成功 final/progress 才更新覆盖并释放已处理音频．发送、接收和成功处理位置分别记录．
- 覆盖空洞按精确区间补录．断线重传未提交范围，重试耗尽的范围保持未覆盖并报告错误．异常重复标记不终止整课．
- 禁用 STT 时输入只推进逻辑音频时间，不保留 PCM 或插入静音．重新启用后从真实 PCM 范围恢复．
- **持久化**：定稿追加到 `sessions/<sid>/transcript.jsonl`（`start`、`end`、`text`），summarize 读取该文件重建笔记材料．
- **覆盖记录**：每次处理完一个区间就往 `sessions/<sid>/coverage.json` 记一段已处理音频；再次跑同一节课时这些区间会被跳过，直播结束后补跑录播也只会补缺口。
- 事件：`stt.partial`、`stt.segment`、`stt.error`．
- UI：仅调试 page（`stt:*`），不出现在侧边栏，只能从命令面板（调试 → 语音转写）打开；page 显示状态、模型、音频时长、已转写段数/字数与最近转写文本。

### 7.5 vlm（板书识别插件）

- 服务名：`vlm`。
- 设置项：enabled（默认 true）、window_seconds（默认 20，窗口以采样点为中心，前后各 10 s）、cadence_live_seconds（直播采样间隔，默认 180）、cadence_recording_seconds（录播采样间隔，默认 300）、history_windows（默认 3）、fps、max_frames（默认 12）、fusion、dedupe_threshold、完整提示词 prompt．模型与思考级别由 `board_recognition` 角色选择，凭据由提供方 `AuthStorage` 管理．
- 工作线程：读 `video`，窗口内取帧 -> 融合 -> VLM -> 追加 `visual`。
- **采样节奏**：直播每 3 分钟取一个 20 秒窗口，录播每 5 分钟取一个 20 秒窗口；窗口之间的视频帧不再需要，工作线程会边等边 `trim`，避免把整堂课解码后的 MJPEG 留在内存里。
- **顺序**：识别请求按窗口顺序执行，前一个窗口提交后再读取历史．`vlm.prepare_ahead=1` 在识别期间准备下一个窗口的图像；设为 0 则顺序准备．
- 融合算法来自 `picture_parser`：
  1. 解码 JPEG -> 灰度（保持原始分辨率，不裁剪、不缩放）；
  2. FFT 相位相关配准（可选）；
  3. 30 分位背景 + 时序活动/方差 + 空间梯度 + 粉笔候选掩码；
  4. 时序持续性 + 单调边缘滤波；
  5. 候选帧鲁棒中位数融合。
- 窗口默认 20 s / 4 fps、每个窗口最多 12 帧；整帧原始分辨率输出，不做裁剪或缩放。
- **VLM 只做观察者**：prompt 要求只转录图中真实可见的内容，不推断、不补全、不纠正，看不清的字符写作 `□`；空黑板回复 `【黑板暂无板书】`。
- **增量转录**：最近 `history_windows` 次转录以纯文本放进 prompt（不附加历史图片）；模型只输出相对最近一次**新增 / 修改 / 擦除**的内容，完全一致时回复 `【无变化】`，插件直接跳过不写入。这样 `visual` 里保存的是板书变更日志，而不是每 20 秒重录一遍整块黑板。
- 去重：与上一条 visual 文本相似度高则跳过（`difflib.SequenceMatcher`，阈值默认 0.92）；输出后处理会折叠连续重复的公式块。
- 保存融合图到 `data_dir/sessions/<session>/vlm/`，UI 可预览。
- **持久化与覆盖记录**：同 STT——每条 visual 追加到 `sessions/<sid>/visual.jsonl`，已采样区间写入 `coverage.json`（`kind="video"`），补跑时跳过已处理区间并在等待期间 `trim` 掉不需要的帧。
- UI 按窗口/段落返回 markdown 块，KaTeX 逐块渲染，避免整页卡死。
- 事件：`vlm.segment`、`vlm.error`。

### 7.6 llm-agent

- 服务名为 `agent`（`AgentService`）．Python 管理应用会话标识、文件授权、任务状态和 UI 事件．
- `AgentProfile` 声明职责提示、角色、允许的工具与步数．`AgentTool` 只选择工具名，不修改上游参数 schema．
- `create_session` 接受 profile 或直接声明 `role`、`system_prompt`、`files/workspace`、`tools` 和 `meta`．默认交互角色为 `agent_chat`．
- `FileWorkspace` 将逻辑文件名映射到实际路径，并声明 `readonly` 文件．SDK 工具只接触会话的授权工作区．
- `run(session_id, input, on_event=...)` 将任务转发给 SDK．`cancel`、`reset` 和 `close` 管理实际运行时会话．每个会话独立加锁，禁止同一会话并发运行．
- SDK 保留完整模型历史和工具状态．Python 显示记录不作为后续模型请求的历史来源．
- 事件包含 `agent.run.started`、`agent.delta`、`agent.reasoning`、`agent.message`、`agent.tool_call`、`agent.tool_result`、`agent.file`、`agent.run.finished` 和 `agent.error`．
- Agent 页面 `agent:chat` 从菜单栏或命令面板打开．`agent.chat` 在后台转发请求，页面显示流式内容与工具结果．

### 7.7 notes-pipeline 与 summarize

- `notes-pipeline` 依赖 `settings`、`llm-agent`，可选依赖 `stt`、`vlm`．统一管理所有课程的片段、统稿、修改和审阅任务．
- `notes.chunk_seconds` 设置核心区间长度，`notes.overlap_seconds` 设置左侧原文上下文，`notes.concurrency` 限制同时运行的笔记 agent 数量．
- 来源通过 `stream.sealed` 报告完成位置．所有启用来源完成核心区间后，才能冻结材料并整理片段．按记录起点归属核心区间，跨边界记录完整保留．
- 每个片段保存只读来源、上下文、板书历史及 3 份私有底稿．每次成功写入都更新生成预览，连续完成的片段发布为未审阅笔记．失败片段保留范围错误，完整原文仍交给整课写作者．
- 片段任务失败后记录错误，不创建重试任务或 attempt 目录．整课写作失败后结束任务．
- 来源终止后，整课写作者读取完整来源和成功底稿，统一结构、符号与跨段推导．随后来源覆盖和正确性审阅者并发只读审阅．任一失败则由同一写作者修改，再执行两项审阅．
- 审阅全部通过才标记通过．关闭审阅时标记未审阅．生成失败保留已有公开笔记．来源缺口与审阅结果分别记录．
- 公开文件为 `notes/note_knowledge.md`、`notes/note_gossip.md`、`notes/note_notice.md`，对应来源为会话目录的 `lecture_source.md`．发布锁保护整组文稿与来源快照．用户问答保留原始块内容和顺序．
- `summarize` 依赖 `settings`、`notes-pipeline`、`reader`，负责笔记页面和课后策略．`direct` 登记生成请求，`fill` 查询录播并请求补录，`skip` 不自动生成完整笔记．手动请求会等待来源终止．
- 各阶段通过 `note_chunk`、`note_writer` 和两项 `note_review_*` 角色选择模型与思考级别．Python 队列保留阶段和来源位置，任务直接使用常驻 Bun 服务，不启动独立 Python 模型 worker．

### 7.8 ask（笔记内问答）

- 依赖：`settings`、`llm-agent`、`reader`；服务名 `ask`。
- 选中菜单：向 reader 注册**全局**菜单项「💬 提问」（不绑定单个页面 / owner，任何笔记页都能用）；弹窗里选择强度：**快速**（instant）或**研究**（research）。
- 两种强度都包含选中位置前后上下文、选中片段与历史问答．模型和思考级别分别由 `ask_instant` 与 `ask_research` 选择．
  - **快速**：不注册文件工具，直接生成回答．
  - **研究**：只注册原生 `read`、`grep` 和 `glob`，可读取完整授权材料．不强制固定搜索顺序，不使用 Python 行数截断．
- 流程：
  - reader 的确定性写入器先在选中段落下方插入 `pending` 问答块（meta 里带 `intensity`），用户立即看到「思考中」；
  - 后台线程通过 agent 插件创建职责会话；`agent.delta`、`agent.reasoning` 和工具调用转发为 `qa.live` 通知．前端在问答块内更新内容，不刷新整页．
  - 结束时 reader 把最终回答写回问答块，meta 里带 `collapsed=true`（自动折叠）与 `status`；前端收到最终 `qa.live` 后原地替换为渲染好的 HTML 并折叠；
  - 失败时 `status=error`，块保持展开显示原因。
- **折叠持久化**：折叠状态记录在问答块 meta 的 `collapsed` 字段里；前端点折叠按钮时本地切换并调用 `reader.qa_collapse`（`refresh=false`，不触发重渲染），状态随笔记文件保存。
- **流式期间的滚动**：问答块在视口上方时，前端在每次内容增长后按高度差补偿 `scrollTop`，保证屏幕内其它内容不动；整页重渲染（轮询）时也按同样的高度差补偿。
- 日志：每一步的耗时、工具调用参数、可见正文，以及**完整的思考（reasoning）文本**都会写入日志，不截断。

### 7.9 instant-summarize（分段笔记）

- 依赖 `settings`、`notes-pipeline`、`reader`，负责分段预览页面和开关．直播与录播均支持分段整理．
- `instant-summarize.enabled=false` 关闭主动片段整理与预览发布，来源日志继续保存．整课请求仍使用完整来源．
- 分段整理使用 `note_chunk` 角色．任务失败保留私有输出、来源位置与错误，恢复时继续使用完整来源．
- 公开文件为 `notes/instant_note_knowledge.md`、`notes/instant_note_gossip.md`、`notes/instant_note_notice.md`．页面标记未审阅并显示缺口．
- reader 页面键为 `<sid>:instant-summarize`，完整笔记页面键为 `<sid>:summarize`．两类页面可以同时打开．

### 7.10 reader（文稿阅读）

- 依赖：无（内置插件，始终加载）。
- 服务：`reader.show(title, docs=[{key,label,path}], active=...)`。
- 页面：每个笔记会话一个独立页 `reader:notes:<session_id>`（`reader:main` 作为无会话时的兜底），按文档分 tab 渲染 Markdown（`split_markdown` 分块 + KaTeX）．多门课的笔记可以同时打开、各自独立．
- 实时刷新：成功 `edit/write` 发出 `agent.file` 事件．应用在发布锁内更新预览并刷新 UI，浏览器在生成期间可读取已保存内容．
- summarize / instant-summarize 的「笔记」工具箱按钮调用各自 action → `reader.show(..., page_key=<notes session>, owner=<plugin id>)`，每门课一个页；不再所有课程共用一页。
- **选中菜单**：用户选中页面正文后会浮出一个小菜单，项由 `reader.register_selection_action(page, item, owner=...)` 注册。`page` 可以是页面 id / page key（按 owner 过滤，只在对应插件提供的文档上出现），也可以是空串 / `"*"`（全局项，任何笔记页都出现）。`ask` 用全局注册提供「💬 提问」。item 形如 `{id,label,icon,kind:"prompt",action,placeholder,intensities}`；`kind="prompt"` 时前端先弹输入框（含强度选择）再连同选中文本一起 post 给插件 action。
- **内联问答块（qa）**：问答对直接写进笔记文件，用一对 HTML 注释包住：`<!-- qa:begin {"id","ts","status","intensity","collapsed"} -->` … `<!-- qa:end -->`，中间是 `**❓ 提问（时间）**`、问题、`**🤖 回答**`、回答（都是 Markdown）。普通 Markdown 阅读器里问题和回答照常可见，只是标记隐藏；reader 把它从 Markdown 流里摘出来渲染成独立组件（两侧不同配色、可折叠、一键删除）。`collapsed` 就是折叠状态的持久化位置（`reader.qa_collapse` 负责改它，`refresh=false`，不触发重渲染）；删除由 `reader.qa_delete` 做确定性的文本切除。reader 还负责把浏览器选中的渲染文本定位回原始 Markdown 段落（精确 → 去空白/去标记 → 行前缀逐级回退），定位失败才附到文末并提示。
- **流式问答**：问答进行中，`ask` 通过 `ui.notify("qa.live", ...)` 把正文/思考增量、工具调用和最终 HTML 推到浏览器；前端按 `data-qa-id` / `data-qa-path` 原地 patch 该块，不做整页刷新。页面轮询命中同一条消息时，key 里忽略 `collapsed`，折叠切换不会触发重渲染。

```text
data/agent/
├── settings.json                 # 设置（0600）
├── auth.json                     # 登录账号 + 会话签名密钥（0600）
├── llm_runtime/
│   ├── models.yml                # 提供方与模型目录
│   ├── roles.yml                 # 职责选择器
│   ├── credentials.db            # OMP AuthStorage
│   ├── sessions/                 # SDK 持久会话
│   └── workspaces/               # 会话授权文件
├── sessions/
│   └── 20261101-101500-ab12/
│       ├── session.json          # 元信息
│       ├── coverage.json         # 已处理的音频/视频时间区间（断点续跑与补录）
│       ├── transcript.jsonl      # STT 已提交的转写行（每行一条）
│       ├── visual.jsonl          # VLM 已提交的板书增量
│       ├── media/                # 录播本地副本（只保留最近 N 节）
│       ├── notes/
│       │   ├── note_knowledge.md
│       │   ├── note_gossip.md
│       │   ├── note_notice.md
│       │   ├── instant_note_knowledge.md
│       │   ├── instant_note_gossip.md
│       │   ├── instant_note_notice.md
│       │   └── lecture_source.md      # 笔记材料（按时间合并的转写 + 板书）
│       ├── vlm/
│       │   └── 000030-000060.jpg
│       ├── stt/
│       │   └── ...
│       └── events.jsonl          # 可选审计日志
└── cache/
    └── courses.json
```

- 流本身默认不落盘；`transcript.jsonl` / `visual.jsonl` 是转写与板书的持久化副本，进程崩溃或提前结束后重新生成笔记仍然有材料可用．
- 每次会话目录由 `SessionStore` 创建；插件通过 `ctx.data_dir / "sessions" / session_id` 写入自己的产物。
- 旧 `data/outputs`、`data/notes` 保留，不被新架构修改。

## 9. 并发模型

- 事件总线：单 dispatcher 线程，顺序分发。
- 时钟：独立线程，每秒 emit。
- 输入源：每个源两个 ffmpeg 子进程 + 两个读线程（音频/视频）。
- STT/VLM：各自一个 worker 线程，按游标消费流。
- Agent：Python 转发线程连接同一常驻 Bun 进程．多个 SDK 会话异步运行，每个会话有独立锁和工具状态．
- 笔记：Python 队列按 `notes.concurrency` 分派任务．core 按 shared/exclusive 规则执行同轮工具．
- Web：FastAPI/uvicorn 线程池处理 action；UI 状态有锁。
- 所有跨线程共享状态用 `threading.RLock`；流读写用锁 + 不可变 chunk 列表。
- 退出：先停输入源，再取消分析和笔记任务．随后关闭 SDK 会话与 Bun 子进程，最后停止时钟和事件总线．

## 10. 错误处理与可观测性

- 每个插件有独立 logger：`lecture_agent.plugin.<id>`。
- 结构化日志：`event`, `plugin`, `session_id`, `error`, `elapsed_ms`。
- 事件 `*.error` 可在 UI 状态栏查看。
- LLM 错误分类：认证、限流、超时、服务端、协议解析、流中断。
- 输入源断开：直播自动重连（指数退避，最大次数可配）；录播结束发 `input.status`。
- Agent 工具错误由 SDK 作为对应调用的结果返回，模型可据此恢复．运行时退出与取消均向调用方返回失败终态．

## 11. 安全与隐私

- Canvas 等插件密钥保存在 `settings.json`（0600）．模型提供方凭据保存在项目 `AuthStorage`，UI 只显示状态．
- 日志不打印 token、URL query 中的签名；使用 vendored `integrations/sjtu_canvas_assistant` 的脱敏工具。
- 文件工具限制在会话 workspace，拒绝 `..` 和绝对路径。
- artifact 路由只允许 `data_dir` 下的路径。
- UI 默认只监听 `127.0.0.1`；`--host 0.0.0.0` 时仍受 `auth.json` 登录保护（HTTP + WebSocket 同一道闸门）。
- 单元测试不访问外网；生产代码不包含 mock provider / mock STT 后端。

## 12. 目录结构

```text
src/                       # 新架构直接位于 src 下，不再有 lecture_agent 子目录
├── __main__.py
├── app.py                 # Runtime 构建 + FastAPI app factory
├── cli.py                 # serve / check
├── config.py              # AppConfig、路径、.env
├── applog.py
├── core/
│   ├── events.py
│   ├── streams.py
│   ├── services.py
│   ├── plugin.py
│   ├── ui.py
│   ├── meta.py
│   └── runtime.py
├── agent_runtime/
│   ├── client.py           # 私有 JSON Lines IPC
│   ├── main.ts             # Bun 入口与项目隔离
│   ├── runtime.ts          # SDK 会话、角色与鉴权
│   ├── workspace.ts        # 文件授权
│   ├── generate.ts         # 单次生成
│   └── opencode.ts         # AuthStorage OAuth 接入
├── llm/
│   ├── types.py            # Python 应用响应类型
│   └── service.py          # 角色生成与用量适配
├── plugins/
│   ├── builtin.py
│   ├── settings/
│   ├── llm_core/
│   ├── input/
│   ├── stt/
│   ├── vlm/
│   ├── agent/
│   ├── summarize/          # 非即时：整堂课一次性整理
│   ├── instant_summarize/  # 即时：增量合并
├── integrations/
│   ├── opencode_go.py     # OpenCode 套餐设备登录
│   ├── sjtu_canvas.py     # 客户端池 + 目录封装
│   └── sjtu_canvas_assistant/  # vendored SJTU Canvas/视频客户端
├── ui/
│   ├── server.py
│   └── static/{index.html,app.js,styles.css}
└── util/
    ├── paths.py
    └── text.py
tests/
docs/
```

## 13. 实施顺序

1. **P0 基础**：streams / events / services / plugin / ui / runtime / config / logging。
2. **P1 运行时**：安装锁定的 OMP SDK，接入 Bun IPC、角色请求与会话生命周期．
3. **P2 行为基础**：settings、模型角色 YAML、提供方 AuthStorage 与授权文件工具．
4. **P3 总结**：片段、整课写作、两项审阅与修订共用 SDK，保留生成期间预览．
5. **P4 输入**：SJTU catalog（VOD + live）/ ffmpeg source / UI pages / tests。
6. **P5 STT/VLM**：兼容 STT 服务接入与音频生命周期；VLM picture_parser 融合、prompt、tests．
7. **P6 UI**：FastAPI + 静态前端 + WebSocket + 端到端演示。
8. **P7 收尾**：README、架构文档、真实 Canvas 冒烟、代码审查。

## 14. 非目标（v1 不做）

- 不做多用户/权限系统。
- 不做视频持久化回放（只保留会话产物）。
- 不做实时字幕的逐字低延迟（按块离线识别）。
- 不做课堂内容自动推送到 Canvas。
- 不依赖旧的 `legacy/sjtu-canvas-assistant`；输入模块使用 vendored 客户端。
- 旧脚本和旧子模块统一移入 git-ignored 的 `legacy/`；新 VLM 插件复用其算法，但运行时不依赖 `legacy/`。

## 15. 验收标准

- 确定性测试覆盖流、事件、插件、配置保存、角色解析、鉴权、IPC、工具授权、取消、恢复和预览．
- `sjtu-skipper check` 输出正确的拓扑加载顺序。
- `sjtu-skipper serve` 能加载真实 settings；输入插件能列出 Canvas 课程与 SJTU 录播/直播（有 token 时）。
- 输入、STT、VLM 三个模块除流接口外没有互相 import；行为插件没有 import 输入/STT/VLM 实现。
- 所有 LLM 请求通过项目 OMP 运行时；STT 只访问配置的兼容 HTTP/WebSocket 服务．
- 实际请求和工具执行验证用户全局配置隔离、同轮读读写读顺序与独立提供方凭据．
- 真实模型记录完整来源访问、工具错误恢复、生成期间预览、审阅修订和最终文稿．
