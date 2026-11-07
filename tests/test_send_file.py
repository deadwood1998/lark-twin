"""larkx send --file: 图片和文件消息、回复/话题透传、数字人发文件的越权拦截。都不访问网络。"""
import argparse
import os

import pytest

from conftest import FakeClient
from larkx import cli, guest, state
from larkx.protocol import media


class MediaClient(FakeClient):
    """多记下每条消息的种类和 in_thread。"""

    def __init__(self):
        super().__init__()
        self.msgs = []

    def send_message(self, msg, chat_id, root_id=None, keep_unread=False, fallback=True, parent_id=None, in_thread=None):
        self.msgs.append({'chat_id': chat_id, 'msg': msg, 'root_id': root_id, 'in_thread': in_thread})
        return super().send_message(msg, chat_id, root_id=root_id, keep_unread=keep_unread, fallback=fallback,
                                    parent_id=parent_id, in_thread=in_thread)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from larkx.storage import Storage
    st = Storage(f'sqlite:///{tmp_path}/t.db')
    client = MediaClient()
    uploads = []
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    monkeypatch.setattr(cli, 'LarkClient', lambda *a, **k: client)
    monkeypatch.setattr(cli, 'LarkAuth', lambda *a, **k: None)
    monkeypatch.setattr(media, 'upload_image', lambda auth, data, name, mime: uploads.append(('image', name, data)) or 'img_k')
    monkeypatch.setattr(media, 'upload_file', lambda auth, fh, size, name, mime, progress=None:
                        uploads.append(('file', name, fh.read(size))) or 'file_k')
    home = tmp_path / 'home'
    (home / '.ssh').mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('LARKX_AGENT_HOME', str(tmp_path / 'agent'))
    monkeypatch.setenv('LARKX_MIRROR', 'false')
    (tmp_path / 'agent' / 'downloads').mkdir(parents=True)
    return argparse.Namespace(st=st, client=client, uploads=uploads, home=home, tmp=tmp_path)


def _args(*files, text=None, root=None, thread=False):
    return argparse.Namespace(chat_id='c1', text=text or [], json=True, root=root, thread=thread, markdown=False,
                              mention=[], file=[str(f) for f in files])


