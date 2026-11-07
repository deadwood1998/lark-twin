"""daemon 和 CLI、网页之间共享的状态, 以及时间解析。

daemon 每轮开始前写入 turn.json, 结束后删除。数字人调用 larkx 时, CLI 据此判断
这次发消息、建任务是否越权: 默认只能作用于触发这一轮的会话, 本人发起时才能跨会话。

relay/ 下登记经本人私聊中转的消息(匿名发送, 默认关, 打开 LARKX_KEEP_UNREAD 才有; 见 LarkClient.send_msg 的
keep_unread), daemon 收到它们时跳过。

forward/ 下登记 larkx forward 转发出去的消息(每个目标会话那份的 cid, 见 LarkClient.forward_message), daemon 收到它们时
只入库, 不当成本人下的指令: 发送人是本人, 内容却是转来的, 以触发前缀开头时不能当成本人在叫数字人。
"""
import hashlib
import hmac
import json
import secrets
import os
import re
import time
from datetime import datetime, timedelta

from loguru import logger

from .config import DATA_DIR

STATE_DIR = DATA_DIR / 'state'
TURN_PATH = STATE_DIR / 'turn.json'
RELAY_DIR = STATE_DIR / 'relay'
FORWARD_DIR = STATE_DIR / 'forward'
# 补收漏推消息只看 7 天内有动静的会话, 登记留得比它久, 中转、转发的消息无论什么时候补收到都认得出来
RELAY_TTL = 8 * 86400
# report: daemon 让数字人给本人写的汇报(每日简报、接管总结), 只能发到本人私聊
KINDS = ('message', 'task', 'say', 'system', 'report')


KEY_BYTES = 32
# 读到了这一轮但不可信(签名不对、没签名、密钥坏了), 或者数字人进程里读不到这一轮时用它: 按访客处理, 只能更严不能更松
UNTRUSTED_TURN = {'kind': 'message', 'chat_id': '', 'anchor': '', 'is_owner': False, 'ref': 'untrusted', 'locked': {},
                  'untrusted': True}


def _key_path():
    return STATE_DIR / 'turn.key'


def _new_turn_key() -> bytes:
    """每一轮换一个新密钥(只有本人可读): 存下来的旧 turn.json(比如本人那一轮的)拷回来也验不过, 挡住重放。
    数字人的工具调用不许读它(见 agent/hooks)。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(KEY_BYTES)
    tmp = _key_path().with_suffix('.tmp')
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(key)
    tmp.replace(_key_path())
    return key


def _sign(data: dict, key: bytes) -> str:
    body = json.dumps({k: v for k, v in data.items() if k != 'sig'}, ensure_ascii=False, sort_keys=True)
    return hmac.new(key, body.encode('utf-8'), hashlib.sha256).hexdigest()


def _verify(data: dict) -> bool:
    try:
        key = _key_path().read_bytes()
    except OSError:
        return False
    if len(key) != KEY_BYTES:
        logger.warning(f'turn.key 长度不对({len(key)} 字节), 这一轮按不可信处理; 下一轮 daemon 会重新生成')
        return False
    sig = data.get('sig')
    if not isinstance(sig, str):
        return False
    # 两边都转成 bytes 再比: 非 ASCII 的 str 直接比会抛 TypeError
    return hmac.compare_digest(sig.encode('utf-8', 'replace'), _sign(data, key).encode('ascii'))


def write_turn(kind: str, chat_id: str = '', anchor: str = '', is_owner: bool = False, ref: str = '',
               locked: dict = None, dev: bool = False, watch: bool = False):
    """dev: 交互协议给这个会话开了开发模式, 别人发起的这一轮不按访客收窄; 发消息仍只能发到本会话(send_denied 只看 is_owner)。
    watch: 本人在线(larkx watch)时别人发起的这一轮, 只能看当前会话、通知本人。
    locked: daemon 自己的配置里数字人不能用环境变量改的几项(见 config.LOCKED_KEYS), 数字人进程里的命令以这里为准。
    整个内容带签名(sig), read_turn 校验不过就当没有这一轮。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {'kind': kind, 'chat_id': str(chat_id or ''), 'anchor': anchor or '', 'is_owner': bool(is_owner),
            'dev': bool(dev), 'watch': bool(watch), 'ref': str(ref or ''), 'pid': os.getpid(), 'started_at': time.time(),
            'locked': locked or {}}
    data['sig'] = _sign(data, _new_turn_key())
    tmp = TURN_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    tmp.replace(TURN_PATH)


def clear_turn():
    TURN_PATH.unlink(missing_ok=True)


