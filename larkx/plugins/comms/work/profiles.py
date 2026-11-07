"""会话配置的读写。和唤起白名单一样: daemon 每条消息现查, 改了立即生效; 只有本人能改, 数字人的会话里调用会被拒绝。"""
import json
import time
from datetime import datetime

from ....config import inside_twin
from ....storage.models import Message
from .models import ChatConfig

FIELDS = ('chat_name', 'listen', 'dev', 'bots', 'instruction', 'workdir', 'max_turns', 'senders')
DENIED = '数字人不能修改会话配置(旁听/开发模式), 只有本人能改'


def _guard():
    if inside_twin():
        raise PermissionError(DENIED)


def get(storage, chat_id: str):
    with storage.Session() as s:
        return s.get(ChatConfig, str(chat_id))


def for_chat(storage, chat_id: str, chat_type=None):
    """这条消息用哪份配置: 会话自己的; 私聊没有自己的配置时用"所有私聊"(chat_id='p2p')那份。"""
    row = get(storage, chat_id)
    if row is None and chat_type == 1:
        row = get(storage, 'p2p')
    return row


def list_all(storage):
    with storage.Session() as s:
        return s.query(ChatConfig).order_by(ChatConfig.updated_at).all()


def save(storage, chat_id: str, **fields):
    """新建或修改; 只改传进来(不是 None)的字段。返回改完的那一行。"""
    _guard()
    chat_id = str(chat_id or '').strip()
    if not chat_id:
        raise ValueError('chat_id 不能为空')
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise ValueError(f'未知字段: {sorted(unknown)}')
    with storage.Session() as s:
        row = s.get(ChatConfig, chat_id)
        if row is None:
            row = ChatConfig(chat_id=chat_id, listen=0, dev=0, bots=0, instruction='', workdir='', max_turns=20,
                              senders='{}', listen_since=0)
            s.add(row)
        if fields.get('listen') and not row.listen:
            # 关 → 开: 从现在起才旁听, 不追溯打开前的聊天
            row.listen_since = int(time.time())
        for key, value in fields.items():
            if value is not None:
                if key == 'senders' and not isinstance(value, str):
                    value = json.dumps({str(k): v for k, v in dict(value).items()}, ensure_ascii=False)
                setattr(row, key, int(value) if key in ('listen', 'dev', 'bots', 'max_turns') else value)
        row.updated_at = datetime.now()
        s.commit()
        s.refresh(row)
        s.expunge(row)
        return row


def remove(storage, chat_id: str) -> bool:
    _guard()
    with storage.Session() as s:
        n = s.query(ChatConfig).filter_by(chat_id=str(chat_id)).delete()
        s.commit()
        return n > 0


def sender_ids_named(storage, name: str) -> dict:
    """本地消息里叫这个名字的发送人 {user_id: 名字}(只看完全一样的名字)。"""
    with storage.Session() as s:
        rows = s.query(Message.sender_id).filter(Message.sender_name == name, Message.sender_id != '').distinct().all()
        return {r[0]: name for r in rows}
