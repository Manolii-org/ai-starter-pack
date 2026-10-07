import assert from 'node:assert/strict';
import test from 'node:test';
import { FeatureRuntime, approvalMessage, killMessage } from '@manolii/feature-controls';
import { DecisionClient, decisionValue, validateSnapshot } from '@manolii/feature-controls/client';
import { Assignments, Controls, Events, bundle, clone, eventFor, golden, kill, setup } from './helpers.mjs';

test('shared golden hard-fence and typed baseline decisions', async () => {
  const runtime = await setup();
  for (const fixture of golden.cases) {
    const context = {...golden.context};
    for (const key of ['excluded','eligible','authorized']) if (key in fixture) context[key]=fixture[key];
    const d = await runtime.evaluate(fixture.key, fixture.surface, context, {now:golden.now});
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
  await assert.rejects(runtime.activate(bundle(golden.payload,2,{...golden.scope,application:'other'}),1,golden.now));
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
  const other={...golden.scope,application:'other'};
  const catalog={...clone(golden.catalog),scope:other};
  const second=new FeatureRuntime(catalog,{controls,trust_policy:'local-test'});
  await second.activate(bundle(golden.payload,1,other),0,golden.now);
  await second.updateKills(kill(1,[],golden.expires_at,other),'local-fixture',golden.now);
  assert.equal((await second.evaluate('child','web',golden.context,{now:golden.now})).value,'enabled');
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
  assert.deepEqual(Object.keys(snapshot).sort(),['application','decisions','environment','expires_at','generated_at','schema_version','surface']);
  const expected={application:golden.scope.application,environment:'test',surface:'web'};
  assert.equal(validateSnapshot(snapshot,expected,golden.now),true);
  assert.equal(validateSnapshot({...snapshot,raw_context:{}},expected,golden.now),false);
  assert.equal(validateSnapshot({...snapshot,decisions:{release:{...snapshot.decisions.release,assignment_id:'private'}}},expected,golden.now),false);
  assert.equal(decisionValue(snapshot,'release',false,expected,golden.now),true);
  assert.equal(decisionValue(snapshot,'unknown',false,expected,golden.now),false);
  assert.equal(decisionValue(snapshot,'release','baseline',expected,golden.now),'baseline');
  assert.equal(decisionValue(snapshot,'release',false,expected,snapshot.expires_at),false);
  assert.equal(decisionValue({...snapshot,schema_version:2},'release',false,expected,golden.now),false);
  const client=new DecisionClient(expected);client.bindSession('account-1');assert.equal(client.setSnapshot(snapshot,'account-1',golden.now),true);
  assert.equal(client.get('release',false,golden.now),true);client.bindSession('account-2');assert.equal(client.get('release',false,golden.now),false);
  assert.equal(client.setSnapshot(snapshot,'account-1',golden.now),false);client.bindSession(null);
});
