import assert from 'node:assert/strict';
import test from 'node:test';
import { createHmac } from 'node:crypto';
import { FeatureRuntime, assessRelease, approvalMessage, compileReleasePayload, killMessage,
  prepareDeploymentPayload, releaseClientStates, validateReleaseManifest } from '@manolii/feature-controls';
import { DecisionClient, ReleaseClient } from '@manolii/feature-controls/client';
import { Controls, bundle, clone, golden, kill } from './helpers.mjs';
import { emptyProvider, readiness, readinessProbe, releaseBundle, releaseCatalog, releaseSetup, releaseSnapshot, sourceA, sourceB } from './release-policy-helpers.mjs';

test('reviewed ready defaults enable automatically; an explicit off survives deployment', async () => {
  const result = await readinessProbe();
  assert.equal(result.initial.status, 'passed');
  assert.deepEqual(result.initial.rows.map(row => [row.feature_key, row.actual, row.status]),
    [['checkout', true, 'ready_on'], ['next-checkout', false, 'development_off']]);
  assert.equal(result.after.status, 'passed');
  assert.deepEqual(result.after.rows.map(row => [row.feature_key, row.actual, row.status]),
    [['checkout', false, 'deliberately_disabled'], ['next-checkout', true, 'ready_on']]);
  assert.equal(result.kills_preserved, true);
  assert.equal(result.after.rows[0].exceptions.checkout.owner, 'team.incident');
});

test('a ready flag hidden by a local off fails qualification', async () => {
  const { runtime, controls } = await releaseSetup({ local_disabled: ['checkout'] });
  const report = assessRelease(readiness, golden.scope, await controls.read(golden.scope),
    [await releaseSnapshot(runtime)], golden.now, sourceA, sourceA);
  assert.equal(report.status, 'failed');
  assert.equal(report.rows[0].status, 'unexpected_off');
});

test('emergency off remains effective immediately without paperwork, and requires follow-up', async () => {
  const { runtime, controls } = await releaseSetup();
  await runtime.updateKills(kill(2, ['checkout']), 'local-fixture', golden.now);
  assert.equal((await runtime.evaluate('checkout', 'web', golden.context, { now: golden.now })).value, false);
  const report = assessRelease(readiness, golden.scope, await controls.read(golden.scope),
    [await releaseSnapshot(runtime)], golden.now, sourceA, sourceA);
  assert.equal(report.status, 'failed'); assert.equal(report.rows[0].status, 'needs_review');
});

test('an overdue exception never re-enables a feature', async () => {
  const { runtime, controls } = await releaseSetup();
  await runtime.updateKills({ ...kill(2, ['checkout']), exceptions: { checkout: { owner: 'team.ops', reason: 'Synthetic hold', review_at: 1001 } } }, 'local-fixture', golden.now);
  const report = assessRelease(readiness, golden.scope, await controls.read(golden.scope),
    [await releaseSnapshot(runtime, 1002)], 1002, sourceA, sourceA);
  assert.equal(report.rows[0].actual, false); assert.equal(report.rows[0].status, 'needs_review');
});

test('unfinished code cannot be enabled by a provider default or a mismatched release', async () => {
  const { runtime } = await releaseSetup();
  const payload = compileReleasePayload(readiness, emptyProvider());
  payload.features['next-checkout'].defaultValue = true;
  await assert.rejects(runtime.activate(releaseBundle(readiness, 2, sourceA, payload), 1, golden.now), /release policy/);
  assert.equal((await runtime.evaluate('next-checkout', 'web', golden.context, { now: golden.now })).value, false);
  const incompatible = clone(readiness); incompatible.features.checkout.state = 'development';
  await assert.rejects(runtime.activate(releaseBundle(incompatible, 2, sourceB), 1, golden.now), /deployed source/);
});

test('deployment does not approve unrelated provider drift or silently migrate a provider false', () => {
  const provider = emptyProvider(); provider.features.legacy = { defaultValue: false };
  const previous = bundle(provider);
  const changed = clone(provider); changed.features.legacy.defaultValue = true;
  assert.throws(() => prepareDeploymentPayload(readiness, changed, previous, golden.now), /provider policy changed/);
  const collision = emptyProvider(); collision.features.checkout = { defaultValue: false };
  assert.throws(() => compileReleasePayload(readiness, collision), /ownership collision/);
  assert.throws(() => prepareDeploymentPayload(readiness, emptyProvider(), bundle(collision), golden.now), /explicit migration/);
  assert.throws(() => prepareDeploymentPayload({ schema_version: 1, features: {} }, emptyProvider(), releaseBundle(), golden.now), /retirement/);
});

