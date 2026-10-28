#!/usr/bin/env node
// UI smoke: load the routes a change affects in a real browser and fail on
// the defects a code review cannot see — a page that errors, a broken asset,
// a console/runtime error, a mobile layout that overflows, or a serious
// accessibility violation. Deterministic and bounded: no model calls.
//
// The same script runs in three places (docs/qa-framework.md):
//   1. agent finalise gate — an agent-check recipe with append_changed_paths,
//      so only routes affected by the candidate are visited;
//   2. ui-smoke-reusable.yml against a preview deployment (`--all`);
//   3. the same reusable on a schedule against production (synthetic monitor).
//
// Usage:
//   node scripts/qa/ui-smoke.mjs [--config config/qa.json] [--base-url URL]
//        [--all | --routes /a,/b] [--profile default|launch] [changed paths...]
//
// Exit: 0 passed, 1 failed, 2 config error, 3 unavailable (no browser or
// server; with on_unavailable=skip it is 0). The finalise gate reads 3 as
// incomplete validation, never as a source failure; CI fails on it.
// Artifacts (screenshots, ARIA snapshots, report.json) go to the ignored
// .ai/agent-checks/ui-smoke/ so they never alter the validated tree.

import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ARTIFACT_DIR = '.ai/agent-checks/ui-smoke';
const MAX_ISSUES_PER_PAGE = 5;
const ISSUE_CHARS = 200;
const UNAVAILABLE_EXIT = 3;
// A Next.js dev server compiles a route on first request; a slow answer is
// still an answer, so the probe waits long enough for that compile.
const PROBE_TIMEOUT_MS = 20000;
const DEFAULTS = {
  base_url: 'http://localhost:3000',
  start_command: null,
  ready_timeout_seconds: 90,
  always_routes: ['/'],
  routes: [],
  infer_next_routes: true,
  dynamic_samples: {},
  max_routes: 6,
  navigation_timeout_ms: 30000,
  viewports: [
    { name: 'mobile', width: 375, height: 812 },
    { name: 'desktop', width: 1280, height: 800 },
  ],
  overflow_check: true,
  axe: { enabled: true, required: false, impacts: ['critical', 'serious'],
    tags: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] },
  ignore_console: [],
  ignore_requests: [],
  // Sent only to the base URL's origin, and only when the variable is set.
  headers_from_env: { 'x-vercel-protection-bypass': 'VERCEL_AUTOMATION_BYPASS_SECRET' },
  storage_state: null,
  on_unavailable: 'fail',
  screenshots: true,
  aria_snapshots: true,
  launch: { network: 'slow-3g', cpu_throttle: 4, max_dom_content_loaded_ms: 10000, max_lcp_ms: 12000 },
};
// Chromium DevTools presets (kbps → bytes/s; latency ms).
const NETWORKS = {
  'slow-3g': { latency: 400, downloadThroughput: (400 * 1024) / 8, uploadThroughput: (400 * 1024) / 8 },
  'fast-3g': { latency: 150, downloadThroughput: (1.6 * 1024 * 1024) / 8, uploadThroughput: (750 * 1024) / 8 },
};

class ConfigError extends Error {}
class Unavailable extends Error {}

const WATCHDOG = `
const [parent, server] = process.argv.slice(1).map(Number);
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
const timer = setInterval(() => {
  if (!alive(server)) { clearInterval(timer); return; }
  if (!alive(parent)) {
    try { process.kill(-server, 'SIGTERM'); } catch {}
    setTimeout(() => { try { process.kill(-server, 'SIGKILL'); } catch {} process.exit(0); }, 1000);
    clearInterval(timer);
  }
}, 500);
`;

