// Options: controlUrl + token in chrome.storage.local (docs/contracts.md V11).
const DEFAULTS = { controlUrl: 'http://127.0.0.1:8000', token: '' };
const $ = id => document.getElementById(id);
const say = text => { $('status').textContent = text; };
const clean = url => String(url || '').trim().replace(/\/+$/, '') || DEFAULTS.controlUrl;

async function load() {
  const o = await chrome.storage.local.get(DEFAULTS);
  $('controlUrl').value = o.controlUrl || DEFAULTS.controlUrl;
  $('token').value = o.token || '';
}

async function save() {
  const controlUrl = clean($('controlUrl').value);
  const token = $('token').value.trim();
  await chrome.storage.local.set({ controlUrl, token });
  $('controlUrl').value = controlUrl;
  say(token ? 'Saved.' : 'Saved. Add the token before sending pages.');
}

// GET /api/extension -> {token, control_url} (V9): the same values Patchwork's setup panel shows.
async function fetchToken() {
  const controlUrl = clean($('controlUrl').value);
  try {
    const r = await fetch(`${controlUrl}/api/extension`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const body = await r.json();
    $('token').value = body.token || '';
    await save();
    say('Connected to Patchwork. Token saved.');
  } catch (e) {
    say(`Could not reach Patchwork at ${controlUrl} (${e.message || e}). Paste the token instead.`);
  }
}

$('save').addEventListener('click', save);
$('fetch').addEventListener('click', fetchToken);
load();
