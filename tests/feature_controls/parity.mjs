import { approvalMessage } from '@manolii/feature-controls';
import { bundle, golden, numericStickyProbe, setup } from './helpers.mjs';
import { readFileSync } from 'node:fs';
globalThis.fetch = () => { throw new Error('offline evaluation attempted network'); };
const runtime=await setup();
const decisions=[];
for(let i=0;i<1000;i++) {
  const d=await runtime.evaluate('experiment','web',{...golden.context,assignment_key:`unit-${i}`},{preview:true,now:golden.now});
  decisions.push({value:d.value,reason:d.reason,configuration_revision:d.configuration_revision,kill_generation:d.kill_generation});
}
const sticky=[];
for(const [epoch,version] of JSON.parse(readFileSync(new URL('./sticky-numerics.json',import.meta.url)))) {
  try { sticky.push(await numericStickyProbe(epoch,version)); }
  catch { sticky.push({epoch,version,status:'rejected'}); }
}
console.log(JSON.stringify({decisions,sticky,approval_bytes:Buffer.from(approvalMessage(bundle())).toString('utf8')}));
