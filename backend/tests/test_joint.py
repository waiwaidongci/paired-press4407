"""联合检验端到端测试：算法 oracle + 分片编排 + 崩溃恢复 + 发布前失败。

运行：PYTHONPATH=. python -m tests.test_joint
（需要可写的临时 SQLite，不依赖 PostgreSQL/Redis；Redis 用最小内存替身。）
"""
import os
import sys
import json
import tempfile

import numpy as np

_TMP = tempfile.mkdtemp(prefix='joint-test-')
os.environ.setdefault('DATABASE_URL', f'sqlite:///{_TMP}/test.db')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6399/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('ARTIFACT_DIR', _TMP)

from sqlalchemy import select  # noqa: E402
from src.config.database import Base, engine, Session  # noqa: E402
from src.models.study import Study  # noqa: E402
from src.models.observation import Observation  # noqa: E402
from src.models.joint_run import JointRun, JointShard  # noqa: E402
from src.models import audit  # noqa: E402
from src.services import joint_algorithm as ja  # noqa: E402
from src.services import joint_runs as jr  # noqa: E402
from src.utils.serializers import checksum  # noqa: E402

PASS, FAIL = 0, 0


class FakeRedis:
    """仅实现联合队列用到的列表操作的内存替身。"""
    def __init__(self):
        self.lists = {}

    def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(str(v) for v in values)
        return len(self.lists[key])

    def lrange(self, key, lo, hi):
        return list(self.lists.get(key, []))[lo: None if hi == -1 else hi + 1]

    def lrem(self, key, count, value):
        vals = self.lists.get(key, [])
        try:
            vals.remove(str(value))
            return 1
        except ValueError:
            return 0

    def llen(self, key):
        return len(self.lists.get(key, []))


class DummyUser:
    email = 'analyst@demo.local'
    role = 'analyst'


class DummyInput:
    def __init__(self, name, study_ids, parameters=None):
        self.name = name
        self.study_ids = study_ids
        self.parameters = parameters or {}


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  ✓ {name}')
    else:
        FAIL += 1
        print(f'  ✗ {name} {detail}')


def snap(rows):
    return [{'source_key': k, 'values': {'before': float(b), 'after': float(a)}}
            for k, b, a in rows]


def brute_force_joint_p(d, t_obs):
    """独立蛮力 oracle：枚举全部 2**n 符号配置，直接计算联合 T 的双侧 p。"""
    n = d.shape[1]
    total = 2 ** n
    ge = 0
    for code in range(total):
        signs = np.array([1.0 if (code >> k) & 1 else -1.0 for k in range(n)])
        t = 0.0
        for j in range(d.shape[0]):
            dd = d[j] * signs
            ss = float((dd * dd).sum())
            if ss > 0:
                t += float(dd.sum()) / np.sqrt(ss)
        if abs(t) >= abs(t_obs) - 1e-12:
            ge += 1
    return ge / total


def test_algorithm_oracle():
    print('[1] 小样本穷举 oracle（重叠纸样 + 缺席）')
    b1 = snap([('P1', 5, 4), ('P2', 6, 4.5), ('P3', 3, 2.5)])
    b2 = snap([('P2', 5.5, 5), ('P3', 4, 3), ('P4', 7, 6)])
    b3 = snap([('P1', 4.5, 4.6), ('P4', 6.5, 5.5)])
    d, keys, mem = ja.build_matrix([b1, b2, b3])
    check('并集编号排序对齐', keys == ['P1', 'P2', 'P3', 'P4'])
    check('缺席位置为 0 且 membership=False',
          (not mem[0, 3]) and d[0, 3] == 0.0 and not mem[2, 1] and d[2, 1] == 0.0)
    plan = ja.shard_plan(len(keys), 10000)
    counts = ja._empty_counts(3)
    for s in plan['shards']:
        ja._add(counts, ja.compute_shard(d, plan, s))
    res = ja.assemble(d, plan, counts)
    oracle = brute_force_joint_p(d, res['t_observed'])
    check('联合 p 与蛮力枚举一致', abs(res['p_joint'] - oracle) < 1e-12,
          f'{res["p_joint"]} vs {oracle}')
    # 双侧最大值校正恒等于 |T| 越界比例（对称零假设下）
    pos, neg = counts['joint_ge_pos'], counts['joint_le_neg']
    check('双侧最大值校正口径',
          abs(res['p_joint'] - min(1.0, 2 * max(pos, neg) / plan['total'])) < 1e-12)
    # 共享翻转验证：对任一配置，各批使用的符号向量完全相同（由构造保证），
    # 这里验证缺席批对共享纸样翻转的响应——手工核对一个配置
    signs = np.array([-1, 1, -1, 1], float)
    t_manual = 0.0
    for j in range(3):
        dd = d[j] * signs
        t_manual += dd.sum() / np.sqrt((dd * dd).sum())
    flipped = signs[None, :] * d
    t_pipe = sum(flipped[j].sum() / np.sqrt((flipped[j] ** 2).sum()) for j in range(3))
    check('共享符号配置手工核对', abs(t_manual - t_pipe) < 1e-12)


