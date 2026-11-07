"""Codex 后端: 每条消息起一个 `codex exec`, 需要接着聊时用 `resume <thread_id>`。

会话策略(LARKX_AGENT_SESSION, 留空时由记忆策略决定, 见 larkx/plugins/memory/):
- single(上游): 所有消息进同一个 thread, 上下文压缩由 Codex 自己完成。
- chat: 按调用方给的 key 分 thread(如每个飞书会话一个), 和它上次交流超过 LARKX_AGENT_SESSION_IDLE 秒就开新 thread;
  key 为 None 的轮次(维护轮)每次都开新 thread。新 thread 最前面放调用方给的说明(note), 由记忆策略告诉它怎么接上前情。
"""
import asyncio
import glob
import json
import os
import signal
import time
import tomllib
from pathlib import Path

from loguru import logger

from ..config import ACTOR_ENV
from .base import SessionCrashed, TurnResult, agent_path, resolve_agent_cli

SESSION_KEY = 'main:codex'
CHAT_KEY_PREFIX = 'codex:'
# 从用户自己的 Codex 配置里沿用的项: 只取模型供应商相关, hooks / MCP / memories 等一律不带进来
COPIED_CONFIG_KEYS = ('model_provider', 'model_providers')


def user_codex_home() -> Path:
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')


def _toml_key(k: str) -> str:
    return k if k.replace('_', '').replace('-', '').isalnum() else json.dumps(k, ensure_ascii=False)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return '[' + ', '.join(_toml_value(x) for x in v) + ']'
    raise TypeError(f'不支持的配置值类型: {type(v).__name__}')


def dump_toml(d: dict, prefix=()) -> list:
    lines = [f'{_toml_key(k)} = {_toml_value(v)}' for k, v in d.items() if not isinstance(v, dict)]
    for k, v in d.items():
        if isinstance(v, dict):
            path = prefix + (k,)
            sub = dump_toml(v, path)
            # 只含子表的中间层不需要单独的表头
            if any(not isinstance(x, dict) for x in v.values()) or not v:
                sub = ['', '[' + '.'.join(_toml_key(p) for p in path) + ']'] + sub
            lines += sub
    return lines


def ensure_codex_home(cfg: dict) -> Path:
    """数字人专用的 CODEX_HOME: 认证软链到用户自己的 auth.json, 配置由 larkx 生成。"""
    home = Path(cfg['agent_codex_dir'])
    home.mkdir(parents=True, exist_ok=True)
    user_home = user_codex_home()
    auth = home / 'auth.json'
    if not auth.exists() and (user_home / 'auth.json').exists():
        if auth.is_symlink():
            auth.unlink()
        auth.symlink_to(user_home / 'auth.json')
    conf = {}
    try:
        user_conf = tomllib.loads((user_home / 'config.toml').read_text(encoding='utf-8'))
        conf.update({k: user_conf[k] for k in COPIED_CONFIG_KEYS if k in user_conf})
    except FileNotFoundError:
        pass
    conf.update({
        'approval_policy': 'never',
        'sandbox_mode': 'danger-full-access',
        'check_for_update_on_startup': False,
        'features': {'hooks': False, 'memories': False},
        'projects': {cfg['agent_home']: {'trust_level': 'trusted'}},
    })
    text = '# 由 larkx 生成, 每次启动覆盖; 认证用软链的 auth.json\n' + '\n'.join(dump_toml(conf)) + '\n'
    (home / 'config.toml').write_text(text, encoding='utf-8')
    from .home import TEMPLATES
    skill = home / 'skills' / 'larkx'
    skill.mkdir(parents=True, exist_ok=True)
    (skill / 'SKILL.md').write_bytes((TEMPLATES / 'skills' / 'larkx' / 'SKILL.md').read_bytes())
    return home


