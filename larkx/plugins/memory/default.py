"""上游 lark-twin 的记忆: memory/MEMORY.md 索引 + 每天一次的回顾, 一轮里随手更新; codex 后端单一 thread。

全部是基类的默认行为, 这里只给个名字。"""
from .base import MemoryStrategy


class DefaultMemory(MemoryStrategy):
    name = 'default'
    description = '上游原样: memory/MEMORY.md + 每日回顾, codex 后端所有消息进同一个 thread'