test('readiness and source identity and disable explanations are approval-bound', () => {
  assert.notDeepEqual(approvalMessage(releaseBundle()), approvalMessage(releaseBundle(readiness, 1, sourceB)));
  const changed = clone(readiness); changed.features.checkout.owner = 'team.other';
  assert.notDeepEqual(approvalMessage(releaseBundle()), approvalMessage(releaseBundle(changed)));
  const disabled = { ...kill(2, ['checkout']), exceptions: { checkout: { owner: 'team.ops', reason: 'Synthetic hold', review_at: 90000 } } };
  const changedReason = clone(disabled); changedReason.exceptions.checkout.reason = 'Different synthetic hold';
  assert.notDeepEqual(killMessage(disabled), killMessage(changedReason));
  assert.match(Buffer.from(killMessage(kill())).toString(), /feature-controls\/kill\/v1/);
  assert.throws(() => killMessage({ ...kill(), exceptions: disabled.exceptions }), /without disable/);
});

test('missing, stale, wrong-scope and mixed-generation evidence cannot qualify a release', async () => {
  const { runtime, controls } = await releaseSetup();
  const state = await controls.read(golden.scope), snapshot = await releaseSnapshot(runtime);
  assert.equal(assessRelease(readiness, golden.scope, state, [], golden.now, sourceA, sourceA).status, 'failed');
  for (const mutation of [s => { s.kill_generation++; }, s => { s.configuration_revision++; },
    s => { s.environment_id = 'other'; }, s => { s.expires_at = golden.now; }]) {
    const changed = clone(snapshot); mutation(changed);
    assert.throws(() => assessRelease(readiness, golden.scope, state, [changed], golden.now, sourceA, sourceA));
  }
  assert.throws(() => assessRelease(readiness, golden.scope, state, [snapshot, snapshot], golden.now, sourceA, sourceA), /duplicate/);
  assert.throws(() => assessRelease(readiness, golden.scope, state, [snapshot], golden.now, sourceB, sourceA), /deployed policy/);
  const fallback = clone(snapshot); fallback.decisions.checkout.status = 'baseline';
  assert.equal(assessRelease(readiness, golden.scope, state, [fallback], golden.now, sourceA, sourceA).rows[0].status, 'unverified');
});

test('readiness requires explicit acceptance and valid dependency declarations', () => {
  const noAcceptance = clone(readiness); noAcceptance.features.checkout.acceptance = [];
  assert.throws(() => validateReleaseManifest(noAcceptance), /acceptance/);
  const badPath = clone(readiness); badPath.features.checkout.acceptance = ['../outside'];
  assert.throws(() => validateReleaseManifest(badPath), /unsafe/);
  const dependency = clone(readiness); dependency.features.checkout.ancestors = ['next-checkout'];
  assert.throws(() => validateReleaseManifest(dependency), /unfinished ancestor/);
  assert.throws(() => validateReleaseManifest({ ...readiness, features: { checkout: { ...readiness.features.checkout, state: 'force_on' } } }));
  assert.throws(() => new FeatureRuntime(releaseCatalog(readiness), { release_manifest: readiness }), /source binding/);
  assert.equal(assessRelease({ schema_version: 1, features: {} }, golden.scope, { bundle: null, kill: null, time_highwater: 0 }, [], golden.now, sourceA, sourceA).status, 'no_features');
});

test('old and new source versions coexist without disabling previously ready features', async () => {
  const { runtime: old, controls } = await releaseSetup();
  const next = clone(readiness);
  next.features['next-checkout'].state = 'ready'; next.features['next-checkout'].acceptance = ['tests/feature_controls/release-policy.test.mjs'];
  next.features.receipt = clone(next.features.checkout);
  const updated = new FeatureRuntime(releaseCatalog(next), { trust_policy: 'local-test', controls, release_manifest: next, release_source_sha: sourceB });
  const previous = await updated.previousForDeployment(golden.now);
  await updated.activate(releaseBundle(next, 2, sourceB, prepareDeploymentPayload(next, emptyProvider(), previous.bundle, golden.now)), 1, golden.now);
  assert.equal((await old.evaluate('checkout', 'web', golden.context, { now: golden.now })).value, true);
  assert.equal((await old.evaluate('next-checkout', 'web', golden.context, { now: golden.now })).value, false);
  assert.equal((await updated.evaluate('receipt', 'web', golden.context, { now: golden.now })).value, true);
  const evidence = await updated.releaseEvidence([golden.context], golden.now);
  assert.equal(assessRelease(next, golden.scope, evidence.controls, evidence.snapshots, golden.now, sourceB, evidence.source_sha).status, 'passed');
  const rollback = await old.releaseEvidence([golden.context], golden.now);
  assert.equal(assessRelease(readiness, golden.scope, rollback.controls, rollback.snapshots, golden.now, sourceA, rollback.source_sha, 'rollback').status, 'passed');
  assert.throws(() => prepareDeploymentPayload(readiness, emptyProvider(), evidence.controls.bundle, golden.now), /retirement|regression/);
});

