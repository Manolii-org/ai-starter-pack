import { readinessProbe } from './release-policy-helpers.mjs';
globalThis.fetch = () => { throw new Error('offline release qualification attempted network'); };
console.log(JSON.stringify(await readinessProbe()));
