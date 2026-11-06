"""会话分类与关注度。

关注度决定分身怎么对待一个会话:
- 重点关注 focus: 每日回顾每条都读, 提炼进记忆;
- 简单关注 light: 只挑和本人有关的(@本人、提到本人负责的事或分类关键词、需要本人处理的);
- 只存不看 archive: 存进本地库能查, 不进回顾, 设了保留天数时按保留期清理。
关注度设在分类上, 单个会话可以覆盖; 私人分类的内容不进工作记忆。

分类以本人确认的为准。画像(profile)按信号给每个会话一个建议(分类名、关注度、原因), 确认前先按建议生效;
本人确认或手动设置过的会话, 画像只更新信号, 不改它的分类。信号: 本人发言、被 @、私聊对象是不是机器人、
群人数、群名、飞书置顶。飞书免打扰只记下来, 不参与降级: 人是因为群太多才免打扰, 分身恰恰该替人读这些群。
"""
import json
import time
from datetime import datetime

from sqlalchemy import case, func, or_

from .storage import mentioned
from .storage.models import Chat, ChatGroup, ChatProfile, Message

ATTENTION = {'focus': '重点关注', 'light': '简单关注', 'archive': '只存不看'}
ALIASES = {'重点': 'focus', '重点关注': 'focus', 'focus': 'focus',
           '简单': 'light', '简单关注': 'light', 'light': 'light',
           '只存': 'archive', '只存不看': 'archive', 'archive': 'archive'}
FOLLOW_GROUP = ('跟分类', 'group', 'auto', '自动')
PROFILE_DAYS = 30
BIG_GROUP = 300
# 建议分类用的名称关键词(不分大小写)
# 通知类机器人里需要本人处理的(审批、权限之类); 公司内部还有别的, 用 LARKX_BOT_ACTION_KEYWORDS 补充
BOT_ACTION = ('审批', 'bpm', '日历', '邮箱', '云文档', '权限', '安全', '数据治理', '纪要', 'okr', '任务')
BOT_ADMIN = ('餐厅', '工资', '报销', '人力', '访客', '物品', '办公用品', '健康', 'ehs', '廉洁', '道德', '公益', 'travel',
             '班车', '广播', '活动', '推送助手')
TEST_WORDS = ('测试', 'test')
ALERT_WORDS = ('报警', '告警', 'alarm', '监控', '值班', 'oncall')
NOTICE_WORDS = ('周知', '通知', '公告', '周会', '双周会')


def parse_attention(text: str) -> str:
    key = ALIASES.get((text or '').strip().lower()) or ALIASES.get((text or '').strip())
    if not key:
        raise ValueError(f'关注度只能是 重点关注 / 简单关注 / 只存不看(或 focus / light / archive), 不认识 {text!r}')
    return key


def action_words(cfg: dict) -> tuple:
    return tuple(w for w in str(cfg.get('bot_action_keywords') or '').split(',') if w.strip())


def own_chats(profiles: dict, cfg: dict = None, owner_id: str = '') -> set:
    """本人自己的私聊(给数字人下指令、收汇报的通道): 记下的本人私聊会话, 加上私聊对象是本人的。清理和界面都用这个判断。"""
    from . import notify
    from .config import load_config
    cfg = load_config() if cfg is None else cfg
    if not owner_id:
        from .config import owner_user_id
        owner_id = owner_user_id()
    own = set()
    for c in (cfg.get('notify_chat'), notify._load().get('owner_chat')):  # 优先认记下的本人私聊, 但必须是私聊
        p = profiles.get(str(c)) if c else None
        if c and (p is None or p.chat_type in (0, 1)):
            own.add(str(c))
    for cid, p in profiles.items():
        if p.chat_type == 1 and ((owner_id and str(p.peer_id or '') == str(owner_id)) or p.suggested_group == '本人'):
            own.add(cid)
    return own


def _has(name: str, words) -> bool:
    low = (name or '').lower()
    return any(w in low for w in words)


