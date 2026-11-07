"""云文档评论: cmd 14004 推送解成消息(评论 ID 取自 feed 链接), 推送没带消息时按 feed id 拉; 触发规则和群一样(别人要 @本人),
打开 doc_anyone 后谁都能唤起, 但别人发起的只能问这篇文档(提示词 + CLI 拦截); daemon 的控制回执、没权限回复回在评论下,
不加表情; doc read / comment / reply 走 lark-cli, reply 的管控和 send 一样。"""
import argparse
import asyncio
import copy
import json
import subprocess
import time

import pytest

from larkx import cli, doc_comments, guest, state
from larkx.config import DEFAULTS
from larkx.daemon.triggers import build_prompt, denied_attempt, should_handle
from larkx.protocol.client import LarkClient, PushStats
from larkx.protocol.proto import decoders
from larkx.protocol.proto.gateway import _lark_all
from larkx.storage import Storage
from larkx.storage.models import Action
from test_reliability import _daemon
from test_triggers import rich

ME, OTHER = '100', '200'
FEED, MSG, COMMENT = '7703836582906874907', '7703913018656853269', '7703913398462926059'
CHAT = 'doc:' + FEED
DOC_URL = 'https://example.feishu.cn/docx/DocxTestToken0000000000000'


def feed_push(status=2, with_message=True, msg_type=2, text=' /ai 这个加一个来源', at=ME):
    """照实际收到的那条通知构造: feed 链接上带 preview_comment_id, 最新一条消息是 lastMessageId。"""
    p = _lark_all().docs.PushDocFeedRequest()
    p.status = status
    f = p.docFeed
    f.id, f.title, f.token, f.lastMessageId = FEED, '接口与协议设计', 'DocxTestToken0000000000000', MSG
    f.url = f'{DOC_URL}?preview_comment_id={COMMENT}'
    if with_message:
        fill_message(p.docMessages[MSG], msg_type, text, at)
    return p.SerializeToString()


def fill_message(m, msg_type=2, text=' /ai 这个加一个来源', at=ME):
    content, data = rich(('at', at, '@赵六'), ('text', text))
    m.id, m.type, m.fromId, m.docFeedId, m.createTime = MSG, msg_type, OTHER, FEED, int(time.time())
    m.content, m.isAtMe = content, at == ME
    for key in data['richText']['elementIds']:
        el = data['richText']['elements']['dictionary'][key]
        m.richText.elementIds.append(key)
        m.richText.elements.dictionary[key].tag = el['tag']
        m.richText.elements.dictionary[key].property = el['property']
    return m


def doc_msg(**kw):
    feed, messages = decoders.doc_feed_push(feed_push(**kw))
    [msg] = decoders.doc_feed_messages(feed, messages)
    return msg


def cfg(**triggers):
    c = copy.deepcopy(DEFAULTS)
    # 只有本人能唤起现在默认开; 这里测别人在评论里唤起, 默认关掉, 要测时显式传 owner_only=True
    c['triggers'].update({'prefix': '/ai', 'owner_only': False, **triggers})
    return c


def test_push_decoded_with_comment_id_from_url():
    msg = doc_msg()
    assert (msg['chat_id'], msg['chat_type'], msg['chat_name']) == (CHAT, decoders.DOC_CHAT_TYPE, '接口与协议设计')
    assert (msg['msg_id'], msg['anchor'], msg['from_id'], msg['at_me']) == (MSG, COMMENT, OTHER, True)
    assert msg['content'] == '@赵六 /ai 这个加一个来源' and msg['msg_type_name'] == 'DOC_COMMENT'
    assert msg['content_data']['doc'] == {'feed_id': FEED, 'token': 'DocxTestToken0000000000000', 'title': '接口与协议设计',
                                          'url': DOC_URL, 'comment_id': COMMENT}


