# 插件开发 API

插件通过 `PluginContext` 访问流、事件、服务和界面．

## 最小插件

```python
from core.plugin import Plugin, PluginContext
from core.ui import Side


class MyPlugin(Plugin):
    id = "my"
    requires = ("settings", "llm")   # 插件 id，不是服务名

    def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.settings = ctx.require("settings")
        self.llm = ctx.llm            # 项目 OMP 运行时

        ctx.ui.add_tab(Side.RIGHT, "my-tab", "我的工具", icon="✦")
        ctx.ui.add_toolbox_item(
            "my-tab", "my-item", "快捷操作",
            render=lambda: [{"type": "text", "text": "hello"}],
        )
        ctx.ui.add_page("my:page", "我的页面", render=self.render_page)
        ctx.ui.register_action("my.action", self.on_action)
        ctx.events.on("clock.tick", self.on_tick, plugin_id=self.id)

    def render_page(self):
        return [{"type": "markdown", "text": "# 标题\n\n$E=mc^2$"}]

    def on_action(self, payload):
        return {"ok": True, "message": "done"}

    def on_tick(self, event):
        pass

    def teardown(self) -> None:
        pass
```

## 流

```python
streams = ctx.streams.current
audio = streams.audio
video = streams.video
transcript = streams.transcript
visual = streams.visual

audio.duration          # 精确秒数
len(audio)              # 整秒（Python len 协议）
audio.read(0, 10)       # PCM s16le
audio.read_as_wav(0, 10)
audio.wait_for(10, timeout=2)

video.duration
video.read_frames(0, 30)  # list[VideoFrame(index,start,end,data)]
video.read(0, 30)         # 拼接 JPEG

len(transcript)           # 片段数
transcript[0].text
transcript.text_since(10) # 从索引 10 开始的拼接文本
transcript.char_count_since(10)
transcript.snapshot_index()
transcript.append("文字", start=1.0, end=2.0, source="stt")
```

规则：

- 追加片段后，片段内容不可变．音频流可丢弃 PCM 并推进时间轴．`audio.duration` 表示时间轴终点，保留区间由 `audio.buffered_ranges()` 返回．
- 输入插件启动会话时调用 `ctx.streams.start_session(meta=...)`．
- 音视频 `stream.appended` 按媒体时长合并通知，默认间隔分别为 0.5 秒和 1 秒．非空文字每次 append 都通知．

## 事件

```python
ctx.events.on("my.event", handler, plugin_id=ctx.plugin_id, priority=0)
ctx.events.once("my.event", handler)
ctx.events.define("my.event", "说明")
ctx.events.emit("my.event", value=1)
```

内置事件：

- `clock.tick`
- `session.started` / `session.stopped`
- `stream.appended` / `stream.sealed`
- `settings.changed`
- `ui.changed`
- `agent.run.started` / `agent.run.ready` / `agent.delta` / `agent.reasoning` / `agent.message` / `agent.tool_call` / `agent.tool_result` / `agent.file` / `agent.execution` / `agent.reviewer` / `agent.run.finished` / `agent.error`
- `vlm.segment` / `vlm.error`
- `stt.partial` / `stt.segment` / `stt.anomaly` / `stt.error`
- `input.status` / `input.error`

## 服务互调

```python
settings = ctx.require("settings")
settings.get("my.enabled")

summary = ctx.require("summarize")
summary.start(session_id)
# 等价的注册表调用
ctx.services.call("summarize", "start", session_id)

# 提供服务的插件声明 provides = ("my.service",)
ctx.register_service("my.service", my_object)
```

约定：

