"""工作记忆的增量: 一个窗口里本人没开免打扰的会话的消息, 压成紧凑文本切段交给消化轮。

窗口有两种划法(by): 'id' 按入库顺序(消息自增 id, 补收晚到的旧消息也落在新窗里), 'time' 按发送时间。
免打扰状态存在本策略自己的表 chat_reminds 里(见 models.py), 不改上游的 chats 表。
"""
import re
import time
from datetime import datetime

from sqlalchemy import func

from ....protocol.proto.decoders import is_doc_chat
from ....storage.models import Message
from .models import ChatRemind

SYSTEM_MSG_TYPE = 6
FEED_PART_CHARS = 60000       # 增量按这个字数切段, 一段交给一轮新会话消化
FEED_MSG_CHARS = 400          # 单条消息最多保留的字数


# ---- 查库 ----

def _window(q, lo, hi, by):
    """by='id': (lo, hi] 按入库顺序(消息自增 id); by='time': [lo, hi) 按发送时间。"""
    if by == 'id':
        return q.filter(Message.id > lo, Message.id <= hi)
    return q.filter(Message.create_time >= lo, Message.create_time < hi)


def chats_with_messages(storage, lo: int, hi: int, by: str = 'time') -> list:
    """时间窗(或 id 窗)里有消息的会话 id。"""
    with storage.Session() as s:
        return [r[0] for r in _window(s.query(Message.chat_id), lo, hi, by).distinct().all()]


def max_message_id(storage) -> int:
    with storage.Session() as s:
        return int(s.query(func.max(Message.id)).scalar() or 0)


def last_message_id_before(storage, ts: float) -> int:
    """在 ts 之前入库的最后一条消息的 id(没有就是 0)。"""
    with storage.Session() as s:
        return int(s.query(func.max(Message.id)).filter(Message.stored_at < datetime.fromtimestamp(ts)).scalar() or 0)


def window_messages(storage, lo: int, hi: int, chat_ids, by: str = 'time') -> list:
    """这些会话在窗口里的消息, 按会话、发送时间排好。"""
    ids = [str(c) for c in chat_ids]
    if not ids:
        return []
    with storage.Session() as s:
        q = _window(s.query(Message).filter(Message.chat_id.in_(ids)), lo, hi, by)
        return q.order_by(Message.chat_id, Message.create_time, Message.id).all()


def chat_remind_set(storage, reminds: dict):
    """reminds: {chat_id: True(提醒) / False(免打扰)}。"""
    now = int(time.time())
    with storage.Session() as s:
        for cid, on in reminds.items():
            row = s.get(ChatRemind, str(cid))
            if row is None:
                row = ChatRemind(chat_id=str(cid))
                s.add(row)
            row.remind, row.checked_at = (1 if on else 0), now
        s.commit()


def chat_reminds(storage, chat_ids) -> dict:
    """{chat_id: 1 提醒 / 0 免打扰 / -1 还没查过}。"""
    ids = [str(c) for c in chat_ids]
    if not ids:
        return {}
    with storage.Session() as s:
        got = {r.chat_id: r.remind for r in s.query(ChatRemind).filter(ChatRemind.chat_id.in_(ids)).all()}
    return {c: got.get(c, -1) for c in ids}


def refresh_reminds(storage, client, chat_ids):
    """现查这些会话本人有没有开免打扰(云文档评论没有免打扰, 不查), 存起来。查失败时抛出异常。"""
    ids = [c for c in chat_ids if not is_doc_chat(c)]
    if ids:
        chat_remind_set(storage, client.chat_reminds(ids))


# ---- 增量 ----

def _flat(text: str, limit: int) -> str:
    text = re.sub(r'\s*\n\s*', ' ⏎ ', (text or '').strip())
    return text if len(text) <= limit else text[:limit] + f'…(截断, 原文 {len(text)} 字)'


def _chat_label(m, chat_name: str) -> str:
    if is_doc_chat(m.chat_id):
        return f'云文档评论「{chat_name or m.chat_id}」'
    kind = '私聊' if m.chat_type == 1 else '群' if m.chat_type in (2, 3) else '会话'
    return f'{kind}「{chat_name or m.sender_name or m.chat_id}」' if kind != '私聊' else f'私聊（{chat_name or m.chat_id}）'


