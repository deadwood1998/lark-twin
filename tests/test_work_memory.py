"""记忆策略 work: 免打扰过滤、增量切段、三层记忆的自动规则、每小时消化、按会话分 thread。"""
import asyncio
import json
import time
from datetime import date, datetime
from pathlib import Path

import pytest

from larkx.agent import TurnResult
from larkx.agent.codex import CHAT_KEY_PREFIX, CodexSession
from larkx.plugins.memory import work
from larkx.plugins.memory.work import GUEST_SESSION_NOTE, NEW_SESSION_NOTE, WorkMemory, feed
from larkx.plugins.memory.work import layout as memory
from larkx.storage import Storage

from test_reliability import _daemon

NOW = int(datetime(2026, 11, 5, 15, 0).timestamp())


def save(st, mid, chat, text, ts, chat_type=2, sender='张三', msg_type=4, msg_type_name='TEXT', chat_name='项目群', root=''):
    st.save_message({'msg_id': mid, 'chat_id': chat, 'chat_type': chat_type, 'from_id': '200', 'content': text,
                     'create_time': ts, 'msg_type': msg_type, 'msg_type_name': msg_type_name, 'root_id': root},
                    sender_name=sender, chat_name=chat_name)


@pytest.fixture
def st(tmp_path):
    return Storage(f'sqlite:///{tmp_path}/m.db')


# ---- 增量 ----

def test_feed_skips_bot_push_p2p(st):
    save(st, 'k1', 'p-bot', '[卡片] 本周技术文章', NOW - 600, chat_type=1, chat_name='ByteTech', sender='ByteTech',
         msg_type=14, msg_type_name='CARD')
    save(st, 'k2', 'p-bot2', '[卡片] 新工单', NOW - 500, chat_type=1, chat_name='工单BOT', sender='工单BOT',
         msg_type=14, msg_type_name='CARD')
    save(st, 'k3', 'p-bot2', '帮我看下这个工单', NOW - 400, chat_type=1, chat_name='工单BOT', sender='张三')
    save(st, 'k4', 'g-card', '[卡片] 发布单', NOW - 300, msg_type=14, msg_type_name='CARD')
    feed.chat_remind_set(st, {'p-bot': True, 'p-bot2': True, 'g-card': True})
    chats = feed.feed_chats(st, NOW - 3600, NOW)
    assert chats['bot'] == ['p-bot']
    assert sorted(chats['include']) == ['g-card', 'p-bot2']
    assert '本周技术文章' not in '\n'.join(feed.build_feed(st, NOW - 3600, NOW))


def test_feed_only_unmuted_chats(st):
    save(st, 'a1', 'g-work', 'feat/listen 已经合进 main 了', NOW - 600)
    save(st, 'a2', 'g-work', '入群', NOW - 590, msg_type=6, msg_type_name='SYSTEM')
    save(st, 'a3', 'g-work', '话题里补一句', NOW - 580, root='a1')
    save(st, 'b1', 'g-alarm', 'CPU 报警', NOW - 500, chat_name='报警群')
    save(st, 'c1', 'g-new', '没查过免打扰的会话', NOW - 400, chat_name='新群')
    save(st, 'd1', 'p-li', '接口文档在哪', NOW - 300, chat_type=1, chat_name='李四', sender='李四')
    save(st, 'e1', 'doc:abc', '这段逻辑有问题', NOW - 200, chat_name='设计文档')
    save(st, 'z1', 'g-work', '窗口外的', NOW - 7200)
    feed.chat_remind_set(st, {'g-work': True, 'g-alarm': False, 'p-li': True})
    chats = feed.feed_chats(st, NOW - 3600, NOW)
    assert sorted(chats['include']) == ['doc:abc', 'g-work', 'p-li']
    assert chats['muted'] == ['g-alarm'] and chats['unknown'] == ['g-new']
    parts = feed.build_feed(st, NOW - 3600, NOW)
    text = '\n'.join(parts)
    assert len(parts) == 1
    assert 'feat/listen 已经合进 main 了' in text and '↳张三: 话题里补一句' in text and '接口文档在哪' in text
    assert '设计文档' in text
    for absent in ('CPU 报警', '没查过免打扰', '入群', '窗口外的'):
        assert absent not in text
    assert feed.build_feed(st, NOW - 60, NOW) == []