- 服务名包括 `settings`、`llm`、`agent`、`input`、`stt`、`vlm`、`notes-pipeline`、`summarize`、`instant-summarize` 和 `reader`．
- `stt.status()` 返回提供方、服务地址/端点、模型、语言、会话 ID、音频时长、处理游标、覆盖时长、片段数、临时文字和失败区间等状态．
- `stt.transcribe(start=None, end=None)` 同步处理保留 PCM 中尚未覆盖的区间．`start` 缺省为已调度游标，`end` 缺省为 `start + 120` 秒，并限制在音频终点内；区间按提供方单次请求上限切分（兼容服务 120 秒，Google v2 55 秒）．调用会追加转写、记录覆盖并推进已调度游标，返回 `ok/start/end/segments/chars/items`．
- `stt.reset(session_id)`、`vlm.reset(session_id)` 清理已停止会话的识别缓存，不启动任务、不删除文件．调用前必须停止来源并等待消费者退出．`StreamHub.clear_results(session_id)` 清理内存转写、板书和处理进度，保留媒体及停止状态．
- `stt.processing(session_id)`、`vlm.processing(session_id)` 只报告指定处理会话是否仍有识别工作执行，用于删除前等待退出，不等待其他课次的工作．
- 服务调用使用公开服务对象，不导入插件实现．共享类型与枚举（`Side`、`BlockType`、`FormInputType`、`TaskState`、`StreamKind`、`MediaKind`、`SettingType`）从 `core` 导入．
- `requires` 声明必需插件，控制加载顺序和服务访问权限．缺失必需插件时加载失败．`optional_requires` 声明可选插件；调用 `ctx.service(name)` 可取得服务或 `None`．

## LLM

```python
from core.agent_types import IpcMethod

response = ctx.llm.call(
    IpcMethod.GENERATE,
    role="board_recognition",
    messages=[{"role": "user", "content": "描述图中板书"}],
)
response["content"]
response["usage"]

for frame in ctx.llm.stream(IpcMethod.GENERATE, role="board_recognition", messages=messages):
    if frame.get("name") == "llm.delta":
        print(frame["data"]["text"])
```

- `llm` 服务提供项目 OMP 运行时的 `call`、`stream`、`cancel`、`close` 和 `session_transcript`．单次生成、交互会话、配置和鉴权共用该接口．文件任务由 `agent` 服务管理应用状态．
- `call`/`stream` 的方法参数必须是 `IpcMethod` 成员（`generate`、`info`、`configure`、`catalog` 等）．配置读写必须走运行时方法，插件不直接解析或写入 `models.yml`/`roles.yml`．
- `ctx.llm.session_transcript(session_id)` 返回可恢复会话的记录路径．会话仍活动、记录缺失或记录无法恢复时，返回 `None`．插件不能直接读取运行时目录．
- 模型和思考级别来自 `roles.yml`．提供方、模型能力与兼容参数来自 `models.yml`．`api` 的可选值由安装的 OMP SDK 决定．
- `ctx.llm.call(IpcMethod.INFO)` 返回角色、提供方状态和用量．Agent 会话创建时固定配置．
- `IpcMethod.CATALOG` 返回结构化配置（提供方、模型能力、角色选择器、原始 YAML）．`PROVIDER_SET`、`PROVIDER_REMOVE`、`MODELS_SET`、`ROLE_SET` 提交结构化修改，校验通过才写盘并返回新的 catalog．`DISCOVER` 请求端点 `${baseUrl}/models`，返回归一化后的候选模型，不修改配置．
- `stt` 通过提供方适配器使用独立协议：兼容 HTTP/WebSocket 服务，或 Google Cloud Speech-to-Text v2 REST．

## UI

### Google 风格主题

应用和登录页加载独立的 `src/ui/static/theme.css` 与 `styles.css`，不使用组件库．

- 界面采用 Google 风格的蓝色主色、圆角按钮、浅色分层和细边框，随系统明暗偏好切换．
- 主题通过应用自带的 CSS 变量配置配色、尺寸、圆角和焦点．
- 控件与布局由应用维护，按钮按下节规范选择外观与颜色．

### 按钮规范

按钮先区分业务操作与低成本的查看操作，再区分强调程度和风险．应用的按钮样式为 `btn-solid`、`btn-outline` 和 `btn-text`．

| 用途           | 样式                        | 示例                                   |
| -------------- | --------------------------- | -------------------------------------- |
| 主要业务操作   | `btn-solid theme-primary`   | 保存、开始处理、生成笔记、发送         |
| 其他业务操作   | `btn-solid theme-secondary` | 停止、暂停、取消排队、编辑条目         |
| 删除数据或凭据 | `btn-solid theme-danger`    | 删除板书、删除笔记、删除模型、删除凭据 |
| 非业务操作     | `btn-outline`               | 打开课次、切换视图、刷新、查看日志     |
| 低强调辅助控件 | `btn-text theme-secondary`  | 折叠、关闭面板、取消表单               |

