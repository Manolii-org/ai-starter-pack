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
  targets: [{ name: 'web', engine: web(), app: { url: process.env.BB_APP_URL ?? 'http://localhost:3000' } }],
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
  },
} satisfies E2EConfig;
