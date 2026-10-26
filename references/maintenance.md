# 维护契约与刻意不修清单

修改本 skill 的脚本前先读本文。Pin 给 CLI 的契约串、入口行为契约、以及明确决定不做的重构都记录在此；缺了这份上下文，容易把「刻意保守」当 bug 修掉，或把 pin 死的 CLI 契约当硬编码清理。相关调研背景见[单聊目录调研](dm-discovery.md)。

## Pin 给 dws 1.0.63 的契约串（2026-10-14 实测）

集中定义在 `scripts/sources/common.py` 顶部。这些字符串与 CLI 输出逐字匹配；CLI 措辞变化时必须同步更新，否则恢复/降级路径静默失效（失效方向保守：不恢复、降级 metadata-only，失败账本仍可见）：

- `MISSING_COLLECTION_MESSAGE`（「下游缺少明确的集合字段，无法判定结果为空」）：chats.py 缺集合 bug 有界重读的触发条件之一，还需严格匹配 category/reason/origin/operation 与全部 failures 字段（见 `recoverable_message_shape`）。
- `OPERATOR_NOT_IN_TASK`：todo.py 把 participant-only 任务降级为 metadata-only 的依据。
- `SPREADSHEET_MARKERS` / `AI_TABLE_MARKERS`：documents.py 按 CLI 错误文本推断 axls/able 类型。

共享常量同在 common.py，改一处生效两处：`TERMINAL_ERROR_CATEGORIES`（chats.call 与 Client.read 两套客户端的终止语义）、`CONTEXT_SOURCES`（context.py 的 SOURCES 与 context_index.py 的 WEEK_SOURCES）、`dws_command`、`acquire_window_lock`。`partial_collections` 的集合键清单是该函数的注册点：新增源适配器的列表键必须登记，否则失败重试的部分行不合并（fail-safe，complete=false 仍暴露）。

## 入口行为契约

- `export-dialogues.sh` 的模式字（auto/collect/update/merge）必须是脚本后第一个参数：export_dialogues.py 分发器只检查 argv[1]，flags 在前会被静默插入 auto。
- 默认模式 auto：当日（或 `--start` 所指日）文件存在→增量 update，不存在→collect；显式 collect 撞已存在输出退出码 1，`--force-full` 才重采。与 collect-context.sh 的 auto 行为对称。
- 两个入口（chats.py / context.py）都持 `<输出>.lock` 文件锁；锁定义只有一份（common.py）。
- 规范根硬编码 `~/.qoder/dws-personal-context`（`chats.DATA_ROOT`），刻意不读 QODER_CONFIG_DIR：开发态覆盖会让读写落到两个目录并静默分叉。

## 刻意不修（2026-10-20 review 决定，勿当遗漏）

- **不合并 chats.call 与 sources/common.Client.read 两套重试引擎**：语义不同（ledger 保留每次部分消息 vs partial_collections 合并重试行），测试覆盖现状，合并的回归风险大于收敛收益。
- **不加 `dws --version` 前置校验**：契约串失配时行为本来就保守退化且账本可见，多一次子进程调用不值。
- **不重构 partial_collections 注册表**：漏登记 fail-safe，注释已标明登记点。
- **redact.py KV 赋值 ≥8 字符下限**：已知局限，SKILL.md 已声明「规则不保证识别任意秘密格式」。
- **chats.py import 期 `os.umask(0o077)` 全局副作用**：CLI 场景无害且保证输出 600 权限，保留。

## SKILL.md 文档分层

按渐进式披露维护，`tests/test_skill_docs.py` 是硬性守卫：