def test_feed_truncates_and_splits(st):
    feed.chat_remind_set(st, {'g1': True, 'g2': True})
    for i in range(30):
        save(st, f'x{i}', 'g1', '长' * 1000, NOW - 1000 + i)
    save(st, 'y', 'g2', '另一个群的消息', NOW - 10, chat_name='二群')
    parts = feed.build_feed(st, NOW - 3600, NOW, part_chars=3000)
    assert len(parts) > 3 and all(len(p) <= 3000 + 500 for p in parts)
    assert '截断, 原文 1000 字' in parts[0]
    assert '（续）' in parts[1]                              # 一个群跨段时, 段首重复会话标题
    assert sum(p.count('\n- ') + p.startswith('- ') for p in parts) == 31   # 每条消息只出现一次
    assert '另一个群的消息' in parts[-1]


# ---- 三层记忆 ----

def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')
    return p


def test_layout_rules_and_pointer(tmp_path):
    (tmp_path / 'memory').mkdir()
    (tmp_path / 'memory' / 'MEMORY.md').write_text('# 记忆索引\n\n- [项目](projects.md) — 旧的\n', encoding='utf-8')
    root = memory.ensure_layout(tmp_path)
    memory.ensure_layout(tmp_path)
    index = (tmp_path / 'memory' / 'MEMORY.md').read_text(encoding='utf-8')
    assert index.count('work/OVERVIEW.md') == 1 and '旧的' in index
    assert (root / 'RULES.md').read_text(encoding='utf-8') == memory.RULES
    assert all((root / d).is_dir() for d in memory.DIMS)
    (root / 'OVERVIEW.md').write_text('# 我改过的总览\n', encoding='utf-8')
    memory.ensure_layout(tmp_path)
    # 总览不覆盖, 只补上缺的"本人在做"小节
    assert (root / 'OVERVIEW.md').read_text(encoding='utf-8') == '# 我改过的总览\n## 本人在做\n'


def test_short_term_pruned_after_24h(tmp_path):
    root = memory.ensure_layout(tmp_path)
    write(root, 'OVERVIEW.md', '# 工作记忆总览\n\n## 进行中的项目\n- 2026-11-01 00:00 这行不在短期小节里, 不动\n\n'
                               '## 近 24 小时\n- 2026-11-04 14:00 太旧\n  续行也一起删\n- 2026-11-04 16:00 还在 24 小时内\n'
                               '- 没写时间的不动\n\n## 待跟进\n- 回李四\n')
    assert memory.prune_short_term(tmp_path, NOW) == 1
    text = (root / 'OVERVIEW.md').read_text(encoding='utf-8')
    assert '太旧' not in text and '续行' not in text
    for kept in ('这行不在短期小节里', '还在 24 小时内', '没写时间的不动', '回李四'):
        assert kept in text


L2 = '''# larkx 数字人
类型：项目
状态：{status}
最后更新：{last}
{extra}
## 现状
在做旁听。

## 近况
- 2026-10-20 10:00 很早的进展
  续行
- 2026-10-27 18:00 上上周的
- 2026-11-04 12:00 最近的
'''


