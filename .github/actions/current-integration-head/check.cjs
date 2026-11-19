const SHA = /^[0-9a-f]{40}$/;

module.exports = async function currentIntegrationHead(github, repo, branch, sha) {
  if (!branch || !/^[A-Za-z0-9._/-]+$/.test(branch) || branch.startsWith('refs/')) {
    throw new Error('Expected an owning integration branch name');
  }
  if (!SHA.test(sha) || sha === '0'.repeat(40)) {
    throw new Error('Expected an immutable candidate SHA');
  }
  const ref = `heads/${branch}`;
  const { data } = await github.rest.git.getRef({ ...repo, ref });
  if (data.ref !== `refs/${ref}` || data.object?.type !== 'commit' ||
      !SHA.test(data.object?.sha) || data.object.sha === '0'.repeat(40)) {
    throw new Error('Could not verify the exact integration branch ref');
  }
  return data.object.sha === sha ? 'current' : 'superseded';
};
