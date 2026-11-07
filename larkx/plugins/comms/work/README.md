# 交互协议 work：旁听、开发模式、点名回复

启用：`larkx plugin use --comms work`，或者 `larkx plugin use --memory work`（记忆策略 work 默认带上它）。

## 做了什么

- **回复格式**：回别人时第一行先写收件人名字加冒号（「张三：」，多人「张三、李四：」），纯文字，不真的 @。本人在私聊里直接下指令时不用写。
- **会话配置**（`larkx group`，只有本人能改）：
  - `--listen on`：旁听。别人发的消息不用 @、不用 `/ai` 也交给数字人，由它判断要不要回（宁缺毋滥；@ 了本人或在回本人的消息一定回）。
  - `--dev on`：开发模式。别人发起的一轮不按访客收窄，可以查代码、跑命令、改代码，仍只能在本会话发消息。
  - `--allow` / `--disallow` / `--allow-all`：会话白名单，非空时只有名单里的人被旁听、享有开发模式。
  - `--instruction` / `--workdir`：附在这个会话每一轮提示词里。
  - `--max-turns`：每 10 分钟最多交给数字人几条（默认 20），超了跳过并告警。
  - `--bots on`：旁听时也处理机器人发的。
  - `p2p`（或“私聊”）是所有私聊共用的一份配置。
- **白名单私聊自动旁听**：唤起白名单里的人和本人的私聊，没有单独配置时默认旁听，只处理加进白名单之后的消息。群不会因为白名单自动旁听。
- 旁听只处理 5 分钟内的消息（daemon 停过再起，补收的旧消息不回），跳过系统消息和已排过队的。

## 代码

- `__init__.py`：`WorkComms`，实现 `listen_profile` / `listen_ok` / `message_context` / `prompt_rules` 四个钩子。
- `profiles.py`：会话配置的读写（表 `chat_configs`，在 `models.py`；旧库里叫 `chat_profiles` 的那张启动时自动改名）。
- `notes.py`：附在提示词里的说明文字。
- `cli.py`：`larkx group`。
- `persona.md` / `skill.md`：启用时追加到人设和数字人 skill 里的内容。