def test_old_items_archived_with_ref(tmp_path):
    root = memory.ensure_layout(tmp_path)
    write(root, 'projects/larkx.md', L2.format(status='进行中', last='2026-11-04', extra=''))
    report = memory.archive_items(tmp_path, date(2026, 11, 5))
    assert [(f, n) for f, n, _ in report] == [('projects/larkx.md', 1), ('projects/larkx.md', 1)]   # 两周各一条
    text = (root / 'projects/larkx.md').read_text(encoding='utf-8')
    assert '很早的进展' not in text and '上上周的' not in text and '最近的' in text
    assert '- 2026-W43 → archive/2026-W43/projects/larkx.md' in text
    assert '- 2026-W44 → archive/2026-W44/projects/larkx.md' in text
    old = (root / 'archive/2026-W43/projects/larkx.md').read_text(encoding='utf-8')
    assert '很早的进展' in old and '续行' in old
    assert memory.archive_items(tmp_path, date(2026, 11, 5)) == []      # 再跑一次没有变化


def test_done_and_stale_files_archived(tmp_path):
    root = memory.ensure_layout(tmp_path)
    write(root, 'projects/larkx.md', L2.format(status='进行中', last='2026-11-05', extra='关联：tasks/旁听.md、tasks/券.md'))
    write(root, 'tasks/旁听.md', L2.format(status='已完成', last='2026-11-02', extra='完成于：2026-11-02'))
    write(root, 'tasks/刚做完.md', L2.format(status='已完成', last='2026-11-04', extra='完成于：2026-11-04'))
    write(root, 'tasks/券.md', L2.format(status='进行中', last='2026-10-28', extra='').replace('- 2026-11-04 12:00 最近的\n', ''))
    write(root, 'tasks/近况还新.md', L2.format(status='进行中', last='2026-10-20', extra=''))   # 最后更新旧, 但近况 11-04 还有动静
    write(root, 'people/张三.md', '# 张三\n类型：人\n状态：活跃\n最后更新：2026-11-05\n')
    write(root, 'OVERVIEW.md', '# 工作记忆总览\n\n## 进行中的任务\n- [旁听](tasks/旁听.md) — 做完了\n'
                               '- [刚做完](tasks/刚做完.md) — 昨天做完\n- [券](tasks/券.md) — 一周没消息\n')
    report = memory.archive_files(tmp_path, date(2026, 11, 5))
    moved = {old: new for old, new, _ in report}
    # 11-02 是周一(W45), 10-28 在 W44: 按完成 / 最后更新的那一周归档
    assert moved == {'tasks/旁听.md': 'archive/2026-W45/tasks/旁听.md', 'tasks/券.md': 'archive/2026-W44/tasks/券.md'}
    assert not (root / 'tasks/旁听.md').exists() and (root / 'tasks/刚做完.md').exists() and (root / 'tasks/近况还新.md').exists()
    stale = (root / 'archive/2026-W44/tasks/券.md').read_text(encoding='utf-8')
    assert '状态：已完成（推定：7 天没有动静' in stale
    overview = (root / 'OVERVIEW.md').read_text(encoding='utf-8')
    assert '刚做完' in overview and 'tasks/旁听.md' not in overview and '一周没消息' not in overview
    proj = (root / 'projects/larkx.md').read_text(encoding='utf-8')
    assert '关联：archive/2026-W45/tasks/旁听.md、archive/2026-W44/tasks/券.md' in proj
    index = (root / 'archive/INDEX.md').read_text(encoding='utf-8')
    assert index.count('→ archive/') == 2 and '7 天没有动静' in index
    assert (root / 'people/张三.md').exists()


def test_maintain_dry_run_changes_nothing(tmp_path):
    root = memory.ensure_layout(tmp_path)
    p = write(root, 'tasks/旧.md', L2.format(status='已完成', last='2026-10-01', extra='完成于：2026-10-01'))
    before = p.read_text(encoding='utf-8')
    result = memory.maintain(tmp_path, now=NOW, dry_run=True)
    assert result['files'] and p.read_text(encoding='utf-8') == before
    assert '整件归档 tasks/旧.md' in memory.summarize(result)


# ---- daemon: 每小时消化 ----