function parseArgs(argv) {
  const args = { config: 'config/qa.json', baseUrl: null, all: false, routes: null, profile: 'default', changed: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const value = argv[i];
    const next = () => {
      if (i + 1 >= argv.length) throw new ConfigError(`${value} needs a value`);
      i += 1;
      return argv[i];
    };
    if (value === '--config') args.config = next();
    else if (value === '--base-url') args.baseUrl = next();
    else if (value === '--all') args.all = true;
    else if (value === '--routes') args.routes = next().split(',').map((r) => r.trim()).filter(Boolean);
    else if (value === '--profile') args.profile = next();
    else if (value.startsWith('--')) throw new ConfigError(`unknown option ${value}`);
    else args.changed.push(value);
  }
  if (!['default', 'launch'].includes(args.profile)) throw new ConfigError('--profile must be default or launch');
  return args;
}

function loadConfig(root, file) {
  const full = path.resolve(root, file);
  let section = {};
  if (fs.existsSync(full)) {
    let data;
    try {
      data = JSON.parse(fs.readFileSync(full, 'utf8'));
    } catch (error) {
      throw new ConfigError(`${file} is not valid JSON: ${error.message}`);
    }
    if (data.schema_version !== 1) throw new ConfigError(`${file} requires schema_version=1`);
    section = data.ui_smoke ?? {};
  }
  const config = { ...DEFAULTS, ...section, axe: { ...DEFAULTS.axe, ...(section.axe ?? {}) },
    launch: { ...DEFAULTS.launch, ...(section.launch ?? {}) } };
  if (!Array.isArray(config.viewports) || config.viewports.length === 0) throw new ConfigError('ui_smoke.viewports must be a nonempty array');
  if (!Array.isArray(config.routes)) throw new ConfigError('ui_smoke.routes must be an array');
  if (config.start_command !== null && !(Array.isArray(config.start_command) && config.start_command.every((s) => typeof s === 'string' && s))) {
    throw new ConfigError('ui_smoke.start_command must be an argv array of strings or null');
  }
  if (!['fail', 'skip'].includes(config.on_unavailable)) throw new ConfigError('ui_smoke.on_unavailable must be fail or skip');
  if (!Number.isInteger(config.max_routes) || config.max_routes < 1) throw new ConfigError('ui_smoke.max_routes must be a positive integer');
  if (!(config.launch.network in NETWORKS)) throw new ConfigError(`ui_smoke.launch.network must be one of ${Object.keys(NETWORKS).join(', ')}`);
  return config;
}

// ── route selection ─────────────────────────────────────────────────────────

export function globToRegExp(glob) {
  let out = '';
  for (let i = 0; i < glob.length; i += 1) {
    const c = glob[i];
    if (c === '*') {
      if (glob[i + 1] === '*') {
        out += glob[i + 2] === '/' ? '(?:.*/)?' : '.*';
        i += glob[i + 2] === '/' ? 2 : 1;
      } else out += '[^/]*';
    } else if (c === '?') out += '[^/]';
    else out += c.replace(/[.+^${}()|[\]\\]/g, '\\$&');
  }
  // Owner-authored glob with every regex metacharacter escaped above.
  return new RegExp(`^${out}$`); // nosemgrep: javascript.lang.security.audit.detect-non-literal-regexp.detect-non-literal-regexp
}

// Router roots only at the repository root, src/, or a monorepo
// apps|packages/<name>/ prefix: a `pages/` folder elsewhere (Playwright page
// objects, components/pages) is not a router and must not invent routes.
const ROUTER_ROOT = String.raw`^(?:(?:apps|packages)\/[^/]+\/)?(?:src\/)?`;
const NEXT_APP = new RegExp(`${ROUTER_ROOT}app\\/((?:[^/]+\\/)*)(page|layout|template|loading|error|not-found)\\.(?:[cm]?[jt]sx?|mdx)$`);
const NEXT_PAGES = new RegExp(`${ROUTER_ROOT}pages\\/(.*)\\.(?:[cm]?[jt]sx?|mdx)$`);
const NOT_A_ROUTE_FILE = /\.(?:test|spec|stories|story)\.[^/]+$/;

