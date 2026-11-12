# VideoScope

视频案例拆解工具：分析整片与黄金开场，提取镜头、动作、视听时序和创意机制，导出用于案例检索与换元素复刻的结构化 JSON 及媒体证据。

## 当前状态

已实现第一阶段的**媒体证据准备**：本地探测、实际帧时间戳、稀疏抽帧、音频提取、SQLite 准备任务、内容哈希复用、显式恢复与证据完整性校验。已接入指定 GitHub 仓库的三个远程 JSON，处理命令运行前先检查更新，下载到独立运行目录并建立跨源去重索引。

已实现限定四样本的 `full` / `opening` 分阶段分析编排、版本化结果契约、证据导出和持久化费用账本，**2 条整片 + 2 条黄金开场的真实拆解已全部完成**。语音、声音和画面文字由同一 Gemini 主模型直接理解；按用户决定，首版不再引入独立 ASR 或第二个语音校验模型。`both` 和通用分析批处理尚未实现。准备成功仍只表示媒体证据已落盘，不代表已观察或理解视频。

## 项目边界

VideoScope 是独立运行的工具，在专门的机器上处理视频，不依赖或占用 AgentCanvas 的运行环境、任务系统、数据库和模型配置。

- **VideoScope**：读取本地视频，提取证据，分析整片或黄金开场，校验并导出结果包。
- **消费方**：导入结果包、归档媒体、保存案例、编码检索向量及提供检索能力。
- **GitHub 仓库**：保存代码、提示词、依赖锁文件、示例配置和文档，不保存运行数据与凭据。

拆解结果保存原片观察、分析依据和迁移条件，不直接生成新任务的视频或最终制作提示词，也不自动认定原片功效、素材权限或案例质量。

## 首版范围

- 本地视频单条分析与批处理。
- 三种模式：整片、黄金开场、两者同时。
- 整片与开场分别保存结果，共用来源视频和基础证据。
- 记录镜头、动作、台词、声音、文字包装、视听同步和观察范围。
- 保留不确定项，支持证据回看，不用模型猜测冒充精确测量。
- 支持指定数据目录、任务状态、断点续跑及调用费用记录。
- 导出版本化 JSON 与媒体证据包，不在工具内生成检索向量。

首版不包含平台爬虫、Web 后台、视频生成或分布式任务平台。

## 技术方案

| 职责 | 计划采用 |
|---|---|
| 主程序 | Python 3.12、uv、Typer |
| 媒体处理 | FFmpeg、ffprobe、OpenCV |
| 语音、声音与画面文字理解 | 由 `gemini-3.8-flash` 直接读取连续音视频；首版不另接 ASR / OCR 模型 |
| 主分析模型 | `gemini-3.8-flash`，通过官方 `google-genai` SDK 调用已授权第三方 `https://api.apiyi.com`；不自动切换型号或端点 |
| 内容校验 | Pydantic、时间与引用检查 |
| 任务记录 | SQLite |
| 媒体与结果 | 独立本地数据目录 |

真实四样本已验证账号、原生流式视听输入、结构化输出和 usage 返回。首版不自动切换其他分析模型，不依赖 Codex 或 Claude Code 作为运行时；本地费用估算仍不等于供应商最终账单。

