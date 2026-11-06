"""larkx urgent: 协议层的加急请求和确认状态、CLI 的校验(只加急本人的消息、群里要指定对象)、数字人的越权规则、send --urgent。"""
import argparse
import json
import sys

import pytest

from larkx import cli, guest, state
from larkx.protocol.client import LarkClient
from larkx.protocol.region import get_region
from larkx.storage import Storage

OWNER, BOT, PEER = '100', '300', '200'
SELF, P2P, GROUP = '699', '701', '702'


# ---- 协议层 ----

def _client(resp=None, chat=None):
    c = object.__new__(LarkClient)
    c.auth = type('A', (), {'user_id': OWNER, 'region': get_region('feishu')})()
    calls = []

    def api(cmd, data):
        calls.append((cmd, data))
        return resp(cmd, data) if callable(resp) else resp

    c.api = api
    c.get_chat_info = lambda chat_id: chat
    return c, calls


def test_put_urgent_request_and_response():
    c, calls = _client({'urgent': {'messageId': '9', 'type': 1, 'fromId': OWNER}, 'chatterIds': [PEER],
                        'urgentAckIds': {PEER: '55'}, 'invisibleChatterIds': []})
    res = c.put_urgent('9', [PEER], 'app', chat_id=P2P)
    assert calls == [('urgent.PutUrgentRequest', {
        'messageId': '9', 'chatterIds': [PEER], 'urgentType': 'APP', 'chatId': P2P, 'selectType': 'SELECT_SOME_CHATTER',
        'urgentChatterGroups': [{'chatterIds': [PEER], 'urgentType': 'APP'}]})]
    assert res == {'urgent_id': '9', 'type': 1, 'chatter_ids': [PEER], 'invisible_chatter_ids': [], 'ack_ids': {PEER: '55'}}
    c.put_urgent('9', [PEER], 'phone')
    assert calls[-1][1]['urgentType'] == 'PHONE' and 'chatId' not in calls[-1][1]


def test_put_urgent_request_builds_with_real_proto():
    from larkx.protocol.proto import gateway
    _, req_cls, _ = gateway.resolve_cmd('urgent.PutUrgentRequest')
    msg = gateway.request_from_dict(req_cls, {'messageId': '9', 'chatterIds': [PEER], 'urgentType': 'SMS',
                                              'urgentChatterGroups': [{'chatterIds': [PEER], 'urgentType': 'SMS'}],
                                              'selectType': 'SELECT_SOME_CHATTER', 'chatId': P2P})
    assert gateway.resolve_cmd('urgent.PutUrgentRequest')[0] == 30000
    assert msg.urgentType == 2 and list(msg.chatterIds) == [PEER] and msg.selectType == 1


def test_urgent_status_reads_urgent_acks():
    # 实测的响应格式: 结果在 urgentAcks 里, status 1 已确认, 2 未确认
    c, calls = _client({'urgentAcks': {'9': {'urgentAckList': [
        {'toId': PEER, 'type': 1, 'status': 1, 'sendTime': 10, 'confirmTime': 20},
        {'toId': BOT, 'type': 3, 'status': 2, 'sendTime': 10}]}}})
    assert c.urgent_status(['9', '8']) == {'9': [
        {'to': PEER, 'type': 'app', 'acked': True, 'send_time': 10, 'confirm_time': 20},
        {'to': BOT, 'type': 'phone', 'acked': False, 'send_time': 10, 'confirm_time': 0}]}
    assert calls == [('urgent.PullUrgentsAckStatusRequest', {'messageIds': ['9', '8']})]


@pytest.mark.parametrize('chat,peer', [
    ({'type': 1, 'key': f'{OWNER}:{PEER}'}, PEER),
    ({'type': 1, 'key': f'{PEER}:{OWNER}'}, PEER),
    ({'type': 2, 'key': ''}, ''),
    (None, ''),
])
def test_p2p_peer(chat, peer):
    c, _ = _client(chat=chat)
    assert c.p2p_peer('1') == peer


# ---- CLI ----

class UrgentClient:
    def __init__(self, fc):
        self.fc = fc

    def __getattr__(self, name):
        return getattr(self.fc, name)