def test_push_skips_states_and_non_message_types():
    assert decoders.doc_feed_push(feed_push(status=4)) == (None, {})      # READ: 本人在别的端看过了
    assert decoders.doc_feed_push(feed_push(status=3)) == (None, {})      # DELETE
    feed, messages = decoders.doc_feed_push(feed_push(msg_type=4))        # SOLVE: 解决评论只是状态变化
    assert decoders.doc_feed_messages(feed, messages) == []


def test_only_latest_message_gets_comment_id():
    """评论 ID 在 feed 链接上, 只对应 lastMessageId; 同一次推来的较早消息拿不到, anchor 留空。"""
    p = _lark_all().docs.PushDocFeedRequest()
    p.ParseFromString(feed_push())
    old = fill_message(p.docMessages['7703900000000000001'])
    old.id, old.createTime = '7703900000000000001', 1
    feed, messages = decoders.doc_feed_push(p.SerializeToString())
    first, last = decoders.doc_feed_messages(feed, messages)
    assert (first['msg_id'], first['anchor'], first['scope']) == ('7703900000000000001', '', 'chat')
    assert (last['msg_id'], last['anchor'], last['scope']) == (MSG, COMMENT, 'topic')


def test_client_pulls_feed_when_push_has_no_message():
    calls, got = [], []
    full = _lark_all().docs.PushDocFeedRequest()
    full.ParseFromString(feed_push())
    from protobuf_to_dict import protobuf_to_dict

    def api(cmd, data=None):
        calls.append((cmd, data))
        return {'docFeeds': {FEED: protobuf_to_dict(full.docFeed)}, 'docMessages': {MSG: protobuf_to_dict(full.docMessages[MSG])}}

    async def on_message(msg):
        got.append((msg['chat_id'], msg['msg_id'], msg['anchor']))
    c = LarkClient.__new__(LarkClient)
    c.api, c.push_stats = api, PushStats()
    asyncio.run(c._doc_feed(feed_push(with_message=False), on_message))
    assert calls == [('docs.PullDocFeedsByIdsRequest', {'docFeedIds': [FEED]})]
    assert got == [(CHAT, MSG, COMMENT)]
    calls.clear()
    asyncio.run(c._doc_feed(feed_push(), on_message))                     # 推送里带着就不再拉
    assert calls == [] and len(got) == 2


def test_triggers_like_group():
    c = cfg()
    assert should_handle(doc_msg(), c, ME)                                # 别人 @本人 /ai
    assert not should_handle(doc_msg(at='999'), c, ME)                    # @ 的是别人: 在叫别人的数字人
    own = dict(doc_msg(at='999'), from_id=ME, content='/ai 总结一下这条评论', at_me=False)
    assert should_handle(own, c, ME)                                      # 本人自己评论不用 @ 自己
    assert not should_handle(doc_msg(text=' 这个加一个来源'), c, ME)         # 没有 /ai 不触发
    assert not should_handle(doc_msg(), cfg(chat_types=['p2p', 'group']), ME)
    assert not should_handle(doc_msg(), cfg(owner_only=True), ME)
    assert denied_attempt(doc_msg(), cfg(owner_only=True), ME)
    assert not denied_attempt(doc_msg(at='999'), cfg(owner_only=True), ME)


def test_prompt_uses_doc_reply():
    msg = doc_msg()
    prompt = build_prompt(msg, '这个加一个来源', chat_name='接口与协议设计', sender_name='耿宇贺')
    assert 'type="doc"' in prompt and f'anchor="{COMMENT}"' in prompt
    assert f"larkx doc reply {CHAT} {COMMENT} - <<'EOF'" in prompt and '「接口与协议设计」' in prompt
    assert 'larkx send' not in prompt


@pytest.fixture
def replies(monkeypatch):
    sent = []

    def fake_reply(cfg, storage, chat_id, comment_id, text):
        sent.append((chat_id, comment_id, text))
        return f'r{len(sent)}'
    monkeypatch.setattr(doc_comments, 'reply', fake_reply)
    return sent


