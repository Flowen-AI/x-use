'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const policy = require('./dependabot-automerge.cjs');

const PATCH = 'version-update:semver-patch';
const MINOR = 'version-update:semver-minor';
const MAJOR = 'version-update:semver-major';
const HEAD = 'a'.repeat(40);
const BASE = 'b'.repeat(40);
const CHANGED = 'c'.repeat(40);
const BOT = { login: 'dependabot[bot]', id: 49699333, type: 'Bot' };
const REPO = { full_name: 'ihuzaifashoukat/x-use', id: 1234 };
const clone = value => structuredClone(value);
function trailer(entries) {
  return `Bump dependencies\n\n---\nupdated-dependencies:\n${entries.map(item =>
    `- dependency-name: ${item.dependencyName}\n  dependency-type: direct:production\n  update-type: ${item.updateType}\n  dependency-group: patch-and-minor`
  ).join('\n')}\n...\n\nSigned-off-by: dependabot[bot] <support@github.com>\n`;
}
function fixture(ecosystem = 'uv', types = [PATCH]) {
  const pr = {
    number: 12, node_id: 'PR_test', user: clone(BOT), state: 'open', merged: false,
    draft: false, commits: 1, changed_files: 1, title: 'Bump dependencies', body: 'Bot update', auto_merge: null,
    base: { repo: clone(REPO), ref: 'main', sha: BASE },
    head: { repo: clone(REPO), ref: `dependabot/${ecosystem}/patch-and-minor`, sha: HEAD },
  };
  const context = {
    repo: { owner: 'ihuzaifashoukat', repo: 'x-use' },
    payload: { repository: clone(REPO), pull_request: clone(pr) },
  };
  const metadata = types.map((type, i) => ({
    dependencyName: `dependency-${i}`, updateType: type, packageEcosystem: ecosystem,
    directory: '/', targetBranch: 'main', maintainerChanges: false,
  }));
  const commits = [{
    sha: HEAD, author: clone(BOT), committer: { login: 'web-flow', id: 19864447, type: 'User' },
    commit: { message: trailer(metadata), verification: { verified: true, reason: 'valid' } },
  }];
  const files = [{ filename: ecosystem === 'uv' ? 'uv.lock' : ecosystem === 'docker' ? 'Dockerfile' : '.github/workflows/ci.yml', status: 'modified' }];
  const env = {
    EXPECTED_HEAD: HEAD, EXPECTED_BASE: BASE, METADATA_OUTCOME: 'success',
    DEPENDABOT_METADATA: JSON.stringify(metadata),
    DEPENDABOT_UPDATE_TYPE: types.includes(MINOR) ? MINOR : PATCH,
  };
  return { pr, context, metadata, commits, files, env };
}
function evaluate(f) {
  const ecosystem = policy.checkSnapshot(f.pr, f.context, { head: f.env.EXPECTED_HEAD, base: f.env.EXPECTED_BASE });
  const message = policy.checkCommit(f.pr, f.commits);
  policy.checkMetadata(f.env.DEPENDABOT_METADATA, f.env.DEPENDABOT_UPDATE_TYPE, ecosystem, message);
  policy.checkFiles(f.pr, f.files, ecosystem);
}
function syncMetadata(f) { f.env.DEPENDABOT_METADATA = JSON.stringify(f.metadata); }
function dockerTransition(previous = '3.12-slim-bookworm', next = '3.14-slim-bookworm', type = MINOR) {
  const f = fixture('docker', [type]);
  Object.assign(f.metadata[0], { dependencyName: 'python', prevVersion: previous, newVersion: next });
  syncMetadata(f);
  f.commits[0].commit.message = `Bump container dependency\n\nUpdates \`python\` from ${previous} to ${next}\n\n---\nupdated-dependencies:\n- dependency-name: python\n  dependency-version: ${next}\n  dependency-type: direct:production\n  dependency-group: docker-patch-and-minor\n...\n`;
  return f;
}
function harness(f = fixture(), options = {}) {
  const calls = [];
  const outputs = {};
  let reads = 0;
  const github = {
    rest: { pulls: {
      get: async args => {
        calls.push({ operation: 'get', args });
        reads++;
        if (options.onRead) await options.onRead(reads, f);
        return { data: clone(f.pr) };
      },
      listCommits: function listCommits() {},
      listFiles: function listFiles() {},
    } },
    paginate: async (method, args) => {
      calls.push({ operation: method.name, args });
      if (options.onPaginate) await options.onPaginate(method.name, f);
      return clone(method.name === 'listCommits' ? f.commits : f.files);
    },
    graphql: async (query, variables) => {
      const operation = query.includes('disablePullRequestAutoMerge') ? 'disable' : 'enable';
      calls.push({ operation, query, variables });
      if (options.onMutation) await options.onMutation(operation, f);
      f.pr.auto_merge = operation === 'disable' ? null : { enabled_by: { login: 'github-actions[bot]' } };
      return {};
    },
  };
  const core = { setOutput: (name, value) => { outputs[name] = value; }, info: () => {} };
  return { github, core, context: f.context, env: f.env, calls, outputs, f };
}

