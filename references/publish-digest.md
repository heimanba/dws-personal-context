# 写回钉钉文档（digest → adoc）

这是本技能唯一的钉钉写操作。只有用户明确要求把日报/周报/结论写到钉钉文档时才执行；未要求时保持零写。只发布 `~/.qoder/dws-personal-context/digest/` 下的 digest 文件，`raw/`、聊天原文、任意本地文件一律不发布。用户要求发布的是临时回答而非 digest 时，先在 `digest/` 落一份同内容的报告文件，再按本 SOP 发布；不直接发布聊天文本。

## 前置条件

1. digest 已由 Agent 按「日报 / 周报 SOP」写入 `~/.qoder/dws-personal-context/digest/<key>.md`，覆盖口径与链接自检已完成；结构按[呈现契约](digest-sop.md#呈现契约)，完整 cite 行收在文末 `## 证据附录`。
2. 用户明确表达了发布意图；只是问「能不能发」时先回答，不写。
3. 发布前运行只读预检，退出码 0 才继续：

```bash
python3 ~/.qoder/skills/dws-personal-context/scripts/publish_digest.py check ~/.qoder/dws-personal-context/digest/2026-10-21.md
```

预检由脚本机器校验，不再人工 grep 或手比 mtime：

- digest 含 `## 覆盖口径`，且开头有机器生成的 `数据源：raw/...` 与 `快照：...` 两行。
- 日期范围内每一天都必须存在 raw 文件和有效快照；两者都缺失也拒绝发布。
- 快照时间与对应 `raw/<起始日>/context.json` 的 mtime 对比（多日 digest 逐日对比）；raw 晚于快照 60 秒以上即 `stale_snapshot`，提示用户重跑 `context_index.py week --markdown` 或更新 digest，不静默发布。
- 正文不含 `sk-`、`Bearer `、`password=`、`secret=`、`token=` 等凭证。
- 链接不含 `…`、空格、`_dt_ac=`、`_dt_sig=`、`[REDACTED_CREDENTIAL]`，不以 `?`/`&` 结尾。
- 发布视图无残留机器 ID（`chat:`/`msg:`/`mail:`/`minutes:`/`calendar:`）；`## 证据附录` 整节不进入发布视图，因此不参与该检查。
- 缺 `## 证据附录` 只产生 `evidence_appendix_missing` 告警（退出码 0），用于提示旧格式 digest 未做引用分层，不阻断发布。

输出 JSON 的 `problems[].code` 即失败原因；只报告行号，不回显凭证。预检不替代逐条 `cite` 核对，但替代人工字面扫描。

## 发布视图

发布的不是 digest 原文，而是 `scripts/publish_view.py` 生成的发布视图：机器 ID 来源引用压缩成读者标签——`时间 | peer 名字 | chat:cid msg:msg` → `私聊·名字 MM-DD HH:MM`；裸 `chat:cid msg:msg`（表格/TODO 形态）按 digest 文件名对应的 `raw/<日>/context.index.json` 会话标题富化为 `私聊·人名`（`--root` 可覆盖；人名已在引用前缀时不重复；cid 尾部 `…` 截断按唯一前缀匹配；查不到回退 私聊）；裸 `mail:/minutes:/calendar:` → `邮件/听记/日程`，连续相同标签折叠为 `邮件×3`；digest 可读性格式 `渠道·名称（IDs）` 的 ID 括号被剥离。URL 与代码块/行内代码受到保护，不参与 ID 替换或残留检查。**`## 证据附录` 整节剔除**（本地取证层，写 digest 时必须放文末；标题出现在代码块/URL 内时不触发）。除该节外，事实内容、URL、覆盖口径一律不动；本地 digest 保持 cite 原文照抄的完整取证版，不因发布改写。

`publish_digest.py` 在新建和更新内部调用发布视图，并对输出做残留机器 ID 硬门禁：非零退出即中止发布，不带着 ID 出门。凭证/链接扫描仍跑本地 digest——发布视图只删不改，本地文件是超集。转换契约由 `tests/test_publish_view.py` 守护。

## 目标解析

| 用户给了什么 | 动作 |
|---|---|
| 文档 URL / nodeId | `publish_digest.py update <digest> --node <ID或URL> --yes`；overwrite 前脚本先判断是否为 ctxring 发布副本 |
| 只有标题 | `dws doc +search --query "<标题>" --page-all --max-pages 20 -f json`；仅当标题完全一致、类型为 adoc、候选唯一时才提议更新；零/多候选停止并展示候选，禁止默认第一项 |
| 什么都没给 | `publish_digest.py create <digest>`：新建到「我的文档」根目录的固定文件夹 `日报`；标题取 digest 第一个 H1（去掉 `#` 和首尾空白），无 H1 用 `工作日报 · <日期>` |
| 指定文件夹名 | 新建时按该名称解析固定文件夹：`--folder-name <名称>`，同名唯一则复用，不存在则创建 |
| 指定文件夹/知识库 | 新建时才传 `--folder <ID>` 或由 Agent 走 `dws doc +create --workspace <ID>`；ID 必须来自用户或真实返回，不猜 |

## 固定文件夹（仅新建）

新建 digest 文档一律放入「我的文档」根目录的固定文件夹，默认 `日报`；用户点名其他文件夹时以用户为准。`publish_digest.py create` 内部调用 `ensure_publish_folder.py` 解析，存在则复用，不存在则创建。排障时可单独运行：

```bash
python3 ~/.qoder/skills/dws-personal-context/scripts/ensure_publish_folder.py              # 默认 日报
python3 ~/.qoder/skills/dws-personal-context/scripts/ensure_publish_folder.py --name 周报  # 用户点名
```

- 输出 `{"name":"日报","nodeId":"<ID>","created":false}`；`create` 自动把 `nodeId` 传给 `+create --folder`。
- 脚本用 `wiki space list --type myWikiSpace` 定位「我的文档」个人空间，分页读完 `wiki node list` 根节点；只认 `nodeType=folder` 且名称**完全一致**的条目，「工作日报」、同名文档都不算。
- **不用 `drive +create-folder`**：它建在钉盘个人空间，在「我的文档」里表现为「我的文件（来自钉盘）」下的子目录，不是根目录。
- 同名文件夹多个 → 停止并列出 nodeId 交用户选择；分页不完整（缺 `hasMore`/`nextPageToken` 或触达页帽）→ 停止，不创建。
- 解析或建夹失败时中止发布并报告，**不回落到根目录**——静默落在根目录等于绕过固定归档约定。
- 用户直接给了文件夹 URL/nodeId 时用 `--folder <ID>` 跳过解析；不猜 ID。
- 只影响新建；更新已有文档不改位置，也不自动移动历史发布副本。

## 新建

```bash
# 正常新建：预检 → 同名检查 → 文件夹解析 → 发布视图 + footer → +create → 回读
python3 ~/.qoder/skills/dws-personal-context/scripts/publish_digest.py create ~/.qoder/dws-personal-context/digest/2026-10-21.md

# 只出计划，不写入；文件夹缺失时返回 wouldCreate=true，不建夹或调用文档创建
python3 ~/.qoder/skills/dws-personal-context/scripts/publish_digest.py create ~/.qoder/dws-personal-context/digest/2026-10-21.md --dry-run

# 用户给定文件夹 ID / 标题覆盖
python3 ~/.qoder/skills/dws-personal-context/scripts/publish_digest.py create <digest> --folder <ID>
python3 ~/.qoder/skills/dws-personal-context/scripts/publish_digest.py create <digest> --name "工作日报 · 2026-10-21"
```

脚本内部固定顺序，不得绕过：

1. 跑「前置条件」的全部预检；有问题退出 1。
2. 按标题精确搜索 adoc；查重失败、分页不完整、结果结构异常或候选回读失败均按未知停止（`duplicate_check_unknown`），不得视为不存在；全部候选都检查，不截取前十个。若存在同名且含 `由 ctxring 发布` footer 的同 key 文档，停止并输出候选，**不自动覆盖**。`--skip-duplicate-check` 只在用户确认要保留重复文档时用。
3. 解析固定文件夹；空 ID、同名歧义、目录不完整都退出 1。dry-run 仅查找，不创建；文件夹缺失时输出建夹及建文档计划，`folder.wouldCreate=true`、`folder.created=false`。
4. 生成发布视图，追加 footer：`> 由 ctxring 发布 · 来源引用已压缩为发布视图，完整取证见本地 digest：digest/<key>.md · 发布：<时刻>`。
5. `dws doc +create --name <标题> --folder <nodeId> --content - --format json`。
6. 正常创建返回 `status=success`、`complete=true`、真实 `nodeId` 且 `data.verified=true` 时通过；否则按返回的真实 ID 用 `+inspect` 和 `+fetch --scope full` 核验全文。CLI 非零退出时也先解析 stdout/stderr JSON 的 `error.details` 或直接操作回执，保留 nodeId、步骤和补偿信息，不能先截断错误再解析。
7. 全文精确相等，或由 `markdown-it-py` 解析后结构等价，才恢复为成功；忽略源位置、分隔符拼写和松紧列表包装，不删除文本空格，不忽略代码、链接、数字、标题或表格单元格差异。非精确匹配时还核对引用链接定义（包括未使用和重复定义），并拒绝行列不齐的表格，避免解析器丢弃原文后误报等价。格式恢复需要 `markdown-it-py`；依赖缺失、全文不匹配或回读失败均保留部分成功，不降级成仅查 footer。

输出 JSON：`{ok, action, title, folder, nodeId, url, verification, status, complete, readback, problems}`。退出码 0=已验证（`verified`、`full_readback` 或 `full_readback_normalized`）；2=创建状态不确定或回读未确认，输出真实 `nodeId`（若已返回）与 `doNotRetryCreate=true`，先按 ID/标题定位，禁止重跑创建；1=预检或 dry-run 失败。`cliStatus`/`cliComplete` 保留上游原始状态，`status`/`complete` 表示独立核验后的结论。`--dry-run` 返回 `executed=false` 与计划，退出 0 不代表已写入。

创建回执与重入：脚本通过 `profile list` 解析稳定账号并固定本次后续调用。`digest/.publish-receipts/` 按账号和标题保存回执并加非阻塞文件锁；dry-run 只读取，不创建回执。真实调用 `doc +create` 前先持久化 pending，收到响应后、回读前即保存 nodeId；原子替换、fsync，回执与锁权限为 600。即使进程中断、没有 footer、摘要发生改变或传 `--skip-duplicate-check`，同账号同标题仍不能再次创建；存在回执返回 `existing_create_receipt` 和已知 ID。应按 ID/标题检查现有文档，再更新已有副本，或在明确需要独立副本时使用新标题。不要删除回执来绕过未知写入。无法解析账号、回执损坏、持久化失败或锁被占用时停止；已记录的 ID 会尽可能随错误返回。stdout/stderr 的业务错误封装独立于进程退出码解析。

失败语义：`partial_success` / `status=unknown` 按返回的 nodeId/标题回读定位，禁止重跑整条创建；若返回未带 URL，用 `dws doc +inspect --node <nodeId> -f json` 取真实链接，不拼接、不猜测。`--folder ""` 会被 CLI 当作未传，脚本已用 `empty_folder` 门禁拦截。

## 更新

```bash
python3 ~/.qoder/skills/dws-personal-context/scripts/publish_digest.py update \
  ~/.qoder/dws-personal-context/digest/2026-10-21.md --node "<nodeId或URL>" --mode append --yes
```

- `--yes` 只有在会话里已确认「更新哪个文档、更新什么内容」后才加；其余情况先 `--dry-run` 展示计划。脚本对 `+checkpoint-update` 固定走「存版本 → 写 → 回读」。
- 默认只追加。`--mode overwrite` 前脚本先 `doc +fetch --scope keyword --keyword "由 ctxring 发布"`；不是 ctxring 发布副本直接失败（`overwrite_requires_ctxring_copy`），没有绕过开关。用户明确说「覆盖/替换」且目标是副本时才用 overwrite。
- `REVISION_CONFLICT`：重新读取当前版本，展示差异；未经用户确认不得改成无 revision 覆盖。
- `partial_success`：保留已完成步骤，不回放成功步骤；`doc_write_commit_unknown`：先回读确认状态，禁止自动重试创建或追加。
- 更新回读使用 footer 中唯一的 `更新：<时刻>` 关键字；`+inspect` 确认节点存在、`+fetch` 确认内容命中才返回 `ok=true`。

## 禁止

- 不发送消息/群通知；不创建待办、日程、审批；不授权/分享/评论/删除文档。
- 不发布 `raw/`、聊天原文、凭证、未脱敏内容；不把 digest 改写成另一份内容再发布。
- 用户未明确要求时，不写任何钉钉线上数据。
- 不把发布链接发到任何群或人（发送消息仍属禁止；只把链接报告给当前用户）。

## 回报

发布成功后报告：doc 标题、nodeId、URL、所属文件夹（新建时）、对应 digest 路径、发布时间、验证方式（`verification=verified` 或 `readback`）。失败时报告已完成步骤和真实 nodeId，不编造链接，不把部分成功说成成功。脚本的 JSON 输出可直接作为回报依据。

## 验证与分发

自动测试使用合成响应验证文件夹解析、发布回执、全文核验、重入与失败状态。真实核验在使用者账号中执行，结果保存在使用者本地，文档 ID、空间 ID、本机路径与业务内容不得写入 skill。分发前运行 `python3 scripts/audit_distribution.py`。