// Map a Next.js file to the route it renders, or null when it is not a
// route file. Dynamic segments resolve only through dynamic_samples.
export function inferNextRoute(file, samples = {}) {
  if (NOT_A_ROUTE_FILE.test(file)) return null;
  let segments;
  const app = file.match(NEXT_APP);
  if (app) {
    segments = app[1].split('/').filter(Boolean);
    // `_private` folders are not routable; `(.)` marks an intercepting route.
    if (segments.some((s) => s.startsWith('_') || s.startsWith('(.'))) return null;
    segments = segments.filter((s) => !(s.startsWith('(') && s.endsWith(')')) && !s.startsWith('@'));
  } else {
    const pages = file.match(NEXT_PAGES);
    if (!pages) return null;
    segments = pages[1].split('/').filter(Boolean);
    if (segments[0] === 'api' || segments.some((s) => s.startsWith('_'))) return null;
    if (segments[segments.length - 1] === 'index') segments.pop();
  }
  const route = `/${segments.join('/')}`;
  if (!route.includes('[')) return route;
  return samples[route] ?? null;
}

export function selectRoutes(config, args) {
  if (args.routes) return { routes: args.routes, notes: [] };
  const notes = [];
  if (args.all) {
    const all = new Set([...config.always_routes, ...config.routes.map((r) => r.path)]);
    return { routes: [...all], notes };
  }
  const selected = new Set();
  for (const file of args.changed) {
    let mapped = false;
    for (const route of config.routes) {
      if ((route.sources ?? []).some((glob) => globToRegExp(glob).test(file))) {
        selected.add(route.path);
        mapped = true;
      }
    }
    if (config.infer_next_routes) {
      const inferred = inferNextRoute(file, config.dynamic_samples);
      if (inferred) {
        selected.add(inferred);
        mapped = true;
      } else if (/\[[^/]+\]/.test(file) && (NEXT_APP.test(file) || NEXT_PAGES.test(file))) {
        notes.push(`dynamic route ${file} skipped: add a ui_smoke.dynamic_samples entry`);
      }
    }
    if (!mapped) for (const route of config.always_routes) selected.add(route);
  }
  if (selected.size === 0) for (const route of config.always_routes) selected.add(route);
  let routes = [...selected].sort();
  if (routes.length > config.max_routes) {
    notes.push(`${routes.length} routes affected; smoking the first ${config.max_routes} (ui_smoke.max_routes)`);
    routes = routes.slice(0, config.max_routes);
  }
  return { routes, notes };
}

// ── environment ─────────────────────────────────────────────────────────────

function requireFrom(root, names) {
  const req = createRequire(path.join(root, 'package.json'));
  const paths = [root, ...(process.env.NODE_PATH ?? '').split(path.delimiter).filter(Boolean),
    path.join(path.dirname(process.execPath), '..', 'lib', 'node_modules')];
  for (const name of names) {
    try {
      return { module: req(req.resolve(name, { paths })), name };
    } catch { /* try the next candidate */ }
  }
  return null;
}

async function reachable(url) {
  try {
    await fetch(url, { signal: AbortSignal.timeout(PROBE_TIMEOUT_MS), redirect: 'manual' });
    return true;
  } catch {
    return false;
  }
}