test('client source readiness cannot be overridden by a server decision or caller mutation', async () => {
  const { runtime } = await releaseSetup();
  const snapshot = await releaseSnapshot(runtime);
  const scope = { ...golden.scope, application_id: 'example-app', surface_id: 'web', context_scope: golden.context.context_scope };
  const decisions = new DecisionClient(scope, { configuration_revision: 0, kill_generation: 0, time_highwater: 0 });
  decisions.bindSession('session', golden.context.context_scope);
  assert.equal(decisions.setSnapshot(snapshot, 'session', golden.now), true);
  const states = releaseClientStates(readiness, 'example-app'), client = new ReleaseClient(decisions, states);
  states['next-checkout'] = true;
  assert.equal(client.enabled('checkout', golden.now), true);
  assert.equal(client.enabled('next-checkout', golden.now), false);
  assert.equal(client.enabled('unknown', golden.now), false);
  assert.deepEqual(releaseClientStates(readiness, 'other-app'), {});
});


test('unsigned stored kill metadata cannot qualify an intentional off, but off remains immediate', async () => {
  const controls = new Controls(), receipts = new Map(), references = new Map();
  const signature = (message, purpose) => createHmac('sha256', 'fixture-only-release-approval').update(purpose).update('\0').update(message).digest('hex');
  const sign = (message, purpose, reference = `fixture-${receipts.size}`) => { receipts.set(reference, signature(message, purpose)); references.set(Buffer.from(message).toString('hex'), reference); return reference; };
  let checked = 0;
  const verifier = {
    verify: async (message, reference, purpose) => signature(message, purpose) === receipts.get(reference),
    verifyKill: async (value, scope) => { checked++; return JSON.stringify(value.scope) === JSON.stringify(scope) &&
      receipts.get(references.get(Buffer.from(killMessage(value)).toString('hex'))) === signature(killMessage(value), 'kill'); },
  };
  const runtime = new FeatureRuntime(releaseCatalog(readiness), { controls, verifier,
    release_manifest: readiness, release_source_sha: sourceA });
  const approved = releaseBundle(); sign(approvalMessage(approved), 'release', approved.approval_ref);
  await runtime.activate(approved, 0, golden.now);
  const initial = kill(); await runtime.updateKills(initial, sign(killMessage(initial), 'kill'), golden.now);
  assert.equal((await runtime.releaseEvidence([golden.context], golden.now)).controls.kill.generation, 1);
  const current = await controls.read(golden.scope), forged = { ...kill(2, ['checkout']), exceptions: {
    checkout: { owner: 'team.incident', reason: 'Synthetic hold', review_at: golden.now + 10000 },
  } };
  assert.equal(await controls.compareAndSwap(golden.scope, current, { ...current, kill: forged }), true);
  assert.equal((await releaseSnapshot(runtime)).decisions.checkout.value, false);
  await assert.rejects(runtime.releaseEvidence([golden.context], golden.now), /stored kill approval/);
  await assert.rejects(runtime.previousForDeployment(golden.now), /stored kill approval/);
  sign(killMessage(forged), 'kill');
  const evidence = await runtime.releaseEvidence([golden.context], golden.now);
  assert.equal(assessRelease(readiness, golden.scope, evidence.controls, evidence.snapshots, golden.now, sourceA, sourceA).status, 'passed');
  assert.ok(checked >= 4);
  const legacy = new FeatureRuntime(releaseCatalog(readiness), { controls, verifier: { verify: verifier.verify },
    release_manifest: readiness, release_source_sha: sourceA });
  await assert.rejects(legacy.inspectForRelease(golden.now), /stored kill approval/);
});
