# 课堂处理架构

## 分层与插件

Python 管理课堂输入、识别、笔记任务和 Web 服务．Bun 子进程执行 OMP SDK 模型会话．

```text
Canvas / SJTU 视频门户 / 本地文件 / 手动 URL
                    |
             input / ffmpeg
                    |
        AudioStream + VideoStream
             |             |
            stt           vlm
             |             |
        transcript       visual
                    |
             notes-pipeline
                    |
      summarize / instant-summarize
                    |
              reader / ask
                    |
           UIManager / Web UI

llm-agent / llm -> JSON Lines IPC -> Bun / OMP SDK
```

- `PluginManager` 按依赖拓扑顺序调用 `setup`，逆序调用 `teardown`．缺失必需依赖或循环依赖会阻止加载．
- `requires` 指定必需插件．`optional_requires` 在插件存在时约束加载顺序．
- `PluginContext` 提供事件、流、LLM、UI、服务、元信息和日志接口．插件通过服务注册与调用公开能力．
- `settings`、`llm`、`llm-agent`、`reader` 和 `ask` 由内置插件组合加载．输入、识别和笔记插件由应用配置控制．
- `notes-pipeline` 依赖 `settings`、`llm-agent`、`llm`，可选依赖 `stt`、`vlm`．

## 流与事件

每个 `StreamSet` 包含音频、视频、转写和板书流．时间单位为秒，媒体时间从会话起点计算．消费者各自维护读取位置．

| 流            | 数据与读取契约                                                                               |
| ------------- | -------------------------------------------------------------------------------------------- |
| `AudioStream` | PCM 字节；`read(start, end)` 返回保留区间的音频；`buffered_ranges` 返回有真实 PCM 的连续区间 |
| `VideoStream` | JPEG 帧；`read_frames` 返回含时间范围的帧；`read` 返回帧字节拼接                             |
| `TextStream`  | 不可变 `TextItem` 序列；包含文本、时间、来源和元信息；按索引增量读取                         |

媒体流的 `duration` 是累计时间，`len()` 返回整秒．文字流的 `len()` 返回条目数．媒体追加不覆盖已有时间区间，但允许时间空洞．`trim(before)` 释放已消费媒体，累计时间保持不变．流不保证保留完整媒体供历史回放．

`StreamHub` 管理会话生命周期和媒体消费者登记．输入通过消费者接口取得背压与完成状态，不导入 STT/VLM 实现．`BacklogGate` 限制待处理媒体量．

- `session.started`、`session.stopped` 报告会话生命周期．
- `stream.appended` 报告追加．音频默认每 0.5 秒合并通知，视频默认每 1 秒合并通知，文字逐条通知．
- `stream.sealed` 报告转写或板书的完成位置、终止状态、启用状态与错误．笔记任务据此判断材料是否完整．
- `clock.tick` 提供周期通知．插件还可定义业务事件．

事件总线在 dispatcher 线程内顺序分发事件．处理器按优先级降序执行，同优先级按注册顺序执行．处理器失败会记录日志并产生 `event.error`，其余处理器继续执行．

## 输入与识别

### 输入

输入插件提供 Canvas 课程、SJTU 录播与直播目录，也接受本地文件和手动 URL．选择课程只加载地址，处理 action 才启动摄取．课程任务进入输入队列．

`FFmpegSource` 分别启动音频和视频 ffmpeg 进程．录播全速解码，直播按直播源摄取．音频转换为单声道 PCM，视频输出 JPEG 帧．两个 reader 均结束后才报告该段输入结束．

远端录播通过可寻址的本地缓存供 ffmpeg 读取，下载与处理可以重叠．下载失败报告输入错误并停止源．连续节次通过 `next_source` 连接，预取后续录像，并将媒体时间接到同一会话．中间节次不结束连续识别．最后一段结束后，输入等待消费者完成，再结束会话．

下载进度汇总整次课的各段字节数．处理进度使用整次课的媒体时长，并跟随已启用消费者的处理位置．本地录像按 `input.keep_recordings` 保留，笔记独立保存．

### 语音识别

`stt` 使用配置的兼容服务．自动转写通过 WebSocket `/v1/stream`，录播使用 `offline`，直播使用 `live`．手动范围转写使用 HTTP．声学切分、上下文和文本合并由服务端负责．

- 每次发送最多 200 ms PCM，接收线程独立处理事件．
- `partial` 更新临时文字，`final` 写入永久转写．
- `accepted` 推进发送额度；成功结果才更新覆盖记录并释放音频．发送位置、接收位置和成功处理位置分别记录．
- 断线重传未提交范围．重试耗尽的范围保留为覆盖缺口并报告错误．
- 禁用 STT 时只推进逻辑音频时间，不保留 PCM 或插入静音．