def _bot_push(msgs) -> bool:
    """私聊里窗口内全是对方发来的卡片(机器人推送: 报警、资讯、安全通知), 本人没说过话。这类不算工作交流。"""
    return bool(msgs) and all(m.direction != 'out' and m.msg_type_name in ('CARD', 'card') for m in msgs)


def feed_chats(storage, lo: int, hi: int, by: str = 'time') -> dict:
    """窗口里有消息的会话, 按免打扰分好: {'include': [...], 'muted': [...], 'unknown': [...], 'bot': [...]}。
    云文档评论没有免打扰, 算提醒; 免打扰状态未知的不消化; 提醒的私聊里只有机器人卡片推送的也不消化(bot)。
    by 见 Storage._window。"""
    ids = chats_with_messages(storage, lo, hi, by)
    reminds = chat_reminds(storage, [c for c in ids if not is_doc_chat(c)])
    out = {'include': [], 'muted': [], 'unknown': [], 'bot': []}
    for cid in ids:
        r = 1 if is_doc_chat(cid) else reminds.get(cid, -1)
        out['include' if r == 1 else 'muted' if r == 0 else 'unknown'].append(cid)
    if out['include']:
        by_chat = {}
        for m in window_messages(storage, lo, hi, out['include'], by):
            if m.msg_type != SYSTEM_MSG_TYPE:
                by_chat.setdefault(m.chat_id, []).append(m)
        bots = {c for c, ms in by_chat.items() if ms[0].chat_type == 1 and _bot_push(ms)}
        out['bot'] = [c for c in out['include'] if c in bots]
        out['include'] = [c for c in out['include'] if c not in bots]
    return out


def build_feed(storage, lo: int, hi: int, by: str = 'time', part_chars: int = FEED_PART_CHARS,
               msg_chars: int = FEED_MSG_CHARS) -> list:
    """把窗口里没开免打扰的会话的消息压成文本, 按 part_chars 切成若干段; 没有消息返回 []。
    同样的窗口和免打扰状态切出来的段是一样的(失败重试时只重跑没成功的段靠这个)。"""
    chats = feed_chats(storage, lo, hi, by)['include']
    rows = [m for m in window_messages(storage, lo, hi, chats, by) if m.msg_type != SYSTEM_MSG_TYPE]
    if not rows:
        return []
    since, until = min(m.create_time or 0 for m in rows), max(m.create_time or 0 for m in rows) + 1
    names = {}
    for m in rows:
        if m.chat_name:
            names[m.chat_id] = m.chat_name
    for cid in {m.chat_id for m in rows} - set(names):
        names[cid] = storage.get_chat_name(cid) or ''
    multi_day = datetime.fromtimestamp(since).date() != datetime.fromtimestamp(max(until - 1, since)).date()
    blocks, current, lines = [], None, []
    for m in rows:
        if m.chat_id != current:
            if lines:
                blocks.append(lines)
            current = m.chat_id
            lines = [f'## {_chat_label(m, names.get(m.chat_id, ""))} chat_id={m.chat_id}']
        ts = datetime.fromtimestamp(m.create_time or 0).strftime('%m-%d %H:%M' if multi_day else '%H:%M')
        who = m.sender_name or m.sender_id or '?'
        thread = '↳' if m.root_id and m.root_id != m.msg_id else ''
        kind = '' if m.msg_type_name in ('', 'TEXT', 'POST', 'text', 'post') else f'[{m.msg_type_name}]'
        lines.append(f'- {ts} {thread}{who}: {kind}{_flat(m.content, msg_chars)}')
    if lines:
        blocks.append(lines)
    parts, buf, size = [], [], 0

    def flush():
        nonlocal buf, size
        if buf:
            parts.append('\n\n'.join(buf))
        buf, size = [], 0

    for block in blocks:
        head, chunk = block[0], [block[0]]
        if buf and size + len(head) + len(block[1]) + 2 > part_chars:
            flush()   # 这一段放不下这个会话的标题和第一条了, 换下一段
        for line in block[1:]:
            if len(chunk) > 1 and size + sum(len(x) + 1 for x in chunk) + len(line) > part_chars:
                buf.append('\n'.join(chunk))
                flush()
                chunk = [head + '（续）']
            chunk.append(line)
        text = '\n'.join(chunk)
        if buf and size + len(text) > part_chars:
            flush()
        buf.append(text)
        size += len(text) + 2
    flush()
    return parts