def test_daemon_enqueues_and_skips_without_comment_id(tmp_path, monkeypatch, fake_client, replies):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    obj.storage.set_ack_reaction(enabled=True, scope='all')
    asyncio.run(obj.on_message(doc_msg(), '接口与协议设计', '耿宇贺'))
    assert obj.inbox.qsize() == 1 and fake_client.reactions == []        # 文档通知加不了表情
    no_anchor = dict(doc_msg(), msg_id='m2', anchor='')
    asyncio.run(obj.on_message(no_anchor, '接口与协议设计', '耿宇贺'))
    assert obj.inbox.qsize() == 1 and replies == []


def test_daemon_control_and_deny_reply_under_comment(tmp_path, monkeypatch, fake_client, replies):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    own = dict(doc_msg(), from_id=ME, content='/ai /stop', at_me=False)

    async def stop():
        return False
    obj.session.interrupt = stop
    asyncio.run(obj.on_message(own, '接口与协议设计', '我'))
    assert replies == [(CHAT, COMMENT, '⏹ 当前没有在处理的任务')] and fake_client.sent == []
    asyncio.run(obj.on_message(dict(own, content='/ai /takeover 1h'), '接口与协议设计', '我'))
    assert fake_client.sent and '不能接管' in fake_client.sent[-1][1]   # 提示发到本人私聊
    obj.cfg['triggers']['owner_only'] = True
    asyncio.run(obj.on_message(doc_msg(), '接口与协议设计', '耿宇贺'))
    assert replies[-1][:2] == (CHAT, COMMENT) and '没有权限' in replies[-1][2]
    s = obj.storage.Session()
    try:
        [a] = s.query(Action).filter(Action.action == 'deny_reply').all()
    finally:
        s.close()
    assert (a.chat_id, a.msg_id, a.ok) == (CHAT, 'r2', 1)


def store_doc(st):
    msg = doc_msg()
    assert st.save_message(msg, sender_name='耿宇贺', chat_name=msg['chat_name'])


def test_reply_calls_lark_cli_with_doc_url(monkeypatch):
    st = Storage()
    store_doc(st)
    calls = []

    def run(argv, input=None, **kw):
        calls.append((argv, input))
        return subprocess.CompletedProcess(argv, 0, json.dumps({'ok': True, 'data': {'reply_id': '7703999'}}), '')
    monkeypatch.setattr(doc_comments.shutil, 'which', lambda exe: '/bin/' + exe)
    monkeypatch.setattr(doc_comments.subprocess, 'run', run)
    assert doc_comments.reply(dict(DEFAULTS), st, CHAT, COMMENT, '好的, 已加') == '7703999'
    [(argv, content)] = calls
    assert argv == ['lark-cli', 'drive', '+add-reply', '--url', DOC_URL, '--comment-id', COMMENT, '--content', '-',
                    '--as', 'user', '--json']
    assert json.loads(content) == [{'type': 'text', 'text': '好的, 已加'}]


def test_reply_errors(monkeypatch):
    st = Storage()
    monkeypatch.setattr(doc_comments.shutil, 'which', lambda exe: '/bin/' + exe)
    with pytest.raises(doc_comments.DocError, match='没收到过它的通知'):
        doc_comments.reply(dict(DEFAULTS), st, CHAT, COMMENT, 'x')
    store_doc(st)
    with pytest.raises(doc_comments.DocError, match='不是云文档会话'):
        doc_comments.reply(dict(DEFAULTS), st, 'c1', COMMENT, 'x')
    monkeypatch.setattr(doc_comments.subprocess, 'run', lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, json.dumps({'ok': False, 'error': {'message': 'comment is solved'}}), ''))
    with pytest.raises(doc_comments.DocError, match='comment is solved'):
        doc_comments.reply(dict(DEFAULTS), st, CHAT, COMMENT, 'x')
    monkeypatch.setattr(doc_comments.shutil, 'which', lambda exe: None)
    with pytest.raises(doc_comments.DocError, match='找不到 lark-cli'):
        doc_comments.reply(dict(DEFAULTS), st, CHAT, COMMENT, 'x')


