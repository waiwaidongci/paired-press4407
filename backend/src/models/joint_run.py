from sqlalchemy import Column, Integer, String, JSON, Text, ForeignKey, UniqueConstraint
from src.config.database import Base


class JointRun(Base):
    """跨纸批联合检验运行。

    创建时一次性冻结全部入选纸批的快照与参数（同一时点的整组输入），
    分片计划与联合分母同时固定；只有全部分片成功后才写入 result，
    任何分片失败整组 failed，不存在半组结论。
    """
    __tablename__ = 'joint_runs'
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    status = Column(String(20), default='queued')
    parameters = Column(JSON, nullable=False)
    # [{'study_id','study_name','snapshot':[...],'checksum':'...'}]
    batches = Column(JSON, nullable=False)
    # 对齐后的纸样编号并集，以及每批纳入/缺席标记
    alignment = Column(JSON, nullable=False)
    plan = Column(JSON, nullable=False)
    checksum = Column(String(64), nullable=False)
    result = Column(JSON, nullable=True)
    error = Column(Text, default='')
    created_at = Column(String(40), nullable=False)


class JointShard(Base):
    """联合检验的一个计算分片，部分计数独立落库、可幂等重算。"""
    __tablename__ = 'joint_shards'
    __table_args__ = (UniqueConstraint('joint_run_id', 'shard_index'),)
    id = Column(Integer, primary_key=True)
    joint_run_id = Column(Integer, ForeignKey('joint_runs.id'), nullable=False)
    shard_index = Column(Integer, nullable=False)
    start = Column(Integer, nullable=False)
    size = Column(Integer, nullable=False)
    status = Column(String(20), default='queued')
    counts = Column(JSON, nullable=True)
    error = Column(Text, default='')
    updated_at = Column(String(40), nullable=False)
