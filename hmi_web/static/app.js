/* CP-01 web HMI: WebSocket client, plant schematic, controls, trends. */
"use strict";

const $ = (id) => document.getElementById(id);
const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v) ? "--" : Number(v).toFixed(d));
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

const app = {
  ws: null, wsOk: false, retry: 1000, msgId: 0, pending: new Map(),
  keys: [], hist: [], events: [], last: null, snap: null,
  sp: { dirty: false, value: 6.7 }, dl: { dirty: false, value: 100 },
  spRange: [5, 12], dlRange: [10, 100], range: 900, evFilter: "all",
  anim: { comp: 0, pump: 0, fan: 0, dash: {} },
};

/* ------------------------------------------------------------------ WS */
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  app.ws = ws;
  ws.onopen = () => { app.wsOk = true; app.retry = 1000; updateLinks(); };
  ws.onclose = () => {
    app.wsOk = false; updateLinks();
    setTimeout(connect, app.retry);
    app.retry = Math.min(10000, app.retry * 1.6);
  };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "hello") onHello(m);
    else if (m.type === "status") onStatus(m);
    else if (m.type === "event") addEvent(m.event, true);
    else if (m.type === "ack") onAck(m);
  };
}

function send(cmd, value, extra = {}) {
  return new Promise((resolve, reject) => {
    if (!app.wsOk) { toast("Not connected", true); return reject(new Error("offline")); }
    const id = ++app.msgId;
    app.pending.set(id, { resolve, reject, cmd });
    app.ws.send(JSON.stringify({ id, cmd, value, ...extra }));
  });
}

function onAck(m) {
  const p = app.pending.get(m.id);
  if (!p) { if (!m.ok) toast(m.error, true); return; }
  app.pending.delete(m.id);
  if (m.ok) { p.resolve(); } else { toast(`${p.cmd}: ${m.error}`, true); p.reject(new Error(m.error)); }
}

function onHello(m) {
  app.keys = m.trend_keys;
  app.hist = m.history;
  app.spRange = m.sp_range; app.dlRange = m.demand_range;
  $("sp-range").min = app.spRange[0]; $("sp-range").max = app.spRange[1];
  $("dl-range").min = app.dlRange[0]; $("dl-range").max = app.dlRange[1];
  $("ev-list").innerHTML = ""; app.events = [];
  m.events.forEach((ev) => addEvent(ev, false));
  charts.forEach((c) => c.draw());
}

/* -------------------------------------------------------------- status */
function onStatus(m) {
  app.snap = m;
  const p = m.plc;
  updateLinks();
  if (!p) return;
  app.last = p;

  // trend sample at 1 Hz (server keeps the same cadence for history)
  const lastT = app.hist.length ? app.hist[app.hist.length - 1][0] : 0;
  if (m.link.s7 && m.t - lastT >= 1) {
    app.hist.push([m.t, ...app.keys.map((k) => p[k])]);
    if (app.hist.length > 3600) app.hist.shift();
  }

  renderSchematic(p, m);
  renderControls(p);
  renderKpis(p);
  renderAlarm(p, m);
  renderSequence(p);
  renderFaults(m);
  charts.forEach((c) => c.draw());
}

function updateLinks() {
  const m = app.snap;
  const set = (id, ok) => { $(id).classList.toggle("ok", !!ok); $(id).classList.toggle("bad", !ok); };
  set("link-ws", app.wsOk);
  const s7 = app.wsOk && m && m.link.s7;
  set("link-s7", s7);
  set("link-plc", s7 && m.link.plc_alive);
  if (m) {
    $("link-s7-text").textContent = `S7 ${m.link.host} R${m.link.rack}/S${m.link.slot}`;
    if (m.plc) $("hb").textContent = String(m.plc.heartbeat).padStart(5, "0");
  }
  const off = !app.wsOk || !s7;
  $("offline").classList.toggle("on", off);
  document.body.classList.toggle("readonly", off);
  $("offline-text").textContent = !app.wsOk
    ? "HMI server unreachable, reconnecting…"
    : `No S7 connection to PLC ${m ? m.link.host : ""}, retrying…`;
}

/* ----------------------------------------------------------- schematic */
function buildWindows() {
  const g = $("windows");
  const ns = "http://www.w3.org/2000/svg";
  for (let r = 0; r < 11; r++) {
    for (let c = 0; c < 3; c++) {
      const w = document.createElementNS(ns, "rect");
      w.setAttribute("x", 12 + c * 20); w.setAttribute("y", 36 + r * 20);
      w.setAttribute("width", 14); w.setAttribute("height", 11); w.setAttribute("rx", 1.5);
      w.dataset.rank = String(((r * 7 + c * 13) % 33) / 33);
      g.appendChild(w);
    }
  }
}