@pytest.fixture
def agent_cli(monkeypatch, fake_client):
    st = Storage()
    store_doc(st)
    sent = []

    def fake_reply(cfg, storage, chat_id, comment_id, text):
        sent.append((chat_id, comment_id, text))
        return '7703999'
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    monkeypatch.setattr(cli, 'LarkClient', lambda *a, **k: fake_client)
    monkeypatch.setattr(cli, 'LarkAuth', lambda *a, **k: None)
    monkeypatch.setattr(doc_comments, 'reply', fake_reply)
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    monkeypatch.setenv('LARKX_MIRROR', 'false')
    monkeypatch.setenv('LARKX_TRIGGER_PREFIX', '/ai')
    monkeypatch.setenv('LARKX_REPLY_SIGNATURE', 'AI 代回')

    def use(turn, text):
        monkeypatch.setattr(state, 'read_turn', lambda: turn)
        return cli.cmd_doc(argparse.Namespace(action='reply', chat_id=CHAT, comment_id=COMMENT, text=[text], json=True))
    return st, sent, use


def doc_actions(st):
    s = st.Session()
    try:
        return [(a.ok, a.error) for a in s.query(Action).filter(Action.action == 'doc_reply').order_by(Action.id)]
    finally:
        s.close()


def test_cli_reply_guarded_like_send(agent_cli):
    st, sent, use = agent_cli
    guest_turn = {'kind': 'message', 'chat_id': CHAT, 'is_owner': False, 'ref': MSG}
    assert use(guest_turn, '好的, 已加') == 0
    assert sent == [(CHAT, COMMENT, '好的, 已加\n\n—— AI 代回')]
    assert [m.content for m in st.get_messages(CHAT) if m.direction == 'out'] == ['好的, 已加\n\n—— AI 代回']
    assert use(guest_turn, '/ai 再来') == 1                                  # 防自回环
    assert use(dict(guest_turn, chat_id='doc:1'), '别的文档') == 1            # 只能回触发这一轮的文档
    assert use(dict(guest_turn, kind='system'), '维护任务') == 1
    assert len(sent) == 1
    assert [ok for ok, _ in doc_actions(st)] == [1, 0, 0, 0]


def test_doc_anyone_opens_docs_only():
    """打开 doc_anyone: 文档评论里谁都能唤起, 不回"没权限"; 还是要 @ 本人和 /ai; IM 里照旧只有本人。"""
    c = cfg(owner_only=True, doc_anyone=True)
    assert should_handle(doc_msg(), c, ME) and not denied_attempt(doc_msg(), c, ME)
    assert not should_handle(doc_msg(at='999'), c, ME)
    assert not should_handle(doc_msg(text=' 这个加一个来源'), c, ME)
    im = {'msg_id': 'm1', 'chat_id': 'c1', 'from_id': OTHER, 'chat_type': 1, 'content': '/ai 在吗'}
    assert not should_handle(im, c, ME) and denied_attempt(im, c, ME)


def test_doc_guest_prompt_limits_to_document():
    msg = doc_msg()
    guest_prompt = build_prompt(msg, '这个字段是干嘛的', chat_name='接口与协议设计', sender_name='耿宇贺')
    assert f'larkx doc read {CHAT}' in guest_prompt and f'larkx doc comment {CHAT} {COMMENT}' in guest_prompt
    assert '只回答和这篇文档、这条评论有关的问题' in guest_prompt and '访客规则' not in guest_prompt
    owner_prompt = build_prompt(msg, '总结一下', chat_name='接口与协议设计', is_owner=True)
    assert '只回答和这篇文档' not in owner_prompt and f'larkx doc read {CHAT}' in owner_prompt


def doc_args(action, chat_id=CHAT, comment_id=COMMENT):
    return argparse.Namespace(action=action, chat_id=chat_id, comment_id=comment_id)


