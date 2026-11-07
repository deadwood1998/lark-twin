"""工作记忆策略自己的表。登记在上游的 Base.metadata 上, Storage 初始化时一起建(见 larkx/plugins.load_models)。"""
from sqlalchemy import BigInteger, Column, Integer, String

from ....storage.models import Base


class ChatRemind(Base):
    """本人有没有给这个会话开免打扰(飞书会话信息的 isRemind)。工作记忆只消化提醒的会话; 每次消化前现查一次。"""
    __tablename__ = 'chat_reminds'
    chat_id = Column(String(64), primary_key=True)
    remind = Column(Integer, default=-1)       # 1 提醒, 0 免打扰, -1 查不到
    checked_at = Column(BigInteger, default=0)