def read_turn():
    """当前这一轮; 没有正在处理的轮次时返回 None。文件在但不可信(被改过、没签名、密钥坏了)时返回 UNTRUSTED_TURN,
    按访客处理。不会抛异常: 调用方(包括 Claude 的 hook)出错跳过检查就等于放行。"""
    try:
        data = json.loads(TURN_PATH.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except Exception:
        return dict(UNTRUSTED_TURN)
    try:
        if not isinstance(data, dict) or not _verify(data):
            return dict(UNTRUSTED_TURN)
        os.kill(int(data.get('pid') or 0), 0)
    except (OSError, ValueError, TypeError):
        return None  # 写入它的 daemon 已经退出
    except Exception:
        return dict(UNTRUSTED_TURN)
    return data


def agent_turn():
    """数字人进程里用的当前轮次: 读不到时也按访客处理(UNTRUSTED_TURN), 不当成"没有限制"。"""
    return read_turn() or dict(UNTRUSTED_TURN)


def _mark(folder, cid: str, chat_id: str):
    """登记一条消息的 cid: 每条一个文件(内容是它所在的会话 id), 多个进程同时写也不会互相覆盖。顺带清掉过期的。"""
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for p in folder.iterdir():
        try:
            if now - p.stat().st_mtime > RELAY_TTL:
                p.unlink()
        except FileNotFoundError:
            pass
    (folder / cid).write_text(str(chat_id), encoding='utf-8')


def _marked(folder, msg: dict) -> bool:
    cid = str(msg.get('cid') or '')
    if not re.fullmatch(r'[\w-]{1,128}', cid):
        return False
    try:
        return (folder / cid).read_text(encoding='utf-8') == str(msg.get('chat_id'))
    except (FileNotFoundError, NotADirectoryError):
        return False


def mark_relay(cid: str, chat_id: str):
    """发中转消息前登记它的 cid。"""
    _mark(RELAY_DIR, cid, chat_id)


def is_relay(msg: dict) -> bool:
    """这条消息是不是登记过的中转消息(cid 和所在会话都对得上); 转发到目标会话的那份 cid 不同, 不算。"""
    return _marked(RELAY_DIR, msg)


def mark_forward(cid: str, chat_id: str):
    """larkx forward 发出前登记目标会话 chat_id 里那份的 cid。"""
    _mark(FORWARD_DIR, cid, chat_id)


def is_forward(msg: dict) -> bool:
    """这条消息是不是 larkx forward 转发出去的(cid 和所在会话都对得上)。"""
    return _marked(FORWARD_DIR, msg)


def is_guest(turn) -> bool:
    """访客发起的这一轮: 别人的消息(含被接管会话)、别人安排的任务。本人发起、终端指令、维护任务都不算。"""
    return bool(turn) and turn.get('kind') in ('message', 'task') and not turn.get('is_owner') and not turn.get('dev')


def send_denied(target_chat: str, turn) -> str:
    """数字人给 target_chat 发消息是否越权; 越权时返回原因, 允许时返回空串。"""
    if not turn:
        return '当前没有正在处理的消息, 数字人不能主动发消息'
    if turn['kind'] == 'system':
        return '维护任务里不能发飞书消息'
    if turn['kind'] == 'report':
        return '' if str(target_chat) == turn['chat_id'] else '汇报只能发到本人私聊'
    if turn['kind'] == 'say' or turn['is_owner']:
        return ''
    if str(target_chat) == turn['chat_id']:
        return ''
    return f'只能发到触发这一轮的会话 {turn["chat_id"] or "(无)"}; 给其他会话发消息需要本人发起'


def deny_cooling(chat_id: str, sender_id: str, cooldown: int, now: float = None) -> bool:
    """没权限回复的冷却: 同一会话里的同一个人 cooldown 秒内已经回过时返回 True; 否则记下这次, 返回 False。

    记在 state/deny_reply.json(只有 daemon 写), daemon 重启后照样算数; 每次写入时顺带清掉已经过了冷却的记录。
    """
    if cooldown <= 0:
        return False
    path = STATE_DIR / 'deny_reply.json'
    now = time.time() if now is None else now
    try:
        sent = json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        sent = {}
    if not isinstance(sent, dict):
        sent = {}
    key = f'{chat_id}:{sender_id}'
    last = sent.get(key)
    if isinstance(last, (int, float)) and 0 <= now - last < cooldown:
        return True
    sent = {k: v for k, v in sent.items() if isinstance(v, (int, float)) and 0 <= now - v < cooldown}
    sent[key] = now
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(sent), encoding='utf-8')
    tmp.replace(path)
    return False


DURATION_UNITS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}


def parse_duration(text: str) -> int:
    """'90s' '30m' '2h' '1d' '1w', 也可以组合如 '1h30m'。"""
    parts = re.findall(r'(\d+)\s*([smhdw])', (text or '').strip().lower())
    if not parts or ''.join(n + u for n, u in parts) != re.sub(r'\s+', '', text.strip().lower()):
        raise ValueError(f'无法识别的时长 {text!r}, 例: 30m、2h、1d、1h30m')
    return sum(int(n) * DURATION_UNITS[u] for n, u in parts)


def parse_at(text: str, now: datetime = None) -> float:
    """'2026-10-22 09:00'、'10-22 09:00'、'09:00'(今天, 已过则明天)。返回 Unix 时间戳。"""
    now = now or datetime.now()
    s = (text or '').strip()
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            pass
    try:
        dt = datetime.strptime(f'{now.year}-{s}', '%Y-%m-%d %H:%M')
        return dt.timestamp()
    except ValueError:
        pass
    try:
        t = datetime.strptime(s, '%H:%M')
    except ValueError:
        raise ValueError(f'无法识别的时间 {text!r}, 例: 2026-10-22 09:00、10-22 09:00、09:00') from None
    dt = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if dt <= now:
        dt += timedelta(days=1)
    return dt.timestamp()


def next_due(due_at: float, every_sec: int, now: float) -> float:
    """周期任务的下一次时间: 跳过已经错过的周期。"""
    if due_at > now:
        return due_at
    missed = int((now - due_at) // every_sec) + 1
    return due_at + missed * every_sec