def suggest(p: ChatProfile, owner_id: str, action_words=()):
    """按信号给出 (建议分类名, 关注度, 原因)。拿不准的给简单关注, 只存不看只给确实没信息量的。"""
    name = p.name or ''
    if p.chat_type == 1:
        if p.peer_id and str(p.peer_id) == str(owner_id):
            return '本人', 'archive', '自己的私聊: 指令和通知通道, 只存不看但不清理'
        if p.peer_bot == 1:
            if _has(name, BOT_ACTION + tuple(action_words)):
                return '需要你处理的通知', 'light', '通知类机器人: 只挑需要你处理的'
            if _has(name, BOT_ADMIN):
                return '行政生活', 'archive', '行政生活类机器人'
            return '机器人推送', 'archive', '机器人推送'
        if p.owner_msgs >= 3 or p.feishu_pinned:
            return '同事私聊', 'focus', f'常聊的同事(30 天你发了 {p.owner_msgs} 条)' + ('、已置顶' if p.feishu_pinned else '')
        return '同事私聊', 'light', '私聊, 最近聊得少'
    engaged = p.owner_msgs >= 3 or p.at_me >= 2
    if _has(name, TEST_WORDS) and not p.owner_msgs:
        return '测试群', 'archive', '群名像测试群, 你没发过言'
    if engaged:
        return '工作群', 'focus', f'30 天你发了 {p.owner_msgs} 条、被 @ {p.at_me} 次'
    if p.feishu_pinned:
        return '工作群', 'focus' if p.owner_msgs else 'light', '你置顶了'
    if _has(name, ALERT_WORDS):
        return '告警', 'light', '告警群: 只挑和你有关的'
    if _has(name, NOTICE_WORDS):
        return '部门周知', 'light', '周知/会议类群: 只挑公告和 @你的'
    if (p.members or 0) >= BIG_GROUP and not p.owner_msgs:
        return '大群', 'archive', f'{p.members} 人的大群, 你没发过言'
    if not p.msgs:
        return '沉寂', 'archive', f'{PROFILE_DAYS} 天没有消息'
    return '工作群', 'light', '一般工作群'


