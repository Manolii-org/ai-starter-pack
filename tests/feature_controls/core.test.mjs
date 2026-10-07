import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import assert from 'node:assert/strict';
import test from 'node:test';
import { FeatureRuntime, approvalMessage, killMessage } from '@manolii/feature-controls';
import { DecisionClient, decisionValue, validateSnapshot } from '@manolii/feature-controls/client';
import { Assignments, Controls, Events, bundle, clone, eventFor, golden, kill, setup } from './helpers.mjs';

test('shared golden hard-fence and typed baseline decisions', async () => {
  const runtime = await setup();
  for (const fixture of golden.cases) {
    const context = {...golden.context,surface_id:fixture.surface_id};
    for (const key of ['excluded','eligible','authorized']) if (key in fixture) context[key]=fixture[key];
    const d = await runtime.evaluate(fixture.key, fixture.surface_id, context, {now:golden.now});
    assert.equal(d.value, fixture.expected_value); assert.equal(d.reason, fixture.expected_reason);
  }
});
test('activation requires durable controls/approval; exact message verification and CAS replay', async () => {
  await assert.rejects(new FeatureRuntime(golden.catalog).activate(bundle(),0,golden.now));
  const controls=new Controls();
  await assert.rejects(new FeatureRuntime(golden.catalog,{controls}).activate(bundle(),0,golden.now));
  let calls=0;
  const runtime=new FeatureRuntime(golden.catalog,{controls,verifier:{verify:async (message,ref,purpose)=>{
    calls++; return purpose==='release' && ref==='local-fixture' && Buffer.from(message).equals(Buffer.from(approvalMessage(bundle())));
  }}});
  await runtime.activate(bundle(),0,golden.now); assert.equal(calls,1);
  await assert.rejects(runtime.activate(bundle(),0,golden.now));
  const outcomes=await Promise.allSettled([runtime.activate(bundle(golden.payload,2),1,golden.now),runtime.activate(bundle(golden.payload,3),1,golden.now)]);
  assert.ok(outcomes.every(x=>x.status==='rejected')); // Approval must bind the changed revision too.
  assert.equal(controls.writes,1);
});
test('local trust cannot write to a store lacking the test-only marker',()=>{
  assert.throws(()=>new FeatureRuntime(golden.catalog,{trust_policy:'local-test',controls:{durable:true}}));
});
test('complete native payload rejects retention, scope/digest/type/Unicode tampering', async () => {
  for (const mutate of [p=>delete p.savedGroups,p=>delete p.features.child,p=>p.features.release.rules[0].force='wrong',
    p=>p.features.experiment.rules[0].seed='😀',p=>p.features.experiment.rules[0].condition={id:'x'},p=>p.experiments.push({key:'auto'})]) {
    const p=clone(golden.payload);mutate(p);
    await assert.rejects(new FeatureRuntime(golden.catalog,{trust_policy:'local-test'}).activate(bundle(p),0,golden.now));
  }
  const runtime=await setup();
  await assert.rejects(runtime.activate({...bundle(golden.payload,2),payload_sha256:'0'.repeat(64)},1,golden.now));
  await assert.rejects(runtime.activate(bundle(golden.payload,2,{...golden.scope,environment_id:'other'}),1,golden.now));
  for (const assignment_key of ['😀','é','x/y','', 'x'.repeat(129), 'x\n']) await assert.rejects(runtime.evaluate('release','web',{...golden.context,assignment_key},{now:golden.now}));
  assert.equal((await runtime.evaluate('release','web',golden.context,{now:golden.now})).value,true);
});
test('ancestor cycles and unknown ancestors reject catalog',()=>{
  for (const ancestor of ['child','missing']) {
    const c=clone(golden.catalog);c.features.child.ancestors=[ancestor];
    assert.throws(()=>new FeatureRuntime(c,{trust_policy:'local-test'}));
  }
});
test('independent monotonic kills survive new release sequence and scoped store',async()=>{
  const controls=new Controls();const runtime=await setup({controls});
  await runtime.updateKills(kill(2,['release']),'local-fixture',golden.now);
  await runtime.activate(bundle(golden.payload,2),1,golden.now);
  assert.equal((await runtime.evaluate('child','web',golden.context,{now:golden.now})).reason,'disabled_or_excluded');
  await assert.rejects(runtime.updateKills(kill(1,[]),'local-fixture',golden.now));
  const other={...golden.scope,environment_id:'other'};
  const catalog={...clone(golden.catalog),scope:other};
  const second=new FeatureRuntime(catalog,{controls,trust_policy:'local-test'});
  await second.activate(bundle(golden.payload,1,other),0,golden.now);
  await second.updateKills(kill(1,[],golden.expires_at,other),'local-fixture',golden.now);
  assert.equal((await second.evaluate('child','web',{...golden.context,scope:other},{now:golden.now})).value,'enabled');
});
test('signed creation-time skew boundary is bounded at thirty seconds',async()=>{
  const almost={...bundle(),created_at:golden.now+29999};
  const raw=Buffer.from(almost.payload_bytes);almost.payload_sha256=(await import('node:crypto')).createHash('sha256').update(raw).digest('hex');
  await new FeatureRuntime(golden.catalog,{trust_policy:'local-test'}).activate(almost,0,golden.now);
  await assert.rejects(new FeatureRuntime(golden.catalog,{trust_policy:'local-test'}).activate({...almost,created_at:golden.now+30001},0,golden.now));
});
test('expiry, local disable dominance, context loss, stale kills and safe baseline/deny',async()=>{
  const runtime=await setup({local_disabled:['release']});
  assert.equal((await runtime.evaluate('release','web',golden.context,{now:golden.now})).value,false);
  assert.equal((await runtime.evaluate('child','web',golden.context,{now:golden.now})).status,'denied');
  const r=await setup();
  await r.updateKills(kill(2,[],2000),'local-fixture',golden.now);
  assert.equal((await r.evaluate('release','web',golden.context,{now:2000})).reason,'kills_expired');
  assert.equal((await r.evaluate('child','web',{...golden.context,expires_at:1000},{now:1000})).status,'denied');
  const expired=await setup({require_fresh_kills:false});
  assert.equal((await expired.evaluate('release','web',{...golden.context,expires_at:200000},{now:100000})).reason,'bundle_expired');
});
test('preview writes/callbacks zero; sticky store required; weights do not rebucket',async()=>{
  const assignments=new Assignments(),events=new Events();const r=await setup({assignments,events});
  const preview=await r.evaluate('experiment','web',golden.context,{now:golden.now,preview:true});
  assert.equal(preview.reason,'preview');assert.equal(assignments.writes,0);assert.equal(events.values.size,0);
  const noStore=await setup();assert.equal((await noStore.evaluate('experiment','web',golden.context,{now:golden.now})).reason,'assignment_store_required');
  const d=await r.evaluate('experiment','web',golden.context,{now:golden.now});assert.equal(assignments.writes,1);
  const p=clone(golden.payload);p.features.experiment.rules[0].weights=d.value==='control'?[0,1]:[1,0];
  await r.activate(bundle(p,2),1,golden.now);
  const after=await r.evaluate('experiment','web',golden.context,{now:golden.now});
  assert.equal(after.value,d.value);assert.equal(after.assignment.assignment_id,d.assignment.assignment_id);
  assert.equal(events.values.size,0);
});
test('explicit exposure/outcome separation, deduplication and forged/preview receipts',async()=>{
  const events=new Events();const r=await setup({assignments:new Assignments(),events});
  const d=await r.evaluate('experiment','web',golden.context,{now:golden.now});
  await assert.rejects(r.recordEvent(eventFor(d,'outcome','outcome-1'),golden.now));
  assert.equal(await r.recordEvent(eventFor(d),golden.now),true);
  assert.equal(await r.recordEvent(eventFor(d),golden.now),false);
  assert.equal(await r.recordEvent(eventFor(d,'outcome','outcome-1'),golden.now),true);
  await assert.rejects(r.recordEvent({...eventFor(d),variant:'forged'},golden.now));
  await assert.rejects(r.recordEvent({...eventFor(d),decision_id:'forged'},golden.now));
  await assert.rejects(r.recordEvent(eventFor(d),d.expires_at));
});
test('wire projection + dependency-light client baseline, scope, expiry and session switching',async()=>{
  const r=await setup();const snapshot=await r.snapshot(['release','missing'],'web',golden.context,{now:golden.now});
  assert.deepEqual(Object.keys(snapshot).sort(),[...Object.keys(golden.scope),'application_id','context_scope','configuration_revision','kill_generation','time_highwater','decisions','expires_at','generated_at','schema_version','surface_id'].sort());
  const expected={...golden.scope,application_id:golden.context.application_id,surface_id:'web',context_scope:golden.context.context_scope};
  const minimum={configuration_revision:0,kill_generation:0,time_highwater:0};
  assert.equal(validateSnapshot(snapshot,expected,golden.now,minimum),true);
  assert.equal(validateSnapshot({...snapshot,raw_context:{}},expected,golden.now,minimum),false);
  assert.equal(validateSnapshot({...snapshot,decisions:{release:{...snapshot.decisions.release,assignment_id:'private'}}},expected,golden.now,minimum),false);
  assert.equal(decisionValue(snapshot,'release',false,expected,golden.now,minimum),true);
  assert.equal(decisionValue(snapshot,'unknown',false,expected,golden.now,minimum),false);
  assert.equal(decisionValue(snapshot,'release','baseline',expected,golden.now,minimum),'baseline');
  assert.equal(decisionValue(snapshot,'release',false,expected,snapshot.expires_at,minimum),false);
  assert.equal(decisionValue({...snapshot,schema_version:2},'release',false,expected,golden.now,minimum),false);
  const client=new DecisionClient(expected,minimum);client.bindSession('account-1',golden.context.context_scope);assert.equal(client.setSnapshot(snapshot,'account-1',golden.now),true);
  assert.equal(client.get('release',false,golden.now),true);client.bindSession('account-2','account-2-context');assert.equal(client.get('release',false,golden.now),false);
  assert.equal(client.setSnapshot(snapshot,'account-1',golden.now),false);client.bindSession(null,null);
});