def _write(path, data=b'x' * 10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _agent(monkeypatch, guest_turn=False):
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    turn = {'kind': 'message', 'chat_id': 'c1', 'is_owner': not guest_turn, 'ref': 'm1'}
    monkeypatch.setattr(state, 'read_turn', lambda: turn)


def test_image_and_file_messages(env, capsys):
    png, pdf = _write(env.tmp / 'a.PNG', b'png'), _write(env.tmp / '报告.pdf', b'pdf!')
    assert cli.cmd_send(_args(png, pdf)) == 0
    assert [u[:2] for u in env.uploads] == [('image', 'a.PNG'), ('file', '报告.pdf')]
    assert [m['msg'] for m in env.client.msgs] == [
        {'kind': 'image', 'key': 'img_k'},
        {'kind': 'file', 'key': 'file_k', 'name': '报告.pdf', 'size': 4, 'mime': 'application/pdf'}]
    assert '"msg_ids": [\n    "m1",\n    "m2"\n  ]' in capsys.readouterr().out
    assert sorted((a.action, a.content, a.ok) for a in env.st.list_actions()) == [
        ('send', '[图片] a.PNG', 1), ('send', '[文件] 报告.pdf', 1)]


def test_big_image_goes_as_file(env, monkeypatch):
    monkeypatch.setattr(cli, 'SEND_IMAGE_MAX', 3)
    assert cli.cmd_send(_args(_write(env.tmp / 'big.jpg', b'1234'))) == 0
    assert env.client.msgs[0]['msg']['kind'] == 'file'


def test_text_first_then_files_with_root_and_thread(env):
    f = _write(env.tmp / 'a.txt')
    assert cli.cmd_send(_args(f, text=['看', '附件'], root='r9', thread=True)) == 0
    assert [(m['msg']['kind'], m['root_id'], m['in_thread']) for m in env.client.msgs] == [
        ('text', 'r9', True), ('file', 'r9', True)]
    assert env.client.msgs[0]['msg']['text'] == '看 附件'
    assert cli.cmd_send(_args(f, root='r9')) == 0
    assert (env.client.msgs[-1]['root_id'], env.client.msgs[-1]['in_thread']) == ('r9', None)


def test_missing_file_and_empty_send(env):
    assert cli.cmd_send(_args(env.tmp / 'nope.png')) == 1
    assert cli.cmd_send(_args(text=['  '])) == 1
    assert env.client.msgs == [] and env.uploads == []


def test_owner_cli_is_unrestricted(env, monkeypatch):
    monkeypatch.delenv('LARKX_ACTOR', raising=False)
    key = _write(env.home / '.ssh' / 'id_rsa')
    assert cli.cmd_send(_args(key)) == 0
    assert env.uploads[0][1] == 'id_rsa'


@pytest.mark.parametrize('rel', ['.ssh/id_rsa', '.larkx/credentials.json', '.larkx/messages.db', 'proj/.env',
                                 'proj/.env.local', '.codex/auth.json', '.claude.json', '.claude/.credentials.json'])
def test_agent_cannot_send_secrets(env, monkeypatch, rel):
    _agent(monkeypatch)
    path = _write(env.home / rel)
    assert cli.cmd_send(_args(path, text=['给你'])) == 1
    assert env.uploads == []
    # 文字也没发出去; 只给本人私聊发了告警, 审计记一条失败
    assert env.client.msgs == [] and [c for c, _ in env.client.sent] == ['self'] and '拦下' in env.client.sent[0][1]
    assert [(a.actor, a.ok) for a in env.st.list_actions()] == [('agent', 0)]


def test_agent_symlink_to_secret_is_blocked(env, monkeypatch):
    _agent(monkeypatch)
    secret = _write(env.home / '.ssh' / 'id_ed25519')
    link = env.tmp / 'agent' / 'downloads' / 'innocent.png'
    os.symlink(secret, link)
    assert cli.cmd_send(_args(link)) == 1
    assert env.uploads == []


def test_agent_owner_turn_can_send_normal_files(env, monkeypatch):
    _agent(monkeypatch)
    assert cli.cmd_send(_args(_write(env.tmp / 'work' / 'a.pdf'))) == 0
    downloads = _write(env.home / '.larkx' / 'agent' / 'downloads' / 'b.png')
    assert cli.cmd_send(_args(downloads)) == 0
    assert [u[1] for u in env.uploads] == ['a.pdf', 'b.png']


def test_guest_turn_only_sends_agent_downloads(env, monkeypatch):
    _agent(monkeypatch, guest_turn=True)
    outside = _write(env.tmp / 'work' / 'a.pdf')
    assert cli.cmd_send(_args(outside)) == 1
    assert env.uploads == [] and '拦下' in env.client.sent[0][1]
    inside = _write(env.tmp / 'agent' / 'downloads' / 'b.pdf')
    assert cli.cmd_send(_args(inside)) == 0
    assert [u[1] for u in env.uploads] == ['b.pdf']
    # main() 里的访客检查也按同样的规则拦
    turn = state.read_turn()
    assert guest.cli_denied('send', _args(outside), turn)
    assert guest.cli_denied('send', _args(inside), turn) == ''


def test_upload_failure_reports_partial(env, monkeypatch, capsys):
    def boom(*a, **k):
        raise media.UploadError('上传图片失败: HTTP 500')
    monkeypatch.setattr(media, 'upload_image', boom)
    assert cli.cmd_send(_args(_write(env.tmp / 'a.png'), text=['hi'])) == 1
    out = capsys.readouterr().out
    assert '"ok": false' in out and '"m1"' in out
    assert sorted((a.content, a.ok) for a in env.st.list_actions()) == [('[图片] a.png', 0), ('hi', 1)]
