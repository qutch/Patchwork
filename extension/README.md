# Patchwork Chrome extension

Press **Alt+Shift+P** (or click the toolbar button) on a job posting and the extension sends that page (its URL,
title and rendered HTML) to your local Patchwork, which adds the posting to your tracker. It sends nothing unless you
press the shortcut or click the button. It has no content scripts and does not watch the pages you visit.

## Install (once)

1. Start Patchwork (the control plane, by default `http://127.0.0.1:8000`).
2. In Chrome open `chrome://extensions`, switch on **Developer mode** (top right), click **Load unpacked** and choose
   this `extension/` folder.
3. Click the puzzle-piece icon in the toolbar and pin **Patchwork: send this page**.
4. Right-click the toolbar button and choose **Options** (or click **Details > Extension options** on
   `chrome://extensions`). Check the address, then click **Get token from Patchwork**. If that fails, copy the token
   from Patchwork's setup panel, paste it and click **Save**.
5. Check the shortcut at `chrome://extensions/shortcuts`. If Alt+Shift+P is already used by something else, Chrome
   leaves it blank: set it there.

## Use

Open a job posting and press Alt+Shift+P. The toolbar badge shows the result. Hover the button to see the details.

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
`storage` holds the address and the token. The host permissions cover `127.0.0.1` and `localhost` (any port) so it
can reach a local Patchwork. To use a Patchwork that is not on this computer, add that address to
`host_permissions` in `manifest.json` and reload the extension.

## Tests

`tests/extension/test_extension.py` loads this folder into headless Chromium (Playwright's `chromium` channel) with a
stub control plane. A test cannot press a Chrome shortcut, so it calls the service worker's `sendActiveTab()`
directly. The shortcut and button handlers call the same function.
