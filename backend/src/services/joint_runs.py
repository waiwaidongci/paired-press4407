"""跨纸批联合检验的编排服务。

职责边界：
- create：整组输入在同一时点冻结（多纸批快照 + 校验和 + 分片计划），登记动作
  不阻塞普通纸批/单批运行的继续登记；随后把分片投入独立 Redis 队列。
- process_shard：单个分片的幂等计算，已完成的分片直接跳过，重复或晚到分片
  不会改变创建时固定的联合分母。
- finalize：全部分片与留档校验通过后一次性发布 result；任一分片失败则整组
  failed，不产生半组结论。
"""
import os
import json
from pathlib import Path

from redis import Redis
from sqlalchemy import select
from fastapi import HTTPException

from src.models.joint_run import JointRun, JointShard
from src.models.study import Study
from src.services import studies
from src.services.domain_validation import validate_joint_parameters
from src.services.audits import log
from src.services import joint_algorithm as ja
from src.utils.serializers import record, now, checksum

QUEUE_KEY = 'joint-analysis-queue'
ARTIFACT_DIR = Path(os.environ.get('ARTIFACT_DIR', '/app/artifacts'))


def _redis():
    return Redis.from_url(os.environ['REDIS_URL'])


def create(db, data, user):
    study_ids = data.study_ids
    if not isinstance(study_ids, list) or len(study_ids) < 2:
        raise HTTPException(422, '联合检验至少选择两个纸批')
    if len(set(study_ids)) != len(study_ids):
        raise HTTPException(422, '纸批重复选择，请去重后再提交')
    try:
        params = validate_joint_parameters(data.parameters)
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e))

    # 整组输入取同一时点：在同一次请求内依次冻结各纸批快照。
    batches = []
    for study_id in study_ids:
        studies.require(db, study_id)
        rows = studies.observations(db, study_id)
        if not rows:
            raise HTTPException(422, f'纸批 {study_id} 为空，无法纳入联合检验')
        snapshot = [{'source_key': x['source_key'], 'values': x['values']} for x in rows]
        study = db.get(Study, study_id)
        batches.append({
            'study_id': study_id,
            'study_name': study.name,
            'snapshot': snapshot,
            'checksum': checksum(snapshot),
        })

    try:
        d, keys, membership = ja.build_matrix([b['snapshot'] for b in batches])
        plan = ja.shard_plan(len(keys), params['resamples'], params['joint_shard_size'])
    except ValueError as e:
        raise HTTPException(422, str(e))

    alignment = {
        'keys': keys,
        # membership[j] 为该纸批实际纳入的纸样编号；缺席编号不出现在其中
        'included': [[keys[k] for k in range(len(keys)) if membership[j, k]]
                     for j in range(len(batches))],
        'batch_sizes': [int(x) for x in membership.sum(axis=1)],
        'union_size': len(keys),
    }
    frozen = {'batches': batches, 'parameters': params, 'plan': plan, 'alignment': alignment}
    run = JointRun(
        name=data.name, status='queued', parameters=params, batches=batches,
        alignment=alignment, plan=plan, checksum=checksum(frozen),
        created_at=now(),
    )
    db.add(run)
    db.flush()
    for shard in plan['shards']:
        db.add(JointShard(
            joint_run_id=run.id, shard_index=shard['index'], start=shard['start'],
            size=shard['size'], status='queued', updated_at=now(),
        ))
    log(db, user, 'joint_run.queue', f'{run.id}: {len(batches)}批/{plan["mode"]}/{plan["total"]}配置')
    db.commit()
    # 每个分片独立入队；重复消息在 process_shard 中被幂等忽略。
    redis = _redis()
    for shard in plan['shards']:
        redis.rpush(QUEUE_KEY, f'{run.id}:{shard["index"]}')
    return get(db, run.id)


def get(db, run_id):
    run = db.get(JointRun, run_id)
    if not run:
        raise HTTPException(404, '联合运行不存在')
    out = record(run)
    out['shards'] = [record(x) for x in db.scalars(
        select(JointShard).where(JointShard.joint_run_id == run_id)
        .order_by(JointShard.shard_index))]
    return out