@pytest.fixture
def env(monkeypatch, fake_client):
    st = Storage()
    fc = fake_client
    fc._owner_chat = SELF
    fc.urgents, fc.peers, fc.urgent_reject = [], {P2P: PEER}, set()
    fc.messages = {
        '1': {'msg_id': '1', 'chat_id': P2P, 'from_id': OWNER, 'content': '在吗', 'recalled': False},
        '2': {'msg_id': '2', 'chat_id': GROUP, 'from_id': OWNER, 'content': '看下', 'recalled': False},
        '3': {'msg_id': '3', 'chat_id': P2P, 'from_id': PEER, 'content': '你好', 'recalled': False},
        '4': {'msg_id': '4', 'chat_id': P2P, 'from_id': OWNER, 'content': 'x', 'recalled': True},
    }

    def put_urgent(msg_id, ids, urgent_type='app', chat_id=''):
        fc.urgents.append((msg_id, list(ids), urgent_type, chat_id))
        return {'urgent_id': msg_id, 'type': 1, 'chatter_ids': [i for i in ids if i not in fc.urgent_reject],
                'invisible_chatter_ids': [], 'ack_ids': {}}

    fc.put_urgent = put_urgent
    fc.p2p_peer = lambda chat_id: fc.peers.get(str(chat_id), '')
    fc.urgent_status = lambda ids: {'1': [{'to': PEER, 'type': 'app', 'acked': False, 'send_time': 1, 'confirm_time': 0}]}
    fc.people = [{'type': 'user', 'id': PEER, 'title': '张三', 'summary': '研发'},
                 {'type': 'user', 'id': '201', 'title': '李四', 'summary': 'A'},
                 {'type': 'user', 'id': '202', 'title': '李四', 'summary': 'B'}]
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    monkeypatch.setattr(cli, 'LarkClient', lambda *a, **k: fc)
    monkeypatch.setattr(cli, 'LarkAuth', lambda *a, **k: None)
    monkeypatch.delenv('LARKX_ACTOR', raising=False)
    return st, fc


def _args(*target, to=(), type='app'):
    return argparse.Namespace(target=list(target), to=list(to), type=type, json=True)


def _out(capsys):
    return json.loads(capsys.readouterr().out)


def test_p2p_defaults_to_peer_and_is_audited(env, capsys):
    st, fc = env
    assert cli.cmd_urgent(_args('1')) == 0
    assert fc.urgents == [('1', [PEER], 'app', P2P)]
    out = _out(capsys)
    assert out['ok'] and out['to'] == [PEER] and out['urgent'] == [PEER] and out['chat_id'] == P2P
    (row,) = st.list_actions()
    assert (row.actor, row.action, row.chat_id, row.msg_id, row.ok) == ('human', 'urgent', P2P, '1', 1)
    assert row.content == f'应用内加急 1 → {PEER}'


def test_group_needs_to_and_resolves_names(env, capsys):
    st, fc = env
    assert cli.cmd_urgent(_args('2')) == 1
    assert '--to' in _out(capsys)['error'] and fc.urgents == []
    assert cli.cmd_urgent(_args('2', to=('张三', '203', '张三'))) == 0
    assert fc.urgents == [('2', [PEER, '203'], 'app', GROUP)]
    capsys.readouterr()
    # 同名的人对应不到唯一的 user_id
    assert cli.cmd_urgent(_args('2', to=('李四',))) == 1
    err = _out(capsys)['error']
    assert '唯一' in err and '201' in err and '202' in err
    assert cli.cmd_urgent(_args('2', to=('王五',))) == 1
    assert '没有搜到' in _out(capsys)['error'] and len(fc.urgents) == 1


@pytest.mark.parametrize('target,why', [
    (('3',), '只能加急本人自己发出的消息'),
    (('4',), '找不到消息'),
    (('9',), '找不到消息'),
    (('om_123',), '用法'),
    (('1', '3'), '用法'),
])
def test_only_own_existing_messages(env, capsys, target, why):
    st, fc = env
    assert cli.cmd_urgent(_args(*target)) == 1
    assert why in _out(capsys)['error'] and fc.urgents == [] and st.list_actions() == []


