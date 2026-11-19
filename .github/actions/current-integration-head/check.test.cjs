const assert = require('node:assert/strict');
const test = require('node:test');
const check = require('./check.cjs');

const repo = { owner: 'owner', repo: 'product' };
const sha = '1'.repeat(40);
const observed = (value, ref = 'refs/heads/staging', type = 'commit') => ({
  data: { ref, object: { type, sha: value } },
});
function github(result, ref = 'heads/staging') {
  return { rest: { git: { getRef: async ({ request, ...args }) => {
    assert.deepEqual(args, { ...repo, ref });
    assert.ok(request.signal instanceof AbortSignal);
    request.signal.throwIfAborted();
    if (result instanceof Error) throw result;
    return result;
  } } } };
}
test('observes the exact current and superseded candidate', async () => {
  assert.equal(await check(github(observed(sha)), repo, 'staging', sha), 'current');
  assert.equal(await check(github(observed('2'.repeat(40))), repo, 'staging', sha), 'superseded');
});
test('permits Git integration branch punctuation', async () => {
  const branch = 'integration/qa+prod';
  const ref = `heads/${branch}`;
  assert.equal(await check(github(observed(sha, `refs/${ref}`), ref), repo, branch, sha), 'current');
});
test('an expired GitHub ref request cannot select a candidate', async (t) => {
  const timeout = new DOMException('Ref request timed out', 'TimeoutError');
  t.mock.method(AbortSignal, 'timeout', (duration) => {
    assert.equal(duration, 15_000);
    return AbortSignal.abort(timeout);
  });
  await assert.rejects(check(github(observed(sha)), repo, 'staging', sha), { name: 'TimeoutError' });
});
test('unavailable and malformed refs cannot select a candidate', async () => {
  for (const result of [
    new Error('unavailable'), observed(sha, 'refs/tags/staging'),
    observed(sha, 'refs/heads/staging', 'tag'), observed('0'.repeat(40)),
  ]) {
    await assert.rejects(check(github(result), repo, 'staging', sha));
  }
  await assert.rejects(check(github(observed(sha)), repo, 'staging', 'main'));
  await assert.rejects(check(github(observed(sha)), repo, 'refs/tags/staging', sha));
});
