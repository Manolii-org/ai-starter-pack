import { readFileSync } from 'node:fs';
import { FeatureRuntime, assessRelease, approvalMessage, compileReleasePayload, killMessage,
  prepareDeploymentPayload, releaseCatalogFeatures, releaseManifestMessage } from '@manolii/feature-controls';
import { Controls, bundle, clone, golden, kill } from './helpers.mjs';

export const readiness = JSON.parse(readFileSync(new URL('./release-readiness.json', import.meta.url)));
export const sourceA = '1'.repeat(40), sourceB = '2'.repeat(40);
export const emptyProvider = () => ({ features: {}, experiments: [], savedGroups: {}, contextualBandits: {} });
export const releaseCatalog = manifest => ({ schema_version: 1, scope: clone(golden.scope),
  applications: ['example-app'], revision: 1, features: releaseCatalogFeatures(manifest) });
export function releaseBundle(manifest = readiness, revision = 1, source = sourceA, payload = compileReleasePayload(manifest, emptyProvider())) {
  return { ...bundle(payload, revision), deployment: { source_sha: source, manifest: clone(manifest) } };
}
export async function releaseSetup(options = {}, manifest = readiness, source = sourceA) {
  const controls = options.controls ?? new Controls();
  const runtime = new FeatureRuntime(releaseCatalog(manifest), { trust_policy: 'local-test', controls,
    release_manifest: manifest, release_source_sha: source, ...options });
  await runtime.activate(releaseBundle(manifest, 1, source), 0, golden.now);
  await runtime.updateKills(kill(), 'local-fixture', golden.now);
  return { controls, runtime };
}
export async function releaseSnapshot(runtime, now = golden.now) {
  return runtime.snapshot(Object.keys(readiness.features), 'web', golden.context, { now });
}
export async function readinessProbe() {
  const { controls, runtime } = await releaseSetup();
  const initial = assessRelease(readiness, golden.scope, await controls.read(golden.scope),
    [await releaseSnapshot(runtime)], golden.now, sourceA, sourceA);
  const disabled = { ...kill(2, ['checkout']), exceptions: {
    checkout: { owner: 'team.incident', reason: 'Synthetic incident investigation', review_at: 90000 },
  } };
  await runtime.updateKills(disabled, 'local-fixture', golden.now);
  const next = clone(readiness);
  next.features['next-checkout'].state = 'ready';
  next.features['next-checkout'].acceptance = ['tests/feature_controls/release-policy.test.mjs'];
  const previous = (await controls.read(golden.scope)).bundle;
  const payload = prepareDeploymentPayload(next, emptyProvider(), previous, golden.now);
  const updated = new FeatureRuntime(releaseCatalog(next), { trust_policy: 'local-test', controls,
    release_manifest: next, release_source_sha: sourceB });
  const release = releaseBundle(next, 2, sourceB, payload);
  await updated.activate(release, 1, golden.now);
  const after = assessRelease(next, golden.scope, await controls.read(golden.scope),
    [await releaseSnapshot(updated)], golden.now, sourceB, sourceB);
  return { initial, after, kills_preserved: JSON.stringify((await controls.read(golden.scope)).kill) === JSON.stringify(disabled),
    kill_bytes: Buffer.from(killMessage(disabled)).toString('utf8'),
    approval_bytes: Buffer.from(approvalMessage(release)).toString('utf8'),
    manifest_bytes: Buffer.from(releaseManifestMessage(next)).toString('utf8') };
}
