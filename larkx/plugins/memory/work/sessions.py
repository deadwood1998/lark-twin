"""工作记忆的第二个来源: 本人自己在 Codex / Claude Code 里的会话(不是数字人的)。

只取两样: 本人的输入, 每轮模型的最终输出; 工具调用、工具结果、思考、注入的系统内容都跳过。
会话文件是只增不改的 JSONL, 而且可能很大(几十 MB): 按文件记读到的字节位置, 每次只读新追加的部分;
逐行读, 先用子串判断这一行可能有用再 json 解析, 大块的工具输出行不解析。
"""
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

CODEX_SESSIONS = Path.home() / '.codex' / 'sessions'
CLAUDE_PROJECTS = Path.home() / '.claude' / 'projects'
USER_CHARS = 600          # 本人一次输入最多留多少字
FINAL_CHARS = 1500        # 一轮最终输出最多留多少字
OFFSET_KEEP_SEC = 7 * 86400   # 多久没更新的文件不再记位置

# 注入进对话、不是本人打的字
CODEX_INJECTED = ('# AGENTS.md', '<environment_context', '<user_instructions', '<INSTRUCTIONS', '<turn_aborted',
                  '<user_shell_command', '<permissions', '<skill', '<subagent')
CLAUDE_INJECTED = ('<command-name>', '<command-message>', '<local-command', '<system-reminder>', '<task-notification>',
                   'Caveat:', '[Request interrupted')


def _ts(s) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return 0.0


def _clip(text: str, n: int) -> str:
    text = ' '.join(str(text).split())
    return text if len(text) <= n else text[:n] + f'…(截断, 原文 {len(text)} 字)'


def _read_new(path: Path, offset: int):
    """从 offset 读到最后一个完整行。返回 (行的列表, 新 offset)。文件变短(被重写)就从头读。"""
    size = path.stat().st_size
    if offset > size:
        offset = 0
    if offset == size:
        return [], offset
    with open(path, 'rb') as fh:
        fh.seek(offset)
        data = fh.read()
    end = data.rfind(b'\n')
    if end < 0:
        return [], offset
    return data[:end + 1].decode('utf-8', 'replace').splitlines(), offset + end + 1


def _first_line(path: Path) -> dict:
    try:
        with open(path, 'rb') as fh:
            return json.loads(fh.readline())
    except (OSError, ValueError):
        return {}


_branch_cache = {}


def _git_branch(cwd: str) -> str:
    if not cwd:
        return ''
    if cwd not in _branch_cache:
        try:
            out = subprocess.run(['git', '-C', cwd, 'rev-parse', '--abbrev-ref', 'HEAD'], capture_output=True,
                                 text=True, timeout=5)
            _branch_cache[cwd] = out.stdout.strip() if out.returncode == 0 else ''
        except (OSError, subprocess.SubprocessError):
            _branch_cache[cwd] = ''
    return _branch_cache[cwd]


def _codex(path: Path, lines, since: float, until: float) -> dict:
    meta = _first_line(path).get('payload') or {}
    if meta.get('thread_source') == 'subagent':
        return {}
    out = {'tool': 'Codex', 'cwd': meta.get('cwd') or '', 'branch': '', 'title': '', 'items': []}
    for line in lines:
        if '"turn_context"' in line[:200]:
            try:
                out['cwd'] = json.loads(line)['payload'].get('cwd') or out['cwd']
            except (ValueError, KeyError, AttributeError):
                pass
            continue
        if '"task_complete"' in line[:200]:
            kind = 'final'
        elif '"role":"user"' in line and '"type":"message"' in line[:200]:
            kind = 'user'
        else:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        ts, p = _ts(d.get('timestamp')), d.get('payload') or {}
        if not since <= ts < until:
            continue
        if kind == 'final':
            text = p.get('last_agent_message') or ''
        else:
            text = ''.join(c.get('text', '') for c in p.get('content') or [] if isinstance(c, dict)).strip()
            if text.startswith(CODEX_INJECTED):
                continue
        if text.strip():
            out['items'].append((ts, kind, text))
    out['branch'] = _git_branch(out['cwd'])
    return out


def _claude_user_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    parts = [c.get('text', '') for c in content or [] if isinstance(c, dict) and c.get('type') == 'text']
    return '\n'.join(t for t in parts if not t.lstrip().startswith('<system-reminder>')).strip()


