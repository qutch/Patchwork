// The extension finds Patchwork by itself (extension/discover.js). Node's built-in runner, no packages:
//   node --test tests/extension/discover.test.js
// (tests/extension/test_discover_node.py runs it from pytest). No real port is probed: every probe is a fake.
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const { PW_PORTS, pwDiscover, pwIsAuto, pwConnection } = require(path.join(__dirname, '..', '..', 'extension', 'discover.js'));

const sleep = ms => new Promise(r => setTimeout(r, ms));

// A fake probe: answers[port] = [delayMs, result] where result is a setup, null, or 'throw'.
function fakeProbe(answers) {
  const calls = [];
  const probe = async port => {
    calls.push(port);
    const [delay, result] = answers[port] || [0, null];
    await sleep(delay);
    if (result === 'throw') throw new Error('connection refused');
    return result;
  };
  return { probe, calls };
}

test('the ports are the ones ./patchwork uses: 38091..38110, never the reserved ones', () => {
  assert.equal(PW_PORTS[0], 38091);
  assert.equal(PW_PORTS[PW_PORTS.length - 1], 38110);
  assert.equal(PW_PORTS.length, 20);
  for (const p of [8000, 8001, 8002, 8003, 5180]) assert.ok(!PW_PORTS.includes(p));
});

test('takes the first port in order that answers with a token, even if a later one answers sooner', async () => {
  const { probe } = fakeProbe({ 1: [0, 'throw'], 2: [40, { token: 'b' }], 3: [0, { token: 'c' }] });
  const found = await pwDiscover(probe, [1, 2, 3]);
  assert.deepEqual(found, { port: 2, setup: { token: 'b' } });
});

test('probes every port at once (in parallel), not one after another', async () => {
  const { probe, calls } = fakeProbe({ 1: [50, null], 2: [50, null], 3: [50, { token: 't' }] });
  const started = Date.now();
  const found = await pwDiscover(probe, [1, 2, 3]);
  assert.equal(found.port, 3);
  assert.deepEqual(calls, [1, 2, 3]);
  assert.ok(Date.now() - started < 140, 'three 50 ms probes took as long as one');
});

test('skips refusals, errors, and answers without a token', async () => {
  const { probe } = fakeProbe({ 1: [0, 'throw'], 2: [0, null], 3: [0, { token: '' }], 4: [0, { status: 'ok' }],
                                5: [0, { token: 'yes', integrations: [] }] });
  assert.deepEqual(await pwDiscover(probe, [1, 2, 3, 4, 5]), { port: 5, setup: { token: 'yes', integrations: [] } });
  assert.equal(await pwDiscover(probe, [1, 2, 3, 4]), null);
  assert.equal(await pwDiscover(probe, []), null);
  const sync = port => { if (port === 1) throw new Error('sync'); return { token: 'z' }; };
  assert.deepEqual(await pwDiscover(sync, [1, 2]), { port: 2, setup: { token: 'z' } });
});

test('auto is the default; any other address is manual', () => {
  for (const v of [undefined, null, '', '  ', 'auto']) assert.ok(pwIsAuto(v), String(v));
  assert.ok(!pwIsAuto('http://127.0.0.1:38091'));
});

// ---- pwConnection over a fake chrome.storage.local ------------------------------------------------------

function fakeChrome(initial) {
  const data = { ...initial };
  global.chrome = {
    storage: {
      local: {
        async get(defaults) { return { ...defaults, ...data }; },
        async set(o) { Object.assign(data, o); },
        async remove(k) { for (const key of [].concat(k)) delete data[key]; },
      },
    },
  };
  return data;
}

test('a manual address wins and is never replaced by discovery', async () => {
  const data = fakeChrome({ controlUrl: 'http://127.0.0.1:9999/', token: 'mine' });
  const { probe, calls } = fakeProbe({ 38091: [0, { token: 'found' }] });
  const conn = await pwConnection({ rediscover: true, probe });
  assert.deepEqual(conn, { mode: 'manual', url: 'http://127.0.0.1:9999', token: 'mine' });
  assert.deepEqual(calls, []);
  assert.equal(data.token, 'mine');
});

test('auto: discovers, caches the address and token, then uses the cache until told to look again', async () => {
  const data = fakeChrome({ discoverPorts: [47101, 47102] });
  let answers = { 47102: [0, { token: 't1', integrations: [] }] };
  const probe = async port => { const [, r] = answers[port] || [0, null]; return r; };
  const conn = await pwConnection({ probe });
  assert.equal(conn.mode, 'auto');
  assert.equal(conn.url, 'http://127.0.0.1:47102');
  assert.equal(conn.token, 't1');
  assert.equal(data.autoUrl, 'http://127.0.0.1:47102');
  assert.equal(data.token, 't1');

  answers = { 47101: [0, { token: 't2' }] };  // Patchwork restarted elsewhere: the cache still says 47102
  assert.equal((await pwConnection({ probe })).url, 'http://127.0.0.1:47102');
  const again = await pwConnection({ rediscover: true, probe });
  assert.equal(again.url, 'http://127.0.0.1:47101');
  assert.equal(data.token, 't2');

  answers = {};  // not running: no address, the stale one is forgotten
  const none = await pwConnection({ rediscover: true, probe });
  assert.deepEqual(none, { mode: 'auto', url: null, token: '' });
  assert.equal(data.autoUrl, undefined);
});

test('auto without discoverPorts probes 38091..38110', async () => {
  fakeChrome({});
  const { probe, calls } = fakeProbe({});
  await pwConnection({ probe });
  assert.deepEqual(calls, PW_PORTS);
});
