# 插件开发 API（v1）

本文是实现期的速查表；完整设计见 `docs/architecture.md`。

## 1. 最小插件

```python
from core.plugin import Plugin, PluginContext


class MyPlugin(Plugin):
    id = "my-plugin"
    requires = ("settings", "llm")   # 插件 id，不是服务名

    def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.settings = ctx.require("settings")
        self.llm = ctx.llm            # 角色请求代理，由项目 Bun 运行时执行

        ctx.ui.add_tab("right", "my-tab", "我的工具", icon="✦")
        ctx.ui.add_toolbox_item(
            "my-tab", "my-item", "快捷操作",
            render=lambda: [{"type": "text", "text": "hello"}],
        )
        ctx.ui.add_page("my:page", "我的页面", render=self.render_page)
        ctx.ui.register_action("my.action", self.on_action, plugin_id=self.id)
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

## 2. 流

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
video.read_frames(0, 30)  # list[VideoFrame(start,end,data)]
video.read(0, 30)         # 拼接 JPEG

len(transcript)           # 片段数
transcript[0].text
transcript.text_since(10) # 第 10 项之后的拼接文本
transcript.char_count_since(10)
transcript.snapshot_index()
transcript.append("文字", start=1.0, end=2.0, source="stt")
```

规则：

- 只能追加；已有片段不可变。
- 输入插件启动会话时调用 `ctx.streams.start_session(meta=...)`；行为插件不要调用。
- 音视频 `stream.appended` 是合并通知（默认 0.5s/1s）；文字流每次 append 都通知。

## 3. 事件

```python
ctx.events.on("my.event", handler, plugin_id=ctx.plugin_id, priority=0)
ctx.events.once("my.event", handler)
ctx.events.define("my.event", "说明")
ctx.events.emit("my.event", value=1)
```

内置事件：

- `clock.tick`
- `session.started` / `session.stopped`
- `stream.appended`
- `settings.changed`
- `ui.changed`
- `llm.request` / `llm.response` / `llm.error`
- `agent.run.started` / `agent.message` / `agent.tool_call` / `agent.tool_result` / `agent.run.finished` / `agent.error`
- `vlm.segment` / `vlm.error`
- `stt.segment` / `stt.error`
- `summarize.started` / `summarize.finished` / `summarize.error`
- `instant-summarize.triggered` / `instant-summarize.updated` / `instant-summarize.error`
- `input.status` / `input.error`

## 4. 服务互调

```python
service = ctx.require("settings")
service.register(spec)
service.get("my.enabled")

ctx.register_service("my.service", my_object)
other = ctx.require("summarize")
ctx.services.call("summarize", "trigger_now", reason="manual")
```

约定：

- 服务名点号命名：`settings`, `llm`, `agent`, `input`, `stt`, `vlm`, `summarize`, `instant-summarize`。
- `stt` 服务：`status()` 返回 state/error/backend/model/location/session/audio/cursor/segments/chars；`transcribe(start=None, end=None)` 立即转写当前音频流的一段（`start` 缺省为工作线程游标，`end` 缺省为游标 + 分块时长；不推进工作线程游标，仅供诊断/手动调用）。
- 插件之间不 import 实现；通过服务方法调用。
- `requires` 只保证 setup 顺序，调用时仍要处理服务缺失。

## 5. LLM

```python
response = ctx.llm.chat(
    role="board_recognition",
    messages=[{"role": "user", "content": "描述图中板书"}],
    max_tokens=1024,
)
response.content
response.usage

for event in ctx.llm.stream(role="board_recognition", messages=[...]):
    if event.type == "text":
        print(event.text)
```

- 单次生成使用 `ctx.llm.chat/stream(role=...)`，文件任务使用 `agent` 服务．两者共用项目 Bun 运行时、角色解析和提供方鉴权．
- Python 不注册 provider，不传输 API key，不实现模型工具循环．`ChatResponse/StreamEvent` 仅作为应用响应类型．
- 模型和思考级别来自 `roles.yml`．提供方、模型能力与兼容参数来自 `models.yml`．`api` 的可选值由已安装 OMP SDK 决定．
- 会话创建时固定配置，后续保存只影响新会话．`ctx.llm.info()` 返回有效角色与提供方状态，`usage_snapshot()` 按 `provider/model` 返回用量．
- `stt` 使用独立 HTTP/WebSocket 服务协议．