def test_doc_guest_cli_only_this_doc_and_comment():
    turn = {'kind': 'message', 'chat_id': CHAT, 'anchor': COMMENT, 'is_owner': False, 'ref': MSG}
    for action in ('read', 'comment', 'reply'):
        assert guest.cli_denied('doc', doc_args(action), turn) == ''
    assert '当前这篇文档' in guest.cli_denied('doc', doc_args('read', chat_id='doc:1'), turn)
    assert '那条评论' in guest.cli_denied('doc', doc_args('reply', comment_id='7703000000000000000'), turn)
    assert '那条评论' in guest.cli_denied('doc', doc_args('comment', comment_id='7703000000000000000'), turn)
    for cmd, args in (('send', argparse.Namespace(chat_id=CHAT)), ('messages', argparse.Namespace(chat_id=CHAT)),
                      ('task', argparse.Namespace()), ('chats', argparse.Namespace())):
        assert guest.cli_denied(cmd, args, turn)
    im_turn = dict(turn, chat_id='c1', anchor='')                          # IM 里的访客也不能去读文档
    assert guest.cli_denied('doc', doc_args('read'), im_turn)


def test_read_and_comment_via_lark_cli(monkeypatch):
    st = Storage()
    store_doc(st)
    calls = []
    outputs = {
        '+fetch': {'document': {'content': '# 分工\n' + 'x' * 10}},
        '+batch-query-comments': {'items': [{'comment_id': COMMENT, 'quote': 'repair_plan_id 是', 'is_solved': False,
                                              'reply_list': {'replies': [
                                                  {'reply_id': 'r1', 'user_id': 'ou_me', 'create_time': 1,
                                                   'content': {'elements': [
                                                       {'type': 'person', 'person': {'user_id': 'ou_geng'}},
                                                       {'type': 'text_run', 'text_run': {'text': ' 这里改成否吧'}}]}}]}}]},
    }

    def run(argv, input=None, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({'ok': True, 'data': outputs[argv[2]]}), '')
    monkeypatch.setattr(doc_comments.shutil, 'which', lambda exe: '/bin/' + exe)
    monkeypatch.setattr(doc_comments.subprocess, 'run', run)
    monkeypatch.setattr(doc_comments, 'READ_MAX_CHARS', 8)
    assert doc_comments.read(dict(DEFAULTS), st, CHAT) == '# 分工\nxxx\n\n(文档太长, 只显示前 8 字)'
    assert calls[-1] == ['lark-cli', 'docs', '+fetch', '--doc', DOC_URL, '--doc-format', 'markdown', '--as', 'user', '--json']
    thread = doc_comments.comment(dict(DEFAULTS), st, CHAT, COMMENT)
    assert thread == {'comment_id': COMMENT, 'quote': 'repair_plan_id 是', 'is_solved': False,
                      'replies': [{'reply_id': 'r1', 'from': 'ou_me', 'create_time': 1, 'text': '@ou_geng 这里改成否吧'}]}


def test_edited_comment_rechecked(fake_client):
    """先发 "/ai ..." 忘了 @本人, 再编辑补上: 推来的还是同一条消息, 要按新内容再判断一次; 内容没变的重复推送不再交给回调;
    已经交给数字人的那版由收件箱按 msg_id 去重, 编辑后不会再处理一次。"""
    from larkx.daemon.listener import Listener
    st, got = Storage(), []

    async def on_stored(msg, chat_name, sender_name):
        got.append((msg['content'], should_handle(msg, cfg(), ME)))
    lis = Listener(fake_client, st, cfg(), ME, on_stored=on_stored)
    first = dict(doc_msg(at='999'), content='/ai 介绍一下文档', at_me=False)
    first['content_data'] = {**first['content_data'], 'richText': {}}
    assert asyncio.run(lis.on_message(first)) is True
    assert asyncio.run(lis.on_message(dict(first))) is False             # 同样内容重复推来
    edited = doc_msg(text=' /ai 介绍一下文档')
    assert asyncio.run(lis.on_message(edited)) is True
    assert got == [('/ai 介绍一下文档', False), ('@赵六 /ai 介绍一下文档', True)]
    [row] = st.get_messages(CHAT)
    assert row.content == '@赵六 /ai 介绍一下文档'