function renderSchematic(p, m) {
  $("t-chws").textContent = `${fmt(p.chws, 2)}°C`;
  $("t-chwr").textContent = `${fmt(p.chwr, 2)}°C`;
  $("t-cwe").textContent = `${fmt(p.cwe)}°C`;
  $("t-cwl").textContent = `${fmt(p.cwl)}°C`;
  $("t-flow").textContent = `${fmt(p.flow)} l/s`;
  $("t-sp").textContent = `${fmt(p.active_sp)}°C`;
  $("t-oat").textContent = `${fmt(p.oat)}°C`;
  $("v-condp").textContent = `${fmt(p.cond_p, 0)} kPa`;
  $("v-evapp").textContent = `${fmt(p.evap_p, 0)} kPa`;

  const C = 2 * Math.PI * 36;
  const arc = $("comp-arc");
  arc.style.strokeDasharray = `${C}`;
  arc.style.strokeDashoffset = `${C * (1 - clamp(p.capacity, 0, 100) / 100)}`;
  arc.style.stroke = p.chiller_alarm ? "var(--red)" : "var(--green)";

  const lamp = (id, on) => $(id).classList.toggle("on", !!on);
  lamp("l-pump", p.pump); lamp("l-flow", p.flow_ok); lamp("l-comp", p.running); lamp("l-local", p.local);

  const loadFrac = clamp(p.load / 500, 0, 1);
  const h = 230 * loadFrac;
  $("load-bar").setAttribute("y", 260 - h); $("load-bar").setAttribute("height", h);
  document.querySelectorAll("#windows rect").forEach((w) => w.classList.toggle("lit", +w.dataset.rank < loadFrac));

  const cwFlowing = p.running || p.cwl - p.cwe > 0.3;
  for (const id of ["p-cwl", "p-cwe"]) $(id).classList.toggle("still", !cwFlowing);
  for (const id of ["p-chws", "p-chwr1", "p-chwr2"]) $(id).classList.toggle("still", p.flow < 0.5);
  $("tower-drops").classList.toggle("off", !cwFlowing);
  const f = m.faults || 0;
  $("tower-fault").classList.toggle("on", !!(f & 1));

  const tag = $("mode-tag");
  tag.className = "tag";
  if (p.lockout) { tag.textContent = "LOCKOUT"; tag.classList.add("bad"); }
  else if (p.comm_fault) { tag.textContent = "COMM FAULT"; tag.classList.add("bad"); }
  else if (p.local) { tag.textContent = "CHILLER LOCAL"; tag.classList.add("warn"); }
  else tag.textContent = p.plant_enable ? "AUTO // ENABLED" : "AUTO // DISABLED";
}

function animate(ts) {
  const a = app.anim;
  const dt = a.t ? Math.min(0.1, (ts - a.t) / 1000) : 0;
  a.t = ts;
  const p = app.last;
  if (p) {
    a.comp += dt * (p.running ? 120 + 6 * p.capacity : 0);
    a.pump += dt * (p.pump ? 540 : 0);
    const cwFlowing = p.running || p.cwl - p.cwe > 0.3;
    const towerFault = (app.snap && app.snap.faults) & 1;
    a.fan += dt * (cwFlowing && !towerFault ? 400 : 0);
    $("comp-rotor").setAttribute("transform", `rotate(${a.comp % 360})`);
    $("pump-imp").setAttribute("transform", `rotate(${a.pump % 360})`);
    $("tower-fan").setAttribute("transform", `rotate(${a.fan % 360})`);
    const chwSpeed = 6 * p.flow;               // ~150 px/s at design flow
    const cwSpeed = cwFlowing ? 90 : 0;
    for (const [id, v] of [["p-chws", chwSpeed], ["p-chwr1", chwSpeed], ["p-chwr2", chwSpeed], ["p-cwl", cwSpeed], ["p-cwe", cwSpeed]]) {
      a.dash[id] = ((a.dash[id] || 0) - v * dt) % 440;
      $(id).style.strokeDashoffset = a.dash[id];
    }
  }
  requestAnimationFrame(animate);
}

/* ------------------------------------------------------------ controls */
function setRangeFill(el) {
  const pct = ((el.value - el.min) / (el.max - el.min)) * 100;
  el.style.setProperty("--p", `${pct}%`);
}

