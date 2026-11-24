import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { test, mock } from 'node:test';

const source = await readFile(new URL('../cloudflare/worker.js', import.meta.url), 'utf8');
const worker = await import(`data:text/javascript;base64,${Buffer.from(
  source + '\nexport { pollOnce };').toString('base64')}`);
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

test('57 repositories fit in six GitHub requests within the existing budget', async t => {
  t.after(() => mock.restoreAll());
  const env = setup();
  env.REPOS = Array.from({ length: 57 }, (_, i) => `a/repo${i}`).join(',');
  let calls = 0;
  mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, 'https://api.github.com/graphql');
    calls++;
    const aliases = [...JSON.parse(options.body).query.matchAll(/(r\d+): repository/g)];
    return response({ data: Object.fromEntries(aliases.map(m => [m[1], gqlPage()])) });
  });
  await worker.pollOnce(env);
  assert.equal(calls, 6);
  assert.equal(JSON.parse(env.stored.get('diag')).ok_repos, 57);
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


test('stale PR tracking KV never causes PR polling or callback delivery', async t => {
  t.after(() => mock.restoreAll());
  const env = setup({ prs: JSON.stringify(['a/b#1']), pr_since: OLD });
  const calls = [];
  mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push(String(url));
    assert.equal(url, 'https://api.github.com/graphql');
    return response({ data: { r0: gqlPage() } });
  });
  await worker.pollOnce(env);
  assert.deepEqual(calls, ['https://api.github.com/graphql']);
  assert.equal(env.stored.get('pr_since'), OLD);
});


test('warm cursor advances only after a durable delivery', async t => {
  t.after(() => mock.restoreAll());
  const env = setup({watermarks: JSON.stringify({'a/b': OLD})});
  const memory = {};
  mock.method(globalThis, 'fetch', async (url, options) => {
    if (url === env.INGEST_URL) return response({});
    return response({data: {r0: gqlPage([issue(1)])}});
  });
  await worker.pollOnce(env, memory);
  assert.equal(memory['a/b'], NOW);
});


test('edge rejects unauthenticated and unmonitored requests without GitHub calls', async t => {
  t.after(() => mock.restoreAll());
  const env = setup();
  const fn = mock.method(globalThis, 'fetch', async () => { throw new Error('must not fetch'); });
  const url = 'https://worker/internal/v1/issue-context';
  const denied = await worker.default.fetch(new Request(url, {method:'POST', body:'{}'}), env);
  assert.equal(denied.status, 403);
  const unmonitored = await worker.default.fetch(new Request(url, {method:'POST',headers:{'X-Ingest-Token':'fake'},body:JSON.stringify({repo:'other/repo',number:1})}), env);
  assert.equal(unmonitored.status, 400);
  assert.equal(fn.mock.callCount(), 0);
});

test('edge issue context requires complete comment and timeline pages', async t => {
  t.after(() => mock.restoreAll());
  const env = setup();
  mock.method(globalThis, 'fetch', async url => {
    if (url.includes('/comments?')) return response([{id:1,body:'comment'}]);
    if (url.includes('/timeline?')) return response([]);
    return response({number:1,state:'open',comments:1});
  });
  const make = () => new Request('https://worker/internal/v1/issue-context',{method:'POST',headers:{'X-Ingest-Token':'fake'},body:JSON.stringify({repo:'a/b',number:1})});
  const result = await worker.default.fetch(make(),env);
  assert.equal(result.status,200);
  assert.equal((await result.json()).comments.length,1);
  mock.restoreAll();
  mock.method(globalThis,'fetch',async url => url.includes('/comments?') ? response({},503) : url.includes('/timeline?') ? response([]) : response({number:1,state:'open',comments:1}));
  assert.equal((await worker.default.fetch(make(),env)).status,503);
});

test('edge PR snapshot validates author and complete count', async t => {
  t.after(() => mock.restoreAll());
  const env = setup();
  const node = {repository:{nameWithOwner:'a/b'},number:2,state:'OPEN',title:'Fix',url:'https://github.com/a/b/pull/2',createdAt:NOW,updatedAt:NOW,closedAt:null};
  mock.method(globalThis,'fetch',async (_url, options)=> {
    const query = JSON.parse(options.body);
    assert.match(query.query,/viewer/);
    return response({data:{viewer:{login:'xixi-box',pullRequests:{totalCount:1,nodes:[node],pageInfo:{hasNextPage:false}}}}});
  });
  const result = await worker.default.fetch(new Request('https://worker/internal/v1/pr-snapshot',{method:'POST',headers:{'X-Ingest-Token':'fake'},body:JSON.stringify({cutoff:null})}),env);
  assert.equal(result.status,200);
  assert.equal((await result.json()).nodes.length,1);
});


test('durable ingress stages versions, retains failed delivery and deduplicates acknowledged replay', async t => {
  t.after(() => mock.restoreAll());
  let DatabaseSync;
  try { ({ DatabaseSync } = await import('node:sqlite')); }
  catch { t.skip('node:sqlite unavailable'); return; }
  const db = new DatabaseSync(':memory:');
  t.after(() => db.close());
  const env = setup();
  let alarm = null;
  const storage = {
    sql: {exec(sql, ...params) {
      const statement = db.prepare(sql);
      if (/^SELECT/i.test(sql)) return {toArray:()=>statement.all(...params)};
      statement.run(...params); return {};
    }},
    transactionSync(fn) { db.exec('BEGIN'); try {fn(); db.exec('COMMIT');} catch(e){db.exec('ROLLBACK');throw e;} },
    async getAlarm(){return alarm;}, async setAlarm(value){alarm=value;}, async deleteAlarm(){alarm=null;},
  };
  const stage = object => new Request('https://internal/stage',{method:'POST',body:JSON.stringify({issues:[object]})});
  const item = {repo:'a/b',number:1,updated_at:NOW};
  let ingress = new worker.IssueIngress({storage},env);
  assert.equal((await ingress.fetch(stage(item))).status,200);
  assert.ok(alarm);
  assert.equal((await ingress.fetch(stage(item))).status,200);
  assert.equal(db.prepare('SELECT COUNT(*) AS n FROM outbox').get().n,1);
  mock.method(globalThis,'fetch',async ()=>response({},503));
  await assert.rejects(ingress.alarm(),/receipt unavailable/);
  assert.equal(db.prepare('SELECT COUNT(*) AS n FROM outbox WHERE sent IS NULL').get().n,1);
  assert.ok(alarm);
  mock.restoreAll();
  let sends=0;
  mock.method(globalThis,'fetch',async (_url,options)=>{
    sends++;
    assert.deepEqual(JSON.parse(options.body).issues,[item]);
    return response({stored:1});
  });
  ingress = new worker.IssueIngress({storage},env);
  await ingress.alarm();
  assert.equal(sends,1);
  assert.equal(db.prepare('SELECT COUNT(*) AS n FROM outbox WHERE sent IS NULL').get().n,0);
  await ingress.fetch(stage(item));
  await ingress.alarm();
  assert.equal(sends,1);
});
