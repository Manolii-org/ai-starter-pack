import type { E2EConfig } from 'e2e';
import { web } from '@e2e-dev/web';
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';

const litellm = createOpenAICompatible({
  name: 'litellm',
  supportsStructuredOutputs: true,
  baseURL: `${(process.env.BB_LITELLM_URL ?? '').replace(/\/$/, '')}/v1`,
  apiKey: process.env.BB_LITELLM_KEY ?? '',
});

const persona = {
  model: litellm.chatModel(process.env.BB_ACTOR_MODEL ?? 'candidate-luna-vision'),
  judge: litellm.chatModel(process.env.BB_JUDGE_MODEL ?? 'candidate-luna-critic'),
  maxSteps: 40,
  maxModelCalls: 40,
  context:
    (process.env.BB_APP_CONTEXT ?? '') +
    ' This is an isolated test stack with synthetic data. Never sign out, delete the account, or change the email address.' +
    ' Not bugs: a link that opens a new tab leaves this one unchanged; lazy-loaded content below the fold;' +
    ' sparse lists that only reflect thin seed data; features that clearly depend on a missing third-party key.',
};

export default {
  tests: 'tests/**/*.e2e.ts',
  targets: [
    { name: 'web', engine: web(), app: { url: process.env.BB_APP_URL ?? 'http://localhost:3000' } },
    // Phone-sized first-time user. The engine has no network throttling, so
    // slow-network behaviour is measured deterministically by
    // `ui-smoke --profile launch` instead (docs/qa-framework.md).
    { name: 'web-mobile', engine: web({ viewport: { width: 375, height: 812 } }), app: { url: process.env.BB_APP_URL ?? 'http://localhost:3000' } },
  ],
  retries: 0,
  workers: 1,
  reporters: ['list'],
  agents: {
    default: persona,
    skeptic: {
      ...persona,
      system: 'Distrust every number, date, count, and claim on screen; cross-check each against every other place it appears.',
    },
    fuzzer: {
      ...persona,
      system: "At every input, run the goal's input matrix before anything else, judging each entry before the next. Never take the happy path.",
    },
    stateful: {
      ...persona,
      system: 'After every change, reload the page and navigate back and forward; report state that is lost, stale, or duplicated.',
    },
    // Launch-QA personas (SKILL.md § Launch QA). Each is one behavioural
    // constraint, so its findings stay attributable.
    impatient: {
      ...persona,
      system: 'You are a first-time visitor in a hurry. Skim; take the most prominent call to action at every step; never read help text. If anything errors, stalls for more than a few seconds, or needs a second attempt, stop and report exactly where you gave up.',
    },
    'trust-checker': {
      ...persona,
      system: 'Before committing to anything, find and read pricing, terms, and privacy information. Report anything missing, contradictory, hard to find, or that differs from what the sign-up or checkout flow later shows.',
    },
    literal: {
      ...persona,
      system: 'You read English as a second language and act only on what the interface literally says. Never infer meaning from icons, colour, position, or convention. Report every label, instruction, or error message you cannot act on without guessing.',
    },
    breaker: {
      ...persona,
      system: "At every form, paste junk (empty, very long, emoji, HTML, SQL-looking text), double-click submit buttons, and press back mid-flow, then continue. Report crashes, duplicate submissions, lost input, or raw errors shown to the user.",
    },
  },
} satisfies E2EConfig;
