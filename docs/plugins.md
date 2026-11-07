# 插件：记忆策略和交互协议

lark-twin 的主体（协议、收发、触发规则、接管、定时任务、控制台）不动；"怎么记"和"怎么跟别人往来"做成两类可替换的插件：

| 类 | 目录 | 管什么 | 配置 |
|---|---|---|---|
| 记忆策略 | `larkx/plugins/memory/` | 记忆落在哪、何时消化、codex 会话怎么切、每轮带什么前情 | `LARKX_MEMORY`，默认 `default` |
| 交互协议 | `larkx/plugins/comms/` | 没人叫数字人时要不要接话、回复格式、别人能让它做多少事 | `LARKX_COMMS`，不写时取记忆策略的 `default_comms` |

`default` 的每个钩子都是空实现，行为和上游一致。现有的非默认插件：

- 记忆策略 `work`：三层工作记忆、每小时消化、读本人自己的 agent 会话、codex 每个会话一个 thread。见 [docs/memory/work-memory.md](memory/work-memory.md)。
- 交互协议 `work`：旁听、开发模式、会话白名单、专属指令、点名回复。见 [larkx/plugins/comms/work/README.md](../larkx/plugins/comms/work/README.md)。

## 切换

```bash
larkx plugin list
larkx plugin use --memory work [--comms default]   # 写进项目 .env, 重启 daemon 生效
larkx --memory work --comms default <命令>          # 只这一次
```

`larkx plugin use` 数字人不能调用。

## 加载流程（`larkx/plugins/__init__.py`）

1. `load_config()` 最后调 `apply_config(cfg)`：校验名字，交互协议跟随记忆策略，把插件 `config` 里的默认值读成 `cfg[key]`（环境变量 `LARKX_<KEY>`），再调两个插件类的 `configure(cfg)`。
2. `Storage()` 建表前调 `load_models()`：**所有**插件的 `models_module` 都 import，表一直都建，切换不丢数据。给已有表补列，往 `larkx.storage.ADDED_COLUMNS` 里登记。
3. `ensure_agent_home()`：人设 `CLAUDE.md` 末尾维护一个由 `plugins.PERSONA_BEGIN` / `PERSONA_END` 两行注释包住的区块，内容是两个插件的 `persona()`；数字人 skill 是基础 SKILL.md 加上两个插件的 `skill()`。切回 default 时区块整个去掉，区块外用户的改动保留。然后调 `memory.ensure_home(home)`。
4. `Daemon.__init__`：`self.memory, self.comms = plugins.load(cfg)`。
5. CLI：`register_cli(sub)` 对**所有**插件都调，子命令一直存在；命令开头用 `plugins.require(kind, name)` 检查，没启用时提示怎么启用。

## 钩子

记忆策略（`larkx/plugins/memory/base.py`，`MemoryStrategy`）：

| 钩子 | 调用处 | 默认 |
|---|---|---|
| `configure(cfg)` 类方法 | 加载配置 | `agent_session` 没写就用 `single` |
| `ensure_home(home)` | 数字人目录初始化 | 无 |
| `persona()` / `skill()` | 人设区块 / skill | 空 |
| `ask_options(ev)` | `_run_turn`，传给 `session.ask(prompt, **opts)` | `{}` |
| `tick(daemon)` | 调度循环，每 15 秒 | 无 |
| `replaced_jobs()` | `daemon/maintenance.py`：这些例行任务（`larkx/jobs.py`，如每日回顾 `review`）不再按时跑 | 空集合 |
| `skip_event(ev)` | `_process` 开头 | `False` |
| `after_event(daemon, ev, result)` | `_process` 的 finally | 无 |

交互协议（`larkx/plugins/comms/base.py`，`CommsProtocol`）：

| 钩子 | 调用处 | 默认 |
|---|---|---|
| `listen_profile(daemon, msg)` | `on_message`，消息不是本人、不在接管、没触发时 | `None`（不旁听） |
| `listen_ok(daemon, msg, profile, chat_name)` | 同上，旁听的这条要不要真的排队 | `True` |
| `message_context(daemon, ev)` | `_handle`，排队后、交给数字人前 | `{'skip': False, 'dev': False, 'note': '', 'watch_note': ''}` |
| `prompt_rules(type_name, is_owner, dev)` | `build_prompt` | `('', None)`：不加回复规则，访客规则不变 |

daemon 给插件用的公共件：`daemon.turn_lock`（和普通消息互斥地跑一轮）、`daemon._commit(msg)`（提交数字人目录的 git）、`daemon._load_state()` / `_save_state()`（`daemon.json`）、`Event.meta`（插件自己的事件附带数据）。

## 加一个新插件

以记忆策略 `foo` 为例：

1. 建 `larkx/plugins/memory/foo/__init__.py`，`class FooMemory(MemoryStrategy)`，设 `name`、`description`，只覆盖要改的钩子。可以把 `work` 的代码复制过来改，插件之间不互相 import。
2. 在 `larkx/plugins/memory/__init__.py` 的 `REGISTRY` 加 `'foo': 'larkx.plugins.memory.foo:FooMemory'`。
3. 有自己的表：写 `models.py`（继承 `larkx.storage.models.Base`），设 `models_module`。有配置项：写进 `config = {'foo_interval': 600}`，读 `cfg['foo_interval']`。
4. 有 `.md` 资源的话，在 `pyproject.toml` 的 package-data 里加上。
5. 测试：`tests/test_plugins.py` 里有默认值、切换、人设区块的例子。

交互协议同理，放 `larkx/plugins/comms/`。

## 不属于插件的能力

富文本发送、云文档评论监听和回复（`larkx doc`）、`send --file` / `--thread`、`larkx watch` / `inbox` / `ping`、签名去重、`chat_reminds` 客户端接口。这些不改变已有行为，所以任何组合下都开着。
