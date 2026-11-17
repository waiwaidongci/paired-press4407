import os,json,logging
from pathlib import Path
from redis import Redis
from sqlalchemy import select
from src.config.database import Session
from src.models.run import Run
from src.models import user,study,observation,review,audit,joint_run
from src.services.algorithm import calculate
from src.services import joint_runs
from src.utils.serializers import checksum

redis=Redis.from_url(os.environ['REDIS_URL'])
ARTIFACT_DIR=Path(os.environ.get('ARTIFACT_DIR','/app/artifacts'))


def recover_jobs(db):
    """启动恢复：单批 queued/running 重新入队；联合分片重入且已存片不重计。"""
    pending=list(db.scalars(select(Run).where(Run.status.in_(['queued','running']))))
    for run in pending: redis.rpush('analysis-queue',run.id)
    joint_runs.recover(redis,db)


def process_single(db,id):
    run=db.get(Run,id)
    if not run or run.status in ('completed','failed'): return
    run.status='running';db.commit()
    try:
        if checksum(run.snapshot)!=run.checksum: raise ValueError('输入快照校验失败')
        result=calculate(run.snapshot,run.parameters)
        result['input_sha256']=run.checksum
        run.result=result;run.status='completed'
        (ARTIFACT_DIR/f'run-{id}.json').write_text(json.dumps(result,ensure_ascii=False,allow_nan=False))
    except Exception as e:
        logging.exception('analysis failed');run.status='failed';run.error=str(e)
    db.commit()


def process_joint(db,run_id,shard_index):
    try:
        joint_runs.process_shard(db,run_id,shard_index)
    except Exception:
        # process_shard 已把分片与整组运行置为 failed 并落库，此处仅记录日志。
        logging.exception('joint shard failed: run=%s shard=%s',run_id,shard_index)


def main():
    ARTIFACT_DIR.mkdir(exist_ok=True)
    # Recover jobs interrupted after dequeue. A project runs exactly one worker.
    with Session() as db:
        recover_jobs(db)
    while True:
        Path('/tmp/worker-heartbeat').touch()
        # 两个队列轮流等待，任一队列有任务即处理；联合分片之间相互独立可并发计算。
        job=redis.blpop('analysis-queue','joint-analysis-queue',timeout=2)
        if not job: continue
        queue,payload=job[0].decode(),job[1]
        with Session() as db:
            if queue=='analysis-queue':
                process_single(db,int(payload))
            else:
                run_id,shard_index=map(int,payload.decode().split(':'))
                process_joint(db,run_id,shard_index)


if __name__=='__main__':
    main()