class ChatStore:
    def __init__(self, storage, action_words=()):
        self.storage = storage
        self.Session = storage.Session
        self.action_words = tuple(w.strip().lower() for w in action_words if w.strip())

    # ---- 分类 ----

    def groups(self) -> list:
        s = self.Session()
        try:
            return s.query(ChatGroup).order_by(ChatGroup.position, ChatGroup.id).all()
        finally:
            s.close()

    def group(self, name_or_id):
        s = self.Session()
        try:
            if isinstance(name_or_id, int) or str(name_or_id).isdigit():
                row = s.get(ChatGroup, int(name_or_id))
                if row:
                    return row
            return s.query(ChatGroup).filter(ChatGroup.name == str(name_or_id).strip()).first()
        finally:
            s.close()

    def create_group(self, name: str, attention: str = 'light', keywords=(), private: bool = False) -> ChatGroup:
        name = (name or '').strip()
        if not name:
            raise ValueError('分类名不能为空')
        if self.group(name):
            raise ValueError(f'已经有分类「{name}」')
        s = self.Session()
        try:
            pos = (s.query(func.max(ChatGroup.position)).scalar() or 0) + 1
            row = ChatGroup(name=name, attention=attention, private=1 if private else 0,
                            keywords=json.dumps(list(keywords or []), ensure_ascii=False), position=pos)
            s.add(row)
            s.commit()
            return row
        finally:
            s.close()

    def update_group(self, group_id: int, **fields):
        s = self.Session()
        try:
            row = s.get(ChatGroup, group_id)
            if not row:
                raise ValueError(f'没有分类 #{group_id}')
            if 'name' in fields and fields['name'] != row.name and \
                    s.query(ChatGroup).filter(ChatGroup.name == fields['name']).first():
                raise ValueError(f'已经有分类「{fields["name"]}」')
            if 'keywords' in fields and not isinstance(fields['keywords'], str):
                fields['keywords'] = json.dumps(list(fields['keywords']), ensure_ascii=False)
            for k, v in fields.items():
                setattr(row, k, v)
            s.commit()
            return row
        finally:
            s.close()

    def delete_group(self, group_id: int) -> int:
        """删分类; 分类里的会话变成未分类(仍算本人确认过, 不会被画像重新归类), 返回会话数。"""
        s = self.Session()
        try:
            n = s.query(ChatProfile).filter(ChatProfile.group_id == group_id).update({'group_id': 0})
            s.query(ChatGroup).filter(ChatGroup.id == group_id).delete()
            s.commit()
            return n
        finally:
            s.close()

    # ---- 会话 ----

    def profile(self, chat_id: str):
        s = self.Session()
        try:
            return s.get(ChatProfile, str(chat_id))
        finally:
            s.close()

    def assign(self, chat_ids, group_id: int = None, attention=None, by: str = 'cli') -> int:
        """本人指定会话的分类和/或关注度(attention='' 表示跟分类); 之后画像不再改它的分类。"""
        s = self.Session()
        try:
            n = 0
            for cid in chat_ids:
                row = s.get(ChatProfile, str(cid)) or ChatProfile(chat_id=str(cid))
                if group_id is not None:
                    row.group_id = group_id
                if attention is not None:
                    row.attention = attention
                row.confirmed = 1
                s.merge(row)
                n += 1
            s.commit()
            return n
        finally:
            s.close()

    def find_chats(self, query: str) -> list:
        """按名字找会话: chat_id 原样; 名字完全一致优先, 否则包含。返回 [(chat_id, 名字)]。"""
        q = (query or '').strip()
        s = self.Session()
        try:
            names = {r.chat_id: r.name for r in s.query(Chat).all() if r.name}
            names.update({r.chat_id: r.name for r in s.query(ChatProfile).all() if r.name})
        finally:
            s.close()
        if q in names:
            return [(q, names[q])]
        if q.isdigit() and len(q) >= 15:
            return [(q, names.get(q, ''))]
        exact = [(c, n) for c, n in names.items() if n == q]
        return exact or [(c, n) for c, n in names.items() if q and q.lower() in n.lower()]

    def view(self, chat_ids=None) -> list:
        """会话一览: 名字、分类、生效的关注度和来源、信号。没有画像的会话按简单关注算。"""
        s = self.Session()
        try:
            groups = {g.id: g for g in s.query(ChatGroup).all()}
            chats = {c.chat_id: c for c in s.query(Chat).all()}
            profiles = {p.chat_id: p for p in s.query(ChatProfile).all()}
        finally:
            s.close()
        own = own_chats(profiles)
        ids = list(chat_ids) if chat_ids is not None else list(dict.fromkeys([*chats, *profiles]))
        rows = []
        for cid in ids:
            p, c = profiles.get(cid), chats.get(cid)
            g = groups.get(p.group_id) if p and p.group_id else None
            if p and p.attention:
                att, source = p.attention, '单独设置'
            elif g:
                att, source = g.attention, '跟分类'
            elif p and p.suggested_attention:
                att, source = p.suggested_attention, '建议'
            else:
                att, source = 'light', '默认'
            rows.append({
                'chat_id': cid, 'name': (p.name if p and p.name else '') or (c.name if c else ''),
                'chat_type': (p.chat_type if p and p.chat_type else (c.chat_type if c else 0)),
                'group': g.name if g else '', 'group_id': g.id if g else 0, 'private': bool(g and g.private),
                'keywords': json.loads(g.keywords or '[]') if g else [],
                'attention': att, 'attention_name': ATTENTION[att], 'attention_source': source,
                'confirmed': bool(p and p.confirmed),
                # 本人自己的私聊: 指令和汇报的通道, 清理时不管关注度都不删
                'kept': cid in own,
                'suggested_group': p.suggested_group if p else '', 'suggested_attention': p.suggested_attention if p else '',
                'reason': p.reason if p else '', 'members': p.members if p else 0,
                'feishu_muted': (p.feishu_muted == 1) if p else False, 'feishu_pinned': bool(p and p.feishu_pinned),
                'peer_bot': (p.peer_bot == 1) if p else False,
                'msgs': p.msgs if p else 0, 'owner_msgs': p.owner_msgs if p else 0, 'at_me': p.at_me if p else 0,
                'last_at': (p.last_at if p and p.last_at else 0),
                'updated_at': c.updated_at.strftime('%Y-%m-%d %H:%M') if c and c.updated_at else '',
            })
        return rows

    def attention_of(self) -> dict:
        """chat_id -> 生效的关注度(没有画像的不在里面, 按简单关注算)。"""
        return {r['chat_id']: r['attention'] for r in self.view()}

    def private_chats(self) -> set:
        return {r['chat_id'] for r in self.view() if r['private']}

    # ---- 画像 ----

    def stats(self, owner_id: str, owner_name: str, since_ts: int) -> dict:
        """本地消息库里每个会话 since_ts 之后的条数、本人发言数、被 @ 次数、最后一条的时间。"""
        s = self.Session()
        try:
            rows = (s.query(Message.chat_id, func.count(),
                            func.sum(case((Message.sender_id == str(owner_id), 1), else_=0)),
                            func.max(Message.create_time))
                    .filter(Message.create_time >= since_ts).group_by(Message.chat_id).all())
            out = {cid: {'msgs': n, 'owner_msgs': int(own or 0), 'at_me': 0, 'last_at': int(last or 0)}
                   for cid, n, own, last in rows}
            # 被 @: 先用 LIKE 粗筛, 再确认本人 id 真的在入库时记下的 @ 列表里(合并转发的记录里也有本人 id, 不算)
            rough = [Message.content_data.like(f'%"{owner_id}"%')]
            if owner_name:
                rough.append(Message.content.like(f'%@{owner_name}%'))
            for cid, data, content in (s.query(Message.chat_id, Message.content_data, Message.content)
                                       .filter(Message.create_time >= since_ts, Message.sender_id != str(owner_id),
                                               or_(*rough))):
                if cid in out and mentioned(data, content, owner_id, owner_name):
                    out[cid]['at_me'] += 1
            # 私聊对象: 出现最多的非本人发送者
            peers = (s.query(Message.chat_id, Message.sender_id, func.count().label('n'))
                     .filter(Message.chat_type == 1, Message.sender_id != str(owner_id), Message.sender_id != '')
                     .group_by(Message.chat_id, Message.sender_id).all())
            best = {}
            for cid, sid, n in peers:
                if n > best.get(cid, ('', 0))[1]:
                    best[cid] = (sid, n)
            for cid, (sid, _) in best.items():
                out.setdefault(cid, {'msgs': 0, 'owner_msgs': 0, 'at_me': 0, 'last_at': 0})['peer_id'] = sid
            return out
        finally:
            s.close()

    def save_profile(self, chat_id: str, owner_id: str, **signals) -> tuple:
        """写信号并重算建议; 返回 (是不是新会话, 建议有没有变)。确认过的会话不改分类。"""
        s = self.Session()
        try:
            row = s.get(ChatProfile, str(chat_id))
            new = row is None
            row = row or ChatProfile(chat_id=str(chat_id))
            for k, v in signals.items():
                if v is not None:
                    setattr(row, k, v)
            for k in ('members', 'msgs', 'owner_msgs', 'at_me', 'last_at', 'feishu_pinned'):
                if getattr(row, k) is None:
                    setattr(row, k, 0)
            for k in ('peer_bot', 'feishu_muted'):
                if getattr(row, k) is None:
                    setattr(row, k, -1)
            before = (row.suggested_group, row.suggested_attention)
            row.suggested_group, row.suggested_attention, row.reason = suggest(row, owner_id, self.action_words)
            row.profiled_at = datetime.now()
            if new or before[1] != row.suggested_attention or not row.suggested_at:
                row.suggested_at = row.profiled_at
            s.merge(row)
            s.commit()
            return new, before != (row.suggested_group, row.suggested_attention)
        finally:
            s.close()


