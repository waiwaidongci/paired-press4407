# 栖木印刷配对试验簿

同纸张两工艺成对色差→配对分析→试印复核；枚举n<=18，以上固定seed Monte Carlo加一校正。原生 HTML/CSS/JavaScript 多模块工作席，所有计算由 Python 完成。除原单批检验外，提供跨纸批联合符号翻转检验（`#joint` 页面），避免“多批分别检验再挑最小 p”对工艺优势的误判。

## 模拟业务
栖木研究协作组，12名分析师与业务人员，日常使用 `paired-press-202611-1.csv` 手工抄表。当前三个痛点：观测与口径不可追溯、重算易出错、结果与复核割裂。上游提供独立合成业务观测；本应用接管数据登记、方法执行和输入冻结；下游负责人形成复测/预算方案，观察员获取复核后的CSV。

核心业务实体：PressStudy / MatchedPair / PairedRun / Review；联合检验扩展 JointRun / JointShard。源码 model/schema/service/controller/route 与前端 api/store/pages 完成链路，不依赖浏览器本地假数据。

## 功能与方法
- 试印研究、成对纸样录入/编辑/删除；重名记录与非法数值明确拒绝。
- 配对差均值与精确符号翻转置换检验（单批，原口径保留）。同一纸张上成对测量；小样本枚举全部符号，较大样本使用固定种子 Monte Carlo 与加一校正。
- 跨纸批联合符号翻转检验：
  - 按纸样编号对齐到并集，同一张纸在所有纸批中共享同一次符号翻转；某纸批缺席的纸样在该批保持缺席（贡献为0）。
  - 联合统计量 T=Σ差值和 / √Σ差值平方和（跨全部纸批求和）。联合对称假设：零假设下各纸批差值均关于0对称且纸批间相互独立，符号可交换翻转。
  - 双侧 p 取正、负两侧尾概率的最大值再乘2（截断为1），对称分布下等价于 |T| 越界概率，有限样本下偏保守，不夸大优势。
  - 整组输入在同一时点冻结并登记，登记动作不阻塞其他录入；检验计划与联合分母（穷举配置数或抽样总数）创建时固定。分片独立计算、同一条规范抽样序列，中断恢复不重计已存片，重复或晚到分片不改变联合分母。
  - 全部纸批与留档校验通过后一次性发布；任一分片失败则整组 failed，无半组结论。结果页同时展示联合校正 p、各纸批原始单批 p 与逐纸样纳入/缺席范围。
- 每次运行冻结参数、观测和SHA256。Redis任务由独立worker计算，写DB与共享产物卷；输入修改不会改写历史结果。
- 负责人复核不可覆盖；审计记录登录、数据改动、排队、复核与导出；240请求/分钟限流、全局可恢复错误。

## 快速启动
```bash
cp .env.example .env
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```
访问 http://localhost:18242 ，API http://localhost:19242/docs 。数据库映射 17742。无需Node构建。可以从中文和带空格目录启动。清理 `docker compose down -v --remove-orphans` 会删除本题演示数据卷。

|角色|账号|密码|权限与典型动作|
|---|---|---|---|
|admin 负责人|admin@demo.local|Research2026!|全部读写、批准复核、下载|
|analyst 分析师|analyst@demo.local|Research2026!|数据修改、创建任务、读取结果及下载；不能复核|
|viewer 观察员|viewer@demo.local|Research2026!|只读；仅可导出已复核结果|

## 一天的工作剧本
1. 09:00 admin登录，查“栖木·米白纸套印”。
2. 09:10 analyst核对 `paired-press-202611-1.csv` 的seed记录。
3. 09:15 来源录入页检查 `原工艺色差 ΔE` 等业务字段，必要时编辑。
4. 09:20 分析执行页保留默认参数 `{"resamples": 10000}`，命名本轮研究。
5. 09:21 冻结输入并排队，worker实算，结果页自动刷新。
6. 09:25 阅读核心数值、方法边界、明细与SHA256；发现失败先修数据重跑。
7. 09:35 admin打开业务复核，写明限制与下一步动作，归档后不可覆盖。
8. 09:40 viewer读取复核意见并下载CSV，在操作留痕页核对下载事件。

