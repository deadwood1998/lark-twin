import time

import pytest


@pytest.fixture(autouse=True)
def _isolate_local_state(monkeypatch, tmp_path):
    """测试一律用临时库和临时状态目录, 不写进本机真实的 ~/.larkx。"""
    import os
    from larkx import daemon, notify, state
    # 本机 .env 里的配置(load_dotenv 会向上找到它)不能带进测试, 一律从代码默认值出发
    for key in [k for k in os.environ if k.startswith('LARKX_')]:
        monkeypatch.delenv(key)
    monkeypatch.setenv('LARKX_STORAGE_URL', f'sqlite:///{tmp_path}/default.db')
    monkeypatch.setattr(state, 'STATE_DIR', tmp_path / 'state')
    monkeypatch.setattr(state, 'TURN_PATH', tmp_path / 'state' / 'turn.json')
    monkeypatch.setattr(state, 'RELAY_DIR', tmp_path / 'state' / 'relay')
    monkeypatch.setattr(state, 'FORWARD_DIR', tmp_path / 'state' / 'forward')
    monkeypatch.setattr(notify, 'NOTIFY_STATE', tmp_path / 'state' / 'notify.json')
    monkeypatch.setattr(daemon, 'DAEMON_STATE', tmp_path / 'state' / 'daemon.json')
    # 本机真实的登录凭证也不能读进测试(里面的 user_id 因机器而异)
    from larkx import config
    from larkx.protocol import auth
    monkeypatch.setattr(config, 'CREDENTIALS_PATH', tmp_path / 'credentials.json')
    monkeypatch.setattr(auth, 'CREDENTIALS_PATH', tmp_path / 'credentials.json')
    monkeypatch.setattr(auth.LarkAuth.__init__, '__defaults__', (tmp_path / 'credentials.json',))
    monkeypatch.setitem(config._owner_cache, 'mtime', None)


class FakeClient:
    """记录发出的消息, 不访问网络。"""

    def __init__(self, user_id='100', owner_chat='self'):
        self.sent, self.api_calls = [], []
        self.replies = []     # send_message 带的回复目标
        self.bots = set()     # is_bot 认作机器人的 id
        self.keep_unread = []  # 每次发送带的 keep_unread
        self.auth = type('A', (), {'user_id': user_id})()
        self._owner_chat = owner_chat
        self.people = []      # search 返回的结果(和 LarkClient.search 的格式一样)
        self.user_names = {}  # get_user_name 能查到的人
        self.searches = []
        self.messages = {}    # get_messages 能查到的消息 {msg_id: 消息}
        self.forwards = []    # forward_message / merge_forward 的调用: (种类, 源消息 id, 目标会话)
        self.forward_error = {}  # 转发这条源消息时抛的异常 {msg_id: 异常}
        self.reactions = []   # add_reaction 的调用: (消息 id, 表情 key)
        self.reaction_error = None  # add_reaction 抛的异常
        self.api_responses = {}

    def send_msg(self, text, chat_id, root_id=None, keep_unread=False):
        self.sent.append((chat_id, text))
        self.keep_unread.append(keep_unread)
        return f'm{len(self.sent)}'

    def send_message(self, msg, chat_id, root_id=None, keep_unread=False, fallback=True, parent_id=None, in_thread=None):
        self.replies.append({'chat_id': chat_id, 'text': msg.get('text'), 'root_id': root_id, 'parent_id': parent_id})
        return self.send_msg(msg.get('text'), chat_id, root_id=root_id, keep_unread=keep_unread)

    def is_bot(self, user_id):
        return str(user_id) in self.bots

    def create_chat(self, user_id):
        return self._owner_chat

    def get_user_name(self, user_id, chat_id):
        return self.user_names.get(str(user_id))

    def search(self, query):
        self.searches.append(query)
        return [r for r in self.people if query in r['title'].replace('<h>', '').replace('</h>', '')]

    def get_group_name(self, chat_id):
        return None

    def api(self, type_or_cmd, data=None):
        self.api_calls.append((type_or_cmd, data))
        r = self.api_responses.get(type_or_cmd)   # 按请求类型预置的返回: dict 或 data -> dict
        if r is not None:
            return r(data) if callable(r) else r
        return {'message': 'ok'}

    def add_reaction(self, msg_id, reaction_type='OK'):
        self.reactions.append((str(msg_id), reaction_type))
        if self.reaction_error:
            raise self.reaction_error
        return 1

    def get_messages(self, message_ids):
        return {str(i): self.messages[str(i)] for i in message_ids if str(i) in self.messages}

    def forward_message(self, msg_id, chat_ids, mark=True):
        return self._forward('forward', [msg_id], chat_ids)

    def merge_forward(self, msg_ids, chat_ids, mark=True):
        return self._forward('merge', msg_ids, chat_ids)

    def _forward(self, kind, msg_ids, chat_ids):
        msg_ids, chat_ids = [str(m) for m in msg_ids], [str(c) for c in chat_ids]
        self.forwards.append((kind, msg_ids, chat_ids))
        if msg_ids[0] in self.forward_error:
            raise self.forward_error[msg_ids[0]]
        n = len(self.forwards)
        return {c: {'msg_id': f'f{n}-{c}', 'chat_id': c, 'chat_type': 1, 'from_id': self.auth.user_id, 'msg_type': 4,
                    'msg_type_name': 'TEXT', 'content': self.messages.get(msg_ids[0], {}).get('content', ''),
                    'position': 9, 'cid': f'cid{n}', 'create_time': int(time.time())} for c in chat_ids}


@pytest.fixture
def fake_client():
    return FakeClient()
