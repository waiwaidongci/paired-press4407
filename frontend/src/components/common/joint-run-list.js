import {store} from '../../stores/research.js';
import {table} from './table.js';
import {status} from './status.js';
import {esc} from '../../utils/format.js';
export function jointRunList(){return table(['编号 / 联合运行','状态','纸批 × 分片','校验和'],store.jointRuns.map(r=>[{html:`<a href="#joint/${r.id}">#${r.id} ${esc(r.name)}</a>`},{html:status(r.status)},`${r.batches.length}批 · ${r.plan.shards.length}片 · 完成 ${r.shards.filter(s=>s.status==='completed').length} 片`,r.checksum.slice(0,12)]));}