const vectors=JSON.parse(readFileSync(new URL('./hardening.json',import.meta.url)));
const minimum={configuration_revision:0,kill_generation:0,time_highwater:0};
const wireScope={...golden.scope,application_id:golden.context.application_id,surface_id:'web',context_scope:golden.context.context_scope};
async function custom(catalog, options={}) {
  const r=new FeatureRuntime(catalog,{trust_policy:'local-test',...options});
  await r.activate(bundle(),0,golden.now);await r.updateKills(kill(),'local-fixture',golden.now);return r;
}
test('runtime and public catalog cannot be mutated after approval',async()=>{
  const c=clone(golden.catalog),r=await custom(c);
  c.features.child.ancestors=[];
  assert.throws(()=>r.catalog.features.child.ancestors.splice(0));
  assert.throws(()=>{r.catalog=clone(golden.catalog);});
  await r.updateKills(kill(2,['release']),'local-fixture',golden.now);
  assert.equal((await r.evaluate('child','web',golden.context,{now:golden.now})).status,'denied');
});
test('native payload objects ignore inherited executable fields',async()=>{
  const r=await setup();await r.evaluate('release','web',golden.context,{now:golden.now});
  const p=clone(golden.payload);p.features.release={defaultValue:false};await r.activate(bundle(p,2),1,golden.now);
  for(const [key,value]of Object.entries({rules:[{force:true}],defaultValue:true,condition:{groups:{$in:['never']}},force:true,variations:[true],savedGroups:{unsafe:['unit-0']},contextualBandits:{unsafe:{}},features:{release:{defaultValue:true}}})) {
    const previous=Object.getOwnPropertyDescriptor(Object.prototype,key);
    try {
      Object.defineProperty(Object.prototype,key,{value,configurable:true});
      assert.equal((await r.evaluate('release','web',golden.context,{now:golden.now})).value,false,key);
    } finally {if(previous)Object.defineProperty(Object.prototype,key,previous);else delete Object.prototype[key];}
  }
});
test('persisted time floor rejects expiry resurrection across runtime restart',async()=>{
  const controls=new Controls(),r=await setup({controls});
  await r.updateKills(kill(2,[],golden.now+10),'local-fixture',golden.now);
  assert.equal((await r.evaluate('release','web',golden.context,{now:golden.now+10})).status,'denied');
  for(const runtime of [r,new FeatureRuntime(golden.catalog,{controls,trust_policy:'local-test'})])
    assert.equal((await runtime.evaluate('release','web',golden.context,{now:golden.now+1})).reason,'controls_unavailable');
  assert.equal((await controls.read(golden.scope)).time_highwater,golden.now+10);
  await assert.rejects(r.activate(bundle(golden.payload,2),1,golden.now+1),/clock rollback/);
});
test('captured snapshot is atomic even when store changes between awaits',async()=>{
  const controls=new Controls(),r=await setup({controls});
  const first=await controls.read(golden.scope),next={...clone(first),bundle:bundle(golden.payload,2),kill:kill(2,['release'])};
  let reads=0;controls.read=async()=>clone(++reads===1?first:next);
  const s=await r.snapshot(['release','child'],'web',golden.context,{now:golden.now});
  assert.equal(reads,1);assert.equal(s.configuration_revision,1);assert.equal(s.kill_generation,1);
  for(const d of Object.values(s.decisions)){assert.equal(d.status,'resolved');assert.equal(d.configuration_revision,1);assert.equal(d.kill_generation,1);}
  assert.equal((await r.evaluate('child','web',golden.context,{now:golden.now})).status,'denied');
  await assert.rejects(r.snapshot(Array(65).fill('release'),'web',golden.context,{now:golden.now}));
});
test('context and preview options are captured before store suspension',async()=>{
  const assignments=new Assignments(),controls=new Controls(),r=await setup({assignments,controls});
  const saved=await controls.read(golden.scope);let release;
  controls.read=()=>new Promise(resolve=>{release=()=>resolve(clone(saved));});
  const ctx=clone(golden.context),options={now:golden.now,preview:true},pending=r.evaluate('experiment','web',ctx,options);
  ctx.assignment_key='different-account';ctx.application_id='different-app';options.preview=false;
  release();assert.equal((await pending).reason,'preview');assert.equal(assignments.writes,0);
});
test('default app isolation and declared shared-boundary continuity',async()=>{
  const c=clone(golden.catalog);c.applications.push('second-app','outside-app');
  const assignments=new Assignments(),r=await custom(c,{assignments});
  const a=await r.evaluate('experiment','web',golden.context,{now:golden.now});
  const b=await r.evaluate('experiment','web',{...golden.context,application_id:'second-app'},{now:golden.now});
  assert.notEqual(a.assignment.assignment_id,b.assignment.assignment_id);
  c.features.experiment.experiment.assignment_boundary={key:'shared-cohort',applications:[golden.context.application_id,'second-app']};
  const shared=await custom(c,{assignments});
  const x=await shared.evaluate('experiment','web',golden.context,{now:golden.now});
  const y=await shared.evaluate('experiment','web',{...golden.context,application_id:'second-app'},{now:golden.now});
  const z=await shared.evaluate('experiment','web',{...golden.context,application_id:'outside-app'},{now:golden.now});
  assert.equal(x.assignment.assignment_id,y.assignment.assignment_id);assert.equal(x.value,y.value);
  assert.notEqual(x.assignment.assignment_id,a.assignment.assignment_id);assert.notEqual(x.assignment.assignment_id,z.assignment.assignment_id);
  c.features.experiment.experiment.assignment_boundary.key='distinct-cohort';
  assert.notEqual((await (await custom(c,{assignments})).evaluate('experiment','web',golden.context,{now:golden.now})).assignment.assignment_id,x.assignment.assignment_id);
  assert.equal(assignments.writes,5);
});
test('application allowlists and trusted projection marker fail closed',async()=>{
  const c=clone(golden.catalog);c.applications.push('second-app');c.features.release.applications=[golden.context.application_id];
  const r=await custom(c);
  assert.equal((await r.evaluate('release','web',{...golden.context,application_id:'second-app'},{now:golden.now})).status,'denied');
  await assert.rejects(r.evaluate('release','web',{...golden.context,projection_source:'client'},{now:golden.now}));
  c.features.experiment.experiment.assignment_boundary={key:'bad',applications:['undeclared']};
  assert.throws(()=>new FeatureRuntime(c));
});
test('restrictive disabled values and statuses survive snapshots and client access',async()=>{
  const c=clone(golden.catalog);c.features.release.baseline=true;
  const r=await custom(c);await r.updateKills(kill(2,['release']),'local-fixture',golden.now);
  const s=await r.snapshot(['release'],'web',golden.context,{now:golden.now});
  assert.equal(s.decisions.release.status,'denied');assert.equal(decisionValue(s,'release',true,wireScope,golden.now,minimum),false);
  const forged=clone(s);forged.decisions.release.value=true;assert.equal(validateSnapshot(forged,wireScope,golden.now,minimum),false);
  c.features.release.disabled_value=true;assert.throws(()=>new FeatureRuntime(c));
});
test('client rejects both rollback components, mixed state and wrong subject; retains floor on logout',async()=>{
  const r=await setup(),old=await r.snapshot(['release'],'web',golden.context,{now:golden.now});
  const client=new DecisionClient(wireScope,minimum);client.bindSession('account-1',golden.context.context_scope);
  assert.equal(client.setSnapshot(old,'account-1',golden.now),true);
  await r.updateKills(kill(2,['release']),'local-fixture',golden.now);
  const killed=await r.snapshot(['release'],'web',golden.context,{now:golden.now});
  assert.equal(client.setSnapshot(killed,'account-1',golden.now),true);
  assert.equal(client.setSnapshot(old,'account-1',golden.now+1),false);assert.equal(client.get('release',true,golden.now+1),false);
  const floor=client.watermark;client.bindSession(null,null);client.bindSession('account-2','account-2-context');
  assert.deepEqual(client.watermark,floor);
  assert.equal(client.setSnapshot({...old,context_scope:'account-2-context'},'account-2',golden.now+1),false);
  await r.activate(bundle(golden.payload,2),1,golden.now);
  const fresh=await r.snapshot(['release'],'web',{...golden.context,context_scope:'account-2-context'},{now:golden.now+1});
  assert.equal(client.setSnapshot(fresh,'account-2',golden.now+1),true);
  const lower=clone(fresh);lower.configuration_revision=1;lower.decisions.release.configuration_revision=1;
  assert.equal(client.setSnapshot(lower,'account-2',golden.now+1),false);
  const mixed=clone(fresh);mixed.decisions.release.kill_generation=3;
  assert.equal(validateSnapshot(mixed,client.scope,golden.now+1,minimum),false);
  assert.equal(validateSnapshot(old,{...wireScope,context_scope:'other-subject'},golden.now,minimum),false);
  assert.equal(validateSnapshot(old,wireScope,golden.now),false);
  assert.equal(client.setSnapshot(fresh,'account-2',golden.now+1),true);
  client.get('release',false,fresh.expires_at);assert.equal(client.get('release',false,golden.now+1),false);
});
for(const {condition,expected}of vectors.targeting)test('native targeting '+JSON.stringify(condition),async()=>{
  const p=clone(golden.payload);p.features.release.rules[0].condition=condition;
  const r=await setup();await r.activate(bundle(p,2),1,golden.now);
  const ctx={...golden.context,groups:['leaf'],roles:['reader'],group_ancestors:{leaf:['branch'],branch:['root'],root:[]}};
  assert.equal((await r.evaluate('release','web',ctx,{now:golden.now})).value,expected);
  if(expected)for(const excluded of ['leaf','branch','root'])
    assert.equal((await r.evaluate('release','web',{...ctx,excluded_groups:[excluded]},{now:golden.now})).status,'denied');
});
test('membership graph rejects missing nodes, cycles and depth overflow',async()=>{
  const r=await setup(),chain=Object.fromEntries(Array.from({length:35},(_,i)=>['n'+i,i<34?['n'+(i+1)]:[]]));
  for(const graph of [{leaf:['missing']},{leaf:['root'],root:['leaf']},chain])
    await assert.rejects(r.evaluate('release','web',{...golden.context,groups:[graph===chain?'n0':'leaf'],group_ancestors:graph},{now:golden.now}));
});
for(const condition of vectors.invalid_conditions)test('unsupported native condition '+JSON.stringify(condition),async()=>{
  const r=await setup(),p=clone(golden.payload);p.features.release.rules[0].condition=condition;
  await assert.rejects(r.activate(bundle(p,2),1,golden.now));
});
for(const v of vectors.values)test('bounded '+v.name+' validates baseline, default, force and variation',async()=>{
  const c=clone(golden.catalog);Object.assign(c.features.release,v.feature);
  const invalid=clone(c);invalid.features.release.baseline=v.invalid;assert.throws(()=>new FeatureRuntime(invalid));
  for(const position of ['default','force','variation']) {
    const catalog=clone(c),p=clone(golden.payload);p.features.release={defaultValue:v.feature.baseline,rules:[{force:v.feature.baseline}]};
    if(position==='default')p.features.release.defaultValue=v.invalid;
    if(position==='force')p.features.release.rules[0].force=v.invalid;
    if(position==='variation'){catalog.features.release.experiment=clone(catalog.features.experiment.experiment);p.features.release.rules=clone(p.features.experiment.rules);p.features.release.rules[0].variations=[v.feature.baseline,v.invalid];}
    await assert.rejects(new FeatureRuntime(catalog,{trust_policy:'local-test'}).activate(bundle(p),0,golden.now));
  }
});
test('unsupported value schema, non-JSON baseline and excessive schema nesting reject',()=>{
  for(const schema of [...vectors.invalid_schemas,(()=>{let s={type:'string'};for(let i=0;i<21;i++)s={type:'array',items:s};return s;})()]) {
    const c=clone(golden.catalog);c.features.child.value_schema=schema;assert.throws(()=>new FeatureRuntime(c));
  }
  const c=clone(golden.catalog);c.features.child.value_type='json';c.features.child.baseline=new Date();assert.throws(()=>new FeatureRuntime(c));
});
test('measurement is explicitly local-test only and rejects forged unit/transition/context receipts',async()=>{
  assert.throws(()=>new FeatureRuntime(golden.catalog,{events:new Events()}),/local-test/);
  const events=new Events(),assignments=new Assignments(),r=await setup({events,assignments});
  const d=await r.evaluate('experiment','web',golden.context,{now:golden.now}),event=eventFor(d);
  for(const change of [{evidence:{...event.evidence,unit_key:'forged'}},{application_id:'other'},{surface_id:'other'},{context_scope:'other'}])
    await assert.rejects(r.recordEvent({...event,...change},golden.now));
  assert.equal(await r.recordEvent(event,golden.now),true);
  const outcome=eventFor(d,'outcome','transition-1');outcome.evidence.transition_key='unrelated';
  await assert.rejects(r.recordEvent(outcome,golden.now));
  const restarted=await setup({events,assignments});await assert.rejects(restarted.recordEvent(eventFor(d,'outcome','transition-2'),golden.now));
});