def _digest_daemon(tmp_path, fake_client, monkeypatch):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    obj.cfg.update(memory_digest_interval=3600, agent_backend='codex', agent_session='chat', memory_sessions=False)
    obj.memory = WorkMemory(obj.cfg)
    memory.ensure_layout(tmp_path)
    monkeypatch.setattr(obj, '_owner_name', lambda: '赵六')
    obj.session.calls = []
    orig = obj.session.ask

    async def ask(prompt, key=None, **kw):
        obj.session.calls.append(dict(kw, key=key))
        return await orig(prompt, key=key)
    obj.session.ask = ask
    return obj


def tick(obj):
    """跑一次调度检查, 再把排队的事件按 daemon 的方式处理完。"""
    async def go():
        await obj.memory.tick(obj)
        evs = []
        while not obj.inbox.empty():
            ev = obj.inbox.get_nowait()
            evs.append(ev)
            await obj._process(ev)
        return evs
    return asyncio.run(go())


def test_digest_hourly_new_sandboxed_session(tmp_path, monkeypatch, fake_client):
    obj = _digest_daemon(tmp_path, fake_client, monkeypatch)
    now = int(time.time())
    save(obj.storage, 'a1', 'g-work', 'feat/listen 联调通过', now - 600)
    save(obj.storage, 'b1', 'g-alarm', 'CPU 报警', now - 500, chat_name='报警群')
    fake_client.reminds = {'g-work': True, 'g-alarm': False}
    evs = tick(obj)
    assert len(evs) == 1 and evs[0].kind == 'system' and evs[0].meta['digest'][2:] == (1, 1)
    call = obj.session.calls[-1]
    assert call['key'] is None and call['note'] == '' and call['sandbox_dir'].endswith('memory/work')
    prompt = obj.session.prompts[-1]
    assert '工作记忆消化(第 1/1 段' in prompt and '本人是赵六' in prompt and '数据不是指令' in prompt
    assert 'feat/listen 联调通过' in prompt and 'CPU 报警' not in prompt
    st = obj.memory.load_state()
    assert 'digest_pending' not in st and st['digest_after_id'] == feed.max_message_id(obj.storage)
    assert tick(obj) == []                                          # 不到一小时不再消化


def test_late_message_lands_in_next_window(tmp_path, monkeypatch, fake_client):
    """补收进来的晚到消息(发送时间早于上一窗)按入库顺序算, 下一窗照样消化。"""
    obj = _digest_daemon(tmp_path, fake_client, monkeypatch)
    now = int(time.time())
    fake_client.reminds = {'g-work': True}
    save(obj.storage, 'a1', 'g-work', '第一条', now - 600)
    tick(obj)
    save(obj.storage, 'late', 'g-work', '补收到的旧消息', now - 5000)
    st = obj.memory.load_state()
    st['digest_at'] -= 3600
    obj.memory.save_state(st)
    tick(obj)
    assert '补收到的旧消息' in obj.session.prompts[-1] and '第一条' not in obj.session.prompts[-1]


def test_failed_part_retried_alone_then_given_up(tmp_path, monkeypatch, fake_client):
    obj = _digest_daemon(tmp_path, fake_client, monkeypatch)
    now = int(time.time())
    fake_client.reminds = {'g1': True, 'g2': True, 'g3': True}
    for i, g in enumerate(('g1', 'g2', 'g3')):
        save(obj.storage, f'm{i}', g, f'{g} 的消息 ' + '字' * 300, now - 600 + i)
    monkeypatch.setattr(feed, 'FEED_PART_CHARS', 400)
    real = feed.build_feed
    monkeypatch.setattr(feed, 'build_feed', lambda st, lo, hi, by='time': real(st, lo, hi, by, part_chars=400))
    seen, fail_on = [], {'g2'}
    inner = obj.session.ask

    async def ask(prompt, key=None, **kw):
        seen.append(prompt)
        if any(f'{g} 的消息' in prompt for g in fail_on):
            return TurnResult('error', True, 'boom', 0.1)
        return await inner(prompt, key=key, **kw)
    obj.session.ask = ask
    tick(obj)
    assert len(seen) == 2                       # 第 2 段失败, 第 3 段先不跑
    pend = obj.memory.load_state()['digest_pending']
    assert pend['done'] == [1] and pend['fails'] == 1
    assert tick(obj) == []                      # 还没到重试时间
    obj.memory.retry_at = 0
    seen.clear()
    fail_on.clear()
    tick(obj)                                   # 只重跑没成功的 2、3 段
    assert len(seen) == 2 and 'g1 的消息' not in ''.join(seen)
    assert 'digest_pending' not in obj.memory.load_state()


