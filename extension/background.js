// Patchwork extension service worker (docs/contracts.md V11, consumes V9 and V15).
//
// Sends the active tab's page (URL, document.title, rendered DOM) to the Patchwork control plane, but only when
// the person presses the `send-page` shortcut (default Alt+Shift+P) or clicks "Send this page" in the popup.
// Nothing runs in the background: no content scripts, no listeners on page loads, no polling except for the page
// just sent. The page goes to the integration chosen in the options or the popup (`integration` in storage; none
// chosen = Patchwork's default integration).
//
// Badge: "…" sending / working, "✓" written by code, "AI" learned now, "dup" already in the tracker,
// "!" held or failed (hover the button for why).
//
// Where Patchwork is: found by itself (discover.js: ports 38091-38110, the ones `./patchwork` uses), cached, and
// found again once after a network failure or a 401. An address set in the options (Advanced) always wins.

importScripts('discover.js');

const NOT_RUNNING = 'Patchwork is not running on this computer: start it with ./patchwork';
const POLL_MS = 700;
const POLL_LIMIT_MS = 30000;
const BADGES = {
  sending: ['…', '#6b7280'],
  code: ['✓', '#15803d'],
  learned_now: ['AI', '#7c3aed'],
  duplicate: ['dup', '#6b7280'],
  held: ['!', '#dc2626'],
  failed: ['!', '#dc2626'],
};

async function chosenIntegration() {
  const o = await chrome.storage.local.get({ integration: '' });
  return String(o.integration || '').trim();
}

async function setBadge(tabId, state, title) {
  const [text, color] = BADGES[state] || BADGES.failed;
  const opts = tabId == null ? {} : { tabId };
  await chrome.action.setBadgeBackgroundColor({ ...opts, color });
  await chrome.action.setBadgeText({ ...opts, text });
  await chrome.action.setTitle({ ...opts, title: `Patchwork: ${title}` });
}

// Runs inside the page (chrome.scripting): what the person sees, as rendered.
function grabPage() {
  return { url: location.href, title: document.title, html: document.documentElement.outerHTML };
}

// Map a V9 TrackerRow to a badge state, or null while it is still in progress.
function outcomeOf(row) {
  if (!row) return null;
  if (row.label && BADGES[row.label]) return row.label;
  if (row.status === 'failed' || row.status === 'held') return 'held';
  if (row.status === 'success') return 'code';
  if (row.status === 'duplicate') return 'duplicate';
  return null;  // pending | running | learning
}

function describe(state, row) {
  const what = [row && row.title, row && row.company].filter(Boolean).join(' at ');
  switch (state) {
    case 'code': return `added ${what || 'the page'} (no AI)`;
    case 'learned_now': return `learned this layout and added ${what || 'the page'}`;
    case 'duplicate': return 'already in your tracker';
    default: return `held for you in Patchwork${row && row.reason ? ` (${row.reason})` : ''}`;
  }
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function pollOutcome(conn, pageId, tabId) {
  const deadline = Date.now() + POLL_LIMIT_MS;
  let row = null;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`${conn.url}/api/pages/${encodeURIComponent(pageId)}`);
      if (r.ok) {
        row = (await r.json()).row;
        const state = outcomeOf(row);
        if (state) return { state, row };
        if (row && row.status === 'learning') await setBadge(tabId, 'sending', 'learning this layout…');
      }
    } catch (e) { /* control plane busy or restarting: keep trying until the deadline */ }
    await sleep(POLL_MS);
  }
  return { state: null, row };
}

async function fail(tabId, message) {
  await setBadge(tabId, 'failed', message);
  return { ok: false, error: message };
}

// The one entry point: called from the shortcut, the toolbar button, or a test.
async function sendActiveTab(tab) {
  if (!tab) [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab) return { ok: false, error: 'no active tab' };
  const tabId = tab.id;
  if (!/^https?:/i.test(tab.url || '')) return fail(tabId, 'only web pages can be sent');
  let conn = await pwConnection();
  if (conn.mode === 'manual' && !conn.token) {
    chrome.runtime.openOptionsPage();
    return fail(tabId, 'set the token in the extension options first');
  }
  if (!conn.url) return fail(tabId, NOT_RUNNING);
  await setBadge(tabId, 'sending', 'sending…');

  let page;
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId }, func: grabPage });
    page = res && res.result;
  } catch (e) {
    return fail(tabId, `could not read this page (${e.message || e})`);
  }
  if (!page || !page.html) return fail(tabId, 'could not read this page');

  const integration = await chosenIntegration();
  const body = JSON.stringify({ url: page.url, title: page.title, html: page.html,
                                ...(integration ? { integration } : {}) });
  const post = async c => {
    try {
      return await fetch(`${c.url}/api/pages`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Patchwork-Token': c.token }, body,
      });
    } catch (e) {
      return null;  // not reachable
    }
  };
  let resp = await post(conn);
  if (conn.mode === 'auto' && (resp === null || resp.status === 401)) {
    // Patchwork restarted on another port, or with another workspace (another token): find it again, once.
    conn = await pwConnection({ rediscover: true });
    if (!conn.url) return fail(tabId, NOT_RUNNING);
    resp = await post(conn);
  }
  if (resp === null) return fail(tabId, `Patchwork is not reachable at ${conn.url}`);
  if (resp.status === 401) {
    return fail(tabId, conn.mode === 'auto' ? 'Patchwork rejected its own token: restart it with ./patchwork'
                                            : 'token rejected: copy it again from Patchwork');
  }
  if (resp.status === 413) return fail(tabId, 'page too large to send');
  if (resp.status === 422 && integration) {
    return fail(tabId, `Patchwork has no integration ${integration}: pick another one in the popup`);
  }
  if (!resp.ok) return fail(tabId, `Patchwork answered HTTP ${resp.status}`);
  const created = await resp.json();

  await setBadge(tabId, 'sending', 'sent; waiting for the result…');
  const { state, row } = await pollOutcome(conn, created.id, tabId);
  if (!state) {
    await setBadge(tabId, 'sending', 'still working: see Patchwork');
    return { ok: true, id: created.id, state: null, row };
  }
  await setBadge(tabId, state, describe(state, row));
  return { ok: true, id: created.id, state, row };
}

chrome.commands.onCommand.addListener((command, tab) => {
  if (command === 'send-page') sendActiveTab(tab);
});
// The popup's "Send this page" button (the toolbar button opens the popup, V15).
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.type !== 'send-page') return false;
  sendActiveTab().then(sendResponse, e => sendResponse({ ok: false, error: String(e && e.message || e) }));
  return true;  // answer asynchronously
});
