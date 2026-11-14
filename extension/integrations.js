// Shared by options.html and popup.html (docs/contracts.md V15): the integration a page is sent to.
// GET {controlUrl}/api/extension -> {token, control_url, integrations: [{name, intent}], default_integration}.
// The choice lives in chrome.storage.local as `integration` ('' or absent = Patchwork's default integration).

const PW_DEFAULTS = { controlUrl: 'http://127.0.0.1:8000', token: '', integration: '' };

function pwCleanUrl(url) {
  return String(url || '').trim().replace(/\/+$/, '') || PW_DEFAULTS.controlUrl;
}

// {token, control_url, integrations, default_integration}, or throws (unreachable / HTTP error).
async function pwFetchSetup(controlUrl) {
  const r = await fetch(`${pwCleanUrl(controlUrl)}/api/extension`);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const body = await r.json();
  return {
    token: body.token || '',
    integrations: Array.isArray(body.integrations) ? body.integrations.filter(i => i && i.name) : [],
    defaultIntegration: body.default_integration || null,
  };
}

// Fill a <select>: "" = the default integration, then one option per integration. Keeps `chosen` selectable
// even when Patchwork no longer lists it (so the person sees what is stored).
function pwFillSelect(select, integrations, defaultIntegration, chosen) {
  select.textContent = '';
  const add = (value, label) => {
    const o = document.createElement('option');
    o.value = value;
    o.textContent = label;
    select.appendChild(o);
  };
  add('', defaultIntegration ? `Patchwork's default (${defaultIntegration})` : "Patchwork's default integration");
  for (const i of integrations) add(i.name, i.intent ? `${i.name}: ${i.intent}` : i.name);
  if (chosen && !integrations.some(i => i.name === chosen)) add(chosen, `${chosen} (not listed by Patchwork)`);
  select.value = chosen || '';
}

async function pwSaveIntegration(value) {
  if (value) await chrome.storage.local.set({ integration: value });
  else await chrome.storage.local.remove('integration');
}