function renderControls(p) {
  const pw = $("btn-power");
  pw.classList.toggle("on", p.plant_enable);
  const ps = $("power-state");
  ps.textContent = p.state_name.replace("_", " ");
  ps.className = "power-state " + (p.state_name === "FAULT" ? "fault" : p.plant_enable ? "on" : "off");
  const st = $("btn-start");
  if (!pw.classList.contains("armed") && !st.classList.contains("armed")) {
    $("power-hint").textContent = p.plant_enable ? "Plant enabled. STOP acts immediately" : "Plant disabled. START needs a confirm click";
  }
  st.disabled = p.plant_enable || p.lockout;
  $("btn-stop").disabled = !p.plant_enable;

  $("c-active-sp").textContent = fmt(p.active_sp);
  if (!app.sp.dirty) { app.sp.value = p.sp_base; }
  $("sp-val").textContent = fmt(app.sp.value);
  if (!app.sp.dragging) { $("sp-range").value = app.sp.value; setRangeFill($("sp-range")); }
  $("sp-readout").classList.toggle("dirty", app.sp.dirty);
  $("sp-apply").disabled = !app.sp.dirty;

  if (!app.dl.dirty) { app.dl.value = p.demand_limit; }
  $("dl-val").textContent = fmt(app.dl.value, 0);
  if (!app.dl.dragging) { $("dl-range").value = app.dl.value; setRangeFill($("dl-range")); }
  $("dl-apply").disabled = !app.dl.dirty;

  if (!app.spresetBusy) $("sw-spreset").checked = p.sp_reset;

  const hot = p.alarm !== 0 || p.lockout || p.state_name === "FAULT";
  $("btn-reset").classList.toggle("hot", hot);
}

function arm(btn, label, action) {
  if (btn.dataset.armed) {
    clearTimeout(+btn.dataset.armed);
    delete btn.dataset.armed;
    btn.classList.remove("armed");
    action();
    return;
  }
  btn.classList.add("armed");
  if (label) label();
  btn.dataset.armed = String(setTimeout(() => { delete btn.dataset.armed; btn.classList.remove("armed"); }, 3000));
}

function bindControls() {
  // STOP is the safe direction: one click, no confirm. START needs a confirm click.
  const stop = () => send("enable", false).then(() => toast("Plant STOP written"));
  const armStart = (btn) => arm(btn, () => { $("power-hint").textContent = "Click again within 3 s to START plant"; },
    () => send("enable", true).then(() => toast("Plant START written")));
  const pw = $("btn-power");
  pw.addEventListener("click", () => {
    const p = app.last; if (!p) return;
    if (p.plant_enable) stop(); else armStart(pw);
  });
  $("btn-start").addEventListener("click", () => armStart($("btn-start")));
  $("btn-stop").addEventListener("click", stop);

  const spR = $("sp-range");
  const setSp = (v) => {
    app.sp.value = Math.round(clamp(v, app.spRange[0], app.spRange[1]) * 10) / 10;
    app.sp.dirty = app.last ? Math.abs(app.sp.value - app.last.sp_base) > 0.04 : true;
    $("sp-val").textContent = fmt(app.sp.value);
    spR.value = app.sp.value; setRangeFill(spR);
    $("sp-readout").classList.toggle("dirty", app.sp.dirty);
    $("sp-apply").disabled = !app.sp.dirty;
  };
  document.querySelectorAll("[data-step]").forEach((b) => b.addEventListener("click", () => setSp(app.sp.value + +b.dataset.step)));
  spR.addEventListener("input", () => { app.sp.dragging = true; setSp(+spR.value); });
  spR.addEventListener("change", () => { app.sp.dragging = false; });
  $("sp-apply").addEventListener("click", () => {
    const v = app.sp.value;
    send("setpoint", v).then(() => { app.sp.dirty = false; toast(`Setpoint ${v.toFixed(1)} °C written`); });
  });

  const dlR = $("dl-range");
  dlR.addEventListener("input", () => {
    app.dl.dragging = true;
    app.dl.value = +dlR.value;
    app.dl.dirty = app.last ? Math.abs(app.dl.value - app.last.demand_limit) > 0.5 : true;
    $("dl-val").textContent = fmt(app.dl.value, 0); setRangeFill(dlR);
    $("dl-apply").disabled = !app.dl.dirty;
  });
  dlR.addEventListener("change", () => { app.dl.dragging = false; });
  $("dl-apply").addEventListener("click", () => {
    const v = app.dl.value;
    send("demand", v).then(() => { app.dl.dirty = false; toast(`Demand limit ${v} % written`); });
  });

  $("sw-spreset").addEventListener("change", (e) => {
    app.spresetBusy = true;
    send("spreset", e.target.checked).finally(() => setTimeout(() => { app.spresetBusy = false; }, 1200));
  });

  const reset = () => send("reset", true).then(() => toast("Alarm reset pulse sent"));
  $("btn-reset").addEventListener("click", reset);
  $("btn-reset-banner").addEventListener("click", reset);

  document.querySelectorAll(".fault-btn").forEach((b) => b.addEventListener("click", () => {
    const name = b.dataset.fault;
    const on = !b.classList.contains("on");
    arm(b, () => { b.querySelector("i").textContent = on ? "INJECT?" : "CLEAR?"; },
      () => send("fault", on, { name }).then(() => toast(`SIM ${name} fault ${on ? "injected" : "cleared"}`)));
  }));

  document.querySelectorAll("#range .chip").forEach((c) => c.addEventListener("click", () => {
    document.querySelectorAll("#range .chip").forEach((x) => x.classList.toggle("on", x === c));
    app.range = +c.dataset.r;
    charts.forEach((ch) => ch.draw());
  }));
  document.querySelectorAll("#ev-filters .chip").forEach((c) => c.addEventListener("click", () => {
    document.querySelectorAll("#ev-filters .chip").forEach((x) => x.classList.toggle("on", x === c));
    app.evFilter = c.dataset.f;
    document.querySelectorAll("#ev-list li").forEach(applyEvFilter);
  }));
}