- 每个独立操作组最多 1 个主操作，同组按钮使用同一尺寸．表格和重复列表操作使用 `btn-sm`．
- 外观与颜色分别选择．`outline` 不限定颜色：「打开课次」「打开完整笔记」是对应页面的主要入口，使用 `btn-outline theme-primary`；普通刷新和辅助入口可用 `theme-secondary`．
- 颜色不能按按钮文字或 action 名自动推断．声明式控件通过 `class` 或 `button_class` 明确指定例外．
- 通用业务操作默认使用 `solid secondary`，表单提交默认使用 `solid primary`．不能把同组的全部操作都设为主操作．
- 停止、暂停、取消排队保留已有结果，使用 `solid secondary`．删除结果、删除凭据使用 `solid danger`．是否保留结果只决定危险色，不决定按钮外观．
- 不可用操作使用原生 `disabled`，不能只显示灰色．仅图标按钮必须提供 `title` 或 `aria-label`．

### UI 注册与 Block

```python
from core.ui import Side

tab = ctx.ui.add_tab(Side.LEFT, "courses", "课程", icon="▤", order=10)
item = ctx.ui.add_toolbox_item("courses", "courses.list", "我的课程", render=render_list,
                               page_id="input:course:123")
page = ctx.ui.add_page("input:course:123", "课程详情", render=render_page, closable=True)
page.open()
ctx.ui.register_command(
    "input.manual",
    "手动播放",
    description="播放 HLS、FLV、MP4 或本地文件",
    category="调试",
    icon="play-circle",
    page_id="input:manual",
)
ctx.ui.refresh("reason")
# 右侧面板顶部显示课程上下文
ctx.ui.set_context_provider(lambda: {"title": "高等数学", "subtitle": "11-05 录播 处理中",
                                     "state": "busy", "selected": True})
```

Block：

```python
{"type": "markdown", "text": "..."}
{"type": "text", "text": "..."}
{"type": "keyvalue", "items": [["课程", "复分析"], ["日期", "2026-11-01"]]}
{"type": "table", "columns": ["节次", "时间"], "rows": [["第 9 节", "15:55"]]}
# row_actions 为每一行生成按钮；keys 把该行字段并入 action 的 values
{"type": "table", "columns": [{"key": "id", "label": "模型"}], "rows": [{"id": "flash"}],
 "row_actions": [{"label": "删除", "action": "llm.model.remove", "keys": ["id"],
                  "value": {"provider": "opencode-go"}, "class": "btn-outline theme-danger btn-sm"}]}
{"type": "cards", "items": [{"title": "...", "subtitle": "...", "actions": [...]}]}
{"type": "list", "items": [{"text": "...", "action": "x", "value": {...}}]}
{"type": "buttons", "buttons": [{"label": "刷新", "action": "input.refresh"}]}
{"type": "queue", "title": "处理队列", "summary": "处理中 1 个，排队中 0 个", "items": [
    {"id": "lesson-1", "title": "复分析", "detail": "11-16 07:55-09:45 录播",
     "subtitle": "处理中", "progress": "00:20 / 1:49:56 (0%)", "theme": "primary",
     "actions": [{"label": "暂停", "action": "input.queue_pause",
                  "value": {"id": "lesson-1"}, "icon": "pause"}]}]}
{"type": "form", "fields": [
    {"name": "url", "label": "直播 URL", "type": "text", "placeholder": "https://..."}
], "action": "input.play_url", "submit_label": "开始"}
# 字段可选：visible_when 按同表单字段值显隐；rows 指定 textarea 行数；
# type=action 的字段渲染按钮，value 作为按钮数据，button_class 覆盖样式
{"type": "form", "fields": [
    {"name": "auth", "label": "鉴权", "type": "select", "options": ["apiKey", "oauth"]},
    {"name": "key", "label": "API Key", "type": "password",
     "visible_when": {"key": "auth", "values": ["apiKey"]}},
], "action": "llm.provider.save"}
{"type": "log", "lines": [{"time": "12:00", "text": "..."}]}
{"type": "image", "src": "/api/artifacts/<session>/vlm/000030-000060.jpg"}
{"type": "tabs", "tabs": [{"title": "知识", "active": True, "blocks": [...],
                          "meta": {"doc_key": "knowledge", "path": "...", "owner": "summarize",
                                   "page_id": "reader:my-page", "selection_actions": [...]}}]}
{"type": "disclosure", "id": "add-provider", "title": "添加提供方", "blocks": [...]}
# expandable 表格的 rows[].details 为该行的编辑区 blocks
{"type": "table", "id": "models", "expandable": True,
 "columns": [{"key": "model", "label": "模型"}],
 "rows": [{"id": "model-1", "model": "flash", "details": [...]}]}
# editor=True 使用表格下方的编辑区；rows[].id 标识选中行，默认第一行
# hide_single 在只有一个标签时隐藏标签栏；label 用于无障碍名称
{"type": "tabs", "id": "providers", "label": "提供方", "hide_single": True,
 "tabs": [{"id": "local", "title": "local", "blocks": [...]}]}
# schema_form 根据字段约束生成对象表单，提交 context 与 definition
{"type": "schema_form", "id": "provider-local", "title": "连接与凭据",
 "schema": {"type": "object", "properties": {"baseUrl": {"type": "string"}}},
 "value": {"baseUrl": "http://localhost:8080/v1"}, "context": {"provider": "local"},
 "submit": {"action": "llm.definition.save", "label": "保存"}}
{"type": "progress", "value": 0.42, "label": "处理进度"}
# 切换开关时，action 的 values 包含 {"enabled": true/false}
{"type": "switch", "label": "分段笔记", "checked": True,
 "action": "instant-summarize.toggle"}
# 内联问答对（reader 渲染成可折叠、可删除的独立组件）
{"type": "qa", "id": "qa-1a2b3c4d", "ts": "2026-11-10 14:03", "status": "done",
 "intensity": "research", "collapsed": True,
 "question": "...", "answer": "...", "question_html": "...", "answer_html": "...",
 "path": "/data/sessions/<sid>/notes/note_knowledge.md",
 "delete_action": "reader.qa_delete", "collapse_action": "reader.qa_collapse"}
# player：两路视频（m3u8 走 hls.js，flv 走 mpegts.js，其余原生播放）
{"type": "player", "sources": [{"key": "main", "label": "课堂画面", "src": "https://.../main.m3u8"},
                              {"key": "screen", "label": "屏幕画面", "src": "...", "muted": True}],
 "live": False, "processing": False, "finished": False, "queued": False}
```