可按需参考或复用 [rea-video](https://github.com/JuneYaooo/rea-video) 的基础处理能力；实际引入源码时遵守其许可证，并保留适用的版权与 NOTICE。

## 数据与调用安全

模型密钥、真实运行配置、原视频、音频、抽帧、任务数据库、日志及结果包应存放于运行机器的数据目录，不提交 Git。

向外部模型上传媒体前，须确认素材可以交给相应供应商处理，并明确付费调用范围与预算。原片可访问不代表可以复用人物肖像、音乐、品牌或其他素材。

## 文档入口

本仓库是拆解工具计划和输出格式的唯一维护源。计划及专属配套资料按开发事项集中存放，事项目录不另建 README。

- [视频拆解—开发计划](docs/开发计划/视频拆解/视频拆解—开发计划.md)：操作、接口、依赖、实现步骤及验收标准。
- [优质视频拆解 JSON](docs/开发计划/视频拆解/优质视频拆解JSON.md)：整片参考与换元素复刻的内容结构讨论稿。
- [黄金开场拆解 JSON](docs/开发计划/视频拆解/黄金开场拆解JSON.md)：实际前三秒、完整开场与后文承接的内容结构讨论稿。

两份 Markdown JSON 继续作为内容基线，不是机器契约。正式类型唯一手工维护于 `src/video_scope/result_models.py`，由其生成 `contracts/1.0.0/` 中的整片、开场及清单 Schema。涉及 AgentCanvas 的口播、音乐等限制是该消费方的使用约束，不应使工具删去原片的真实观察。消费方自己的导入、存储、检索及制作使用规则仍由消费方维护。

## 安装与运行

当前支持 macOS / Linux（本地调度锁使用 `flock`），需要 Python 3.12、uv，以及 PATH 中的 FFmpeg / ffprobe。运行目录必须在代码仓库外。依赖由 `uv.lock` 固定。

```bash
uv sync --locked
uv run video-scope doctor
uv run video-scope prepare /绝对路径/视频.mp4 --data-dir ~/VideoScopeData
uv run video-scope status <任务标识> --data-dir ~/VideoScopeData
uv run video-scope resume <任务标识> --data-dir ~/VideoScopeData
uv run video-scope validate-evidence ~/VideoScopeData/evidence/<哈希>-media-0.1.0
```

本地证据保留原片副本、最多 12 张稀疏静态帧及存在音轨时的 16 kHz 单声道 WAV。支持常见自包含容器（MOV/MP4、Matroska/WebM、AVI、MPEG-TS/MPEG、M4V、FLV、Ogg），拒绝播放清单和外部协议。目前只处理首个非封面视频流与首个音轨，不代表完成多轨分析；所有实际帧时间戳会保存，长视频的探测耗时与清单大小随帧数增加。时间以首视频帧归零，保留 `video_start_time`；WAV 时间映射需加上 `audio_start_offset`，该值为 `null` 时不能假定音画对齐。编码宽高与旋转角度分别记录，JPEG 由 FFmpeg 自动旋转。稀疏帧不是切镜检测，不替代动作/声音/文字核对。`evidence.json` 标记 `prepared_not_analyzed`，不能作为计划中的 `manifest.json` 拆解结果包导入。

批量准备使用 JSONL，每行只有 `source` 字段；相对路径以清单所在目录为基准。单条失败不阻断其余条目，但命令最终返回非零退出码。并发上限目前固定为 1，不启动后台任务。

```json
{"source": "/绝对路径/视频.mp4"}
```

```bash
uv run video-scope prepare-batch /路径/清单.jsonl --data-dir ~/VideoScopeData
uv run video-scope export-evidence <任务标识> --data-dir ~/VideoScopeData \
  --output ~/VideoScopeExports/新目录 --allow-source-copy
```

导出包含原片，须确认具有向该目标目录交付的权限；不上传任何文件。目标目录已存在则拒绝覆盖。导出失败不改变准备成功状态，可重新导出。准备缓存使用来源内容哈希和媒体处理版本，复用前检查全部媒体哈希；损坏缓存报错而非静默采用或覆盖。中断后用 `resume` 显式恢复；尚未完成的媒体处理从头重做，没有付费重试问题。强制终止可能留下 `temporary/` 下的中间文件，程序不会自动清理其他会话的数据。

`VIDEOSCOPE_DATA_DIR` 可替代重复的 `--data-dir`。默认是 `~/.local/share/video-scope`。本地任务数据库记录输入绝对路径，不应外发；证据清单只记录相对媒体路径。准备与校验不读取任何供应商密钥。

```bash
uv run ruff check .
uv run mypy src tests
uv run pytest -q
```

工程测试中的合成媒体仅用于 FFmpeg、时间戳、缓存与校验检查，不是实际来源视频的业务验收。

## 远程数据源与重复视频过滤

固定来源是 [804607843-sudo/804607843-sudo.github.io](https://github.com/804607843-sudo/804607843-sudo.github.io) 的 `main` 分支：

- `static/data/video.json`
- `static/data/golden3s_groups.json`
- `static/data/gold3s.json`

`prepare`、`prepare-batch`、`resume` 每次启动先检查远端，一次批次只检查一次；`doctor`、`status`、证据校验和导出不联网。也可以单独同步：

```bash
uv run video-scope sync-sources --data-dir ~/VideoScopeData
# 断网时显式使用已有且校验通过的快照，不声称数据最新：
uv run video-scope sync-sources --data-dir ~/VideoScopeData --offline
uv run video-scope prepare /路径/视频.mp4 --data-dir ~/VideoScopeData --offline
```

同步先查询 GitHub 分支提交，再查询该提交的三个文件对象。没有变化就不重复下载；只有部分 JSON 变化时只下载变化文件。首次三个文件合计约 195 MB（以当前远端为准）；每文件设 128 MiB 上限。无需 GitHub 密钥，也不会读取或发送 `.env` 中的模型密钥。若匿名 API 返回 403/429，则使用 `git ls-remote` 获取分支提交，再读取固定提交文件的 ETag/长度；该路径需要 PATH 中有 Git，输出 `version_check: git-etag`。两种检查方式切换不会导致完整缓存无故重新下载。

三个文件始终固定到同一个 Git 提交，核对版本标识、文件长度、JSON 结构并建立索引后，才原子切换 `sources/current.json`。API 路径校验上游 Git blob；限流路径核对 HEAD/GET 的 ETag 一致性。两条路径都计算本地 SHA-256 并在复用前校验，不把 ETag 当作内容哈希。下载中断或格式错误保留旧快照，默认停止处理而不是静默用旧数据；只有显式 `--offline` 才使用已存在的完整快照。远端在一次检查之后发生的新提交留到下次运行读取。

本地目录：

```text
<数据目录>/sources/
├── current.json
└── snapshot-<唯一标识>/
    ├── video.json
    ├── golden3s_groups.json
    ├── gold3s.json
    └── catalog.sqlite3
```

历史快照保留，不随更新覆盖或自动删除。当前数据加索引每份快照约 630 MB，需为历史版本预留空间。`catalog.sqlite3` 是从本地 JSON 建立的去重视频目录：`videos` 保存唯一视频，`observations` 保留文件、记录下标与组内位置，原始投放周期和台词等仍在 JSON 中完整保留。不要直接修改快照；索引或 JSON 损坏会被哈希检查发现，联网同步可重建。去重算法版本改变时也会重建索引，不静默沿用旧规则。

去重规则：优先 `视频ID` / `sourceVid` / `vid`，缺失时使用精确的抖音作品链接；只有链接唯一对应一个视频 ID 时才补关联。不同视频 ID 不因相同标题、素材 ID、台词或统计周期而合并。组内原视频与 TOP3 分别入目录，不能把整组当作一条视频。无法确定身份的记录独立保留；冲突会计数，不强行合并。视频文件进入本地准备流程后，仍按 SHA-256 复用字节相同的媒体证据；JSON 层面不能证明两个不同视频 ID 的文件内容相同。

**同步命令只同步 JSON 和建立去重目录，不自动下载其中的视频，不上传视频，不启动全量分析或付费调用。** 播放地址可能过期，JSON 中的台词和效果数据也不等于已核实观察。另行执行显式授权的 `acceptance` 才会选取四样本、下载和请求分析；不是全量抓取或分析队列。

## 开发进度

| 事项 | 状态 |
|---|---|
| 独立环境、锁文件与运行数据隔离 | 已实现 |
| 本地媒体准备、哈希校验、准备任务恢复 | 已实现；工程验证见开发计划 |
| 远程 JSON 更新检查、本地快照与跨源视频去重 | 已实现；不自动下载或分析视频 |
| 单视频整片分析与正式结果包导出 | 已实现固定四样本范围内的两阶段编排和本地导出 |
| 黄金开场与两种结果复用 | opening 已实现；both 未实现 |
| 分析恢复与费用统计 | 已实现持久化步骤、共享预算、未知请求暂停及显式保留审计的重试；通用批处理未实现 |
| 语音 / OCR / 切镜候选 | Gemini 直接读取声音与画面文字，不另接 ASR / OCR；OpenCV 候选已实现，不等于真实切点 |
| 真实视频与模型验收 | 四样本 4 / 4 完成；四包机器校验通过，不把工程夹具计入真实结果 |

## 本轮实际验收状态（2026-11-12）

**四样本真实结果包已完成 4 / 4：opening 2 条、full 2 条。四个包均通过程序校验。** 用户明确要求只使用 Gemini，不再增加独立语音模型；本地仍保留 Schema、时间范围、引用和媒体哈希检查。此前超时调用保留为 `abandoned`，费用结果仍为未知，不删除审计记录或宣称免费。

实际账本：共保留 12 次调用记录，9 次成功、1 次明确拒绝、1 次由用户授权放弃阻塞、1 次 TLS 建连失败且未送达；当前 `unknown_calls=0`、`unsettled_reserved_usd=0`，已返回用量估算 **$0.46578300**。旧超时请求的原预留 **$2.957527** 单列为 `abandoned_reservation_usd`，不再占用本地重试额度，但可能费用没有得到供应商最终核账，因此 `bill_is_final=false`。

已完成包：

- `~/VideoScopeData/campaigns/four-examples-v1/results/opening-e380721ef2c8-d2ce68c1877a/`
- `~/VideoScopeData/campaigns/four-examples-v1/results/full-2e9ffe690c8f-95cfb2500b00/`
- `~/VideoScopeData/campaigns/four-examples-v1/results/opening-6db3b1957c58-4d57c37698fd/`
- `~/VideoScopeData/campaigns/four-examples-v1/results/full-2c5c2503d58c-b39bab399370/`

四次实际 `validate-package` 均返回 valid=true、warnings=[]，文件数分别为 17、19、18、22。四条分析状态均为 `completed_human_pending`；这是结果契约保留的复核状态，不表示另一个语音模型仍是交付前置条件。

full 1 的流式视觉调用收到 264 块、15,506 字符、STOP 和完整 usage；终止块中的无文本元数据 Part 曾被本地收集器误判，修复后直接从持久化 JSONL 恢复，没有再次请求。该阶段输入 27,464 token、输出 7,314 token，估算 $0.04802550。随后视听阶段收到 377 块、22,648 字符和完整 usage；输入 32,803 token（其中 AUDIO 600、VIDEO 1,584、IMAGE 18,700、TEXT 11,919），输出 10,414 token，估算 $0.06365475。结果包含 13 条口播、2 条声音、14 条文字包装和 5 个视听同步点；这些是待人工复核的模型结论。

opening 2 两阶段估算 $0.04186350 / $0.04956450。full 2 两阶段估算 $0.06126900 / $0.07829025；其中视听阶段一次 TLS 握手 `ConnectError` 且 0 分块、空日志、无 HTTP 状态，按 HTTPX 的“未能建立连接”语义记为 `connection_failed` 并保留审计，下一次命令才重新请求。ReadTimeout、写入失败或收到任何响应数据仍不会走该路径。

已经完成的实际非付费检查：

- 固定四个不同身份和 SHA-256 的远程视频，整片时长 24.2 秒、20.916667 秒，开场原片时长 29.133333 秒、64.5 秒。
- 四条均已下载、探测、生成完整本地证据；轻量时序指纹与抽帧目视检查未发现明显同内容重复，不作为版权或全库唯一性证明。
- 两条 full 准备完整原片；两条 opening 各准备连续前 12 秒及密集关键帧，四份输入均保留音轨。媒体 Base64 估计分别约 1.74 / 18.44 / 7.99 / 4.42 MB，真实序列化请求仍由发送前硬检查限制在 20 MB 内。
- 实际只读价格查询得到输入 $0.75 / 百万 token、输出 $3.75 / 百万 token；不假设缓存折扣，不代表最终账单或模型调用成功。
- `uv run ruff check .`、`uv run mypy src tests`、diff 格式检查通过；`uv run pytest -q` **125 项通过**。测试覆盖显式放弃审计、严格建连失败分类、终止元数据 Part 兼容、完整流日志无网络恢复及按观察秒预留输入费用。

运行数据位于 `~/VideoScopeData`，固定清单、账本和四个结果包在 `campaigns/four-examples-v1/`，各阶段响应保存在 `analyses/`。四样本活动状态为 completed；本轮代码仍未提交或推送。

## 四样本验收入口

本轮只处理固定活动 `four-examples-v1`：两条 full、两条 opening，共用累计 5 美元本地账本。只使用本项目 `.env` 的 `GEMINI_API_KEY`、`GEMINI_MODEL=gemini-3.8-flash`、`GEMINI_BASE_URL=https://api.apiyi.com`；环境变量优先。这是第三方端点，不是 Google 直连。

```bash
# 有外部上传及费用授权时才运行；没有 --allow-upload 会拒绝：
uv run video-scope acceptance --data-dir ~/VideoScopeData --allow-upload
# 仅处理/恢复活动中已经锁定的一条样本，不接受任意新视频：
uv run video-scope analyze <样本完整SHA256> --data-dir ~/VideoScopeData --allow-upload
# 不联网、不付费：
uv run video-scope status --data-dir ~/VideoScopeData
uv run video-scope validate-package /绝对路径/结果包
```

分析入口默认先同步远端；`--offline` 只跳过远端 JSON 更新，**不会禁止已授权的模型联网调用**。已有四样本清单恢复时不重新选样，不因元数据更新替换媒体。需要恢复分析时重跑同一 `analyze` 或 `acceptance`；原 `resume` 仍只恢复媒体准备任务。

只有用户明确接受一笔未知请求可能仍被供应商计费并授权重新请求时，才可在同一条 `analyze` 命令追加 `--retry-unknown-call <调用ID>`。旧调用会改为 `abandoned` 并保留原预留、错误和请求审计；该操作只是解除本地阻塞，不是供应商核账结论。

供应商采用原生 `generateContent` 内联视频和 JPEG，保留音轨，整个序列化请求不超过 20,000,000 字节。不用 Files API、任意视频 URL 或未经确认的供应商 FPS 字段。开场初始输入为前 12 秒以内的连续视听片段，加实际前三秒的密集 PTS 帧；没有提供的后文不能写成已观察。完整开场不是固定三秒。语音、声音和画面文字均由同一 Gemini 主模型读取，首版不再接独立 ASR / OCR。

### 离线复核清单

`review-package` 先完整校验结果包，再向标准输出生成 JSON 待复核清单，按原片时间排列镜头、口播、声音、文字包装及同步点，并保留观察范围、开场范围、后文承接、不确定项与媒体引用。媒体相对路径以原结果包目录为基准。所有条目均为 `pending`；内容仍是模型结论，需要回看原片核对。命令不联网，不修改结果包、人工复核状态或费用账本。

```bash
uv run video-scope review-package /绝对路径/结果包 > /结果包外/复核清单.json
```

### 可选原生流式接收

`analyze` 和 `acceptance` 支持显式 `--stream`，默认仍为非流式。使用官方 SDK 的 `generate_content_stream()`，实际路由为 `/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse`，不是 OpenAI 兼容接口。

```bash
uv run video-scope analyze <样本完整SHA256> --data-dir ~/VideoScopeData --allow-upload --stream
```

每个 SDK 分块先写入调用对应的 `.response.stream.jsonl` 并 flush/fsync，随后在 stderr 显示块数、累计字符数和耗时；终端不输出原始素材内容。读取超时仍为 300 秒空闲等待，收到数据后可继续接收，并不代表首块前不会超时。流式不等于实时视频输入。

只有正常结束且收到终止原因和终止时/之后的完整 usage，才生成最终响应并结算；累计 usage 不按块重复相加。完整、连续且包含终止原因和最终 usage 的 JSONL 可在不联网的情况下恢复最终响应；缺终止标记、缺最终 usage、中途断流或异常事件仍只保留证据及预留，不自动重发。已完整落盘的成功响应继续复用。日志最多 8 MB、8192 块；响应凭据不进入日志。

四样本已验证供应商原生流式，成功阶段均返回 STOP、完整 usage 和 `gemini-3.8-flash` 模型版本。最慢一次 opening 视听首块约 159 秒，仍在 300 秒空闲读取上限内。旧非流式超时调用仍以 `abandoned` 保留可能费用，不与成功调用合并或假定免费。

运行数据保存在 `campaigns/four-examples-v1/`、`analyses/`、`evidence/` 和 `tasks.sqlite3`，不进 Git。结果包位于活动的 `results/`，包含版本 1.0.0 清单、业务 JSON、原片与真实帧。媒体路径由程序绑定；包校验通过仍标记 `human_pending`，不证明台词、切点或同步已完成人工验收。

费用按实时价格、每个观察秒一个 4,096-token 视觉余量、全部显式静态帧、音频/文本余量、16,384 输出 token 上限及 25% 余量预留，避免把 60 FPS 容器的每个解码帧误当成模型计费帧。主分析使用不超过 4 美元的账本额度，活动最多 12 次已送达请求，每样本最多两次主分析及一次具体校验补看。关闭 SDK 自动重试；发送后超时、缺 usage 或费用不明时暂停，不释放潜在费用。持久化响应可本地恢复，导出失败不重跑模型。**本地估算不是供应商硬扣费限额，也不是最终账单。**