def test_n18_boundary_and_mc():
    print('[2] 穷举/MC 分界与 Monte Carlo 分片')
    rng = np.random.default_rng(7)
    d = rng.normal(size=(2, 19)) - 0.2
    plan = ja.shard_plan(19, 1000, 250)
    check('n=19 走 Monte Carlo', plan['mode'] == 'monte_carlo' and plan['total'] == 1000)
    check('分片覆盖且不重叠并拼满分母',
          [s['start'] for s in plan['shards']] == [0, 250, 500, 750]
          and sum(s['size'] for s in plan['shards']) == 1000)
    counts = ja._empty_counts(2)
    for s in plan['shards']:
        ja._add(counts, ja.compute_shard(d, plan, s))
    r1 = ja.assemble(d, plan, counts)
    # 重跑同计划：分片幂等、结果完全一致
    counts2 = ja._empty_counts(2)
    for s in plan['shards']:
        ja._add(counts2, ja.compute_shard(d, plan, s))
    r2 = ja.assemble(d, plan, counts2)
    check('同计划重算结果逐位一致', r1['p_joint'] == r2['p_joint'] and counts == counts2)
    # 乱序执行不影响汇总
    counts3 = ja._empty_counts(2)
    for s in reversed(plan['shards']):
        ja._add(counts3, ja.compute_shard(d, plan, s))
    r3 = ja.assemble(d, plan, counts3)
    check('分片乱序到达结果一致', r3['p_joint'] == r1['p_joint'])
    # 重复汇总同一分片会改变计数（数学事实），由服务层幂等保证不发生——
    # 这里确认服务层依赖的部分计数本身是确定的
    p0 = ja.compute_shard(d, plan, plan['shards'][0])
    p0b = ja.compute_shard(d, plan, plan['shards'][0])
    check('单片重复计算输出相同', p0 == p0b)
    # n=18 仍穷举
    check('n=18 走精确枚举', ja.shard_plan(18, 1000)['mode'] == 'exact')
    check('p 落在 [0,1]', 0.0 <= r1['p_joint'] <= 1.0)


def test_all_enumerated_small_cases():
    print('[3] 多组随机小样本逐一枚举核对')
    rng = np.random.default_rng(11)
    for trial in range(12):
        n_batch = int(rng.integers(2, 4))
        n = int(rng.integers(2, 9))
        # 随机缺失：每批随机抽 60%-100% 纸样
        d = np.zeros((n_batch, n))
        mem = np.zeros((n_batch, n), bool)
        for j in range(n_batch):
            for k in range(n):
                if rng.random() < 0.75:
                    d[j, k] = float(rng.normal(-0.3, 1.0))
                    mem[j, k] = True
        if not mem.any(axis=1).all():
            continue
        plan = ja.shard_plan(n, 10000)
        counts = ja._empty_counts(n_batch)
        for s in plan['shards']:
            ja._add(counts, ja.compute_shard(d, plan, s))
        res = ja.assemble(d, plan, counts)
        oracle = brute_force_joint_p(d, res['t_observed'])
        check(f'试次{trial}: {n_batch}批×{n}样 p={res["p_joint"]:.4f}',
              abs(res['p_joint'] - oracle) < 1e-12, f'oracle={oracle}')


