"""交互协议 work 的会话配置(larkx group): 旁听指定群、开发模式、专属指令; 以及默认就有的 watch / ping / send --thread。"""
import argparse
import asyncio
import time
from types import SimpleNamespace

import pytest

from larkx import state
from larkx.plugins.comms.work import WorkComms, profiles
from larkx.plugins.comms.work.notes import (ADDRESS_NOTE, DEV_GUEST_NOTE, LISTEN_MENTION_NOTE, LISTEN_NOTE, LISTEN_REPLY_NOTE,
                                    profile_senders, sender_allowed)
from larkx.daemon.triggers import FROM_TYPE_BOT, GUEST_NOTE

from test_reliability import _daemon as _base_daemon


@pytest.fixture(autouse=True)
def _work_comms(monkeypatch):
    """这个文件的用例都在交互协议 work 下跑(larkx group 要求启用它)。"""
    monkeypatch.setenv('LARKX_COMMS', 'work')


def _daemon(tmp_path, fake_client, monkeypatch):
    obj = _base_daemon(tmp_path, fake_client, monkeypatch)
    obj.comms = WorkComms(obj.cfg)
    return obj

OTHER = '200'
GROUP = 'g1'


def gmsg(content='这个接口怎么返回 500 了', msg_id='m1', from_id=OTHER, **kw):
    return {'msg_id': msg_id, 'chat_id': GROUP, 'chat_type': 2, 'from_id': from_id, 'content': content,
            'create_time': int(time.time()), **kw}


def run(obj, *msgs):
    async def go():
        for m in msgs:
            await obj.on_message(m, '联调群', '张三')
    asyncio.run(go())


def queued(obj):
    out = []
    while not obj.inbox.empty():
        out.append(obj.inbox.get_nowait())
    return out