## 6. UI

```python
tab = ctx.ui.add_tab("left", "courses", "课程", icon="▤", order=10)
item = ctx.ui.add_toolbox_item("courses", "courses.list", "我的课程", render=render_list,
                               page_id="input:course:123")
page = ctx.ui.add_page("input:course:123", "课程详情", render=render_page, closable=True)
page.open()
ctx.ui.register_command("input.manual", "手动播放", category="调试", page_id="input:manual")
ctx.ui.refresh("reason")
# 右侧面板顶部的上下文条：返回 {"title", "subtitle", "state", "selected"}；
# 返回 None 时（且没有历史）右侧面板显示“还没有选择课程”的占位。
ctx.ui.set_context_provider(lambda: {"title": "高等数学", "subtitle": "11-05 · 录播 · 处理中",
                                     "state": "busy", "selected": True})
```

Block：

```python
{"type": "markdown", "text": "..."}
{"type": "text", "text": "..."}
{"type": "keyvalue", "items": [["课程", "复分析"], ["日期", "2026-11-01"]]}
{"type": "table", "columns": ["节次", "时间"], "rows": [["第9节", "15:55"]],
 "row_actions": [{"label": "观看", "action": "input.play", "keys": ["video_id"]}]}
{"type": "cards", "items": [{"title": "...", "subtitle": "...", "actions": [...]}]}
{"type": "list", "items": [{"text": "...", "action": "x", "value": {...}}]}
{"type": "buttons", "buttons": [{"label": "刷新", "action": "input.refresh"}]}
{"type": "form", "fields": [
    {"name": "url", "label": "直播 URL", "type": "text", "placeholder": "https://..."}
], "action": "input.play_url", "submit_label": "开始"}
{"type": "log", "lines": [{"time": "12:00", "text": "..."}]}
{"type": "image", "src": "/api/artifacts/<session>/vlm/000030-000060.jpg"}
{"type": "tabs", "tabs": [{"title": "知识", "active": True, "blocks": [...],
                          "meta": {"doc_key": "knowledge", "path": "...", "owner": "summarize",
                                   "selection_actions": [...]}}]}
{"type": "progress", "value": 0.42, "label": "处理进度"}
# 布尔开关：切换后以 {"enabled": true/false} 调用 action
{"type": "switch", "label": "实时总结", "checked": True,
 "action": "instant-summarize.toggle", "hint": "关闭后不再累积内容"}
# 内联问答对（reader 渲染成可折叠、可删除的独立组件）
{"type": "qa", "id": "qa-1a2b3c4d", "ts": "2026-11-10 14:03", "status": "done",
 "intensity": "research", "collapsed": True,
 "question": "...", "answer": "...", "question_html": "...", "answer_html": "...",
 "path": "/data/sessions/<sid>/notes/note_knowledge.md",
 "delete_action": "reader.qa_delete", "collapse_action": "reader.qa_collapse"}
# player：两路视频（m3u8 走 hls.js，flv 走 mpegts.js，其余原生播放）
{"type": "player", "sources": [{"key": "main", "label": "课堂", "src": "https://.../main.m3u8"},
                              {"key": "screen", "label": "屏幕", "src": "...", "muted": True}],
 "live": False, "processing": False, "finished": False, "part": 1, "parts": 2,
 "start_action": "input.start_processing", "pause_action": "input.pause_processing"}
{"type": "tree", "nodes": [
    {"label": "高等数学", "subtitle": "2026 秋 · 张三", "expanded": True, "live": True,
     "toggle": {"action": "input.toggle_course", "value": {"course": "1"}},
     "children": [
         {"label": "11-05 08:00–09:35", "badge": "直播中", "badge_class": "live",
          "active": True,
          "action": {"action": "input.open_class", "value": {"kind": "live"}}},
     ]},
]}
```

