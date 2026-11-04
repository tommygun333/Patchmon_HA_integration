/* PatchMon Security Reports card — authenticated WebSocket data, no dependencies.
 * Modes: attention, viewer, host. Full report bodies never enter entity attributes.
 */
const html = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[ch]));
const dt = (value) => {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) :
    new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"}).format(date);
};
const good = new Set(["completed", "verified", "recovered_verified", "validated", "no_security_updates"]);
function statusText(value) { return String(value || "unknown").replaceAll("_", " "); }
function statusTag(value) { return `<span class="status ${good.has(value) ? "ok" : "attention"}">${html(statusText(value))}</span>`; }

class PatchMonSecurityReportCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
    this._cfg = {mode: "viewer", title: "Security maintenance", hide_when_read: true, limit: 26};
    this._reports = [];
    this._details = null;
    this._selectedId = null;
    this._hostId = null;
    this._loaded = false;
    this._loading = false;
    this._expanded = false;
    this._fingerprint = "";
  }
  static getStubConfig() { return {mode: "viewer", title: "Security maintenance"}; }
  getCardSize() { return this._cfg.mode === "attention" ? (this._expanded ? 8 : 1) : 8; }
  setConfig(config) {
    if (config.mode && !["viewer", "attention", "host"].includes(config.mode))
      throw new Error("PatchMon reports: mode must be viewer, attention, or host");
    this._cfg = {...this._cfg, ...config};
    this.render();
    if (this._hass) void this.refresh();
  }
  set hass(hass) {
    this._hass = hass;
    if (!hass || this._loading) return;
    const states = hass.states || {};
    const entity = this._cfg.unread_entity || Object.keys(states).find((id) =>
      id.startsWith("binary_sensor.") && id.includes("security_maintenance_report_unread"));
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
      const data = await this._hass.callWS({
        type: "patchmon/security_reports", config_entry_id: this._cfg.config_entry_id,
        host_id: this._cfg.host_id, limit: Math.min(26, Math.max(1, this._cfg.limit || 26)),
      });
      this._reports = data.reports || [];
      this._unread = Boolean(data.unread);
      this._active = data.active;
      this._error = null;
      this._loaded = true;
      const newest = this._reports[0]?.report_id;
      if (!this._selectedId || !this._reports.some((r) => r.report_id === this._selectedId))
        this._selectedId = newest;
      if ((this._cfg.mode !== "attention" || this._expanded) && this._selectedId)
        await this.loadDetail(this._selectedId, false);
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
      const data = await this._hass.callWS({type: "patchmon/security_reports",
        config_entry_id: this._cfg.config_entry_id, report_id: id});
      this._details = data.report;
      this._error = null;
    } catch (err) { this._error = String(err?.message || err); }
    if (redraw) this.render();
  }
  async acknowledge() {
    try {
      await this._hass.callWS({type: "patchmon/security_report_ack",
        config_entry_id: this._cfg.config_entry_id});
      this._unread = false;
      this.render();
    } catch (err) { this._error = `Could not mark read: ${String(err?.message || err)}`; this.render(); }
  }
  _rowReport(report) {
    const sum = report.summary || {};
    return `<button class="history-item ${report.report_id === this._selectedId ? "selected" : ""}"
      data-action="report" data-id="${html(report.report_id)}">
      <span><strong>${html(dt(report.started_at))}</strong><small>${html(report.mode || "install")}
      · ${sum.verified ?? 0} verified · ${sum.failed ?? 0} failed
      · ${sum.reboot_required ?? 0} reboot pending</small></span>${statusTag(report.status)}</button>`;
  }
  _hostRow(host) {
    const verification = host.verification || {};
    const run = host.installation || {};
    const selected = host.security_packages || [];
    const versions = verification.installed_versions_after || {};
    const open = this._hostId === host.host_id;
    return `<div class="host">
      <button class="host-title" data-action="host" data-id="${html(host.host_id)}">
        <span><strong>${html(host.hostname || host.host_id)}</strong>
        <small>${selected.length || host.security_count || 0} selected security packages
        · reboot ${verification.reboot_required || host.reboot_required ? "required" : "not required"}</small></span>
        ${statusTag(host.state)}</button>
      ${open ? `<div class="host-detail">
        <div class="meta">Validation: ${html(host.validation?.status || "not run")} · Installation: ${html(run.status || "not run")}
        · Host: ${html(verification.host_status || "—")}</div>
        <div class="meta">Validation run: <code>${html(host.validation?.run_id || "—")}</code></div>
        <div class="meta">Installation run: <code>${html(run.run_id || "—")}</code></div>
        <div class="meta">Pre-health: ${html(host.pre_health?.status || "not configured")}
          · Post-health: ${html(host.post_health?.status || "not configured")}</div>
        ${(host.error || run.error || verification.reason || host.validation?.reason || host.recovery_note) ? `<p class="warn">${html(host.error || run.error || verification.reason || host.validation?.reason || host.recovery_note)}</p>` : ""}
        ${(host.post_health?.failed_entities || []).length ? `<p class="warn">Unhealthy services: ${html(host.post_health.failed_entities.join(", "))}</p>` : ""}
        ${host.validation?.removals?.length ? `<p class="warn">Simulated removals: ${html(host.validation.removals.join(", "))}</p>` : ""}
        ${selected.length ? `<table><thead><tr><th>Package</th><th>Before</th><th>After</th></tr></thead><tbody>
          ${selected.map((pkg) => `<tr><td>${html(pkg.name)}</td><td>${html(pkg.installed_version || "—")}</td>
          <td>${html(versions[pkg.name] || (host.state === "verified" ? "Not reported" : "—"))}</td></tr>`).join("")}
        </tbody></table>` : `<div class="meta">No security packages selected</div>`}
        ${host.validation?.extra_dependencies?.length ? `<p>Validation dependencies: ${html(host.validation.extra_dependencies.join(", "))}</p>` : ""}
        ${run.packages_affected?.length ? `<div class="meta">Affected: ${html(run.packages_affected.map((p) => typeof p === "string" ? p : p.name).join(", "))}</div>` : ""}
        ${verification.remaining_selected_packages?.length ? `<p class="warn">Still pending: ${html(verification.remaining_selected_packages.join(", "))}</p>` : ""}
        </div>` : ""}</div>`;
  }
  render() {
    if (!this.shadowRoot) return;
    const attention = this._cfg.mode === "attention";
    if (attention && !this._expanded && this._cfg.hide_when_read && this._loaded && !this._unread && !this._active) {
      this.shadowRoot.innerHTML = "";
      return;
    }
    const summary = this._reports[0]?.summary || {};
    const compact = attention && !this._expanded;
    const active = this._active?.status ? `<p class="warn">Maintenance in progress / recovery: ${html(statusText(this._active.status))}</p>` : "";
    const body = compact ? `<button class="attention" data-action="open"><strong>🛡️ ${html(this._cfg.title)}</strong>
       <span>${html(this._reports.length ? dt(this._reports[0].started_at) : "No saved cycles")}
       · ${html(this._reports[0]?.status || "—")} · ${summary.failed || 0} errors
       · ${summary.reboot_required || 0} reboot pending</span><span>View report ›</span></button>` : `
      <div class="header"><strong>${html(this._cfg.title)}</strong><span class="actions">
      <button data-action="refresh">Refresh</button>
      ${this._unread ? `<button data-action="ack">Mark read</button>` : ""}
      ${attention ? `<button data-action="close">Close</button>` : ""}</span></div>
      ${active}<div class="layout">
      <aside><h4>History · newest first</h4>${this._reports.map((r) => this._rowReport(r)).join("") || '<p>No saved install cycles yet.</p>'}</aside>
      <section><h4>Cycle details</h4>${this._details ? this._reportDetail(this._details) : '<p>Select a cycle to view details.</p>'}</section></div>`;
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;color:var(--primary-text-color)} ha-card{display:block;padding:14px;border-radius:var(--ha-card-border-radius,12px);background:var(--ha-card-background,var(--card-background-color));box-shadow:var(--ha-card-box-shadow,none);border:1px solid var(--divider-color)}
      button{font:inherit;cursor:pointer;color:inherit;background:transparent;border:0}button:focus-visible{outline:2px solid var(--primary-color);outline-offset:2px}
      .attention,.host-title,.history-item{width:100%;display:flex;justify-content:space-between;align-items:center;text-align:left;gap:10px;padding:9px;border-radius:8px}.attention{flex-wrap:wrap}.attention:hover,.history-item:hover,.host-title:hover,.selected{background:var(--secondary-background-color)}
      .attention>span{font-size:12px;color:var(--secondary-text-color)}.header{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}.actions{display:flex;gap:5px}.actions button{font-size:12px;border:1px solid var(--divider-color);border-radius:6px;padding:5px 8px}.layout{display:grid;grid-template-columns:minmax(185px,30%) minmax(0,1fr);gap:12px;margin-top:12px}aside{border-right:1px solid var(--divider-color);padding-right:10px}h4{margin:0 0 7px;font-size:13px}.history-item{margin:0 0 3px}.history-item small,.host-title small{display:block;color:var(--secondary-text-color);font-size:11px;margin-top:3px}.status{display:inline-block;font-size:11px;white-space:nowrap;padding:3px 6px;border-radius:6px;background:var(--secondary-background-color);text-transform:capitalize}.status.ok{color:var(--success-color,#339970)}.status.attention{color:var(--error-color,#c75a55)}
      .host{border-bottom:1px solid var(--divider-color)}.host-detail{padding:8px 6px 12px}.meta{font-size:12px;color:var(--secondary-text-color);margin:6px 0;overflow-wrap:anywhere}.stats{font-size:12px;margin:6px 0 12px}.warn{font-size:12px;color:var(--error-color,#c75a55)}table{border-collapse:collapse;width:100%;font-size:12px;margin-top:6px}th,td{padding:6px;border-bottom:1px solid var(--divider-color);text-align:left;overflow-wrap:anywhere}code{overflow-wrap:anywhere} .error{margin:8px 0;color:var(--error-color,#c75a55)}
      @media(max-width:640px){.layout{display:block}aside{border:0;padding:0 0 8px;border-bottom:1px solid var(--divider-color);margin-bottom:10px}.history-item{padding:7px}.host-title{flex-wrap:wrap}}
    </style><ha-card>${body}${this._error ? `<p class="error">${html(this._error)}</p>` : ""}${this._loading ? '<small>Refreshing…</small>' : ""}</ha-card>`;
    this.shadowRoot.querySelectorAll("[data-action]").forEach((node) => {
      node.onclick = () => {
        const action = node.dataset.action;
        if (action === "refresh") void this.refresh();
        if (action === "ack") void this.acknowledge();
        if (action === "open") { this._expanded = true; this.render(); if (this._selectedId) void this.loadDetail(this._selectedId); }
        if (action === "close") { this._expanded = false; this.render(); }
        if (action === "report") void this.loadDetail(node.dataset.id);
        if (action === "host") { this._hostId = this._hostId === node.dataset.id ? null : node.dataset.id; this.render(); }
      };
    });
  }
  _reportDetail(report) {
    const s = report.summary || {};
    const pre = report.preflight || {};
    const blocked = pre.failures || [];
    return `<div class="stats"><strong>${html(dt(report.started_at))}</strong> · ${statusTag(report.status)}
      <p>${s.hosts ?? 0} hosts · ${s.verified ?? 0} verified · ${s.no_security_updates ?? 0} without updates
      · ${s.failed ?? 0} failures · ${s.reboot_required ?? 0} reboot pending</p>
      <p>Backup gate: ${html(pre.status || "not checked")}
      ${blocked.length ? ` — ${html(blocked.join("; "))}` : ""}</p>
      <div class="meta">Report ID: <code>${html(report.report_id)}</code></div></div>
      ${(report.hosts || []).map((host) => this._hostRow(host)).join("")}`;
  }
}
if (!customElements.get("patchmon-security-report-card"))
  customElements.define("patchmon-security-report-card", PatchMonSecurityReportCard);
window.customCards = window.customCards || [];
window.customCards.push({type: "patchmon-security-report-card", name: "PatchMon Security Reports",
  description: "Unread security-maintenance attention, fleet history, and per-host reports"});