test('known kill dominates baseline=true with absent, expired, invalid or unavailable provider',async()=>{
  for(const mode of ['absent','expired','invalid','unavailable']) {
    const c=clone(golden.catalog);Object.assign(c.features.release,{failure:'baseline',baseline:true});
    const controls=new Controls(),r=await custom(c,{controls});
    await r.updateKills(kill(2,['release']),'local-fixture',golden.now);
    const state=await controls.read(golden.scope);
    if(mode==='absent')state.bundle=null;
    if(mode==='expired')state.bundle.expires_at=golden.now;
    if(mode==='invalid')state.bundle.payload_sha256='0'.repeat(64);
    let providerCalls=0;r.provider=async()=>{providerCalls++;throw new Error('unavailable');};
    controls.read=async()=>clone(state);
    const d=await r.evaluate('release','web',golden.context,{now:golden.now});
    assert.equal(d.value,false,mode);assert.equal(d.status,'denied');assert.equal(providerCalls,0);
    const snapshot=await r.snapshot(['release'],'web',golden.context,{now:golden.now});
    const client=new DecisionClient(wireScope,minimum);client.bindSession('same-session',golden.context.context_scope);
    assert.equal(client.setSnapshot(snapshot,'same-session',golden.now),true,mode);
    assert.equal(client.get('release',true,golden.now),false);
    assert.equal(client.get('release',true,snapshot.expires_at),false);
    assert.equal(client.get('release',true,golden.now+1),false);
    assert.equal(client.get('release',true,golden.now+1),false);
  }
});
for(const bad of vectors.invalid_identifiers)test('ASCII identity/seed/namespace/wire-key rejects '+JSON.stringify(bad),async()=>{
  const r=await setup(),p=clone(golden.payload);p.features.experiment.rules[0].seed=bad;
  await assert.rejects(r.activate(bundle(p,2),1,golden.now));
  await assert.rejects(r.evaluate('release','web',{...golden.context,assignment_key:bad},{now:golden.now}));
  const c=clone(golden.catalog);c.scope.feature_namespace=bad;assert.throws(()=>new FeatureRuntime(c));
  const wire=await r.snapshot(['release'],'web',golden.context,{now:golden.now});
  wire.decisions[bad]=wire.decisions.release;delete wire.decisions.release;
  assert.equal(validateSnapshot(wire,wireScope,golden.now,minimum),false);
});

