## 工作记忆（记忆策略 work）

- `larkx work-memory`：看工作记忆状态（总览字数、各维度个数、消化到哪了）。
- `larkx work-memory feed --since 2h`：看某段时间里本人没开免打扰的会话的增量（消化轮已经把增量放在提示里，一般不用自己查）。
- `larkx work-memory archive --dry-run`：预演归档规则。归档由 daemon 自动跑，不要手动移动 `archive/` 里的文件。
- 访客发起的轮次不能用 `larkx work-memory`。