Action：

```python
ctx.ui.register_action("my.action", handler)

def handler(payload: dict):
    # payload 还包含 page_id / item_id
    return {"ok": True, "message": "...", "refresh": True, "open_page": "my:page"}


def device_login(payload: dict):
    # open_url 让浏览器新开一个外部标签页（设备授权等），页面里仍需给出可复制链接
    return {"ok": True, "message": "已生成授权链接", "refresh": True,
            "open_url": "https://example.com/device?code=ABCD"}
```

返回 `refresh=True` 会让前端重新拉取 state．长任务需在后台执行，action 立即返回．`open_page` 打开应用内页面，`open_url` 打开外部链接（仅 `http`/`https`），两者可同时使用．

浏览器是导航的唯一来源．`GET /api/ui/state` 接收 `page`、`class`、`doc` 和 JSON 数组 `pages` 查询参数．`POST /api/ui/action` 的请求体必须包含同名字段组成的 `navigation` 对象．页面打开、关闭接口也接收 `{navigation: {...}}`．命令响应返回目标 `navigation`，浏览器负责更新 URL．服务端不保存导航，UI 状态响应不能覆盖浏览器导航．

插件在 UI 请求内通过 `core.navigation.current_navigation()` 读取临时导航．`ctx.ui.open_page()`、`close_page()` 和 `show_home()` 只修改该请求的导航．这些方法不能为后台任务保存选择．后台工作必须使用显式课次 ID、文稿路径和任务 ID．`StreamHub` 只管理媒体与处理会话，不提供所选课次接口．

Action 归属：

- action id 必须以插件 `id` 开头，后接 `.`．插件不能以其他插件的 id 注册 action，也不能重复注册同一个 action id．
- `ctx.ui.unregister_action(action_id)` 只允许移除本插件注册的 action．
- `ctx.ui.action_owner(action_id)` 返回注册方插件 id 或 `None`，`ctx.ui.has_action(action_id)` 判断是否存在．
- 驱动其他插件的 action 时，用 `uses_actions = ("other.action",)` 声明．如果声明的 action 不存在，或注册方未加载，插件加载失败．
- 运行时再次确认：目标 action 不存在时不要构造失效的交互控件，例如省略 `send_action`．