`stt.server_url` 默认 `http://127.0.0.1:8766`，可由 `STT_SERVER_URL` 初始化．语言与提示上下文分别通过 `stt.language`、`stt.context` 配置．

### 板书识别

`vlm` 按采样窗口读取视频帧，通过 `picture_parser` 融合图像，再由 `board_recognition` 角色识别．请求按窗口顺序执行，`vlm.prepare_ahead` 控制图像预处理是否提前执行．已不需要的帧会释放．

模型每个采样窗口读取采样时间和融合图，逐板面转录为 Markdown．提示词见 `src/plugins/vlm/prompts.py`．模型对照图像复核符号，不读取旧转录．程序用 `vlm.history_windows` 指定的历史窗口精确去重，再把新行写入 `visual`．融合图、板书文本和采样覆盖区间分别保存．

## OMP SDK 运行时

### 进程与会话

应用管理一个常驻 Bun 子进程，多个 SDK 会话在进程内异步执行．依赖由项目 `package.json` 与 `bun.lock` 固定，运行环境要求 Bun >=1.4.4．

Python 与 Bun 使用带请求 ID 的 JSON Lines 通信．stdout 传 IPC 消息，stderr 传日志．IPC 提供会话创建、恢复、执行、消息中断、关闭、取消及单次生成．

`createAgentSession()` 管理模型历史、文件工具、会话存储和上下文压缩．Python 管理应用任务、授权文件和 UI 事件．同一会话禁止并发运行．取消会终止对应模型流与工具执行．运行时退出使受影响请求失败．

`message` 只向已接纳消息的活动 run 提交输入．run 在同一所有者内中断 SDK prompt，等待工具安全退出，再提交按发送顺序合并的消息．`agent.run.ready` 表示运行时已建立消息接纳状态．完成验证后，运行时同步关闭消息接纳；关闭目标拒绝消息，不创建替代会话．

### 配置与授权

运行时导入 SDK 前清除继承的 `PI_*`、`OMP_*` 环境变量，并设置项目 agent 目录．会话使用 `Settings.isolated()` 和显式工具名单．配置禁用扩展发现、MCP、LSP、IRC 及外部上下文发现．课堂提示模板来自 `src/agent_runtime/prompts/classroom.md`．

`FileWorkspace` 将逻辑文件名映射到授权路径，并声明只读文件．OMP 原生 `read`、`grep`、`glob`、`edit`、`write` 执行文件操作．工作区检查调用涉及的路径和写入目标，限制访问与遍历范围．只读会话仅注册读取工具．工具错误作为调用结果返回模型．

每个会话保留独立工具状态．编辑默认使用 hashline，`tools.format=auto` 由 SDK 选择工具协议．同轮只读调用按 shared 规则并行执行，写入按 exclusive 规则保持顺序．写入完成后，运行时发送文件变更事件供应用更新预览．

设置页编辑 `data_dir/llm_runtime/models.yml` 与 `roles.yml`．保存时同时检查两份配置．会话保留创建时的配置．

- `models.yml` 使用 OMP `providers` 结构，声明提供方、模型、协议和能力．仅显式配置的模型可被调用．
- `roles.yml` 使用 `provider/model:level` 或 `@role` 选择器．未填写角色继承 `default`．
- `board_recognition` 选择板书模型，`note_chunk` 选择片段模型，`note_writer` 选择笔记写作模型，`note_review` 选择独立审阅模型．
- `ask_instant`、`ask_research` 选择问答模型，`compaction` 选择上下文压缩模型．

提供方凭据保存在项目 `AuthStorage` 的 `credentials.db`．设置页支持 API key 和 OpenCode 设备登录．OAuth 刷新与登出共用凭据存储．界面只显示凭据状态，不回显密钥．

### 笔记执行与审阅

`notes-pipeline` 按 `notes.chunk_seconds` 划分核心区间，并使用 `notes.overlap_seconds` 提供左侧上下文．所有启用来源完成区间后，任务冻结来源并整理片段．`notes.concurrency` 限制笔记 agent 并发数．

片段保存只读来源、上下文、板书历史和 3 份底稿．成功写入更新预览，连续完成的片段可发布为未审阅笔记．片段失败记录范围错误，完整来源仍交给笔记写作者．

来源终止后，笔记写作者读取完整来源和成功底稿．写作与修订共用 SDK 会话．启用审阅时，写作者通过 `review_notes` 调用独立只读审阅者．审阅者读取来源和 3 份文稿的冻结副本，返回 `correct` 布尔值、findings、explanation 和 confidence．