test('JSON declaration cannot disguise an enabled boolean disabled value',()=>{
  const c=clone(golden.catalog);Object.assign(c.features.release,{value_type:'json',baseline:true,disabled_value:true});
  assert.throws(()=>new FeatureRuntime(c));
});

test('verifier outage preserves killed and ordinary baseline/deny decisions in evaluate and snapshot',async()=>{
  for(const errorName of ['RuntimeError','OSError'])for(const mode of ['killed','baseline','deny']) {
    const catalog=clone(golden.catalog);catalog.features.release.baseline=true;
    catalog.features.release.failure=mode==='deny'?'deny':'baseline';
    const release=bundle(),kills=kill(1,mode==='killed'?['release']:[]);
    let outage=false,calls=0;
    const runtime=new FeatureRuntime(catalog,{controls:new Controls(),verifier:{verify:async(message,ref,purpose)=>{
      calls++;if(outage){const error=new Error('verifier unavailable');error.name=errorName;throw error;}
      return ref==='local-fixture'&&Buffer.from(message).equals(Buffer.from(purpose==='release'?approvalMessage(release):killMessage(kills)));
    }}});
    await runtime.activate(release,0,golden.now);await runtime.updateKills(kills,'local-fixture',golden.now);outage=true;
    runtime.provider=async()=>assert.fail('provider must not run with unavailable approval');
    const expected={release:mode==='baseline'?[true,'baseline']:[false,'denied'],child:['disabled','denied']};
    const reason=mode==='killed'?'disabled_or_excluded':'invalid_bundle';
    for(const [key,[value,status]] of Object.entries(expected)) {
      const decision=await runtime.evaluate(key,'web',golden.context,{now:golden.now});
      assert.deepEqual([decision.value,decision.status,decision.reason],[value,status,reason]);
    }
    const before=calls,snapshot=await runtime.snapshot(Object.keys(expected),'web',golden.context,{now:golden.now});
    assert.equal(calls,before+1);assert.equal(snapshot.configuration_revision,1);assert.equal(snapshot.kill_generation,1);
    for(const [key,[value,status]] of Object.entries(expected)) {
      const d=snapshot.decisions[key];assert.deepEqual([d.value,d.status,d.reason],[value,status,reason]);
      assert.equal(d.configuration_revision,1);assert.equal(d.kill_generation,1);
    }
  }
});