`agent_activity` 使用精确 SDK 会话 ID 发送消息：

```python
{"type": "agent_activity", "id": "my-log",
 "send_action": "agent.message", "send_values": {},
 "agents": [{"id": session.id, "session_id": session.id, "role": "main",
             "label": "Agent", "status": "running", "blocks": []}]}
```

发送 action 收到 `{agent_session_id, message, **send_values}`．笔记使用 `notes-pipeline.message`，`send_values` 包含处理会话的 `session_id`，由流水线判断是否恢复笔记写作．控件 ID、角色和 SDK 会话 ID 不得混用．

### reader：选中菜单与内联问答

```python
reader = ctx.require("reader")

# owner 决定文档显示哪些页面级菜单项
page_id = reader.show(title="笔记", docs=docs, active="knowledge",
                      page_key=session_id, owner="my-plugin")

# 页面级菜单项按页面和 owner 匹配
reader.register_selection_action(
    page_id,
    {"id": "my.ask", "label": "提问", "kind": "prompt",
     "action": "my.ask", "placeholder": "针对选中的内容提问"},
    owner="my-plugin",
)

# 全局菜单项适用于所有 reader 页面
reader.register_selection_action(
    "",
    {"id": "ask.ask", "label": "提问", "kind": "prompt", "action": "ask.ask",
     "intensities": [{"value": "instant", "label": "快速"},
                      {"value": "research", "label": "研究"}]},
    owner="ask",
)

# 问答块由 reader 做确定性写入；插件只负责产生回答文本
created = reader.qa_begin(path, question="为什么？", selection=selection,
                          intensity="research")
context = reader.qa_context(path, created["id"], before_words=2000, after_words=2000,
                        history_words=1000)
answer = my_agent_answer(...)          # 只读 agent，不写文件
reader.qa_finish(path, created["id"], answer=answer, status="done",
                 collapsed=True)       # collapsed 持久化到块 meta
reader.qa_set_collapsed(path, created["id"], False)
```

- `docs` 中每项包含 `key`、`label` 和 `path`．`path` 指向文稿文件，`owner` 可按文稿覆盖．
- `kind="prompt"` 在选中文段下方显示提问编辑器．提交成功后，编辑器原地变成问答块．action 返回 `data.qa_id`，用于保留同一问答组件．配置 `intensities` 时显示强度选择．提交的 values 包含 `{doc_key, path, selection, question, intensity, page_id}`．
- 提问编辑器中，Enter 换行，Shift+Enter 或 Ctrl+Enter 发送．取消不修改文稿，提交失败时保留输入．
- `qa_begin` 在选中段落下方插入 `pending` 块．未找到选中内容时，问答块追加到文末．`qa_finish` 更新同一块的回答、`status` 和 `collapsed`．
- `reader.qa_collapse` 接收 `{path, id, collapsed}`，保存后刷新展示．`reader.qa_delete` 接收 `{path, id}`．
- 页面级项按文档 owner 过滤．全局项不按 owner 过滤，action 需检查文档是否可用．
- 流式展示使用 `ctx.ui.notify("qa.live", payload)`．payload 包含 `path`、`id` 和流式增量，结束时包含 `status`、`answer_html` 和 `collapsed`．前端直接更新问答块．

UI 注册表提供通用 action `ui.open_page`：

```python
{"type": "buttons", "buttons": [
    {"label": "打开笔记页", "action": "ui.open_page",
     "value": {"page_id": "my:page"}}
]}
```

## 设置

```python
from core.settings_spec import SettingType, SettingsSpec

settings = ctx.require("settings")
settings.register(SettingsSpec(
    key="my.api_key",
    label="API Key",
    type=SettingType.SECRET,
    default="",
    group="My Plugin",
    description="...",
))
value = settings.get("my.api_key")
settings.set("my.api_key", "new")
```