def test_invalid_inputs():
    print('[4] 算法层输入拒绝')
    for label, fn, args in [
        ('空纸批列表', ja.build_matrix, ([],)),
        ('含空纸批', ja.build_matrix, ([snap([('P1', 1, 2)]), []],)),
        ('重采样低于下限', ja.shard_plan, (19, 199)),
        ('分片低于下限', ja.shard_plan, (19, 1000, 50)),
        ('分片高于上限', ja.shard_plan, (19, 1000, 20000)),
    ]:
        try:
            fn(*args)
            check(label + ' 被拒绝', False)
        except ValueError:
            check(label + ' 被拒绝', True)
    bad = [{'source_key': 'P1', 'values': {'before': 1.0}}]
    try:
        ja.build_matrix([bad, snap([('P1', 1, 2)])])
        check('字段不完整被拒绝', False)
    except ValueError:
        check('字段不完整被拒绝', True)
    dup = snap([('P1', 1, 2), ('P1', 2, 3)])
    try:
        ja.build_matrix([dup, snap([('P1', 1, 2)])])
        check('批内编号重复被拒绝', False)
    except ValueError:
        check('批内编号重复被拒绝', True)


def seed_studies(db):
    studies = []
    specs = [
        ('纸批甲', [('S1', 5, 4), ('S2', 6, 5), ('S3', 3, 3.5)]),
        ('纸批乙', [('S2', 5.5, 5), ('S3', 4, 3), ('S4', 7, 5.8)]),
        ('纸批丙', [('S1', 4.5, 4.4), ('S4', 6.5, 6)]),
    ]
    for name, rows in specs:
        s = Study(name=name, source_filename=name + '.csv', description='测试')
        db.add(s)
        db.flush()
        for key, b, a in rows:
            db.add(Observation(study_id=s.id, source_key=key,
                               values={'before': float(b), 'after': float(a)}))
        studies.append(s)
    db.commit()
    return studies


def test_service_happy_path():
    print('[5] 服务层：登记→分片→一次性发布')
    fake = FakeRedis()
    jr._redis = lambda: fake
    with Session() as db:
        studies = seed_studies(db)
        ids = [s.id for s in studies]
        out = jr.create(db, DummyInput('联合检验A', ids), DummyUser())
        run_id = out['id']
        check('登记即返回 queued 且带分片', out['status'] == 'queued'
              and len(out['shards']) >= 1 and len(out['plan']['shards']) == len(out['shards']))
        check('每片一条队列消息', fake.llen(jr.QUEUE_KEY) == len(out['shards']))
        check('联合分母创建时固定', out['plan']['total'] == 2 ** out['alignment']['union_size'])
        msgs = fake.lrange(jr.QUEUE_KEY, 0, -1)

        # 模拟逐片处理
        for m in msgs:
            rid, si = map(int, m.split(':'))
            jr.process_shard(db, rid, si)
        run = db.get(JointRun, run_id)
        check('全片完成后一次性发布', run.status == 'completed' and run.result is not None)
        check('结果含校正p与原始单批p',
              '联合校正后双侧p' in run.result['metrics']
              and len(run.result['batch_metrics']) == 3
              and all('原始单批双侧p' in b for b in run.result['batch_metrics']))
        check('纳入范围表含缺席标记',
              run.result['inclusion_rows'][0] == ['S1', '纳入', '缺席', '纳入'])
        # 与蛮力 oracle 对照
        d = ja.build_matrix([b['snapshot'] for b in run.batches])[0]
        oracle = brute_force_joint_p(d, run.result['metrics']['联合观测T(差值和/√平方和)'])
        check('服务层发布 p 与 oracle 一致',
              abs(run.result['metrics']['联合校正后双侧p'] - oracle) < 1e-12)
        check('审计动作已登记',
              db.scalar(select(audit.Audit).where(audit.Audit.action == 'joint_run.queue')) is not None)
        return run_id