Action：

```python
ctx.ui.register_action("my.action", handler, plugin_id=ctx.plugin_id)

def handler(payload: dict):
    # payload 还包含 page_id / item_id
    return {"ok": True, "message": "...", "refresh": True, "open_page": "my:page"}
```

返回 `refresh=True` 会让前端重新拉取 state；长任务请放到后台线程，立即返回。

### 6.1 reader：选中菜单与内联问答

```python
reader = ctx.require("reader")

# owner 标记“这一页的文档是我给的”，决定选中菜单里出现谁注册的页面级项
page_id = reader.show(title="笔记", docs=docs, active="knowledge",
                      page_key=session_id, owner="my-plugin")

# 页面级菜单项：只在“这一页 + 这个 owner”的文档上出现
reader.register_selection_action(
    page_id,
    {"id": "my.ask", "label": "提问", "icon": "💬", "kind": "prompt",
     "action": "my.ask", "placeholder": "针对选中的内容提问"},
    owner="my-plugin",
)

# 全局菜单项：任何笔记页都出现（ask 插件就是这么注册“提问”的）
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
ctx = reader.qa_context(path, created["id"], before_words=2000, after_words=2000,
                        history_words=1000)
answer = my_agent_answer(...)          # 只读 agent，不写文件
reader.qa_finish(path, created["id"], answer=answer, status="done",
                 collapsed=True)       # collapsed 持久化到块 meta
reader.qa_set_collapsed(path, created["id"], False)
```

- `kind="prompt"`：前端先弹输入框（有 `intensities` 时带强度选择），提交时 values 里带 `{doc_key, path, selection, question, intensity, page_id}`。
- `qa_begin` 在选中段落下方插入 `pending` 块（问题立刻可见）；`qa_finish` 把回答写回同一块，并把 `status` / `collapsed` 写进 meta。
- 折叠由 `reader.qa_collapse`（values: `{path, id, collapsed}`，返回 `refresh=false`）完成；删除由 `reader.qa_delete`（values: `{path, id}`）完成，插件不用管。
- 页面级项只按当前文档的 owner 暴露；全局项不看 owner，由 action 自己判断文档是否合法。
- 流式过程中的实时展示走 `ctx.ui.notify("qa.live", payload)`（WebSocket `{"type":"notify","topic":"qa.live"}`），payload 里带 `path` / `id` / `phase` / `reasoning_delta` / `text_delta` / `tools` / `elapsed`；结束时带 `status` / `answer_html` / `collapsed`，前端原地 patch，不触发整页刷新。

Web 层还预注册了通用 action `ui.open_page`，插件可以在按钮里这样用：

```python
{"type": "buttons", "buttons": [
    {"label": "打开笔记页", "action": "ui.open_page",
     "value": {"page_id": "summarize:notes"}}
]}
```

## 7. 设置

```python
from plugins.settings import SettingsSpec

settings = ctx.require("settings")
settings.register(SettingsSpec(
    key="my.api_key",
    label="API Key",
    type="secret",
    default="",
    group="My Plugin",
    description="...",
))
value = settings.get("my.api_key")
settings.set("my.api_key", "new")
```

- secret 在 UI/公开输出中掩码；内部 `get` 返回真实值。
- `env=("ENV_NAME",)`：首次注册时把环境变量写入 `settings.json`，之后以设置为准，避免隐藏回退。
- `visible_when=("other.key", (value, ...))`：仅当另一个设置取指定值时显示该字段。
- `group_note="..."`：显示在该设置分组表单上方。
- `hidden=True`：持久化但不在表单显示（适合由插件管理的 token）。
- `readonly=True`：只读展示，保存时忽略。
- `type="action", action="..."`：渲染为按钮，点击调用对应 UI action。
- 设置变化发 `settings.changed`。

