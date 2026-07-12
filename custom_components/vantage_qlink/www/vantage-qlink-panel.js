/* Vantage QLink console panel — native replacement for the standalone
   vantage-qlink-api web UI, sharing the integration's controller socket. */

const COMMANDS = [
  ["VCL", "Set return delimiter/format for RS-232", "<flag>"],
  ["VEC", "Echo a number (comm test)", "<number>"],
  ["VET", "Execute a time function", "<master> <function#> <state>"],
  ["VGA", "Get load level (master/module/load)", "<master> <module> <load>"],
  ["VGB", "Get load level (m/enclosure/module/load)", "<m> <e> <mod> <load>"],
  ["VGC", "Get station-bus load level (LVRS/wallbox)", "<master> <station> <load>"],
  ["VGD", "Get all 8 load levels of a module", "<master> <enclosure> <module>"],
  ["VGH", "Read thermostat registers (Q-ETS)", "<master> <station> <type>..."],
  ["VGL", "Get load level by contractor number", "<contractor #>"],
  ["VGN", "Get name of master/station/load", "<master> <address> <pos>"],
  ["VGS", "Get switch function state", "<master> <station> <switch>"],
  ["VGT", "Get all 10 switch states of a station", "<master> <station>"],
  ["VGV", "Get master version + date/time", "<master>"],
  ["VIC", "Output IR code (cflag form)", "<m> <cflag> <code> <state> <emitter>"],
  ["VIR", "Output IR code", "<m> <code> <state> <emitter>"],
  ["VLA", "Set load level (master/module/load)", "<m> <mod> <load> <level> {<fade>}"],
  ["VLB", "Set load level (m/e/mod/load)", "<m> <e> <mod> <load> <level> {<fade>}"],
  ["VLC", "Set station-bus load level", "<m> <station> <load> <level> {<fade>}"],
  ["VLD", "Set a switch LED state", "<master> <station> <led> <state>"],
  ["VLO", "Set load level by contractor number", "<con_num> <level> {<fade>}"],
  ["VLP", "Change module load profile", "<m> <e> <mod> <load> <profile>"],
  ["VLS", "Get a switch LED state", "<master> <station> <led>"],
  ["VLT", "Get all LED states of a station", "<master> <station> <format>"],
  ["VOD", "Enable/disable LED-change reporting", "<enable 0-3>"],
  ["VOL", "Enable/disable load-change reporting", "<enable>"],
  ["VOS", "Enable/disable switch-press reporting", "<format> <enable>"],
  ["VPG", "Program via .qlk-format string", "<string>"],
  ["VQA", "Which master/port is this connection on", ""],
  ["VQM", "List master controllers", ""],
  ["VQP", "List modules of a master", "<master>"],
  ["VQS", "List stations of a master", "<master>"],
  ["VQT", "Get time function parameters", "<master> <function#>"],
  ["VSC", "Configure a station", "<master> <station> <serialno>"],
  ["VSH", "Set thermostat parameters", "<m> <station> <type> <temp>..."],
  ["VSP", "Change station load profile", "<master> <station> <load> <profile>"],
  ["VST", "Set time function parameters", "<m> <function#> <state> {...}"],
  ["VSW", "Execute a switch function", "<master> <station> <switch> <state>"],
  ["V?S", "Set dimmer station max adjust/error", "<m> <addr> <97> <adj> <err>"],
  ["V?V", "Vacation load history", "<master>"],
  ["V?D", "Vacation load history for a day", "<master> <day> <limit>"],
];

