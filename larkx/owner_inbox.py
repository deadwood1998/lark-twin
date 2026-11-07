"""需要本人亲自处理的事: 一行一条 JSON 追加到 state/owner_inbox.jsonl。

两个来源:
- daemon: 别人的消息 @ 了本人(确定性的, 不靠模型判断);
- 数字人: 判断某件事要本人处理时运行 `larkx ping "<要本人做什么>"`(访客轮也能用, 只写本地文件)。

本人在终端里 `tail -F` 这个文件(或 Claude Code 的 Monitor)就能实时收到, 处理完用 larkx send 回复。
"""
import json
import time

from . import state


def path():
    return state.STATE_DIR / 'owner_inbox.jsonl'


def append(kind: str, text: str, chat_id: str = '', chat_name: str = '', msg_id: str = '', root_id: str = '',
           sender: str = '') -> dict:
    entry = {'ts': int(time.time()), 'time': time.strftime('%m-%d %H:%M'), 'kind': kind, 'chat_id': str(chat_id or ''),
             'chat_name': chat_name or '', 'msg_id': str(msg_id or ''), 'root_id': str(root_id or ''),
             'from': sender or '', 'text': ' '.join(str(text or '').split())[:500]}
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
    return entry


def recent(since: float) -> list:
    try:
        lines = path().read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get('ts', 0) >= since:
            out.append(e)
    return out


# ---- 本人在线: larkx watch 运行期间, 别人发起的轮次只判断和 ping, 不回复、不执行 ----

def watch_path():
    return state.STATE_DIR / 'watch.json'


def start_watch():
    import os
    p = watch_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({'pid': os.getpid(), 'started_at': int(time.time())}), encoding='utf-8')


def stop_watch():
    import os
    try:
        if json.loads(watch_path().read_text(encoding='utf-8')).get('pid') == os.getpid():
            watch_path().unlink(missing_ok=True)
    except (FileNotFoundError, ValueError):
        pass


def watching() -> bool:
    """有 larkx watch 在跑(进程还活着)。watch 被强杀留下的文件按进程是否存活判断, 不会一直生效。"""
    import os
    try:
        pid = int(json.loads(watch_path().read_text(encoding='utf-8')).get('pid') or 0)
        os.kill(pid, 0)
        return pid > 0
    except (FileNotFoundError, ValueError, OSError):
        return False


def format_entry(e: dict) -> str:
    where = e.get('chat_name') or e.get('chat_id') or ''
    who = f" {e['from']}" if e.get('from') else ''
    ids = ' '.join(f'{k}={e[k]}' for k in ('chat_id', 'msg_id', 'root_id') if e.get(k))
    return f"{e.get('time', '')} [{e.get('kind')}] {where}{who}: {e.get('text', '')}  ({ids})"


def follow(since: float = None, interval: float = 0.5):
    """逐条产出收件箱新增的条目(先补 since 之后的旧条目)。文件被删或截短就从头读。"""
    if since is not None:
        yield from recent(since)
    p = path()
    pos = p.stat().st_size if p.exists() else 0
    buf = ''
    while True:
        size = p.stat().st_size if p.exists() else 0
        if size < pos:
            pos, buf = 0, ''
        if size > pos:
            with open(p, 'r', encoding='utf-8') as fh:
                fh.seek(pos)
                buf += fh.read()
                pos = fh.tell()
            *lines, buf = buf.split('\n')
            for line in lines:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
        time.sleep(interval)