def test_no_profile_keeps_default_rules(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    run(obj, gmsg())
    assert queued(obj) == []


def test_listen_takes_messages_without_at(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    profiles.save(obj.storage, GROUP, listen=1, instruction='按联调协议回复')
    run(obj, gmsg())
    evs = queued(obj)
    assert len(evs) == 1 and evs[0].listen and not evs[0].is_owner
    asyncio.run(obj._handle(evs[0]))
    prompt = obj.session.prompts[-1]
    assert LISTEN_NOTE.split(':')[0] in prompt and '按联调协议回复' in prompt
    assert GUEST_NOTE in prompt                         # 没开开发模式: 仍按访客
    assert fake_client.replies == []                    # 不回"没有权限", 也没有确认表情


def test_listen_skips_own_system_bot_and_duplicates(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    profiles.save(obj.storage, GROUP, listen=1)
    me = fake_client.auth.user_id
    fake_client.bots.add('300')
    run(obj,
        gmsg('我自己说的', msg_id='a', from_id=me),           # 本人(含数字人以本人身份发的): 不会自己触发自己
        gmsg('[系统消息] type=7', msg_id='b'),
        gmsg('机器人通知', msg_id='c', from_id='300'),
        gmsg('应用机器人', msg_id='d', from_id='301', from_type=2),
        gmsg('正常消息', msg_id='e'),
        gmsg('正常消息', msg_id='e'))                        # 重复推送
    assert [e.ref for e in queued(obj)] == ['e']
    profiles.save(obj.storage, GROUP, bots=1)
    run(obj, gmsg('机器人通知', msg_id='c2', from_id='300'))
    assert [e.ref for e in queued(obj)] == ['c2']


def test_listen_rate_limit_alerts_once(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    profiles.save(obj.storage, GROUP, listen=1, max_turns=2)
    run(obj, *[gmsg(f'消息{i}', msg_id=f'r{i}') for i in range(5)])
    assert [e.ref for e in queued(obj)] == ['r0', 'r1']
    alerts = [t for _, t in fake_client.sent if '旁听' in (t or '')]
    assert len(alerts) == 1


def test_listen_turned_off_while_queued(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    profiles.save(obj.storage, GROUP, listen=1)
    run(obj, gmsg())
    ev = queued(obj)[0]
    profiles.save(obj.storage, GROUP, listen=0)
    asyncio.run(obj._handle(ev))
    assert obj.session.prompts == []


def test_listen_survives_restart(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    profiles.save(obj.storage, GROUP, listen=1)
    run(obj, gmsg())
    queued(obj)
    obj._restore_inbox()
    evs = queued(obj)
    assert len(evs) == 1 and evs[0].listen


def test_dev_mode_lifts_guest_but_not_send_scope(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    profiles.save(obj.storage, GROUP, listen=1, dev=1, workdir=str(tmp_path))
    turns = []
    real_write = state.write_turn

    def spy(*a, **k):
        real_write(*a, **k)
        turns.append(state.read_turn())
    monkeypatch.setattr(state, 'write_turn', spy)
    run(obj, gmsg())
    asyncio.run(obj._handle(queued(obj)[0]))
    turn = turns[-1]
    assert turn['dev'] and not turn['is_owner'] and not state.is_guest(turn)
    assert state.send_denied(GROUP, turn) == ''
    assert state.send_denied('other-chat', turn)       # 开发模式也只能在本会话发消息
    prompt = obj.session.prompts[-1]
    assert DEV_GUEST_NOTE in prompt and GUEST_NOTE not in prompt and str(tmp_path) in prompt


def test_dev_mode_applies_to_normal_invocation(tmp_path, monkeypatch, fake_client):
    """没开旁听、只开开发模式: 群里 @ 本人的 /ai 照常触发, 这一轮按开发模式放开。"""
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    profiles.save(obj.storage, GROUP, dev=1)
    run(obj, gmsg('没 @ 不触发', msg_id='x1'), gmsg('/ai 查一下日志', msg_id='x2', at_me=True))
    evs = queued(obj)
    assert [e.ref for e in evs] == ['x2'] and not evs[0].listen
    asyncio.run(obj._handle(evs[0]))
    assert DEV_GUEST_NOTE in obj.session.prompts[-1]


def test_twin_cannot_change_profiles(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    with pytest.raises(PermissionError):
        profiles.save(obj.storage, GROUP, listen=1)
    with pytest.raises(PermissionError):
        profiles.remove(obj.storage, GROUP)


def test_cli_set_list_remove(tmp_path, monkeypatch, capsys):
    from larkx.plugins.comms.work.cli import cmd_group
    from larkx.storage import Storage
    monkeypatch.delenv('LARKX_ACTOR', raising=False)
    st = Storage()

    def ns(action, target=None, **kw):
        base = dict(action=action, target=target, listen=None, dev=None, bots=None, instruction=None, workdir=None,
                    max_turns=None, json=False)
        base.update(kw)
        return argparse.Namespace(**base)
    assert cmd_group(ns('set', '7704735334747082004', listen='on', instruction='按联调协议', workdir=str(tmp_path))) == 0
    row = profiles.get(st, '7704735334747082004')
    assert row.listen == 1 and row.dev == 0 and row.instruction == '按联调协议' and row.workdir == str(tmp_path)
    assert cmd_group(ns('set', '7704735334747082004', dev='on')) == 0
    row = profiles.get(st, '7704735334747082004')
    assert row.listen == 1 and row.dev == 1 and row.instruction == '按联调协议'   # 没传的字段不变
    assert cmd_group(ns('set', '7704735334747082004', workdir=str(tmp_path / 'nope'))) == 1
    capsys.readouterr()
    assert cmd_group(ns('list')) == 0
    assert '旁听开' in capsys.readouterr().out
    assert cmd_group(ns('remove', '7704735334747082004')) == 0
    assert profiles.get(st, '7704735334747082004') is None


def test_send_thread_flag(tmp_path, monkeypatch, fake_client):
    """larkx send --root X --thread: 普通群里回进以 X 为根的话题; 不带 --root 时报错。"""
    from larkx import cli
    from larkx.storage import Storage
    st = Storage(f'sqlite:///{tmp_path}/t.db')
    calls = []
    real = fake_client.send_message

    def spy(msg, chat_id, root_id=None, keep_unread=False, fallback=True, parent_id=None, in_thread=None):
        calls.append((root_id, in_thread, msg.get('kind')))
        return real(msg, chat_id, root_id=root_id, keep_unread=keep_unread)
    monkeypatch.setattr(fake_client, 'send_message', spy)
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    monkeypatch.setattr(cli, 'LarkClient', lambda *a, **k: fake_client)
    monkeypatch.setattr(cli, 'LarkAuth', lambda *a, **k: None)
    monkeypatch.delenv('LARKX_ACTOR', raising=False)

    def ns(**kw):
        return argparse.Namespace(**{'chat_id': 'c1', 'text': ['进度更新'], 'root': None, 'json': True, 'mention': [],
                                     'markdown': False, 'thread': False, **kw})
    assert cli.cmd_send(ns(root='om_root', thread=True)) == 0
    assert cli.cmd_send(ns(root='om_root', thread=True, markdown=True)) == 0
    assert cli.cmd_send(ns(root='om_root')) == 0
    assert calls == [('om_root', True, 'text'), ('om_root', True, 'markdown')]   # 不带 --thread 走原来的引用回复
    assert len(fake_client.sent) == 3
    assert cli.cmd_send(ns(thread=True)) == 1


def test_sender_whitelist_limits_listen_and_dev(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    profiles.save(obj.storage, GROUP, listen=1, dev=1, senders={'201': '李四'})
    run(obj, gmsg('张三说的', msg_id='w1'), gmsg('李四说的', msg_id='w2', from_id='201'))
    evs = queued(obj)
    assert [e.ref for e in evs] == ['w2']
    asyncio.run(obj._handle(evs[0]))
    assert DEV_GUEST_NOTE in obj.session.prompts[-1]
    # 不在会话白名单里的人 @ 本人 /ai: 走默认规则, 按访客处理, 不享有开发模式
    run(obj, gmsg('/ai 查日志', msg_id='w3', at_me=True))
    ev = queued(obj)[0]
    asyncio.run(obj._handle(ev))
    assert GUEST_NOTE in obj.session.prompts[-1] and DEV_GUEST_NOTE not in obj.session.prompts[-1]


def test_all_p2p_profile(tmp_path, monkeypatch, fake_client):
    """chat_id='p2p' 是所有私聊的配置; 单个私聊有自己的配置时用自己的。"""
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    profiles.save(obj.storage, 'p2p', listen=1, senders={'201': '李四'})

    def pmsg(chat, sender, mid):
        return {'msg_id': mid, 'chat_id': chat, 'chat_type': 1, 'from_id': sender, 'content': '在吗, 问个事',
                'create_time': int(time.time())}
    run(obj, pmsg('p-li', '201', 'a'), pmsg('p-zhang', OTHER, 'b'))
    assert [e.ref for e in queued(obj)] == ['a']
    profiles.save(obj.storage, 'p-zhang', listen=1)
    run(obj, pmsg('p-zhang', OTHER, 'c'))
    assert [e.ref for e in queued(obj)] == ['c']
    # 群不会用私聊的配置
    run(obj, gmsg(msg_id='d', from_id='201'))
    assert queued(obj) == []


def test_reply_starts_with_name_note():
    from larkx.daemon.triggers import build_prompt
    comms = WorkComms({})
    p = build_prompt(gmsg(), 'hi', sender_name='张三', comms=comms)
    assert ADDRESS_NOTE in p and '张三、李四：' in ADDRESS_NOTE
    # 本人在私聊里给数字人下指令: 回的就是本人, 不加名字; 本人在群里叫它时照样加
    own_p2p = dict(gmsg(), chat_type=1)
    assert ADDRESS_NOTE not in build_prompt(own_p2p, 'hi', is_owner=True, comms=comms)
    assert ADDRESS_NOTE in build_prompt(gmsg(), 'hi', is_owner=True, comms=comms)
    # 默认协议(上游): 不加
    assert ADDRESS_NOTE not in build_prompt(gmsg(), 'hi', sender_name='张三')


def test_old_db_gets_senders_column(tmp_path):
    import sqlite3
    from larkx.storage import Storage
    db = tmp_path / 'old.db'
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE chat_profiles (chat_id VARCHAR(32) PRIMARY KEY, chat_name VARCHAR(256), listen INTEGER, "
                "dev INTEGER, bots INTEGER, instruction TEXT, workdir TEXT, max_turns INTEGER, updated_at DATETIME)")
    con.execute("INSERT INTO chat_profiles (chat_id, listen) VALUES ('g1', 1)")
    con.commit(); con.close()
    st = Storage(f'sqlite:///{db}')
    row = profiles.get(st, 'g1')                     # 旧的 chat_profiles 改名成 chat_configs, 配置还在
    assert row.listen == 1 and sender_allowed(row, '999')
    profiles.save(st, 'g1', senders={'201': '李四'})
    assert not sender_allowed(profiles.get(st, 'g1'), '999')
    from sqlalchemy import inspect
    assert {c['name'] for c in inspect(st.engine).get_columns('chat_profiles')} >= {'group_id', 'suggested_at'}  # 上游的会话画像表


def test_cli_allow_disallow(tmp_path, monkeypatch, capsys):
    from larkx.plugins.comms.work.cli import cmd_group
    from larkx.storage import Storage
    monkeypatch.delenv('LARKX_ACTOR', raising=False)
    st = Storage()

    def ns(action, target=None, **kw):
        base = dict(action=action, target=target, listen=None, dev=None, bots=None, instruction=None, workdir=None,
                    max_turns=None, json=False, allow=None, disallow=None, allow_all=False)
        base.update(kw)
        return argparse.Namespace(**base)
    assert cmd_group(ns('set', '私聊', listen='on', allow=['2010001', '2020002'])) == 0
    assert set(profile_senders(profiles.get(st, 'p2p'))) == {'2010001', '2020002'}
    assert cmd_group(ns('set', 'p2p', disallow=['2010001'])) == 0
    assert set(profile_senders(profiles.get(st, 'p2p'))) == {'2020002'}
    assert cmd_group(ns('set', 'p2p', allow_all=True)) == 0
    assert profile_senders(profiles.get(st, 'p2p')) == {}


def test_listen_ignores_old_and_catch_up_messages(tmp_path, monkeypatch, fake_client):
    """打开旁听前的消息、daemon 停机期间补收到的旧消息, 都只入库不交给数字人。"""
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    profiles.save(obj.storage, GROUP, listen=1)
    since = profiles.get(obj.storage, GROUP).listen_since
    assert since and abs(since - time.time()) < 5
    run(obj, gmsg(msg_id='old', create_time=since - 30),           # 打开旁听之前发的
        gmsg(msg_id='stale', create_time=int(time.time()) - 900),   # 补收到的 15 分钟前的
        gmsg(msg_id='new'))
    assert [e.ref for e in queued(obj)] == ['new']
    # 关了再开: 从重新打开的时间算
    profiles.save(obj.storage, GROUP, listen=0)
    profiles.save(obj.storage, GROUP, listen=1)
    assert profiles.get(obj.storage, GROUP).listen_since >= since


def test_whitelisted_person_p2p_needs_no_prefix(tmp_path, monkeypatch, fake_client):
    """唤起白名单里的人: 他和本人的私聊默认旁听(不用 /ai); 群不会因此旁听; 显式配置优先。"""
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    obj.storage.whitelist_add('2010001', '李四')
    now = int(time.time())

    def pmsg(mid, sender='2010001', chat='p-li', **kw):
        return {'msg_id': mid, 'chat_id': chat, 'chat_type': 1, 'from_id': sender, 'content': '接口文档在哪',
                'create_time': now, **kw}
    run(obj, pmsg('a'), pmsg('b', sender='3030003', chat='p-other'),          # 不在白名单里的人: 不旁听
        pmsg('old', create_time=now - 3600),                                  # 加进白名单之前的: 不追溯
        gmsg('群里说的', msg_id='g', from_id='2010001'))                       # 群: 不因白名单旁听
    evs = queued(obj)
    assert [e.ref for e in evs] == ['a'] and evs[0].listen
    asyncio.run(obj._handle(evs[0]))
    assert LISTEN_NOTE.split(':')[0] in obj.session.prompts[-1]
    # 本人给这个私聊显式关了旁听: 以显式配置为准
    profiles.save(obj.storage, 'p-li', listen=0)
    run(obj, pmsg('c'))
    assert queued(obj) == []


def test_signature_not_doubled():
    from larkx.config import with_signature
    cfg = {'reply_signature': '🤖 lark-twin · 数字分身'}
    once = with_signature('张三：\n好的', cfg)
    assert once == '张三：\n好的\n\n—— 🤖 lark-twin · 数字分身'
    assert with_signature(once, cfg) == once
    assert with_signature('好的\n\n—— 🤖 lark-twin · 数字分身\n\n—— 🤖 lark-twin · 数字分身', cfg) == '好的\n\n—— 🤖 lark-twin · 数字分身'
    assert with_signature('好的', {'reply_signature': ''}) == '好的'


def test_listen_mention_of_owner_must_reply(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    profiles.save(obj.storage, GROUP, listen=1)
    run(obj, gmsg())
    run(obj, dict(gmsg(), msg_id='m-at', at_me=True))
    plain, at = queued(obj)
    asyncio.run(obj._handle(plain))
    assert LISTEN_MENTION_NOTE not in obj.session.prompts[-1]
    asyncio.run(obj._handle(at))
    assert LISTEN_MENTION_NOTE in obj.session.prompts[-1]


def test_listen_reply_to_owner_counts_as_finding_owner(tmp_path, monkeypatch, fake_client):
    """旁听的群里, 回复本人消息、在本人发起的话题里的消息没有 @ 本人, 也要告诉数字人对方在找本人。"""
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    profiles.save(obj.storage, GROUP, listen=1)
    me = fake_client.auth.user_id
    obj.storage.save_message(gmsg('@张三 同步一下进度', msg_id='o1', from_id=me), sender_name='我', direction='out')
    run(obj, gmsg('收到, 在看', msg_id='r1', root_id='o1', parent_id='o1', anchor='o1'),
        gmsg('别的话题', msg_id='r2', root_id='x1', parent_id='x1', anchor='x1'))
    reply, other = queued(obj)
    asyncio.run(obj._handle(reply))
    assert LISTEN_REPLY_NOTE in obj.session.prompts[-1] and LISTEN_MENTION_NOTE not in obj.session.prompts[-1]
    asyncio.run(obj._handle(other))
    assert LISTEN_REPLY_NOTE not in obj.session.prompts[-1]


def test_owner_inbox_only_written_by_agent(tmp_path, monkeypatch, fake_client):
    import argparse
    from larkx import cli, owner_inbox, state
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    profiles.save(obj.storage, GROUP, listen=1)
    run(obj, dict(gmsg('@本人 帮忙看下', msg_id='m2'), at_me=True), dict(gmsg('在吗', msg_id='m3', chat_id='p-1'), chat_type=1))
    assert owner_inbox.recent(0) == []                 # 不自动写, 只由数字人判断后 ping
    state.write_turn('message', GROUP, 'root-1', False, 'm9')
    assert cli.cmd_ping(argparse.Namespace(text=['需要本人决定', '是否借权限'], chat='', msg='', json=False)) == 0
    e = owner_inbox.recent(0)[-1]
    assert (e['kind'], e['chat_id'], e['msg_id'], e['root_id'], e['text']) == ('ping', GROUP, 'm9', 'root-1', '需要本人决定 是否借权限')


def test_watch_mode_judges_only(tmp_path, monkeypatch, fake_client):
    import argparse
    from larkx import cli, guest, owner_inbox, state
    from larkx.daemon.triggers import WATCH_NOTE
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    profiles.save(obj.storage, GROUP, listen=1, dev=1)
    assert not owner_inbox.watching()
    owner_inbox.start_watch()                       # 本进程活着 = 有 watch 在跑
    try:
        assert owner_inbox.watching()
        seen = {}

        async def ask(prompt, key=None, **kw):
            seen['turn'] = state.read_turn()
            seen['prompt'] = prompt
            return SimpleNamespace(is_error=False, subtype='success', text='', duration_s=0, tools=[])
        obj.session.ask = ask
        run(obj, dict(gmsg('@本人 帮忙看下', msg_id='w1'), at_me=True))
        ev, = queued(obj)
        asyncio.run(obj._handle(ev))
        turn = seen['turn']
        assert turn['watch'] and not turn['dev'] and WATCH_NOTE in seen['prompt']
        assert guest.cli_denied('send', argparse.Namespace(chat_id=GROUP), turn)
        assert guest.cli_denied('ping', argparse.Namespace(), turn) == ''
        # 没权限的人唤起: 不回"没有权限", 进收件箱
        obj.cfg['triggers']['owner_only'] = True
        run(obj, dict(gmsg('/ai 帮我跑一下', msg_id='w2', chat_id='p-x'), chat_type=1),
            dict(gmsg('[卡片] 周报', msg_id='w3', chat_id='p-y', msg_type=14), chat_type=1),
            dict(gmsg('机器人', msg_id='w4', chat_id='p-z', from_type=FROM_TYPE_BOT), chat_type=1))
        judged = queued(obj)
        assert fake_client.replies == []                     # 没权限的也不回"没有权限", 交给数字人判断
        assert [e.ref for e in judged] == ['w2', 'w3'] and all(e.judge for e in judged)
        asyncio.run(obj._handle(judged[0]))
        assert seen['turn']['watch'] and WATCH_NOTE in seen['prompt']
        assert owner_inbox.recent(0) == []                   # 写不写由数字人 ping 决定
    finally:
        owner_inbox.stop_watch()
    assert not owner_inbox.watching()
    seen.clear()
    asyncio.run(obj._handle(judged[1]))                      # 本人下线了: 只供判断的不再处理
    assert seen == {}


def test_watch_mode_flags_every_owner_related_message(tmp_path, monkeypatch, fake_client):
    """本人在线: @ 本人、私聊、回本人的消息或本人话题里的、白名单唤起的, 都告诉数字人和本人有关、要 ping;
    旁听群里找本人的不受旁听上限影响; 旁听到的无关闲聊不带这条说明; 都不回、不加表情。"""
    from larkx import owner_inbox
    from larkx.daemon.triggers import WATCH_NOTE, WATCH_OWNER_NOTE
    flag = WATCH_OWNER_NOTE.split('(')[0]
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    profiles.save(obj.storage, GROUP, listen=1, max_turns=1)
    obj.storage.whitelist_add('300', '白名单里的人')
    obj.storage.set_ack_reaction(True, 'all')
    me = fake_client.auth.user_id
    obj.storage.save_message(gmsg('@张三 同步一下进度', msg_id='o1', from_id=me), sender_name='我', direction='out')
    owner_inbox.start_watch()
    try:
        run(obj, gmsg('闲聊', msg_id='c1'),                                            # 旁听: 用掉上限
            gmsg('收到, 在看', msg_id='r1', root_id='o1', parent_id='o1', anchor='o1'),   # 回本人: 不受上限影响
            dict(gmsg('@本人 看下', msg_id='a1'), at_me=True),
            dict(gmsg('在吗', msg_id='p1', chat_id='p-1'), chat_type=1),
            dict(gmsg('/ai 帮我查下', msg_id='w1', chat_id='p-w', from_id='300'), chat_type=1))
        evs = {e.ref: e for e in queued(obj)}
        assert set(evs) == {'c1', 'r1', 'a1', 'p1', 'w1'}
        for ref, e in evs.items():
            asyncio.run(obj._handle(e))
            prompt = obj.session.prompts[-1]
            assert WATCH_NOTE in prompt and (flag in prompt) == (ref != 'c1'), ref
        assert fake_client.replies == [] and fake_client.sent == [] and fake_client.reactions == []
    finally:
        owner_inbox.stop_watch()
