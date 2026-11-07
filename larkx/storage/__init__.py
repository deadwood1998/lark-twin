import json
from datetime import datetime
from sqlalchemy import create_engine, desc, func, or_
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified
from ..config import inside_twin, load_config
from .models import (Action, AgentSession, Base, CatchupMark, Chat, Control, InboxItem, Message, Setting, Takeover, Task,
                     WhitelistEntry)

# 建表前要跑的迁移 fn(engine)(比如给旧表改名): 插件的 models 模块登记进来, 见 larkx/plugins.load_models
MIGRATIONS = []

# 触发时先回的表情: 固定用 OK(👌)
ACK_REACTION = 'OK'
# 先回表情的范围: owner 只对本人自己发的 /ai 消息, all 所有触发数字人的消息(包括白名单里的人)
ACK_SCOPES = ('owner', 'all')


def _json_default(o):
    # 内联缩略图等原始字节对入库无用,转成字符串会撑爆长度上限、截断出非法 JSON
    if isinstance(o, (bytes, bytearray)):
        return f'<{len(o)} bytes>'
    return str(o)


# create_all 只建新表不加列: 表已经存在、后来才加的列在这里补上(列名, 类型)。插件的表在自己的 models 模块里登记
ADDED_COLUMNS = {'chat_profiles': [('suggested_at', 'DATETIME')],
                 'chat_groups': [('feishu_label_id', "VARCHAR(32) DEFAULT ''")]}


def _add_columns(engine):
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    for table, cols in ADDED_COLUMNS.items():
        if not insp.has_table(table):
            continue
        have = {c['name'] for c in insp.get_columns(table)}
        for name, typ in cols:
            if name in have:
                continue
            try:
                with engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {typ}'))
            except Exception as e:  # daemon 和 CLI 同时第一次启动: 另一边刚加上了
                if 'duplicate column' not in str(e).lower():
                    raise


def mentioned(content_data: str, content: str, user_id: str, name: str = '') -> bool:
    """消息是不是 @ 了本人: 入库时记下的 @ 列表里有本人 id, 或者正文里有 @本人名字(英文名不分大小写, 和 LIKE 一致)。"""
    try:
        ids = (json.loads(content_data or '{}') or {}).get('_larkx_mention_ids') or []
    except (ValueError, AttributeError):
        ids = []
    return str(user_id) in {str(x) for x in ids} or bool(name and f'@{name}'.lower() in (content or '').lower())