class VantageQlinkPanel extends HTMLElement {
  constructor() {
    super();
    this._built = false;
    this._paused = false;
    this._history = [];
    this._pollBusy = false;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) {
      this._build();
      this._built = true;
      this._poll();
    }
  }

  connectedCallback() {
    this._timer = setInterval(() => this._poll(), 2000);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
  }

  async _poll() {
    if (!this._hass || this._paused || this._pollBusy || !this._built) return;
    this._pollBusy = true;
    try {
      const d = await this._hass.connection.sendMessagePromise({
        type: "vantage_qlink/panel_data",
      });
      this._update(d);
    } catch (e) {
      /* transient (restart etc.) */
    } finally {
      this._pollBusy = false;
    }
  }

  async _send() {
    const input = this.querySelector("#vq-cmd");
    let cmd = input.value.trim();
    if (!cmd) return;
    if (this.querySelector("#vq-detail").checked) {
      const parts = cmd.split(/\s+/);
      if (!/[#$]$/.test(parts[0])) parts[0] += "#";
      cmd = parts.join(" ");
    }
    const out = this.querySelector("#vq-response");
    this._history.unshift(cmd);
    this._history = this._history.slice(0, 20);
    out.textContent = `> ${cmd}\n…`;
    try {
      const res = await this._hass.connection.sendMessagePromise({
        type: "call_service",
        domain: "vantage_qlink",
        service: "send_command",
        service_data: { command: cmd },
        return_response: true,
      });
      const lines = (res && res.response && res.response.response) || [];
      out.textContent = `> ${cmd}\n${lines.join("\n") || "(no reply)"}`;
    } catch (e) {
      out.textContent = `> ${cmd}\nERROR: ${e.message || e}`;
    }
  }

  _update(d) {
    const chip = this.querySelector("#vq-chip");
    const meta = this.querySelector("#vq-meta");
    if (!d.loaded) {
      chip.className = "chip off";
      chip.textContent = "integration not loaded";
      return;
    }
    const c = d.connection;
    chip.className = "chip " + (c.connected ? "on" : "off");
    chip.textContent = c.connected ? "connected" : "disconnected";
    meta.textContent =
      `${c.host}:${c.port} · gap ${c.send_gap_ms}ms · ` +
      `VOS ${c.push_switches ? "on" : "off"} · VOL ${c.push_loads ? "on" : "off"} · ` +
      `${d.load_count} loads · ${Object.keys(d.learned_map || {}).length} mapped`;

    const traffic = this.querySelector("#vq-traffic");
    const atBottom =
      traffic.scrollHeight - traffic.scrollTop - traffic.clientHeight < 30;
    traffic.innerHTML = (d.traffic || [])
      .map((l) => {
        const cls = l.startsWith("TX") ? "tx" : "rx";
        return `<div class="${cls}">${this._esc(l)}</div>`;
      })
      .join("");
    if (atBottom) traffic.scrollTop = traffic.scrollHeight;

    if (d.discovery && d.discovery.stations && !this._discoveryDone) {
      this._discoveryDone = true;
      this._renderDiscovery(d.discovery);
    }
  }

  _renderDiscovery(disc) {
    const el = this.querySelector("#vq-discovery");
    const masters = (disc.masters || []).join(", ") || "—";
    const modules = (disc.modules || []).length;
    const rows = (disc.stations || [])
      .slice()
      .sort((a, b) => a.master - b.master || a.station - b.station)
      .map((s) => {
        const nm = (s.name || "").split("|");
        return `<tr>
          <td>${s.master}-${s.station}</td>
          <td>${this._esc(nm[0] || "")}</td>
          <td>${this._esc(nm[1] || "")}</td>
          <td>${this._esc(s.type_name || "")}</td>
          <td>${(s.programmed_switches || []).join(" ") || "—"}</td>
        </tr>`;
      })
      .join("");
    el.innerHTML = `
      <div class="hint">masters: ${masters} · ${modules} modules · ${(disc.stations || []).length} stations</div>
      <table>
        <thead><tr><th>m-s</th><th>name</th><th>room</th><th>type</th><th>buttons</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  _esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
  }

  _build() {
    this.innerHTML = `
      <style>
        vantage-qlink-panel { display: block; }
        .vq-wrap { padding: 16px; max-width: 1400px; margin: 0 auto;
          color: var(--primary-text-color); font-family: var(--paper-font-body1_-_font-family, sans-serif); }
        .vq-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
        .vq-head h1 { font-size: 20px; font-weight: 400; margin: 0; }
        .chip { padding: 2px 10px; border-radius: 10px; font-size: 12px; }
        .chip.on { background: var(--success-color, #0f9d58); color: #fff; }
        .chip.off { background: var(--error-color, #db4437); color: #fff; }
        #vq-meta { color: var(--secondary-text-color); font-size: 12px; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }
        .card { background: var(--card-background-color); border-radius: 12px;
          box-shadow: var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.25));
          border: 1px solid var(--divider-color); padding: 12px 14px; }
        .card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .5px;
          color: var(--secondary-text-color); margin: 0 0 8px; font-weight: 500; }
        .row { display: flex; gap: 8px; align-items: center; }
        #vq-cmd { flex: 1; padding: 8px 10px; border-radius: 8px;
          border: 1px solid var(--divider-color); background: var(--primary-background-color);
          color: var(--primary-text-color); font-family: monospace; }
        label.det { font-size: 12px; color: var(--secondary-text-color); white-space: nowrap; }
        button.vq { border: none; border-radius: 8px; padding: 8px 16px; cursor: pointer;
          background: var(--primary-color); color: var(--text-primary-color, #fff); }
        button.ghost { background: transparent; color: var(--primary-color);
          border: 1px solid var(--primary-color); }
        pre#vq-response { background: var(--primary-background-color); border-radius: 8px;
          padding: 10px; min-height: 48px; font-size: 12px; white-space: pre-wrap;
          border: 1px solid var(--divider-color); }
        #vq-traffic { height: 320px; overflow: auto; background: var(--primary-background-color);
          border: 1px solid var(--divider-color); border-radius: 8px; padding: 8px;
          font-family: monospace; font-size: 11.5px; line-height: 1.5; }
        #vq-traffic .tx { color: var(--primary-color); }
        #vq-traffic .rx { color: var(--secondary-text-color); }
        table { width: 100%; border-collapse: collapse; font-size: 12px; }
        th { text-align: left; color: var(--secondary-text-color); font-weight: 500;
          border-bottom: 1px solid var(--divider-color); padding: 4px 6px; }
        td { padding: 3px 6px; border-bottom: 1px solid var(--divider-color); }
        tbody tr:hover { background: var(--secondary-background-color); cursor: pointer; }
        .scroll { max-height: 300px; overflow: auto; }
        .hint { color: var(--secondary-text-color); font-size: 12px; margin-bottom: 6px; }
        details summary { cursor: pointer; color: var(--primary-color); font-size: 13px; margin: 4px 0; }
      </style>
      <div class="vq-wrap">
        <div class="vq-head">
          <h1>Vantage QLink console</h1>
          <span id="vq-chip" class="chip off">…</span>
          <span id="vq-meta"></span>
        </div>
        <div class="grid">
          <div class="card">
            <h2>Send command</h2>
            <div class="row">
              <input id="vq-cmd" placeholder="e.g. VGL 1005 — click a command below to fill" spellcheck="false"/>
              <label class="det"><input type="checkbox" id="vq-detail" checked/> # detailed</label>
              <button class="vq" id="vq-send">Send</button>
            </div>
            <pre id="vq-response">Ready. Commands go through the integration's connection — no port conflicts.</pre>
            <details>
              <summary>Command reference (40)</summary>
              <div class="scroll"><table>
                <thead><tr><th>cmd</th><th>description</th><th>params</th></tr></thead>
                <tbody id="vq-cmds"></tbody>
              </table></div>
            </details>
          </div>
          <div class="card">
            <h2>Live traffic <button class="ghost vq" id="vq-pause" style="float:right;padding:2px 10px;font-size:11px">pause</button></h2>
            <div id="vq-traffic"></div>
          </div>
          <div class="card" style="grid-column: 1 / -1;">
            <h2>Discovered system</h2>
            <div id="vq-discovery" class="scroll"><span class="hint">discovery pending…</span></div>
          </div>
        </div>
      </div>`;

    const tbody = this.querySelector("#vq-cmds");
    tbody.innerHTML = COMMANDS.map(
      ([c, d, p]) =>
        `<tr data-c="${c}"><td><code>${c}</code></td><td>${d}</td><td><code>${this._esc(
          p
        )}</code></td></tr>`
    ).join("");
    tbody.addEventListener("click", (ev) => {
      const tr = ev.target.closest("tr[data-c]");
      if (!tr) return;
      const input = this.querySelector("#vq-cmd");
      input.value = tr.dataset.c + " ";
      input.focus();
    });

    this.querySelector("#vq-send").addEventListener("click", () => this._send());
    this.querySelector("#vq-cmd").addEventListener("keydown", (e) => {
      if (e.key === "Enter") this._send();
    });
    const pauseBtn = this.querySelector("#vq-pause");
    pauseBtn.addEventListener("click", () => {
      this._paused = !this._paused;
      pauseBtn.textContent = this._paused ? "resume" : "pause";
    });
  }
}

customElements.define("vantage-qlink-panel", VantageQlinkPanel);
