# 数据源路由与命令

采集全部只读；写回 digest 见 [publish-digest.md](publish-digest.md)。采集/更新/索引读取的完整机制见 [context-collection.md](context-collection.md)，单聊脚本协议见 [dialogues-protocol.md](dialogues-protocol.md)。

## IM（仅私聊）

| 场景 | 命令 |
|---|---|
| 窗口内私聊采集 | `scripts/export-dialogues.sh`（默认今天 00:00 → 现在，生成 `~/.qoder/dws-personal-context/raw/<窗口起始日>/dialogues.json`） |
| 与某人的 1:1 聊天 | `dws chat +chat-messages --user <userId> --start "..." --end "..." --order asc`（或 `--open-dingtalk-id <id>`；已知单聊 cid 也可用 `--open-conversation-id <cid>`） |
| 增量刷新 | `scripts/export-dialogues.sh update --base <基线文件>`；只补基线内会话缺口，并尽力探测基线外新会话（`--no-universe` 可关探测） |
| 最近会话清单 / 新会话探测 | `dws chat +chat-list --types p2p --page-size 100 --page-all`——尽力而为，完整性不可证明 |
| 私聊内关键词 / 内容检索 | 多源产出用 `context_index.py search`；仅有 `dialogues.json` 时用本地 jq 过滤（脚本保证只含私聊） |

**采集纪律**：同日重复提问复用既有产出——默认路径按窗口起始日固定，直接重跑 `export-dialogues.sh`（多源为 `collect-context.sh`）即以当日文件为基线增量更新，不要另起文件名或换目录。只读取经 `conversation-info` 验证为单聊的会话正文；目录和消息搜索底层可能包含群会话元数据，不能称为“零群流量”。跨会话 `+search-msg` 仅用于发现候选 CID，不用于采集正文；正文只按验证后的 CID 调用 `+chat-messages`。

## 其他源

| 源 | 命令 |
|---|---|
| 日历 | `dws calendar event list --start <ISO8601> --end <ISO8601>`，按真实游标翻页 |
| 待办 | 按角色分别 `dws todo task list --role-types <role>` 合并任务和 roles；仅 creator/executor 调用 `task get --task-id <taskId>`，participant-only 保留列表元数据；详情明确拒绝 `operator not in task` 时也降级为无详情 |
| 审批 | `list-pending` 使用必填 `--start/--end`；`list-submitted / list-executed / list-cc` 不支持时间筛选，只能按返回的创建时间在本地过滤。四类列表单页上限 20。保留角色来源后再读取详情。窗口内 0 条不是「无审批」的强证据：submitted/executed/cc 是当前状态快照 + 本地时间过滤，解释依据看 `coverage.scope.localTimeFilter.perRole`（returned=接口返回、kept=落入窗口） |
| 听记 | `dws minutes +search --scope all --start <RFC3339> --end <RFC3339> --page-all` → `+detail --id <taskUuid> --artifacts basic,summary,keywords` |
| 邮件 | `dws mail mailbox list` 获取本人邮箱，再 `message search --email <邮箱> --query <KQL>` → `message get --email <邮箱> --id <messageId>` |
| 文档 / 钉盘 | `dws drive +recent --operate-type 1 --page-all` → 按 `docUrl` 和读取错误推断真实类型；仅 ALIDOC 读取 `doc +fetch --node <nodeId> --scope full`，表格/其他类型保留元数据 |
| 人名解析 | `dws contact`（姓名 → userId；固定后复用；同名多候选先消歧） |

## 采集入口与范围

跨源采集用 `scripts/collect-context.sh`，后续用 `scripts/update-context.sh --base <文件>`。需要多源采集、选择角色/邮箱、恢复失败项时读 [context-collection.md](context-collection.md)。各源分别记录查询范围和完整性，不能把最近列表或状态快照当成全部历史。

- `collect-context.sh` 默认 auto：输出不存在则全量采集，已存在则以该文件为基线更新并原地替换；默认写 `raw/<窗口起始日>/context.json` + `.index.json`。
- 补采历史日必须显式 `--start "当日 00:00:00" --end "次日 00:00:00"`；对历史日文件裸跑 update 会把窗口扩到当前时刻。
- 可选范围：`--calendar-id`、`--todo-roles`、`--todo-status`、`--todo-window`、`--approval-roles`、`--minutes-scope`、`--mailbox`、`--profile`。细节见 context-collection.md。