- **SKILL.md 只放路由与不变量**：触发边界、流程骨架、产物路径、写回摘要、回答格式、当前操作限制、越界处理。目标 ≤7,000 字符 / ≤110 行；超限说明细节该下沉。
- **细节进 references/**：具体命令、参数、失败语义、历史核实记录分别放 `sources.md` / `digest-sop.md` / `publish-digest.md` / `dialogues-protocol.md` / `verification-log.md`；SKILL.md 保留一行链接与触发条件。
- **核实记录按日期归档在 [verification-log.md](verification-log.md)**：新增核实事件时追加条目并推进文件内日期口径；不改写旧条目来冒充新核实——旧观测是快照凭证，复测过才能更新。SKILL.md「当前操作限制」只保留仍然生效的结论。
- 改文档后跑 `python3 -m unittest tests.test_skill_docs -v` 与全量测试；链接必须是可解析的相对路径。

## 写回钉钉文档契约（2026-10-21 新增）

唯一写出口：用户显式要求时，发布 `digest/*.md`；原始 `raw/`、聊天原文和任意本地文件不发布。发布内容为 `scripts/publish_view.py` 生成的发布视图（机器 ID 来源引用压缩、裸 `chat:cid` 按 raw 索引富化为 `私聊·人名`，残留 ID 非零退出，`tests/test_publish_view.py` 守护）；本地 digest 不因发布改写。新建一律进「我的文档」根目录的固定文件夹：`scripts/ensure_publish_folder.py` 用 `wiki space list --type myWikiSpace` 定位个人空间，分页读 `wiki node list` 根节点，按 `nodeType=folder` + 名称完全一致解析；唯一命中复用、零命中用 `wiki node create --type folder` 创建、多个命中或分页不完整即停且不回落到根目录（默认 `日报`，`tests/test_ensure_publish_folder.py` 守护）。刻意不用 `drive +create-folder`：它建在钉盘个人空间，会落到「我的文件（来自钉盘）」挂载点下而不是「我的文档」根。用户给定文件夹 ID 时直接使用。新建用 `dws doc +create --folder`（`confirmation=not_required`）；更新用 `+checkpoint-update`（`confirmation=user_required`，默认 append），不改文档位置；overwrite 仅限用户明确要求且目标为已发布副本。发布前校验 digest 的覆盖口径/快照指针与链接/凭证；raw mtime 晚于快照时提示过期。以上预检、文件夹解析、发布视图、建/更与回读统一由 `scripts/publish_digest.py` 编排（`check` 只读；`create`/`update` 为写操作，`update` 必须显式 `--yes`；overwrite 仅限含 footer 的发布副本），不靠 Agent 手工串 `publish_view.py`/`ensure_publish_folder.py`/`dws doc` 命令；`tests/test_publish_digest.py` 守护。不发送消息、不建待办/日程/审批、不授权/分享/评论/删除。完整 SOP 见[publish-digest.md](publish-digest.md)。本机 dws 1.0.62 的 `+create --dry-run`/Schema 与 `wiki space list --type myWikiSpace`/`wiki node list`/`wiki node create --type folder` 已核实；本文件仍按 1.0.63 pin 维护。

## 取证与采集性能契约（2026-10-21 新增）

批量取证：`context_index.py read/cite` 支持 `--all-messages`（仅 chat）、重复 `--id` 与 `--all`，同一份原始文件只解析一次。多条输出 `{requested,resolved,results[]}`，`results[i]` 与对应单条调用逐字段相同，单条选择仍返回原有形状（不包 `results`）；解析不到的 ID 降级为该条 `{source,id,error}` 并让退出码变 `1`，不吞掉整批；`--root` 跨天合并模式拒绝批量选择。写日报取几十条引用一律走批量，不逐条起进程、不手写 jq/python 解析（`tests/test_context_index.py` 守护）。

耗时埋点只加字段不改语义：各源 `coverage.elapsedMs`、整轮 `meta.elapsedMs`，索引透传，`week` JSON 暴露 `days[].collectMs` 与各源 `elapsedMs`；chat 另有相位级 `coverage.timings`（identity/discovery/readPhase/networkWait/fetch/checkpoint/conversations；`fetchMs` 是各请求自身耗时之和，`fetchMs/readPhaseMs` 即产物自证的重叠加速比——`readPhaseMs≈networkWaitMs+checkpointMs` 对串行/并发恒成立，不能单独用来判断是否并发生效），为此在 `export_dialogues.py` 的公共格式 coverage 白名单里加了 `timings`（只收整数，不含账号数据），`decode_dataset` 原样回读，续读语义不变。`week --markdown` 是 digest 覆盖口径的唯一来源，**刻意不显示耗时**，避免格式漂移。请求次数看 `coverage.reads` 长度，不另设计数器。

## 已知边界

- 需 python ≥3.9（zoneinfo）；无版本检查，旧解释器直接 ImportError。
- 消息搜索补充探测上限 6 次调用、每次页钳 40、未验证 CID 判型并发 8（`search_message_candidates` / `enrich_with_message_search`）；`--retries`/`--timeout` 约束单次调用，不约束这批调用的总量。**搜索调用超时按页预算缩放**（2026-10-22 修复）：单次调用在服务端连续翻页，满 40 页实测 55–70s，裸 `--timeout`（默认 60s）会在正常尾延迟上触发超时、作废整次已分页尝试再整段重读（实测一跑损失 ~61s）；现为 `max(timeout, 页数×2.5+10)`（默认页预算下 floor 110s），目录与其余调用仍用基础 `--timeout`（`tests/test_chats.py::CollectionRegressionTests::test_search_invocation_timeout_scales_with_the_page_budget` 守护）。
- **消息搜索分页是采集的剩余主成本，勿按 bug 修**（2026-10-22 实测归因）：10-21 单日全量 379.7s 中 chat 323.5s ≈ identity 4s + 目录发现 ~221s + 串行读取 ~98s；目录发现里的消息搜索补充分页（~161 页 × ~1.4s/页 ≈ 220s）占新总时长 ~72%，是真正的瓶颈。cursor 链式翻页天然串行，页上限钳 40 与 6 次调用上限是刻意的上游保守性，提页提速只会放大超时风险与丢页概率；除非上游提供并行分页或更大页型，此成本不可再降。读取侧并发后全量 chat 323.5→251.7s（读取 98→~27s，~3.6x；受控探针 8 路并发 2.65x、预取循环 16 会话 3.14x）。
- 逐会话 `+chat-messages` 读取按 `--message-workers`（1-8，默认 4；`context.py` 传 `min(8, --workers)`）有界并发：只重叠网络请求，账本顺序、逐会话 checkpoint、未完成 CID 保留与串行逐字段一致，中断续读语义不变；读取区间改为发请求前统一校验，无效区间立即失败且不再有部分写入（`tests/test_chats.py::MessageReadOverlapTests` 用调用区间重叠证明并发、用 `--message-workers 1` 证明可退回串行）。上游未公布并发上限，因此钳在 8。
- **非 chat 源不做并发**（2026-10-21 实测后决定，勿重复评估）：单轮全量采集中 chat 占约 85% 墙钟，其余六源合计约 15% 且其中一源独占大半；并发化非 chat 源收益上限 <5%，却要动 `save_progress` 的逐源原子 checkpoint 与 `refreshedSources` 确定性。要提速先动 chat。
- 目录全集覆盖未获证明（见 [verification-log.md](verification-log.md) 与 SKILL.md「当前操作限制」），complete=false 是常态而非异常。

## 安装名称与数据格式

技能名与目录为 `dws-personal-context`，中文标题为“钉钉个人上下文与日报”。规范安装目录是 `~/.qoder/skills/dws-personal-context/`。数据固定存储在 `~/.qoder/dws-personal-context/`，不枚举或回退到其他目录。单聊协议固定为 `dws-personal-context-dialogues` schemaVersion=2，多源协议固定为 schemaVersion=3，索引固定为 `dws-personal-context-index` schemaVersion=1，发布副本以 `完整取证见本地 digest` 识别、可见文案为 `由 dws-personal-context 发布`。这些都是各自唯一的正式契约，不提供格式升级、标记别名、旧路径或导入回退；仅对改名前写入的单聊与索引文件保留只读兼容（兼容常量见 `chats.py` / `context_index.py`），再次写出一律用新标识。

## 分发检查

分发包只包含技能说明、通用参考、脚本和虚构测试样本，不包含账号级核实日志、运行数据、缓存、发布回执或本机路径。对外标识示例使用带 Synthetic 前缀的虚构 ID；链接使用保留域名或公开开发文档域名。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 scripts/audit_distribution.py
```
