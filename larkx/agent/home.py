import hashlib
import json
import subprocess
from pathlib import Path

from loguru import logger

from .. import plugins

TEMPLATES = Path(__file__).parent / 'templates'
# 历次发布的人设模板。数字人目录里的 CLAUDE.md 和其中某一版完全相同(用户没改过)时, 自动升级到当前模板
OLD_TEMPLATE_SHA256 = {'6927812ddbec998de7f1785c415889ed74adb266f83768a6a5647216dad11bca',
                       '3b3a64fe8303a2113a3e00f29583653354def072076e55e45a1d4d730d8beb88',
                       'de2a5b963785a90cbcd3e8e3ae5a0f627804a044352d36d5d8eed506a97337a6',
                       '3e39cab454487972a823ba89b2b290b2019ee01882bf6edab2d46bcc632519a0',
                       '23f7839130d2f6ada2e12fe75d3b57a5ac4688d313bccde0ee0b05526524f01c',
                       '37d1677e0b79a5b53a2efb1148a645e91c413dffec5dda17ae36352f13780a0d',
                       # 工作记忆和交互约定写死在模板里的那一版; 现在它们由插件写进 CLAUDE.md 末尾的插件区块
                       '21ede3015d733a55a12914fd78c5da8d32864bbe10686851270da59e36458c43'}
GITIGNORE = 'downloads/\nrepos/\n'
MEMORY_INDEX = '# 记忆索引\n\n每行一条: - [标题](文件名.md) — 一句话说明。具体内容写在对应文件里。\n'


def _git(home: Path, *args, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-C', str(home), *args], capture_output=True, text=True, check=check)


def ensure_agent_home(cfg: dict) -> Path:
    home = Path(cfg['agent_home'])
    home.mkdir(parents=True, exist_ok=True)
    Path(cfg['agent_claude_dir']).mkdir(parents=True, exist_ok=True)
    # Claude Code 会从工作目录一路向上找 .claude/skills, 直到 git 仓库根; 目录在家目录下时
    # 会把 ~/.claude/skills 里本机的所有 skill 都加载进来。让数字人目录自成一个仓库, 查找停在这里。
    # 这个仓库同时记录人设和记忆的每次变化, 只在本机, 不推送
    if not (home / '.git').exists():
        subprocess.run(['git', 'init', '-q', str(home)], check=True)
    _git(home, 'config', 'user.name', 'lark-twin')
    _git(home, 'config', 'user.email', 'lark-twin@localhost')
    gitignore = home / '.gitignore'
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE, encoding='utf-8')
    else:
        # 旧版目录的 .gitignore 缺新加的条目时补在末尾, 保留用户自己加的内容
        text = gitignore.read_text(encoding='utf-8')
        lines = {line.strip() for line in text.splitlines()}
        missing = [line for line in GITIGNORE.splitlines() if line not in lines]
        if missing:
            sep = '' if not text or text.endswith('\n') else '\n'
            gitignore.write_text(text + sep + ''.join(f'{line}\n' for line in missing), encoding='utf-8')
    (home / 'downloads').mkdir(exist_ok=True)
    # 数字人自己克隆的代码仓库放这里, 不进数字人目录的 git
    (home / 'repos').mkdir(exist_ok=True)
    # 人设和行为准则归用户所有, 只在首次创建; 用户没改过的旧版模板自动升级。
    # 启用的插件(记忆策略、交互协议)各自的说明写在末尾的插件区块里, 每次启动按当前插件重写, 区块外不动
    memory_plugin, comms_plugin = plugins.load(cfg)
    claude_md = home / 'CLAUDE.md'
    template = (TEMPLATES / 'CLAUDE.md').read_text(encoding='utf-8')
    if not claude_md.exists():
        text = template
    else:
        text = claude_md.read_text(encoding='utf-8')
        if hashlib.sha256(plugins.strip_persona_block(text).encode('utf-8')).hexdigest() in OLD_TEMPLATE_SHA256:
            text = template
            logger.info('CLAUDE.md 是未修改过的旧版模板, 已升级到当前模板')
    text = plugins.with_persona_block(text, plugins.persona_block(memory_plugin, comms_plugin))
    if not claude_md.exists() or claude_md.read_text(encoding='utf-8') != text:
        claude_md.write_text(text, encoding='utf-8')
    # Codex 读 AGENTS.md: 指向同一份人设
    agents_md = home / 'AGENTS.md'
    if not agents_md.exists() and not agents_md.is_symlink():
        agents_md.symlink_to('CLAUDE.md')
    memory = home / 'memory'
    memory.mkdir(exist_ok=True)
    if not (memory / 'MEMORY.md').exists():
        (memory / 'MEMORY.md').write_text(MEMORY_INDEX, encoding='utf-8')
    memory_plugin.ensure_home(home)   # 记忆策略自己的目录和规则
    # 会话记录默认 30 天清理, 常驻会话要长期保留。写在项目配置里而不是 --settings 参数,
    # 因为 ccd 这类启动器自己也会传 --settings, 两个参数会互相覆盖
    settings = home / '.claude' / 'settings.json'
    settings.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(settings.read_text(encoding='utf-8')) if settings.exists() else {}
    if data.get('cleanupPeriodDays') != 3650:
        data['cleanupPeriodDays'] = 3650
        settings.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    # skill 是 larkx CLI 的使用手册, 跟着代码版本走, 每次启动都同步; 启用的插件的命令说明接在后面
    skill_dir = home / '.claude' / 'skills' / 'larkx'
    skill_dir.mkdir(parents=True, exist_ok=True)
    base_skill = (TEMPLATES / 'skills' / 'larkx' / 'SKILL.md').read_text(encoding='utf-8')
    (skill_dir / 'SKILL.md').write_text(plugins.skill_text(base_skill, memory_plugin, comms_plugin), encoding='utf-8')
    commit_agent_home(home, '启动时同步数字人目录')
    return home


def commit_agent_home(home: Path, message: str) -> bool:
    """把人设、记忆等变化提交进数字人目录自己的 git 仓库; 没有变化时返回 False。
    打开结构化记忆(LARKX_MEMORY_STRUCTURED)时, 提交前按记忆文件的字段重建 memory/MEMORY.md。"""
    try:
        from ..config import load_config
        if load_config().get('memory_structured'):
            from ..memory import build_index
            build_index(home)
    except Exception as e:
        logger.warning(f'重建记忆索引失败: {e!r}')
    try:
        _git(home, 'add', '-A')
        if _git(home, 'diff', '--cached', '--quiet', check=False).returncode == 0:
            return False
        _git(home, 'commit', '-q', '-m', message)
        return True
    except subprocess.CalledProcessError as e:
        logger.warning(f'提交数字人目录失败: {e.stderr.strip()[:200]}')
        return False
