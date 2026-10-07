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
  durable = true; values = new Map();
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
  async read(scope) { return clone(this.values.get(JSON.stringify(scope)) ?? {bundle:null,kill:null}); }
  async compareAndSwap(scope, expected, next) {
    const key = JSON.stringify(scope);
    if (JSON.stringify(await this.read(scope)) !== JSON.stringify(expected)) return false;
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
  return { schema_version:1, scope:d.scope, event_id, kind, decision_id:d.decision_id,
    feature_key:d.feature_key, assignment_id:d.assignment.assignment_id,
    configuration_revision:d.configuration_revision, allocation_epoch:d.allocation_epoch,
    variant:d.assignment.variant, timestamp:golden.now,
    evidence:{ source:kind==='exposure'?'render':'business-transition',unit_key:'render-unit' } };
}