def list_runs(db):
    return [get(db, x) for x in db.scalars(select(JointRun.id).order_by(JointRun.id.desc()))]


def _rebuild_d(run):
    return ja.build_matrix([b['snapshot'] for b in run.batches])[0]


def process_shard(db, run_id, shard_index):
    """计算一个分片。返回 True 表示本次实际执行，False 表示幂等跳过。"""
    run = db.get(JointRun, run_id)
    if not run or run.status in ('completed', 'failed'):
        return False
    shard = db.scalar(select(JointShard).where(
        JointShard.joint_run_id == run_id, JointShard.shard_index == shard_index))
    if not shard:
        raise ValueError(f'分片 {shard_index} 不在计划内')
    if shard.status == 'completed' and shard.counts is not None:
        return False  # 中断恢复/重复消息：已存片不重计

    shard.status = 'running'
    shard.updated_at = now()
    db.commit()
    try:
        # 留档核对：冻结快照被篡改则整组失败，绝不带错发布。
        frozen = {'batches': run.batches, 'parameters': run.parameters,
                  'plan': run.plan, 'alignment': run.alignment}
        if checksum(frozen) != run.checksum:
            raise ValueError('联合输入快照校验失败')
        for batch in run.batches:
            if checksum(batch['snapshot']) != batch['checksum']:
                raise ValueError(f'纸批 {batch["study_id"]} 快照校验失败')
        d = _rebuild_d(run)
        plan_shard = run.plan['shards'][shard_index]
        if plan_shard['start'] != shard.start or plan_shard['size'] != shard.size:
            raise ValueError('分片区间与冻结计划不一致')
        counts = ja.compute_shard(d, run.plan, plan_shard)
        shard.counts = counts
        shard.status = 'completed'
        shard.error = ''
        shard.updated_at = now()
        db.commit()
    except Exception as e:
        shard.status = 'failed'
        shard.error = str(e)
        shard.updated_at = now()
        run.status = 'failed'
        run.error = f'分片 {shard_index} 失败: {e}'
        db.commit()
        raise
    finalize_if_ready(db, run_id)
    return True


def finalize_if_ready(db, run_id):
    """全部分片成功且留档核对通过后一次性发布；否则保持原状。"""
    run = db.get(JointRun, run_id)
    if not run or run.status in ('completed', 'failed'):
        return
    shards = list(db.scalars(select(JointShard)
                             .where(JointShard.joint_run_id == run_id)
                             .order_by(JointShard.shard_index)))
    if any(s.status == 'failed' for s in shards):
        run.status = 'failed'
        run.error = '存在失败分片，联合检验整组废弃，不发布半组结论'
        db.commit()
        return
    if not shards or any(s.status != 'completed' or s.counts is None for s in shards):
        return  # 其余分片仍在计算，等待

    # 发布前最后一次留档核对。
    frozen = {'batches': run.batches, 'parameters': run.parameters,
              'plan': run.plan, 'alignment': run.alignment}
    if checksum(frozen) != run.checksum:
        run.status = 'failed'
        run.error = '发布前联合输入校验失败'
        db.commit()
        return
    for batch in run.batches:
        if checksum(batch['snapshot']) != batch['checksum']:
            run.status = 'failed'
            run.error = f'发布前纸批 {batch["study_id"]} 校验失败'
            db.commit()
            return

    d = _rebuild_d(run)
    counts = ja._empty_counts(d.shape[0])
    for shard in shards:  # 按计划顺序汇总，与到达顺序无关
        ja._add(counts, shard.counts)
    stats = ja.assemble(d, run.plan, counts)
    run.result = _build_result(run, d, stats)
    run.result['input_sha256'] = run.checksum
    run.status = 'completed'
    db.commit()
    ARTIFACT_DIR.mkdir(exist_ok=True)
    (ARTIFACT_DIR / f'joint-run-{run.id}.json').write_text(
        json.dumps(run.result, ensure_ascii=False, allow_nan=False))
    log(db, 'worker', 'joint_run.complete',
        f'{run.id}: 校正p={stats["p_joint"]:.6g}/{stats["method"]}')
    db.commit()