def test_gives_up_after_max_fails(tmp_path, monkeypatch, fake_client):
    obj = _digest_daemon(tmp_path, fake_client, monkeypatch)
    fake_client.reminds = {'g1': True}
    save(obj.storage, 'm1', 'g1', '总失败', int(time.time()) - 60)

    async def fail(prompt, key=None, **kw):
        return TurnResult('error', True, 'boom', 0.1)
    obj.session.ask = fail
    for _ in range(work.DIGEST_MAX_FAILS):
        obj.memory.retry_at = 0
        tick(obj)
    st = obj.memory.load_state()
    assert 'digest_pending' not in st and st['digest_after_id'] == feed.max_message_id(obj.storage)


def test_restart_resumes_pending_window(tmp_path, monkeypatch, fake_client):
    obj = _digest_daemon(tmp_path, fake_client, monkeypatch)
    fake_client.reminds = {'g1': True}
    save(obj.storage, 'm1', 'g1', '重启前没消化完的', int(time.time()) - 60)
    asyncio.run(obj.memory.tick(obj))            # 排了队, daemon 就停了
    obj2 = _digest_daemon(tmp_path, fake_client, monkeypatch)
    obj2.storage = obj.storage
    tick(obj2)
    assert '重启前没消化完的' in obj2.session.prompts[-1]
    assert 'digest_pending' not in obj2.memory.load_state()


def test_digest_without_messages_still_archives(tmp_path, monkeypatch, fake_client):
    obj = _digest_daemon(tmp_path, fake_client, monkeypatch)
    root = memory.work_dir(tmp_path)
    write(root, 'tasks/旧.md', L2.format(status='已完成', last='2020-01-01', extra='完成于：2020-01-01'))
    tick(obj)
    assert obj.session.prompts == [] and not (root / 'tasks/旧.md').exists()


def test_remind_lookup_failure_postpones(tmp_path, monkeypatch, fake_client):
    obj = _digest_daemon(tmp_path, fake_client, monkeypatch)
    save(obj.storage, 'a1', 'g-work', '要等查到免打扰状态', int(time.time()) - 600)
    fake_client.remind_error = RuntimeError('网络断了')
    assert tick(obj) == [] and obj.memory.retry_at > time.time()
    fake_client.remind_error, fake_client.reminds = None, {'g-work': True}
    obj.memory.retry_at = 0
    tick(obj)
    assert '要等查到免打扰状态' in obj.session.prompts[-1]


def test_digest_only_for_codex_chat_mode(tmp_path, monkeypatch, fake_client):
    obj = _digest_daemon(tmp_path, fake_client, monkeypatch)
    for backend, mode, on in (('codex', 'chat', True), ('codex', 'single', False), ('claude', 'chat', False)):
        obj.cfg.update(agent_backend=backend, agent_session=mode)
        assert obj.memory.enabled() is on