/* ---------------------------------------------------------------- KPIs */
function renderKpis(p) {
  const Cg = 2 * Math.PI * 48;
  const g = $("g-cap");
  g.style.strokeDasharray = `${Cg}`;
  g.style.strokeDashoffset = `${Cg * (1 - clamp(p.capacity, 0, 100) / 100)}`;
  const Cd = 2 * Math.PI * 56;
  $("g-dl").style.strokeDasharray = `${Cd * clamp(p.demand_limit, 0, 100) / 100} ${Cd}`;
  $("k-cap").textContent = fmt(p.capacity, 0);
  $("k-cool").textContent = fmt(p.cooling, 0);
  $("k-load").textContent = fmt(p.load, 0);
  $("k-cool-bar").style.width = `${clamp(p.cooling / 600, 0, 1) * 100}%`;
  $("k-load-mark").style.left = `${clamp(p.load / 600, 0, 1) * 100}%`;
  $("k-power").textContent = fmt(p.power, 1);
  $("k-cop").textContent = fmt(p.cop, 2);
  $("k-cop-bar").style.width = `${clamp(p.cop / 8, 0, 1) * 100}%`;
  $("k-dt").textContent = fmt(p.chwr - p.chws, 1);
  $("k-flow").textContent = fmt(p.flow, 1);
  $("k-lift").textContent = fmt(p.cond_p - p.evap_p, 0);
  $("k-pe").textContent = fmt(p.evap_p, 0);
  $("k-pc").textContent = fmt(p.cond_p, 0);
  $("k-hours").textContent = p.run_hours;
  $("k-starts").textContent = p.starts;
  $("k-trips").textContent = p.trips;
}

/* -------------------------------------------------------------- alarms */
function renderAlarm(p, m) {
  const active = p.alarm !== 0 || p.lockout || p.comm_fault;
  $("alarm-banner").classList.toggle("on", active);
  if (active) {
    $("ab-code").textContent = `A${String(p.alarm).padStart(3, "0")}`;
    $("ab-text").textContent = (m.alarm_text || "").toUpperCase() + (p.trips ? `  //  ${p.trips} TRIP(S) IN LAST HOUR` : "");
  }
}

function renderSequence(p) {
  const s = p.state;
  document.querySelectorAll("#sequence li").forEach((li) => {
    const i = +li.dataset.s;
    li.classList.toggle("on", i === s);
    li.classList.toggle("done", s !== 5 && s <= 3 && i < s);
  });
}

function renderFaults(m) {
  $("sim-panel").style.display = m.sim_faults ? "" : "none";
  document.querySelectorAll(".fault-btn").forEach((b) => {
    const bit = { tower: 0, pump: 1, refrigerant: 2 }[b.dataset.fault];
    const unknown = m.faults === null || m.faults === undefined;
    const on = !unknown && !!(m.faults & (1 << bit));
    b.disabled = unknown;
    b.classList.toggle("on", on);
    if (!b.classList.contains("armed")) b.querySelector("i").textContent = unknown ? "N/A" : on ? "ACTIVE" : "OFF";
  });
}

