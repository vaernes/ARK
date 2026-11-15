/* Shared ARK UI behaviour: modal a11y, ARIA tabs, KPI cards, busy buttons,
   nav state, and helpers (el, showToast, confirmModal, arkSwitchTab, arkFieldError).
   Local only (air-gapped). Loaded synchronously in <head> so page scripts can use the
   helpers immediately; DOM enhancements run on DOMContentLoaded. */
(function () {
  'use strict';

  const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  function visible(node) {
    return !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
  }

  // ---- Modal dialogs: focus move/trap/restore, Esc and backdrop close ----
  function enhanceModal(backdrop) {
    const card = backdrop.querySelector('.modal-card');
    const title = backdrop.querySelector('h3');
    backdrop.setAttribute('role', 'dialog');
    backdrop.setAttribute('aria-modal', 'true');
    if (title && title.id) backdrop.setAttribute('aria-labelledby', title.id);
    if (card) card.setAttribute('tabindex', '-1');

    let opener = null;

    const cancel = () => {
      const btn = backdrop.querySelector('[id$="cancel"], button.secondary');
      if (btn) btn.click();
    };

    backdrop.addEventListener('mousedown', (ev) => {
      if (ev.target === backdrop) cancel();
    });

    backdrop.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') {
        ev.preventDefault();
        cancel();
        return;
      }
      if (ev.key !== 'Tab') return;
      const items = Array.from(backdrop.querySelectorAll(FOCUSABLE)).filter(visible);
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (ev.shiftKey && document.activeElement === first) {
        ev.preventDefault();
        last.focus();
      } else if (!ev.shiftKey && document.activeElement === last) {
        ev.preventDefault();
        first.focus();
      }
    });

    new MutationObserver(() => {
      if (backdrop.classList.contains('open')) {
        if (!opener) opener = document.activeElement;
        // Default focus on the safe choice (the first button) for destructive prompts.
        const target = backdrop.querySelector('button.secondary') || backdrop.querySelector(FOCUSABLE) || card;
        if (target) target.focus();
      } else if (opener) {
        if (opener.isConnected && typeof opener.focus === 'function') opener.focus();
        opener = null;
      }
    }).observe(backdrop, { attributes: true, attributeFilter: ['class'] });
  }

  // ---- ARIA tabs: roving tabindex, arrow keys, aria-controls ----
  function enhanceTablist(list) {
    const tabs = Array.from(list.querySelectorAll('[role="tab"]'));
    if (!tabs.length) return;

    tabs.forEach((tab) => {
      const key = (tab.id || '').replace(/^tab-btn-/, '');
      const panel = key ? document.getElementById(`panel-${key}`) : null;
      if (panel) {
        tab.setAttribute('aria-controls', panel.id);
        if (!panel.getAttribute('aria-labelledby')) panel.setAttribute('aria-labelledby', tab.id);
      }
    });

    const sync = () => {
      tabs.forEach((tab) => {
        tab.tabIndex = tab.getAttribute('aria-selected') === 'true' ? 0 : -1;
      });
    };
    sync();
    new MutationObserver(sync).observe(list, { attributes: true, subtree: true, attributeFilter: ['aria-selected'] });

    list.addEventListener('keydown', (ev) => {
      const idx = tabs.indexOf(document.activeElement);
      if (idx < 0) return;
      let next = -1;
      if (ev.key === 'ArrowRight') next = (idx + 1) % tabs.length;
      else if (ev.key === 'ArrowLeft') next = (idx - 1 + tabs.length) % tabs.length;
      else if (ev.key === 'Home') next = 0;
      else if (ev.key === 'End') next = tabs.length - 1;
      if (next < 0) return;
      ev.preventDefault();
      tabs[next].focus();
      tabs[next].click();
    });
  }

  // ---- Clickable KPI cards become keyboard-operable buttons ----
  function enhanceKpiCards() {
    document.querySelectorAll('.kpi-card[onclick]').forEach((card) => {
      card.setAttribute('role', 'button');
      card.tabIndex = 0;
      card.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          card.click();
        }
      });
    });
  }

  // ---- Busy state: disable the triggering button while an async handler runs ----
  function guardAsyncHandlers() {
    const names = new Set();
    document.querySelectorAll('button[onclick]').forEach((btn) => {
      const m = /^\s*(?:return\s+)?([A-Za-z_$][\w$]*)\s*\(/.exec(btn.getAttribute('onclick') || '');
      if (m) names.add(m[1]);
    });

    names.forEach((name) => {
      const fn = window[name];
      if (typeof fn !== 'function' || fn.constructor.name !== 'AsyncFunction') return;
      if (/^(confirmModal|switchTab|refreshAll)$/.test(name)) return;

      const wrapped = function (...args) {
        const ev = window.event;
        const btn = ev && ev.currentTarget instanceof HTMLButtonElement ? ev.currentTarget : null;
        if (!btn) return fn.apply(this, args);
        if (btn.getAttribute('aria-busy') === 'true') return undefined;
        btn.setAttribute('aria-busy', 'true');
        const done = () => btn.removeAttribute('aria-busy');
        let result;
        try {
          result = fn.apply(this, args);
        } catch (err) {
          done();
          throw err;
        }
        Promise.resolve(result).then(done, done);
        return result;
      };
      Object.defineProperty(wrapped, 'name', { value: name });
      window[name] = wrapped;
    });
  }

  // ---- Header nav: mark the current page, give Sign Out its own style ----
  function markNav() {
    const path = window.location.pathname.replace(/\/+$/, '') || '/';
    document.querySelectorAll('header a.nav-link[href]').forEach((a) => {
      const href = a.getAttribute('href');
      if (href === '/oauth2/sign_out') {
        a.classList.add('signout');
        return;
      }
      const target = href.replace(/\/+$/, '') || '/';
      if (target !== '/' && path === target) a.setAttribute('aria-current', 'page');
    });
  }

  // ---- Shared toast + confirm dialog (pages without their own get these) ----
  function ensureToastContainer() {
    let box = document.getElementById('toast-container');
    if (!box) {
      box = document.createElement('div');
      box.id = 'toast-container';
      box.setAttribute('aria-live', 'polite');
      document.body.appendChild(box);
    }
    return box;
  }

  function arkToast(msg, kind) {
    if (!msg) return;
    const toast = document.createElement('div');
    toast.className = `toast ${kind || 'ok'}`;
    const text = document.createElement('span');
    text.textContent = msg;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'secondary sm';
    close.setAttribute('aria-label', 'Dismiss notification');
    close.style.cssText = 'padding:2px 6px;font-size:0.7rem;margin-top:0;';
    close.textContent = '\u00d7';
    close.addEventListener('click', () => toast.remove());
    toast.append(text, close);
    ensureToastContainer().appendChild(toast);
    setTimeout(() => toast.remove(), 4500);
  }

  function ensureModal() {
    if (document.getElementById('ark-modal-backdrop')) return;
    const backdrop = document.createElement('div');
    backdrop.id = 'ark-modal-backdrop';
    backdrop.className = 'modal-backdrop';
    backdrop.innerHTML =
      '<div class="modal-card">' +
      '<h3 id="ark-modal-title">Confirm Action</h3>' +
      '<p id="ark-modal-message"></p>' +
      '<div id="ark-modal-extra" style="display:none;margin-bottom:12px;">' +
      '<label style="display:inline-flex;align-items:center;gap:8px;color:var(--text);font-weight:500;cursor:pointer;">' +
      '<input id="ark-modal-checkbox" type="checkbox" style="width:auto;" />' +
      '<span id="ark-modal-checkbox-label"></span></label></div>' +
      '<div style="display:flex;justify-content:flex-end;gap:8px;">' +
      '<button type="button" id="ark-modal-cancel" class="secondary sm" style="padding:7px 14px;">Cancel</button>' +
      '<button type="button" id="ark-modal-confirm" class="amber sm" style="padding:7px 14px;">Confirm</button>' +
      '</div></div>';
    document.body.appendChild(backdrop);
  }

  function arkConfirm(opts) {
    const { title = 'Confirm Action', message = '', confirmText = 'Confirm', danger = false, checkboxLabel = '' } = opts || {};
    return new Promise((resolve) => {
      const backdrop = document.getElementById('ark-modal-backdrop');
      const checkbox = document.getElementById('ark-modal-checkbox');
      const confirmBtn = document.getElementById('ark-modal-confirm');
      const cancelBtn = document.getElementById('ark-modal-cancel');
      document.getElementById('ark-modal-title').textContent = title;
      document.getElementById('ark-modal-message').textContent = message;
      checkbox.checked = false;
      document.getElementById('ark-modal-checkbox-label').textContent = checkboxLabel;
      document.getElementById('ark-modal-extra').style.display = checkboxLabel ? 'block' : 'none';
      confirmBtn.textContent = confirmText;
      confirmBtn.className = `${danger ? 'danger' : 'amber'} sm`;
      backdrop.classList.add('open');

      const finish = (result) => {
        backdrop.classList.remove('open');
        confirmBtn.onclick = null;
        cancelBtn.onclick = null;
        resolve(result);
      };
      confirmBtn.onclick = () => finish({ confirmed: true, checked: checkbox.checked });
      cancelBtn.onclick = () => finish({ confirmed: false, checked: false });
    });
  }

  window.arkToast = arkToast;
  window.arkConfirm = arkConfirm;
  window.showToast = arkToast;
  window.confirmModal = arkConfirm;

  // Small DOM builder shared by all pages (text-safe: uses textContent, never innerHTML).
  function arkEl(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'className') node.className = v;
      else if (k === 'style') node.setAttribute('style', v);
      else if (k === 'text') node.textContent = v;
      else if (k === 'colSpan') node.colSpan = v;
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2).toLowerCase(), v);
      else node.setAttribute(k, v);
    }
    const list = children === undefined ? [] : (Array.isArray(children) ? children : [children]);
    for (const child of list) {
      if (child === null || child === undefined) continue;
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    }
    return node;
  }
  window.el = arkEl;

  // Shared tab switching: pages pass their tab ids; sets active classes, ARIA state and URL hash.
  function arkSwitchTab(tabIds, tabId, updateHash) {
    for (const id of tabIds) {
      const btn = document.getElementById(`tab-btn-${id}`);
      const panel = document.getElementById(`panel-${id}`);
      const isCurrent = id === tabId;
      if (btn) {
        btn.classList.toggle('active', isCurrent);
        btn.setAttribute('aria-selected', isCurrent ? 'true' : 'false');
      }
      if (panel) panel.classList.toggle('active', isCurrent);
    }
    if (updateHash !== false && window.history && window.history.replaceState) {
      window.history.replaceState(null, '', `#${tabId}`);
    }
  }
  window.arkSwitchTab = arkSwitchTab;

  // Overflow menu for dense table action cells. items: [{ text, onClick, danger }].
  // The list is position:fixed so scrollable cards cannot clip it.
  function arkMenu(label, items) {
    const details = document.createElement('details');
    details.className = 'ark-menu';
    const summary = document.createElement('summary');
    summary.textContent = label;
    summary.setAttribute('aria-haspopup', 'menu');
    const list = document.createElement('div');
    list.className = 'ark-menu-list';
    list.setAttribute('role', 'menu');
    for (const item of items) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.setAttribute('role', 'menuitem');
      btn.className = item.danger ? 'ark-menu-item danger-item' : 'ark-menu-item';
      btn.textContent = item.text;
      btn.addEventListener('click', () => {
        details.open = false;
        item.onClick();
      });
      list.appendChild(btn);
    }
    details.append(summary, list);

    details.addEventListener('toggle', () => {
      if (!details.open) return;
      document.querySelectorAll('details.ark-menu[open]').forEach((d) => {
        if (d !== details) d.open = false;
      });
      const r = summary.getBoundingClientRect();
      list.style.top = `${Math.round(r.bottom + 4)}px`;
      list.style.right = `${Math.max(8, Math.round(window.innerWidth - r.right))}px`;
      const first = list.querySelector('button');
      if (first) first.focus();
    });
    details.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && details.open) {
        ev.stopPropagation();
        details.open = false;
        summary.focus();
      } else if ((ev.key === 'ArrowDown' || ev.key === 'ArrowUp') && details.open) {
        ev.preventDefault();
        const btns = Array.from(list.querySelectorAll('button'));
        const idx = btns.indexOf(document.activeElement);
        const next = ev.key === 'ArrowDown' ? (idx + 1) % btns.length : (idx - 1 + btns.length) % btns.length;
        btns[next].focus();
      }
    });
    return details;
  }
  window.arkMenu = arkMenu;

  function closeMenus(ev) {
    document.querySelectorAll('details.ark-menu[open]').forEach((d) => {
      if (!ev || !d.contains(ev.target)) d.open = false;
    });
  }
  document.addEventListener('click', closeMenus);
  window.addEventListener('scroll', () => closeMenus(null), true);
  window.addEventListener('resize', () => closeMenus(null));

  // Inline validation: marks the field invalid, shows a message under it, focuses it.
  function arkFieldError(target, message) {
    const input = typeof target === 'string' ? document.getElementById(target) : target;
    if (!input) return;
    const errId = `${input.id || 'field'}-error`;
    let err = document.getElementById(errId);
    if (!err) {
      err = document.createElement('div');
      err.id = errId;
      err.className = 'field-error';
      err.setAttribute('role', 'alert');
      input.insertAdjacentElement('afterend', err);
    }
    err.textContent = message;
    input.setAttribute('aria-invalid', 'true');
    input.setAttribute('aria-describedby', errId);
    input.focus();
    const clear = () => {
      input.removeAttribute('aria-invalid');
      input.removeAttribute('aria-describedby');
      err.remove();
    };
    input.addEventListener('input', clear, { once: true });
    input.addEventListener('change', clear, { once: true });
  }
  window.arkFieldError = arkFieldError;

  function init() {
    ensureModal();
    document.querySelectorAll('.modal-backdrop').forEach(enhanceModal);
    document.querySelectorAll('[role="tablist"]').forEach(enhanceTablist);
    const alertBox = document.getElementById('ui-alert');
    if (alertBox) alertBox.setAttribute('role', 'alert');
    enhanceKpiCards();
    guardAsyncHandlers();
    markNav();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
