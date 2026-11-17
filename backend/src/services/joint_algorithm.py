"""跨纸批联合符号翻转检验。

设计要点（与需求逐条对应）：
- 多个纸批按纸样编号（source_key）对齐到并集；同一张纸在所有纸批中共享同一次
  符号翻转（flip plan 只按并集位置生成一次），某纸批缺该纸样则该位置贡献为 0，
  “缺席保持缺席”。
- 联合统计量：T = Σd / sqrt(Σd²)，其中求和跨全部纸批的全部配对差。零假设下
  联合对称（各纸批差值在零假设下均关于 0 对称且相互独立），符号可交换翻转，
  分布关于 0 对称；双侧 p 取两侧尾概率的最大值再乘 2（上界 1），即“双侧最大值
  校正”，对称分布下等价于 |T| 越界概率，非对称有限样本下偏保守，避免夸大优势。
- 检验拆成互不重叠的分片（穷举按编码区间、Monte Carlo 按抽样序号区间），每个
  分片只写自己的部分计数；联合分母（总配置数/总抽样数）在创建时由计划固定，
  重复或晚到分片不会改变分母。
"""
import math
import numpy as np

EXACT_LIMIT = 18          # 并集纸样数不超过该值时全量穷举 2**n 种符号
DEFAULT_MC_SHARD = 2000   # Monte Carlo 默认每片抽样数
MIN_SHARD, MAX_SHARD = 100, 10000
INNER_BLOCK = 256         # 片内再分块，控制单次内存占用


def build_matrix(batch_snapshots):
    """把多个纸批快照对齐成差值矩阵。

    返回 (d, keys, membership)：
    d[j, k] 为第 j 个纸批中编号 keys[k] 的配对差 (after-before)，缺席为 0；
    membership[j, k] 标记该纸批是否实际纳入该纸样。
    """
    if not batch_snapshots:
        raise ValueError('至少选择一个纸批')
    if any(not snap for snap in batch_snapshots):
        raise ValueError('存在空纸批，无法联合检验')
    keys = sorted({row['source_key'] for snap in batch_snapshots for row in snap})
    if not keys:
        raise ValueError('纸批为空，无法联合检验')
    index = {key: k for k, key in enumerate(keys)}
    n_batch, n = len(batch_snapshots), len(keys)
    d = np.zeros((n_batch, n), dtype=float)
    membership = np.zeros((n_batch, n), dtype=bool)
    for j, snap in enumerate(batch_snapshots):
        seen = set()
        for row in snap:
            values = row['values']
            if 'before' not in values or 'after' not in values:
                raise ValueError('观测字段不完整')
            before, after = float(values['before']), float(values['after'])
            if not (math.isfinite(before) and math.isfinite(after)):
                raise ValueError('观测必须为有限数值')
            key = row['source_key']
            if key in seen:
                raise ValueError(f'纸批内纸样编号重复: {key}')
            seen.add(key)
            k = index[key]
            d[j, k] = after - before
            membership[j, k] = True
    if not np.isfinite(d).all():
        raise ValueError('差值必须为有限数值')
    return d, keys, membership


def shard_plan(n_keys, resamples, shard_size=DEFAULT_MC_SHARD):
    """生成检验计划：穷举或 Monte Carlo，以及不重叠的分片区间。

    每个分片 {'index','start','size'}；total 为联合分母（配置总数或抽样总数），
    在计划创建时固定，之后任何重复/晚到分片都无法改变它。
    """
    if n_keys <= EXACT_LIMIT:
        total = 2 ** n_keys
        size = 2048  # 与单批穷举一致的编码块宽度
        plan = [{'index': i, 'start': start, 'size': min(size, total - start)}
                for i, start in enumerate(range(0, total, size))]
        return {'mode': 'exact', 'total': total, 'seed': None, 'shards': plan}
    if not isinstance(shard_size, int) or not (MIN_SHARD <= shard_size <= MAX_SHARD):
        raise ValueError(f'分片大小必须为 {MIN_SHARD} 到 {MAX_SHARD} 的整数')
    if not isinstance(resamples, int) or not (200 <= resamples <= 20000):
        raise ValueError('置换次数必须是200到20000的整数')
    plan = [{'index': i, 'start': start, 'size': min(shard_size, resamples - start)}
            for i, start in enumerate(range(0, resamples, shard_size))]
    return {'mode': 'monte_carlo', 'total': resamples, 'seed': 42, 'shards': plan}


def _observed(d):
    """全正号（观测本身）下的联合与各纸批统计量。"""
    sums = d.sum(axis=1)
    ss = (d * d).sum(axis=1)
    denom_j = np.sqrt(ss)
    t_j = np.divide(sums, denom_j, out=np.zeros_like(sums), where=denom_j > 0)
    total_sum, total_ss = float(d.sum()), float((d * d).sum())
    t_obs = total_sum / math.sqrt(total_ss) if total_ss > 0 else 0.0
    return t_obs, t_j, sums, ss