## 技术与目录
|部分|实现|
|---|---|
|前端|原生ES modules、HTML/CSS、Nginx1.27，无前端框架|
|API|Python3.11、FastAPI0.115、SQLAlchemy2、JWT、PBKDF2密码|
|计算|NumPy2.2、SciPy1.15，固定seed42|
|数据库|PostgreSQL16（含 joint_runs、joint_shards 两张联合检验表，启动自动建表）|
|任务|Redis7持久队列，Python独立worker双队列（单批 analysis-queue / 联合 joint-analysis-queue），心跳healthcheck|

`frontend/src/`分api、stores、types、components/common、hooks、pages、router、utils、constants；`backend/src/`分config、models、schemas、services、controllers、routes、middlewares、utils、constants；`backend/scripts/seed_demo.py`自然键插入、重复不覆盖修改；`backend/migrations/README.md`说明schema版本。

环境变量 `.env.example`：COMPOSE_PROJECT_NAME、FRONTEND_PORT、BACKEND_PORT、DB_PORT、DB_PASSWORD、JWT_SECRET、SEED_DEMO。开发时在本地venv安装backend/requirements.txt，设置DATABASE_URL、REDIS_URL、JWT_SECRET再运行uvicorn；前端开发经Nginx代理 `/api/` 到不带前缀的API。

共享枚举：角色admin/analyst/viewer和队列queued/running/completed/failed；后端`src/constants/enums.py`、认证与worker使用相同值；前端`src/types/enums.js`、状态与角色界面使用相同值。Google Fonts：Noto Serif SC + IBM Plex Mono；断网使用serif/monospace回退。

## 算法输入、失败态与复现
单批输入为选定批次全部观测和参数，输出核心数值、明细、解释；参数经后端严格白名单和范围验证。同一纸张上成对测量；小样本枚举全部符号，较大样本使用固定种子 Monte Carlo 与加一校正。空批次拒绝；不完整字段/非有限数/非法计数拒绝；常量相关列与不足样本使任务failed并保留错误。固定seed42、输入checksum相同可复现。独立oracle：小n穷举手算；交换两列统计量变号p相同；缺失配对拒绝。

联合检验输入为至少两个纸批在同一时点冻结的整组快照，按纸样编号并集对齐。n≤18 精确枚举全部 2**n 个共享符号配置，n>19 走固定种子 Monte Carlo（加一校正），n=18/19 为枚举/模拟分界。分片区间在登记时写死、联合分母固定；单片失败即整组 failed 且不写 result，发布前再做一次全量留档校验，失败同样整组废弃。恢复时未完成分片重新入队、已完成分片幂等跳过；若全部片已完成但发布动作中断，worker 启动恢复直接汇总已存片完成发布。验证见 `backend/TESTING.md`。

## 测试
后端测试不依赖 PostgreSQL/Redis（SQLite 内存库 + 内存假 Redis），在 `backend/` 下执行：
```bash
PYTHONPATH=. python -m tests.test_joint        # 算法 oracle、对齐/缺席、分片幂等、崩溃恢复、失败无半组（58项）
PYTHONPATH=. python -m tests.test_joint_api    # 路由/鉴权/参数校验/单批回归（16项）
PYTHONPATH=. python -m tests.test_worker_loop  # 双队列 worker、恢复重入、重复消息、失败路径（7项）
```
小样本联合 p 用独立蛮力枚举（不经过分片代码路径）逐组核对；另测重复分片、晚到分片、发布前崩溃与快照篡改。

已知边界：小型本地研究应用，使用合成数据，不含企业SSO与外发邮件；结果由人工复核，不自动执行业务决策；单worker恢复queued/running任务，数据库与Redis间未采用分布式事务。以当前测试目录TESTING.md记录实际验证结果。

MIT License。
