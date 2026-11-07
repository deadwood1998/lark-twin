## 工作记忆（记忆策略 work）

- 不同会话的消息可能进同一个你，也可能是各自的新会话：新会话不带之前的对话，前情从记忆接上。
- 工作记忆在 `memory/work/`：`OVERVIEW.md` 是总览和近 24 小时动态，`me.md` 是本人自己在做的事（在哪个仓库、目录、分支，到哪一步），`projects/` `tasks/` `people/` 是三个维度的详情，`archive/` 是按周归档的历史（非必要不读），规则见 `memory/work/RULES.md`。
- daemon 每小时开新会话把增量消化进来；新会话先读 `MEMORY.md` 和 `work/OVERVIEW.md` 接上前情。