test('out-of-order prior-account responses cannot clear the bound account denial',async()=>{
  const r=new FeatureRuntime(golden.catalog,{trust_policy:'local-test'});
  await r.activate(bundle(),0,golden.now);await r.updateKills(kill(1,['release']),'local-fixture',golden.now);
  const a=await r.snapshot(['release'],'web',{...golden.context,context_scope:'context-a'},{now:golden.now});
  const b=await r.snapshot(['release'],'web',{...golden.context,context_scope:'context-b'},{now:golden.now});
  const client=new DecisionClient({...wireScope,context_scope:'context-a'},minimum);
  client.bindSession('session-a','context-a');client.bindSession('session-b','context-b');
  assert.equal(client.setSnapshot(b,'session-b',golden.now),true);
  assert.equal(b.configuration_revision,1);assert.equal(b.kill_generation,1);
  const floor=client.watermark;
  for(const response of [a,null,b]) {
    assert.equal(client.setSnapshot(response,'session-a',golden.now+1),false);
    assert.deepEqual(client.watermark,floor);assert.equal(client.get('release',true,golden.now),false);
  }
  assert.equal(client.setSnapshot(a,'session-b',golden.now),false);
  assert.equal(client.get('release',true,golden.now),false);
  client.bindSession('session-a','context-a');assert.equal(client.get('release',true,golden.now),true);
  assert.deepEqual(client.watermark,floor);
  assert.equal(client.setSnapshot(a,'session-a',golden.now),true);
  client.clear();assert.equal(client.get('release',true,golden.now),true);assert.deepEqual(client.watermark,floor);
});