def _claude(path: Path, lines, since: float, until: float) -> dict:
    """一轮的最终输出 = 下一次本人输入之前, 主线程里最后一段 assistant 文字。"""
    out = {'tool': 'Claude Code', 'cwd': '', 'branch': '', 'title': '', 'items': []}
    last = None   # 这一轮到目前为止最后一段 assistant 文字 (ts, text)

    def flush():
        nonlocal last
        if last and since <= last[0] < until:
            out['items'].append((last[0], 'final', last[1]))
        last = None

    for line in lines:
        head = line[:300]
        if '"type":"ai-title"' in head or '"type":"summary"' in head:
            try:
                d = json.loads(line)
                out['title'] = d.get('aiTitle') or d.get('summary') or out['title']
            except ValueError:
                pass
            continue
        if '"isSidechain":false' not in head:
            continue
        is_user = '"type":"user"' in line and '"tool_result"' not in line and '"isMeta":true' not in line
        is_text = '"type":"assistant"' in line and '"type":"text"' in line
        if not (is_user or is_text):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        msg, ts = d.get('message') or {}, _ts(d.get('timestamp'))
        out['cwd'] = d.get('cwd') or out['cwd']
        out['branch'] = d.get('gitBranch') or out['branch']
        if d.get('type') == 'user':
            text = _claude_user_text(msg.get('content'))
            if not text or text.startswith(CLAUDE_INJECTED):
                continue
            flush()
            if since <= ts < until:
                out['items'].append((ts, 'user', text))
        elif d.get('type') == 'assistant':
            text = '\n'.join(c.get('text', '') for c in msg.get('content') or []
                             if isinstance(c, dict) and c.get('type') == 'text').strip()
            if text:
                last = (ts, text)
    flush()
    return out


def _candidates(since: float):
    """最近有更新的会话文件: Codex 按 mtime 扫整个 sessions 目录(旧会话 resume 后还写在原来日期的目录里);
    Claude 只看 projects/<项目>/<会话>.jsonl, 子 agent 的文件在更深的目录里, 不要。"""
    larkx_home = str(Path.home() / '.larkx')
    if CODEX_SESSIONS.exists():
        for root, _, files in os.walk(CODEX_SESSIONS):
            for f in files:
                p = Path(root) / f
                if f.endswith('.jsonl') and _mtime(p) >= since:
                    yield 'codex', p
    if CLAUDE_PROJECTS.exists():
        for p in CLAUDE_PROJECTS.glob('*/*.jsonl'):
            if _mtime(p) >= since and not p.parent.name.startswith(larkx_home.replace('/', '-').replace('.', '-')):
                yield 'claude', p


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def collect(since: float, until: float, offsets: dict = None) -> tuple:
    """[since, until) 里本人在 Codex / Claude Code 会话里的输入和最终输出。
    offsets 是 {文件: 读到的字节位置}: 给了就只读新追加的部分, 返回更新后的 offsets(没给就整份读、按时间过滤, 用于预览)。
    返回 (sessions, offsets); sessions 每项 {'tool','cwd','branch','title','items': [(ts, 'user'|'final', text)]}。"""
    track = offsets is not None
    offsets = dict(offsets or {})
    sessions = []
    larkx_home = str(Path.home() / '.larkx')
    for kind, path in _candidates(since):
        key = str(path)
        seen = track and key in offsets
        try:
            lines, end = _read_new(path, offsets.get(key, 0) if track else 0)
        except OSError:
            continue
        if track:
            offsets[key] = end
        if not lines:
            continue
        # 记着位置的文件, 新读到的就是增量, 不再按时间卡(读的时候还在写的那几条也算进来);
        # 第一次见的文件整份读, 只要 since 之后的
        lo, hi = (0, float('inf')) if seen else (since, until if not track else float('inf'))
        s = (_codex if kind == 'codex' else _claude)(path, lines, lo, hi)
        if s and s['items'] and not s['cwd'].startswith(larkx_home):
            s['items'].sort(key=lambda x: x[0])
            sessions.append(s)
    if track:
        now = time.time()
        offsets = {k: v for k, v in offsets.items() if now - _mtime(Path(k)) < OFFSET_KEEP_SEC}
    sessions.sort(key=lambda s: s['items'][0][0])
    return sessions, offsets


def render(sessions) -> str:
    """压成文本, 和消息增量一起喂给消化轮; 没有就返回 ''。"""
    if not sessions:
        return ''
    days = {datetime.fromtimestamp(ts).date() for s in sessions for ts, _, _ in s['items']}
    fmt = '%m-%d %H:%M' if len(days) > 1 else '%H:%M'
    blocks = ['# 本人的 AI 编码会话(Codex / Claude Code): 本人的输入和每轮的最终输出']
    for s in sessions:
        head = f'## {s["tool"]} 会话'
        if s['title']:
            head += f'「{s["title"]}」'
        if s['cwd']:
            head += f' 目录={s["cwd"]}'
        if s['branch'] and s['branch'] != 'HEAD':
            head += f' 分支={s["branch"]}'
        lines = [head]
        for ts, kind, text in s['items']:
            who, n = ('本人', USER_CHARS) if kind == 'user' else ('最终输出', FINAL_CHARS)
            lines.append(f'- {datetime.fromtimestamp(ts).strftime(fmt)} {who}: {_clip(text, n)}')
        blocks.append('\n'.join(lines))
    return '\n\n'.join(blocks)


def split(text: str, part_chars: int) -> list:
    """按会话块切段, 每段不超过 part_chars(单个块超长就硬切)。"""
    if not text:
        return []
    head, *blocks = text.split('\n\n')
    parts, cur = [], head
    for b in blocks:
        while len(b) > part_chars:
            parts.append(f'{head}\n\n{b[:part_chars]}')
            b = b[part_chars:]
        if len(cur) + len(b) + 2 > part_chars and cur != head:
            parts.append(cur)
            cur = head
        cur += '\n\n' + b
    if cur != head:
        parts.append(cur)
    return parts
