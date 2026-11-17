"""worker 双队列循环测试：单批与联合分片共存、恢复重入、重复消息幂等。

运行：PYTHONPATH=. python -m tests.test_worker_loop
"""
import os
import logging
import tempfile

logging.disable(logging.CRITICAL)  # 失败路径的预期异常日志静默
_TMP = tempfile.mkdtemp(prefix='worker-test-')
os.environ.setdefault('DATABASE_URL', f'sqlite:///{_TMP}/worker.db')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6399/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('ARTIFACT_DIR', _TMP)

import numpy as np  # noqa: E402
from sqlalchemy import select  # noqa: E402
from src.config.database import Base, engine, Session  # noqa: E402
from src.models.study import Study  # noqa: E402
from src.models.observation import Observation  # noqa: E402
from src.models.run import Run  # noqa: E402
from src.models.joint_run import JointRun  # noqa: E402
from src.services import joint_runs as jr  # noqa: E402
from src.services import runs as runs_svc  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f'  ✓ {name}')
    else:
        FAIL += 1; print(f'  ✗ {name} {detail}')


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.blpop_calls = 0

    def rpush(self, key, *vals):
        self.data.setdefault(key, []).extend(str(v) for v in vals)
        return len(self.data[key])

    def blpop(self, *keys, timeout=2):
        self.blpop_calls += 1
        for key in keys:
            q = self.data.get(key)
            if q:
                return key.encode(), q.pop(0).encode()
        return None


class DummyUser:
    email = 'analyst@demo.local'; role = 'analyst'


class DummyJoint:
    def __init__(self, name, ids, params=None):
        self.name = name; self.study_ids = ids; self.parameters = params or {}


class DummyRun:
    def __init__(self, study_id, name, parameters=None):
        self.study_id = study_id; self.name = name; self.parameters = parameters or {}


Base.metadata.create_all(engine)
fake = FakeRedis()
jr._redis = lambda: fake
runs_svc.Redis = type('R', (), {'from_url': staticmethod(lambda url: fake)})

with Session() as db:
    studies = []
    for j, keys in enumerate([[f'W{k}' for k in range(20)],
                              [f'W{k}' for k in range(10, 30)]]):
        s = Study(name=f'W纸批{j}', source_filename=f'w{j}.csv', description='')
        db.add(s); db.flush()
        rng = np.random.default_rng(j)
        for key in keys:
            b = float(rng.uniform(3, 6))
            db.add(Observation(study_id=s.id, source_key=key,
                               values={'before': b, 'after': max(0.1, b + float(rng.normal(-0.4, 0.5)))}))
        studies.append(s)
    db.commit()
    ids = [s.id for s in studies]

    # 一个联合运行（MC 多片）
    joint = jr.create(db, DummyJoint('W联合', ids, {'resamples': 1000, 'joint_shard_size': 250}), DummyUser())
    jid = joint['id']
    n_shards = len(joint['shards'])
    # 一个普通单批运行
    single = runs_svc.create(db, DummyRun(ids[0], 'W单批'), DummyUser())
    sid = single['id']

# 以模块方式加载 worker 前替换其 redis 客户端，使模块级 Redis.from_url 拿到 FakeRedis。
import sys  # noqa: E402
import types  # noqa: E402
fake_redis_pkg = types.ModuleType('redis')
fake_redis_pkg.Redis = type('Redis', (), {'from_url': staticmethod(lambda url: fake)})
sys.modules['redis'] = fake_redis_pkg
import src.worker as worker  # noqa: E402
worker.redis = fake
# 模拟 worker 冷启动：Redis 队列为空，仅靠数据库状态恢复入队
fake.data.clear()
with Session() as db:
    worker.recover_jobs(db)

check('worker 恢复把单批与联合分片都放入队列',
      fake.data.get('analysis-queue') == [str(sid)]
      and len(fake.data.get('joint-analysis-queue', [])) == n_shards,
      str(fake.data))

# 驱动有限步消费循环（复刻 worker 循环体，避免无限 while）
def pump(max_idle=3):
    idle = 0
    processed = 0
    while idle < max_idle:
        job = fake.blpop('analysis-queue', 'joint-analysis-queue', timeout=2)
        if not job:
            idle += 1; continue
        idle = 0
        queue, payload = job[0].decode(), job[1]
        with Session() as db:
            if queue == 'analysis-queue':
                worker.process_single(db, int(payload))
            else:
                rid, si = map(int, payload.decode().split(':'))
                worker.process_joint(db, rid, si)
        processed += 1
    return processed


n = pump()
with Session() as db:
    jrun = db.get(JointRun, jid)
    srun = db.get(Run, sid)
    check('联合运行一次性发布', jrun.status == 'completed' and jrun.result is not None)
    check('单批运行同时完成', srun.status == 'completed' and srun.result is not None)
    check('消费消息数 = 单批1 + 联合分片数', n == 1 + n_shards, f'{n} vs {1+n_shards}')
    check('联合产物与单批产物均写出',
          (jr.ARTIFACT_DIR / f'joint-run-{jid}.json').exists()
          and (worker.ARTIFACT_DIR / f'run-{sid}.json').exists())

# 重复/晚到消息：重新入队同一联合分片，应被幂等忽略，结果不变
import json  # noqa: E402
with Session() as db:
    before = json.dumps(db.get(JointRun, jid).result, sort_keys=True)
fake.rpush('joint-analysis-queue', f'{jid}:0')
pump()
with Session() as db:
    after = json.dumps(db.get(JointRun, jid).result, sort_keys=True)
    check('重复分片消息不改变已发布结果', before == after)

# 失败路径：登记新联合运行，篡改快照后消费，整组 failed、无半组
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402
with Session() as db:
    j2 = jr.create(db, DummyJoint('W联合失败', ids, {'resamples': 1000, 'joint_shard_size': 250}), DummyUser())
    j2id = j2['id']
    # 先消费第一片
    first = fake.data['joint-analysis-queue'].pop(0)
    rid, si = map(int, first.split(':'))
    with Session() as db2:
        jr.process_shard(db2, rid, si)
    run = db.get(JointRun, j2id)
    run.batches[0]['snapshot'][0]['values']['before'] = 999.0
    flag_modified(run, 'batches')
    db.commit()
pump()
with Session() as db:
    run = db.get(JointRun, j2id)
    check('篡改后整组 failed 且无半组结论', run.status == 'failed' and run.result is None)

print(f'\n结果：{PASS} 通过 / {FAIL} 失败')
raise SystemExit(1 if FAIL else 0)