class CodexTurnParser:
    """把 `codex exec --json` 的事件流折叠成一轮的结果。"""

    def __init__(self):
        self.thread_id = ''
        self.texts = []
        self.tools = []
        self.completed = False
        self.failed = False
        self.error = ''
        self.usage = {}

    def feed(self, line: str):
        """返回这一行里刚执行完的 shell 命令(用于日志), 没有则返回 None。"""
        try:
            e = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return None
        t = e.get('type')
        if t == 'thread.started':
            self.thread_id = e.get('thread_id') or self.thread_id
        elif t == 'item.completed':
            item = e.get('item') or {}
            kind = item.get('type')
            if kind == 'agent_message':
                self.texts.append(item.get('text') or '')
            elif kind == 'command_execution':
                self.tools.append('shell')
                return item.get('command') or ''
            elif kind in ('file_change', 'mcp_tool_call', 'web_search'):
                self.tools.append(kind)
        elif t == 'turn.completed':
            self.completed = True
            self.usage = e.get('usage') or {}
        elif t == 'turn.failed':
            self.failed = True
            self.error = (e.get('error') or {}).get('message') or ''
        return None


class CodexSession:
    def __init__(self, cfg: dict, storage):
        self.cfg = cfg
        self.storage = storage
        self.home = Path(cfg['agent_home'])
        self.codex_home = Path(cfg['agent_codex_dir'])
        self.cli = resolve_agent_cli(cfg.get('codex_cli') or 'codex')
        self.mode = cfg.get('agent_session') or 'single'
        self.idle = int(cfg.get('agent_session_idle') or 7200)
        self.session_id = ''
        self.busy = False
        self.events = []
        self._proc = None
        self._interrupted = False
        self._lock = asyncio.Lock()

    def _rollout_exists(self, tid: str) -> bool:
        return bool(tid) and bool(glob.glob(str(self.codex_home / 'sessions' / '**' / f'rollout-*{tid}.jsonl'), recursive=True))

    async def start(self):
        ensure_codex_home(self.cfg)
        if self.mode != 'single':
            logger.info(f'数字人会话(codex): 每个会话一个 thread, 空闲 {self.idle} 秒后开新的; 维护轮每次开新的')
            return
        row = self.storage.session_get(SESSION_KEY)
        if row and self._rollout_exists(row.agent_session_id):
            self.session_id = row.agent_session_id
            logger.info(f'数字人会话已恢复(codex): {self.session_id}')
        else:
            logger.info('数字人会话(codex)将在第一条消息时创建')

    def _storage_key(self, key):
        """这一轮用哪个存储键接着聊; None 表示开新 thread、用完不留。"""
        if self.mode == 'single':
            return SESSION_KEY
        return CHAT_KEY_PREFIX + key if key else None

    def _resume_id(self, skey) -> str:
        if not skey:
            return ''
        row = self.storage.session_get(skey)
        if not row or not self._rollout_exists(row.agent_session_id):
            return ''
        if self.mode != 'single' and row.last_active and time.time() - row.last_active.timestamp() > self.idle:
            return ''
        return row.agent_session_id

    def _args(self, resume_id: str = '', sandbox_dir: str = '') -> list:
        args = [self.cli, 'exec'] + (['resume', resume_id] if resume_id else [])
        args += ['--json', '--skip-git-repo-check']
        if sandbox_dir:
            # 只能写这个目录、不能联网: 处理的是别人发的原始消息, 里面的指令不能变成真的操作
            args += ['-s', 'workspace-write', '-C', sandbox_dir, '-c', 'sandbox_workspace_write.network_access=false']
        else:
            args += ['--dangerously-bypass-approvals-and-sandbox']
        args += ['-c', f'shell_environment_policy.set.{ACTOR_ENV}="agent"']
        if self.cfg.get('agent_model'):
            args += ['-m', self.cfg['agent_model']]
        return args + ['-']

    async def ask(self, prompt: str, key=None, note=None, sandbox_dir: str = '') -> TurnResult:
        """key: 这一轮属于哪个会话(如 chat:<chat_id>), None 表示开新 thread、用完不留(single 模式下忽略, 都进同一个 thread);
        note: chat 模式开了新 thread 时放在最前面的说明, 不给就不加;
        sandbox_dir: 非空时这一轮开新 thread, 在沙箱里跑, 只能写这个目录、不能联网。"""
        async with self._lock:
            self.busy = True
            self._interrupted = False
            started = time.monotonic()
            skey = None if sandbox_dir else self._storage_key(key)
            resume_id = self._resume_id(skey)
            if not resume_id and self.mode != 'single' and note:
                prompt = note + prompt
            self.session_id = resume_id
            env = dict(os.environ, CODEX_HOME=str(self.codex_home), PATH=agent_path())
            env[ACTOR_ENV] = 'agent'
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *self._args(resume_id, sandbox_dir), cwd=str(self.home), env=env, limit=16 * 1024 * 1024,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                self._proc.stdin.write(prompt.encode('utf-8'))
                await self._proc.stdin.drain()
                self._proc.stdin.close()
                return await self._finish(started, skey)
            except OSError as e:
                raise SessionCrashed(f'启动 codex 失败: {e!r}') from e
            finally:
                self.busy = False
                self._proc = None

    async def _finish(self, started: float, skey=SESSION_KEY) -> TurnResult:
        parser = CodexTurnParser()
        stderr_task = asyncio.create_task(self._proc.stderr.read())

        async def read_events():
            async for raw in self._proc.stdout:
                cmd = parser.feed(raw.decode('utf-8', errors='replace'))
                if cmd is not None:
                    logger.info(f'[agent] $ {cmd[:300]}')

        try:
            await asyncio.wait_for(read_events(), self.cfg['agent_turn_timeout'])
        except asyncio.TimeoutError:
            logger.warning('这一轮处理超时, 中断')
            self.events.append(f'一轮处理超过 {self.cfg["agent_turn_timeout"]} 秒, 已中断')
            await self.interrupt()
            try:
                await asyncio.wait_for(read_events(), 30)
            except asyncio.TimeoutError:
                self._proc.kill()
        rc = await self._proc.wait()
        stderr = (await stderr_task).decode('utf-8', errors='replace')
        if parser.thread_id and parser.thread_id != self.session_id:
            self.session_id = parser.thread_id
            if skey:
                chat_id = skey.split('chat:', 1)[1] if skey.startswith(CHAT_KEY_PREFIX + 'chat:') else ''
                self.storage.session_upsert(skey, chat_id, '', self.session_id, True)
        if self.session_id and skey:
            self.storage.session_touch(skey)
        text = '\n'.join(t for t in parser.texts if t).strip()
        duration = time.monotonic() - started
        if parser.completed:
            return TurnResult('success', False, text, duration, parser.tools)
        if self._interrupted:
            return TurnResult('interrupted', True, text, duration, parser.tools)
        if parser.failed:
            return TurnResult('error', True, parser.error or text, duration, parser.tools)
        raise SessionCrashed(f'codex 异常退出({rc}): {stderr.strip()[-300:]}')

    async def context_pressure(self):
        return None

    async def interrupt(self) -> bool:
        if self.busy and self._proc and self._proc.returncode is None:
            self._interrupted = True
            self._proc.send_signal(signal.SIGINT)
            return True
        return False

    async def reset(self):
        await self.interrupt()
        async with self._lock:
            self.session_id = ''
            self.storage.session_upsert(SESSION_KEY, '', '', '', False)
            for row in self.storage.list_sessions(limit=10000):
                if row.session_key.startswith(CHAT_KEY_PREFIX) and row.agent_session_id:
                    self.storage.session_upsert(row.session_key, row.chat_id, '', '', False)
        logger.info('数字人会话(codex)已重置, 下一条消息开新会话')

    async def close(self):
        await self.interrupt()
