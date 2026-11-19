const SHA = /^[0-9a-f]{40}$/;

module.exports = async function currentIntegrationHead(github, repo, branch, sha) {
  if (typeof branch !== 'string' || !branch || branch.startsWith('refs/')) {
    throw new Error('Expected an owning integration branch name');
  }
  if (!SHA.test(sha) || sha === '0'.repeat(40)) {
    throw new Error('Expected an immutable candidate SHA');
  }
  const ref = `heads/${branch}`;
  // GitHub validates branch syntax; the returned ref must match the requested
  // one exactly, including case and punctuation.
  const { data } = await github.rest.git.getRef({ ...repo, ref });
  if (data.ref !== `refs/${ref}` || data.object?.type !== 'commit' ||
      !SHA.test(data.object?.sha) || data.object.sha === '0'.repeat(40)) {
    throw new Error('Could not verify the exact integration branch ref');
  }
  return data.object.sha === sha ? 'current' : 'superseded';
};
