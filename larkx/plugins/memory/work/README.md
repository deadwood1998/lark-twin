# 记忆策略 work：工作记忆

设计思路和规格见 [docs/memory/work-memory.md](../../../../docs/memory/work-memory.md)。

启用：`larkx plugin use --memory work`。要求 codex 后端。

- `__init__.py`：插件入口（钩子实现）、消化循环
- `layout.py`：目录结构、记忆规则 `RULES.md`、代码执行的归档规则
- `feed.py`：增量消息、免打扰状态（`chat_reminds` 表）
- `sessions.py`：本人的 Codex / Claude Code 会话
- `models.py`：`chat_reminds` 表
- `cli.py`：`larkx work-memory`
- `persona.md` / `skill.md`：启用时追加到人设和数字人 skill 的内容
