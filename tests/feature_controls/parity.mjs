import { approvalMessage } from '@manolii/feature-controls';
import { bundle, golden, setup } from './helpers.mjs';
globalThis.fetch = () => { throw new Error('offline evaluation attempted network'); };
const runtime=await setup();
const decisions=[];
for(let i=0;i<1000;i++) {
  const d=await runtime.evaluate('experiment','web',{...golden.context,assignment_key:`unit-${i}`},{preview:true,now:golden.now});
  decisions.push({value:d.value,reason:d.reason,configuration_revision:d.configuration_revision,kill_generation:d.kill_generation});
}
console.log(JSON.stringify({decisions,approval_bytes:Buffer.from(approvalMessage(bundle())).toString('utf8')}));
