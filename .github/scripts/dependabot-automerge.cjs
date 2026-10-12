'use strict';

// No PR source is evaluated. All input below is untrusted API/metadata data.
const REPOSITORY = 'ihuzaifashoukat/x-use';
const BOT_ID = 49699333;
const PATCH = 'version-update:semver-patch';
const MINOR = 'version-update:semver-minor';
const SHA = /^[a-f0-9]{40}$/;
const ECOSYSTEMS = new Set(['uv', 'github_actions', 'docker']);
const DISABLE = `mutation($id: ID!) {
  disablePullRequestAutoMerge(input: {pullRequestId: $id}) { pullRequest { id } }
}`;
const ENABLE = `mutation($id: ID!, $head: GitObjectID!) {
  enablePullRequestAutoMerge(input: {
    pullRequestId: $id, expectedHeadOid: $head, mergeMethod: MERGE
  }) { pullRequest { id } }
}`;

function reject(reason) { throw new PolicyRejection(reason); }
class PolicyRejection extends Error {}
function bot(user) {
  return user?.login === 'dependabot[bot]' && user?.id === BOT_ID && user?.type === 'Bot';
}
function repository(repo, id) {
  return repo?.full_name === REPOSITORY && repo.id === id;
}
function scope(pr, context) {
  const id = context.payload.repository?.id;
  return Number.isSafeInteger(id) && id > 0 &&
    `${context.repo.owner}/${context.repo.repo}` === REPOSITORY &&
    Number.isSafeInteger(pr?.number) && pr.number > 0 &&
    pr.number === context.payload.pull_request?.number &&
    repository(pr.base?.repo, id) && bot(pr.user) &&
    typeof pr.node_id === 'string' && pr.node_id.length > 0;
}
function checkSnapshot(pr, context, expected = {}) {
  if (!scope(pr, context)) reject('PR is outside the trusted Dependabot scope.');
  const event = context.payload.pull_request;
  if (pr.state !== 'open' || pr.merged || pr.draft) reject('PR is not open and ready.');
  if (pr.base.ref !== 'main') reject('PR does not target main.');
  if (!repository(pr.head?.repo, context.payload.repository.id)) reject('Fork PR requires manual review.');
  if (!SHA.test(pr.head.sha) || !SHA.test(pr.base.sha)) reject('Missing immutable commit identity.');
  if (pr.head.sha !== event.head?.sha || pr.head.ref !== event.head?.ref ||
      pr.base.ref !== event.base?.ref || pr.title !== event.title || pr.body !== event.body) {
    reject('Event is stale; wait for the current PR event.');
  }
  if ((expected.head && expected.head !== pr.head.sha) ||
      (expected.base && expected.base !== pr.base.sha)) reject('PR changed during policy evaluation.');
  const parts = pr.head.ref.split('/');
  if (parts[0] !== 'dependabot' || !ECOSYSTEMS.has(parts[1])) reject('Unsupported Dependabot ecosystem.');
  if (pr.commits !== 1) reject('Only a single verified Dependabot commit is eligible.');
  return parts[1];
}
function checkCommit(pr, commits) {
  // fetch-metadata v3.1.0 verifies the first commit, not every commit. Requiring
  // one commit also prevents a maintainer amendment from inheriting bot metadata.
  if (!Array.isArray(commits) || commits.length !== 1 || commits[0].sha !== pr.head.sha ||
      !bot(commits[0].author) || commits[0].commit?.verification?.verified !== true ||
      commits[0].commit.verification.reason !== 'valid') reject('Current commit is not verified Dependabot content.');
  const committer = commits[0].committer;
  if (!bot(committer) && !(committer?.login === 'web-flow' && committer.id === 19864447 &&
      committer.type === 'User')) reject('Commit was amended by an unexpected committer.');
  return commits[0].commit.message;
}
function scalar(value) {
  if (/^'[A-Za-z0-9_.:/@+-]+'$/.test(value) || /^"[A-Za-z0-9_.:/@+-]+"$/.test(value)) return value.slice(1, -1);
  if (/^[A-Za-z0-9_.:/@+-]+$/.test(value)) return value;
  reject('Ambiguous signed dependency metadata requires manual review.');
}
function signedDependencies(message) {
  // Intentionally accept only Dependabot's simple scalar trailer, not general
  // YAML. Future formats, aliases, duplicate keys and inferred types fail closed.
  if (typeof message !== 'string' || message.length > 524288) reject('Invalid commit metadata.');
  const fragments = [...message.replaceAll('\r\n', '\n').matchAll(/^---\n([\s\S]*?)\n\.\.\.(?:\n|$)/gm)];
  if (fragments.length !== 1) reject('Expected one signed dependency trailer.');
  const lines = fragments[0][1].split('\n');
  if (lines.shift() !== 'updated-dependencies:') reject('Unknown signed dependency trailer.');
  const entries = [];
  let current;
  for (const line of lines) {
    const start = line.match(/^- dependency-name: (.+)$/);
    if (start) {
      current = { 'dependency-name': scalar(start[1]) };
      entries.push(current);
      continue;
    }
    const item = line.match(/^  (dependency-version|dependency-type|update-type|dependency-group): (.+)$/);
    if (!item || !current || Object.hasOwn(current, item[1])) reject('Unknown or duplicate signed dependency field.');
    current[item[1]] = scalar(item[2]);
  }
  if (entries.length === 0 || entries.length > 250) reject('Invalid signed dependency count.');
  return entries;
}
function checkMetadata(raw, aggregate, ecosystem, message) {
  if (typeof raw !== 'string' || raw.length > 524288) reject('Invalid metadata output.');
  let entries;
  try { entries = JSON.parse(raw); } catch { reject('Malformed metadata output.'); }
  if (!Array.isArray(entries) || entries.length === 0 || entries.length > 250) reject('Missing dependency metadata.');
  const signed = signedDependencies(message);
  if (signed.length !== entries.length) reject('Metadata does not match signed dependency count.');
  for (let i = 0; i < entries.length; i++) {
    const item = entries[i];
    if (!item || typeof item !== 'object' || Array.isArray(item) ||
        ![PATCH, MINOR].includes(item.updateType) || item.packageEcosystem !== ecosystem ||
        item.directory !== '/' || item.targetBranch !== 'main' || item.maintainerChanges !== false ||
        item.dependencyName !== signed[i]['dependency-name'] ||
        item.updateType !== signed[i]['update-type']) reject('Every signed dependency must be a root patch or minor update.');
  }
  const highest = entries.some(item => item.updateType === MINOR) ? MINOR : PATCH;
  if (aggregate !== highest) reject('Aggregate update type does not match every dependency.');
}
function checkFiles(pr, files, ecosystem) {
  if (!Array.isArray(files) || !Number.isSafeInteger(pr.changed_files) || pr.changed_files < 1 ||
      pr.changed_files > 100 || files.length !== pr.changed_files) reject('Incomplete or excessive changed file list.');
  const names = new Set();
  for (const file of files) {
    const name = file.filename;
    const allowed = ecosystem === 'uv' ? ['pyproject.toml', 'uv.lock'].includes(name) :
      ecosystem === 'docker' ? name === 'Dockerfile' :
        /^\.github\/workflows\/[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml$/.test(name);
    if (!allowed || file.status !== 'modified' || file.previous_filename || names.has(name)) {
      reject('Changed files require manual review.');
    }
    names.add(name);
  }
}
function request(context) {
  return { ...context.repo, pull_number: context.payload.pull_request.number };
}
async function readPR(github, context) { return (await github.rest.pulls.get(request(context))).data; }
async function disarm(github, pr, context) {
  if (scope(pr, context) && pr.state === 'open' && pr.auto_merge) {
    await github.graphql(DISABLE, { id: pr.node_id });
  }
}
async function commitsFor(github, context) {
  return github.paginate(github.rest.pulls.listCommits, { ...request(context), per_page: 100 });
}
function report(core, error) {
  if (!(error instanceof PolicyRejection)) throw error;
  core.info(`Manual review: ${error.message}`);
  return { enabled: false, reason: error.message };
}
async function prepare({ github, context, core }) {
  core.setOutput('eligible', 'false');
  const pr = await readPR(github, context);
  // Revoke before parsing metadata, including on retarget/draft/stale events.
  await disarm(github, pr, context);
  try {
    checkSnapshot(pr, context);
    checkCommit(pr, await commitsFor(github, context));
    core.setOutput('head', pr.head.sha);
    core.setOutput('base', pr.base.sha);
    core.setOutput('eligible', 'true');
  } catch (error) { return report(core, error); }
}
async function apply({ github, context, core, env }) {
  let pr = await readPR(github, context);
  let enableAttemptedFor;
  await disarm(github, pr, context);
  try {
    if (env.METADATA_OUTCOME !== 'success' || !SHA.test(env.EXPECTED_HEAD ?? '') ||
        !SHA.test(env.EXPECTED_BASE ?? '')) reject('Verified metadata step did not complete.');
    const expected = { head: env.EXPECTED_HEAD, base: env.EXPECTED_BASE };
    const ecosystem = checkSnapshot(pr, context, expected);
    const [commits, files] = await Promise.all([
      commitsFor(github, context),
      github.paginate(github.rest.pulls.listFiles, { ...request(context), per_page: 100 }),
    ]);
    const message = checkCommit(pr, commits);
    checkMetadata(env.DEPENDABOT_METADATA, env.DEPENDABOT_UPDATE_TYPE, ecosystem, message);
    checkFiles(pr, files, ecosystem);
    pr = await readPR(github, context);
    checkSnapshot(pr, context, expected);
    // Native auto-merge waits for GitHub's required checks/reviews. This is never
    // a direct merge or bypass, including when GitHub refuses the mutation.
    enableAttemptedFor = pr.node_id;
    await github.graphql(ENABLE, { id: pr.node_id, head: expected.head });
    const after = await readPR(github, context);
    if (after.state === 'open') checkSnapshot(after, context, expected);
    core.info('Eligible patch/minor update queued for GitHub required checks and reviews.');
    return { enabled: true };
  } catch (error) {
    // A concurrent edit or failed metadata/API request must not retain our old
    // permission. If cleanup itself fails, fail the job rather than claim safety.
    if (enableAttemptedFor) {
      // The PR node ID is immutable and was scoped before the enable attempt.
      // Revoke even if a follow-up REST read fails or the enable response was
      // lost; another successful read is not a prerequisite for cleanup.
      await github.graphql(DISABLE, { id: enableAttemptedFor });
    } else {
      await disarm(github, await readPR(github, context), context);
    }
    return report(core, error);
  }
}

module.exports = { prepare, apply, checkSnapshot, checkCommit, signedDependencies, checkMetadata, checkFiles };
