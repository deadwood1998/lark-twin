"""交互协议 work: 按会话配置旁听、开发模式、会话白名单、专属指令(larkx group), 回复第一行点名收件人。

- 旁听: 配置了的会话里别人发的消息不用 @、不用 /ai 也交给数字人, 它自己判断要不要回(宁缺毋滥, 找本人的必须回);
  唤起白名单里的人和本人的私聊默认就是旁听。每个会话 10 分钟内最多交给数字人 max_turns 条。
- 开发模式: 别人发起的一轮不按访客收窄, 可以查代码、跑命令、改代码, 仍只能在本会话发消息。
- 回复格式: 第一行写「名字：」点明对象(纯文字, 不真的 @)。
说明见同目录的 README.md。
"""
import asyncio
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from loguru import logger

from ....protocol.proto.decoders import is_doc_chat
from ..base import CommsProtocol
from . import profiles
from .notes import (ADDRESS_NOTE, DEV_GUEST_NOTE, LISTEN_MENTION_NOTE, LISTEN_REPLY_NOTE, profile_note,
                    sender_allowed)

HERE = Path(__file__).parent
LISTEN_WINDOW = 600        # 旁听限流的时间窗(秒), 每个会话在窗口内最多交给数字人 max_turns 条
LISTEN_MAX_AGE = 300       # 旁听只处理这么久(秒)以内的消息: daemon 停过一阵再启动时, 补收到的旧聊天只入库, 不去回
FROM_TYPE_BOT = 2
# 多人的会话: 回复要点名收件人(本人在私聊里直接给数字人下指令时不用)
MULTI_CHAT_TYPES = ('group', 'topic_group', 'doc')


class WorkComms(CommsProtocol):
    name = 'work'
    description = '会话配置(larkx group): 旁听 / 开发模式 / 会话白名单 / 专属指令, 白名单里的人私聊自动旁听; 回复点名收件人'
    models_module = 'larkx.plugins.comms.work.models'

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._hits = {}   # 旁听限流: {chat_id: deque[交给数字人的时间]}

    @classmethod
    def register_cli(cls, sub):
        from .cli import register
        register(sub)

    def persona(self) -> str:
        return (HERE / 'persona.md').read_text(encoding='utf-8')

    def skill(self) -> str:
        return (HERE / 'skill.md').read_text(encoding='utf-8')

    # ---- 会话配置 ----

    def profile_for(self, storage, chat_id, chat_type, sender_id):
        """这条消息用哪份会话配置。会话自己的配置(和"所有私聊")优先; 都没有时, 唤起白名单里的人和本人的私聊
        默认就是旁听: 不用 /ai, 只处理加进白名单之后的消息。群不会因为白名单自动旁听, 群要整个群单独配置。"""
        profile = profiles.for_chat(storage, chat_id, chat_type)
        if profile is not None or chat_type != 1 or not sender_id:
            return profile
        entry = storage.whitelist_get(str(sender_id))
        if entry is None:
            return None
        since = int(entry.added_at.timestamp()) if entry.added_at else 0
        return SimpleNamespace(chat_id=str(chat_id), chat_name='', listen=1, dev=0, bots=0, instruction='', workdir='',
                               max_turns=20, senders='{}', listen_since=since)

    def listen_profile(self, daemon, msg: dict):
        if is_doc_chat(msg.get('chat_id')):
            return None
        profile = self.profile_for(daemon.storage, str(msg['chat_id']), msg.get('chat_type'), msg.get('from_id'))
        # 会话白名单非空时只旁听名单里的人; 其他人照默认规则(要 @、要前缀、可能回"没有权限")
        if profile and profile.listen and sender_allowed(profile, msg.get('from_id')):
            return profile
        return None

    async def listen_ok(self, daemon, msg: dict, profile, chat_name: str = '') -> bool:
        """旁听到的这条要不要交给数字人: 跳过系统消息、(默认)机器人、已经排过队的; 每个会话 10 分钟内最多 max_turns 条。"""
        chat_id = str(msg['chat_id'])
        content = (msg.get('content') or '').strip()
        if not content or content.startswith('[系统消息]'):
            return False
        created = float(msg.get('create_time') or 0)
        if created and (created < float(getattr(profile, 'listen_since', 0) or 0) or time.time() - created > LISTEN_MAX_AGE):
            return False
        if msg.get('msg_id') and daemon.storage.inbox_seen(msg['msg_id']):
            return False
        if not profile.bots:
            if msg.get('from_type') == FROM_TYPE_BOT:
                return False
            try:
                if await asyncio.to_thread(daemon.client.is_bot, str(msg.get('from_id'))):
                    return False
            except Exception as e:
                logger.debug(f'旁听: 查不到 {msg.get("from_id")} 是不是机器人: {e!r}')
        hits = self._hits.setdefault(chat_id, deque())
        now = time.time()
        while hits and now - hits[0] > LISTEN_WINDOW:
            hits.popleft()
        limit = int(profile.max_turns or 0)
        if limit > 0 and len(hits) >= limit:
            logger.warning(f'旁听「{chat_name or chat_id}」{LISTEN_WINDOW // 60} 分钟内已到 {limit} 条上限, 跳过 {msg.get("msg_id")}')
            daemon.notifier.alert(f'旁听「{chat_name or chat_id}」{LISTEN_WINDOW // 60} 分钟内已经交给数字人 {limit} 条, '
                                  f'这个会话之后的消息先跳过(防止刷屏或和别的机器人互相回复), 其他会话不受影响。'
                                  f'调整上限: larkx group set {profile.chat_id} --max-turns N',
                                  key=f'listen-cap:{chat_id}', cooldown=LISTEN_WINDOW)
            return False
        hits.append(now)
        return True

    def message_context(self, daemon, ev) -> dict:
        """会话配置按处理这一刻的算: 排队期间本人关了旁听就不再处理, 关了开发模式就按访客处理。"""
        from ....daemon.triggers import mentions_me
        msg = ev.msg or {}
        sender = msg.get('from_id')
        profile = None if is_doc_chat(ev.chat_id) else \
            self.profile_for(daemon.storage, ev.chat_id, msg.get('chat_type'), sender)
        if ev.listen and not (profile and profile.listen and sender_allowed(profile, sender)):
            logger.info(f'旁听已关闭或发送人已不在会话白名单里, 跳过排队中的 {ev.ref}')
            return {'skip': True}
        dev = bool(profile and profile.dev and sender_allowed(profile, sender)) and not ev.is_owner
        note = profile_note(profile, listen=ev.listen)
        if ev.listen and msg and mentions_me(msg, daemon.auth.user_id):
            note = '; '.join(n for n in (note, LISTEN_MENTION_NOTE) if n)
        elif ev.listen and msg and daemon.storage.replies_to_owner(msg, daemon.auth.user_id):
            note = '; '.join(n for n in (note, LISTEN_REPLY_NOTE) if n)
        return {'skip': False, 'dev': dev, 'note': note, 'watch_note': profile_note(profile)}

    def prompt_rules(self, type_name: str, is_owner: bool, dev: bool) -> tuple:
        # 本人在私聊里直接给数字人下指令时, 回的就是本人, 不用写名字
        extra = ADDRESS_NOTE if (not is_owner or type_name in MULTI_CHAT_TYPES) else ''
        guest = DEV_GUEST_NOTE if (dev and type_name != 'doc') else None
        return extra, guest