- `type` 必须是 `SettingType` 成员（`SettingType.SECRET`、`SettingType.INT` 等），字符串形式会被拒绝．
- secret 在 UI 和公开输出中掩码，内部 `get` 返回真实值．
- `env=("ENV_NAME",)`：注册时，如果设置尚未存储，则读取环境变量并写入 `settings.json`．
- `visible_when=("other.key", (value, ...))`：仅当另一个设置取指定值时显示该字段．
- `section="..."`：将同一分类的字段组成表单分组．
- `hidden=True`：持久化但不在表单显示．
- `readonly=True`：只读展示，保存时忽略．
- `type=SettingType.ACTION, action="..."`：渲染为按钮，点击调用对应 UI action．
- 设置变化发 `settings.changed`．

LLM 设置提供对象表单与原始 JSON 编辑区．配置保存在 `settings.json` 的 `llm.models`、`llm.roles` 中，提交后共用运行时校验．全部 8 个固定角色必须显式配置，继承使用 `@default`，多余或缺失角色均拒绝保存．SDK 所需文件只存在于临时工作区．

提供方凭据保存在 `auth.json` 的 `llm` 字段，运行时载入临时 `AuthStorage`．OpenCode 登录、刷新和登出即时保存凭据．无鉴权服务设置 `auth: none`．

## Agent

```python
from core.agent_types import AgentProfile, AgentTool
from core.workspace import FileWorkspace

agent = ctx.require("agent")
ctx.llm.register_role(ctx.plugin_id, "my.reader", "文稿问答")
agent.register_agent(AgentProfile(
    name="my.reader",
    role="my.reader",
    system_prompt="根据授权文件回答课堂问题．",
    tools=[AgentTool("read"), AgentTool("grep"), AgentTool("glob")],
))

workspace = FileWorkspace(
    {"notes.md": "/path/notes.md", "source.md": "/path/source.md"},
    readonly=("notes.md", "source.md"),
)
info = agent.create_session(name="my-reader", agent="my.reader", workspace=workspace)
result = agent.run(info.id, "解释这一段", on_event=lambda event: print(event))

# 写作会话只允许修改列出的文稿．
writer = agent.create_session(
    name="my-writer",
    role="note_writer",
    system_prompt="根据完整来源维护笔记．",
    workspace=FileWorkspace(
        {"notes.md": "/path/notes.md", "source.md": "/path/source.md"},
        readonly=("source.md",),
    ),
    tools=["read", "grep", "glob", "edit", "write"],
)
agent.cancel(writer.id)
agent.close(writer.id)
```

工具使用 OMP 的 schema 和动态描述．调用方声明工具名：

| 工具    | 行为                                     |
| ------- | ---------------------------------------- |
| `read`  | 读取授权文件，支持路径选择器、范围和快照 |
| `grep`  | 按正则搜索授权工作区                     |
| `glob`  | 列出授权工作区文件                       |
| `edit`  | 修改授权可写文稿，检查编辑目标与快照     |
| `write` | 完整写入授权可写文稿                     |

`FileWorkspace` 声明文件映射和只读文件．工具使用工作区内的相对路径．运行时拒绝绝对路径、URI、目录逃逸和符号链接．只读会话需将 `tools` 限制为 `read/grep/glob`．

`create_session`、`reopen` 和 `get_session` 返回不可变的 `AgentSessionInfo`．`step` 从 0 开始，运行结束后记录 SDK 返回的步数．运行中的状态以 `agent.*` 事件为准．会话仍活动时，`reopen` 失败．

`agent.session_workspace(session_id)` 返回会话文件映射的副本，不复制文稿内容．修改传入或返回的文件映射，不会改变会话授权的路径或只读标记．`meta` 只向运行时传递选项，例如 `review`．

`on_event` 接收 `AgentEvent`，事件名见「事件」一节．文稿变化触发 `agent.file`，应用在 publication lock 内写回授权文件并刷新预览．

`message(session_id, message)` 返回 `{accepted, reason?}`．消息只进入活动 run，立即中断目标回合并在同一任务中继续；`agent.run.ready` 表示开始接纳．关闭目标拒绝消息，不创建空会话．`cancel` 终止整个任务；`close` 取消执行并释放会话．SDK 保存模型历史，Python 的显示记录用于界面展示．

`reopen(session_id, *, name, workspace, system_prompt, role, tools, meta)` 恢复已关闭的 Agent 会话．指针或 transcript 缺失时失败，不回退到创建．业务所有者负责重新进入原任务流程；笔记流水线只恢复已发布的笔记写作，已完成的分段笔记、笔记审阅和问答任务不恢复．

