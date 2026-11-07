# lark-twin

**你在飞书上的数字分身。**

它以你本人的账号常驻在线，收到 `/ai` 开头的消息就替你处理。它记得住事，会按时提醒和跟进。驱动它的是你自己的 Claude Code 或 Codex。

> **仅供娱乐。** 本项目通过非官方的飞书网页版协议、以你本人的账号收发消息，它说的话、做的事都算在你名下。使用前请确认符合所在公司和飞书的相关规定，风险自负。聊天记录会存在运行它的机器上，企业内使用时只能部署在企业内的服务器，详见[注意](#注意)。

> 飞书私有协议层源自 [cv-cat/LarkAgentX](https://github.com/cv-cat/LarkAgentX)（`larkx/protocol/`），版权归原作者所有，其余部分为重写。上游未提供许可证，本仓库亦不另行授权。

## 特性

- **本人身份**：基于飞书网页版协议，实时收发消息，无需创建应用或机器人。
- **一个会话**：所有会话共用同一个"你"，重启或崩溃后自动接上之前的对话。
- **长期记忆**：人物、偏好、约定都会写进记忆，每天回顾一次，每次修改都有 git 记录。
- **定时跟进**：可以安排"半小时后提醒我""每天早上汇总谁找过我"这类任务。
- **替你聊**：接管某个会话一段时间，由它代你和对方往来，结束后给你一份总结。
- **有边界**：没有你的指令，不会去别的会话发消息。它在外面说的话会实时抄送给你，异常时会告警，每次发送都有审计记录。
- **不丢消息**：消息先落库再处理；重启后会接着处理没回完的消息，并从服务器补收长连接漏推的消息。
- **看得见**：网页控制台里能看到它在做什么、和谁聊了什么，也能管理任务、接管、记忆和人设，必要时一键打断。
- **不留已读**：数字人收消息、翻聊天记录都不会标为已读，对方看不到已读，你自己的未读提醒也还在。发消息默认是普通发送，对方同样看不到已读，只会清掉你自己在那个会话的未读；想连这个也保住，可以打开 `LARKX_KEEP_UNREAD`（匿名发送，见 `.env.example`）。

## 快速开始

在仓库目录里打开 Claude Code 或 Codex，说一句 **"帮我部署 lark-twin"**，内置的运维 skill 会一步步带你完成。

也可以手动部署：

```bash
uv venv && uv pip install -e .
cp .env.example .env          # 选择 claude / codex，并填写启动命令
.venv/bin/larkx auth qr       # 用飞书扫码登录
.venv/bin/larkx doctor        # 自检
.venv/bin/larkx service install && .venv/bin/larkx service start
.venv/bin/larkx skill install # 让你自己的 Claude Code / Codex 也能操作飞书
```

## 使用

**在飞书里**

| 消息 | 作用 |
|---|---|
| `/ai …` | 交给数字人处理。别人发的，它替你回应；你自己发的，它当作指令执行 |
| `@你 /ai …` | 在群里点名叫它。群里别人发的必须 @你 才算（不带 @ 的 `/ai` 多半是在叫别的机器人，不理会）；你自己在群里发 `/ai …` 不用 @ 自己（`LARKX_TRIGGER_GROUP_AT_ONLY`） |
| `/ai /takeover 2h 目标` · `/ai /release` | 接管 · 结束接管当前会话。接管期间对方说的话都由它回复 |
| `/ai /stop` · `/ai /clear` | 打断当前任务 · 开启新会话，长期记忆保留 |

**在云文档评论里**：有人在评论里 @你，或者评论、回复你的文档时，数字人也收得到（飞书推来的文档通知，不是 IM 消息）。评论里带 `/ai` 时按上面同样的规则触发：别人要 @你，你自己评论不用 @ 自己，白名单和 `LARKX_TRIGGER_OWNER_ONLY` 一样生效，没权限时在评论下回那句 `LARKX_DENY_REPLY`。想让评论里谁都能叫它，打开 `LARKX_TRIGGER_DOC_ANYONE`：不看白名单和只有本人能唤起，但别人发起的只回答和这篇文档有关的问题，能用的命令也只剩读这篇文档、看和回复那条评论（`larkx doc read` / `comment` / `reply`）。你自己的评论飞书不推送，叫不起来。数字人在这条评论下回复（`larkx doc reply`），只发纯文本。回复评论走官方开放平台，需要本机装好 [lark-cli](https://github.com/larksuite/cli) 并以用户身份登录（`LARKX_LARK_CLI`）。评论里不能接管，补收也补不回漏推的文档通知。

以 `/` 开头的命令只认你本人。默认数字人只听你一个人的（`LARKX_TRIGGER_OWNER_ONLY` 默认 `true`，安全优先）：别人发的 `/ai`（私聊、群聊，包括 @你 的）都不理会；你主动接管的会话、定时任务、`larkx say` 照常。想放行少数几个人，用下面的唤起白名单；想让所有人都能唤起，在 `.env` 设 `LARKX_TRIGGER_OWNER_ONLY=false`。

**谁能唤起它（唤起白名单）**：你自己一直能。白名单里有人时，只有你和白名单里的人能唤起，别人的 `/ai` 一律不理；白名单为空时按上面的 `LARKX_TRIGGER_OWNER_ONLY`。群里照旧要 @你。白名单里的人和其他人一样按访客对待，不会拿到你的权限。在控制台的"白名单"页搜名字添加（同名的看头像和部门区分），或者用 `larkx whitelist add <名字或 user_id>`，两边是同一份名单，改了立即生效，不用重启。数字人自己改不了白名单：它调用 `larkx whitelist add/remove` 会被拒绝并通知你。（启用交互协议 `work` 时，白名单里的人和你的私聊还会自动旁听，见下面的[插件](#插件记忆策略和交互协议)。）

**没权限的人想唤起时**：私聊里发 `/ai …`、或者在群里 @你 发 `/ai …` 的人没有权限时，daemon 马上引用回复他一句 `LARKX_DENY_REPLY`（默认"你没有权限唤起{owner}的 lark-twin"，`{owner}` 换成你的名字），不经过数字人、不加签名。同一会话里同一个人 10 分钟内只回一次（`LARKX_DENY_REPLY_COOLDOWN`，秒）。群里没 @你 的 `/ai`、普通聊天、机器人发的都不回；接管中的会话照常交给数字人。`LARKX_DENY_REPLY` 设成空就关掉。每次回复都记进审计（`larkx audit`、控制台"动态"页）。

**在终端，或者你自己的 agent 里**

```bash
larkx send <chat_id> - <<'EOF'   # 以你的身份发消息
larkx send <chat_id> -m -        # 发 markdown 富文本：代码块、列表、标题、引用、加粗/斜体、链接
larkx send <chat_id> --file a.png --file b.pdf   # 发图片(png/jpg/gif/webp ≤10MB)和文件；带正文时先发正文
larkx send <chat_id> --root <msg_id> --thread -  # 回进以那条为根的话题（普通群默认是引用回复）
larkx say --wait "…"             # 直接指挥数字人
larkx task add --in 30m -        # 安排定时任务
larkx takeover start <chat_id> --for 2h --goal "…"   # 让它替你聊一段
larkx mentions --since 24h       # 最近谁找过你，有没有回
larkx whitelist add 张三         # 让张三也能唤起数字人（list / remove 查看、移除）
larkx audit                      # 它发过什么
larkx jobs                       # 每日回顾等例行任务的上次结果和下次时间; jobs log 看运行记录
larkx groups                     # 会话分类和关注度; groups assign <会话名> --to <分类名> 按名字调整
larkx chats --attention 重点关注 # 按关注度、活跃时间筛会话(打开 LARKX_CHAT_GROUPS 后)
larkx prune --dry-run            # 按保留天数清理旧消息, 先看会删多少
larkx organize show              # 看最新的整理方案; organize apply 3 --items 1-5,8 执行、reject 放弃、undo 3 撤销
larkx backfill --dry-run         # 回填最近的聊天历史, 先看规模(打开 LARKX_BACKFILL_DAYS 后启动时自动做一次)
larkx memory list --type 事 --status 待跟进   # 按字段筛记忆; memory lint 检查、memory stats 统计(结构化记忆)
larkx recall <msg_id>            # 撤回一条
larkx urgent <msg_id> [--to 名字]  # 加急本人发的一条(默认应用内; 私聊默认对方); urgent status <msg_id> 看谁确认了
larkx service status | logs -f   # 运行状态
```

数字人发消息默认用纯文本，只在确实需要排版时才用 `-m`；回复别人时第一行写「名字：」点明对象（纯文字，不真的 @），在你自己的私聊里回你时不写。它用 `--file` 发文件时不能发凭证和密钥（`~/.larkx` 里除 `agent/downloads/` 以外的文件、`.env*`、`~/.ssh`、Codex/Claude 登录文件，软链接也按真实路径查），访客发起的一轮里只能发 `~/.larkx/agent/downloads/` 里的文件；被拦下的会记进审计并通知你。你自己在终端用不受限制。

**在浏览器里**

`larkx service install` 会同时装上网页控制台（`lark-twin-web.service`，只监听 `127.0.0.1:18341`），对外由你自己的 Nginx 反向代理。`larkx web url` 打印带令牌的登录链接，打开一次即可；`larkx web reset-token` 让所有已登录的浏览器失效。

打开这个地址就是数字人控制台：总览、对话、动态、任务、接管、白名单、记忆、人设、日志和指挥。以前的 `/console` 地址会跳回首页。

执行 `larkx skill install` 之后，直接对你的 Claude Code 或 Codex 说"给张三发个消息""看看某个群在聊什么"就可以。完整命令见 `larkx --help`。

## 消息监听任务

除了 `/ai` 和定时任务，可以把指定群的消息作为事件，触发一项预设的 agent 任务。底层复用现有消息入库、Task 调度、Codex/Claude 执行、审计和通知；不改变普通 `/ai` 的权限规则。

对数字人说：“创建监听任务，监听某群中 @指定机器人的消息，把同样任务发送到我的测试群并 @测试机器人。”它会确认群、发送人和机器人的身份，创建草稿，展示配置；你确认后启用。也可以说“查看监听任务”“暂停某监听”“查看最近触发记录”。

终端管理（参数为飞书网页版的真实 ID）：

```bash
larkx rule add --name 对照测试 --chat SOURCE_CHAT_ID \
  --sender OWNER_USER_ID --mention SOURCE_BOT_ID --instruction '这里填写预设任务指令'
larkx rule list --json
larkx rule enable 1
larkx rule pause 1
larkx rule runs --json
larkx rule delete 1
```

- 来源群必须精确匹配；发送人、@对象、关键词分别支持重复传入 `--sender`、`--mention`、`--keyword`。同类条件任意命中，不同类同时满足。@按协议身份匹配，普通文字“@名字”不算。
- 草稿不触发；启用/恢复后只处理新的消息，约 15 秒内进入原有任务队列。多个任务按原队列串行执行，耗时受已有任务影响。
- 默认排除本人消息；监听本人任务时显式指定 `--sender`。已审计的 agent/daemon 发送不再触发规则。规则是独立授权，不受普通 `/ai` 的 owner-only、前缀和 @本人限制。
- 消息与任务的去重关联、扫描进度及任务创建一起提交；补收不会重复创建任务。新增规则只处理启用后的消息，不追溯旧记录。
- 暂停/删除取消未开始的事件任务，执行中的任务继续；打断用 `/ai /stop`。执行中重启、调用失败或超时的任务记为 unknown，不自动重试，避免重复副作用；需要查审计人工确认。普通定时任务恢复行为不变。
- 第一版无独立配置页面，无自动语义过滤。条件命中后由预设指令交给现有 coding agent 执行，保留其完整本机权限；不是权限沙箱。

真实 @发送复用现有协议能力：`larkx send CHAT_ID --mention USER_ID:显示名 --json -`，正文从 stdin 读取。`--mention` 可以重复，也可以和 `-m/--markdown` 一起用；正文里写了 `@显示名`（与某个 `--mention` 的显示名一致）的地方就地变成真实 @（代码块里的除外），没写到的放在正文最前面。

## 本人在线：收件箱和 larkx watch

- 收件箱 `~/.larkx/state/owner_inbox.jsonl` 只由数字人写：它看过消息、判断要你亲自处理时运行 `larkx ping "<要你做什么>"`。`larkx inbox --since 2h` 查看。
- `larkx watch`：实时打印收件箱新增的条目。运行期间你算"在线"：
  - 别人私聊你、@ 你、没权限想唤起的消息，平时数字人不处理（或只回"没有权限"），在线时都交给数字人看一眼；
  - 别人发起的每一轮数字人只判断、要你处理就 ping，不回复、不执行任务（代码层面也拦着：这一轮只能用 `larkx messages/history/ping`）；
  - Ctrl+C 退出后恢复正常；排队中只因在线才交给它的消息不再处理。本人自己发起的指令不受影响。
- 回复进话题：`larkx send <chat_id> --root <msg_id> --thread`。在普通群里，这会回进以那条消息为根的话题（还没有话题就新开一个），而不是在主会话里引用回复。

## 插件：记忆策略和交互协议

默认（`default` / `default`）就是上游 lark-twin 原样的行为。另外两类能力做成了插件，一个开关切换：

- **记忆策略**（`larkx/plugins/memory/`）：数字人怎么记、怎么接上前情。
- **交互协议**（`larkx/plugins/comms/`）：哪些消息交给数字人、回复要遵守什么格式。

`--memory` 会把交互协议一起切到这个记忆策略配套的那个，`--comms` 可以单独覆盖：

```bash
larkx plugin list                       # 有哪些插件、现在用的哪个
larkx plugin use --memory work          # 长期切换(写进 .env): 记忆策略 work + 交互协议 work
larkx plugin use --memory work --comms default   # 只要工作记忆, 不要旁听
larkx plugin use --memory default       # 回到上游原样
larkx --memory work daemon              # 只这一次临时指定
```

切换后重启 daemon。人设 `CLAUDE.md` 和数字人的 skill 里，插件相关的说明放在一个单独的区块里，切换时自动加上或去掉，区块外你自己的改动不受影响。插件的表在库里一直都建，切回默认也不会丢数据。

以下能力不属于插件，任何组合下都有：富文本发送、云文档评论监听和回复（`larkx doc`）、`send --file` / `--thread`、`larkx watch` / `inbox` / `ping`。

想加新的记忆策略或交互协议，见 [docs/plugins.md](docs/plugins.md)。

### 交互协议 work：旁听、开发模式、点名回复

回复第一行先点名收件人（「张三：」，纯文字不真 @）；唤起白名单里的人和你的私聊自动旁听。想让数字人在某个群里不用 @、不用 `/ai` 也能按需回复（比如联调群），或者让群里别人也能叫它查代码、改代码，就给这个群单独做配置：

```bash
larkx group set <chat_id或群名> --listen on \
  --workdir ~/code/xxx --instruction '按联调互报协议回复，一事一话题'   # 旁听 + 专属指令 + 代码目录
larkx group set <chat_id> --dev on          # 开发模式：别人发起的一轮也能查代码、跑命令、改代码
larkx group set <chat_id> --bots on         # 旁听时机器人发的也处理（默认不处理）
larkx group set <chat_id> --max-turns 10    # 旁听每 10 分钟最多交给数字人几条（默认 20，0 不限）
larkx group set <chat_id> --allow 张三 --allow 李四   # 会话白名单：只旁听这几个人（--disallow 移出，--allow-all 清空）
larkx group set 张三 --listen on                      # 私聊也一样，写对方名字或私聊的 chat_id
larkx group set p2p --listen on --allow 张三 --allow 李四  # 所有私聊：这几个人私聊你时自动处理
larkx group list | show <chat_id> | remove <chat_id>
```

- **私聊**：单个私聊和群一样配置。`p2p`（也可以写“私聊”）是所有私聊共用的一份配置，某个私聊有自己的配置时，以它自己的为准。所有私聊开旁听时，建议配合白名单使用。
- **会话白名单**：名单非空时，只旁听名单里的人，开发模式也只对他们生效；其他人按默认规则处理（要 @、要前缀，按访客对待）。名字先在本地消息里找，找不到再在线搜；同名时要求改用 user_id。

- **旁听**：别人在这个群里发的每条消息都会交给数字人，由它自己判断要不要回，不需要回就不发。以下几种不交给它：本人自己发的（包括数字人以本人身份发的，所以不会自己触发自己）、系统消息、机器人发的（`--bots on` 可以打开）。在旁听的群里，没权限的人发 `/ai` 不会收到“没有权限”的自动回复。每个群 10 分钟内超过 `--max-turns` 条就跳过并告警，防止刷屏，也防止和别的机器人互相回复。
- **开发模式**：这个群里别人发起的每一轮（包括旁听到的、`@你 /ai` 的）都不按访客收窄，可以查代码、跑命令、改代码，但仍然只能在本会话里发消息。**只给信得过的群开。**
- 专属指令和代码目录会附在这个群的每一轮提示词里。改完立即生效，不用重启；排队中的消息按处理那一刻的配置来（关了旁听就不再处理）。
- 只有本人能改：数字人调用 `larkx group set/remove` 会被拒绝，并通知你。

### 记忆策略 work：三层工作记忆

数字人每小时开一个新会话，把你**没开免打扰**的会话里新增的消息消化成三层工作记忆（`~/.larkx/agent/memory/work/`），只记工作，不记闲聊和隐私：

- **第一层 `OVERVIEW.md`**：总览（进行中的项目、任务、最近活跃的人，各一行，链接到第二层）+ 近 24 小时的零碎细节 + 待跟进，最多 6000 字。新会话先读它。
- **第二层 `projects/` `tasks/` `people/`**：项目里谁在做什么、在哪个分支、完成没有；任务的负责人、仓库和分支、完成情况；每个人最近在哪些项目的哪个分支做什么。
- **第三层 `archive/<年-W周>/`**：按周归档的历史，非必要不读。

新增和更新由数字人做；时间规则由 larkx 用代码执行：
- 短期记忆超过 24 小时删掉；
- 第二层的近况超过 7 天移进归档；
- 完成超过 2 天，或 7 天没动静（默认已经做完）的项目、任务、人，整个移进归档，并修好引用。

免打扰状态取自飞书会话设置，每次消化前现查。消息本来就都存在本地库里，不另存。

codex 后端也不再把所有消息塞进同一个越来越大的会话：每个飞书会话一个 thread，隔 2 小时以上再聊就开新会话，从记忆文件接上前情。

```bash
larkx work-memory                       # 状态：总览字数、各维度个数、消化到哪了、最近 1 小时消化/跳过了几个会话
larkx work-memory feed --since 2h       # 看某段时间会被消化的增量（只含没开免打扰的会话）
larkx work-memory archive --dry-run     # 预演归档规则；去掉 --dry-run 立即执行
```

设计细节见 [larkx/plugins/memory/work/README.md](larkx/plugins/memory/work/README.md)。

## 配套 skill

`skills/` 下有三个：`lark-twin`（在本仓库里部署运维）、`larkx`（装进你的 Claude Code / Codex，用来操作飞书）、`larkx-agent`（数字人自己用的 CLI 手册）。说明见 [skills/README.md](skills/README.md)。

## 配置

都写在 `.env` 里，完整说明见 `.env.example`。

| 配置项 | 说明 |
|---|---|
| `LARKX_AGENT_BACKEND` | `claude` 或 `codex` |
| `LARKX_CLAUDE_CLI` / `LARKX_CODEX_CLI` | 你平时启动 agent 的命令，比如 `claude`、`ccd`、`codex`，认证沿用你自己的配置 |
| `LARKX_MEMORY` / `LARKX_COMMS` | 记忆策略 / 交互协议，默认 `default`（上游原样），可选 `work`；`LARKX_COMMS` 不写时跟着记忆策略走。也可以用 `larkx plugin use` 改 |
| `LARKX_AGENT_SESSION` / `LARKX_AGENT_SESSION_IDLE` | codex 后端的会话策略：`single`（所有消息一个 thread）或 `chat`（每个会话一个 thread，空闲超过这么多秒开新的，默认 7200）。不写时由记忆策略决定：`default` 用 `single`，`work` 用 `chat` |
| `LARKX_MEMORY_DIGEST_INTERVAL` | （记忆策略 work）工作记忆每隔多少秒消化一次增量，默认 3600，0 关闭 |
| `LARKX_MEMORY_SESSIONS` | （记忆策略 work）消化时也读本人自己的 Codex / Claude Code 会话里新增的部分（只取输入和最终输出），默认开，`false` 关闭 |
| `LARKX_AGENT_FALLBACK_MODEL` | 用 Claude 时，主模型被安全策略拦截后改用的备用模型 |
| `LARKX_TRIGGER_PREFIX` | 触发前缀，默认是 `/ai` |
| `LARKX_REPLY_SIGNATURE` | 每条回复末尾的署名，比如 `🤖 lark-twin · 数字分身`，会和正文空一行、前面加"——" |
| `LARKX_DENY_REPLY` / `LARKX_DENY_REPLY_COOLDOWN` | 没权限的人发 `/ai` 时自动回的一句话（`{owner}` 换成你的名字，设成空关掉）· 同一会话同一人多少秒内只回一次，默认 600 |
| `LARKX_CHAT_GROUPS` | 会话分类与关注度，默认关。打开后每天给会话画像，每日回顾只看重点和简单关注的会话 |
| `LARKX_RETENTION_ARCHIVE_DAYS` / `LARKX_RETENTION_LIGHT_DAYS` | 只存不看、简单关注的会话消息保留天数，默认 0 一直保留；要打开会话分类才生效，没画像的会话不删 |
| `LARKX_ORGANIZE` / `LARKX_ORGANIZE_ITEMS` | 整理方案：分身提出整理会话的调整，本人在私聊里同意后执行、可撤销，默认关（要先打开会话分类）。`LARKX_ORGANIZE_ITEMS` 选提哪几类：分类、置顶、免打扰（默认）、标签 |
| `LARKX_BACKFILL_DAYS` | 首次启动回填最近多少天的聊天，回填完分批初始化记忆，默认 0 不回填 |
| `LARKX_BRIEFING_AT` | 每天几点私信本人工作简报（如 `09:00`，周一附上周回顾），默认关 |
| `LARKX_MEMORY_STRUCTURED` | 结构化记忆：记忆文件开头写字段，`MEMORY.md` 由代码生成，默认关 |
| `LARKX_ALERT_CMD` | 飞书登录失效时的备用告警命令，比如用 lark-cli 机器人身份发给你 |
| `LARKX_WEB_URL` | 网页控制台的对外地址（Nginx 入口），用来生成登录链接 |

数字人的人设在 `~/.larkx/agent/CLAUDE.md`，记忆在 `~/.larkx/agent/memory/`，都可以直接修改。数字人自己克隆的代码仓库放在 `~/.larkx/agent/repos/`，本机已有的仓库它会先查 `memory/repos.md` 直接用，不重复克隆。

## 注意

- 用的是非官方协议，飞书一升级就可能失效，也有被风控的可能。如果是公司账号，先确认公司规定是否允许。
- `~/.larkx/credentials.json` 就是你账号的完整登录态，务必妥善保管。控制台的令牌 `~/.larkx/web_token` 能看到你的聊天和记忆，同样不要外传。
- 聊天记录会存在本地：数字人收到的所有消息（不只是 `/ai`）都存进 `~/.larkx/messages.db`；日志 `~/.larkx/logs/`、记忆 `~/.larkx/agent/memory/` 和 agent 会话记录（`~/.larkx/claude/`、`~/.larkx/codex/`）里也有聊天内容。
- **在企业内使用时，不能把服务部署在企业外的服务器上**（如个人云主机、境外 VPS），否则上面这些聊天记录和登录态都会落到企业外部，有数据泄露风险。同理，数字人处理消息时会把相关的聊天、文件和代码发给驱动它的模型，模型调用也要走企业批准的通道，不要经过第三方中转。
- 边界由 CLI 在代码里拦截，但数字人拥有完整的 shell 权限，所以这些限制只能防住误操作，不能当作安全隔离。

## 开发

```bash
.venv/bin/python -m pytest -q tests
```

飞书协议更新后，重新提取 schema 的方法见 `dev/proto_pipeline/README.md`。