for (const ecosystem of ['uv', 'github_actions', 'docker']) {
  for (const types of [[PATCH], [MINOR], [PATCH, MINOR]]) {
    test(`accepts signed ${ecosystem} ${types.join(',')}`, () => evaluate(fixture(ecosystem, types)));
  }
}

const rejected = [
  ['wrong PR author login', f => { f.pr.user.login = 'maintainer'; }],
  ['spoofed bot numeric ID', f => { f.pr.user.id = 99; }],
  ['spoofed bot type', f => { f.pr.user.type = 'User'; }],
  ['different repository', f => { f.context.repo.owner = 'another'; }],
  ['wrong base repository ID', f => { f.pr.base.repo.id++; }],
  ['wrong PR number', f => { f.pr.number++; }],
  ['fork', f => { f.pr.head.repo.id++; }],
  ['deleted head repository', f => { f.pr.head.repo = null; }],
  ['wrong base branch', f => { f.pr.base.ref = 'release'; }],
  ['draft', f => { f.pr.draft = true; }],
  ['closed PR', f => { f.pr.state = 'closed'; }],
  ['merged PR', f => { f.pr.merged = true; }],
  ['unknown head SHA', f => { f.pr.head.sha = ''; }],
  ['head changed during run', f => { f.pr.head.sha = CHANGED; }],
  ['base changed during run', f => { f.pr.base.sha = CHANGED; }],
  ['title changed during run', f => { f.pr.title = 'Update requirement from 1.0 to 1.1'; }],
  ['body changed during run', f => { f.pr.body = 'Maintainer changes'; }],
  ['branch changed during run', f => { f.pr.head.ref += '-new'; }],
  ['unsupported ecosystem', f => { f.pr.head.ref = f.context.payload.pull_request.head.ref = 'dependabot/npm_and_yarn/group'; }],
  ['non-bot branch', f => { f.pr.head.ref = f.context.payload.pull_request.head.ref = 'feature/uv/group'; }],
  ['two commits', f => { f.pr.commits = 2; }],
  ['extra returned commit', f => { f.commits.push(clone(f.commits[0])); }],
  ['no returned commit', f => { f.commits = []; }],
  ['commit differs from current head', f => { f.commits[0].sha = CHANGED; }],
  ['human commit author', f => { f.commits[0].author.login = 'maintainer'; }],
  ['human commit committer', f => { f.commits[0].committer = { login: 'maintainer', id: 42, type: 'User' }; }],
  ['spoofed web-flow committer ID', f => { f.commits[0].committer.id = 42; }],
  ['unverified commit', f => { f.commits[0].commit.verification.verified = false; }],
  ['invalid commit verification', f => { f.commits[0].commit.verification.reason = 'bad_email'; }],
  ['malformed JSON', f => { f.env.DEPENDABOT_METADATA = '{'; }],
  ['empty metadata', f => { f.env.DEPENDABOT_METADATA = '[]'; }],
  ['oversized metadata', f => { f.env.DEPENDABOT_METADATA = 'x'.repeat(524289); }],
  ['non-array metadata', f => { f.env.DEPENDABOT_METADATA = '{}'; }],
  ['major update', f => { f.metadata[0].updateType = MAJOR; syncMetadata(f); }],
  ['unknown update', f => { f.metadata[0].updateType = ''; syncMetadata(f); }],
  ['mixed metadata ecosystem', f => { f.metadata[0].packageEcosystem = 'docker'; syncMetadata(f); }],
  ['nonroot directory', f => { f.metadata[0].directory = '/subdir'; syncMetadata(f); }],
  ['wrong metadata target', f => { f.metadata[0].targetBranch = 'release'; syncMetadata(f); }],
  ['maintainer change', f => { f.metadata[0].maintainerChanges = true; syncMetadata(f); }],
  ['missing maintainer field', f => { delete f.metadata[0].maintainerChanges; syncMetadata(f); }],
  ['metadata transplanted from another dependency', f => { f.metadata[0].dependencyName = 'other'; syncMetadata(f); }],
  ['aggregate contradicts dependencies', f => { f.env.DEPENDABOT_UPDATE_TYPE = MINOR; }],
  ['signature trailer missing', f => { f.commits[0].commit.message = 'Bump package'; }],
  ['ambiguous extra trailer', f => { f.commits[0].commit.message += f.commits[0].commit.message; }],
  ['duplicate YAML keys', f => { f.commits[0].commit.message = f.commits[0].commit.message.replace('  dependency-type:', '  update-type: version-update:semver-major\n  dependency-type:'); }],
  ['inferred rather than signed update type', f => { f.commits[0].commit.message = f.commits[0].commit.message.replace(/^  update-type:.*\n/m, ''); }],
  ['signed major masked by metadata patch', f => { f.commits[0].commit.message = f.commits[0].commit.message.replace(PATCH, MAJOR); }],
  ['YAML alias or anchor', f => { f.commits[0].commit.message = f.commits[0].commit.message.replace('dependency-0', '&alias dependency-0'); }],
  ['unknown YAML field', f => { f.commits[0].commit.message = f.commits[0].commit.message.replace('  dependency-type:', '  unexpected: value\n  dependency-type:'); }],
  ['incomplete file list', f => { f.pr.changed_files++; }],
  ['excessive file list', f => { f.pr.changed_files = 101; }],
  ['empty file list', f => { f.pr.changed_files = 0; f.files = []; }],
  ['new file', f => { f.files[0].status = 'added'; }],
  ['deleted file', f => { f.files[0].status = 'removed'; }],
  ['renamed file', f => { f.files[0].status = 'renamed'; }],
  ['previous filename', f => { f.files[0].previous_filename = 'old'; }],
  ['unrelated code file', f => { f.files[0].filename = 'src/xuse/mcp/server.py'; }],
  ['path traversal', f => { f.files[0].filename = '../uv.lock'; }],
  ['duplicate files', f => { f.files.push(clone(f.files[0])); f.pr.changed_files = 2; }],
];
for (const [name, mutate] of rejected) {
  test(`rejects ${name}`, () => {
    const f = fixture(); mutate(f);
    assert.throws(() => evaluate(f));
  });
}