def feishu_pins(client):
    """本人飞书侧边栏的置顶会话 id; 拿不全(还有更多)或请求失败时返回 None, 这时不改画像里的置顶信号。"""
    try:
        resp = client.api('feed.PullShortcutsRequest', {}) or {}
        if resp.get('hasMore'):  # 这个接口的 position 不是翻页偏移, 拿不全时用大 count 再要一次
            resp = client.api('feed.PullShortcutsRequest', {'count': 1000}) or {}
        if resp.get('hasMore'):
            return None
        return {str(x.get('id')) for x in resp.get('shortcuts') or [] if x.get('type', 1) == 1}
    except Exception:
        return None


def profile_all(client, storage, owner_id: str, now: float = None, action_words=()) -> dict:
    """刷新所有本地会话的画像: 本地消息统计 + 服务端的人数、免打扰、置顶、私聊对象是不是机器人。返回统计。"""
    now = now or time.time()
    st = ChatStore(storage, action_words)
    owner_name = storage.owner_name(owner_id)
    stats = st.stats(owner_id, owner_name, int(now - PROFILE_DAYS * 86400))
    s = storage.Session()
    try:
        local = {c.chat_id: c for c in s.query(Chat).all()}
        chat_ids = list(local)
        known = {p.chat_id: p for p in s.query(ChatProfile).all()}
    finally:
        s.close()
    pins = feishu_pins(client)
    info = {}
    for i in range(0, len(chat_ids), 20):
        try:
            resp = client.api('chats.PullChatsByIdsRequest', {'chatIds': chat_ids[i:i + 20]}) or {}
            info.update({str(k): v for k, v in (resp.get('chats') or {}).items()})
        except Exception:
            pass
    new, changed = [], 0
    for cid in chat_ids:
        ch, sig, old = info.get(cid) or {}, stats.get(cid, {}), known.get(cid)
        chat_type = ch.get('type') if isinstance(ch.get('type'), int) else (local[cid].chat_type or None)
        peer = sig.get('peer_id') or (old.peer_id if old else '')
        if chat_type == 1 and ch.get('key'):
            peer = next((x for x in str(ch['key']).split(':') if x and x != str(owner_id)), str(owner_id))
        peer_bot = old.peer_bot if old and old.peer_bot in (0, 1) else -1
        if chat_type == 1 and peer and peer_bot == -1 and peer != str(owner_id):
            try:
                bot = client.is_bot(peer)
                # LarkClient 查失败时按真人返回且不记住: 这时保持未知, 下次画像再查, 免得一次失败就记成真人
                cached = getattr(client, '_bots', None)
                peer_bot = (1 if bot else 0) if cached is None or str(peer) in cached else -1
            except Exception:
                pass
        remind = (ch.get('userSetting') or {}).get('isRemind', ch.get('isRemind'))
        name = ch.get('name') or (old.name if old else '') or storage.get_chat_name(cid)
        if not name and chat_type == 1 and peer:
            try:
                name = client.get_user_name(peer, cid) or ''
            except Exception:
                name = ''
        is_new, did_change = st.save_profile(
            cid, owner_id, name=name or None, chat_type=chat_type, members=ch.get('memberCount'), peer_id=peer or None,
            peer_bot=peer_bot, feishu_muted=None if remind is None else (0 if remind else 1),
            feishu_pinned=None if pins is None else (1 if cid in pins else 0), msgs=sig.get('msgs', 0), owner_msgs=sig.get('owner_msgs', 0),
            at_me=sig.get('at_me', 0), last_at=sig.get('last_at') or (old.last_at if old else 0))
        if is_new:
            new.append(cid)
        changed += did_change and not is_new
    return {'chats': len(chat_ids), 'new': new, 'changed': changed}


def activity(storage, owner_id: str, since_ts: int) -> list:
    """since_ts 之后有消息的会话, 带上关注度、本人发言、被 @ 次数, 按重点 > 简单、条数多的在前。"""
    st = ChatStore(storage)
    owner_name = storage.owner_name(owner_id)
    stats = st.stats(owner_id, owner_name, since_ts)
    rows = [dict(r, recent=stats[r['chat_id']]['msgs'], recent_owner=stats[r['chat_id']]['owner_msgs'],
                 recent_at_me=stats[r['chat_id']]['at_me'])
            for r in st.view(list(stats)) if stats[r['chat_id']]['msgs']]
    order = {'focus': 0, 'light': 1, 'archive': 2}
    return sorted(rows, key=lambda r: (order[r['attention']], -r['recent']))
