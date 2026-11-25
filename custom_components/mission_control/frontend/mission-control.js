/*
 * Mission Control — panel loader.
 *
 * The integration registers this file as the sidebar panel's module. It holds no
 * application logic: it imports app.js with a cache-busting query, so an updated
 * app.js is picked up on the next page load without a browser cache clear.
 */

const BASE = new URL('./', import.meta.url).pathname;
const BUST = Date.now();

class MissionControlPanel extends HTMLElement {
  constructor() {
    super();
    this._props = {};
    this._impl = null;
    this._loading = false;
  }

  _forward(k, v) {
    this._props[k] = v;
    if (this._impl) {
      try { this._impl[k] = v; } catch (e) { /* impl not ready for this prop */ }
    }
  }

  set hass(v) { this._forward('hass', v); }
  get hass() { return this._props.hass; }
  set narrow(v) { this._forward('narrow', v); }
  set panel(v) { this._forward('panel', v); }
  set route(v) { this._forward('route', v); }

  connectedCallback() {
    if (this._loading) return;
    this._loading = true;
    this._boot();
  }

  async _boot() {
    this.style.display = 'block';
    this.style.height = '100%';
    try {
      await import(`${BASE}app.js?v=${BUST}`);
      const el = document.createElement('mission-control-app');
      this.appendChild(el);
      this._impl = el;
      for (const k of Object.keys(this._props)) {
        try { el[k] = this._props[k]; } catch (e) { /* ignore */ }
      }
    } catch (err) {
      console.error('[mission-control] failed to load app.js', err);
      this.innerHTML =
        '<div style="padding:32px;font:14px/1.6 system-ui;color:#ff6b6b;background:#0e1013;height:100%">' +
        '<b>Mission Control failed to load.</b><br><br>' +
        '<code style="color:#9aa0a8">' + String(err && err.message ? err.message : err) + '</code><br><br>' +
        '<span style="color:#9aa0a8">Expected module at ' + BASE + 'app.js — check the file exists and the browser console for the parse error.</span>' +
        '</div>';
    }
  }
}

if (!customElements.get('mission-control-panel')) {
  customElements.define('mission-control-panel', MissionControlPanel);
}
