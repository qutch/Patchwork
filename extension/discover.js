// Finding Patchwork by itself (one-command launcher design): `./patchwork` serves on 38091, else the next free
// port up to 38110. The extension probes those ports with GET /api/extension and takes the first, in port order,
// that answers with a token. The address and token are cached in chrome.storage.local (`autoUrl`, `token`).
// A manual address (options, "Advanced") always wins and is never replaced by discovery.
//
// Loaded by the service worker (importScripts), the options page and the popup (<script>), and by node's test
// runner (module.exports at the end). pwDiscover is pure: tests pass a fake probe.

const PW_PORTS = Array.from({ length: 20 }, (_, i) => 38091 + i);  // 38091..38110
const PW_PROBE_TIMEOUT_MS = 800;
const PW_AUTO = 'auto';

// probe(port) -> Promise of a setup ({token, ...}) or null; it may also reject. All ports are probed at once;
// the answer is the first port, in `ports` order, whose probe gave a token: {port, setup}, or null.
async function pwDiscover(probe, ports) {
  const pending = ports.map(port => Promise.resolve().then(() => probe(port)).catch(() => null));
  for (let i = 0; i < ports.length; i++) {
    const setup = await pending[i];
    if (setup && setup.token) return { port: ports[i], setup };
  }
  return null;
}

// GET http://127.0.0.1:{port}/api/extension with a short timeout: the setup if it is Patchwork's, else null.
async function pwProbe(port, timeoutMs = PW_PROBE_TIMEOUT_MS) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(`http://127.0.0.1:${port}/api/extension`, { signal: ctl.signal, cache: 'no-store' });
    if (!r.ok) return null;
    const body = await r.json();
    return body && typeof body.token === 'string' && body.token ? body : null;
  } catch (e) {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// '' or 'auto' = find Patchwork by itself; anything else is a manual address.
function pwIsAuto(controlUrl) {
  const v = String(controlUrl || '').trim();
  return v === '' || v === PW_AUTO;
}

// Where to send pages: {mode: 'auto'|'manual', url, token}. url is null when no Patchwork was found.
// In auto mode the cached address is used unless `rediscover` (after a network failure or a 401).
// `discoverPorts` in storage replaces PW_PORTS (tests only: they must never probe the owner's ports).
async function pwConnection({ rediscover = false, probe = pwProbe } = {}) {
  const o = await chrome.storage.local.get({ controlUrl: PW_AUTO, token: '', autoUrl: '', discoverPorts: null });
  if (!pwIsAuto(o.controlUrl)) {
    return { mode: 'manual', url: String(o.controlUrl).trim().replace(/\/+$/, ''), token: String(o.token || '').trim() };
  }
  if (!rediscover && o.autoUrl && o.token) return { mode: 'auto', url: o.autoUrl, token: o.token };
  const ports = Array.isArray(o.discoverPorts) && o.discoverPorts.length ? o.discoverPorts : PW_PORTS;
  const found = await pwDiscover(probe, ports);
  if (!found) {
    await chrome.storage.local.remove('autoUrl');
    return { mode: 'auto', url: null, token: '' };
  }
  const url = `http://127.0.0.1:${found.port}`;
  await chrome.storage.local.set({ autoUrl: url, token: found.setup.token });
  return { mode: 'auto', url, token: found.setup.token, setup: found.setup };
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { PW_PORTS, PW_AUTO, pwDiscover, pwProbe, pwIsAuto, pwConnection };
}