def test_cannot_urgent_self(env, capsys):
    _, fc = env
    assert cli.cmd_urgent(_args('2', to=(OWNER,))) == 1
    assert '本人自己' in _out(capsys)['error'] and fc.urgents == []


def test_partial_rejection_is_reported(env, capsys):
    st, fc = env
    fc.urgent_reject = {'203'}
    assert cli.cmd_urgent(_args('2', to=(PEER, '203'))) == 1
    out = _out(capsys)
    assert not out['ok'] and out['missed'] == ['203'] and '203' in out['error']
    assert st.list_actions()[0].ok == 0


def test_server_error_is_audited(env, capsys):
    st, fc = env

    def boom(*a, **k):
        raise RuntimeError('HTTP 400')
    fc.put_urgent = boom
    assert cli.cmd_urgent(_args('1')) == 1
    assert '加急失败: HTTP 400' in _out(capsys)['error']
    (row,) = st.list_actions()
    assert (row.action, row.ok) == ('urgent', 0)


def test_human_can_use_sms_and_phone(env, capsys):
    _, fc = env
    assert cli.cmd_urgent(_args('1', type='phone')) == 0
    assert fc.urgents[-1][2] == 'phone'


def test_status(env, capsys):
    _, fc = env
    assert cli.cmd_urgent(_args('status', '1', '2')) == 0
    out = _out(capsys)
    assert [(m['msg_id'], m['urgent']) for m in out['messages']] == [('1', True), ('2', False)]
    assert out['messages'][0]['acks'][0]['acked'] is False
    args = _args('status', '1')
    args.json = False
    assert cli.cmd_urgent(args) == 0
    assert capsys.readouterr().out == f'1 → {PEER}: 应用内加急, 未确认\n'
    assert cli.cmd_urgent(_args('status')) == 1


# ---- 数字人 ----

def turn(kind='message', chat=P2P, owner=False, ref='m1', **kw):
    return {'kind': kind, 'chat_id': chat, 'anchor': '', 'is_owner': owner, 'ref': ref, **kw}


@pytest.fixture
def agent(env, monkeypatch):
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    monkeypatch.setenv('LARKX_MIRROR', 'false')

    def use(t):
        monkeypatch.setattr(state, 'read_turn', lambda: t)
    return (*env, use)


@pytest.mark.parametrize('current,why', [
    (None, '当前没有正在处理的消息'),
    (turn(kind='system', chat=''), '维护任务'),
    (turn(kind='report', chat=SELF), '汇报'),
    (turn(), '需要本人发起'),                         # 访客, 哪怕就在当前会话
    (turn(kind='task'), '需要本人发起'),              # 别人安排的任务
    (dict(state.UNTRUSTED_TURN, is_owner=True), '需要本人发起'),
])
def test_agent_denied_unless_owner_turn(agent, capsys, current, why):
    st, fc, use = agent
    use(current)
    assert cli.cmd_urgent(_args('1')) == 1
    assert why in _out(capsys)['error'] and fc.urgents == []
    (row,) = st.list_actions()
    assert (row.actor, row.action, row.ok) == ('agent', 'urgent', 0) and why in row.error
    assert [c for c, _ in fc.sent] == [SELF] and '拦下了数字人' in fc.sent[0][1]


@pytest.mark.parametrize('current', [turn(owner=True, chat=GROUP), turn(kind='say', chat=SELF, owner=True),
                                     turn(kind='task', owner=True)])
def test_owner_turn_can_urgent_other_chats(agent, capsys, current):
    st, fc, use = agent
    use(current)
    assert cli.cmd_urgent(_args('1')) == 0
    assert fc.urgents == [('1', [PEER], 'app', P2P)]
    row = st.list_actions()[0]
    assert (row.actor, row.ok, row.turn_kind) == ('agent', 1, current['kind'])


@pytest.mark.parametrize('kind', ['sms', 'phone'])
def test_agent_never_uses_paid_types(agent, capsys, kind):
    _, fc, use = agent
    use(turn(owner=True))
    assert cli.cmd_urgent(_args('1', type=kind)) == 1
    assert '可能计费' in _out(capsys)['error'] and fc.urgents == []


