# Patchwork Chrome extension

Press **Alt+Shift+P** (or click **Send this page** in the toolbar button's popup) on a page (a job posting, a
product, a paper, ...) and the extension sends that page (its URL, title and rendered HTML) to your local Patchwork,
which adds it to the integration you chose. It sends nothing unless you press the shortcut or click the button. It has no content scripts and does not watch the pages you visit.

## Install (once)

1. Start Patchwork: `./patchwork` at the repo root.
2. In Chrome open `chrome://extensions`, switch on **Developer mode** (top right), click **Load unpacked** and choose
   this `extension/` folder.
3. Click the puzzle-piece icon in the toolbar and pin **Patchwork: send this page**.
4. Nothing to set: the extension finds Patchwork by itself. It asks `http://127.0.0.1:38091` to `:38110` (the ports
   `./patchwork` uses) for `GET /api/extension` and takes the address and the token from the first that answers. If
   Patchwork restarts on another port or with another workspace, the next send finds it again. The options page
   (right-click the toolbar button, **Options**) says "Connected automatically to ...". To use another address, set
   it under **Advanced** and click **Get token from Patchwork** (or paste the token from Patchwork's setup panel);
   an address set there always wins. Clear it, or click **Find Patchwork**, to go back to automatic.
5. Check the shortcut at `chrome://extensions/shortcuts`. If Alt+Shift+P is already used by something else, Chrome
   leaves it blank: set it there.

## Use

Open a page and press Alt+Shift+P. The toolbar badge shows the result. Hover the button to see the details.

**Which integration.** The options page and the popup have a "Send pages to" list, read from Patchwork
(`GET /api/extension` → `integrations`, `default_integration`). Pick one and every page goes to it (the extension
sends `integration` with the page); leave "Patchwork's default" and pages go to the default integration
(`jobs_to_tracker` when it exists). The popup's choice is remembered until you change it.

| Badge | Meaning |
|---|---|
| `…` | sending, or Patchwork is still working (for example, learning a new layout) |
| `✓` | added to the tracker by code (no AI) |
| `AI` | Patchwork learned this page layout just now and added the posting |
| `dup` | this posting is already in the tracker |
| `!` | held for you in Patchwork, or something failed (wrong token, Patchwork not running) |

The extension waits up to 30 s for the result. After that the badge stays `…` and the result appears in Patchwork.

## Permissions

`activeTab` + `scripting` let it read the tab you are on, and only when you press the shortcut or click the button.
`storage` holds the address (or `auto`), the token and the chosen integration. The host permissions cover `127.0.0.1` and `localhost` (any port) so it
can reach a local Patchwork. To use a Patchwork that is not on this computer, add that address to
`host_permissions` in `manifest.json` and reload the extension.

## Tests

`tests/extension/test_extension.py` loads this folder into headless Chromium (Playwright's `chromium` channel) with a
stub control plane. A test cannot press a Chrome shortcut, so it calls the service worker's `sendActiveTab()`
directly. The shortcut and button handlers call the same function. Those tests point discovery at their own ports
(`discoverPorts` in storage), never at 38091-38110. `tests/extension/discover.test.js` tests the discovery function
itself with node's built-in runner (`node --test tests/extension/discover.test.js`; pytest runs it too).