def _build_result(run, d, stats):
    alignment = run.alignment
    batch_metrics = []
    for j, batch in enumerate(run.batches):
        n_pairs = alignment['batch_sizes'][j]
        mean_d = float(d[j].sum() / n_pairs) if n_pairs else 0.0
        batch_metrics.append({
            'study_id': batch['study_id'],
            'study_name': batch['study_name'],
            '纳入纸样数': n_pairs,
            '平均差(新减原)': mean_d,
            '差值和': stats['batch_sum_d'][j],
            '差值平方和': stats['batch_sum_d2'][j],
            '原始单批双侧p': stats['p_batches'][j],
            '单批观测T': stats['batch_t_observed'][j],
        })
    inclusion_rows = [
        [key] + ['纳入' if key in alignment['included'][j] else '缺席'
                 for j in range(len(run.batches))]
        for key in alignment['keys']
    ]
    metrics = {
        '纳入纸批数': len(run.batches),
        '纸样编号并集数': alignment['union_size'],
        '联合配对数(含跨批重复纸样)': int(sum(alignment['batch_sizes'])),
        '联合观测T(差值和/√平方和)': stats['t_observed'],
        '联合校正后双侧p': stats['p_joint'],
        '计算方法': stats['method'],
        '枚举或模拟配置总数': stats['total_configurations'],
        '分片数': len(run.plan['shards']),
        '联合定向越界计数(正侧)': stats['counts']['joint_ge_pos'],
        '联合定向越界计数(负侧)': stats['counts']['joint_le_neg'],
    }
    caveat = (
        '联合对称假设：零假设下各纸批的配对差均关于0对称且纸批间相互独立，'
        '因此每张纸的差值符号可交换；同一张纸在所有纸批中共享同一次符号翻转，'
        '缺席纸样在该纸批中保持缺席（贡献为0）。双侧p取正负两侧尾概率的最大值'
        '再乘2（截断为1），对称分布下等价于|T|越界概率，有限样本下偏保守。'
        '各纸批单独检验再挑最小p会因多重比较夸大工艺优势，联合检验只给一个'
        '校正后p；未拒绝不意味着等效。')
    interpretation = (
        f"联合 {len(run.batches)} 个纸批、{alignment['union_size']} 个纸样编号，"
        f"观测 T={stats['t_observed']:.6g}，双侧最大值校正 p={stats['p_joint']:.6g}"
        f"（{stats['method']}）。各纸批原始单批 p 见纸批明细，请勿以其中最小值"
        "替代联合结论。")
    return {
        'metrics': metrics,
        'batch_metrics': batch_metrics,
        'interpretation': interpretation,
        'caveat': caveat,
        'inclusion_columns': ['纸样编号'] + [b['study_name'] for b in run.batches],
        'inclusion_rows': inclusion_rows,
        'table_title': '纸样编号对齐与各纸批纳入/缺席范围',
    }


def recover(redis, db):
    """worker 启动时恢复未完成工作；已完成分片不重计。

    - queued/running 分片重新入队（重复消息由 process_shard 幂等忽略）；
    - 兜底：全片已完成但运行尚未发布（最后一片提交后、finalize 提交前崩溃）
      时直接重新汇总，不依赖遗留队列消息。
    """
    pending = list(db.scalars(select(JointShard).where(JointShard.status.in_(['queued', 'running']))))
    for shard in pending:
        run = db.get(JointRun, shard.joint_run_id)
        if run and run.status not in ('completed', 'failed'):
            redis.rpush(QUEUE_KEY, f'{run.id}:{shard.shard_index}')
    for run in db.scalars(select(JointRun).where(JointRun.status.in_(['queued', 'running']))):
        # 全片完成时执行汇总发布，否则原样返回等待分片消息。
        finalize_if_ready(db, run.id)
