"""例行任务的调度和执行(见 larkx/jobs.py)。

daemon 的调度循环每 15 秒调一次 tick: 到期的任务先记运行记录再执行。
- local 任务在线程里跑本模块的 _job_<名字>, 不挡住调度循环;
- agent 任务排进收件箱作为一轮 system 维护任务, 这一轮结束时 daemon 回调 on_turn_done 记结果。
失败时给本人告警(同一任务 6 小时内只告一次), 详情在 larkx jobs log。
"""
import asyncio
import json
import threading
import time
from datetime import datetime

from loguru import logger

from .. import backfill, digest, groups, jobs, retention
from .prompts import BUILDERS
from .triggers import build_report_prompt, build_system_prompt

ALERT_COOLDOWN = 6 * 3600
BOOTSTRAP_BATCH = 8
BOOTSTRAP_MAX = 40  # 初始化记忆最多看这么多个会话(按消息多少或本人发言多少排)


def bootstrap_chats(storage, cfg: dict, owner_id: str) -> list:
    """初始化记忆要看的会话。打开会话分类时: 重点关注(私人分类除外), 30 天消息多的在前;
    没打开时: 回填期间本人发过言的会话, 本人发言多的在前。"""
    cs = groups.ChatStore(storage)
    if cfg.get('chat_groups'):
        rows = [r for r in cs.view() if r['attention'] == 'focus' and not r['private']]
        return [r['chat_id'] for r in sorted(rows, key=lambda r: -(r['msgs'] or 0))][:BOOTSTRAP_MAX]
    since = int(time.time() - int(cfg.get('backfill_days') or 30) * 86400)
    stats = cs.stats(owner_id, storage.owner_name(owner_id), since)
    rows = sorted((cid for cid, s in stats.items() if s['owner_msgs']), key=lambda cid: -stats[cid]['owner_msgs'])
    return rows[:BOOTSTRAP_MAX]


class Paused(Exception):
    """停服务时本地任务主动停下: 记为暂停, 不算失败、不告警。"""