LLM 设置使用独立的 `models.yml` 与 `roles.yml` 编辑区，不注册模型、密钥或 YAML 字符串的标量设置．两份文件保存在 `data_dir/llm_runtime/`，保存前一起检查．角色支持 `provider/model:level`、`@role` 和缺省继承 `default`．

API key 按提供方写入项目 OMP `AuthStorage`（`llm_runtime/credentials.db`）．OpenCode 登录、刷新和登出也使用同一凭据存储．无鉴权服务明确设置 `auth: none`．设置页不回显凭据，普通插件 secret 仍按上述接口管理．

## 8. Agent

```python
from core.agent_types import AgentProfile, AgentTool
from plugins.agent import FileWorkspace

agent = ctx.require("agent")
agent.register_agent(AgentProfile(
    name="my.reader",
    role="ask_research",
    system_prompt="根据授权文件回答课堂问题．",
    tools=[AgentTool("read"), AgentTool("grep"), AgentTool("glob")],
))

workspace = FileWorkspace(
    {"notes.md": "/path/notes.md", "source.md": "/path/source.md"},
    readonly=("notes.md", "source.md"),
)
session = agent.create_session(name="my-reader", agent="my.reader", workspace=workspace)
result = agent.run(session.id, "解释这一段", on_event=lambda event: print(event))

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

工具直接使用 OMP 的 schema 和动态描述．调用方只声明工具名，不设置 Python 工具参数或截断规则：

| 工具    | 行为                                         | 调度      |
| ------- | -------------------------------------------- | --------- |
| `read`  | 路径选择器、范围、快照与继续读取             | shared    |
| `grep`  | 正则、文件过滤与命中上下文                   | shared    |
| `glob`  | 列出授权工作区文件                           | shared    |
| `edit`  | 模型选择的上游编辑模式，多文件修改与快照检查 | exclusive |
| `write` | 创建文件或完整写入，遵守上游写入约束         | exclusive |

`read/grep/edit` 共用会话级工具状态．默认编辑模式为 hashline，模型策略可选择 replace 模式．追加内容使用原生 `edit`．

连续只读调用可并行．写入等待前面的调用完成，后面的读取等待写入完成．多个写入保持调用顺序．工具错误返回给对应调用，SDK 继续管理恢复和上下文压缩．完整来源在压缩后仍可读取．

`FileWorkspace` 声明授权文件和只读文件．运行时检查全部工具路径和编辑目标，拒绝目录逃逸、符号链接逃逸与未开放 URI．只读会话不注册 `edit/write`．职责提示追加到 SDK 的模型提示模板，不覆盖上游工具说明．

`on_event` 接收 `agent.run.started`、`agent.delta`、`agent.reasoning`、`agent.tool_call`、`agent.tool_result`、`agent.message`、`agent.file` 和终态事件．成功写入触发 `agent.file`，应用在 publication lock 内更新预览．审阅会话读取同一锁下的固定快照．

`cancel` 终止实际模型流与工具执行．`reset` 清除会话，`close` 取消并释放会话．Bun 进程退出时请求返回错误，已保存文稿与来源位置供恢复使用．SDK 保存模型历史，Python 显示记录不再回传给模型．

## 9. 文件与数据目录

```python
ctx.data_dir                         # data/agent
ctx.session_dir()                    # data/agent/sessions/<current session>
ctx.session_dir("other-session")     # 指定会话
```

产物建议：

- 笔记：`notes/`
- VLM 融合图：`vlm/`
- STT 临时文件：`stt/`

## 10. 测试

```python
from config import load_config
from core.runtime import build_runtime

runtime = build_runtime(load_config(data_dir=tmp_path), plugins=[MyPlugin()])
runtime.start()
...
runtime.stop()
```

- 确定性测试不访问外网，使用脚本化运行时或本地请求记录．测试面向配置、授权、事件和可见文稿行为，不绑定旧 Python provider 或 worker 实现．
- 事件异步分发，断言前调用 `runtime.bus.flush(2.0)`。
- 线程型插件在 `teardown` 中必须停止并 join。
