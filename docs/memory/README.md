# 记忆策略

数字人怎么记、怎么接上前情，由记忆策略决定（`LARKX_MEMORY`，见 [../plugins.md](../plugins.md)）。

| 策略 | 记在哪 | 什么时候整理 | 会话 | 文档 |
|---|---|---|---|---|
| `default` | `memory/MEMORY.md` 索引 + 各条记忆文件（可选结构化字段） | 每日回顾、每周整理 | codex 后端所有消息一个 thread | 主 README 的"记忆"相关章节 |
| `work` | 在 default 之上加 `memory/work/` 三层工作记忆 | 每小时增量消化（代替每日回顾），代码按时间归档 | codex 每个飞书会话一个 thread，空闲久了开新的 | [work-memory.md](work-memory.md) |

切换：`larkx plugin use --memory work`，切回 `larkx plugin use --memory default`。两者的数据互不影响，切换不丢东西。

新写一个记忆策略：在 `larkx/plugins/memory/` 下建目录、继承 `MemoryStrategy`、在 `REGISTRY` 里登记，设计文档放这里。步骤见 [../plugins.md](../plugins.md#加一个新插件)。