def test_daily_review_replaced_by_digest(tmp_path, monkeypatch, fake_client):
    obj = _digest_daemon(tmp_path, fake_client, monkeypatch)
    obj.cfg['memory_review_hour'] = 0
    calls = []

    async def stop(*a):
        raise asyncio.CancelledError()

    async def digest():
        calls.append(1)

    async def noop():
        pass
    from larkx import daemon as d
    monkeypatch.setattr(d.asyncio, 'sleep', stop)
    monkeypatch.setattr(obj.memory, 'tick', lambda daemon: digest())
    monkeypatch.setattr(obj, '_finish_takeovers', noop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(obj._schedule())
    assert calls == [1]
    assert all(ev.ref != 'daily-review' for ev in [obj.inbox.get_nowait() for _ in range(obj.inbox.qsize())])


def test_session_notes():
    from larkx.daemon import Event
    assert work.session_note(Event('system', 'x')) == ''
    assert work.session_note(Event('message', 'x', is_owner=True)) == NEW_SESSION_NOTE   # 本人: 读记忆接上前情
    assert work.session_note(Event('message', 'x')) == GUEST_SESSION_NOTE                # 访客: 不读工作记忆
    assert work.session_note(Event('message', 'x', dev=True)) == NEW_SESSION_NOTE


def test_session_keys():
    from larkx.daemon import Event
    assert work.session_key(Event('system', 'x')) is None
    assert work.session_key(Event('say', 'x')) == 'say'
    assert work.session_key(Event('message', 'x', chat_id='g1')) == 'chat:g1'
    assert work.session_key(Event('task', 'x', chat_id='')) is None


# ---- codex: 按会话分 thread ----

class FakeProc:
    def __init__(self, thread_id):
        lines = [json.dumps({'type': 'thread.started', 'thread_id': thread_id}),
                 json.dumps({'type': 'turn.completed', 'usage': {}})]
        self.stdout = self._aiter([(x + '\n').encode() for x in lines])
        self.stderr = type('E', (), {'read': staticmethod(lambda: asyncio.sleep(0, b''))})()
        self.stdin = type('I', (), {'write': lambda s, b: FakeProc.inputs.append(b.decode()),
                                    'drain': lambda s: asyncio.sleep(0), 'close': lambda s: None})()
        self.returncode = 0

    inputs = []

    @staticmethod
    async def _aiter(items):
        for x in items:
            yield x

    async def wait(self):
        return 0


def test_codex_threads_per_chat(tmp_path, monkeypatch):
    st = Storage(f'sqlite:///{tmp_path}/c.db')
    cfg = {'agent_home': str(tmp_path), 'agent_codex_dir': str(tmp_path / 'codex'), 'codex_cli': 'sh',
           'agent_turn_timeout': 30, 'agent_session': 'chat', 'agent_session_idle': 7200}
    sess = CodexSession(cfg, st)
    calls, threads = [], iter(['t-a', 't-x', 't-a', 't-b'])

    async def spawn(*args, **kw):
        calls.append(args)
        return FakeProc(next(threads))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(sess, '_rollout_exists', lambda tid: bool(tid))
    FakeProc.inputs.clear()

    asyncio.run(sess.ask('第一条', key='chat:g1', note=NEW_SESSION_NOTE))
    assert 'resume' not in calls[-1] and FakeProc.inputs[-1].startswith(NEW_SESSION_NOTE)
    assert st.session_get(CHAT_KEY_PREFIX + 'chat:g1').chat_id == 'g1'
    asyncio.run(sess.ask('维护', key=None))                         # 新开, 不留
    assert 'resume' not in calls[-1]
    asyncio.run(sess.ask('第二条', key='chat:g1', note=NEW_SESSION_NOTE))                  # 同一会话接着聊
    assert calls[-1][2:4] == ('resume', 't-a') and not FakeProc.inputs[-1].startswith(NEW_SESSION_NOTE)
    # 空闲超过 idle: 开新 thread
    from larkx.storage.models import AgentSession
    with st.Session() as s:
        row = s.query(AgentSession).filter_by(session_key=CHAT_KEY_PREFIX + 'chat:g1').first()
        row.last_active = datetime.fromtimestamp(time.time() - 8000)
        s.commit()
    asyncio.run(sess.ask('隔了很久', key='chat:g1'))
    assert 'resume' not in calls[-1]
    assert st.session_get(CHAT_KEY_PREFIX + 'chat:g1').agent_session_id == 't-b'
    asyncio.run(sess.reset())
    assert st.session_get(CHAT_KEY_PREFIX + 'chat:g1').agent_session_id == ''


def test_relink_keeps_followups_and_handles_cjk(tmp_path):
    root = memory.ensure_layout(tmp_path)
    write(root, 'tasks/a.md', L2.format(status='已完成', last='2026-11-01', extra='完成于：2026-11-01').replace('- 2026-11-04 12:00 最近的\n', ''))
    write(root, 'tasks/ba.md', L2.format(status='进行中', last='2026-11-05', extra='关联：见tasks/a.md的结论、./tasks/a.md'))
    write(root, 'OVERVIEW.md', '# 总览\n\n## 进行中的任务\n- [a](tasks/a.md) — 做完了\n- [ba](tasks/ba.md) — 还在做\n\n'
                               '## 近 24 小时\n- 2026-11-05 13:00 张三说 tasks/a.md 的结论要同步\n\n## 待跟进\n- 等本人确认 tasks/a.md 的上线时间\n')
    memory.archive_files(tmp_path, date(2026, 11, 5))
    overview = (root / 'OVERVIEW.md').read_text(encoding='utf-8')
    assert '— 做完了' not in overview and '还在做' in overview
    assert '张三说 archive/2026-W44/tasks/a.md 的结论' in overview
    assert '等本人确认 archive/2026-W44/tasks/a.md 的上线时间' in overview
    ba = (root / 'tasks/ba.md').read_text(encoding='utf-8')
    assert '见archive/2026-W44/tasks/a.md的结论、archive/2026-W44/tasks/a.md' in ba


def test_people_kept_until_30_days_and_status_parsing(tmp_path):
    root = memory.ensure_layout(tmp_path)
    write(root, 'people/张三.md', '# 张三\n类型：人\n状态：活跃\n最后更新：2026-10-20\n')
    write(root, 'people/老王.md', '# 老王\n类型：人\n状态：活跃\n最后更新：2026-09-01\n')
    write(root, 'tasks/完成度.md', '# x\n- **状态**：完成度 30%\n- **最后更新**：2026-11-01\n')
    write(root, 'tasks/加粗完成.md', '# y\n- **状态**：已完成\n- **完成于**：2026年11月1日\n')
    moved = {old for old, _, _ in memory.archive_files(tmp_path, date(2026, 11, 5))}
    assert moved == {'people/老王.md', 'tasks/加粗完成.md'}


def test_tidy_overview_drops_blank_lines_between_items(tmp_path):
    memory.ensure_layout(tmp_path)
    p = memory.work_dir(tmp_path) / 'OVERVIEW.md'
    p.write_text('# 总览\n\n## 进行中的项目\n- a\n\n- b\n\n## 待跟进\n- c\n', encoding='utf-8')
    assert memory.tidy_overview(tmp_path)
    assert p.read_text(encoding='utf-8') == '# 总览\n\n## 进行中的项目\n- a\n- b\n\n## 待跟进\n- c\n'
    assert not memory.tidy_overview(tmp_path)


def test_prune_short_term_heading_without_spaces(tmp_path):
    memory.ensure_layout(tmp_path)
    p = memory.work_dir(tmp_path) / 'OVERVIEW.md'
    p.write_text('# 总览\n\n## 近24小时\n- 2026-11-03 10:00 旧的\n- 2026-11-05 10:00 新的\n\n## 待跟进\n', encoding='utf-8')
    now = datetime(2026, 11, 5, 12, 0).timestamp()
    assert memory.prune_short_term(tmp_path, now) == 1
    assert '旧的' not in p.read_text(encoding='utf-8') and '新的' in p.read_text(encoding='utf-8')


def test_digest_includes_own_coding_sessions(tmp_path, monkeypatch, fake_client):
    from larkx.plugins.memory.work import sessions as session_feed
    obj = _digest_daemon(tmp_path, fake_client, monkeypatch)
    obj.cfg['memory_sessions'] = True
    seen = []

    def collect(since, until, offsets):
        seen.append(dict(offsets))
        s = {'tool': 'Codex', 'cwd': '/repo/a', 'branch': 'feat/x', 'title': '',
             'items': [(until - 60, 'user', '修登录超时'), (until - 30, 'final', '已修复并提 MR')]}
        return [s], {'/f.jsonl': 123}
    monkeypatch.setattr(session_feed, 'collect', collect)
    evs = tick(obj)
    assert len(evs) == 1
    prompt = obj.session.prompts[-1]
    assert '# 本人的 AI 编码会话' in prompt and '分支=feat/x' in prompt and '最终输出: 已修复并提 MR' in prompt
    st = obj.memory.load_state()
    assert st['session_offsets'] == {'/f.jsonl': 123} and 'digest_pending' not in st
    assert not (tmp_path / 'state' / 'digest_sessions.md').exists()


# ---- 本人的工作(me.md) ----

def test_self_file_and_overview_section(tmp_path):
    root = memory.ensure_layout(tmp_path)
    me = (root / 'me.md').read_text(encoding='utf-8')
    assert all(f'## {h}' in me for h in ('正在做', '工作环境', '近况', '归档'))
    assert (root / 'OVERVIEW.md').read_text(encoding='utf-8').index('## 本人在做') < \
        (root / 'OVERVIEW.md').read_text(encoding='utf-8').index('## 进行中的项目')
    assert 'me.md' in (root / 'RULES.md').read_text(encoding='utf-8')
    # 本人改过的 me.md 不被模板覆盖
    write(root, 'me.md', '# 本人\n类型：本人\n\n## 正在做\n- 重构 — group/repo feat/x — 联调中\n- 评审方案 — 等对方\n')
    memory.ensure_layout(tmp_path)
    assert memory.self_doing(tmp_path) == 2


def test_old_overview_gets_self_section(tmp_path):
    root = memory.work_dir(tmp_path)
    write(root, 'OVERVIEW.md', '# 工作记忆总览\n\n## 进行中的项目\n- a\n\n## 待跟进\n- b\n')
    memory.ensure_layout(tmp_path)
    text = (root / 'OVERVIEW.md').read_text(encoding='utf-8')
    assert text.count('## 本人在做') == 1 and text.index('## 本人在做') < text.index('## 进行中的项目')
    assert '- a' in text and '- b' in text
    memory.ensure_layout(tmp_path)
    assert (root / 'OVERVIEW.md').read_text(encoding='utf-8').count('## 本人在做') == 1


def test_self_file_items_archived_but_file_kept(tmp_path):
    root = memory.ensure_layout(tmp_path)
    write(root, 'me.md', '# 本人\n类型：本人\n最后更新：2026-09-01\n\n## 正在做\n- 重构 — tasks/重构.md\n\n'
                         '## 近况\n- 2026-10-20 10:00 在 feat/x 提交了重试逻辑\n- 2026-11-04 09:00 联调通过\n\n## 归档\n')
    write(root, 'tasks/重构.md', '# 重构\n类型：任务\n状态：已完成\n完成于：2026-11-01\n')
    write(root, 'OVERVIEW.md', '# 总览\n\n## 本人在做\n- 重构 — 见 [tasks/重构](tasks/重构.md)\n- 评审方案\n\n## 待跟进\n')
    result = memory.maintain(tmp_path, now=NOW)
    assert ('me.md', 1, 'archive/2026-W43/me.md') in result['items']
    assert 'me.md' not in {old for old, _, _ in result['files']}          # 不整件归档
    me = (root / 'me.md').read_text(encoding='utf-8')
    assert '联调通过' in me and '提交了重试逻辑' not in me and 'archive/2026-W43/me.md' in me
    assert 'archive/2026-W44/tasks/重构.md' in me                            # 引用改成归档路径
    overview = (root / 'OVERVIEW.md').read_text(encoding='utf-8')
    assert 'tasks/重构' not in overview and '- 评审方案' in overview          # 做完归档的那一行去掉
