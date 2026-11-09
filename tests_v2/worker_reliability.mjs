import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { test, mock } from 'node:test';

const source = await readFile(new URL('../cloudflare/worker.js', import.meta.url), 'utf8');
const worker = await import(`data:text/javascript;base64,${Buffer.from(
  source + '\nexport { pollOnce, notifyPrComments };').toString('base64')}`);
const NOW = '2026-11-06T04:00:00.000Z';
const OLD = '2026-11-06T03:00:00.000Z';
const RealDate = Date;

function setup(values = {}) {
  mock.method(globalThis, 'Date', class extends RealDate {
    constructor(...args) { super(...(args.length ? args : [NOW])); }
    static now() { return new RealDate(NOW).getTime(); }
  });
  const stored = new Map(Object.entries(values));
  const puts = [];
  return {
    REPOS: 'a/b', GH_TOKEN: 'fake', INGEST_URL: 'https://dashboard/api/ingest/issues',
    INGEST_TOKEN: 'fake', MY_LOGIN: 'me', stored, puts,
    SEEN: {
      get: async key => stored.get(key) || null,
      put: async (key, value) => { puts.push(key); stored.set(key, value); },
    },
  };
}

function response(value, status = 200, headers = {}) {
  return new Response(JSON.stringify(value), { status, headers });
}
function issue(number) {
  return { number, title: 'Bug', body: 'Reproduce bug', labels: [], comments: 0 };
}
function gqlPage(nodes = [], cursor = null) {
  return { issues: { nodes: nodes.map(it => ({ ...it, labels: { nodes: [] },
    assignees: { nodes: [] }, comments: { totalCount: 0 }, reactions: { totalCount: 0 } })),
  pageInfo: { hasNextPage: Boolean(cursor), endCursor: cursor } } };
}

test('new repositories do not inherit a stale shared watermark', async t => {
  t.after(() => mock.restoreAll());
  const env = setup({ since: OLD, watermarks: JSON.stringify({ 'a/b': OLD }) });
  env.REPOS = 'a/b,c/d';
  mock.method(globalThis, 'fetch', async (_url, options) => {
    const query = JSON.parse(options.body).query;
    assert.match(query, /2026-11-06T02:59:00.000Z/);
    assert.match(query, /2026-11-06T03:44:00.000Z/);
    return response({ data: { r0: gqlPage(), r1: gqlPage() } });
  });
  await worker.pollOnce(env);
  assert.equal(JSON.parse(env.stored.get('diag')).ok_repos, 2);
});

test('changed issues follow pagination before advancing the repository watermark', async t => {
  t.after(() => mock.restoreAll());
  const env = setup({ watermarks: JSON.stringify({ 'a/b': OLD }) });
  const delivered = [];
  mock.method(globalThis, 'fetch', async (url, options) => {
    if (url === env.INGEST_URL) {
      delivered.push(...JSON.parse(options.body).issues);
      return response({});
    }
    const query = JSON.parse(options.body).query;
    return response({ data: { r0: query.includes('after:"next"')
      ? gqlPage([issue(2)]) : gqlPage([issue(1)], 'next') } });
  });
  await worker.pollOnce(env);
  assert.deepEqual(delivered.map(x => x.number), [1, 2]);
  assert.equal(JSON.parse(env.stored.get('watermarks'))['a/b'], NOW);
});

test('a failed repository retains its own watermark while successful repositories advance', async t => {
  t.after(() => mock.restoreAll());
  const env = setup({ watermarks: JSON.stringify({ 'a/b': OLD, 'c/d': OLD }) });
  env.REPOS = 'a/b,c/d';
  mock.method(globalThis, 'fetch', async () => response({ data: {
    r0: gqlPage(), r1: null }, errors: [{ path: ['r1'], message: 'unavailable' }] }));
  await worker.pollOnce(env);
  const marks = JSON.parse(env.stored.get('watermarks'));
  assert.equal(marks['a/b'], NOW);
  assert.equal(marks['c/d'], OLD);
});

test('no watermark advances when both direct delivery and durable fallback fail', async t => {
  t.after(() => mock.restoreAll());
  const env = setup({ watermarks: JSON.stringify({ 'a/b': OLD }), since: OLD });
  const put = env.SEEN.put;
  env.SEEN.put = async (key, value) => {
    if (key.startsWith('pending:')) throw new Error('KV quota');
    return put(key, value);
  };
  mock.method(globalThis, 'fetch', async url => url === env.INGEST_URL
    ? response({}, 503) : response({ data: { r0: gqlPage([issue(1)]) } }));
  await assert.rejects(worker.pollOnce(env), /KV quota/);
  assert.equal(env.stored.get('since'), OLD);
  assert.equal(JSON.parse(env.stored.get('watermarks'))['a/b'], OLD);
});

