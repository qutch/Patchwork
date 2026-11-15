// Popup (contracts V15): switch the integration pages go to, and send the current page.
// Helpers (pwFetchSetup, pwFillSelect, pwSaveIntegration) come from integrations.js; sending is the service
// worker's sendActiveTab() (the same code the shortcut runs), reached with a `send-page` message.
const $ = id => document.getElementById(id);
const say = text => { $('status').textContent = text; };

async function load() {
  const o = await chrome.storage.local.get(PW_DEFAULTS);
  const chosen = o.integration || '';
  pwFillSelect($('integration'), [], null, chosen);
  if (pwIsAuto(o.controlUrl)) {  // found by itself: look now (pwConnection, discover.js)
    const conn = await pwConnection({ rediscover: true });
    if (!conn.url) {
      say('Patchwork is not running. Start it with ./patchwork.');
      return;
    }
    const body = conn.setup || {};
    const integrations = Array.isArray(body.integrations) ? body.integrations.filter(i => i && i.name) : [];
    pwFillSelect($('integration'), integrations, body.default_integration || null, chosen);
    return;
  }
  if (!o.token) {
    say('Set the token in the options first.');
    return;
  }
  try {
    const setup = await pwFetchSetup(o.controlUrl);
    pwFillSelect($('integration'), setup.integrations, setup.defaultIntegration, chosen);
  } catch (e) {
    say(`Patchwork is not reachable at ${pwCleanUrl(o.controlUrl)}.`);
  }
}

$('integration').addEventListener('change', async () => {
  await pwSaveIntegration($('integration').value);
  say($('integration').value ? `Pages now go to ${$('integration').value}.` : "Pages now go to Patchwork's default.");
});

$('send').addEventListener('click', async () => {
  say('Sending…');
  const res = await chrome.runtime.sendMessage({ type: 'send-page' });
  if (!res || !res.ok) say((res && res.error) || 'could not send this page');
  else if (!res.state) say('Sent; Patchwork is still working on it.');
  else say(res.state === 'duplicate' ? 'Already there.' : res.state === 'held' ? 'Held for you in Patchwork.' : 'Added.');
});

$('options').addEventListener('click', e => { e.preventDefault(); chrome.runtime.openOptionsPage(); });
load();
