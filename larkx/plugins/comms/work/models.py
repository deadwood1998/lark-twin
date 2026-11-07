"""交互协议 work 自己的表: 会话配置(旁听 / 开发模式 / 会话白名单 / 专属指令)。"""
from datetime import datetime

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text, inspect, text

from ....storage import ADDED_COLUMNS, MIGRATIONS
from ....storage.models import Base


class ChatConfig(Base):
    """指定会话(多是群)的配置, 只有本人能改(larkx group), 数字人改不了。daemon 每条消息现查, 改了立即生效。
    - listen 旁听: 别人在这里发的每条消息都交给数字人, 不用 @、不用 /ai, 它自己判断要不要回;
    - dev 开发模式: 这里别人发起的一轮不按访客收窄, 可以查代码、跑命令、改代码(仍只能在本会话发消息);
    - instruction / workdir: 这个会话的专属指令、相关代码目录, 每一轮都附在提示词里。"""
    __tablename__ = 'chat_configs'
    # chat_id 是 'p2p' 时是"所有私聊"的配置: 单个私聊没有自己的配置时用它(一般配合白名单, 只自动处理这些人的私聊)
    chat_id = Column(String(32), primary_key=True)
    chat_name = Column(String(256), default='')
    listen = Column(Integer, default=0)
    dev = Column(Integer, default=0)
    bots = Column(Integer, default=0)          # 旁听时机器人发的也处理(默认不处理, 免得和别的机器人互相刷屏)
    instruction = Column(Text, default='')
    workdir = Column(Text, default='')
    max_turns = Column(Integer, default=20)    # 旁听每 10 分钟最多交给数字人几条, 超过就跳过并告警
    listen_since = Column(BigInteger, default=0)  # 旁听打开的时间(Unix 秒): 只处理这之后的消息, 打开时不追溯旧聊天
    senders = Column(Text, default='{}')       # 本会话的白名单 {user_id: 名字}: 非空时只旁听、只对这些人开开发模式; 空 = 所有人
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


# 后加的列: 老库里的表只建过一次, create_all 不会补列
ADDED_COLUMNS['chat_configs'] = [('senders', "TEXT DEFAULT '{}'"), ('listen_since', 'BIGINT DEFAULT 0')]


def _rename_old_table(engine):
    """这张表以前叫 chat_profiles, 和上游的会话画像表重名了: 旧库里那张(有 listen 列、没有 group_id 列)改名过来,
    会话配置不丢; 上游的 chat_profiles 随后由 create_all 新建。"""
    insp = inspect(engine)
    if not insp.has_table('chat_profiles') or insp.has_table('chat_configs'):
        return
    cols = {c['name'] for c in insp.get_columns('chat_profiles')}
    if 'listen' in cols and 'group_id' not in cols:
        with engine.begin() as conn:
            conn.execute(text('ALTER TABLE chat_profiles RENAME TO chat_configs'))


MIGRATIONS.append(_rename_old_table)
