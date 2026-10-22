# 日报 / 周报 SOP

用于日报、周报、TODO 类提问。默认只写 `~/.qoder/dws-personal-context/digest/`；用户明确要求时再按 [publish-digest.md](publish-digest.md) 发布。

## 1. 采集

单日跑 `collect-context.sh`（当日已有文件自动变为增量更新，不需要 `--out`）；周报 = 各日已有 `raw/<日期>/context.json` + `context_index.py week` 汇总，缺哪天补采哪天，不采集整周大窗口。**补采历史日必须显式 `--start "当日 00:00:00" --end "次日 00:00:00"`**：fresh collect 默认「今天零点→现在」，对历史日文件裸跑 update 会把窗口扩到当前时刻，两者都会造成窗口错位。

## 2. 故障恢复

采集被杀或中断，先 `ls ~/.qoder/dws-personal-context/raw/<日期>/` 查磁盘已有产物，再决定续读；不要为求快并发再跑一个采集——同目标第二进程会被 `.lock` 拒绝（退出码 1）。缺源时 `update-context.sh --base <当日文件>` 补拉，结果原地写回规范路径，不再产生 `-updated.json` 旁路文件。只补失败项用 `update-context.sh --base <当日文件> --only-failed`：按各源原始区间重试未完成的读取，不扩窗、不重读已完成项、不新增源（chat 侧等价于 `export-dialogues.sh update --only-failed`，只重读账本里 `complete!=true` 的 CID）。

## 3. 读取与取证

一律 `context_index.py read/search`；search 聊天命中自带 `cid+msgId+createTime+role`，可直接入引用。周报先用 `week --start <日> --end <日>` 拿逐日覆盖（status/sources）和会话矩阵（totalMessages/perDay），再按矩阵挑重点会话 `read` 原文。**跨天会话直接 `read --root ~/.qoder/dws-personal-context/raw --start <日> --end <日> --source chat --id <cid>` 合并读取**（msgId 去重、时间排序、perDay 账本），不逐日分别读再人工拼接。**引用即取证**：digest 里每条引用的日期/时间/msgId 用 `cite --message-id` 取规范行后照抄，不从中间笔记转写；引用长消息原文前先 cite 取完整未截断正文。

**批量取证**（同一原始文件只解析一次，几十条引用不要逐条起进程）：`cite --index <当日索引> --source chat --id <cid> --all-messages` 一次取完该会话全部消息的规范引用行；重复 `--id` 取多条记录；`--all` 取该源全部条目。批量输出 `{requested,resolved,results[]}`，`results[i]` 与单条调用逐字段相同；单条选择仍返回原有形状（无 `results` 包裹）。解析不到的 ID 降级为该条 `{source,id,error}` 并让整体退出码变 1，不静默丢弃其余结果。`--root` 跨天合并模式只服务单会话，批量走 `--index`。禁止为「一次拿多条」手写 jq/python 解析脚本。

## 4. 覆盖口径

结论必须附各源 complete/stopReason；approval 0 条、目录未证明、`snapshotRetained>0`（条目含历史保留行）都要按[当前操作限制](../SKILL.md#当前操作限制)解释，不得宣称全集。`week --markdown` 里的 `⚠窗口→` 行是跨天混算值，不是单日口径，照抄时标记和脚注不得删。

## 5. 写 digest

单日 `digest/<YYYY-MM-DD>.md`；多日 `digest/<起始>_<结束>.md`（如 `digest/2026-10-12_2026-10-16.md`），日期键与 `raw/<窗口起始日>` 同键。结构：结论 → 覆盖口径 → 时间线/按人事项 → TODO 表（带来源）→ 缺口。

**覆盖口径节整段照抄 `week --markdown` 输出**（单日也跑 `--start <日> --end <日>`），开头两行机器生成的「数据源 + 快照」指针不得手写或省略——快照时刻是 digest 对应 raw 版本的凭证，raw 后续被 update 改动即可据此判断 digest 数字是否过期。**digest 正文（结论、归并、三态判断）由 Agent 写出，脚本不生成**；机械契约全部走脚本：写后跑 `publish_digest.py check <digest>` 校验结构/快照/链接/凭证，用户显式要求发布时用 `publish_digest.py create/update`，不手工串 `publish_view.py` 与 `doc` 命令。默认不回写钉钉。

**链接保留**：来源消息含可点击链接（alidocs 文档/知识库、Aone 工单/CR、代码仓、发布页、数据平台）时，时间线条目与 TODO 表来源列须附 markdown 链接；URL 从 `cite` 全文照抄，不从 `preview` 取（preview 会截断 URL），不得改写或缩短；脱敏占位符参数（如 `_dt_ac=[REDACTED_CREDENTIAL]`）连同其配套签名参数（`_dt_sig` 等）整段剔除后再成链，不手工回填凭证；图片/头像/埋点类 URL 不收录。

**来源引用可读性**：任何来源引用都带人类可读上下文，禁止裸 ID 串——成串同源引用写 `邮件×N（mail:…、…）`；听记/单一来源附名称 `听记·会议名（minutes:ID）`；表格内 chat 写 `私聊·人名 MM-DD HH:MM（chat:cid msg:msg）`；正文完整 cite 行（时间/人名/ID 齐全）保持原样。IDs 括号收尾供 cite 复核，发布视图（publish_view.py）自动剥离。

**写后自检**：digest 中每条 markdown 链接的 URL 必须与 `cite` 原文逐字符一致（脱敏参数剔除者除外），不得含 `…`、空格或以 `?`/`&` 结尾。

## TODO 表

列为：事项 / 来源（`私聊·人名 时间（chat:cid msg:msg）`、`听记·标题（minutes:ID）`、`邮件×N（mail:…）`，或 taskId、eventId、messageId；来源含链接时附从 `cite` 全文照抄的 markdown 链接）/ 责任人 / 截止 / 状态。每条 TODO 都是派生判断，必须带来源并沿用三态标注；未见后续不等于未完成，收到/好不等于完成。

## 交付物路径

- 单日：`digest/<YYYY-MM-DD>.md`（与 `raw/<窗口起始日>` 同键）。
- 多日 / 周报：`digest/<起始>_<结束>.md`。
- 只写本地 `~/.qoder/dws-personal-context/digest/`；不写项目目录、当前目录或 `/tmp`。
