import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import test from 'node:test';

const source = await readFile(new URL('../relay/worker.js', import.meta.url), 'utf8');
const worker = (await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)).default;

test('ingestion and queue access require separate credentials', async () => {
  const values = new Map();
  const env = { RELAY_TOKEN: 'read-secret', INGEST_TOKEN: 'write-secret', RELAY: {
    async put(k, v) { values.set(k, v); },
    async get(k) { return values.get(k); },
    async list() { return { keys: [...values.keys()].sort().map(name => ({ name })) }; },
  } };
  const post = token => new Request('https://relay.example/ingest/demo', {
    method: 'POST', headers: token ? { authorization: `Bearer ${token}` } : {}, body: '{"title":"hello"}',
  });
  assert.equal((await worker.fetch(post(), env)).status, 401);
  assert.equal((await worker.fetch(post('wrong'), env)).status, 401);
  assert.equal((await worker.fetch(post('read-secret'), env)).status, 401);
  assert.equal(values.size, 0);
  assert.equal((await worker.fetch(post('write-secret'), { ...env, INGEST_TOKEN: undefined })).status, 503);
  assert.equal((await worker.fetch(post('write-secret'), env)).status, 200);
  assert.equal(values.size, 1);
  const poll = token => new Request('https://relay.example/events', { headers: { authorization: `Bearer ${token}` } });
  assert.equal((await worker.fetch(poll('write-secret'), env)).status, 401);
  const response = await worker.fetch(poll('read-secret'), env);
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.events[0].route, 'demo');
  assert.equal(data.events[0].body, '{"title":"hello"}');
  assert.equal((await (await worker.fetch(new Request(`https://relay.example/events?since=${data.since}`, {
    headers: { authorization: 'Bearer read-secret' },
  }), env)).json()).events.length, 0);
});

for (const value of [[1, 2], 42, 'hello', null]) {
  test(`normalizes JSON ${JSON.stringify(value)} before storage`, async () => {
    let saved;
    const env = { INGEST_TOKEN: 'write', RELAY: { async put(k, v) { saved = JSON.parse(v); } } };
    const response = await worker.fetch(new Request('https://relay.example/ingest/demo', {
      method: 'POST', headers: { authorization: 'Bearer write' }, body: JSON.stringify(value),
    }), env);
    assert.equal(response.status, 200);
    assert.deepEqual(JSON.parse(saved.body), { value });
  });
}