test('known boolean denial dominates rejecting value predicates in both accessors',async()=>{
  const r=new FeatureRuntime(golden.catalog,{trust_policy:'local-test'});
  await r.activate(bundle(),0,golden.now);await r.updateKills(kill(1,['release']),'local-fixture',golden.now);
  const wire=await r.snapshot(['release'],'web',golden.context,{now:golden.now});
  for(const accepts of [value=>value===true,()=>false,()=>{throw new Error('predicate must not run');}]) {
    assert.equal(decisionValue(wire,'release',true,wireScope,golden.now,minimum,accepts),false);
    const client=new DecisionClient(wireScope,minimum);client.bindSession('session',golden.context.context_scope);
    assert.equal(client.setSnapshot(wire,'session',golden.now),true);
    for(const now of [golden.now,wire.expires_at,golden.now-1])assert.equal(client.get('release',true,now,accepts),false);
  }
});

test('nonboolean denials reject incompatible access instead of enabling a baseline',async()=>{
  const r=new FeatureRuntime(golden.catalog,{trust_policy:'local-test'});
  await r.activate(bundle(),0,golden.now);await r.updateKills(kill(1,['release']),'local-fixture',golden.now);
  const wire=await r.snapshot(['release'],'web',golden.context,{now:golden.now});
  for(const [value,baseline,rejects] of [[0,1,v=>v===1],['off','on',v=>v==='on'],
    [{enabled:false},{enabled:true},v=>v?.enabled===true],[[],['enabled'],v=>Array.isArray(v)&&v.length>0],
    [null,1,v=>typeof v==='number']]) {
    const denied=clone(wire);denied.decisions.release.value=value;
    assert.equal(validateSnapshot(denied,wireScope,golden.now,minimum),true);
    assert.throws(()=>decisionValue(denied,'release',baseline,wireScope,golden.now,minimum,rejects),/denied value/);
    const client=new DecisionClient(wireScope,minimum);client.bindSession('session',golden.context.context_scope);
    assert.equal(client.setSnapshot(denied,'session',golden.now),true);
    for(const now of [golden.now,denied.expires_at,golden.now-1])
      assert.throws(()=>client.get('release',baseline,now,rejects),/denied value/);
    const accepts=v=>JSON.stringify(v)===JSON.stringify(value);
    assert.deepEqual(decisionValue(denied,'release',baseline,wireScope,golden.now,minimum,accepts),value);
    assert.deepEqual(client.get('release',baseline,denied.expires_at,accepts),value);
    assert.deepEqual(client.get('release',baseline,golden.now-1,accepts),value);
    if(value!==null&&typeof value==='object') {
      assert.throws(()=>decisionValue(denied,'release',baseline,wireScope,golden.now,minimum),/denied value/);
      const returned=client.get('release',baseline,denied.expires_at,accepts);
      if(Array.isArray(returned))returned.push('enabled');else returned.enabled=true;
      assert.deepEqual(client.get('release',baseline,denied.expires_at,accepts),value);
      assert.deepEqual(client.get('release',baseline,denied.expires_at,v=>{if(Array.isArray(v))v.push('enabled');else v.enabled=true;return true;}),value);
    }
  }
});

