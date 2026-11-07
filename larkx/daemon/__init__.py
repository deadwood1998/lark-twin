"""larkx daemon: 常驻收消息, 把触发的消息、到点的任务排队交给一直开着的数字人会话。"""
import asyncio
import json
import signal
import time
from datetime import datetime, timedelta

from loguru import logger

from .. import doc_comments, notify, owner_inbox, plugins, state
from .. import whitelist as wl
from ..agent import SessionCrashed, create_session, ensure_agent_home
from ..agent.home import commit_agent_home
from ..config import CREDENTIALS_PATH, DATA_DIR, load_config, locked_values, with_signature
from ..notify import Notifier
from ..protocol import AuthExpired, LarkAuth, LarkClient
from ..protocol.proto.decoders import is_doc_chat
from ..storage import ACK_REACTION, Storage
from ..event_rules import RuleStore
from .listener import Listener
from .events import Event
from .maintenance import Maintenance
from .prompts import TAKEOVER_SUMMARY, flush_prompt
from .triggers import (FROM_TYPE_BOT, WATCH_NOTE, WATCH_OWNER_NOTE, build_prompt, build_report_prompt, build_say_prompt,
                       build_system_prompt, build_task_prompt, denied_attempt, mentions_me, parse_command, parse_takeover,
                       should_handle, strip_mentions, strip_prefix, takeover_note)

LOG_PATH = DATA_DIR / 'logs' / 'daemon.log'
DAEMON_STATE = state.STATE_DIR / 'daemon.json'
SCHEDULER_INTERVAL = 15
RETRY_NOTE = '上一次处理这条消息时 agent 进程中断过。先用 larkx messages 查一下是否已经回复过, 避免重复回复'
RESTART_NOTE = 'daemon 重启前收到了这条消息但没处理完。先用 larkx messages 查一下是否已经回复过, 避免重复回复'
MSG_FIELDS = ('msg_id', 'chat_id', 'chat_type', 'from_id', 'anchor', 'at_me', 'create_time', 'content')
CATCHUP_DELAY = 5
CATCHUP_DAYS = 7           # 补收核对本地多少天内有动静的会话
CATCHUP_BATCH = 50         # 每次按 position 拉多少条
CATCHUP_ROUND_MAX = 2000   # 每轮补收最多拉多少个 position, 没拉完的下一轮接着拉
WS_MISS_COOLDOWN = 86400   # 同一个会话的漏推告警多久报一次
RECONNECT_CATCHUP_DELAY = 5  # 长连接重新连上后等几秒再补收, 让连接先稳定
WS_GAP_MARGIN = 60         # 断线窗口前后各放宽多少秒: 断开前刚发、重连后马上发的消息也可能没推到
WS_GAP_KEEP = 86400        # 断线窗口记多久: 这期间补收到的、发送时间落在窗口里的消息都不算长连接漏推
SYSTEM_MSG_TYPE = 6        # Message.Type.SYSTEM: 入群、改群名、"大群已关闭通知"(type=1500)这类系统提示
RECONNECT_ALERT = (3, 600)  # 10 分钟内断线 3 次就告警