`review_passed` 记录文稿是否通过审阅．未启用审阅时自动通过．启用审阅时，文稿必须通过审阅才能发布．通过结果绑定文稿摘要，聊天消息不撤销通过．

审阅 operation 独立于工具等待；异步报告只交付 1 次．报告与冻结文稿摘要绑定，文稿改变后旧报告不能批准新内容．停止父任务时取消并回收审阅子会话．

已发布写作通过保存的 writer 描述恢复 SDK transcript．用户消息以原文传入，运行中使用 SDK 原生 steering．恢复任务进入 notes-pipeline 队列，并保留用户问答．启动和最终发布清理期间拒绝消息．

发布锁保护文稿与来源快照．发布时保留用户问答块及顺序．任务状态与底稿保存在 `note_work/`．

`summarize` 管理笔记页面和课后策略：`direct` 请求生成，`fill` 请求录播补录，`skip` 不自动生成．手动生成等待来源终止．`instant-summarize` 管理片段预览与开关，关闭预览不停止来源日志保存．

## UI 与文稿阅读

`UIManager` 管理侧边栏、工具箱、页面、声明式 block 和 action．前端渲染 block 并回传 action．后台任务通过刷新事件和通知更新页面．笔记执行视图显示写作者、审阅者、模型输出与工具执行记录．

`agent_activity` 通过 `send_action`、`send_values` 和各记录的 `session_id` 指定消息目标．共享编辑器按目标保存草稿、光标、选区和滚动位置．日志更新不替换输入节点；问答完成时，存在草稿或待响应提交的编辑器保留在最终答案旁．

UI action 按插件命名空间注册，注册与注销都校验所有者；驱动其他插件的 action 需用 `uses_actions` 声明，加载时校验目标存在且属于已加载插件．拥有者缺席时，发送方省略 `send_action`，前端隐藏编辑器只保留日志．

`reader.show` 接受文档列表，为每个笔记会话建立独立页面．reader 先解析整份 Markdown，再分离内联问答组件，保留跨段脚注引用．服务端使用 `markdown-it-py` 渲染，前端用 KaTeX 渲染数学公式．

`ask` 注册全局选中菜单．快速问答不注册文件工具；研究问答允许 `read`、`grep`、`glob` 读取授权材料．两种问答均包含选区上下文与历史问答．问答保存在笔记内的 `qa` 注释块中，reader 负责定位、写入、折叠和删除．`qa.live` 通知在浏览器原地更新问答内容．

Web 服务由 FastAPI 提供：

- `/api/ui/state`、`/api/ui/navigate` 和 `/api/ui/action` 提供 UI 状态、导航与操作．
- `/api/artifacts/{session_id}/{subpath}` 提供会话产物，路径限制在会话目录内．
- `/api/media` 转发允许的 SJTU 媒体 URL，并支持范围请求．
- `/ws` 推送刷新与业务通知．

HTTP 和 WebSocket 共用 `auth.json` 登录保护．登录页允许未登录访问，API 对未登录请求返回 401．默认监听地址为 `127.0.0.1`．

## 持久化与退出

| 路径（相对 `data_dir`）                                          | 内容                                              |
| ---------------------------------------------------------------- | ------------------------------------------------- |
| `settings.json`、`auth.json`                                     | 插件设置、登录账号与签名密钥                      |
| `llm_runtime/`                                                   | 模型与角色 YAML、提供方凭据、SDK 会话、授权工作区 |
| `sessions/<sid>/meta.json`                                       | 会话元信息                                        |
| `sessions/<sid>/coverage.json`                                   | 音频与视频处理覆盖区间                            |
| `sessions/<sid>/transcript.jsonl`、`visual.jsonl`                | 转写与板书记录                                    |
| `sessions/<sid>/media/`、`vlm/`                                  | 本地录像与融合图                                  |
| `sessions/<sid>/note_work/`                                      | 片段底稿、笔记工作区与任务状态                    |
| `sessions/<sid>/lecture_source.md`                               | 发布使用的完整来源                                |
| `sessions/<sid>/notes/note_{knowledge,gossip,notice}.md`         | 知识、闲话与通知笔记                              |
| `sessions/<sid>/notes/instant_note_{knowledge,gossip,notice}.md` | 片段预览                                          |

媒体流保存在内存，文本日志与覆盖记录供补录和笔记生成使用．退出时先停止输入，再取消分析和笔记任务，关闭 SDK 会话与 Bun 子进程，最后停止时钟和事件总线．