test('explicit payload bytes preserve Unicode/fraction semantics without a generic JSON digest',async()=>{
  const spec=vectors.unicode_numeric,c=clone(golden.catalog);
  c.features={data:{value_type:'json',baseline:null,disabled_value:null,allowed_values:[null,spec.value],failure:'deny',surfaces:['web'],ancestors:[]}};
  const b=bundle();b.payload_bytes=spec.payload_bytes;b.payload_sha256=createHash('sha256').update(b.payload_bytes,'utf8').digest('hex');
  assert.equal(Buffer.from(approvalMessage(b)).toString(),spec.approval_message);
  const integral=clone(b);for(const field of ['revision','catalog_revision','created_at','expires_at']) integral[field]*=1.0;
  assert.equal(Buffer.from(approvalMessage(integral)).toString(),spec.approval_message);
  assert.throws(()=>approvalMessage({...b,revision:1.5}));
  assert.deepEqual(killMessage(kill(1,[])),killMessage({...kill(1,[]),generation:1.0}));
  const r=new FeatureRuntime(c,{trust_policy:'local-test'});
  await r.activate(b,0,golden.now);await r.updateKills(kill(1,[]),'local-fixture',golden.now);
  const d=await r.evaluate('data','web',golden.context,{now:golden.now});
  assert.equal(d.value['é'],'café');assert.equal(d.value['𝄞'],'music');assert.equal(d.value.one,1);assert.equal(d.value.tiny,1e-7);
  const bad=bundle();bad.payload_bytes=spec.unsafe_payload_bytes;bad.payload_sha256=createHash('sha256').update(bad.payload_bytes,'utf8').digest('hex');
  const unrestricted=clone(c);delete unrestricted.features.data.allowed_values;
  await assert.rejects(new FeatureRuntime(unrestricted,{trust_policy:'local-test'}).activate(bad,0,golden.now));
  const unsafe=clone(unrestricted);unsafe.features.data.baseline=spec.unsafe_integer;assert.throws(()=>new FeatureRuntime(unsafe));
  const unsafeEnum=clone(c);unsafeEnum.features.data.allowed_values.push(spec.unsafe_integer);assert.throws(()=>new FeatureRuntime(unsafeEnum));
  const wire=await r.snapshot(['data'],'web',golden.context,{now:golden.now});
  wire.decisions.data.value=spec.unsafe_integer;assert.equal(validateSnapshot(wire,wireScope,golden.now,minimum),false);
});