def _count_block(signs, d, t_obs, t_j_obs):
    """统计一个符号块上联合与各纸批的双侧/定向越界次数。

    signs 形状 (m, n)，元素为 +1/-1；一次配置作用于全部纸批（共享翻转）。
    两侧尾概率都以 |观测值| 为对称阈值（正侧 T≥|T_obs|、负侧 T≤-|T_obs|），
    与观测统计量本身的正负号无关，再由调用方做双侧最大值校正。
    """
    flipped = signs[:, None, :] * d[None, :, :]            # (m, J, n)
    sums = flipped.sum(axis=2)                              # (m, J)
    ss = (flipped * flipped).sum(axis=2)
    denom = np.sqrt(ss)
    t = np.divide(sums, denom, out=np.zeros_like(sums), where=denom > 0)
    joint_t = t.sum(axis=1)
    eps = 1e-12
    abs_t, abs_tj = abs(t_obs), np.abs(t_j_obs)
    return {
        'joint_ge_pos': int(np.sum(joint_t >= abs_t - eps)),
        'joint_le_neg': int(np.sum(joint_t <= -abs_t + eps)),
        'batch_ge_pos': [int(x) for x in np.sum(t >= abs_tj[None, :] - eps, axis=0)],
        'batch_le_neg': [int(x) for x in np.sum(t <= -abs_tj[None, :] + eps, axis=0)],
    }


def _empty_counts(n_batch):
    return {'joint_ge_pos': 0, 'joint_le_neg': 0,
            'batch_ge_pos': [0] * n_batch, 'batch_le_neg': [0] * n_batch}


def _add(acc, part):
    for key in ('joint_ge_pos', 'joint_le_neg'):
        acc[key] += part[key]
    for key in ('batch_ge_pos', 'batch_le_neg'):
        acc[key] = [a + b for a, b in zip(acc[key], part[key])]
    return acc


def compute_shard(d, plan, shard):
    """独立计算单个分片，只返回该片的部分计数（可并发/重入执行）。"""
    n = d.shape[1]
    start, size = shard['start'], shard['size']
    t_obs, t_j_obs, _, _ = _observed(d)
    acc = _empty_counts(d.shape[0])
    if plan['mode'] == 'exact':
        for lo in range(start, start + size, INNER_BLOCK):
            hi = min(lo + INNER_BLOCK, start + size)
            codes = np.arange(lo, hi, dtype=np.uint64)
            signs = 2 * ((codes[:, None] >> np.arange(n, dtype=np.uint64)) & 1).astype(float) - 1
            _add(acc, _count_block(signs, d, t_obs, t_j_obs))
        return acc
    # Monte Carlo：父种子 + 分片序号派生独立子流。规范抽样序列定义为各子流按
    # 分片序号拼接；同一分片无论何时执行、重试几次都生成同一段抽样，与执行顺序
    # 无关，因此中断恢复不会改变联合分母与计数。
    rng = np.random.default_rng(np.random.SeedSequence([plan['seed'], shard['index']]))
    for lo in range(0, size, INNER_BLOCK):
        m = min(INNER_BLOCK, size - lo)
        signs = rng.choice([-1.0, 1.0], size=(m, n))
        _add(acc, _count_block(signs, d, t_obs, t_j_obs))
    return acc


def assemble(d, plan, counts):
    """把各分片部分计数汇总为最终检验结果（只在全部分片就绪后调用一次）。"""
    total = plan['total']
    t_obs, t_j_obs, sums, ss = _observed(d)

    def two_sided_max(ge_pos, le_neg):
        # 双侧最大值校正：两侧尾概率取大再翻倍，截断到 1；对称零假设下等于
        # |T| 越界概率，有限样本不对称时偏保守，不会把工艺优势判得更显著。
        return min(1.0, 2.0 * max(ge_pos / total, le_neg / total))

    if plan['mode'] == 'exact':
        joint_p = two_sided_max(counts['joint_ge_pos'], counts['joint_le_neg'])
        method = '精确枚举（联合对称符号翻转）'
        batch_p = [two_sided_max(p, q) for p, q in zip(counts['batch_ge_pos'], counts['batch_le_neg'])]
    else:
        joint_p = two_sided_max(counts['joint_ge_pos'] + 1, counts['joint_le_neg'] + 1)
        method = 'Monte Carlo 双侧最大值加一校正'
        batch_p = [two_sided_max(p + 1, q + 1)
                   for p, q in zip(counts['batch_ge_pos'], counts['batch_le_neg'])]
    return {
        't_observed': float(t_obs),
        'p_joint': float(joint_p),
        'p_batches': [float(p) for p in batch_p],
        'method': method,
        'total_configurations': int(total),
        'counts': counts,
        'batch_t_observed': [float(x) for x in t_j_obs],
        'batch_sum_d': [float(x) for x in sums],
        'batch_sum_d2': [float(x) for x in ss],
        'plan_mode': plan['mode'],
    }


def reference_exact(d):
    """不分片的整量穷举，仅作测试 oracle 使用。"""
    n = d.shape[1]
    plan = shard_plan(n, resamples=10000)
    assert plan['mode'] == 'exact'
    counts = _empty_counts(d.shape[0])
    for shard in plan['shards']:
        _add(counts, compute_shard(d, plan, shard))
    return assemble(d, plan, counts)