`can_resume(session_id)` 返回 `(bool, reason)`：会话仍活动或运行时已无可用 transcript 时为 `False`．恢复前用它判断，不要直接读取运行时目录．

### 角色注册与设置

核心内置 `default` 和 `compaction`．业务角色由插件注册，角色 ID 使用非空字符串．插件必须先注册角色，再创建会话或注册角色扩展．

```python
ctx.llm.register_role(ctx.plugin_id, "my.reader", "文稿问答")
ctx.llm.register_role(ctx.plugin_id, "my.vision", "图像分析", input=("text", "image"))
ctx.llm.list_roles()
ctx.llm.unregister_roles(ctx.plugin_id)
```

`input` 声明角色要求的模型输入能力，默认为 `("text",)`．角色 ID 不得重复，不得覆盖内置角色．插件卸载时按所有者清理角色．

设置页显示已注册角色及其模型选择器．已有选择器保持不变；缺失条目补为 `@default`，未注册条目静默删除．`compaction` 用于运行时上下文压缩，未单独配置时继承 `default`．

### 角色扩展

插件通过 `AgentExtension.roles` 选择扩展生效的角色，可追加提示词、工具和结束钩子．角色 ID 必须已注册．

内置 `note_check_stc` 插件使用此接口向 `note_chunk`、`note_writer`、`note_review` 追加共用表达规则，并只向 `note_review` 注册 `note_check_stc` 工具和结束钩子．设置 `note_check_stc.enabled` 默认为 `true`，关闭或卸载后不影响普通笔记流程；已创建会话保留原快照．工具接受 `{"path": "note_knowledge.md"}`，只读解析授权文件，返回 `file`、`errors`、`warnings` 和含位置、规则、原文、原因与建议的 `issues`．结束钩子要求本轮至少成功检查一份受审笔记，文件读取失败和检查来源文件不计入；诊断不直接决定审阅通过与否．

```python
from core.agent_extensions import AgentExtension, AgentExtensionTool, AgentCompletionHook


def lookup(context):
    value = context["parameters"]["query"]
    return {
        "content": [{"type": "text", "text": value}],
        "details": {"role": context["role"]},
    }


def check_output(context):
    if not context["text"].strip():
        return {"continue": "结束前必须提供回答．"}
    return None


agent.register_extension(ctx.plugin_id, AgentExtension(
    id="course_context",
    roles=("my.reader",),
    prompt="使用已注册的课程上下文．",
    tools=(AgentExtensionTool(
        name="course_lookup",
        description="按查询读取课程上下文．",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=lookup,
        label="课程查询",
    ),),
    completion_hooks=(AgentCompletionHook(id="output", handler=check_output),),
))
```

`AgentExtension` 的字段如下：

| 字段               | 类型                              | 默认值 |
| ------------------ | --------------------------------- | ------ |
| `id`               | `str`                             | 必填   |
| `roles`            | `tuple[str, ...]`                 | 必填   |
| `priority`         | `int`                             | `0`    |
| `prompt`           | `str`                             | `""`   |
| `tools`            | `tuple[AgentExtensionTool, ...]`  | `()`   |
| `completion_hooks` | `tuple[AgentCompletionHook, ...]` | `()`   |

`AgentExtensionTool` 必填字段为 `name`、`description`、`parameters` 和 `handler`．`parameters` 使用 JSON Schema 对象，`label` 默认为 `None`．`AgentCompletionHook` 必填字段为 `id` 和 `handler`，`priority` 默认为 `0`．

通过 `create_session(..., role="my.reader")` 或 `reopen(..., role="my.reader")` 选择角色．创建会话时未指定角色则使用 Agent 配置中的角色，没有配置时使用 `default`．扩展仅在声明的角色中生效，注册时拒绝未知角色．

工具名接受字符串，`AgentToolName` 仅列出内置工具．扩展声明的工具会加入匹配角色；会话 `tools` 中的自定义名称必须有对应扩展声明．

注册约束如下：

- `(owner, id)` 扩展键不得重复．
- 角色重叠的扩展不得使用相同工具名．`read`、`grep`、`glob`、`edit`、`write` 和 `review_notes` 为保留名称．
- 同一所有者在重叠角色中的钩子 ID 不得重复．所有者和钩子 ID 不得包含 `/`．
- 不同角色可使用相同工具名或钩子 ID．