function applyEvFilter(li) {
  const f = app.evFilter;
  const lvl = li.dataset.level;
  const show = f === "all" || (f === "alarm" && ["alarm", "warn", "clear"].includes(lvl)) || (f === "cmd" && lvl === "cmd");
  li.classList.toggle("hidden", !show);
}

function addEvent(ev, live) {
  const li = document.createElement("li");
  li.className = ev.level;
  li.dataset.level = ev.level;
  const t = new Date(ev.t * 1000);
  const time = document.createElement("time");
  time.textContent = t.toLocaleTimeString([], { hour12: false });
  const span = document.createElement("span");
  span.textContent = ev.text;
  li.append(time, span);
  applyEvFilter(li);
  const list = $("ev-list");
  list.prepend(li);
  while (list.children.length > 300) list.lastChild.remove();
  if (live && ev.level === "alarm") toast(ev.text, true);
}

function toast(text, err = false) {
  const d = document.createElement("div");
  d.className = "toast" + (err ? " err" : "");
  d.textContent = text;
  $("toasts").appendChild(d);
  setTimeout(() => d.remove(), err ? 6000 : 2500);
}

/* -------------------------------------------------------------- trends */
class Trend {
  constructor(canvas, legend, series, unit) {
    this.c = canvas; this.ctx = canvas.getContext("2d");
    this.series = series; this.unit = unit; this.hover = null;
    this.legend = legend;
    legend.innerHTML = "";
    series.forEach((s) => {
      const el = document.createElement("span");
      el.innerHTML = `<i style="background:${s.color};color:${s.color}"></i>${s.label} <b>--</b>`;
      el.addEventListener("click", () => { s.off = !s.off; el.classList.toggle("off", s.off); this.draw(); });
      s.el = el.querySelector("b");
      legend.appendChild(el);
    });
    canvas.addEventListener("mousemove", (e) => { const r = canvas.getBoundingClientRect(); this.hover = e.clientX - r.left; this.draw(); });
    canvas.addEventListener("mouseleave", () => { this.hover = null; this.draw(); });
    new ResizeObserver(() => this.draw()).observe(canvas);
  }