class Storage:
    def __init__(self, url: str=None):
        cfg = load_config()
        self.url = url or cfg['storage_url']
        self.engine = create_engine(self.url, pool_pre_ping=True)
        from ..plugins import load_models
        load_models()   # 插件的表也登记进 Base.metadata, 一起建
        for migrate in MIGRATIONS:
            migrate(self.engine)
        Base.metadata.create_all(self.engine)
        _add_columns(self.engine)
        # 方法返回的对象在会话关闭后还要读属性, 提交时不让它们过期
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def save_message(self, msg: dict, sender_name='', chat_name='', direction='in', at_msg_time=False) -> bool:
        """at_msg_time: 回填历史时用, 会话的更新时间取消息时间(不早于原值), 不把回填的旧会话都顶到最前面。"""
        s = self.Session()
        try:
            from ..event_rules import mention_ids
            data = dict(msg.get('content_data') or {}) if isinstance(msg.get('content_data'), dict) else {}
            data['_larkx_mention_ids'] = mention_ids(msg)
            encoded = json.dumps(data, ensure_ascii=False, default=_json_default)
            if len(encoded) > 20000:
                encoded = json.dumps({'_larkx_mention_ids': data['_larkx_mention_ids'], '_truncated': True})
            fields = dict(msg_id=str(msg['msg_id']), chat_id=str(msg['chat_id']), chat_name=chat_name or '', chat_type=msg.get('chat_type', 0), scope=msg.get('scope', 'chat'), anchor=msg.get('anchor', '') or '', root_id=msg.get('root_id', '') or '', parent_id=msg.get('parent_id', '') or '', sender_id=str(msg.get('from_id') or ''), sender_name=sender_name or '', msg_type=msg.get('msg_type', 0), msg_type_name=msg.get('msg_type_name', ''), content=msg.get('content', '') or '', content_data=encoded, position=msg.get('position') or 0, create_time=msg.get('create_time') or 0, direction=direction)
            row = s.query(Message).filter_by(msg_id=fields['msg_id'], chat_id=fields['chat_id']).first()
            if row:
                # lark send 先落的记录没有 position;WS 回显到达时用回显补全,并视作首次入库
                if row.position or not fields['position']:
                    return False
                for k, v in fields.items():
                    setattr(row, k, v)
            else:
                row = Message(**fields)
                s.add(row)
            self._touch_chat(s, row, at_msg_time)
            s.commit()
            return True
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def update_edited(self, msg: dict) -> bool:
        """已入库的消息被编辑过(同一个 msg_id 推来了不同的内容): 换成新内容, 返回 True; 没有这条或内容没变返回 False。
        目前只有云文档评论会这样推(编辑评论推来的还是同一条消息), IM 消息的编辑不走这里。"""
        s = self.Session()
        try:
            from ..event_rules import mention_ids
            row = s.query(Message).filter_by(msg_id=str(msg['msg_id']), chat_id=str(msg['chat_id'])).first()
            content = msg.get('content', '') or ''
            if not row or row.content == content:
                return False
            data = dict(msg.get('content_data') or {}) if isinstance(msg.get('content_data'), dict) else {}
            data['_larkx_mention_ids'] = mention_ids(msg)
            row.content = content
            row.content_data = json.dumps(data, ensure_ascii=False, default=_json_default)
            if msg.get('anchor'):
                row.anchor, row.scope = msg['anchor'], msg.get('scope', row.scope)
            self._touch_chat(s, row)
            s.commit()
            return True
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def _touch_chat(self, s, row: Message, at_msg_time=False):
        chat = s.query(Chat).filter_by(chat_id=row.chat_id).first()
        msg_time = datetime.fromtimestamp(row.create_time) if at_msg_time and row.create_time else None
        if not chat:
            # 回填时新会话的时间直接取消息时间: 后面的查询会先把这行写进库, 默认值(当前时间)就改不回来了
            chat = Chat(chat_id=row.chat_id, chat_type=row.chat_type, **({'updated_at': msg_time} if msg_time else {}))
            s.add(chat)
        if row.chat_name and (not chat.name):
            chat.name = row.chat_name
        if row.position and not s.get(CatchupMark, row.chat_id):
            # 会话第一次有带 position 的消息入库: 补收从这里核对起; 升级前就有的会话从当时的 last_position 起, 以前缺的不回填
            s.add(CatchupMark(chat_id=row.chat_id, position=chat.last_position or row.position))
        if row.position and row.position > (chat.last_position or 0):
            chat.last_position = row.position
        if msg_time:
            chat.updated_at = max(chat.updated_at, msg_time) if chat.updated_at else msg_time
            flag_modified(chat, 'updated_at')  # 值没变也要显式写上, 否则改了别的列时 onupdate 会填成当前时间
        else:
            chat.updated_at = datetime.now()

    def update_chat_name(self, chat_id: str, name: str, chat_type: int=0):
        s = self.Session()
        try:
            chat = s.query(Chat).filter_by(chat_id=str(chat_id)).first()
            if not chat:
                chat = Chat(chat_id=str(chat_id), chat_type=chat_type)
                s.add(chat)
            chat.name = name
            s.commit()
        finally:
            s.close()

    def list_chats(self, limit=50):
        s = self.Session()
        try:
            return s.query(Chat).order_by(desc(Chat.updated_at)).limit(limit).all()
        finally:
            s.close()

    def get_messages(self, chat_id: str, anchor: str=None, limit=50, before_ts: int=None, since_ts: int=None):
        s = self.Session()
        try:
            q = s.query(Message).filter(Message.chat_id == str(chat_id))
            if anchor is not None:
                q = q.filter(Message.anchor == anchor)
            if before_ts:
                q = q.filter(Message.create_time < before_ts)
            if since_ts:
                q = q.filter(Message.create_time >= since_ts)
            rows = q.order_by(desc(Message.create_time)).limit(limit).all()
            rows.reverse()
            return rows
        finally:
            s.close()

    def get_chat_name(self, chat_id: str) -> str:
        s = self.Session()
        try:
            chat = s.query(Chat).filter_by(chat_id=str(chat_id)).first()
            return chat.name if chat else ''
        finally:
            s.close()

    # ---- 清理 ----

    def prune_messages(self, before_ts: int, chat_ids, dry_run=False) -> int:
        """删掉这些会话里 create_time 早于 before_ts 的消息, 返回条数。
        会话列表(chats)和补收位置不动: 会话名还要用, 补收只往前推进, 删掉的旧消息不会被拉回来。"""
        ids = [str(c) for c in chat_ids]
        n = 0
        s = self.Session()
        try:
            for i in range(0, len(ids), 500):
                q = s.query(Message).filter(Message.create_time > 0, Message.create_time < int(before_ts),
                                            Message.chat_id.in_(ids[i:i + 500]))
                n += q.count() if dry_run else q.delete(synchronize_session=False)
            if not dry_run:
                s.commit()
            return n
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def vacuum(self):
        """SQLite 删除后文件不会变小, 整理一次把空间还给磁盘; 其他数据库什么都不做。"""
        if self.engine.dialect.name != 'sqlite':
            return
        with self.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
            conn.exec_driver_sql('VACUUM')

    def session_get(self, session_key: str):
        s = self.Session()
        try:
            return s.query(AgentSession).filter_by(session_key=session_key).first()
        finally:
            s.close()

    def session_upsert(self, session_key: str, chat_id: str, anchor: str, agent_session_id: str, started: bool):
        s = self.Session()
        try:
            row = s.query(AgentSession).filter_by(session_key=session_key).first()
            if not row:
                row = AgentSession(session_key=session_key, chat_id=chat_id, anchor=anchor or '')
                s.add(row)
            row.agent_session_id = agent_session_id
            row.started = 1 if started else 0
            s.commit()
        finally:
            s.close()

    def session_touch(self, session_key: str):
        s = self.Session()
        try:
            row = s.query(AgentSession).filter_by(session_key=session_key).first()
            if row:
                row.msg_count = (row.msg_count or 0) + 1
                row.last_active = datetime.now()
                s.commit()
        finally:
            s.close()

    def list_sessions(self, limit=50):
        s = self.Session()
        try:
            return s.query(AgentSession).order_by(desc(AgentSession.last_active)).limit(limit).all()
        finally:
            s.close()

    def log_action(self, **fields):
        s = self.Session()
        try:
            if 'content' in fields:
                fields['content'] = (fields['content'] or '')[:2000]
            s.add(Action(**fields))
            s.commit()
        finally:
            s.close()

    def list_actions(self, limit=50):
        s = self.Session()
        try:
            rows = s.query(Action).order_by(desc(Action.id)).limit(limit).all()
            rows.reverse()
            return rows
        finally:
            s.close()

    def add_task(self, **fields) -> int:
        s = self.Session()
        try:
            row = Task(**fields)
            s.add(row)
            s.commit()
            return row.id
        finally:
            s.close()

    def get_task(self, task_id: int):
        s = self.Session()
        try:
            return s.get(Task, task_id)
        finally:
            s.close()

    def list_tasks(self, include_finished=False, limit=100):
        s = self.Session()
        try:
            q = s.query(Task)
            if not include_finished:
                q = q.filter(Task.status.in_(('pending', 'running')))
            return q.order_by(Task.due_at).limit(limit).all()
        finally:
            s.close()

    def due_tasks(self, now: float):
        s = self.Session()
        try:
            return s.query(Task).filter(Task.status == 'pending', Task.due_at <= now).order_by(Task.due_at).all()
        finally:
            s.close()

    def update_task(self, task_id: int, **fields):
        s = self.Session()
        try:
            row = s.get(Task, task_id)
            if row:
                for k, v in fields.items():
                    setattr(row, k, v)
                s.commit()
        finally:
            s.close()

    def reset_running_tasks(self) -> int:
        """daemon 重启时, 上次没跑完的任务重新排队。"""
        s = self.Session()
        try:
            n = s.query(Task).filter(Task.status == 'running').update({'status': 'pending'})
            s.commit()
            return n
        finally:
            s.close()

    def count_actions(self, actor: str, action, since: datetime) -> int:
        """action 可以是一个动作名, 也可以是几个(如 ('send', 'forward'), 发消息和转发合起来算)。"""
        actions = (action,) if isinstance(action, str) else tuple(action)
        s = self.Session()
        try:
            return s.query(Action).filter(Action.actor == actor, Action.action.in_(actions), Action.ok == 1,
                                          Action.created_at >= since).count()
        finally:
            s.close()

    def find_action(self, actor: str, action, msg_id: str):
        """action 同 count_actions。"""
        actions = (action,) if isinstance(action, str) else tuple(action)
        s = self.Session()
        try:
            return s.query(Action).filter(Action.actor == actor, Action.action.in_(actions), Action.msg_id == str(msg_id),
                                          Action.ok == 1).first()
        finally:
            s.close()

    def recalled_ids(self, msg_ids) -> set:
        """这些消息里已经成功撤回的。"""
        ids = [str(m) for m in msg_ids if m]
        if not ids:
            return set()
        s = self.Session()
        try:
            rows = s.query(Action.msg_id).filter(Action.action == 'recall', Action.ok == 1, Action.msg_id.in_(ids)).all()
            return {r[0] for r in rows}
        finally:
            s.close()

    def catchup_marks(self, since: datetime, extra=()) -> dict:
        """补收要核对的会话 {chat_id: 核对到的 position}: since 之后有动静的全部会话, 加上 extra 里有核对位置的会话。
        还没有核对位置的会话(升级前就有的)从当前 last_position 起算并记下, 以前缺的不回填; 一条消息都没有的会话不核对。"""
        s = self.Session()
        try:
            marks = {m.chat_id: m.position or 0 for m in s.query(CatchupMark).all()}
            out = {cid: marks[cid] for cid in map(str, extra) if cid in marks}
            for c in s.query(Chat).filter(Chat.updated_at >= since).all():
                if c.chat_id not in marks and c.last_position:
                    s.add(CatchupMark(chat_id=c.chat_id, position=c.last_position))
                    marks[c.chat_id] = c.last_position
                if c.chat_id in marks:
                    out[c.chat_id] = marks[c.chat_id]
            s.commit()
            return out
        finally:
            s.close()

    def catchup_advance(self, chat_id: str, position: int):
        """补收把核对位置之后到 position 的消息都拉过、入过库了: 核对位置推进到 position(只进不退)。"""
        s = self.Session()
        try:
            row = s.get(CatchupMark, str(chat_id))
            if not row:
                row = CatchupMark(chat_id=str(chat_id), position=0)
                s.add(row)
            if int(position) > (row.position or 0):
                row.position = int(position)
            s.commit()
        finally:
            s.close()

    def inbox_add(self, msg_id: str, chat_id: str, payload: dict) -> int:
        s = self.Session()
        try:
            row = InboxItem(msg_id=str(msg_id), chat_id=str(chat_id), payload=json.dumps(payload, ensure_ascii=False, default=_json_default))
            s.add(row)
            s.commit()
            return row.id
        finally:
            s.close()

    def inbox_seen(self, msg_id: str) -> bool:
        s = self.Session()
        try:
            return s.query(InboxItem).filter_by(msg_id=str(msg_id)).first() is not None
        finally:
            s.close()

    def inbox_set(self, item_id: int, status: str):
        s = self.Session()
        try:
            row = s.get(InboxItem, item_id)
            if row:
                row.status = status
                s.commit()
        finally:
            s.close()

    def inbox_unfinished(self):
        s = self.Session()
        try:
            return s.query(InboxItem).filter(InboxItem.status == 'pending').order_by(InboxItem.id).all()
        finally:
            s.close()

    def takeover_start(self, chat_id: str, until: int, goal: str = '', creator: str = ''):
        s = self.Session()
        try:
            row = s.get(Takeover, str(chat_id)) or Takeover(chat_id=str(chat_id))
            row.until, row.goal, row.creator, row.active, row.created_at = int(until), goal or '', creator, 1, datetime.now()
            s.merge(row)
            s.commit()
        finally:
            s.close()

    def takeover_get(self, chat_id: str, now: float):
        s = self.Session()
        try:
            row = s.get(Takeover, str(chat_id))
            return row if row and row.active and row.until > now else None
        finally:
            s.close()

    def takeover_list(self):
        s = self.Session()
        try:
            return s.query(Takeover).filter(Takeover.active == 1).order_by(Takeover.until).all()  # 不含正在收尾的
        finally:
            s.close()

    def takeover_expire(self, chat_id: str, now: float):
        """本人提前结束: 立即停止接管(active=2), 由 daemon 在下一次调度时收尾。"""
        s = self.Session()
        try:
            row = s.get(Takeover, str(chat_id))
            if row and row.active == 1:
                row.until, row.active = int(now), 2
                s.commit()
                return True
            return False
        finally:
            s.close()

    def takeover_finish_due(self, now: float):
        """取出已到期或被本人结束的接管并标记为结束, 每条只会被取出一次。返回 [(接管, 是否本人结束)]。"""
        s = self.Session()
        try:
            rows = s.query(Takeover).filter(Takeover.active.in_((1, 2)), Takeover.until <= now).all()
            out = [(r, r.active == 2) for r in rows]
            for r in rows:
                r.active = 0
            s.commit()
            return out
        finally:
            s.close()

    def owner_name(self, user_id: str) -> str:
        s = self.Session()
        try:
            row = (s.query(Message.sender_name, func.count()).filter(Message.sender_id == str(user_id),
                   Message.sender_name.notin_(('', '我'))).group_by(Message.sender_name).order_by(desc(func.count())).first())
            return row[0] if row else ''
        finally:
            s.close()

    @staticmethod
    def _own_messages(s, user_id: str):
        """本人亲自发的消息。数字人以本人身份发的、daemon 自动回的(审计里不是 human 发的)不算,
        不然数字人回过话的话题都会变成"找本人"。"""
        auto = s.query(Action.msg_id).filter(Action.actor != 'human', Action.msg_id != '')
        return s.query(Message).filter(Message.sender_id == str(user_id), Message.msg_id.notin_(auto))

    def replies_to_owner(self, msg: dict, user_id: str) -> bool:
        """这条消息在回本人: 直接回复本人发的消息, 或在本人发起、回过话的话题里。和 @ 本人一样算找本人。"""
        s = self.Session()
        try:
            return self._replies_owner(s, msg, user_id)
        finally:
            s.close()

    def _replies_owner(self, s, msg, user_id: str) -> bool:
        """msg 是消息 dict 或 Message 行。"""
        get = msg.get if isinstance(msg, dict) else (lambda k: getattr(msg, k, None))
        parent, root = str(get('parent_id') or ''), str(get('root_id') or get('anchor') or '')
        if not parent and not root:
            return False
        cond = [Message.msg_id.in_([x for x in (parent, root) if x])]
        if root:
            cond += [Message.root_id == root, Message.anchor == root]
        own = self._own_messages(s, user_id).filter(Message.chat_id == str(get('chat_id')), or_(*cond))
        return own.first() is not None

    def mentions(self, user_id: str, name: str, since_ts: int, limit=200):
        """since_ts 之后找本人的消息: 别人发来的私聊, @ 到本人的群消息, 以及在回本人的消息(见 replies_to_owner);
        附带本人之后有没有在该会话回过话。"""
        s = self.Session()
        try:
            q = s.query(Message).filter(Message.create_time >= since_ts, Message.sender_id != str(user_id),
                                        Message.direction == 'in')
            # 群里的 @: 正文里的 @名字, 或者入库时记下的 @ 列表(_larkx_mention_ids)里有本人。
            # 先用 LIKE 粗筛, 再解析确认: content_data 里别处带着本人 id(比如合并转发的记录)不算
            own = self._own_messages(s, user_id)
            ids = own.with_entities(Message.msg_id)
            roots = own.filter(Message.root_id != '').with_entities(Message.root_id)
            anchors = own.filter(Message.anchor != '').with_entities(Message.anchor)
            # 在回本人: 回复了本人发的消息, 或者在本人发起、回过话的话题里
            replies = [Message.parent_id.in_(ids), Message.root_id.in_(ids), Message.root_id.in_(roots),
                       Message.root_id.in_(anchors)]
            cond = [Message.chat_type == 1, Message.content_data.like(f'%"{user_id}"%'), *replies]
            if name:
                cond.append(Message.content.like(f'%@{name}%'))
            rows = []
            for r in q.filter(or_(*cond)).order_by(Message.create_time).yield_per(500):  # 分批读, 不一次读进全部候选
                if r.chat_type == 1 or mentioned(r.content_data, r.content, user_id, name) or \
                        self._replies_owner(s, r, user_id):
                    rows.append(r)
                    if len(rows) >= limit:
                        break
            out = []
            for r in rows:
                replied = s.query(Message.id).filter(Message.chat_id == r.chat_id, Message.sender_id == str(user_id),
                                                     Message.create_time >= r.create_time).first() is not None
                kind = 'p2p' if r.chat_type == 1 else 'at' if name and f'@{name}' in (r.content or '') else 'reply'
                out.append({'msg_id': r.msg_id, 'chat_id': r.chat_id, 'chat_name': r.chat_name, 'chat_type': r.chat_type,
                            'sender_name': r.sender_name, 'sender_id': r.sender_id, 'create_time': r.create_time,
                            'content': r.content, 'kind': kind, 'replied': replied})
            return out
        finally:
            s.close()

    def control_add(self, action: str) -> int:
        s = self.Session()
        try:
            row = Control(action=action)
            s.add(row)
            s.commit()
            return row.id
        finally:
            s.close()

    def control_get(self, control_id: int):
        s = self.Session()
        try:
            return s.get(Control, control_id)
        finally:
            s.close()

    def control_pending(self):
        s = self.Session()
        try:
            return s.query(Control).filter(Control.status == 'pending').order_by(Control.id).all()
        finally:
            s.close()

    def control_done(self, control_id: int, result: str, status: str = 'done'):
        s = self.Session()
        try:
            row = s.get(Control, control_id)
            if row:
                row.status, row.result = status, result
                s.commit()
        finally:
            s.close()

    def list_inbox(self, limit=50):
        s = self.Session()
        try:
            rows = s.query(InboxItem).order_by(desc(InboxItem.id)).limit(limit).all()
            return rows
        finally:
            s.close()

    def actions_for_turns(self, refs):
        s = self.Session()
        try:
            return s.query(Action).filter(Action.turn_ref.in_([str(r) for r in refs])).order_by(Action.id).all() if refs else []
        finally:
            s.close()

    def count_actions_since(self, since: datetime, actor: str = 'agent', action: str = 'send') -> int:
        return self.count_actions(actor, action, since)

    # ---- 唤起白名单 ----
    # daemon 每条消息都现查, 改了立即生效。只有本人能改: 数字人的会话里调用会被拒绝(CLI 也先拦一道)

    @staticmethod
    def _whitelist_guard():
        if inside_twin():
            raise PermissionError('数字人不能修改唤起白名单, 只有本人能改')

    def whitelist_list(self):
        s = self.Session()
        try:
            return s.query(WhitelistEntry).order_by(WhitelistEntry.added_at, WhitelistEntry.user_id).all()
        finally:
            s.close()

    def whitelist_ids(self) -> set:
        s = self.Session()
        try:
            return {r[0] for r in s.query(WhitelistEntry.user_id).all()}
        finally:
            s.close()

    def whitelist_get(self, user_id: str):
        s = self.Session()
        try:
            return s.get(WhitelistEntry, str(user_id))
        finally:
            s.close()

    def whitelist_add(self, user_id: str, name: str = '', department: str = '', avatar_key: str = '',
                      added_by: str = '') -> bool:
        """加入白名单; 已经在里面时只刷新名字等快照, 返回 False。"""
        self._whitelist_guard()
        user_id = str(user_id or '').strip()
        if not user_id.isdigit():
            raise ValueError(f'user_id 不合法: {user_id!r}')
        s = self.Session()
        try:
            row = s.get(WhitelistEntry, user_id)
            created = row is None
            if created:
                row = WhitelistEntry(user_id=user_id, added_by=added_by, added_at=datetime.now())
                s.add(row)
            for key, value, size in (('name', name, 256), ('department', department, 512), ('avatar_key', avatar_key, 256)):
                if value or created:
                    setattr(row, key, (value or '')[:size])
            s.commit()
            return created
        finally:
            s.close()

    def whitelist_remove(self, user_id: str) -> bool:
        self._whitelist_guard()
        s = self.Session()
        try:
            n = s.query(WhitelistEntry).filter_by(user_id=str(user_id)).delete()
            s.commit()
            return n > 0
        finally:
            s.close()


    # ---- 网页上的设置 ----
    # 和白名单一样: daemon 每次用到时现查, 网页改了立即生效, 不用重启; 只有本人能改, 数字人的会话里调用会被拒绝

    def setting_get(self, key: str, default=None):
        s = self.Session()
        try:
            row = s.get(Setting, key)
        finally:
            s.close()
        if not row:
            return default
        try:
            return json.loads(row.value)
        except ValueError:
            return default

    def setting_set(self, key: str, value):
        if inside_twin():
            raise PermissionError('数字人不能修改设置, 只有本人能改')
        s = self.Session()
        try:
            row = s.get(Setting, key)
            if not row:
                row = Setting(key=key)
                s.add(row)
            row.value = json.dumps(value, ensure_ascii=False)
            s.commit()
        finally:
            s.close()

    def ack_reaction(self) -> dict:
        """触发时先回一个表情的设置: {'enabled': 开没开(默认关), 'scope': ACK_SCOPES 之一(默认 owner)}。"""
        v = self.setting_get('ack_reaction')
        v = v if isinstance(v, dict) else {}
        return {'enabled': v.get('enabled') is True, 'scope': v.get('scope') if v.get('scope') in ACK_SCOPES else 'owner'}

    def set_ack_reaction(self, enabled=None, scope=None) -> dict:
        """改其中一项或两项, 没给的不变; 返回改后的设置。"""
        cur = self.ack_reaction()
        if enabled is not None:
            if not isinstance(enabled, bool):
                raise ValueError('enabled 只能是 true 或 false')
            cur['enabled'] = enabled
        if scope is not None:
            if scope not in ACK_SCOPES:
                raise ValueError(f'scope 只能是 {" 或 ".join(ACK_SCOPES)}')
            cur['scope'] = scope
        self.setting_set('ack_reaction', cur)
        return cur