扩展按 `(priority, owner, id)` 排序：整数升序，字符串按区分大小写的字典序排列．结束钩子使用自身的优先级和 ID，按同一规则串行执行．某个钩子拒绝结束后，后续钩子仍会执行；运行时按顺序合并原因，交回同一个 Agent 继续处理．钩子异常使本次运行失败．强制取消不执行结束钩子．

仅带有 `meta.review_targets` 的审阅会话先执行内置 JSON 格式校验，再执行插件结束钩子．审阅钩子也会收到格式错误的报告，解析前必须检查文本或处理解析异常．其他角色不要求 JSON 审阅报告．

同步处理函数接收一个字典：

- 公共字段：`session_id: str`、`role: str` 和 `workspace: dict[str, str]`．
- 工具字段：`parameters`，内容为模型提交的参数．
- 钩子字段：`text: str` 和 `tool_calls: list[dict]`．
- 每条工具调用包含 `name`、`arguments` 和 `result`．`tool_calls` 覆盖本次运行及钩子要求的继续执行，下次运行前清空．

`workspace` 将授权逻辑文件名映射到运行时路径，可能指向临时文件或审阅快照．回调期间必须使用这些路径，不得保留到回调结束之后．处理函数运行在回调工作线程，而非 IPC 读取线程；不同会话的处理函数可能并发执行．

钩子返回 `None` 表示允许结束，返回 `{"continue": "必须修正的内容"}` 表示拒绝结束．继续指令不得为空或仅含空白．工具返回 `{"content": [{"type": "text", "text": "结果"}], "details": {}}`，可添加 `isError: true` 标记工具失败．

自定义处理函数是受信任的 Python 代码，不受文件工具沙箱限制．插件必须遵守工作区授权和任务的只读要求．

服务在创建和重开会话时保存注册表快照．注册表变化不改变运行中会话的扩展与回调．`unregister_extensions(owner)` 和插件卸载会移除该所有者对后续会话的扩展．以插件 ID 作为 `owner`，才能在卸载时自动清理．

### 审阅声明与传输字段

审阅配置指定已注册的角色、目标和来源：

```python
ctx.llm.register_role(ctx.plugin_id, "my.reviewer", "文稿审阅")
meta = {
    "review": {
        "role": "my.reviewer",
        "prompt": "依据来源核对已保存的笔记．",
        "system": "只读审阅授权文件．",
        "maxRounds": 0,
        "targets": ["notes.md"],
        "sources": ["source.md"],
    },
}
```

目标和来源使用工作区逻辑文件名，每个文件都必须已获工作区授权．审阅快照、内容摘要和报告路径校验包含全部目标．目标或来源变化会使原审阅批准失效．笔记流水线以全部 `NOTE_HEADERS` 文件为目标，以 `SOURCE_FILENAME` 为来源；写作 Agent 通过 `review_notes` 请求审阅．

创建和重开请求使用 `role` 选择角色，并包含以下 `extensions` 对象：

```text
{
  prompts: string[],
  tools: {name, description, parameters, label?}[],
  completion_hooks: {owner: string, id: string, priority: integer}[],
  reviewer: {prompts, tools, completion_hooks}
}
```

扩展数组默认为空．Python 按 `meta.review.role` 生成 `reviewer` 的扩展．审阅回调的会话 ID 为父会话 ID 加 `-review`．

运行时回调包含 `{session_id, role, kind, name, arguments}`．`kind` 为 `tool` 或 `completion_hook`．工具参数为 `{parameters, workspace}`，钩子参数为 `{text, workspace, tool_calls}`．工具回调名称为工具名，钩子回调名称为 `owner/id`．Python 按会话快照分派回调，拒绝未知会话、角色、工具和钩子．

## 文件与数据目录

```python
ctx.data_dir                         # data/agent
ctx.session_dir()                    # data/agent/sessions/<session_id>
ctx.session_dir("other-session")     # 指定会话
```

会话产物包括 `notes/` 下的文稿和 `vlm/` 下的融合图．

## 测试

- 测试通过 `llm=` 注入脚本化服务，避免访问外网．
- 事件异步分发，断言前调用 `runtime.bus.flush(2.0)`．
- 线程型插件在 `teardown` 中必须停止并 join．
