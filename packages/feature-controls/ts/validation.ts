import { createHash } from "node:crypto";
import { Ajv } from "ajv";
import schema from "../../../contracts/feature-controls/schema.json" with { type: "json" };
import type { Catalog, Feature, Json, ReleaseBundle, Scope } from "./types.js";
const ajv = new Ajv({ strict: true, ownProperties: true });
ajv.addSchema(schema);
const validators = new Map<string, ReturnType<typeof ajv.compile>>();
export function validate(kind: string, value: unknown): void {
  let check = validators.get(kind);
  if (!check) { check = ajv.compile({ $ref: `${schema.$id}#/$defs/${kind}` }); validators.set(kind, check); }
  if (!check(value)) throw new Error(`invalid ${kind}`);
}
export function ascii(value: unknown): asserts value is string {
  if (typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value)) throw new Error("invalid ASCII identifier");
}
export function sameScope(a: Scope, b: Scope): boolean {
  return a.namespace === b.namespace && a.application === b.application && a.environment === b.environment;
}
export function matches(value: unknown, feature: Feature): value is Json {
  if (feature.value_type === "json") return value !== undefined && validJson(value);
  return typeof value === feature.value_type && (typeof value !== "number" || Number.isFinite(value));
}
function validJson(value: unknown): boolean {
  try { return JSON.stringify(value) !== undefined; } catch { return false; }
}
export function validateCatalog(catalog: Catalog): void {
  validate("catalog", catalog);
  for (const [key, feature] of Object.entries(catalog.features)) {
    if (!matches(feature.baseline, feature)) throw new Error("invalid baseline");
    const walk = (node: string, path: Set<string>): void => {
      if (path.has(node) || !Object.hasOwn(catalog.features, node)) throw new Error("invalid ancestor graph");
      if (path.size > 32) throw new Error("ancestor depth exceeded");
      for (const ancestor of catalog.features[node].ancestors) walk(ancestor, new Set([...path, node]));
    };
    walk(key, new Set());
  }
}
export function payloadOf(bundle: ReleaseBundle, catalog: Catalog): Record<string, unknown> {
  validate("bundle", bundle);
  if (!sameScope(bundle.scope, catalog.scope) || bundle.catalog_revision !== catalog.revision ||
      bundle.created_at >= bundle.expires_at || Buffer.byteLength(bundle.payload_bytes, "utf8") > 1048576 ||
      createHash("sha256").update(bundle.payload_bytes, "utf8").digest("hex") !== bundle.payload_sha256) throw new Error("bundle binding mismatch");
  const payload: unknown = JSON.parse(bundle.payload_bytes);
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("invalid payload");
  const p = payload as Record<string, unknown>;
  for (const key of ["features", "savedGroups", "contextualBandits"]) {
    if (!p[key] || typeof p[key] !== "object" || Array.isArray(p[key])) throw new Error("incomplete payload");
  }
  if (!Array.isArray(p.experiments) || p.experiments.length || Object.keys(p.savedGroups as object).length ||
      Object.keys(p.contextualBandits as object).length || Object.keys(p).some(k => !["features", "experiments", "savedGroups", "contextualBandits"].includes(k))) throw new Error("unsupported payload capability");
  const features = p.features as Record<string, Record<string, unknown>>;
  if (Object.keys(features).length !== Object.keys(catalog.features).length) throw new Error("incomplete feature catalog");
  for (const [key, native] of Object.entries(features)) {
    const feature = Object.hasOwn(catalog.features, key) ? catalog.features[key] : undefined;
    if (!feature || !native || typeof native !== "object" || !matches(native.defaultValue, feature)) throw new Error("invalid native feature");
    if (native.rules !== undefined && !Array.isArray(native.rules)) throw new Error("invalid rules");
    for (const rule of (native.rules ?? []) as Record<string, unknown>[]) {
      if (!rule || typeof rule !== "object") throw new Error("invalid rule");
      if (Object.keys(rule).some(k => !["force", "coverage", "seed", "hashVersion", "key", "variations", "weights", "meta", "bucketVersion", "hashAttribute"].includes(k))) throw new Error("unsupported rule capability");
      if (rule.seed !== undefined) ascii(rule.seed);
      if (rule.key !== undefined) ascii(rule.key);
      if (rule.hashAttribute !== undefined && rule.hashAttribute !== "id") throw new Error("unsupported hash attribute");
      if (["fallbackAttribute", "namespace", "parentConditions", "prerequisites", "range", "filters", "url", "urlPatterns", "bandit", "condition"].some(k => Object.hasOwn(rule, k))) throw new Error("unsupported rule capability");
      if (rule.force !== undefined && !matches(rule.force, feature)) throw new Error("invalid forced value");
      if (rule.variations !== undefined) {
        if (!feature.experiment || rule.key !== feature.experiment.key || rule.hashVersion !== 2 || rule.seed === undefined ||
            rule.bucketVersion !== feature.experiment.epoch || !Array.isArray(rule.variations) || rule.variations.length < 2 || rule.variations.length > 128 ||
            !rule.variations.every(v => matches(v, feature)) || !Array.isArray(rule.meta) || rule.meta.length !== rule.variations.length ||
            !Array.isArray(rule.weights) || rule.weights.length !== rule.variations.length ||
            !rule.weights.every(w => typeof w === "number" && Number.isFinite(w) && w >= 0 && w <= 1) ||
            Math.abs(rule.weights.reduce((a: number, b: number) => a + b, 0) - 1) > 1e-9) throw new Error("invalid experiment");
        const keys = rule.meta.map((m: unknown) => {
          if (!m || typeof m !== "object" || Object.keys(m).length !== 1) throw new Error("invalid metadata");
          const key = (m as Record<string, unknown>).key;
          ascii(key); return key;
        });
        if (new Set(keys).size !== keys.length || rule.disableStickyBucketing === true) throw new Error("invalid variation metadata");
      }
      if (rule.coverage !== undefined && (typeof rule.coverage !== "number" || !Number.isFinite(rule.coverage) || rule.coverage < 0 || rule.coverage > 1 || rule.hashVersion !== 2 || rule.seed === undefined)) throw new Error("invalid coverage");
    }
  }
  return p;
}
export function approvalMessage(bundle: ReleaseBundle): Uint8Array {
  const s = bundle.scope;
  return Buffer.from(JSON.stringify(["feature-controls/release/v1", s.namespace, s.application, s.environment,
    bundle.revision, bundle.catalog_revision, bundle.created_at, bundle.expires_at, bundle.provider_semantics,
    bundle.payload_sha256, bundle.approval_ref]), "utf8");
}