for (const badType of [MAJOR, '', 'unknown']) {
  test(`rejects second grouped dependency ${badType || 'missing'} despite aggregate minor`, () => {
    const f = fixture('uv', [MINOR, PATCH]);
    f.metadata[1].updateType = badType;
    f.commits[0].commit.message = trailer(f.metadata);
    syncMetadata(f);
    assert.throws(() => evaluate(f));
  });
}
test('supports quoted simple signed scalars and bot committer', () => {
  const f = fixture();
  f.commits[0].committer = clone(BOT);
  f.commits[0].commit.message = f.commits[0].commit.message.replace('dependency-0', '"dependency-0"');
  evaluate(f);
});
test('supports grouped action metadata with versions and dependency-group per record', () => {
  const f = fixture('github_actions', [MINOR, MINOR]);
  f.metadata[0].dependencyName = 'docker/login-action';
  f.metadata[1].dependencyName = 'docker/build-push-action';
  syncMetadata(f);
  f.commits[0].commit.message = trailer(f.metadata)
    .replace('  dependency-type:', '  dependency-version: 4.4.0\n  dependency-type:')
    .replace('- dependency-name: docker/build-push-action\n', '- dependency-name: docker/build-push-action\n  dependency-version: 7.3.0\n');
  evaluate(f);
});
test('accepts signed Docker minor transition when Dependabot omits update-type', () => {
  evaluate(dockerTransition());
});
test('accepts signed Docker patch transition with unchanged variant', () => {
  evaluate(dockerTransition('3.12.10-slim-bookworm', '3.12.11-slim-bookworm', PATCH));
});
test('accepts signed Docker numeric tags with no platform suffix', () => {
  evaluate(dockerTransition('3.12.1', '3.14.0', MINOR));
});
for (const [name, previous, next] of [
  ['major', '3.12-slim-bookworm', '4.0-slim-bookworm'],
  ['minor downgrade', '3.14-slim-bookworm', '3.12-slim-bookworm'],
  ['patch downgrade', '3.12.11-slim-bookworm', '3.12.10-slim-bookworm'],
  ['no change', '3.14-slim-bookworm', '3.14-slim-bookworm'],
  ['variant change', '3.12-slim-bookworm', '3.14-slim-trixie'],
  ['precision change', '3.12-slim-bookworm', '3.14.0-slim-bookworm'],
  ['prefix change', '3.12-slim-bookworm', 'v3.14-slim-bookworm'],
  ['prerelease', '3.12-rc1', '3.14-rc1'],
  ['development tag', '3.12-slim-dev', '3.14-slim-dev'],
  ['unrecognized prerelease suffix', '3.12-experimental', '3.14-experimental'],
  ['unrecognized stable variant', '3.12-alpine3.22', '3.14-alpine3.22'],
  ['unknown version', 'latest', '3.14-slim-bookworm'],
  ['leading zero', '3.012-slim-bookworm', '3.14-slim-bookworm'],
  ['digest', 'sha256:abcdef', 'sha256:123456'],
]) {
  test(`rejects signed Docker ${name} when type is missing`, () => {
    assert.throws(() => evaluate(dockerTransition(previous, next)));
  });
}
for (const [name, change] of [
  ['missing transition', f => { f.commits[0].commit.message = f.commits[0].commit.message.replace(/^Updates.*\n/m, ''); }],
  ['duplicate transition', f => { f.commits[0].commit.message += 'Updates `python` from 3.12-slim-bookworm to 3.14-slim-bookworm\n'; }],
  ['different dependency', f => { f.commits[0].commit.message = f.commits[0].commit.message.replace('Updates `python`', 'Updates `other`'); }],
  ['unsigned previous version', f => { f.metadata[0].prevVersion = '3.13-slim-bookworm'; syncMetadata(f); }],
  ['unsigned new version', f => { f.metadata[0].newVersion = '3.15-slim-bookworm'; syncMetadata(f); }],
  ['mismatched YAML new version', f => { f.commits[0].commit.message = f.commits[0].commit.message.replace('dependency-version: 3.14', 'dependency-version: 3.15'); }],
  ['missing YAML version', f => { f.commits[0].commit.message = f.commits[0].commit.message.replace(/^  dependency-version:.*\n/m, ''); }],
  ['wrong computed type', f => { f.metadata[0].updateType = PATCH; f.env.DEPENDABOT_UPDATE_TYPE = PATCH; syncMetadata(f); }],
  ['explicit major cannot be overridden', f => { f.commits[0].commit.message = f.commits[0].commit.message.replace('  dependency-type:', `  update-type: ${MAJOR}\n  dependency-type:`); }],
]) {
  test(`rejects Docker ${name}`, () => {
    const f = dockerTransition(); change(f); assert.throws(() => evaluate(f));
  });
}
test('does not extend missing-type fallback to other ecosystems or mutable PR text', () => {
  const f = dockerTransition();
  f.pr.head.ref = f.context.payload.pull_request.head.ref = 'dependabot/uv/group';
  f.metadata[0].packageEcosystem = 'uv'; syncMetadata(f);
  f.files[0].filename = 'uv.lock';
  assert.throws(() => evaluate(f));
  const title = dockerTransition();
  title.commits[0].commit.message = title.commits[0].commit.message.replace(/^Updates.*\n/m, '');
  title.pr.title = title.context.payload.pull_request.title = 'Updates `python` from 3.12-slim-bookworm to 3.14-slim-bookworm';
  assert.throws(() => evaluate(title));
});
test('rejects repeated same-name Docker dependencies with omitted update types', () => {
  const f = dockerTransition();
  f.metadata.push(clone(f.metadata[0])); syncMetadata(f);
  const entry = f.commits[0].commit.message.match(/(- dependency-name:[\s\S]*?)\n\.\.\./)[1];
  f.commits[0].commit.message = f.commits[0].commit.message.replace('\n...\n', `\n${entry}\n...\n`);
  assert.throws(() => evaluate(f), /Repeated Docker dependency/);
});
test('checks every omitted Docker type in a grouped update', () => {
  const f = dockerTransition();
  f.metadata.push({ ...f.metadata[0], dependencyName: 'runtime', prevVersion: '1.2.1', newVersion: '1.2.2', updateType: PATCH });
  syncMetadata(f);
  f.commits[0].commit.message = f.commits[0].commit.message
    .replace('\n---\n', '\nUpdates `runtime` from 1.2.1 to 1.2.2\n\n---\n')
    .replace('\n...\n', '\n- dependency-name: runtime\n  dependency-version: 1.2.2\n  dependency-type: direct:production\n  dependency-group: docker-patch-and-minor\n...\n');
  evaluate(f);
  f.metadata[1].newVersion = '2.0.0'; syncMetadata(f);
  f.commits[0].commit.message = f.commits[0].commit.message.replaceAll('1.2.2', '2.0.0');
  assert.throws(() => evaluate(f), /Docker major updates/);
});
test('signed Docker fallback uses the same protected native auto-merge path', async () => {
  const h = harness(dockerTransition());
  assert.equal((await policy.apply(h)).enabled, true);
  const enabled = h.calls.find(call => call.operation === 'enable');
  assert.deepEqual(enabled.variables, { id: 'PR_test', head: HEAD });
  assert.match(enabled.query, /expectedHeadOid: \$head/);
});
test('rejects nested workflow directories and file-type confusion', () => {
  for (const filename of ['.github/workflows/nested/test.yml', '.github/workflows/ci.yml.js', 'Dockerfile', '.github/scripts/policy.cjs']) {
    const f = fixture('github_actions'); f.files[0].filename = filename;
    assert.throws(() => evaluate(f));
  }
});
test('prepare revokes old permission before validating stale or draft PR', async () => {
  const f = fixture(); f.pr.auto_merge = {}; f.pr.draft = true;
  const h = harness(f);
  await policy.prepare(h);
  assert.deepEqual(h.calls.map(c => c.operation), ['get', 'disable']);
  assert.equal(h.outputs.eligible, 'false');
});
test('prepare binds eligible metadata step to current head and base', async () => {
  const h = harness(); await policy.prepare(h);
  assert.deepEqual(h.outputs, { eligible: 'true', head: HEAD, base: BASE });
});
test('cannot mutate unrelated human-authored PRs', async () => {
  const f = fixture(); f.pr.user.login = 'maintainer'; f.pr.auto_merge = {};
  const h = harness(f); await policy.prepare(h); await policy.apply(h);
  assert.equal(h.calls.filter(c => ['enable', 'disable'].includes(c.operation)).length, 0);
});
test('native enable binds the exact head and delegates pending checks to GitHub', async () => {
  const h = harness(); const result = await policy.apply(h);
  assert.equal(result.enabled, true);
  const enabled = h.calls.find(c => c.operation === 'enable');
  assert.deepEqual(enabled.variables, { id: 'PR_test', head: HEAD });
  assert.match(enabled.query, /expectedHeadOid: \$head/);
  assert.match(enabled.query, /mergeMethod: MERGE/);
  assert.doesNotMatch(enabled.query, /\bmergePullRequest\(/);
  assert.equal(h.calls.filter(c => c.operation === 'get').length, 3);
});
for (const outcome of ['failure', 'skipped', 'cancelled', '']) {
  test(`metadata ${outcome || 'absent'} revokes old auto-merge`, async () => {
    const f = fixture(); f.pr.auto_merge = {}; f.env.METADATA_OUTCOME = outcome;
    const h = harness(f); const result = await policy.apply(h);
    assert.equal(result.enabled, false);
    assert.equal(h.calls.filter(c => c.operation === 'disable').length, 1);
    assert.equal(h.calls.filter(c => c.operation === 'enable').length, 0);
  });
}
test('major update revokes old auto-merge', async () => {
  const f = fixture(); f.pr.auto_merge = {}; f.metadata[0].updateType = MAJOR; syncMetadata(f);
  const h = harness(f); assert.equal((await policy.apply(h)).enabled, false);
  assert.equal(f.pr.auto_merge, null);
});
test('fresh read detects head race while files are loading', async () => {
  const h = harness(fixture(), { onPaginate: (_, f) => { f.pr.head.sha = CHANGED; } });
  assert.equal((await policy.apply(h)).enabled, false);
  assert.equal(h.calls.some(c => c.operation === 'enable'), false);
});
test('fresh read detects base race while files are loading', async () => {
  const h = harness(fixture(), { onPaginate: (_, f) => { f.pr.base.sha = CHANGED; } });
  assert.equal((await policy.apply(h)).enabled, false);
  assert.equal(h.calls.some(c => c.operation === 'enable'), false);
});
test('expected-head mutation failure has no direct merge fallback', async () => {
  const h = harness(fixture(), { onMutation: operation => { if (operation === 'enable') throw new Error('Head changed'); } });
  await assert.rejects(policy.apply(h), /Head changed/);
  assert.equal(h.calls.filter(c => c.operation === 'enable').length, 1);
});
for (const change of ['head', 'base', 'draft']) {
  test(`post-enable ${change} race revokes native auto-merge`, async () => {
    const h = harness(fixture(), { onMutation: (operation, f) => {
      if (operation === 'enable') {
        if (change === 'draft') f.pr.draft = true;
        else f.pr[change].sha = CHANGED;
      }
    } });
    assert.equal((await policy.apply(h)).enabled, false);
    assert.deepEqual(h.calls.filter(c => ['enable', 'disable'].includes(c.operation)).map(c => c.operation), ['enable', 'disable']);
  });
}
test('API error propagates and old permission is not retained', async () => {
  const f = fixture(); f.pr.auto_merge = {};
  const h = harness(f, { onPaginate: () => { throw new Error('API unavailable'); } });
  await assert.rejects(policy.apply(h), /API unavailable/);
  assert.equal(f.pr.auto_merge, null);
});
test('failed post-enable API read revokes using already verified immutable PR identity', async () => {
  const h = harness(fixture(), { onRead: reads => {
    if (reads >= 3) throw new Error('REST unavailable after enable');
  } });
  await assert.rejects(policy.apply(h), /REST unavailable after enable/);
  assert.deepEqual(h.calls.filter(c => ['enable', 'disable'].includes(c.operation)).map(c => c.operation), ['enable', 'disable']);
  assert.equal(h.f.pr.auto_merge, null);
});
test('lost enable response also attempts native revocation without a further PR read', async () => {
  const h = harness(fixture(), { onMutation: (operation, f) => {
    if (operation === 'enable') {
      f.pr.auto_merge = {};
      throw new Error('Enable response lost');
    }
  } });
  await assert.rejects(policy.apply(h), /Enable response lost/);
  assert.equal(h.f.pr.auto_merge, null);
  assert.equal(h.calls.filter(c => c.operation === 'get').length, 2);
});
test('failed revocation fails the job before any enablement', async () => {
  const f = fixture(); f.pr.auto_merge = {};
  const h = harness(f, { onMutation: () => { throw new Error('Cannot revoke'); } });
  await assert.rejects(policy.apply(h), /Cannot revoke/);
  assert.equal(h.calls.some(c => c.operation === 'enable'), false);
});
test('workflow uses trusted main and immutable actions without evaluating PR content', () => {
  const workflow = fs.readFileSync(path.join(__dirname, '../workflows/dependabot-automerge.yml'), 'utf8').replaceAll('\r\n', '\n');
  assert.match(workflow, /pull_request_target:/);
  assert.match(workflow, /types: \[opened, reopened, synchronize, edited, ready_for_review, converted_to_draft, auto_merge_enabled\]/);
  assert.doesNotMatch(workflow, /branches:/);
  assert(workflow.includes(`repository: ${REPO.full_name}\n`));
  assert.match(workflow, /ref: main/);
  assert.match(workflow, /persist-credentials: false/);
  assert.match(workflow, /cancel-in-progress: false/);
  assert.match(workflow, /permissions: \{\}/);
  const actions = [...workflow.matchAll(/uses: ([^\s]+)/g)].map(m => m[1]);
  assert.equal(actions.length, 4);
  assert(actions.every(action => /^[a-z/-]+@[a-f0-9]{40}$/.test(action)));
  for (const script of workflow.matchAll(/script: \|\n((?:            .*\n)+)/g)) {
    assert.doesNotMatch(script[1], /\$\{\{/);
  }
  assert.doesNotMatch(workflow, /secrets\.(?!GITHUB_TOKEN)[A-Z_]+/);
  assert.doesNotMatch(workflow, /skip-(?:commit-)?verification: true/);
  assert.doesNotMatch(workflow, /pull_request\.head\.sha|ref: \$\{\{/);
});