class Maintenance:
    def __init__(self, daemon):
        self.d = daemon
        self.store = jobs.JobStore(daemon.storage)
        self._tasks = set()
        self.stopping = threading.Event()  # 停服务: 让线程里跑的回填在下一批之前停下

    def stop(self):
        self.stopping.set()

    def recover(self, legacy_state: dict = None):
        n = self.store.recover()
        if n:
            logger.info(f'{n} 个上次没跑完的例行任务记为中断, 稍后重试')
        # 升级前每日回顾记在 daemon.json 里: 今天已经回顾过的补一条记录, 免得同一天再回顾一次
        today = datetime.now().strftime('%Y-%m-%d')
        if (legacy_state or {}).get('last_review') == today and not self.store.runs('review', today):
            self.store.create('review', today, trigger='startup', status='ok', summary='升级前今天已经回顾过')

    async def tick(self, now: datetime = None):
        now = now or datetime.now()
        # 记忆策略接管的例行任务(比如每小时消化代替每日回顾)不按时跑
        replaced = self.d.memory.replaced_jobs()
        for job in jobs.JOBS:
            if job.name in replaced:
                continue
            if job.once:
                if job.when(self.d.cfg) is None:
                    continue
                key = self._once_slot(job)
                due = self.store.due(job, self.d.cfg, now, cur=(key, now)) if key else None
            else:
                due = self.store.due(job, self.d.cfg, now)
            if not due:
                continue
            if due[0] == 'skip':
                self.store.create(job.name, due[1], status='skipped', summary='错过计划时间, 这一次不跑')
                continue
            if job.kind == 'report' and self.reported(job.name, due[1]):
                # 之前某次(出错、中断、重启前)其实已经发到本人私聊: 直接记完成, 不再发一遍
                self.store.create(job.name, due[1], status='ok', summary='这一时段已经发过, 不再重发')
                continue
            await self.start(job, due[1], 'schedule', due[2])

    def reported(self, job: str, slot_key: str) -> bool:
        """这个时段之前的运行里, 有没有哪一次已经成功发到本人私聊。"""
        refs = [r.ref for r in self.store.runs(job, slot_key) if r.ref]
        owner = self.d.notifier.owner_chat()
        return bool(refs and owner) and any(a.action == 'send' and a.ok and a.chat_id == owner
                                            for a in self.d.storage.actions_for_turns(refs))

    async def start(self, job: jobs.Job, slot_key: str, trigger: str, attempt: int = 1):
        run = self.store.create(job.name, slot_key, trigger, attempt)
        logger.info(f'例行任务「{job.title}」开始({trigger}, #{run.id})')
        if job.kind == 'local':
            task = asyncio.create_task(self._run_local(job, run))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            from . import Event
            owner_chat = self.d.notifier.owner_chat()
            # 生成提示词会读飞书(置顶、标签、标记), 放到线程里, 不挡住事件循环
            text = await asyncio.to_thread(BUILDERS[job.name], self.d.cfg, self.d.storage, datetime.now(),
                                           str(self.d.auth.user_id), client=self.d.client, owner_chat=owner_chat)
            if job.kind == 'report':
                if not owner_chat:
                    self._failed(run, '找不到本人私聊, 发不了汇报')
                    return run
                ev = Event('report', build_report_prompt(job.name, text, owner_chat), chat_id=owner_chat, ref=run.ref,
                           run_id=run.id)
            else:
                ev = Event('system', build_system_prompt(text), chat_id=owner_chat, ref=run.ref, run_id=run.id)
            await self.d.inbox.put(ev)
        return run

    def _once_slot(self, job: jobs.Job):
        """一次性任务现在该不该跑, 该跑时返回时段名。"""
        if job.name == 'backfill':
            done = self.store.get_state('backfill').get('done')
            # 等启动后第一轮补收结束: 那一轮给最近的会话记好了核对位置, 回填只拉位置以下的。
            # 按天给时段: 某天重试用完, 第二天从断点接着回填, 不会永远停住
            if done or getattr(self.d, '_feed_since', None) is None:
                return None
            return 'once-' + datetime.now().strftime('%Y-%m-%d')
        if job.name == 'bootstrap':
            bf, bs = self.store.get_state('backfill'), self.store.get_state('bootstrap')
            if not bf.get('done') or bs.get('done'):
                return None
            if self.d.cfg.get('chat_groups'):
                prof = self.store.last('profile', status=('ok',))
                if not prof or not prof.finished_at or prof.finished_at.timestamp() < bf.get('finished_at', 0):
                    return None  # 回填后的画像还没跑完
            if 'chats' not in bs:
                bs['chats'] = bootstrap_chats(self.d.storage, self.d.cfg, str(self.d.auth.user_id))
                bs['next'] = 0
                self.store.put_state('bootstrap', bs)
            while bs['next'] * BOOTSTRAP_BATCH < len(bs['chats']) and self._batch_gave_up(f"batch-{bs['next'] + 1}"):
                # 这一批重试用完还没成功: 跳过它接着下一批, 免得初始化记忆永远停在这里
                logger.warning(f"初始化记忆第 {bs['next'] + 1} 批重试用完, 跳过")
                bs.setdefault('skipped', []).append(bs['next'] + 1)
                bs['next'] += 1
                self.store.put_state('bootstrap', bs)
            if bs['next'] * BOOTSTRAP_BATCH >= len(bs['chats']):
                last = -(-len(bs['chats']) // BOOTSTRAP_BATCH)
                # 最后一批被跳过了, 它带的"通读合并"那一步没做: 单独补一轮收尾
                if last in bs.get('skipped', []) and len(bs['skipped']) < last and not bs.get('final') \
                        and not self._batch_gave_up('final'):
                    return 'final'
                bs['done'] = True
                self.store.put_state('bootstrap', bs)
                return None
            return f"batch-{bs['next'] + 1}"
        if job.name == 'organize_init':
            if int(self.d.cfg.get('backfill_days') or 0) > 0 and jobs.get('bootstrap').when(self.d.cfg):
                # 初始化记忆之后, 了解了本人的工作再提分类方案
                return 'once' if self.store.get_state('bootstrap').get('done') else None
            return 'once' if self.store.last('profile', status=('ok',)) else None  # 没回填: 第一次画像之后
        return None

    def _batch_gave_up(self, key: str) -> bool:
        """这一批不会再跑了: 本人 /stop 打断(记为跳过), 或者重试用完还没成功。"""
        runs = [r for r in self.store.runs('bootstrap', key) if r.status != jobs.PAUSED]
        if any(r.status in jobs.ACTIVE or r.status == 'ok' for r in runs):
            return False
        return any(r.status == 'skipped' for r in runs) or len(runs) >= jobs.get('bootstrap').max_attempts

    async def request(self, name: str) -> str:
        """手动立即运行(网页"立即运行"、larkx jobs run)。"""
        job = jobs.get(name)
        if not job:
            return f'没有例行任务 {name}, 可用: {", ".join(j.name for j in jobs.JOBS)}'
        if job.when(self.d.cfg) is None:
            return f'「{job.title}」没有打开, 不能运行'
        run = await self.start(job, f'manual-{int(datetime.now().timestamp())}', 'manual')
        return f'已开始「{job.title}」#{run.id}'

    # ---- 结果 ----

    def started(self, run_id: int):
        self.store.start(run_id)

    def on_turn_done(self, ev, result, stopped_by_owner: bool = False):
        run = self.store.get(ev.run_id)
        if not run:
            return
        if stopped_by_owner:  # 本人 /stop: 不算失败, 不告警, 这个时段也不再自动重跑
            self.store._set(run.id, status='skipped', finished_at=datetime.now(), summary='本人停止了这一轮, 这一次不再自动重跑')
            return
        if run.job == 'briefing':
            return self._briefing_done(ev, run, result)
        if run.job in ('organize', 'organize_init') and result is not None and not result.is_error:
            return self._organize_done(run, result)
        if result is not None and not result.is_error:
            self.store.finish(run.id, result.text or '')
            if run.job == 'bootstrap' and run.slot.startswith('batch-'):
                bs = self.store.get_state('bootstrap')
                bs['next'] = max(bs.get('next', 0), int(run.slot[6:]))
                self.store.put_state('bootstrap', bs)
            elif run.job == 'bootstrap' and run.slot == 'final':
                bs = self.store.get_state('bootstrap')
                bs['final'] = True
                self.store.put_state('bootstrap', bs)
            return
        err = ('agent 会话中断, 结果未知' if result is None else f'{result.subtype}: {result.text or ""}')
        self._failed(run, err)

    def turn_error(self, ev, err: str):
        """这一轮抛了会话中断以外的异常。汇报已经发到本人私聊的, 算完成, 不再告警。"""
        run = self.store.get(ev.run_id)
        if not run or run.status not in jobs.ACTIVE:
            return
        job = jobs.get(run.job)
        if job and job.kind == 'report' and self.reported(run.job, run.slot):
            self.store.finish(run.id, f'已发出(发出后这一轮报错: {err[:100]})')
            return
        self._failed(run, err)

    def _organize_done(self, run, result):
        """整理会话这一轮结束: 把数字人提交的方案私信给本人; 没提交说明这次不需要调整。"""
        from ..organize import OrganizeStore
        store = OrganizeStore(self.d.storage)
        props = store.by_source(run.ref)
        for p in props:
            if self.d.notifier.send(store.render(p.id)):
                store.mark_notified(p.id)
        self.store.finish(run.id, (f"提交了方案 #{', #'.join(str(p.id) for p in props)}; " if props else '没有提交方案; ')
                          + (result.text or ''))

    def _briefing_done(self, ev, run, result):
        """简报这一轮结束: 查审计里有没有真的发到本人私聊; 没发出就由代码直接发数据版兜底。"""
        sent = any(a.action == 'send' and a.ok and a.chat_id == ev.chat_id
                   for a in self.d.storage.actions_for_turns([run.ref]))
        if sent:  # 已经发到本人私聊就算完成; 这一轮之后报错或中断也不再补发, 免得发两遍
            ok = result is not None and not result.is_error
            self.store.finish(run.id, (result.text if ok and result.text else '已发出')
                              + ('' if ok else '(发出后这一轮没有正常结束)'))
            return
        data = digest.briefing_data(self.d.storage, str(self.d.auth.user_id), client=self.d.client,
                                    home=self.d.cfg.get('agent_home'), cfg=self.d.cfg)
        ok = self.d.notifier.send(digest.render(data) + '\n\n(分身这次没写成简报, 以上是数据版)')
        why = '数字人没有发出简报' if result is not None and not result.is_error else \
            ('agent 会话中断' if result is None else f'{result.subtype}: {result.text or ""}')
        if ok:  # 本人已经收到数据版: 这一时段算完成, 不再重跑, 免得再收到一份
            self.store.finish(run.id, f'{why}; 已发数据版兜底')
        else:
            self._failed(run, why + '; 兜底也没发出去')

    async def _run_local(self, job: jobs.Job, run):
        self.store.start(run.id)
        try:
            summary = await asyncio.to_thread(getattr(self, f'_job_{job.name}'), run)
            self.store.finish(run.id, summary if isinstance(summary, str) else json.dumps(summary, ensure_ascii=False))
            logger.info(f'例行任务「{job.title}」完成: {summary}')
            if job.name == 'backfill' and self.store.get_state('backfill').get('done') and self.d.cfg.get('chat_groups'):
                await self.start(jobs.get('profile'), 'after-backfill', 'chain')  # 接着画像, 然后初始化记忆
        except Paused as e:
            self.store._set(run.id, status=jobs.PAUSED, finished_at=datetime.now(), summary=str(e))
            logger.info(f'例行任务「{job.title}」暂停: {e}')
        except asyncio.CancelledError:
            # 停服务(SIGTERM)时任务被取消, 不经过上面的 Paused: 同样记为暂停, 下次启动接着跑
            self.store._set(run.id, status=jobs.PAUSED, finished_at=datetime.now(), summary='停服务时暂停')
            raise
        except Exception as e:
            logger.exception(f'例行任务「{job.title}」失败')
            self._failed(run, repr(e))

    # ---- 本地任务 ----

    def _job_profile(self, run) -> str:
        res = groups.profile_all(self.d.client, self.d.storage, str(self.d.auth.user_id),
                                 action_words=groups.action_words(self.d.cfg))
        if res['new']:
            st = self.store.get_state('profile')
            st['new_chats'] = sorted(set(st.get('new_chats', [])) | set(res['new']))
            self.store.put_state('profile', st)
        return f"{res['chats']} 个会话; 新会话 {len(res['new'])} 个; 建议有变化 {res['changed']} 个"

    def _job_prune(self, run) -> str:
        return retention.summary(retention.run(self.d.cfg, self.d.storage, owner_id=str(self.d.auth.user_id)))  # 不整理数据库文件, 见 retention.run

    def _job_backfill(self, run) -> str:
        saved = self.store.get_state('backfill')

        def progress(st):
            if st.get('done') and not st.get('finished_at'):
                st['finished_at'] = datetime.now().timestamp()
            self.store.put_state('backfill', st)
        bf = backfill.Backfill(self.d.client, self.d.storage, str(self.d.auth.user_id),
                               int(self.d.cfg.get('backfill_days') or 30), saved=saved, progress=progress)
        res = bf.run(should_stop=self.stopping.is_set)
        if res.get('stopped'):
            raise Paused(backfill.summary(res))
        if not res['done']:
            raise RuntimeError(backfill.summary(res))
        return backfill.summary(res)

    def _failed(self, run, err: str):
        self.store.fail(run.id, err)
        job = jobs.get(run.job)
        title = job.title if job else run.job
        logger.error(f'例行任务「{title}」#{run.id} 失败: {err[:200]}')
        self.d.notifier.alert(f'例行任务「{title}」失败: {err[:150]}。详情: larkx jobs log --job {run.job}',
                              key=f'job:{run.job}', cooldown=ALERT_COOLDOWN)
