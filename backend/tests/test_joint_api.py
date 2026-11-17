"""联合检验 HTTP 层集成测试（SQLite + FastAPI TestClient，不起真实 Redis 消费）。

运行：PYTHONPATH=. python -m tests.test_joint_api
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix='joint-api-')
os.environ.setdefault('DATABASE_URL', f'sqlite:///{_TMP}/api.db')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6399/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('ARTIFACT_DIR', _TMP)

from fastapi.testclient import TestClient  # noqa: E402
from src.main import app  # noqa: E402
from src.config.database import Base, engine, Session  # noqa: E402
from src.models.user import User  # noqa: E402
from src.models.study import Study  # noqa: E402
from src.models.observation import Observation  # noqa: E402
from src.utils.security import hash_password  # noqa: E402
from src.services import joint_runs as jr  # noqa: E402

Base.metadata.create_all(engine)

PASS = FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f'  ✓ {name}')
    else:
        FAIL += 1; print(f'  ✗ {name} {detail}')


class FakeRedis:
    def __init__(self): self.items = []; self.counters = {}
    def rpush(self, key, *vals): self.items.extend(str(v) for v in vals); return len(self.items)
    def incr(self, key): self.counters[key] = self.counters.get(key, 0) + 1; return self.counters[key]
    def expire(self, key, seconds): return True
    def ping(self): return True


with Session() as db:
    for email in ('analyst@demo.local', 'viewer@demo.local'):
        if not db.query(User).filter_by(email=email).first():
            db.add(User(email=email, role=email.split('@')[0],
                        password_hash=hash_password('pw')))
    for j, keys in enumerate([['A1', 'A2', 'A3'], ['A2', 'A3', 'A4']]):
        s = Study(name=f'API纸批{j}', source_filename=f'f{j}.csv', description='')
        db.add(s); db.flush()
        for k in keys:
            db.add(Observation(study_id=s.id, source_key=k,
                               values={'before': 5.0, 'after': 4.0}))
    db.commit()
    study_ids = [s.id for s in db.query(Study).order_by(Study.id).all()]

jr._redis = lambda: FakeRedis()
from src.middlewares import rate_limit  # noqa: E402
rate_limit.redis = FakeRedis()
from src.services import runs as runs_svc  # noqa: E402
runs_svc.Redis = type('R', (), {'from_url': staticmethod(lambda url: FakeRedis())})
client = TestClient(app)

# 鉴权
r = client.post('/auth/login', json={'email': 'analyst@demo.local', 'password': 'pw'})
check('分析师登录', r.status_code == 200, r.text)
token = r.json()['token']
H = {'Authorization': f'Bearer {token}'}

r = client.post('/auth/login', json={'email': 'viewer@demo.local', 'password': 'pw'})
viewer_headers = {'Authorization': f'Bearer {r.json()["token"]}'}

# overview 聚合了 joint_runs 且不破坏原字段
r = client.get('/overview', headers=H)
check('overview 含 joint_runs 与原 runs/studies',
      r.status_code == 200 and 'joint_runs' in r.json() and 'runs' in r.json() and 'studies' in r.json())

# 未鉴权拒绝
r = client.post('/joint-runs', json={'name': 'x', 'study_ids': study_ids})
check('未登录拒绝登记', r.status_code == 401)

# viewer 只读
r = client.post('/joint-runs', headers=viewer_headers,
                json={'name': '观察员越权', 'study_ids': study_ids})
check('观察员不能登记联合检验', r.status_code == 403)

# 参数校验
r = client.post('/joint-runs', headers=H, json={'name': 'x', 'study_ids': study_ids[:1]})
check('单纸批 422', r.status_code == 422)
r = client.post('/joint-runs', headers=H,
                json={'name': '联合API检验', 'study_ids': study_ids, 'parameters': {'bad': 1}})
check('未知参数 422', r.status_code == 422, r.text)

# 正常登记（worker 不消费，分片保持 queued；直接驱动 service 完成模拟）
r = client.post('/joint-runs', headers=H,
                json={'name': '联合API检验', 'study_ids': study_ids})
check('登记 200 且冻结整组', r.status_code == 200, r.text)
joint_id = r.json()['id']
check('返回体含对齐信息与分片',
      r.json()['alignment']['union_size'] == 4 and len(r.json()['shards']) >= 1)

r = client.get(f'/joint-runs/{joint_id}', headers=H)
check('可查询联合运行', r.status_code == 200 and r.json()['status'] == 'queued')
r = client.get('/joint-runs', headers=H)
check('联合运行列表', r.status_code == 200 and any(x['id'] == joint_id for x in r.json()))
r = client.get('/joint-runs/999999', headers=H)
check('不存在 404', r.status_code == 404)

# 模拟 worker 逐片处理，验证 API 最终发布的结果结构
with Session() as db:
    run = jr.get(db, joint_id)
    for s in run['plan']['shards']:
        jr.process_shard(db, joint_id, s['index'])
r = client.get(f'/joint-runs/{joint_id}', headers=H)
body = r.json()
check('API 发布 completed', body['status'] == 'completed')
res = body['result']
check('结果含校正 p、原始单批 p、纳入范围',
      '联合校正后双侧p' in res['metrics']
      and len(res['batch_metrics']) == 2
      and all('原始单批双侧p' in b for b in res['batch_metrics'])
      and res['inclusion_rows'][0] == ['A1', '纳入', '缺席'])
check('结果含联合对称假设说明', '联合对称假设' in res['caveat'])

# 单批链路回归：登记单批运行（不消费队列也应可创建并查询）
r = client.post('/runs', headers=H,
                json={'study_id': study_ids[0], 'name': '单批回归', 'parameters': {}})
check('单批运行登记不受影响', r.status_code == 200 and 'id' in r.json(), r.text)
r = client.get('/runs', headers=H)
check('单批运行列表仍可用', r.status_code == 200 and len(r.json()) >= 1)

print(f'\n结果：{PASS} 通过 / {FAIL} 失败')
raise SystemExit(1 if FAIL else 0)
