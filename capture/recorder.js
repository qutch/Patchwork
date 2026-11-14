// Patchwork recorder: injected into every page with add_init_script, streams DOM events to
// Python through the `pwRecord` binding (expose_binding). Network and navigation are
// captured on the Python side. Event shape: fixtures/recording.json.
//
// Redaction (always on): password fields and fields with autocomplete=cc-* / one-time-code /
// *-password are never recorded, whatever the event.
(() => {
  if (window.__pwRecorderInstalled) return;
  window.__pwRecorderInstalled = true;

  const DEBOUNCE_MS = 400;
  const queue = [];

  function send(ev) {
    ev.t = ev.t || Date.now();
    ev.url = location.href;
    if (typeof window.pwRecord === 'function') {
      while (queue.length) window.pwRecord(queue.shift()).catch(() => {});
      window.pwRecord(ev).catch(() => {});
    } else {
      queue.push(ev);
    }
  }

  // ---------- redaction ----------
  function isSensitive(el) {
    if (!el || el.nodeType !== 1) return false;
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' && (el.type || '').toLowerCase() === 'password') return true;
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase().split(/\s+/);
    return ac.some(t => t.startsWith('cc-') || t === 'one-time-code' || t === 'current-password' || t === 'new-password');
  }

  // ---------- element descriptor ----------
  const IMPLICIT_ROLES = {
    a: el => (el.hasAttribute('href') ? 'link' : null), button: () => 'button', select: el => (el.multiple || el.size > 1 ? 'listbox' : 'combobox'),
    textarea: () => 'textbox', form: () => 'form', img: () => 'img', nav: () => 'navigation', main: () => 'main',
    table: () => 'table', tr: () => 'row', td: () => 'cell', th: () => 'columnheader', ul: () => 'list', ol: () => 'list',
    li: () => 'listitem', h1: () => 'heading', h2: () => 'heading', h3: () => 'heading', h4: () => 'heading',
    dialog: () => 'dialog', summary: () => 'button', option: () => 'option',
    input: el => ({
      button: 'button', submit: 'button', reset: 'button', image: 'button', checkbox: 'checkbox', radio: 'radio',
      number: 'spinbutton', range: 'slider', search: 'searchbox', hidden: null, file: null,
    }[(el.type || 'text').toLowerCase()] ?? 'textbox'),
  };
  const CONTROLS = new Set(['input', 'select', 'textarea', 'button']);

  const clean = (s, n = 80) => {
    s = (s || '').replace(/\s+/g, ' ').trim();
    return s ? (s.length > n ? s.slice(0, n - 1) + '…' : s) : null;
  };

  function role(el) {
    const r = el.getAttribute('role');
    if (r) return r;
    const f = IMPLICIT_ROLES[el.tagName.toLowerCase()];
    return f ? f(el) : null;
  }

  function ownText(node) {
    // Text of a node without the text of form controls inside it (a <label> wrapping a <select>).
    const c = node.cloneNode(true);
    c.querySelectorAll && c.querySelectorAll('select,input,textarea,button,option,script,style').forEach(x => x.remove());
    return clean(c.textContent);
  }

  function labelText(el) {
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(ownText).join(' ');
      if (t) return clean(t);
    }
    if (el.labels && el.labels.length) return ownText(el.labels[0]);
    return clean(el.getAttribute('aria-label'));
  }

  function accessibleName(el) {
    const tag = el.tagName.toLowerCase();
    if (CONTROLS.has(tag)) {
      if (tag === 'button' || (tag === 'input' && /^(button|submit|reset)$/i.test(el.type))) {
        return clean(el.getAttribute('aria-label')) || clean(el.value || el.textContent);
      }
      return labelText(el) || clean(el.getAttribute('placeholder')) || clean(el.getAttribute('title'));
    }
    const fromContent = NAME_FROM_CONTENT.has(tag) || NAME_FROM_CONTENT_ROLES.has(el.getAttribute('role'));
    return clean(el.getAttribute('aria-label')) || clean(el.getAttribute('alt')) ||
      (fromContent ? clean(el.textContent) : null) || clean(el.getAttribute('title'));
  }
  const NAME_FROM_CONTENT = new Set(['a', 'summary', 'option', 'label', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'td', 'th', 'li', 'legend', 'caption']);
  const NAME_FROM_CONTENT_ROLES = new Set(['link', 'button', 'tab', 'menuitem', 'option', 'cell', 'heading', 'checkbox', 'radio', 'switch']);

  function cssPath(el) {
    const seg = e => {
      const tag = e.tagName.toLowerCase();
      if (e.id) return `${tag}#${CSS.escape(e.id)}`;
      const n = e.getAttribute('name');
      return n && CONTROLS.has(tag) ? `${tag}[name=${n}]` : tag;
    };
    const parts = [seg(el)];
    let cur = el.parentElement;
    while (cur && parts.length < 5 && !el.id) {
      const tag = cur.tagName.toLowerCase();
      if (tag === 'body' || tag === 'html') break;
      if (!['tbody', 'thead', 'label', 'span', 'div'].includes(tag) || cur.id) parts.unshift(seg(cur));
      if (cur.id) break;
      cur = cur.parentElement;
    }
    return parts.join(' ');
  }

  function nearby(el) {
    const tag = el.tagName.toLowerCase();
    if (CONTROLS.has(tag) && tag !== 'button') {
      const l = labelText(el);
      if (l) return l;
    }
    for (let cur = el; cur && cur !== document.body; cur = cur.parentElement) {
      let sib = cur.previousElementSibling;
      while (sib) {
        const t = ownText(sib);
        if (t) return clean(t, 60);
        sib = sib.previousElementSibling;
      }
    }
    return null;
  }

  function describe(el) {
    if (!el || el.nodeType !== 1) return null;
    const tag = el.tagName.toLowerCase();
    const aname = accessibleName(el);
    const nameAttr = el.getAttribute('name');
    return {
      tag,
      role: role(el),
      name: (CONTROLS.has(tag) && nameAttr) ? nameAttr : aname,
      label: CONTROLS.has(tag) ? labelText(el) : null,
      testid: el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-qa') || null,
      css: cssPath(el),
      nearby: nearby(el),
      id: el.id || null,
      accessible_name: aname,
    };
  }

  const isField = el => el && /^(input|select|textarea)$/i.test(el.tagName) || (el && el.isContentEditable);
  const isChoice = el => /^select$/i.test(el.tagName) || (/^input$/i.test(el.tagName) && /^(checkbox|radio|file|range|color|date|time|datetime-local|month|week)$/i.test(el.type));
  const valueOf = el => {
    if (el.isContentEditable) return el.innerText;
    if (/^input$/i.test(el.tagName) && /^(checkbox|radio)$/i.test(el.type)) return el.checked ? (el.value || 'on') : '';
    if (/^input$/i.test(el.tagName) && el.type === 'file') return Array.from(el.files || []).map(f => f.name).join(', ');
    return el.value;
  };

  // ---------- input debouncing ----------
  const pending = new Map();     // el -> timer
  const lastSent = new WeakMap(); // el -> last value sent
  const pasted = new WeakMap();   // el -> text pasted into it

  function emitValue(el, type) {
    if (isSensitive(el)) return;
    const value = valueOf(el);
    if (lastSent.get(el) === value) return;
    lastSent.set(el, value);
    if (type === 'input' && pasted.get(el) === value) return; // the paste event already carries it
    send({ type, value, el: describe(el) });
  }

  function flush(el) {
    if (!pending.has(el)) return;
    clearTimeout(pending.get(el));
    pending.delete(el);
    emitValue(el, 'input');
  }
  function flushAll() { for (const el of Array.from(pending.keys())) flush(el); }
  window.__pwFlush = flushAll;

  document.addEventListener('input', e => {
    const el = e.composedPath ? e.composedPath()[0] : e.target;
    if (!isField(el) || isSensitive(el) || isChoice(el)) return;
    if (pending.has(el)) clearTimeout(pending.get(el));
    pending.set(el, setTimeout(() => flush(el), DEBOUNCE_MS));
  }, true);

  document.addEventListener('change', e => {
    const el = e.target;
    if (!isField(el) || isSensitive(el)) return;
    if (isChoice(el)) emitValue(el, 'change');
    else { if (pending.has(el)) flush(el); else emitValue(el, 'input'); }
  }, true);

  document.addEventListener('blur', e => { if (pending.has(e.target)) flush(e.target); }, true);

  // Tell Python the *names* (never the values) of sensitive fields, so it can mask the same
  // keys in request bodies (a form posting {card: ...}). Python does not log this event.
  const reported = new Set();
  const reportSensitive = e => {
    const el = e.target;
    if (!isSensitive(el)) return;
    const key = el.getAttribute('name') || el.id;
    if (key && !reported.has(key)) { reported.add(key); send({ type: 'sensitive_field', name: key }); }
  };
  document.addEventListener('focus', reportSensitive, true);
  document.addEventListener('input', reportSensitive, true);

  // ---------- other events ----------
  document.addEventListener('click', e => {
    const raw = e.target && e.target.nodeType === 1 ? e.target : e.target && e.target.parentElement;
    if (!raw) return;
    const el = raw.closest('a,button,input,select,textarea,label,summary,[role],[onclick],[data-testid]') || raw;
    if (isSensitive(el)) return;
    send({ type: 'click', el: describe(el) });
  }, true);

  document.addEventListener('keydown', e => {
    if (e.key !== 'Enter' || isSensitive(e.target)) return;
    if (isField(e.target)) flush(e.target);
    send({ type: 'keydown', key: 'Enter', el: describe(e.target) });
  }, true);

  document.addEventListener('submit', e => {
    flushAll();
    const form = e.target;
    const desc = describe(form);
    if (e.submitter) desc.nearby = clean(e.submitter.value || e.submitter.textContent) || desc.nearby;
    else {
      const b = form.querySelector('button[type=submit],button:not([type]),input[type=submit]');
      if (b) desc.nearby = clean(b.value || b.textContent) || desc.nearby;
    }
    send({ type: 'submit', el: desc });
  }, true);

  document.addEventListener('copy', e => {
    const t = e.target && e.target.nodeType === 1 ? e.target : null;
    let text = '', el = null, near = null;
    if (t && /^(input|textarea)$/i.test(t.tagName) && t.selectionStart != null) {
      if (isSensitive(t)) return;
      text = t.value.slice(t.selectionStart, t.selectionEnd);
      el = t;
    } else {
      const sel = document.getSelection();
      text = sel ? sel.toString() : '';
      if (sel && sel.rangeCount) {
        const r = sel.getRangeAt(0);
        const n = r.startContainer;
        el = n.nodeType === 1 ? n : n.parentElement;
        if (n.nodeType === 3) {
          const before = n.textContent.slice(0, r.startOffset).split('\n').pop();
          near = clean(before.slice(-40));
        }
      }
      el = el || t;
    }
    if (!text || (el && isSensitive(el))) return;
    const desc = describe(el);
    if (desc && near) desc.nearby = near;
    send({ type: 'copy', text, el: desc });
  }, true);

  document.addEventListener('paste', e => {
    const el = e.target;
    if (isSensitive(el)) return;
    const value = (e.clipboardData && e.clipboardData.getData('text')) || '';
    if (isField(el)) pasted.set(el, value);
    send({ type: 'paste', value, el: describe(el) });
  }, true);

  window.addEventListener('pagehide', flushAll, true);
  document.addEventListener('visibilitychange', () => { if (document.hidden) flushAll(); }, true);
})();
