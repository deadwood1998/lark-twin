"""记忆策略。用 LARKX_MEMORY=<名字> 或 larkx --memory <名字> 选, 默认 default(上游原样)。

新增一个策略: 在这个目录下建子包, 继承 base.MemoryStrategy, 再登记到下面的 REGISTRY。见 docs/plugins.md。
"""
from .base import MemoryStrategy

REGISTRY = {
    'default': 'larkx.plugins.memory.default:DefaultMemory',
    'work': 'larkx.plugins.memory.work:WorkMemory',
}
