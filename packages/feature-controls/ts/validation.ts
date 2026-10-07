import { createHash } from "node:crypto";
import { Ajv } from "ajv";
import schema from "../../../contracts/feature-controls/schema.json" with { type: "json" };
import type { AssignmentBoundary, Catalog, Context, Feature, Json, ReleaseBundle, Scope } from "./types.js";
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
  return a.ecosystem_id === b.ecosystem_id && a.deployment_id === b.deployment_id &&
    a.feature_namespace === b.feature_namespace && a.environment_id === b.environment_id;
}
const valueValidators = new WeakMap<Feature, ReturnType<typeof ajv.compile>>();
export function matches(value: unknown, feature: Feature): value is Json {
  if (!validJson(value) || (feature.value_type !== "json" && typeof value !== feature.value_type)) return false;
  if (feature.allowed_values && !feature.allowed_values.some(item => sameJson(value, item))) return false;
  let check = valueValidators.get(feature);
  if (!check) {
    check = ajv.compile(feature.value_schema ?? {});
    if (Object.isFrozen(feature)) valueValidators.set(feature, check);
  }
  return !!check(value);
}
function sameJson(left: Json, right: Json): boolean {
  if (typeof left !== typeof right || left === null || right === null) return left === right;
  if (Array.isArray(left) || Array.isArray(right)) return Array.isArray(left) && Array.isArray(right) &&
    left.length === right.length && left.every((item, index) => sameJson(item, right[index]));
  if (typeof left !== "object" || typeof right !== "object") return left === right;
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length &&
    keys.every(key => Object.hasOwn(right, key) && sameJson(left[key], right[key]));
}
function validJson(value: unknown, depth = 0): value is Json {
  if (depth > 20) return false;
  if (value === null || typeof value === "boolean" || typeof value === "string") return true;
  if (typeof value === "number") return Number.isFinite(value) && (!Number.isInteger(value) || Number.isSafeInteger(value));
  if (Array.isArray(value)) return value.length <= 1000 && value.every((v: unknown) => validJson(v, depth + 1));
  return !!value && typeof value === "object" &&
    (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null) && Object.keys(value).length <= 1000 &&
    Object.values(value).every((v: unknown) => validJson(v, depth + 1));
}
export function validateCatalog(catalog: Catalog): void {
  validate("catalog", catalog);
  const heights = new Map<string, number>();
  const walk = (node: string, path: Set<string>): number => {
    if (path.has(node) || !Object.hasOwn(catalog.features, node) || path.size > 32) throw new Error("invalid ancestor graph");
    const cached = heights.get(node);
    if (cached !== undefined) return cached;
    const height = Math.max(0, ...catalog.features[node].ancestors.map(a => 1 + walk(a, new Set([...path, node]))));
    if (height > 32) throw new Error("ancestor depth exceeded");
    heights.set(node, height); return height;
  };
  for (const [key, feature] of Object.entries(catalog.features)) {
    const apps = feature.applications ?? catalog.applications, shared = feature.experiment?.assignment_boundary;
    if (apps.some(k => !catalog.applications.includes(k)) || shared?.applications.some(k => !apps.includes(k))) throw new Error("invalid application boundary");
    if (feature.value_schema) valueSchema(feature.value_schema);
    if (feature.allowed_values && !validJson(feature.allowed_values)) throw new Error("invalid allowed values");
    if (!matches(feature.baseline, feature) || !matches(feature.disabled_value, feature) ||
        (feature.disabled_value === true)) throw new Error("invalid baseline or disabled value");
    walk(key, new Set());
  }
}
export function payloadOf(bundle: ReleaseBundle, catalog: Catalog): Record<string, unknown> {
  validate("bundle", bundle);
  if (!sameScope(bundle.scope, catalog.scope) || bundle.catalog_revision !== catalog.revision ||
      bundle.created_at >= bundle.expires_at || Buffer.byteLength(bundle.payload_bytes, "utf8") > 1048576 ||
      createHash("sha256").update(bundle.payload_bytes, "utf8").digest("hex") !== bundle.payload_sha256) throw new Error("bundle binding mismatch");
  const payload: unknown = ownJson(bundle.payload_bytes);
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
    if (!feature || !native || typeof native !== "object" || Object.keys(native).some(k => !["defaultValue", "rules"].includes(k)) ||
        !matches(native.defaultValue, feature)) throw new Error("invalid native feature");
    if (native.rules !== undefined && !Array.isArray(native.rules)) throw new Error("invalid rules");
    for (const rule of (native.rules ?? []) as Record<string, unknown>[]) {
      if (!rule || typeof rule !== "object") throw new Error("invalid rule");
      if (Object.keys(rule).some(k => !["force", "coverage", "seed", "hashVersion", "key", "variations", "weights", "meta", "bucketVersion", "hashAttribute", "condition"].includes(k))) throw new Error("unsupported rule capability");
      if (Object.hasOwn(rule, "hashVersion") && rule.hashVersion !== 2) throw new Error("invalid hash version");
      if (Object.hasOwn(rule, "bucketVersion") && (!Number.isSafeInteger(rule.bucketVersion) || (rule.bucketVersion as number) < 0)) throw new Error("invalid bucket version");
      if (rule.condition !== undefined) nativeCondition(rule.condition);
      if (rule.seed !== undefined) ascii(rule.seed);
      if (rule.key !== undefined) ascii(rule.key);
      if (rule.hashAttribute !== undefined && rule.hashAttribute !== "id") throw new Error("unsupported hash attribute");
      if (["fallbackAttribute", "namespace", "parentConditions", "prerequisites", "range", "filters", "url", "urlPatterns", "bandit"].some(k => Object.hasOwn(rule, k))) throw new Error("unsupported rule capability");
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
  validate("bundle", bundle);
  const s = bundle.scope;
  return Buffer.from(JSON.stringify(["feature-controls/release/v1", s.ecosystem_id, s.deployment_id, s.environment_id, s.feature_namespace,
    bundle.revision, bundle.catalog_revision, bundle.created_at, bundle.expires_at, bundle.provider_semantics,
    bundle.payload_sha256, bundle.approval_ref]), "utf8");
}

export function boundaryOf(feature: Feature, context: Context): AssignmentBoundary {
  const boundary = feature.experiment?.assignment_boundary;
  return boundary?.applications.includes(context.application_id) ? { mode: "shared", key: boundary.key } : { mode: "application", key: context.application_id };
}
export function allocationId(context: Context, boundary: AssignmentBoundary): string {
  const s = context.scope;
  return [s.ecosystem_id, s.deployment_id, s.environment_id, s.feature_namespace, boundary.mode, boundary.key, context.assignment_key]
    .map(part => `${part.length}.${part}`).join(".");
}

export function attributesOf(context: Context): { id: string; groups: string[]; roles: string[]; tenant: string } {
  const graph = context.group_ancestors, closures = new Map<string, Set<string>>(), heights = new Map<string, number>();
  const walk = (key: string, path: Set<string>): Set<string> => {
    if (path.has(key) || path.size > 32 || !Object.hasOwn(graph, key)) throw new Error("invalid membership graph");
    const cached = closures.get(key); if (cached) return cached;
    const result = new Set([key]); let height = 0;
    for (const parent of graph[key]) {
      for (const ancestor of walk(parent, new Set([...path, key]))) result.add(ancestor);
      height = Math.max(height, 1 + heights.get(parent)!);
    }
    if (height > 32) throw new Error("membership depth exceeded");
    heights.set(key, height);
    closures.set(key, result); return result;
  };
  for (const key of Object.keys(graph)) walk(key, new Set());
  const groups = new Set<string>();
  for (const key of context.groups) for (const ancestor of walk(key, new Set())) groups.add(ancestor);
  return { id: context.assignment_key, groups: [...groups].sort(), roles: [...context.roles], tenant: context.tenant_key };
}

function nativeCondition(value: unknown): void {
  if (!value || typeof value !== "object" || Array.isArray(value) || !Object.keys(value).length || Object.keys(value).length > 3) throw new Error("invalid condition");
  for (const [attribute, condition] of Object.entries(value as Record<string, unknown>)) {
    if (!["groups", "roles", "tenant"].includes(attribute) || !condition || typeof condition !== "object" || Array.isArray(condition)) throw new Error("unsupported condition");
    const operators = Object.entries(condition as Record<string, unknown>); if (operators.length !== 1) throw new Error("invalid condition operator");
    const [operator, operand] = operators[0];
    if (attribute === "tenant") {
      if (!["$eq", "$ne"].includes(operator)) throw new Error("unsupported condition operator");
      ascii(operand);
    } else {
      if (!["$in", "$nin"].includes(operator) || !Array.isArray(operand) || !operand.length || operand.length > 128) throw new Error("unsupported condition operator");
      for (const key of operand) ascii(key);
      if (new Set(operand).size !== operand.length) throw new Error("duplicate condition value");
    }
  }
}

function valueSchema(value: Record<string, unknown>, depth = 0): void {
  const keywords = ["type", "enum", "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "items", "properties", "required", "additionalProperties"];
  if (depth > 8 || !validJson(value) || JSON.stringify(value).length > 16384 || Object.keys(value).some(k => !keywords.includes(k)) ||
      !["null", "boolean", "number", "integer", "string", "array", "object"].includes(value.type as string)) throw new Error("unsupported value schema");
  if (value.enum !== undefined && (!Array.isArray(value.enum) || !value.enum.length || value.enum.length > 128 || !validJson(value.enum))) throw new Error("invalid value enum");
  if (value.additionalProperties !== undefined && value.additionalProperties !== false) throw new Error("object schema must be closed");
  if (value.type === "object" && value.additionalProperties !== false) throw new Error("object schema must be closed");
  const allowedByType: Record<string, string[]> = {
    null: [], boolean: [], number: ["minimum", "maximum"], integer: ["minimum", "maximum"],
    string: ["minLength", "maxLength"], array: ["minItems", "maxItems", "items"],
    object: ["properties", "required", "additionalProperties"],
  };
  const structural = ["minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "items", "properties", "required", "additionalProperties"];
  if (structural.some(k => value[k] !== undefined && !allowedByType[value.type as string].includes(k))) throw new Error("schema keyword does not apply to type");
  for (const field of ["minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"]) {
    if (value[field] !== undefined && (typeof value[field] !== "number" || !Number.isFinite(value[field]))) throw new Error("invalid value bound");
  }
  if (value.properties !== undefined) {
    if (!value.properties || typeof value.properties !== "object" || Array.isArray(value.properties) || Object.keys(value.properties).length > 64) throw new Error("invalid schema properties");
    for (const [key, child] of Object.entries(value.properties)) { ascii(key); valueSchema(child as Record<string, unknown>, depth + 1); }
  }
  if (value.items !== undefined) valueSchema(value.items as Record<string, unknown>, depth + 1);
  if (!ajv.validateSchema(value)) throw new Error("invalid value schema");
  ajv.compile(value);
}

export function ownJson(bytes: string): unknown {
  return JSON.parse(bytes, (_key: string, value: unknown): unknown =>
    value && typeof value === "object" && !Array.isArray(value) ? Object.assign(Object.create(null) as Record<string, unknown>, value) : value) as unknown;
}
