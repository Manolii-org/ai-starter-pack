import { killMessage, validateTime } from "./lifecycle.js";
import { createHash } from "node:crypto";
import { ownJson, sameScope, validate, validateReleaseManifest, releaseCatalogFeatures, releaseManifestMessage, compatibleReleaseManifest } from "./validation.js";
import type { Catalog, ControlState, DisableException, Json, ReleaseBundle, ReleaseManifest, Scope, Snapshot } from "./types.js";

export { validateReleaseManifest, releaseCatalogFeatures } from "./validation.js";

/** Defence in depth: inject these into RuntimeOptions.local_disabled. */
export function unfinishedReleaseFeatures(manifest: ReleaseManifest): string[] {
  validateReleaseManifest(manifest);
  return Object.keys(manifest.features).filter(key => manifest.features[key].state === "development");
}

/** Build-time, application-only client projection: no owners or private rules. */
export function releaseClientStates(manifest: ReleaseManifest, application_id: string): Record<string, boolean> {
  validateReleaseManifest(manifest);
  return Object.fromEntries(Object.entries(manifest.features).filter(([, feature]) => feature.applications.includes(application_id))
    .map(([key, feature]) => [key, feature.state === "ready"]));
}

function providerPayload(value: unknown): Record<string, Json> {
  const serialized = JSON.stringify(value);
  if (Buffer.byteLength(serialized, "utf8") > 1048576) throw new Error("release provider payload too large");
  const parsed = ownJson(serialized);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("unsupported release provider payload");
  const payload = parsed as Record<string, Json>;
  if (
      Object.keys(payload).some(key => !["features", "experiments", "savedGroups", "contextualBandits"].includes(key)) ||
      !payload.features || typeof payload.features !== "object" || Array.isArray(payload.features) ||
      !Array.isArray(payload.experiments) || payload.experiments.length ||
      !payload.savedGroups || typeof payload.savedGroups !== "object" || Array.isArray(payload.savedGroups) || Object.keys(payload.savedGroups).length ||
      !payload.contextualBandits || typeof payload.contextualBandits !== "object" || Array.isArray(payload.contextualBandits) || Object.keys(payload.contextualBandits).length) {
    throw new Error("unsupported release provider payload");
  }
  return payload;
}

/** Generates new bytes for normal signed publication; never mutates an approval. */
export function compileReleasePayload(manifest: ReleaseManifest, provider: unknown): Record<string, Json> {
  validateReleaseManifest(manifest);
  const payload = providerPayload(provider), features = payload.features as Record<string, Json>;
  for (const [key, definition] of Object.entries(manifest.features)) {
    if (Object.hasOwn(features, key)) throw new Error("release/provider ownership collision; explicit migration required");
    features[key] = { defaultValue: definition.state === "ready" };
  }
  return providerPayload(payload);
}

function equalJson(left: Json, right: Json): boolean {
  if (left === right) return true;
  if (!left || !right || typeof left !== "object" || typeof right !== "object" || Array.isArray(left) !== Array.isArray(right)) return false;
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length && keys.every(key => Object.hasOwn(right, key) &&
    equalJson((left as Record<string, Json>)[key], (right as Record<string, Json>)[key]));
}

/** Caller must first verify the previous immutable approval and its lease. */
export function prepareDeploymentPayload(manifest: ReleaseManifest, provider: unknown, approved: ReleaseBundle, now: number): Record<string, Json> {
  validateTime(now); validate("bundle", approved);
  if (approved.created_at > now || approved.expires_at <= now ||
      createHash("sha256").update(approved.payload_bytes, "utf8").digest("hex") !== approved.payload_sha256) throw new Error("invalid previous release");
  const next = compileReleasePayload(manifest, provider), previous = providerPayload(ownJson(approved.payload_bytes));
  const previousFeatures = previous.features as Record<string, Json>;
  const previousManaged = approved.deployment?.manifest.features ?? {};
  if (approved.deployment) validateReleaseManifest(approved.deployment.manifest);
  for (const key of Object.keys(manifest.features)) {
    if (Object.hasOwn(previousFeatures, key) && !Object.hasOwn(previousManaged, key)) throw new Error("existing provider control needs explicit migration");
  }
  for (const key of Object.keys(previousManaged)) {
    if (!Object.hasOwn(manifest.features, key)) throw new Error("release feature retirement needs explicit migration");
    if (previousManaged[key].state === "ready" && manifest.features[key].state !== "ready") throw new Error("ready feature regression needs explicit migration");
    for (const name of ["applications", "surfaces", "ancestors"] as const) {
      if (JSON.stringify([...(previousManaged[key][name] ?? [])].sort()) !== JSON.stringify([...(manifest.features[key][name] ?? [])].sort())) throw new Error("release boundary change needs explicit migration");
    }
    delete previousFeatures[key];
  }
  const expectedProvider = providerPayload(next);
  for (const key of Object.keys(manifest.features)) delete (expectedProvider.features as Record<string, Json>)[key];
  if (!equalJson(previous, expectedProvider)) throw new Error("provider policy changed; explicit publication required");
  return next;
}

export interface ReleaseCheckRow {
  feature_key: string; application_id: string; surface_id: string;
  state: "development" | "ready"; expected: boolean; actual: boolean | null;
  status: "ready_on" | "development_off" | "deliberately_disabled" | "needs_review" | "unexpected_off" | "unexpected_on" | "unverified";
  disabling_features: string[]; exceptions: Record<string, DisableException>;
}
export interface ReleaseCheck {
  status: "passed" | "failed" | "no_features"; rows: ReleaseCheckRow[];
  configuration_revision: number | null; kill_generation: number;
}