async function ensureServer(root, config, baseUrl) {
  if (await reachable(baseUrl)) return { stop: async () => {} };
  if (!config.start_command) {
    throw new Unavailable(`no server at ${baseUrl} and ui_smoke.start_command is not set`);
  }
  const [cmd, ...cmdArgs] = config.start_command;
  // Server output goes to a log beside the other artifacts, so a server that
  // will not start explains itself without flooding the agent's context.
  const logPath = path.join(root, ARTIFACT_DIR, 'server.log');
  const logFd = fs.openSync(logPath, 'w');
  const child = spawn(cmd, cmdArgs, { cwd: root, detached: true, stdio: ['ignore', logFd, logFd], env: { ...process.env, BROWSER: 'none' } });
  fs.closeSync(logFd);
  const logTail = () => {
    try {
      const lines = fs.readFileSync(logPath, 'utf8').split('\n').map((l) => l.trim()).filter(Boolean);
      return lines.length ? ` — last output: ${trim(lines.slice(-3).join(' | '))}` : '';
    } catch { return ''; }
  };
  let exited = false;
  child.on('exit', () => { exited = true; });
  child.on('error', () => { exited = true; });
  // Watchdog: if this process dies uncatchably (SIGKILL from a recipe
  // timeout), a detached sibling still stops the server's process group.
  if (child.pid) {
    spawn(process.execPath, ['-e', WATCHDOG, String(process.pid), String(child.pid)],
      { detached: true, stdio: 'ignore' }).unref();
  }
  const killGroup = (signal) => { try { process.kill(-child.pid, signal); } catch { /* already gone */ } };
  const stop = async () => {
    if (exited) return;
    killGroup('SIGTERM');
    await new Promise((resolve) => setTimeout(resolve, 500));
    killGroup('SIGKILL');
  };
  for (const signal of ['SIGTERM', 'SIGINT', 'SIGHUP']) {
    process.once(signal, () => { killGroup('SIGTERM'); setTimeout(() => { killGroup('SIGKILL'); process.exit(130); }, 300); });
  }
  const deadline = Date.now() + config.ready_timeout_seconds * 1000;
  while (Date.now() < deadline) {
    if (exited) throw new Unavailable(`start_command exited before ${baseUrl} was reachable${logTail()}`);
    if (await reachable(baseUrl)) return { stop };
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  await stop();
  throw new Unavailable(`${baseUrl} not reachable within ${config.ready_timeout_seconds}s of start_command${logTail()}`);
}

// ── page checks ─────────────────────────────────────────────────────────────

const trim = (text) => String(text).replace(/\s+/g, ' ').slice(0, ISSUE_CHARS);
const slug = (route) => (route === '/' ? 'root' : route.replace(/^\//, '').replace(/[^A-Za-z0-9._-]+/g, '_'));

async function checkPage(context, { route, viewport, baseUrl, config, axeSource, runAxe, launch, outDir }) {
  const page = await context.newPage();
  const issues = [];
  const origin = new URL(baseUrl).origin;
  // Patterns come from the repository's own config/qa.json (trusted, like
  // package.json scripts), never from page or network content.
  const ignoredConsole = config.ignore_console.map((r) => new RegExp(r)); // nosemgrep: javascript.lang.security.audit.detect-non-literal-regexp.detect-non-literal-regexp
  const ignoredRequests = config.ignore_requests.map((r) => new RegExp(r)); // nosemgrep: javascript.lang.security.audit.detect-non-literal-regexp.detect-non-literal-regexp
  const sameOrigin = (url) => { try { return new URL(url).origin === origin; } catch { return false; } };
  const requestIgnored = (url) => ignoredRequests.some((r) => r.test(url));
  page.on('pageerror', (error) => issues.push(`runtime error: ${trim(error.message)}`));
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    const text = message.text();
    // Resource failures are judged by the request rules below, with origin
    // context. Errors logged by third-party scripts (analytics, widgets) are
    // not this app's defects and would make production smokes flap.
    const source = message.location()?.url ?? '';
    if (/^Failed to load resource/.test(text) || (source && !sameOrigin(source))
        || ignoredConsole.some((r) => r.test(text))) return;
    issues.push(`console error: ${trim(text)}`);
  });
  page.on('requestfailed', (request) => {
    const failure = request.failure()?.errorText ?? 'failed';
    if (!sameOrigin(request.url()) || requestIgnored(request.url()) || /ERR_ABORTED|NS_BINDING_ABORTED/.test(failure)) return;
    issues.push(`request failed: ${trim(`${request.url()} ${failure}`)}`);
  });
  page.on('response', (response) => {
    const url = response.url();
    if (!sameOrigin(url) || requestIgnored(url) || response.request().isNavigationRequest()) return;
    const status = response.status();
    const type = response.request().resourceType();
    if (status >= 500 || (status >= 400 && ['script', 'stylesheet', 'font'].includes(type))) {
      issues.push(`${type} ${status}: ${trim(url)}`);
    }
  });
  let cdp = null;
  if (launch) {
    cdp = await context.newCDPSession(page);
    const network = NETWORKS[config.launch.network];
    if (network) await cdp.send('Network.emulateNetworkConditions', { offline: false, ...network });
    if (config.launch.cpu_throttle > 1) await cdp.send('Emulation.setCPUThrottlingRate', { rate: config.launch.cpu_throttle });
  }
  const timing = {};
  try {
    const response = await page.goto(new URL(route, baseUrl).toString(), {
      waitUntil: 'domcontentloaded', timeout: config.navigation_timeout_ms * (launch ? 3 : 1),
    });
    const status = response ? response.status() : 0;
    if (!response || status >= 400) issues.unshift(`document HTTP ${status || 'no response'}`);
    await page.waitForLoadState('load', { timeout: config.navigation_timeout_ms }).catch(() => {});
    await page.waitForLoadState('networkidle', { timeout: 3000 }).catch(() => {});
    if (launch) {
      // DOMContentLoaded plus Largest Contentful Paint (the Core Web Vital
      // Lighthouse budgets use), read from the page's own buffered entries.
      Object.assign(timing, await page.evaluate(() => new Promise((resolve) => {
        const nav = performance.getEntriesByType('navigation')[0];
        const dcl = nav ? Math.round(nav.domContentLoadedEventEnd) : null;
        let lcp = null;
        try {
          new PerformanceObserver((list) => {
            const entries = list.getEntries();
            if (entries.length) lcp = Math.round(entries[entries.length - 1].startTime);
          }).observe({ type: 'largest-contentful-paint', buffered: true });
        } catch { /* LCP unsupported: report DCL only */ }
        setTimeout(() => resolve({ dom_content_loaded_ms: dcl, lcp_ms: lcp }), 250);
      })));
      if (timing.dom_content_loaded_ms > config.launch.max_dom_content_loaded_ms) {
        issues.push(`slow on ${config.launch.network}: DOMContentLoaded ${timing.dom_content_loaded_ms}ms > ${config.launch.max_dom_content_loaded_ms}ms`);
      }
      if (timing.lcp_ms > config.launch.max_lcp_ms) {
        issues.push(`slow on ${config.launch.network}: LCP ${timing.lcp_ms}ms > ${config.launch.max_lcp_ms}ms`);
      }
    }
    if (config.overflow_check && viewport.width <= 480 && status > 0 && status < 400) {
      const layout = await page.evaluate(() => ({
        responsive: /width\s*=\s*device-width/.test(document.querySelector('meta[name="viewport"]')?.content ?? ''),
        width: Math.max(document.documentElement.scrollWidth, window.innerWidth),
      }));
      // Without a width=device-width viewport meta a phone lays the page out
      // at ~980px and zooms out. With it, mobile Chromium widens the layout
      // viewport to fit overflowing content, so compare against the screen.
      if (!layout.responsive) issues.push('no responsive viewport meta (width=device-width): phones render a zoomed-out desktop layout');
      else if (layout.width - viewport.width > 2) issues.push(`horizontal overflow ${layout.width - viewport.width}px at ${viewport.width}px wide`);
    }
    if (runAxe && axeSource) {
      await page.evaluate(axeSource);
      const result = await page.evaluate(async (tags) => window.axe.run(document, {
        runOnly: { type: 'tag', values: tags }, resultTypes: ['violations'],
      }), config.axe.tags);
      for (const violation of result.violations) {
        if (config.axe.impacts.includes(violation.impact)) {
          issues.push(`a11y ${violation.impact}: ${violation.id} (${violation.nodes.length} node${violation.nodes.length === 1 ? '' : 's'})`);
        }
      }
    }
    const base = path.join(outDir, `${slug(route)}.${viewport.name}${launch ? '.launch' : ''}`);
    if (config.screenshots) await page.screenshot({ path: `${base}.png` }).catch(() => {});
    if (config.aria_snapshots && runAxe && typeof page.locator('body').ariaSnapshot === 'function') {
      const aria = await page.locator('body').ariaSnapshot().catch(() => '');
      if (aria) fs.writeFileSync(`${base}.aria.yml`, aria.slice(0, 20000));
    }
  } catch (error) {
    issues.unshift(`navigation: ${trim(error.message.split('\n')[0])}`);
  } finally {
    if (cdp) await cdp.detach().catch(() => {});
    await page.close().catch(() => {});
  }
  return { route, viewport: viewport.name, launch, issues, ...timing };
}

// ── main ────────────────────────────────────────────────────────────────────

function writeReport(root, report) {
  const dir = path.join(root, ARTIFACT_DIR);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'report.json'), `${JSON.stringify(report, null, 2)}\n`);
}