class Daemon:
    def __init__(self, auth):
        self.cfg = load_config()
        self.auth = auth
        self.client = LarkClient(auth)
        self.storage = Storage()
        self.rule_store = RuleStore(self.storage)
        self.session = create_session(self.cfg, self.storage)
        self.notifier = Notifier(self.cfg, self.client)
        self.maintenance = Maintenance(self)
        self.prefix = (self.cfg.get('triggers') or {}).get('prefix') or ''
        self.inbox = asyncio.Queue()
        self.home = None
        self.loop = None
        self.listener = None
        self._disconnects = []
        self._feed_since = None  # 补收上一轮从 feed 拉到了什么时候, None 表示 daemon 启动后还没拉过
        self._ws_down_at = None  # 长连接这次断开的时间(重连失败不改), 重新连上后清掉
        self._ws_gaps = []       # 最近的断线窗口 [(断开时间, 重新连上的时间)]
        self._catchup_wake = asyncio.Event()  # 长连接重新连上时叫醒补收循环, 马上补收一轮
        self.turn_lock = asyncio.Lock()  # 数字人的一轮处理和插件的后台维护(如记忆归档)不同时改文件
        # 插件: 记忆策略(larkx/plugins/memory/)和交互协议(larkx/plugins/comms/), 默认是上游原样
        self.memory, self.comms = plugins.load(self.cfg)
        self._ack_tasks = set()  # 后台正在加的表情回复(留着引用, 免得任务被回收)
        self._owner_stopped = False  # 本人 /stop 打断了当前这一轮

    def __getattr__(self, name):
        # 没走 __init__ 构造的(测试里常见)也能用: 插件按 cfg 现取(默认就是上游原样), 锁现建
        if name in ('memory', 'comms'):
            # 只补缺的那个: 已经设好的(比如测试里换成了别的协议)不能被覆盖
            loaded = dict(zip(('memory', 'comms'), plugins.load(self.__dict__.get('cfg') or {})))
            setattr(self, name, loaded[name])
            return self.__dict__[name]
        if name == 'turn_lock':
            self.turn_lock = asyncio.Lock()
            return self.turn_lock
        raise AttributeError(name)

    def _banner(self):
        t = self.cfg.get('triggers') or {}
        print('─' * 56, flush=True)
        print(f'larkx daemon  region={self.auth.region.name}  user={self.auth.user_id}  agent={self.cfg["agent_backend"]}', flush=True)
        print(f'  插件: {plugins.describe(self.cfg)}', flush=True)
        print(f'  数字人目录: {self.home}', flush=True)
        print(f'  触发前缀: {t.get("prefix") or "(全部消息)"}  允许自己触发: {self.cfg.get("allow_self")}', flush=True)
        print(f'  群里别人需 @我: {bool(t.get("group_at_only"))}  只有本人能唤起: {bool(t.get("owner_only"))}', flush=True)
        print(f'  唤起白名单: {len(self.storage.whitelist_ids())} 人(非空时只有本人和白名单里的人能唤起, 改了立即生效)', flush=True)
        deny = self._deny_text()
        print(f'  没权限的人唤起时回复: {deny or "(不回复)"}'
              + (f'  同一会话同一人 {self.cfg["deny_reply_cooldown"]}s 内只回一次' if deny else ''), flush=True)
        ack = self.storage.ack_reaction()
        scope = '只对本人自己发的 /ai' if ack['scope'] == 'owner' else '所有触发数字人的消息'
        print(f'  触发时先回 {ACK_REACTION} 表情: {f"打开, {scope}" if ack["enabled"] else "关"}(网页"设置"页改, 立即生效)', flush=True)
        print('─' * 56, flush=True)

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.home = ensure_agent_home(self.cfg)
        self._banner()
        self.rule_store.recover()
        self.maintenance.recover(self._legacy_state())
        n = self.storage.reset_running_tasks()
        if n:
            logger.info(f'{n} 个上次没跑完的任务已重新排队')
        await self.session.start()
        self._restore_inbox()
        self.listener = Listener(self.client, self.storage, self.cfg, self.auth.user_id, on_stored=self._from_listener,
                                 on_disconnect=self._on_disconnect, on_connect=self._on_connect)
        workers = [asyncio.create_task(self._consume()), asyncio.create_task(self._schedule()),
                   asyncio.create_task(self._catch_up_loop()), asyncio.create_task(self._control_loop())]
        try:
            await self.listener.run_forever()
        finally:
            self.maintenance.stop()
            for w in workers:
                w.cancel()
            state.clear_turn()
            await self.session.close()

    # ---- 飞书消息 ----

    async def _from_listener(self, msg, chat_name, sender_name):
        # 监听回调跑在协议层的事件循环线程里, 切回 daemon 主循环处理, 队列和会话只在主循环里使用
        fut = asyncio.run_coroutine_threadsafe(self.on_message(msg, chat_name, sender_name), self.loop)
        await asyncio.wrap_future(fut)

    async def on_message(self, msg: dict, chat_name: str = '', sender_name: str = ''):
        is_owner = str(msg.get('from_id')) == str(self.auth.user_id)
        chat_id = str(msg['chat_id'])
        if is_doc_chat(chat_id) and not msg.get('anchor'):
            # 拿不到评论 ID 的文档通知(正文里的 @、一次推来几条里较早的那些)回复不了, 只入库
            return
        tk = None if is_owner else self.storage.takeover_get(chat_id, time.time())
        # 白名单每条都现查: 命令行、网页改了立即生效, 不用重启
        whitelist = () if is_owner else self.storage.whitelist_ids()
        handle = bool(tk) or should_handle(msg, self.cfg, self.auth.user_id, whitelist=whitelist)
        denied = not handle and not is_owner and denied_attempt(msg, self.cfg, self.auth.user_id, whitelist)
        # 旁听(交互协议决定, 上游没有): 别人在这个会话发的消息不用 @、不用前缀也交给数字人, 它自己判断要不要回;
        # 旁听的会话里不回"没有权限"。本人自己的消息(包括数字人以本人身份发的)不旁听, 不会自己触发自己
        profile = None if is_owner or tk or handle else self.comms.listen_profile(self, msg)
        listen = profile is not None
        if listen:
            denied = False
        age = time.time() - float(msg.get('create_time') or time.time())
        if age > self.cfg['inbox_max_age']:
            # 太久远的消息(多是补收到的)只入库, 不做任何对外动作: 不交给数字人, 不回"没有权限", 不接管, 也不执行本人的指令
            if handle or denied or listen:
                logger.info(f'跳过 {age / 60:.0f} 分钟前的消息 {msg.get("msg_id")}(超过 LARKX_INBOX_MAX_AGE)')
            return
        if not is_owner and not handle and owner_inbox.watching() and not is_doc_chat(chat_id) and \
                msg.get('from_type') != FROM_TYPE_BOT and msg.get('msg_type') != SYSTEM_MSG_TYPE and \
                (denied or msg.get('chat_type') == 1 or mentions_me(msg, self.auth.user_id) or
                 self.storage.replies_to_owner(msg, self.auth.user_id)):
            # 本人在线(larkx watch): 别人私聊本人、@ 本人、回本人消息的、没权限想唤起的, 平时数字人不处理或只回"没有权限";
            # 在线时都交给数字人看一眼, 由它判断要不要写进本人收件箱(larkx ping), 不回复。旁听的会话里也走这里,
            # 不受旁听的条数上限和机器人过滤影响, 找本人的不会漏
            await self._enqueue_message(msg, (msg.get('content') or '').strip(), chat_name, sender_name, False, judge=True)
            return
        if denied:
            # 没权限的人想唤起: 马上回一句"没有权限", 不交给数字人
            await self._deny(msg, chat_name, sender_name)
            return
        if listen:
            if await self.comms.listen_ok(self, msg, profile, chat_name):
                await self._enqueue_message(msg, (msg.get('content') or '').strip(), chat_name, sender_name, False,
                                            listen=True)
            return
        if not handle:
            return
        if tk:
            # 接管中的会话: 对方说的每句话都交给数字人, 不看触发规则
            return await self._enqueue_message(msg, (msg.get('content') or '').strip(), chat_name, sender_name, False,
                                               note=takeover_note(tk.goal, tk.until))
        body = strip_prefix(msg.get('content'), self.prefix)
        # 主会话是所有会话共用的, 清空、打断、接管只认本人
        cmd, rest = parse_command(body) if is_owner else (None, body)
        root_id = msg.get('anchor') or None
        if cmd == 'stop':
            n = self._drain_inbox()
            stopped = await self._owner_interrupt()
            if not rest:
                parts = ['⏹ 已停止当前任务' if stopped else '⏹ 当前没有在处理的任务']
                if n:
                    parts.append(f'清空 {n} 条排队消息')
                await self._reply(chat_id, ', '.join(parts), root_id)
                return
        elif cmd == 'clear':
            self._drain_inbox()
            self._mark_owner_stop()
            await self.session.reset()
            if not rest:
                await self._reply(chat_id, '✅ 已开启新会话(长期记忆保留)', root_id)
                return
        elif cmd in ('takeover', 'release') and is_doc_chat(chat_id):
            self.notifier.send(f'云文档「{chat_name or chat_id}」的评论不能接管, 接管只对 IM 会话有效')
            return
        elif cmd == 'takeover':
            if chat_id == self.notifier.owner_chat():
                self.notifier.send('要接管哪个会话, 就在那个会话里发 /ai /takeover [时长] [目标]; '
                                   '或者在这里直接告诉数字人, 比如"接管和张三的私聊两小时, 帮我约下周二开会"')
                return
            seconds, goal = parse_takeover(rest, self.cfg['takeover_default'])
            until = time.time() + seconds
            self.storage.takeover_start(chat_id, until, goal, 'owner')
            when = datetime.fromtimestamp(until).strftime('%H:%M')
            # 确认发到本人私聊, 不在对方看得到的会话里出现
            self.notifier.send(f'✅ 已接管「{chat_name or chat_id}」到 {when}' + (f', 目标: {goal}' if goal else '')
                               + '。对方的消息都由数字人回复, 它发的每条都会抄送给你; 在那个会话发 /ai /release 或运行 '
                               f'larkx takeover stop {chat_id} 结束。')
            return
        elif cmd == 'release':
            if self.storage.takeover_expire(chat_id, time.time()):
                await self._finish_takeovers()
            else:
                self.notifier.send(f'「{chat_name or chat_id}」当前没有在接管')
            return
        if not rest:
            return
        await self._enqueue_message(msg, rest, chat_name, sender_name, is_owner, ack=True)

    def _owner_name(self) -> str:
        """本人在飞书上的名字(LARKX_DENY_REPLY 里的 {owner}); 查到一次就记住。"""
        name = getattr(self, '_owner_display_name', '')
        if not name:
            try:
                name = self.client.get_user_name(str(self.auth.user_id), '0') or ''
            except Exception as e:
                logger.warning(f'查本人的名字失败: {e!r}')
            name = name or self.storage.owner_name(self.auth.user_id)
            self._owner_display_name = name
        return name

    def _deny_text(self) -> str:
        raw = self.cfg.get('deny_reply') or ''
        return wl.deny_text(self.cfg, self._owner_name() if '{owner}' in raw else '')

    async def _deny(self, msg, chat_name='', sender_name=''):
        """没权限的人想唤起数字人: 引用回复一句 LARKX_DENY_REPLY(不加签名, 不经过 agent), 同一会话同一人冷却时间内只回一次。"""
        try:
            chat_id, sender_id, msg_id = str(msg['chat_id']), str(msg.get('from_id')), str(msg.get('msg_id') or '')
            age = time.time() - float(msg.get('create_time') or time.time())
            if age > self.cfg['inbox_max_age'] or not msg_id or not (self.cfg.get('deny_reply') or '').strip():
                return
            text = await asyncio.to_thread(self._deny_text)
            if self.prefix and strip_mentions(text).startswith(self.prefix):
                # 以触发前缀开头的回复会再次触发本人自己的数字人, 不发
                logger.warning(f'LARKX_DENY_REPLY 不能以触发前缀 {self.prefix} 开头, 没有回复')
                return
            who = f'{sender_name or sender_id}({sender_id})'
            where = chat_name or chat_id
            if await asyncio.to_thread(self.client.is_bot, sender_id):
                logger.info(f'[没权限] {who} 是机器人, 不回复')
                return
            if state.deny_cooling(chat_id, sender_id, self.cfg.get('deny_reply_cooldown', 0)):
                logger.info(f'[没权限] {who} 在「{where}」想唤起数字人, 冷却时间内已经回过, 这次不回')
                return
            error = '发送失败'
            if is_doc_chat(chat_id):
                # 云文档里回在他那条评论下面
                sent, error = await self._doc_reply(chat_id, msg.get('anchor'), text)
            else:
                # 引用回复他的那条: 话题群、普通群的话题里回进那个话题(rootId 是话题的根), 别的地方留在主会话
                sent = await asyncio.to_thread(self.client.send_message, {'kind': 'text', 'text': text}, chat_id,
                                               root_id=msg.get('anchor') or msg_id, parent_id=msg_id)
            self.storage.log_action(actor='daemon', action='deny_reply', chat_id=chat_id, msg_id=str(sent or ''),
                                    content=text, ok=1 if sent else 0, error='' if sent else error,
                                    turn_kind='message', turn_ref=msg_id)
            if sent:
                logger.info(f'[没权限] {who} 在「{where}」想唤起数字人, 已回复: {text}')
            else:
                logger.error(f'[没权限] 给 {who} 在「{where}」的回复发送失败: {text}')
        except Exception as e:
            logger.exception(f'没权限回复出错: {e!r}')

    async def _enqueue_message(self, msg, body, chat_name, sender_name, is_owner, note='', ack=False, listen=False,
                               judge=False):
        if not body:
            return
        if msg.get('msg_id') and self.storage.inbox_seen(msg['msg_id']):
            return
        if ack:
            # 网页上打开了"触发时先回一个表情"时先在后台加上, 不等它, 接着排队
            self._ack(msg, is_owner)
        # 先落库再排队: daemon 中途重启时, 没处理完的消息下次启动接着处理
        slim = {k: msg.get(k) for k in MSG_FIELDS}
        inbox_id = self.storage.inbox_add(slim['msg_id'] or '', str(msg['chat_id']),
                                          {'msg': slim, 'body': body, 'chat_name': chat_name, 'sender_name': sender_name,
                                           'is_owner': is_owner, 'note': note, 'listen': listen, 'judge': judge})
        await self.inbox.put(self._message_event(slim, body, chat_name, sender_name, is_owner, inbox_id, note=note,
                                                 listen=listen, judge=judge))

    def _ack(self, msg, is_owner):
        """触发时先回一个表情: 网页"设置"页打开了, 并且在范围里(只对本人自己发的 /ai, 或所有触发数字人的消息), 就以本人身份
        在触发的这条消息上加一个 OK(👌), 表示收到了。设置每条现查, 网页改了下一条就生效。在后台线程里加, 不耽误排队; 失败只打 warning。

        只有正常交给数字人的那一支会调到这里: 接管中的会话、没权限被拒的、超过 LARKX_INBOX_MAX_AGE 的旧消息、纯控制命令
        (/stop、/clear、/takeover、/release 后面没有正文)、定时任务和事件任务都不加。已经在收件箱里的(重复推送、补收又收到)
        在调用前就返回了, 重启后接着处理的(_restore_inbox)也不经过这里, 所以每条只加一次。

        加表情会把那条消息标成已读(对方能看到已读回执, 删掉表情也撤不回), 所以默认关、范围默认只对本人。实测(见
        LarkClient.add_reaction): 加在本人自己的消息上, 会话里别人的消息都还是未读, 本人的已读位置也不动; 加在别人的消息上,
        那一条马上已读, 还可能把本人在这个会话的已读位置推到最新, 之后别人新发的也成了已读(三次里出现过一次)。
        """
        msg_id = str(msg.get('msg_id') or '')
        if not is_owner and owner_inbox.watching():
            # 本人在线: 别人的消息数字人不回, 表情也不加(加了对方会以为在处理, 那条也会被标成已读)
            return
        if not msg_id or is_doc_chat(msg.get('chat_id')):
            # 云文档通知不是 IM 消息, 加不了表情
            return
        try:
            conf = self.storage.ack_reaction()
        except Exception as e:
            logger.warning(f'[表情回复] 读设置失败, 消息 {msg_id} 不加: {e!r}')
            return
        if not conf['enabled'] or (conf['scope'] == 'owner' and not is_owner):
            return
        task = asyncio.create_task(self._add_ack(str(msg['chat_id']), msg_id))
        self._ack_tasks.add(task)
        task.add_done_callback(self._ack_tasks.discard)

    async def _add_ack(self, chat_id, msg_id):
        ok, error = 1, ''
        try:
            await asyncio.to_thread(self.client.add_reaction, msg_id, ACK_REACTION)
            logger.info(f'[表情回复] 在消息 {msg_id} 上加了 {ACK_REACTION}')
        except Exception as e:
            ok, error = 0, str(e)[:300]
            logger.warning(f'[表情回复] 在消息 {msg_id} 上加 {ACK_REACTION} 失败: {e!r}')
        try:
            # 记进动态(网页上显示成"表情回复"); 不是发出的消息, 不算进发出的条数和发送上限, 也不能撤回
            self.storage.log_action(actor='daemon', action='ack_reaction', chat_id=chat_id, msg_id=msg_id, content=ACK_REACTION,
                                    ok=ok, error=error, turn_kind='message', turn_ref=msg_id)
        except Exception as e:
            logger.warning(f'[表情回复] 记录动作失败: {e!r}')

    async def _finish_takeovers(self):
        """收尾已到期或被本人结束的接管: 让数字人在本人私聊发一份总结。"""
        owner_chat = self.notifier.owner_chat()
        for tk, stopped in self.storage.takeover_finish_due(time.time()):
            why = '本人结束' if stopped else '到期'
            name = self.storage.get_chat_name(tk.chat_id) or tk.chat_id
            logger.info(f'接管结束({why}): {name}')
            if not owner_chat:
                continue
            prompt = build_report_prompt('takeover', TAKEOVER_SUMMARY.format(
                name=name, chat=tk.chat_id, why=why, goal=tk.goal or '(无)'), owner_chat)
            await self.inbox.put(Event('report', prompt, chat_id=owner_chat, ref=f'takeover-end:{tk.chat_id}'))

    @staticmethod
    def _message_event(msg, body, chat_name, sender_name, is_owner, inbox_id, note='', listen=False, judge=False):
        return Event('message', '', chat_id=str(msg['chat_id']), anchor=msg.get('anchor') or '', is_owner=is_owner,
                     ref=str(msg.get('msg_id') or ''), msg=msg, body=body, chat_name=chat_name, sender_name=sender_name,
                     inbox_id=inbox_id, note=note, listen=listen, judge=judge)

    def _restore_inbox(self):
        restored = expired = 0
        for row in self.storage.inbox_unfinished():
            try:
                p = json.loads(row.payload)
            except json.JSONDecodeError:
                self.storage.inbox_set(row.id, 'failed')
                continue
            age = time.time() - float(p['msg'].get('create_time') or 0)
            if age > self.cfg['inbox_max_age']:
                self.storage.inbox_set(row.id, 'expired')
                expired += 1
                continue
            note = '; '.join(n for n in (p.get('note'), RESTART_NOTE) if n)
            self.inbox.put_nowait(self._message_event(p['msg'], p['body'], p.get('chat_name', ''), p.get('sender_name', ''),
                                                      p.get('is_owner', False), row.id, note=note,
                                                      listen=p.get('listen', False), judge=p.get('judge', False)))
            restored += 1
        if restored or expired:
            logger.info(f'收件箱: {restored} 条重启前没处理完的消息接着处理, {expired} 条太久远已放弃')

    # ---- 外部控制(网页) ----

    async def _control_loop(self):
        while True:
            try:
                for c in self.storage.control_pending():
                    self.storage.control_done(c.id, await self.control(c.action))
            except Exception as e:
                logger.warning(f'处理控制请求失败: {e!r}')
            await asyncio.sleep(1)

    def _mark_owner_stop(self):
        """开新会话会打断正在跑的这一轮: 和 /stop 一样算本人打断, 例行任务不告警、不重跑。"""
        if getattr(self.session, 'busy', False):
            self._owner_stopped = True

    async def _owner_interrupt(self) -> bool:
        """本人要求停止当前这一轮(/stop、网页"停止")。记下来, 例行任务就不按失败告警和重跑。"""
        stopped = await self.session.interrupt()
        if stopped:
            self._owner_stopped = True
        return stopped

    async def control(self, action: str) -> str:
        if action == 'interrupt':
            n = self._drain_inbox()
            stopped = await self._owner_interrupt()
            return ('已停止当前任务' if stopped else '当前没有在处理的任务') + (f', 清空 {n} 条排队消息' if n else '')
        if action == 'clear':
            self._drain_inbox()
            self._mark_owner_stop()
            await self.session.reset()
            return '已开启新会话(长期记忆保留)'
        if action.startswith('job:'):
            return await self.maintenance.request(action[4:])
        return f'未知的控制动作 {action}'

    # ---- 漏收检查与断线告警 ----

    async def _catch_up_loop(self):
        interval = self.cfg.get('catchup_interval') or 0
        await asyncio.sleep(CATCHUP_DELAY)
        startup = True
        while True:
            try:
                got, left = await self.catch_up()
                n = sum(got.values())
                if got and not startup:
                    logger.warning(f'补收到 {n} 条 WS 没推送的消息: {self._chat_counts(got, limit=0)}')
                    self._alert_ws_miss(got, left)
                elif got:
                    logger.info(f'补收到 {n} 条 daemon 启动前漏收的消息: {self._chat_counts(got, limit=0)}')
                if left:
                    logger.info(f'补收还有 {left} 条没拉完, 下一轮接着拉')
                # daemon 启动前积压的消息拉完之前不告警, 那些不是长连接这会儿漏推的
                startup = startup and left > 0
            except Exception as e:
                logger.warning(f'核对漏收消息失败: {e!r}')
            if not interval:
                return
            await self._wait_catch_up(interval)

    async def _wait_catch_up(self, interval):
        """等下一轮补收: 到点了, 或者长连接断线后重新连上(提前醒, 等几秒让连接稳定再补, 断线期间的消息不用等一整个周期)。
        补收只在 _catch_up_loop 里一轮接一轮地跑, 不会有两轮同时补同一个会话; 正在补收时重新连上, 这一轮完了马上再补一轮。"""
        try:
            await asyncio.wait_for(self._catchup_wake.wait(), interval)
        except asyncio.TimeoutError:
            return
        self._catchup_wake.clear()
        await asyncio.sleep(RECONNECT_CATCHUP_DELAY)

    async def catch_up(self):
        """补收: 核对会话在服务端的最新 position, 把核对位置之后的消息分批拉回来入库(并照常判断是否触发)。

        核对哪些会话: 本地 7 天内有动静的全部会话, 加上消息列表(feed)里上一轮以来有更新的会话(长连接一条都没推过的会话
        本地没有记录, 只能从 feed 发现)。核对位置只由这里推进, 一段消息都拉过、入过库才推进, WS 推来的消息不动它。
        每轮最多拉 CATCHUP_ROUND_MAX 个 position, 缺得少的会话先补, 没拉完的下一轮从核对位置接着拉, 不会跳过。
        返回 ({chat_id: 第一次入库的非系统消息条数}, 留到下一轮的条数)。
        系统提示照样入库、照常判断是否触发, 但不算在返回的条数里, 只打一条 INFO: 有些系统提示服务端本来就不推
        (比如大群的"已关闭通知"提示, type=1500), 补收到它们不说明长连接漏推, 不能拿去告警。
        发送时间落在断线窗口里(长连接断开到重新连上, 前后各放宽 WS_GAP_MARGIN 秒)的消息也一样: 照样入库、触发,
        不算在返回的条数里, 只打一条 INFO"断线期间补收 N 条", 那是断线时本来就收不到的, 不是长连接漏推。
        """
        started, first = time.time(), getattr(self, '_feed_since', None) is None
        feed = []
        try:
            since = started - CATCHUP_DAYS * 86400 if first else self._feed_since
            feed = await asyncio.to_thread(self.client.feed_chats, since)
            self._feed_since = started - 300  # 留点余量, 防止 feed 的更新时间比这边晚一点
        except Exception as e:
            logger.warning(f'从消息列表(feed)拉最近有更新的会话失败, 这一轮只核对本地 7 天内有动静的会话: {e!r}')
        marks = self.storage.catchup_marks(datetime.now() - timedelta(days=CATCHUP_DAYS), extra=feed)
        ids = list(dict.fromkeys([*marks, *feed]))
        gaps = []
        for i in range(0, len(ids), 20):
            resp = await asyncio.to_thread(self.client.api, 'chats.PullChatsByIdsRequest', {'chatIds': ids[i:i + 20]})
            for cid, info in ((resp or {}).get('chats') or {}).items():
                latest = int(info.get('lastMessagePosition') or 0)
                if cid not in marks:
                    # feed 里新发现、还没有核对位置的会话: daemon 启动后第一轮从服务端当前位置核对起, 以前的不回填;
                    # 之后的轮次里它是刚有更新才出现的, 往前多拉 CATCHUP_BATCH 条, 把让它出现的那几条补上
                    marks[cid] = latest if first else max(0, latest - CATCHUP_BATCH)
                    self.storage.catchup_advance(cid, marks[cid])
                if latest > marks[cid]:
                    gaps.append((latest - marks[cid], cid, latest))
        new, system, sys_types, budget, left = {}, {}, {}, CATCHUP_ROUND_MAX, 0
        offline, hit_gaps = {}, set()
        # 缺得少的先补: 零星漏推的消息尽快收到, 积压很多的会话用剩下的额度慢慢补
        for _, cid, latest in sorted(gaps):
            pos = marks[cid]
            try:
                while pos < latest and budget > 0:
                    end = min(latest, pos + CATCHUP_BATCH, pos + budget)
                    for m in await asyncio.to_thread(self.client.pull_history, cid, range(pos + 1, end + 1)):
                        if not (m.get('from_id') and await self.listener.on_message(m)):
                            continue
                        gap = self._ws_gap(m.get('create_time'))
                        if m.get('msg_type') == SYSTEM_MSG_TYPE:
                            system[cid] = system.get(cid, 0) + 1
                            t = (m.get('content_data') or {}).get('type', '?')
                            sys_types[t] = sys_types.get(t, 0) + 1
                        elif gap:
                            offline[cid] = offline.get(cid, 0) + 1
                            hit_gaps.add(gap)
                        else:
                            new[cid] = new.get(cid, 0) + 1
                    # 这一段都拉过、入过库才推进核对位置; 中途出错时下一轮从这里接着拉
                    self.storage.catchup_advance(cid, end)
                    budget -= end - pos
                    pos = end
            except Exception as e:
                logger.warning(f'补收会话 {cid} 出错, 下一轮从 position {pos + 1} 接着拉: {e!r}')
            left += latest - pos
        if system:
            types = '、'.join(f'type={t} {n} 条' for t, n in sorted(sys_types.items(), key=lambda kv: -kv[1]))
            logger.info(f'补收到 {sum(system.values())} 条系统提示({types}), 已入库, 不算长连接漏推: '
                        f'{self._chat_counts(system, limit=0)}')
        if offline:
            hms = lambda t: datetime.fromtimestamp(t).strftime('%H:%M:%S')
            when = '、'.join(f'{hms(down)} 断开 {hms(up)} 连上' for down, up in sorted(hit_gaps))
            logger.info(f'断线期间补收 {sum(offline.values())} 条(长连接 {when}), 已入库, 不算长连接漏推: '
                        f'{self._chat_counts(offline, limit=0)}')
        return new, left

    def _ws_gap(self, create_time):
        """发送时间落在哪个断线窗口里(前后各放宽 WS_GAP_MARGIN 秒), 返回 (断开时间, 重新连上的时间); 不在任何窗口里返回 None。"""
        try:
            t = float(create_time)
        except (TypeError, ValueError):
            return None
        return next(((down, up) for down, up in self._ws_gaps if down - WS_GAP_MARGIN <= t <= up + WS_GAP_MARGIN), None)

    def _chat_counts(self, got: dict, limit: int = 10) -> str:
        """{chat_id: 条数} 写成「会话名」N 条, 多的在前; limit 为 0 时全列出来。"""
        items = sorted(got.items(), key=lambda kv: -kv[1])
        shown = items[:limit] if limit else items
        text = '、'.join(f'「{self.storage.get_chat_name(cid) or cid}」{n} 条' for cid, n in shown)
        return text + (f' 等 {len(items)} 个会话' if len(items) > len(shown) else '')

    def _alert_ws_miss(self, got: dict, left: int):
        """长连接漏推告警: 写明哪些会话各漏了多少条。同一个会话一天内只报一次, 持续漏推的会话不会每轮都报。"""
        fresh = {cid: n for cid, n in got.items() if not notify.cooling(f'ws-miss:{cid}', WS_MISS_COOLDOWN)}
        if not fresh:
            return
        text = (f'长连接漏推了 {sum(fresh.values())} 条消息, 已从服务器补收入库'
                f'(超过 {self.cfg["inbox_max_age"] / 3600:g} 小时的旧消息只入库, 不交给数字人): {self._chat_counts(fresh)}')
        quiet = {cid: n for cid, n in got.items() if cid not in fresh}
        if quiet:
            text += f'。另有 {len(quiet)} 个会话一天内报过, 这轮也补收了 {sum(quiet.values())} 条'
        if left:
            text += f'。还有 {left} 条没拉完, 下一轮接着补'
        self.notifier.alert(text + '。同一个会话一天内只报一次。', cooldown=0)

    def _on_disconnect(self, error):
        now = time.time()
        if self._ws_down_at is None:
            # 重连失败也会走到这里, 断线窗口从第一次断开算起
            self._ws_down_at = now
        times, window = RECONNECT_ALERT
        self._disconnects = [t for t in self._disconnects if now - t < window] + [now]
        if len(self._disconnects) >= times:
            self.notifier.alert(f'飞书长连接 {window // 60} 分钟内断开了 {len(self._disconnects)} 次, 最近一次: {error}',
                                key='ws-unstable', cooldown=3600)

    def _on_connect(self):
        """长连接连上了。是断线后重新连上的话, 记下断线窗口, 叫醒补收循环马上补一轮: 断线期间的消息长连接推不过来,
        不用等到下一个补收周期; 补收到的、发送时间落在窗口里的消息不算长连接漏推, 不告警。"""
        down, self._ws_down_at = self._ws_down_at, None
        if down is None:
            return
        now = time.time()
        self._ws_gaps = [g for g in self._ws_gaps if g[1] > now - WS_GAP_KEEP] + [(down, now)]
        logger.info(f'长连接断开 {now - down:.0f} 秒后重新连上, {RECONNECT_CATCHUP_DELAY} 秒后补收断线期间的消息')
        self._catchup_wake.set()

    def _drain_inbox(self) -> int:
        """清空排队的飞书消息; 定时任务和终端指令放回队列。"""
        kept, n = [], 0
        while not self.inbox.empty():
            ev = self.inbox.get_nowait()
            if ev.kind == 'message':
                n += 1
                if ev.inbox_id:
                    self.storage.inbox_set(ev.inbox_id, 'cancelled')
            else:
                kept.append(ev)
        for ev in kept:
            self.inbox.put_nowait(ev)
        return n

    async def _reply(self, chat_id, text, root_id=None):
        text = with_signature(text, self.cfg)
        if is_doc_chat(chat_id):
            sent, error = await self._doc_reply(chat_id, root_id, text)
            if not sent:
                logger.error(f'控制回执回复评论失败 [{chat_id}]: {text}: {error}')
            return
        msg_id = await asyncio.to_thread(self.client.send_msg, text, chat_id, root_id, self.cfg.get('keep_unread', False))
        if not msg_id:
            logger.error(f'控制回执发送失败 [{chat_id}]: {text}')

    async def _doc_reply(self, chat_id, comment_id, text) -> tuple:
        """daemon 自己在云文档评论下回一句(控制回执、没权限回复)。返回 (回复的 id, 失败原因), 成功时原因为空。"""
        try:
            return await asyncio.to_thread(doc_comments.reply, self.cfg, self.storage, chat_id, comment_id, text), ''
        except doc_comments.DocError as e:
            return '', str(e)

    # ---- 定时任务与每日回顾 ----

    @staticmethod
    def _legacy_state() -> dict:
        """升级前记在 daemon.json 里的每日回顾日期(现在改成例行任务的运行记录)。"""
        try:
            return json.loads(DAEMON_STATE.read_text(encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    async def _schedule(self):
        while True:
            try:
                now = time.time()
                self.rule_store.reconcile(self.auth.user_id)
                for task in self.storage.due_tasks(now):
                    if task.kind == 'event':
                        if not self.rule_store.queue(task.id):
                            continue
                    else:
                        self.storage.update_task(task.id, status='running')
                    if task.kind == 'event':
                        ev = Event('task', task.prompt, chat_id=task.chat_id, is_owner=True,
                                   ref=f'event#{task.id}', task_id=task.id)
                    elif task.kind == 'say':
                        ev = Event('say', build_say_prompt(task.prompt), is_owner=True, ref=f'say#{task.id}', task_id=task.id)
                    else:
                        ev = Event('task', build_task_prompt(task, now), chat_id=task.chat_id, anchor=task.anchor,
                                   is_owner=bool(task.by_owner), ref=f'task#{task.id}', task_id=task.id)
                    await self.inbox.put(ev)
                await self._finish_takeovers()
                await self.maintenance.tick()
                await self.memory.tick(self)
            except Exception as e:
                logger.exception(f'调度异常: {e!r}')
            await asyncio.sleep(SCHEDULER_INTERVAL)

    async def _commit(self, message: str):
        """把数字人目录的变化提交进它自己的 git 仓库(插件收尾时也用)。"""
        await asyncio.to_thread(commit_agent_home, self.home, message)

    def _finish_task(self, ev: Event, result):
        task = self.storage.get_task(ev.task_id)
        if not task:
            return
        fields = {'run_count': (task.run_count or 0) + 1, 'last_run_at': int(time.time()),
                  'result': (result.text if result else '')[:4000]}
        if task.every_sec and task.status != 'cancelled':
            fields.update(status='pending', due_at=int(state.next_due(task.due_at, task.every_sec, time.time())))
        elif task.status != 'cancelled':
            fields['status'] = ('done' if result and not result.is_error else
                                'unknown' if task.kind == 'event' else 'failed')
        self.storage.update_task(task.id, **fields)

    # ---- 交给数字人 ----

    async def _run_turn(self, ev: Event, prompt: str):
        state.write_turn(ev.kind, ev.chat_id, ev.anchor, ev.is_owner, ev.ref, locked=locked_values(self.cfg),
                         dev=ev.dev, watch=getattr(ev, 'watch', False))
        try:
            # 记忆策略决定这一轮接着哪个会话、新会话怎么接上前情(上游: 不带参数)
            return await self.session.ask(prompt, **self.memory.ask_options(ev))
        finally:
            state.clear_turn()

    async def _handle(self, ev: Event):
        event_task = self.storage.get_task(ev.task_id) if ev.task_id else None
        is_event = bool(event_task and event_task.kind == 'event')
        if is_event and not self.rule_store.start(ev.task_id):
            return  # paused/cancelled/duplicate queue item
        note, result = ev.note, None
        self._owner_stopped = False
        extra = watch_extra = ''
        if ev.kind == 'message':
            # 交互协议按处理这一刻的会话配置算: 排队期间关了旁听就不再处理, 关了开发模式就按访客处理(上游: 什么都不改)
            ctx = self.comms.message_context(self, ev)
            if ctx.get('skip'):
                if ev.inbox_id:
                    self.storage.inbox_set(ev.inbox_id, 'done')
                return
            ev.dev = bool(ctx.get('dev'))
            extra, watch_extra = ctx.get('note') or '', ctx.get('watch_note') or ''
        # 本人在线(larkx watch): 别人发起的消息和任务只判断要不要本人处理、写收件箱, 不回复也不执行(代码也拦着)
        ev.watch = ev.kind in ('message', 'task') and not ev.is_owner and owner_inbox.watching()
        if ev.judge and not ev.watch:
            # 排队期间本人退出了 watch: 这条平时本来就不交给数字人, 不处理(也不补回"没有权限")
            logger.info(f'本人已不在线, 跳过排队中只供判断的 {ev.ref}')
            if ev.inbox_id:
                self.storage.inbox_set(ev.inbox_id, 'done')
            return
        if ev.watch:
            ev.dev = False
            owner = self._watch_reason(ev) if ev.kind == 'message' else ''
            extra = '; '.join(n for n in (watch_extra, WATCH_NOTE, WATCH_OWNER_NOTE.format(reason=owner) if owner else '')
                              if n)
        # 事件任务和例行任务里给本人的汇报(简报)崩溃后不当轮重跑: 汇报可能已经发出去了, 由 maintenance 查过再决定要不要兜底。
        # 接管结束的总结没有运行记录和兜底, 照常重试一次
        for attempt in ((1,) if is_event or (ev.run_id and ev.kind == 'report') else (1, 2)):
            prompt = ev.prompt
            if ev.kind == 'message':
                prompt = build_prompt(ev.msg, ev.body, ev.chat_name, ev.sender_name, is_owner=ev.is_owner,
                                      note='; '.join(n for n in (extra, note) if n), dev=ev.dev, comms=self.comms)
            try:
                if ev.run_id:
                    self.maintenance.started(ev.run_id)
                result = await self._run_turn(ev, prompt)
                break
            except SessionCrashed as e:
                logger.error(f'agent 会话中断({attempt}/2): {e}')
                note = RETRY_NOTE
            except Exception as e:
                if ev.run_id:  # 不让运行记录一直停在"运行中"
                    self.maintenance.turn_error(ev, repr(e))
                if not is_event:
                    raise
                logger.exception(f'事件任务 {ev.task_id} 异常，结果未知，不自动重试')
                break
        if is_event:
            self._finish_task(ev, result)
        if ev.inbox_id:
            self.storage.inbox_set(ev.inbox_id, 'done' if result else 'failed')
        if ev.run_id:
            self.maintenance.on_turn_done(ev, result, stopped_by_owner=getattr(self, '_owner_stopped', False))
        self._report(ev, result)
        if result:
            logger.info(f'[agent] {ev.kind} {ev.ref} 结束: {result.subtype} {result.duration_s:.1f}s 工具调用 {len(result.tools)} 次'
                        + (f' | 输出: {result.text[:200]}' if result.text else ''))
        if ev.task_id and not is_event:
            self._finish_task(ev, result)
        await asyncio.to_thread(commit_agent_home, self.home, f'{ev.kind} {ev.ref}'.strip())
        return result

    def _watch_reason(self, ev: Event) -> str:
        """本人在线时, 这条消息为什么算和本人有关(要写收件箱); 无关返回空。"""
        msg, me = ev.msg or {}, self.auth.user_id
        if mentions_me(msg, me):
            return '@ 了本人'
        if msg.get('chat_type') == 1:
            return '私聊本人'
        if self.storage.replies_to_owner(msg, me):
            return '回复了本人的消息, 或在本人发起、回过话的话题里'
        if not ev.listen and not ev.judge:
            return '在叫本人的数字人(白名单里的人用触发前缀, 或接管中的会话)'
        return ''

    def _report(self, ev: Event, result):
        """把这一轮里值得本人知道的异常发给本人。"""
        for note in getattr(self.session, 'events', [])[:]:
            self.notifier.alert(note, key=f'session:{note[:30]}', cooldown=1800)
        if hasattr(self.session, 'events'):
            self.session.events.clear()
        if ev.run_id:
            return  # 例行任务的失败由 maintenance 告警
        if result is None:
            self.notifier.alert(f'处理 {ev.kind} {ev.ref} 时 agent 中断或异常, 这一条结果未知', key='crash', cooldown=900)
        elif result.is_error and result.subtype != 'interrupted' and ev.kind != 'system':
            self.notifier.alert(f'处理 {ev.kind} {ev.ref} 失败({result.subtype}): {result.text[:150]}',
                                key=f'error:{result.text[:30]}', cooldown=1800)

    async def _maybe_flush_memory(self):
        pressure = await self.session.context_pressure()
        if pressure is None or pressure < self.cfg['memory_flush_ratio']:
            return
        logger.info(f'[agent] 上下文已用到压缩阈值的 {pressure:.0%}, 先整理记忆再压缩')
        ev = Event('system', build_system_prompt(flush_prompt(self.cfg)), ref='memory-flush')
        try:
            await self._run_turn(ev, ev.prompt)
            await asyncio.to_thread(commit_agent_home, self.home, 'system memory-flush')
            await self._run_turn(Event('system', '/compact', ref='compact'), '/compact')
        except SessionCrashed as e:
            logger.error(f'整理记忆时 agent 会话中断: {e}')

    async def _process(self, ev: Event):
        result = None
        try:
            if not self.memory.skip_event(ev):
                async with self.turn_lock:
                    result = await self._handle(ev)
                await self._maybe_flush_memory()
        except Exception as e:
            logger.exception(f'处理 {ev.kind} {ev.ref} 异常: {e!r}')
        finally:
            # 出异常也要告诉记忆策略这一轮结束了(如消化轮记上这一段, 不然那一窗永远收不了尾)
            await self.memory.after_event(self, ev, result)

    async def _consume(self):
        while True:
            await self._process(await self.inbox.get())


async def _run_until_signal(coro):
    task = asyncio.ensure_future(coro)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        return await task
    except asyncio.CancelledError:
        logger.info('收到停止信号, 已退出')


def _log_to_file():
    # 自己写日志文件: 有的机器上普通用户读不到自己的 journal
    logger.add(LOG_PATH, level='INFO', rotation='10 MB', retention=5, encoding='utf-8')


def run_daemon(auth):
    _log_to_file()
    return asyncio.run(_run_until_signal(Daemon(auth).run()))


def _credentials_mtime() -> float:
    try:
        return CREDENTIALS_PATH.stat().st_mtime
    except FileNotFoundError:
        return 0


def run_service(wait_interval: int = 60):
    """无人值守运行(systemd): 飞书登录失效时用备用通道告警, 等本人重新登录后自动恢复。"""
    _log_to_file()
    cfg = load_config()
    notifier = Notifier(cfg)
    while True:
        auth = LarkAuth()
        ok, msg = auth.validate(fetch_profile=True)
        if ok and not auth.user_id:
            ok, msg = False, '拿不到当前账号的 user_id'
        if ok:
            auth.save()
            try:
                return asyncio.run(_run_until_signal(Daemon(auth).run()))
            except AuthExpired as e:
                msg = str(e)
        logger.error(f'飞书登录不可用: {msg}')
        notifier.fallback_alert(f'飞书登录已失效, 数字人暂停工作。在服务器上运行 larkx auth qr 重新登录后会自动恢复。({msg[:120]})',
                                key='auth-expired', cooldown=6 * 3600)
        seen = _credentials_mtime()
        while _credentials_mtime() == seen:
            time.sleep(wait_interval)
        logger.info('检测到凭证更新, 重新连接')


def run_listen(auth):
    """只收消息入库, 不接数字人。"""
    cfg = load_config()
    listener = Listener(LarkClient(auth), Storage(), cfg, auth.user_id)
    return asyncio.run(_run_until_signal(listener.run_forever()))
