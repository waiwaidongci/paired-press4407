import {DOMAIN} from '../constants/domain.js';
import {store,load} from '../stores/research.js';
import {jointRuns} from '../api/research.js';
import {jointRunList} from '../components/common/joint-run-list.js';
import {bindForm} from '../hooks/forms.js';
import {table} from '../components/common/table.js';
import {status} from '../components/common/status.js';
import {esc,fmt} from '../utils/format.js';

const DEFAULTS=DOMAIN.joint_defaults;

export async function render(id){
  const writable=localStorage.getItem('research-role')!=='viewer';
  let detail='';
  if(id){
    const r=await jointRuns.get(id);
    store.jointRunId=r.id;
    detail=renderDetail(r);
  }
  return `<div class="eyebrow">06 / JOINT BATCH TEST</div>
<h1>跨纸批联合符号翻转检验</h1>
<p class="lead">${esc(DOMAIN.joint_algorithm)}</p>
<div class="method-note">按纸样编号对齐多个纸批：同一张纸在所有纸批中共享同一次符号翻转，缺席保持缺席；联合统计量 T=Σ差值和/√Σ差值平方和，双侧取两侧尾概率最大值再翻倍校正。联合分母在登记时固定，分片独立计算、幂等重入，全部纸批与留档核对通过后一次性发布；任一分片失败则整组失败、无半组结论。</div>
${writable?`<form id="joint-form" class="worksheet">
<label>联合研究名称<input name="name" required minlength="2" value="跨批联合检验 · ${new Date().toISOString().slice(0,10)}"></label>
<fieldset><legend>选择至少两个纸批（整组输入取同一时点冻结）</legend><div class="inline-form">${store.studies.map(s=>`<label class="com"><input type="checkbox" name="study_ids" value="${s.id}"> ${esc(s.name)} · ${s.row_count}条</label>`).join('')}</div></fieldset>
<div class="inline-form">${Object.entries(DEFAULTS).map(([k,v])=>`<label>${k==='resamples'?'重采样次数 (200–20000，n>18时生效)':'Monte Carlo 分片大小 (100–10000)'}<input name="${k}" type="number" step="1" value="${v}" required></label>`).join('')}</div>
<button type="submit">冻结整组输入并排队联合检验 →</button>
</form>`:'<p class="method-note">观察员可阅读联合检验记录，分析师与负责人可以发起。</p>'}
<h2>联合检验记录</h2>${jointRunList()}
${detail}`;
}

function shardTable(r){
  return table(['分片','区间起点','大小','状态','错误'],r.shards.map(s=>[s.shard_index,s.start,s.size,{html:status(s.status)},s.error||'—']));
}

function renderDetail(r){
  if(r.status==='failed')return `<h2>#${r.id} ${esc(r.name)} ${status(r.status)}</h2><div class="error">联合检验失败：${esc(r.error)}。已完成分片的部分计数保留，但不发布任何半组结论；请修复后重新登记联合检验。</div><h2>分片状态</h2>${shardTable(r)}`;
  if(!r.result)return `<h2>#${r.id} ${esc(r.name)} ${status(r.status)}</h2><p class="method-note">分片计算中（${r.shards.filter(s=>s.status==='completed').length}/${r.shards.length} 片完成）。此页每2秒更新，全部纸批与留档核对后一次性发布。</p><h2>分片状态</h2>${shardTable(r)}`;
  const res=r.result;
  return `<h2>#${r.id} ${esc(r.name)} ${status(r.status)}</h2>
<div class="checksum">INPUT SHA256 ${r.checksum}</div>
<section class="result-intro"><strong>${esc(res.interpretation)}</strong><p>${esc(res.caveat)}</p></section>
<h2>联合检验核心数值（校正后）</h2>
${table(['业务指标','计算值'],Object.entries(res.metrics).map(([k,v])=>[k,fmt(v)]))}
<h2>各纸批原始单批结果（保留原口径，仅供对照，勿挑最小值）</h2>
${table(['纸批','纳入纸样数','平均差(新减原)','差值和','差值平方和','单批观测T','原始单批双侧p'],res.batch_metrics.map(b=>[b.study_name,b['纳入纸样数'],b['平均差(新减原)'],b['差值和'],b['差值平方和'],b['单批观测T'],b['原始单批双侧p']]))}
<h2>${esc(res.table_title)}</h2>
${table(res.inclusion_columns,res.inclusion_rows)}
<h2>分片执行与纳入范围</h2>${shardTable(r)}
<details><summary>冻结的参数、计划与各纸批快照</summary><pre>${esc(JSON.stringify({parameters:r.parameters,plan:r.plan,batches:r.batches},null,2))}</pre></details>`;
}

export function bind(refresh){
  bindForm('joint-form',async ()=>{
    const form=document.getElementById('joint-form');
    const fd=new FormData(form);
    const study_ids=fd.getAll('study_ids').map(Number);
    if(study_ids.length<2)throw new Error('联合检验至少选择两个纸批');
    const parameters=Object.fromEntries(Object.keys(DEFAULTS).map(k=>[k,+fd.get(k)]));
    const r=await jointRuns.create({study_ids,name:fd.get('name'),parameters});
    store.jointRunId=r.id;
    await load();
    location.hash='joint/'+r.id;
  });
}