def test_owner_turn_urgent_elsewhere_is_mirrored(agent, monkeypatch, capsys):
    _, fc, use = agent
    monkeypatch.setenv('LARKX_MIRROR', 'true')
    use(turn(owner=True, chat=GROUP))
    assert cli.cmd_urgent(_args('1')) == 0
    assert [c for c, _ in fc.sent] == [SELF] and '加急' in fc.sent[0][1]


def test_guest_cli_blocks_urgent():
    t = turn()
    assert 'larkx urgent' in guest.cli_denied('urgent', _args('1'), t)
    assert 'urgent' not in guest.GUEST_CLI_ALLOWED


# ---- send --urgent ----

def _send(chat, text='请看一下', urgent=True, urgent_to=(), urgent_type=None):
    return argparse.Namespace(chat_id=chat, text=[text], root=None, markdown=False, mention=[], json=True,
                              urgent=urgent, urgent_to=list(urgent_to), urgent_type=urgent_type)


def test_send_then_urgent_in_p2p(env, capsys):
    st, fc = env
    assert cli.cmd_send(_send(P2P)) == 0
    assert fc.sent == [(P2P, '请看一下')] and fc.urgents == [('m1', [PEER], 'app', P2P)]
    out = _out(capsys)
    assert out['ok'] and out['msg_id'] == 'm1' and out['urgent']['urgent'] == [PEER]
    assert [a.action for a in st.list_actions()] == ['send', 'urgent']


def test_send_urgent_in_group_checks_before_sending(env, capsys):
    st, fc = env
    assert cli.cmd_send(_send(GROUP)) == 1
    assert '--urgent-to' in _out(capsys)['error'] and fc.sent == [] and fc.urgents == []
    # --urgent-to / --urgent-type 本身就表示要加急
    assert cli.cmd_send(_send(GROUP, urgent=False, urgent_to=('张三',))) == 0
    assert fc.urgents == [('m1', [PEER], 'app', GROUP)]
    assert cli.cmd_send(_send(GROUP, urgent=False, urgent_to=(PEER,), urgent_type='sms')) == 0
    assert fc.urgents[-1][2] == 'sms'


def test_send_reports_failed_urgent_without_resending(env, capsys):
    _, fc = env
    fc.urgent_reject = {PEER}
    assert cli.cmd_send(_send(P2P)) == 1
    out = _out(capsys)
    assert not out['ok'] and out['msg_id'] == 'm1' and '消息已发出' in out['error']
    assert len(fc.sent) == 1


def test_send_without_urgent_is_unchanged(env, capsys):
    _, fc = env
    assert cli.cmd_send(_send(P2P, urgent=False)) == 0
    assert fc.urgents == [] and _out(capsys) == {'ok': True, 'msg_id': 'm1', 'chat_id': P2P}


def test_guest_send_urgent_is_denied_before_sending(agent, capsys):
    st, fc, use = agent
    use(turn())
    assert cli.cmd_send(_send(P2P)) == 1
    assert '需要本人发起' in _out(capsys)['error'] and fc.sent == [] and fc.urgents == []
    assert [(a.action, a.ok) for a in st.list_actions()] == [('urgent', 0)]


def test_main_parses_urgent(monkeypatch):
    got = []
    monkeypatch.setattr(cli, 'cmd_urgent', lambda args: got.append(args) or 0)
    monkeypatch.setattr(sys, 'argv', ['larkx', 'urgent', '1', '--to', PEER, '--to', '张三', '--type', 'sms', '--json'])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0
    assert (got[0].target, got[0].to, got[0].type, got[0].json) == (['1'], [PEER, '张三'], 'sms', True)
    monkeypatch.setattr(sys, 'argv', ['larkx', 'urgent', 'status', '1', '2'])
    with pytest.raises(SystemExit):
        cli.main()
    assert (got[1].target, got[1].type) == (['status', '1', '2'], 'app')
    monkeypatch.setattr(sys, 'argv', ['larkx', 'urgent', '1', '--type', 'fax'])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 2