  draw() {
    const c = this.c, ctx = this.ctx, dpr = window.devicePixelRatio || 1;
    const W = c.clientWidth, H = c.clientHeight;
    if (!W) return;
    if (c.width !== W * dpr || c.height !== H * dpr) { c.width = W * dpr; c.height = H * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const pad = { l: 44, r: 12, t: 8, b: 22 };
    const pw = W - pad.l - pad.r, ph = H - pad.t - pad.b;
    const hist = app.hist;
    const tMax = hist.length ? hist[hist.length - 1][0] : Date.now() / 1000;
    const tMin = tMax - app.range;
    let i0 = hist.findIndex((r) => r[0] >= tMin); if (i0 < 0) i0 = hist.length;
    const rows = hist.slice(i0);
    const idx = (k) => app.keys.indexOf(k) + 1;

    let lo = Infinity, hi = -Infinity;
    for (const s of this.series) {
      if (s.off) continue;
      const j = idx(s.key);
      for (const r of rows) { const v = r[j]; if (v < lo) lo = v; if (v > hi) hi = v; }
    }
    if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
    const floor0 = lo >= 0;
    if (hi - lo < (this.unit === "°C" ? 2 : 20)) { const m = (hi + lo) / 2, h = this.unit === "°C" ? 1 : 10; lo = m - h; hi = m + h; }
    const span = hi - lo;
    lo -= span * 0.08; hi += span * 0.08;
    if (floor0) lo = Math.max(0, lo);
    const step = niceStep((hi - lo) / 5);
    lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;

    const X = (t) => pad.l + ((t - tMin) / app.range) * pw;
    const Y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * ph;

    // grid
    ctx.font = "10px 'JetBrains Mono', monospace";
    ctx.strokeStyle = "rgba(0,240,255,.08)"; ctx.fillStyle = "#6e8aa6"; ctx.lineWidth = 1;
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (let v = lo; v <= hi + 1e-9; v += step) {
      const y = Math.round(Y(v)) + 0.5;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
      ctx.fillText(+v.toFixed(2), pad.l - 6, y);
    }
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    const tStep = app.range / 6;
    for (let k = 0; k <= 6; k++) {
      const t = tMin + k * tStep, x = Math.round(X(t)) + 0.5;
      ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + ph); ctx.stroke();
      if (k > 0) ctx.fillText(new Date(t * 1000).toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: app.range <= 300 ? "2-digit" : undefined }), x, pad.t + ph + 6);
    }
    ctx.fillText(this.unit, pad.l - 22, pad.t + ph + 6);

    // series
    ctx.save();
    ctx.beginPath(); ctx.rect(pad.l, pad.t, pw, ph); ctx.clip();
    for (const s of this.series) {
      if (s.off || rows.length < 2) continue;
      const j = idx(s.key);
      ctx.beginPath();
      rows.forEach((r, n) => { const x = X(r[0]), y = Y(r[j]); n ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.strokeStyle = s.color; ctx.lineWidth = s.width || 2;
      ctx.setLineDash(s.dash || []);
      ctx.shadowColor = s.color; ctx.shadowBlur = s.dash ? 0 : 8;
      ctx.stroke();
      if (s.fill) {
        ctx.shadowBlur = 0; ctx.setLineDash([]);
        ctx.lineTo(X(rows[rows.length - 1][0]), pad.t + ph); ctx.lineTo(X(rows[0][0]), pad.t + ph); ctx.closePath();
        const g = ctx.createLinearGradient(0, pad.t, 0, pad.t + ph);
        g.addColorStop(0, s.color + "40"); g.addColorStop(1, s.color + "00");
        ctx.fillStyle = g; ctx.fill();
      }
    }
    ctx.restore();
    ctx.setLineDash([]); ctx.shadowBlur = 0;

    // hover crosshair / legend values
    let row = rows[rows.length - 1];
    if (this.hover !== null && this.hover >= pad.l && rows.length) {
      const t = tMin + ((this.hover - pad.l) / pw) * app.range;
      let best = rows[0];
      for (const r of rows) if (Math.abs(r[0] - t) < Math.abs(best[0] - t)) best = r;
      row = best;
      const x = X(row[0]);
      ctx.strokeStyle = "rgba(255,255,255,.5)"; ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + ph); ctx.stroke(); ctx.setLineDash([]);
      for (const s of this.series) {
        if (s.off) continue;
        ctx.fillStyle = s.color; ctx.beginPath(); ctx.arc(x, Y(row[idx(s.key)]), 3.5, 0, 7); ctx.fill();
      }
      ctx.fillStyle = "#fff"; ctx.textAlign = x > W - 80 ? "right" : "left"; ctx.textBaseline = "top";
      ctx.fillText(new Date(row[0] * 1000).toLocaleTimeString([], { hour12: false }), x + (x > W - 80 ? -6 : 6), pad.t + 2);
    }
    for (const s of this.series) s.el.textContent = row ? fmt(row[idx(s.key)], s.dec ?? 1) : "--";
  }
}

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / p;
  return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * p;
}

let charts = [];

/* ---------------------------------------------------------------- boot */
function clock() {
  $("clock").textContent = new Date().toLocaleTimeString([], { hour12: false });
}

window.addEventListener("DOMContentLoaded", () => {
  buildWindows();
  charts = [
    new Trend($("c-temp"), $("lg-temp"), [
      { key: "chws", label: "CHWS", color: "#00f0ff", fill: true, dec: 2 },
      { key: "chwr", label: "CHWR", color: "#9b6bff", dec: 2 },
      { key: "active_sp", label: "SP", color: "#ffffff", dash: [6, 4], width: 1.5 },
      { key: "cwe", label: "CWE", color: "#ffb020" },
      { key: "cwl", label: "CWL", color: "#ff6a3d" },
      { key: "oat", label: "OAT", color: "#2bff9a", width: 1.5 },
    ], "°C"),
    new Trend($("c-kw"), $("lg-kw"), [
      { key: "load", label: "LOAD kW", color: "#ffb020", dash: [4, 3], width: 1.5, dec: 0 },
      { key: "cooling", label: "COOLING kW", color: "#00f0ff", fill: true, dec: 0 },
      { key: "power", label: "POWER kW", color: "#ff2e9a", fill: true },
      { key: "capacity", label: "CAP %", color: "#2bff9a", width: 1.5, dec: 0 },
    ], "kW"),
  ];
  bindControls();
  clock(); setInterval(clock, 1000);
  requestAnimationFrame(animate);
  connect();
});