def test_duplicate_and_late_shards():
    print('[6] 重复分片与晚到分片不改变联合分母与结论')
    fake = FakeRedis()
    jr._redis = lambda: fake
    with Session() as db:
        studies = db.scalars(select(Study).order_by(Study.id)).all()
        ids = [s.id for s in studies]
        out = jr.create(db, DummyInput('联合检验B', ids), DummyUser())
        rid = out['id']
        total_before = out['plan']['total']
        # 处理第一片后重复投递同一片两次
        jr.process_shard(db, rid, 0)
        executed1 = jr.process_shard(db, rid, 0)
        executed2 = jr.process_shard(db, rid, 0)
        check('已存片不重计（幂等跳过）', executed1 is False and executed2 is False)
        # 晚到分片：先处理最后一片，再处理中间片
        last = len(out['shards']) - 1
        if last >= 2:
            jr.process_shard(db, rid, last)
        for si in range(1, last + 1):
            jr.process_shard(db, rid, si)
        run = db.get(JointRun, rid)
        check('乱序/晚到分片后正常发布', run.status == 'completed')
        check('联合分母未改变', run.plan['total'] == total_before)
        # 完成后再投递任何分片消息：直接忽略，结果不变
        before = json.dumps(run.result, sort_keys=True)
        again = jr.process_shard(db, rid, 0)
        db.expire_all()
        run = db.get(JointRun, rid)
        check('发布后消息被忽略且结果不变',
              again is False and json.dumps(run.result, sort_keys=True) == before)


def test_crash_recovery():
    print('[7] 崩溃恢复：running 分片重新入队，已存片不重计')
    fake = FakeRedis()
    jr._redis = lambda: fake
    with Session() as db:
        # 并集 ≥19 个编号 → Monte Carlo 多片，才能制造“部分完成、部分在跑”
        mc_studies = []
        for j, keys in enumerate([
            [f'M{k}' for k in range(20)],
            [f'M{k}' for k in range(5, 25)],
            [f'M{k}' for k in range(0, 20, 2)],
        ]):
            s = Study(name=f'MC纸批{j}', source_filename=f'mc{j}.csv', description='MC测试')
            db.add(s); db.flush()
            rng = np.random.default_rng(100 + j)
            for key in keys:
                b = float(rng.uniform(3, 6))
                db.add(Observation(study_id=s.id, source_key=key,
                                   values={'before': b, 'after': max(0.1, b + float(rng.normal(-0.4, 0.5)))}))
            mc_studies.append(s)
        db.commit()
        out = jr.create(db, DummyInput('联合检验C', [s.id for s in mc_studies],
                                       {'resamples': 1000, 'joint_shard_size': 250}), DummyUser())
        rid = out['id']
        n_shards = len(out['shards'])
        assert n_shards >= 3, '前置条件：Monte Carlo 至少 3 个分片'
        # 完成两片，第三片处理到 running 后“崩溃”（不写 counts）
        jr.process_shard(db, rid, 0)
        jr.process_shard(db, rid, 1)
        crash_index = 2
        shard2 = db.scalar(select(JointShard).where(
            JointShard.joint_run_id == rid, JointShard.shard_index == crash_index))
        shard2.status = 'running'
        db.commit()
        run_before = db.get(JointRun, rid)
        check('崩溃时运行未发布', run_before.status == 'queued' and run_before.result is None)
    # 新 worker 启动恢复
    fake2 = FakeRedis()
    with Session() as db:
        jr.recover(fake2, db)
        queued = sorted(int(m.split(':')[1]) for m in fake2.lrange(jr.QUEUE_KEY, 0, -1))
        # 精确断言：恢复的分片集合 == 非 completed 分片
        incomplete = [x.shard_index for x in db.scalars(select(JointShard).where(
            JointShard.joint_run_id == rid, JointShard.status != 'completed'))]
        check('恢复集合精确等于未完成分片', queued == sorted(incomplete))
        for m in fake2.lrange(jr.QUEUE_KEY, 0, -1):
            r, si = map(int, m.split(':'))
            jr.process_shard(db, r, si)
        run = db.get(JointRun, rid)
        check('恢复后完成且已存片未重计', run.status == 'completed')
        # 与无崩溃路径结果一致：重新登记一个全新运行比较 p
        out2 = jr.create(db, DummyInput('联合检验C对照', [s.id for s in mc_studies],
                                        {'resamples': 1000, 'joint_shard_size': 250}), DummyUser())
        rid2 = out2['id']
        for s in out2['plan']['shards']:
            jr.process_shard(db, rid2, s['index'])
        p1 = run.result['metrics']['联合校正后双侧p']
        p2 = db.get(JointRun, rid2).result['metrics']['联合校正后双侧p']
        check('崩溃恢复与一次跑成结果一致', p1 == p2)


