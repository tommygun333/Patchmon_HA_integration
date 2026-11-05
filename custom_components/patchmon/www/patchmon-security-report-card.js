/* PatchMon Reports — authenticated WebSocket viewer, native dialog and hidden panel. */
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[ch]));
const dt = (value) => {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) :
    new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"}).format(date);
};
const good = new Set([
  "completed", "verified", "recovered_verified", "validated",
  "no_security_updates", "no_updates"
]);
const statusText = (value) => String(value || "unknown").replaceAll("_", " ");
const statusTag = (value) => `<span class="status ${good.has(value) ? "ok" : "attention"}">${esc(statusText(value))}</span>`;
const sourceTag = (value) => `<span class="source">${esc(value || "automation")}</span>`;

function openPatchMonReportDialog(element, hass, params) {
  element.dispatchEvent(new CustomEvent("show-dialog", {
    bubbles: true,
    composed: true,
    detail: {
      dialogTag: "patchmon-security-report-dialog",
      dialogImport: async () => undefined,
      dialogParams: {...params, hass},
    },
  }));
}

class PatchMonSecurityReportCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
    this._cfg = {mode: "viewer", title: "PatchMon reports", limit: 26};
    this._reports = [];
    this._details = null;
    this._selectedId = null;
    this._hostId = null;
    this._loaded = false;
    this._loading = false;
    this._expanded = false;
    this._fingerprint = "";
  }

  static getStubConfig() { return {mode: "viewer", title: "PatchMon reports"}; }

  getCardSize() {
    if (this._cfg.display === "dialog" || this._cfg.dialog === true) return 1;
    return this._cfg.mode === "attention" ? (this._expanded ? 8 : 1) : 8;
  }

  setConfig(config) {
    if (config.mode && !["viewer", "attention", "host"].includes(config.mode)) {
      throw new Error("PatchMon reports: mode must be viewer, attention, or host");
    }
    const reportSource = config.report_source ??
      ((config.mode || this._cfg.mode) === "attention" ? "automation" : undefined);
    this._cfg = {...this._cfg, ...config, report_source: reportSource};
    this.render();
    if (this._hass) void this.refresh();
  }

  set hass(hass) {
    this._hass = hass;
    if (!hass || this._loading) return;
    const states = hass.states || {};
    const configured = this._cfg.unread_entity;
    const entity = configured || Object.keys(states).find((id) => {
      const state = states[id];
      if (!id.startsWith("binary_sensor.")) return false;
      return this._cfg.report_source === "automation"
        ? state?.attributes?.report_source === "automation"
        : id.includes("security_maintenance_report_unread");
    });
    const status = entity ? states[entity] : null;
    const stamp = `${status?.state || ""}:${status?.attributes?.report_id || ""}`;
    if (!this._loaded || (status && stamp !== this._fingerprint)) {
      this._fingerprint = stamp;
      void this.refresh();
    }
  }

  async refresh() {
    if (!this._hass || this._loading) return;
    this._loading = true;
    this.render();
    try {
      const message = {
        type: "patchmon/security_reports",
        config_entry_id: this._cfg.config_entry_id,
        host_id: this._cfg.host_id,
        limit: Math.min(26, Math.max(1, this._cfg.limit || 26)),
      };
      if (this._cfg.report_source) message.report_source = this._cfg.report_source;
      const data = await this._hass.callWS(message);
      this._reports = data.reports || [];
      this._unread = this._cfg.report_source === "automation"
        ? Boolean(data.automation_unread) : Boolean(data.unread);
      this._active = data.active;
      this._error = null;
      this._loaded = true;
      const requested = this._cfg.report_id;
      const newest = this._reports[0]?.report_id;
      if (requested) {
        this._selectedId = requested;
      } else if (!this._selectedId || !this._reports.some((report) => report.report_id === this._selectedId)) {
        this._selectedId = newest;
      }
      const launcher = this._cfg.display === "dialog" || this._cfg.dialog === true;
      if (!launcher && (this._cfg.mode !== "attention" || this._expanded) && this._selectedId) {
        await this.loadDetail(this._selectedId, false);
      }
    } catch (err) {
      this._error = String(err?.message || err);
    } finally {
      this._loading = false;
      this.render();
    }
  }

  async loadDetail(id, redraw = true) {
    this._selectedId = id;
    this._details = null;
    try {
      const data = await this._hass.callWS({
        type: "patchmon/security_reports",
        config_entry_id: this._cfg.config_entry_id,
        report_id: id,
      });
      this._details = data.report;
      this._error = null;
    } catch (err) {
      this._error = String(err?.message || err);
    }
    if (redraw) this.render();
  }

  async acknowledge() {
    try {
      const type = this._cfg.report_source === "automation"
        ? "patchmon/automation_security_report_ack" : "patchmon/security_report_ack";
      await this._hass.callWS({
        type,
        config_entry_id: this._cfg.config_entry_id,
      });
      this._unread = false;
      this.render();
    } catch (err) {
      this._error = `Could not mark read: ${String(err?.message || err)}`;
      this.render();
    }
  }

  _openDialog(reportId = this._selectedId || this._reports[0]?.report_id) {
    openPatchMonReportDialog(this, this._hass, {
      config_entry_id: this._cfg.config_entry_id,
      report_id: reportId,
      host_id: this._cfg.host_id,
      report_source: this._cfg.report_source,
      title: this._cfg.title || "PatchMon report",
    });
  }

  _rowReport(report) {
    const sum = report.summary || {};
    return `<button class="history-item ${report.report_id === this._selectedId ? "selected" : ""}"
      data-action="report" data-id="${esc(report.report_id)}">
      <span><strong>${esc(dt(report.started_at))}</strong><small>${sourceTag(report.report_source)}
      ${esc(report.mode || report.report_type || "report")} · ${sum.validated ?? sum.verified ?? 0} successful
      · ${sum.failed ?? 0} failed · ${sum.reboot_required ?? 0} reboot pending</small></span>
      ${statusTag(report.status)}</button>`;
  }

  _hostRow(host) {
    const verification = host.verification || {};
    const validation = host.validation || {};
    const run = host.installation || {};
    const selected = host.security_packages || [];
    const versions = verification.installed_versions_after || {};
    const open = this._hostId === host.host_id;
    return `<div class="host">
      <button class="host-title" data-action="host" data-id="${esc(host.host_id)}">
        <span><strong>${esc(host.hostname || host.host_id)}</strong>
        <small>${selected.length || host.selected_count || host.security_count || 0} selected packages
        · reboot ${verification.reboot_required || host.reboot_required ? "required" : "not required"}</small></span>
        ${statusTag(host.state)}</button>
      ${open ? `<div class="host-detail">
        <div class="meta">Validation: ${esc(validation.status || "not run")} · Installation: ${esc(run.status || "not run")}
        · Host: ${esc(verification.host_status || "—")}</div>
        <div class="meta">Host UUID: <code>${esc(host.host_id)}</code></div>
        <div class="meta">Validation run: <code>${esc(validation.run_id || "—")}</code></div>
        <div class="meta">Installation run: <code>${esc(run.run_id || "—")}</code></div>
        <div class="meta">Pre-health: ${esc(host.pre_health?.status || "not configured")}
          · Post-health: ${esc(host.post_health?.status || "not configured")}</div>
        ${(host.error || run.error || verification.reason || validation.reason || host.recovery_note)
          ? `<p class="warn">${esc(host.error || run.error || verification.reason || validation.reason || host.recovery_note)}</p>` : ""}
        ${(host.post_health?.failed_entities || []).length
          ? `<p class="warn">Unhealthy services: ${esc(host.post_health.failed_entities.join(", "))}</p>` : ""}
        ${validation.removals?.length
          ? `<p class="warn">Simulated removals: ${esc(validation.removals.join(", "))}</p>` : ""}
        ${selected.length ? `<table><thead><tr><th>Package</th><th>Before</th><th>After/candidate</th></tr></thead><tbody>
          ${selected.map((pkg) => `<tr><td>${esc(pkg.name)}</td><td>${esc(pkg.installed_version || "—")}</td>
          <td>${esc(versions[pkg.name] || pkg.available_version || "—")}</td></tr>`).join("")}
        </tbody></table>` : `<div class="meta">No packages selected</div>`}
        ${validation.extra_dependencies?.length
          ? `<p>Validation dependencies: ${esc(validation.extra_dependencies.join(", "))}</p>` : ""}
        ${run.packages_affected?.length
          ? `<div class="meta">Affected: ${esc(run.packages_affected.map((pkg) => typeof pkg === "string" ? pkg : pkg.name).join(", "))}</div>` : ""}
        ${verification.remaining_selected_packages?.length
          ? `<p class="warn">Still pending: ${esc(verification.remaining_selected_packages.join(", "))}</p>` : ""}
        ${validation.console_output_tail
          ? `<details><summary>Validation console tail</summary><pre>${esc(validation.console_output_tail)}</pre></details>` : ""}
        </div>` : ""}</div>`;
  }

  _reportDetail(report) {
    const summary = report.summary || {};
    const preflight = report.preflight || {};
    const blocked = preflight.failures || [];
    return `<div class="stats"><strong>${esc(dt(report.started_at))}</strong>
      · ${sourceTag(report.report_source)} · ${statusTag(report.status)}
      <p>${summary.hosts ?? 0} hosts · ${summary.verified ?? 0} verified
      · ${summary.validated ?? 0} validated · ${summary.no_security_updates ?? 0} without updates
      · ${summary.failed ?? 0} failures · ${summary.reboot_required ?? 0} reboot pending</p>
      ${report.report_type === "security_maintenance" ? `<p>Backup gate: ${esc(preflight.status || "not checked")}
        ${blocked.length ? ` — ${esc(blocked.join("; "))}` : ""}</p>` : ""}
      <div class="meta">Report ID: <code>${esc(report.report_id)}</code></div></div>
      ${(report.hosts || []).map((host) => this._hostRow(host)).join("")}`;
  }

  render() {
    if (!this.shadowRoot) return;
    const attention = this._cfg.mode === "attention";
    const launcher = this._cfg.display === "dialog" || this._cfg.dialog === true;
    const newest = this._reports[0];
    const summary = newest?.summary || {};
    let body;
    if (launcher) {
      body = `<button class="attention" data-action="dialog"><strong>🛡️ ${esc(this._cfg.title)}</strong>
        <span>${esc(newest ? dt(newest.started_at) : "No saved reports")} · ${esc(newest?.status || "—")}
        · ${summary.failed || 0} errors</span><span>View report ›</span></button>`;
    } else if (attention && !this._expanded) {
      body = `<button class="attention" data-action="open"><strong>🛡️ ${esc(this._cfg.title)}</strong>
        <span>${esc(newest ? dt(newest.started_at) : "No saved reports")} · ${esc(newest?.status || "—")}
        · ${summary.failed || 0} errors · ${summary.reboot_required || 0} reboot pending</span>
        <span>View report ›</span></button>`;
    } else {
      const active = this._active?.status
        ? `<p class="warn">Maintenance in progress / recovery: ${esc(statusText(this._active.status))}</p>` : "";
      body = `<div class="header"><strong>${esc(this._cfg.title)}</strong><span class="actions">
        <button data-action="refresh">Refresh</button>
        ${this._unread ? `<button data-action="ack">Mark read</button>` : ""}
        ${attention ? `<button data-action="close">Close</button>` : ""}</span></div>
        ${active}<div class="layout"><aside><h4>History · newest first</h4>
        ${this._reports.map((report) => this._rowReport(report)).join("") || "<p>No saved reports yet.</p>"}</aside>
        <section><h4>Report details</h4>${this._details ? this._reportDetail(this._details) : "<p>Select a report to view details.</p>"}</section></div>`;
    }
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;color:var(--primary-text-color)}ha-card{display:block;padding:14px;border-radius:var(--ha-card-border-radius,12px);background:var(--ha-card-background,var(--card-background-color));box-shadow:var(--ha-card-box-shadow,none);border:1px solid var(--divider-color)}
      button{font:inherit;cursor:pointer;color:inherit;background:transparent;border:0}button:focus-visible{outline:2px solid var(--primary-color);outline-offset:2px}.attention,.host-title,.history-item{width:100%;display:flex;justify-content:space-between;align-items:center;text-align:left;gap:10px;padding:9px;border-radius:8px}.attention{flex-wrap:wrap}.attention:hover,.history-item:hover,.host-title:hover,.selected{background:var(--secondary-background-color)}
      .attention>span{font-size:12px;color:var(--secondary-text-color)}.header{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}.actions{display:flex;gap:5px}.actions button{font-size:12px;border:1px solid var(--divider-color);border-radius:6px;padding:5px 8px}.layout{display:grid;grid-template-columns:minmax(210px,30%) minmax(0,1fr);gap:12px;margin-top:12px}aside{border-right:1px solid var(--divider-color);padding-right:10px}h4{margin:0 0 7px;font-size:13px}.history-item{margin:0 0 3px}.history-item small,.host-title small{display:block;color:var(--secondary-text-color);font-size:11px;margin-top:3px}.status,.source{display:inline-block;font-size:11px;white-space:nowrap;padding:3px 6px;border-radius:6px;background:var(--secondary-background-color);text-transform:capitalize}.source{margin-right:4px;color:var(--secondary-text-color)}.status.ok{color:var(--success-color,#339970)}.status.attention{color:var(--error-color,#c75a55)}
      .host{border-bottom:1px solid var(--divider-color)}.host-detail{padding:8px 6px 12px}.meta{font-size:12px;color:var(--secondary-text-color);margin:6px 0;overflow-wrap:anywhere}.stats{font-size:12px;margin:6px 0 12px}.warn,.error{font-size:12px;color:var(--error-color,#c75a55)}table{border-collapse:collapse;width:100%;font-size:12px;margin-top:6px}th,td{padding:6px;border-bottom:1px solid var(--divider-color);text-align:left;overflow-wrap:anywhere}code{overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:240px;overflow:auto;padding:8px;background:var(--secondary-background-color);border-radius:6px;font-size:11px}details{margin-top:8px}summary{cursor:pointer;font-size:12px}
      @media(max-width:640px){.layout{display:block}aside{border:0;padding:0 0 8px;border-bottom:1px solid var(--divider-color);margin-bottom:10px}.history-item{padding:7px}.host-title{flex-wrap:wrap}}
    </style><ha-card>${body}${this._error ? `<p class="error">${esc(this._error)}</p>` : ""}${this._loading ? "<small>Refreshing…</small>" : ""}</ha-card>`;
    this.shadowRoot.querySelectorAll("[data-action]").forEach((node) => {
      node.onclick = () => {
        const action = node.dataset.action;
        if (action === "refresh") void this.refresh();
        if (action === "ack") void this.acknowledge();
        if (action === "open") { this._expanded = true; this.render(); if (this._selectedId) void this.loadDetail(this._selectedId); }
        if (action === "close") { this._expanded = false; this.render(); }
        if (action === "dialog") this._openDialog();
        if (action === "report") void this.loadDetail(node.dataset.id);
        if (action === "host") { this._hostId = this._hostId === node.dataset.id ? null : node.dataset.id; this.render(); }
      };
    });
  }
}

class PatchMonSecurityReportDialog extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
  }

  set hass(hass) { this._hass = hass; }

  showDialog(params) {
    this._params = params;
    this._hass = params.hass || this._hass;
    this._render();
  }

  closeDialog() {
    const dialog = this.shadowRoot?.querySelector("ha-dialog");
    if (dialog) dialog.open = false;
    return true;
  }

  _render() {
    if (!this._params || !this._hass) return;
    this.shadowRoot.innerHTML = `<style>
      ha-dialog{--dialog-content-padding:0}.content{width:min(1000px,88vw);max-height:78vh;overflow:auto;padding:12px;box-sizing:border-box}
      @media(max-width:640px){.content{width:94vw;max-height:82vh;padding:6px}}
    </style><ha-dialog open width="large" header-title="${esc(this._params.title || "PatchMon report")}">
      <div class="content"></div><ha-button slot="primaryAction">Close</ha-button></ha-dialog>`;
    const dialog = this.shadowRoot.querySelector("ha-dialog");
    const close = this.shadowRoot.querySelector("ha-button");
    close.onclick = () => { dialog.open = false; };
    dialog.addEventListener("closed", () => {
      this.dispatchEvent(new CustomEvent("dialog-closed", {
        bubbles: true, composed: true, detail: {dialog: this.localName},
      }));
    }, {once: true});
    const card = document.createElement("patchmon-security-report-card");
    card.setConfig({
      mode: "viewer",
      title: this._params.title || "PatchMon report",
      config_entry_id: this._params.config_entry_id,
      report_id: this._params.report_id,
      host_id: this._params.host_id,
      report_source: this._params.report_source,
      limit: 26,
    });
    card.hass = this._hass;
    this.shadowRoot.querySelector(".content").appendChild(card);
  }
}

class PatchMonSecurityReportPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
  }
  set hass(value) { this._hass = value; this._render(); }
  set route(value) { this._route = value; this._render(); }
  set panel(value) { this._panel = value; this._render(); }
  set narrow(value) { this._narrow = value; this._render(); }

  _render() {
    if (!this._hass || !this._route) return;
    const parts = String(this._route.path || "").split("/").filter(Boolean).map(decodeURIComponent);
    const [configEntryId, reportId] = parts;
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;min-height:100%;background:var(--primary-background-color);color:var(--primary-text-color)}
      header{display:flex;align-items:center;gap:12px;height:64px;padding:0 20px;background:var(--app-header-background-color,var(--primary-color));color:var(--app-header-text-color,#fff);box-sizing:border-box}h1{font-size:20px;margin:0}.page{max-width:1200px;margin:0 auto;padding:16px;box-sizing:border-box}.error{color:var(--error-color);padding:20px}
      @media(max-width:640px){header{height:56px;padding:0 12px}.page{padding:8px}}
    </style><header><ha-icon icon="mdi:shield-text-outline"></ha-icon><h1>PatchMon report</h1></header><main class="page"></main>`;
    const main = this.shadowRoot.querySelector("main");
    if (!configEntryId || !reportId) {
      main.innerHTML = "<p class=\"error\">The report link is incomplete.</p>";
      return;
    }
    const card = document.createElement("patchmon-security-report-card");
    card.setConfig({
      mode: "viewer", title: "PatchMon report", config_entry_id: configEntryId,
      report_id: reportId, limit: 26,
    });
    card.hass = this._hass;
    main.appendChild(card);
  }
}

if (!customElements.get("patchmon-security-report-card")) {
  customElements.define("patchmon-security-report-card", PatchMonSecurityReportCard);
}
if (!customElements.get("patchmon-security-report-dialog")) {
  customElements.define("patchmon-security-report-dialog", PatchMonSecurityReportDialog);
}
if (!customElements.get("patchmon-security-report-panel")) {
  customElements.define("patchmon-security-report-panel", PatchMonSecurityReportPanel);
}
window.customCards = window.customCards || [];
window.customCards.push({
  type: "patchmon-security-report-card",
  name: "PatchMon Reports",
  description: "Automatic/manual history, native per-host report dialogs, and unread attention",
});