/** Consumes trusted store state and authenticated runtime evidence, never client input. */
export function assessRelease(manifest: ReleaseManifest, scope: Scope, controls: ControlState, snapshots: Snapshot[], now: number, source_sha: string, observed_source_sha: string, operation: "deploy" | "rollback" = "deploy"): ReleaseCheck {
  validateReleaseManifest(manifest); validateTime(now); validate("control_state", controls);
  if (!["deploy", "rollback"].includes(operation)) throw new Error("invalid release operation");
  if (!Object.keys(manifest.features).length) return { status: "no_features", rows: [], configuration_revision: controls.bundle?.revision ?? null, kill_generation: controls.kill?.generation ?? 0 };
  const { kill, bundle } = controls;
  if (!kill || !bundle || !bundle.deployment || !/^[0-9a-f]{40}$/.test(source_sha) || source_sha !== observed_source_sha ||
      !(operation === "rollback" ? compatibleReleaseManifest(manifest, bundle.deployment.manifest) :
        Buffer.from(releaseManifestMessage(bundle.deployment.manifest)).equals(Buffer.from(releaseManifestMessage(manifest)))) ||
      !sameScope(bundle.scope, scope) || bundle.created_at > now || bundle.expires_at <= now || controls.time_highwater > now ||
      createHash("sha256").update(bundle.payload_bytes, "utf8").digest("hex") !== bundle.payload_sha256) throw new Error("release does not match deployed policy");
  killMessage(kill);
  if (!sameScope(kill.scope, scope) || kill.expires_at <= now || snapshots.length > 128) throw new Error("invalid release-check binding");
  const captured = new Map<string, Snapshot>();
  let revision: number | null = null;
  for (const snapshot of snapshots) {
    validate("snapshot", snapshot);
    if (!sameScope(snapshot, scope) || snapshot.kill_generation !== kill.generation || snapshot.configuration_revision !== bundle.revision ||
        snapshot.generated_at > now || snapshot.expires_at <= now || snapshot.time_highwater > now ||
        revision !== null && snapshot.configuration_revision !== revision) throw new Error("stale or mixed runtime evidence");
    revision = snapshot.configuration_revision;
    const key = JSON.stringify([snapshot.application_id, snapshot.surface_id]);
    const previous = captured.get(key);
    if (previous) {
      if (previous.context_scope !== snapshot.context_scope || Object.keys(snapshot.decisions).some(feature => Object.hasOwn(previous.decisions, feature))) throw new Error("duplicate runtime evidence");
      previous.decisions = { ...previous.decisions, ...structuredClone(snapshot.decisions) };
      previous.expires_at = Math.min(previous.expires_at, snapshot.expires_at);
    } else captured.set(key, structuredClone(snapshot));
  }
  const disabled = new Set(kill.disabled), rows: ReleaseCheckRow[] = [];
  const blockers = (key: string, visited = new Set<string>()): string[] => {
    if (visited.has(key)) return [];
    visited.add(key);
    return [...(disabled.has(key) ? [key] : []), ...(manifest.features[key].ancestors ?? []).flatMap(parent => blockers(parent, visited))];
  };
  for (const [key, definition] of Object.entries(manifest.features)) {
    const disabling = blockers(key).sort(), exceptions: Record<string, DisableException> = {};
    for (const parent of disabling) {
      const exception = kill.exceptions?.[parent];
      if (exception) exceptions[parent] = structuredClone(exception);
    }
    const needsReview = disabling.some(parent => !exceptions[parent] || exceptions[parent].review_at <= now);
    for (const app of definition.applications) for (const surface of definition.surfaces) {
      const snapshot = captured.get(JSON.stringify([app, surface]));
      const decision = snapshot?.decisions[key];
      const expected = definition.state === "ready" && !disabling.length;
      let status: ReleaseCheckRow["status"] = "unverified";
      const coherent = decision && decision.expires_at > now &&
        decision.configuration_revision === snapshot.configuration_revision && decision.kill_generation === kill.generation;
      if (coherent && typeof decision.value === "boolean" &&
          (decision.status === "resolved" && decision.reason === "released" || decision.status === "denied" && decision.reason === "disabled_or_excluded")) {
        if (decision.value !== expected) status = decision.value ? "unexpected_on" : "unexpected_off";
        else if (needsReview) status = "needs_review";
        else if (disabling.length && decision.status === "denied" && decision.reason === "disabled_or_excluded") status = "deliberately_disabled";
        else if (!disabling.length && decision.status === "resolved" && decision.reason === "released") status = expected ? "ready_on" : "development_off";
        else if (!disabling.length && definition.state === "development" && decision.status === "denied" && decision.reason === "disabled_or_excluded") status = "development_off";
      }
      rows.push({ feature_key: key, application_id: app, surface_id: surface, state: definition.state, expected,
        actual: typeof decision?.value === "boolean" ? decision.value : null, status, disabling_features: disabling, exceptions });
    }
  }
  return { status: !rows.length ? "no_features" : rows.every(row => ["ready_on", "development_off", "deliberately_disabled"].includes(row.status)) ? "passed" : "failed",
    rows, configuration_revision: revision, kill_generation: kill.generation };
}

/** Checks existing catalogue ownership before a product opts into generated defaults. */
export function assertReleaseCatalog(manifest: ReleaseManifest, catalog: Catalog): void {
  const expected = releaseCatalogFeatures(manifest);
  for (const [key, definition] of Object.entries(expected)) {
    const actual = catalog.features[key];
    if (!actual || !equalJson(actual as unknown as Json, definition as unknown as Json)) throw new Error("release catalogue does not match source policy");
  }
}