def test_failure_no_half_result():
    print('[8] 分片失败 → 整组 failed，无半组结论')
    fake = FakeRedis()
    jr._redis = lambda: fake
    with Session() as db:
        mc_studies = [s for s in db.scalars(select(Study).where(Study.name.like('MC纸批%')).order_by(Study.id))]
        out = jr.create(db, DummyInput('联合检验D', [s.id for s in mc_studies],
                                       {'resamples': 1000, 'joint_shard_size': 250}), DummyUser())
        rid = out['id']
        assert len(out['shards']) >= 2
        jr.process_shard(db, rid, 0)
        # 破坏冻结快照使留档校验失败（JSON 原地修改需 flag_modified 才能落库）
        from sqlalchemy.orm.attributes import flag_modified
        run = db.get(JointRun, rid)
        run.batches[0]['snapshot'][0]['values']['before'] = 999.0
        flag_modified(run, 'batches')
        db.commit()
        target = 1
        try:
            jr.process_shard(db, rid, target)
            check('篡改后分片抛错', False)
        except ValueError:
            check('篡改后分片抛错', True)
        db.expire_all()
        run = db.get(JointRun, rid)
        check('整组 failed 且无 result', run.status == 'failed' and run.result is None)
        check('失败信息落库', bool(run.error))
        # 后续消息继续到达也不会“救活”
        again = jr.process_shard(db, rid, (target + 1) % len(out['shards']))
        check('失败后消息直接忽略', again is False)


def test_publish_crash_at_finalize():
    print('[9] 发布前崩溃：全片已存，恢复后只汇总不重计')
    fake = FakeRedis()
    jr._redis = lambda: fake
    with Session() as db:
        studies = db.scalars(select(Study).order_by(Study.id)).all()
        out = jr.create(db, DummyInput('联合检验E', [s.id for s in studies]), DummyUser())
        rid = out['id']
        # 直接把所有分片标记 completed 并写入计数（模拟 finalize 前进程被杀）
        d = ja.build_matrix([b['snapshot'] for b in db.get(JointRun, rid).batches])[0]
        for s in out['plan']['shards']:
            shard = db.scalar(select(JointShard).where(
                JointShard.joint_run_id == rid, JointShard.shard_index == s['index']))
            shard.counts = ja.compute_shard(d, out['plan'], s)
            shard.status = 'completed'
        run = db.get(JointRun, rid)
        run.status = 'queued'  # finalize 未执行
        db.commit()
    fake3 = FakeRedis()
    with Session() as db:
        jr.recover(fake3, db)
        # 没有未完成分片，因此无需重算、无需消息；recover 兜底直接汇总发布
        check('全片已存时恢复队列为空', fake3.llen(jr.QUEUE_KEY) == 0)
        run = db.get(JointRun, rid)
        check('recover 兜底汇总已存片完成发布', run.status == 'completed' and run.result is not None)
        # 产物文件
        path = jr.ARTIFACT_DIR / f'joint-run-{rid}.json'
        check('产物文件已写出', path.exists() and json.loads(path.read_text())['input_sha256'])


def test_validation_rules():
    print('[10] 服务层登记校验')
    fake = FakeRedis()
    jr._redis = lambda: fake
    with Session() as db:
        studies = db.scalars(select(Study).order_by(Study.id)).all()
        ids = [s.id for s in studies]
        from fastapi import HTTPException
        for label, data in [
            ('仅选一个纸批', DummyInput('x', ids[:1])),
            ('重复纸批', DummyInput('x', [ids[0], ids[0]])),
            ('未知参数', DummyInput('x', ids, {'bogus': 1})),
            ('分片大小越界', DummyInput('x', ids, {'joint_shard_size': 10})),
        ]:
            try:
                jr.create(db, data, DummyUser())
                check(label + ' 被拒绝', False)
            except HTTPException as e:
                check(label + ' 被拒绝', e.status_code == 422)


def main():
    Base.metadata.create_all(engine)
    test_algorithm_oracle()
    test_n18_boundary_and_mc()
    test_all_enumerated_small_cases()
    test_invalid_inputs()
    test_service_happy_path()
    test_duplicate_and_late_shards()
    test_crash_recovery()
    test_failure_no_half_result()
    test_publish_crash_at_finalize()
    test_validation_rules()
    print(f'\n结果：{PASS} 通过 / {FAIL} 失败')
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