test('PR polling runs even when no eligible issue is found', async t => {
  t.after(() => mock.restoreAll());
  const env = setup({ prs: JSON.stringify(['a/b#9']) });
  let polled = false;
  mock.method(globalThis, 'fetch', async url => {
    if (String(url).includes('/issues/9/comments')) polled = true;
    return response(String(url).endsWith('/graphql') ? { data: { r0: gqlPage() } } : []);
  });
  await worker.pollOnce(env);
  assert.equal(polled, true);
});

test('fifty repositories use five batched GitHub requests', async t => {
  t.after(() => mock.restoreAll());
  const env = setup();
  env.REPOS = Array.from({ length: 50 }, (_, i) => `a/repo${i}`).join(',');
  let calls = 0;
  mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, 'https://api.github.com/graphql');
    calls++;
    const aliases = [...JSON.parse(options.body).query.matchAll(/(r\d+): repository/g)];
    return response({ data: Object.fromEntries(aliases.map(m => [m[1], gqlPage()])) });
  });
  await worker.pollOnce(env);
  assert.equal(calls, 5);
  assert.equal(Object.keys(JSON.parse(env.stored.get('watermarks'))).length, 50);
});

test('overlapping cron invocations share the same in-flight scan', async t => {
  t.after(() => mock.restoreAll());
  const env = setup();
  let calls = 0;
  mock.method(globalThis, 'fetch', async () => {
    calls++;
    return response({ data: { r0: gqlPage() } });
  });
  const waiting = [];
  const ctx = { waitUntil: promise => waiting.push(promise) };
  worker.default.scheduled({}, env, ctx);
  worker.default.scheduled({}, env, ctx);
  await Promise.all(waiting);
  assert.equal(calls, 1);
});

test('repeated fallback delivery writes one durable batch instead of spending KV quota again', async t => {
  t.after(() => mock.restoreAll());
  const env = setup();
  mock.method(globalThis, 'fetch', async url => url === env.INGEST_URL
    ? response({}, 503) : response({ data: { r0: gqlPage([issue(1)]) } }));
  await worker.pollOnce(env);
  await worker.pollOnce(env);
  assert.equal(env.puts.filter(key => key.startsWith('pending:')).length, 1);
});

test('PR comment HTTP rejection does not advance its delivery watermark', async t => {
  t.after(() => mock.restoreAll());
  const env = setup({ prs: JSON.stringify(['a/b#9']), pr_since: OLD });
  mock.method(globalThis, 'fetch', async url => String(url).includes('/api/ingest/')
    ? response({}, 503)
    : response([{ id: 3, user: { login: 'maintainer' }, body: 'Please fix' }]));
  await worker.notifyPrComments(env, new RealDate(NOW));
  assert.equal(env.stored.get('pr_since'), OLD);
  assert.equal(JSON.parse(env.stored.get('pr_watermarks'))['a/b#9'], OLD);
});

test('PR comments are paginated and failed PRs keep their own watermark', async t => {
  t.after(() => mock.restoreAll());
  const env = setup({ prs: JSON.stringify(['a/b#9', 'a/b#10']),
    pr_watermarks: JSON.stringify({ 'a/b#9': OLD, 'a/b#10': OLD }) });
  const delivered = [];
  mock.method(globalThis, 'fetch', async (url, options) => {
    if (String(url).includes('/api/ingest/')) {
      delivered.push(...JSON.parse(options.body).events);
      return response({});
    }
    if (String(url).includes('/issues/10/')) return response({}, 503);
    const c = id => ({ id, user: { login: 'maintainer' }, body: 'Fix it' });
    if (new URL(url).searchParams.get('page') === '2') return response([c(2)]);
    return response([c(1)], 200, {
      Link: '<https://api.github.com/repos/a/b/issues/9/comments?page=2>; rel="next"',
    });
  });
  await worker.notifyPrComments(env, new RealDate(NOW));
  assert.deepEqual(delivered.map(x => x.comment_id), [1, 2]);
  const marks = JSON.parse(env.stored.get('pr_watermarks'));
  assert.equal(marks['a/b#9'], NOW);
  assert.equal(marks['a/b#10'], OLD);
});
