import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { FeatureRuntime, PROVIDER_SEMANTICS } from '@manolii/feature-controls';
export const golden = JSON.parse(readFileSync(new URL('./golden.json', import.meta.url)));
export const clone = value => structuredClone(value);
export function bundle(payload = golden.payload, revision = 1, scope = golden.scope) {
  const payload_bytes = JSON.stringify(payload);
  return { schema_version: 1, scope: clone(scope), revision, catalog_revision: 1, created_at: 0,
    expires_at: golden.expires_at, provider_semantics: PROVIDER_SEMANTICS, payload_bytes,
    payload_sha256: createHash('sha256').update(payload_bytes).digest('hex'), approval_ref: 'local-fixture' };
}
export function kill(generation = 1, disabled = [], expires_at = golden.expires_at, scope = golden.scope) {
  return { schema_version: 1, scope: clone(scope), generation, disabled, expires_at };
}
export function numericBundle(epoch, version) {
  const catalog = clone(golden.catalog), payload = clone(golden.payload);
  catalog.features.experiment.experiment.epoch = 'EPOCH_LITERAL';
  payload.features.experiment.rules[0].bucketVersion = 'VERSION_LITERAL';
  const catalogRaw = JSON.stringify(catalog).replace('"EPOCH_LITERAL"', epoch);
  const release = bundle(payload);
  release.payload_bytes = release.payload_bytes.replace('"VERSION_LITERAL"', version);
  release.payload_sha256 = createHash('sha256').update(release.payload_bytes).digest('hex');
  return { catalog: JSON.parse(catalogRaw), release };
}
export async function numericStickyProbe(epoch, version) {
  const {catalog, release} = numericBundle(epoch, version);
  const initialize = async assignments => {
    const controls = new Controls(), runtime = new FeatureRuntime(catalog, {trust_policy:'local-test', controls, assignments});
    await runtime.activate(release, 0, golden.now);await runtime.updateKills(kill(), 'local-fixture', golden.now);
    const provider = runtime.provider.bind(runtime), calls = [];
    runtime.provider = async (...args) => {
      const result = await provider(...args);
      calls.push({key:result.experimentResult?.key,sticky:result.experimentResult?.stickyBucketUsed});
      return result;
    };
    return {runtime, calls, controls};
  };
  const fresh = await initialize();
  await fresh.runtime.evaluate('experiment','web',golden.context,{now:golden.now,preview:true});
  const saved = fresh.calls.at(-1).key === 'control' ? 'treatment' : 'control';
  const reads = [], creates = [], assignment = {assignment_id:'persisted-choice',variant:saved};
  const store = {durable:true,read:async key => {reads.push(clone(key));return clone(assignment);},
    createIfAbsent:async (key, variant) => {creates.push({key:clone(key),variant});return clone(assignment);}};
  const {runtime,calls,controls} = await initialize(store);
  const preview = await runtime.evaluate('experiment','web',golden.context,{now:golden.now,preview:true});
  const previewCreates = creates.length;
  const live = await runtime.evaluate('experiment','web',golden.context,{now:golden.now});
  return {epoch,version,saved,natural:fresh.calls.at(-1).key,preview:preview.value,preview_creates:previewCreates,
    live:live.value,status:live.status,reason:live.reason,calls,
    epochs:[...reads.map(k=>k.allocation_epoch),...creates.map(x=>x.key.allocation_epoch)],
    payload_unchanged:JSON.stringify((await controls.read(golden.scope)).bundle)===JSON.stringify(release),
    integer_metadata:reads.every(k=>Number.isSafeInteger(k.allocation_epoch))&&creates.every(x=>Number.isSafeInteger(x.key.allocation_epoch)),
    payload_hash:release.payload_sha256};
}
export class Assignments {
  durable = true; values = new Map(); writes = 0;
  async read(key) { return clone(this.values.get(JSON.stringify(key)) ?? null); }
  async createIfAbsent(key, variant) {
    const k = JSON.stringify(key);
    if (!this.values.has(k)) { this.values.set(k, { assignment_id: `assignment-${this.values.size}`, variant }); this.writes++; }
    return clone(this.values.get(k));
  }
}
export class Events {
  durable = true; test_only = true; values = new Map();
  async appendIfAbsent(event) {
    if (this.values.has(event.event_id)) {
      if (JSON.stringify(this.values.get(event.event_id)) !== JSON.stringify(event)) throw new Error('event collision');
      return false;
    }
    this.values.set(event.event_id, clone(event)); return true;
  }
}
export class Controls {
  durable = true; test_only = true; values = new Map(); writes = 0;
  async read(scope) { return clone(this.values.get(JSON.stringify(scope)) ?? {bundle:null,kill:null,time_highwater:0}); }
  async compareAndSwap(scope, expected, next) {
    const key = JSON.stringify(scope);
    if (JSON.stringify(this.values.get(key) ?? {bundle:null,kill:null,time_highwater:0}) !== JSON.stringify(expected)) return false;
    this.values.set(key, clone(next)); this.writes++; return true;
  }
}
export async function setup(options = {}) {
  const runtime = new FeatureRuntime(clone(golden.catalog), { trust_policy: 'local-test', ...options });
  await runtime.activate(bundle(), 0, golden.now);
  await runtime.updateKills(kill(), 'local-fixture', golden.now);
  return runtime;
}
export function eventFor(d, kind='exposure', event_id='event-1') {
  return { schema_version:1, scope:d.scope, application_id:d.application_id, surface_id:d.surface_id, context_scope:d.context_scope, event_id, kind, decision_id:d.decision_id,
    feature_key:d.feature_key, assignment_id:d.assignment.assignment_id,
    configuration_revision:d.configuration_revision, allocation_epoch:d.allocation_epoch,
    variant:d.assignment.variant, timestamp:golden.now,
    evidence:{ source:kind==='exposure'?'render':'business-transition',unit_key:d.unit_key, ...(kind==='outcome'?{transition_key:event_id}:{}) } };
}
