// Options: controlUrl + token + the integration pages go to, in chrome.storage.local (contracts V11, V15).
// Helpers (pwFetchSetup, pwFillSelect, pwSaveIntegration, pwCleanUrl) come from integrations.js.
const $ = id => document.getElementById(id);
const say = text => { $('status').textContent = text; };

let listed = { integrations: [], defaultIntegration: null };

async function refreshIntegrations(controlUrl, chosen) {
  try {
    const setup = await pwFetchSetup(controlUrl);
    listed = { integrations: setup.integrations, defaultIntegration: setup.defaultIntegration };
  } catch (e) { /* Patchwork not reachable: keep what we have */ }
  pwFillSelect($('integration'), listed.integrations, listed.defaultIntegration, chosen);
}

async function load() {
  const o = await chrome.storage.local.get(PW_DEFAULTS);
  $('controlUrl').value = o.controlUrl || PW_DEFAULTS.controlUrl;
  $('token').value = o.token || '';
  pwFillSelect($('integration'), [], null, o.integration || '');
  // Ask Patchwork for its integrations only once it is set up (a token saved), never on a first open.
  if (o.token) await refreshIntegrations($('controlUrl').value, o.integration || '');
}

async function save() {
  const controlUrl = pwCleanUrl($('controlUrl').value);
  const token = $('token').value.trim();
  await chrome.storage.local.set({ controlUrl, token });
  await pwSaveIntegration($('integration').value);
  $('controlUrl').value = controlUrl;
  say(token ? 'Saved.' : 'Saved. Add the token before sending pages.');
}

// GET /api/extension -> {token, control_url, integrations, default_integration} (V9, V15): the same values
// Patchwork's setup panel shows.
async function fetchToken() {
  const controlUrl = pwCleanUrl($('controlUrl').value);
  try {
    const setup = await pwFetchSetup(controlUrl);
    $('token').value = setup.token;
    listed = { integrations: setup.integrations, defaultIntegration: setup.defaultIntegration };
    pwFillSelect($('integration'), listed.integrations, listed.defaultIntegration, $('integration').value);
    await save();
    say('Connected to Patchwork. Token saved.');
  } catch (e) {
    say(`Could not reach Patchwork at ${controlUrl} (${e.message || e}). Paste the token instead.`);
  }
}

$('save').addEventListener('click', save);
$('fetch').addEventListener('click', fetchToken);
load();