async function main() {
  const root = process.cwd();
  let args;
  let config;
  try {
    args = parseArgs(process.argv.slice(2));
    config = loadConfig(root, args.config);
  } catch (error) {
    console.error(`UI-SMOKE-CONFIG-ERROR: ${error.message}`);
    return 2;
  }
  const baseUrl = args.baseUrl ?? process.env.QA_BASE_URL ?? config.base_url;
  const { routes, notes } = selectRoutes(config, args);
  const outDir = path.join(root, ARTIFACT_DIR);
  fs.rmSync(outDir, { recursive: true, force: true });
  fs.mkdirSync(outDir, { recursive: true });
  const report = { status: 'passed', base_url: baseUrl, profile: args.profile, routes, notes, results: [] };
  const unavailable = (reason) => {
    report.status = 'unavailable';
    report.reason = reason;
    writeReport(root, report);
    const verdict = config.on_unavailable === 'skip' ? 'UI-SMOKE-INCOMPLETE' : 'UI-SMOKE-UNAVAILABLE';
    console.log(`${verdict}: ${reason}`);
    return config.on_unavailable === 'skip' ? 0 : UNAVAILABLE_EXIT;
  };

  const pw = requireFrom(root, ['playwright', '@playwright/test', 'playwright-core']);
  if (!pw?.module?.chromium) return unavailable('playwright is not installed (npm i -D playwright)');
  let axeSource = null;
  if (config.axe.enabled) {
    const axe = requireFrom(root, ['axe-core']) ?? requireFrom(path.join(root, 'node_modules', '@axe-core', 'playwright'), ['axe-core']);
    axeSource = axe?.module?.source ?? null;
    if (!axeSource) {
      if (config.axe.required) return unavailable('axe-core is not installed (npm i -D axe-core) and ui_smoke.axe.required is true');
      notes.push('a11y skipped: axe-core not installed');
    }
  }
  const headers = {};
  for (const [header, envName] of Object.entries(config.headers_from_env)) {
    if (process.env[envName]) headers[header] = process.env[envName];
  }

  let server;
  try {
    server = await ensureServer(root, config, baseUrl);
  } catch (error) {
    if (error instanceof Unavailable) return unavailable(error.message);
    throw error;
  }
  let browser;
  try {
    try {
      browser = await pw.module.chromium.launch();
    } catch (error) {
      return unavailable(`chromium could not launch (npx playwright install chromium): ${trim(error.message.split('\n')[0])}`);
    }
    console.log(`UI-SMOKE base=${baseUrl} profile=${args.profile} routes=${routes.join(',')} viewports=${config.viewports.map((v) => v.name).join(',')}`);
    for (const note of notes) console.log(`note: ${note}`);
    const storageState = config.storage_state && fs.existsSync(path.resolve(root, config.storage_state))
      ? path.resolve(root, config.storage_state) : undefined;
    const plan = [];
    for (const route of routes) {
      config.viewports.forEach((viewport, index) => plan.push({ route, viewport, runAxe: index === 0, launch: false }));
      if (args.profile === 'launch') {
        const mobile = config.viewports.find((v) => v.width <= 480) ?? config.viewports[0];
        plan.push({ route, viewport: mobile, runAxe: false, launch: true });
      }
    }
    for (const item of plan) {
      const context = await browser.newContext({
        viewport: { width: item.viewport.width, height: item.viewport.height },
        isMobile: item.viewport.width <= 480, hasTouch: item.viewport.width <= 480,
        storageState, ignoreHTTPSErrors: false,
      });
      if (Object.keys(headers).length > 0) {
        // Never extraHTTPHeaders: that would hand a bypass secret to every
        // third-party origin the page loads (CDN, analytics, identity).
        const origin = new URL(baseUrl).origin;
        // fetch + fulfill with maxRedirects 0 (not route.continue): continue()
        // keeps the injected headers on redirect hops, so a 302 to an SSO or
        // CDN origin would receive the secret. Each hop re-enters this
        // predicate instead, and only same-origin hops get the headers.
        // A fulfilled document loses its address space, and Chromium's
        // Private Network Access then blocks its localhost subresources, so
        // a document is only fulfilled when it is a redirect. Otherwise the
        // browser's own request goes out (one extra same-origin GET).
        await context.route((url) => url.origin === origin, async (route) => {
          const request = route.request();
          const merged = { ...request.headers(), ...headers };
          try {
            const response = await route.fetch({ headers: merged, maxRedirects: 0 });
            const redirect = response.status() >= 300 && response.status() < 400;
            if (request.isNavigationRequest() && !redirect && request.method() === 'GET') {
              await route.continue({ headers: merged });
            } else {
              await route.fulfill({ response });
            }
          } catch {
            // Surface as a failed request instead of leaving it hanging.
            await route.abort('failed').catch(() => {});
          }
        });
      }
      const result = await checkPage(context, { ...item, baseUrl, config, axeSource, outDir });
      await context.close().catch(() => {});
      report.results.push(result);
      const label = `${item.route} ${item.viewport.name}${item.launch ? ` launch(${config.launch.network})` : ''}`;
      if (result.issues.length === 0) {
        const perf = [result.dom_content_loaded_ms && `dcl=${result.dom_content_loaded_ms}ms`, result.lcp_ms && `lcp=${result.lcp_ms}ms`].filter(Boolean).join(' ');
        console.log(`PASS ${label}${perf ? ` ${perf}` : ''}`);
      } else {
        const shown = result.issues.slice(0, MAX_ISSUES_PER_PAGE);
        const more = result.issues.length - shown.length;
        console.log(`FAIL ${label}: ${shown.join('; ')}${more > 0 ? `; +${more} more` : ''}`);
      }
    }
  } finally {
    if (browser) await browser.close().catch(() => {});
    await server.stop();
  }
  const failed = report.results.filter((r) => r.issues.length > 0).length;
  report.status = failed ? 'failed' : 'passed';
  writeReport(root, report);
  const artifacts = path.relative(root, outDir) || '.';
  console.log(`UI-SMOKE-${failed ? 'FAILED' : 'PASSED'} ${report.results.length - failed}/${report.results.length} page checks passed — artifacts: ${artifacts}/`);
  return failed ? 1 : 0;
}

const invokedDirectly = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) {
  main().then((code) => process.exit(code), (error) => {
    console.error(`UI-SMOKE-ERROR: ${error.stack ?? error}`);
    process.exit(1);
  });
}
