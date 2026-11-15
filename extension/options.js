// Options (contracts V11, V15; one-command launcher design): how the extension reaches Patchwork, and the
// integration pages go to, in chrome.storage.local.
// By default Patchwork is found by itself (pwConnection, discover.js). "Advanced" sets an address (and token)
// by hand, which then wins; clearing the address goes back to automatic.
// Helpers (pwFetchSetup, pwFillSelect, pwSaveIntegration, pwCleanUrl) come from integrations.js.
const $ = id => document.getElementById(id);
const say = text => { $('status').textContent = text; };
const connection = text => { $('connection').textContent = text; };

let listed = { integrations: [], defaultIntegration: null };

function showIntegrations(setup, chosen) {
  if (setup) listed = { integrations: setup.integrations || [], defaultIntegration: setup.defaultIntegration || null };
  pwFillSelect($('integration'), listed.integrations, listed.defaultIntegration, chosen);
}

// GET /api/extension's body -> the shape pwFillSelect wants.
function setupOf(body) {
  return {
    integrations: Array.isArray(body.integrations) ? body.integrations.filter(i => i && i.name) : [],
    defaultIntegration: body.default_integration || null,
  };
}

async function refreshIntegrations(controlUrl, chosen) {
  let setup = null;
  try {
    setup = await pwFetchSetup(controlUrl);
  } catch (e) { /* Patchwork not reachable: keep what we have */ }
  showIntegrations(setup, chosen);
}

// Automatic: look at the ports ./patchwork uses, now (the cached address may be out of date).
async function connectAuto(chosen) {
  connection('Looking for Patchwork on this computer…');
  const conn = await pwConnection({ rediscover: true });
  if (!conn.url) {
    connection('Patchwork is not running on this computer. Start it with ./patchwork, then click "Find Patchwork".');
    showIntegrations(null, chosen);
    return false;
  }
  connection(`Connected automatically to ${conn.url}`);
  showIntegrations(setupOf(conn.setup || {}), chosen);
  return true;
}

async function load() {
  const o = await chrome.storage.local.get(PW_DEFAULTS);
  const chosen = o.integration || '';
  pwFillSelect($('integration'), [], null, chosen);
  if (pwIsAuto(o.controlUrl)) {
    $('controlUrl').value = '';
    $('token').value = '';
    await connectAuto(chosen);
    return;
  }
  $('controlUrl').value = o.controlUrl;
  $('token').value = o.token || '';
  connection(`Using the address set below (${pwCleanUrl(o.controlUrl)}). Clear it to find Patchwork automatically.`);
  // Ask Patchwork for its integrations only once it is set up (a token saved), never on a first open.
  if (o.token) await refreshIntegrations(o.controlUrl, chosen);
}

async function save() {
  const controlUrl = pwCleanUrl($('controlUrl').value);
  await pwSaveIntegration($('integration').value);
  if (pwIsAuto(controlUrl)) {
    await chrome.storage.local.remove('controlUrl');
    $('controlUrl').value = '';
    $('token').value = '';
    const found = await connectAuto($('integration').value);
    say(found ? 'Saved. Patchwork is found automatically.' : 'Saved. Patchwork will be found automatically once it runs.');
    return;
  }
  const token = $('token').value.trim();
  await chrome.storage.local.set({ controlUrl, token });
  await chrome.storage.local.remove('autoUrl');
  $('controlUrl').value = controlUrl;
  connection(`Using the address set below (${controlUrl}). Clear it to find Patchwork automatically.`);
  say(token ? 'Saved.' : 'Saved. Add the token before sending pages.');
}

// GET /api/extension -> {token, control_url, integrations, default_integration} (V9, V15): the same values
// Patchwork's setup panel shows. With no address typed, this is "find Patchwork" again.
async function fetchToken() {
  const controlUrl = pwCleanUrl($('controlUrl').value);
  if (pwIsAuto(controlUrl)) {
    await save();
    return;
  }
  try {
    const setup = await pwFetchSetup(controlUrl);
    $('token').value = setup.token;
    showIntegrations(setup, $('integration').value);
    await save();
    say('Connected to Patchwork. Token saved.');
  } catch (e) {
    say(`Could not reach Patchwork at ${controlUrl} (${e.message || e}). Paste the token instead.`);
  }
}

$('save').addEventListener('click', save);
$('fetch').addEventListener('click', fetchToken);
// "Find Patchwork": automatic again (a manual address is cleared), and look now.
$('find').addEventListener('click', async () => {
  $('controlUrl').value = '';
  await save();
});
load();
