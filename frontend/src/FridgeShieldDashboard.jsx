/*
  FridgeShield dashboard prototype
  ================================
  Everything in the "Fake simulation" section stands in for the Python backend.
  When the backend is ready, delete createSim/step and the action functions, and
  replace them with a hook that receives the same shape over a WebSocket:
    - fridges: { id, section, target, stock, capacity, reported, estimate,
                 compressor, health, security, connectivity, rescue, offer, history }
    - trust:   trust[observer][subject] = 0..1
    - known:   which event ids each fridge knows about
    - events:  the event log
    - plan:    the current rescue plan
  The UI components below only read that shape, so they shouldn't need to change.

  Dependencies in your Vite project: tailwindcss, recharts, lucide-react.
*/
import { useState, useEffect, useRef } from "react";
import { LineChart, Line, XAxis, YAxis, ReferenceArea, ResponsiveContainer, Tooltip } from "recharts";
import {
  Play, Pause, RotateCcw, Zap, Unplug, Plug, Lock, Eye, EyeOff,
  ShieldAlert, ShieldCheck, Send, Bot, Clapperboard,
} from "lucide-react";

// Where the Python backend runs. Start it from the backend folder with: uvicorn main:app --reload
const API_URL = "http://localhost:8000";

/* ------------------------------------------------------------------ */
/* Store layout                                                        */
/* ------------------------------------------------------------------ */

const FRIDGES = [
  { id: "F1", section: "Dairy", x: 110, y: 95, target: 3, capacity: 60, stock: 35 },
  { id: "F3", section: "Dairy", x: 250, y: 95, target: 3, capacity: 60, stock: 30 },
  { id: "F4", section: "Dairy", x: 430, y: 95, target: 3, capacity: 60, stock: 40 },
  { id: "F5", section: "Drinks", x: 570, y: 95, target: 4, capacity: 50, stock: 30 },
  { id: "F2", section: "Meat", x: 110, y: 215, target: 2, capacity: 50, stock: 45 },
  { id: "F6", section: "Meat", x: 250, y: 215, target: 2, capacity: 50, stock: 42 },
  { id: "F7", section: "Ready meals", x: 430, y: 215, target: 3, capacity: 45, stock: 30 },
  { id: "F8", section: "Freezer", x: 570, y: 215, target: -18, capacity: 50, stock: 35, frozen: true },
];
const IDS = ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8"];
const CONFIG = Object.fromEntries(FRIDGES.map((f) => [f.id, f]));
const LINKS = [
  ["F1", "F3"], ["F3", "F4"], ["F4", "F5"], ["F2", "F6"], ["F6", "F7"], ["F7", "F8"],
  ["F1", "F2"], ["F3", "F6"], ["F4", "F7"], ["F5", "F8"],
];
const LEFT = new Set(["F1", "F2", "F3", "F6"]);
const crosses = ([a, b]) => LEFT.has(a) !== LEFT.has(b);
const AMBIENT = 19;
const LEAK = 0.03;

function mulberry32(seed) {
  let a = seed;
  return () => {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const fmtTime = (t) => {
  const m = 120 + t;
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(((m % 60) + 60) % 60).padStart(2, "0")}`;
};
const r1 = (n) => n.toFixed(1);

/* ------------------------------------------------------------------ */
/* Fake simulation (replace with the backend later)                    */
/* ------------------------------------------------------------------ */

function createSim(script) {
  const rand = mulberry32(42);
  const fridges = {};
  for (const c of FRIDGES) {
    const t0 = c.target + (rand() - 0.5) * 0.6;
    fridges[c.id] = {
      ...c, temp: t0, reported: t0, estimate: t0, compressor: false, door: false, doorTicks: 0,
      wear: 0, wearing: false, earlyWarned: false, spoofed: false, spoofDetected: false, liar: false,
      mismatch: 0, alertTicks: 0, rejectedAt: null, health: "normal", security: "normal",
      connectivity: "connected", rescue: "none", offer: 0, offered: false, requested: false,
      receivingTicks: 0, seq: 0, history: [],
    };
  }
  const trust = {};
  for (const a of IDS) { trust[a] = {}; for (const b of IDS) if (a !== b) trust[a][b] = 0.9; }
  const known = {};
  for (const id of IDS) known[id] = new Set();
  const sim = {
    t: 0, rand, fridges, trust, known, events: [], pending: [], messages: [], partitioned: false,
    centralOnline: true, syncTicks: 0, plan: null, script, msgId: 0, evId: 0, unverified: [],
    reconciled: 0, trustCat: Object.fromEntries(IDS.map((id) => [id, "trusted"])),
  };
  for (let i = -20; i < 0; i++) { sim.t = i; for (const id of IDS) updateFridge(sim, fridges[id]); }
  sim.t = 0;
  return sim;
}

function emit(sim, origin, kind, subject, text, opts = {}) {
  const local = !opts.observer && !opts.truth && !!origin;
  const ev = { id: ++sim.evId, t: sim.t, origin, kind, subject, text, local, truth: !!opts.truth, data: opts.data || {} };
  if (local) {
    ev.seq = ++sim.fridges[origin].seq;
    sim.known[origin].add(ev.id);
    sim.pending.push({ id: ev.id, at: origin });
  }
  sim.events.push(ev);
  return ev;
}
const eventById = (sim, id) => sim.events[id - 1];

const linkUp = (sim, l) => !(sim.partitioned && crosses(l));
const neighbours = (sim, id) =>
  LINKS.filter((l) => l.includes(id) && linkUp(sim, l)).map((l) => (l[0] === id ? l[1] : l[0]));
function reachable(sim, a, b) {
  if (a === b) return true;
  const seen = new Set([a]);
  const queue = [a];
  while (queue.length) {
    const x = queue.shift();
    for (const n of neighbours(sim, x)) {
      if (n === b) return true;
      if (!seen.has(n)) { seen.add(n); queue.push(n); }
    }
  }
  return false;
}

function trustCategory(sim, id) {
  const views = IDS.filter((o) => o !== id).map((o) => sim.trust[o][id]);
  if (views.filter((v) => v < 0.45).length >= 2) return "distrusted";
  if (views.filter((v) => v < 0.75).length >= 2) return "unverified";
  return "trusted";
}
function contradict(sim, observer, origin) {
  sim.trust[observer][origin] = Math.max(0, Math.min(sim.trust[observer][origin], 0.6) - 0.35);
}
function makeOffer(sim, me, to, litres) {
  me.offered = true; me.rescue = "donor"; me.offer = litres;
  emit(sim, me.id, "offer", to, `${me.id} offers ${litres} L of spare space to ${to}`, { data: { litres } });
}

// What a fridge does when it hears about an event for the first time.
function onReceive(sim, n, ev) {
  const me = sim.fridges[n];
  if (ev.kind === "claim" && n !== ev.origin) {
    if (reachable(sim, n, ev.subject)) contradict(sim, n, ev.origin);
    else {
      sim.trust[n][ev.origin] = Math.min(sim.trust[n][ev.origin], 0.6);
      sim.unverified.push({ observer: n, origin: ev.origin, subject: ev.subject });
    }
  }
  if (ev.kind === "rescue_request" && n !== ev.origin && !me.offered) {
    const req = sim.fridges[ev.origin];
    const spare = me.capacity - me.stock;
    if (me.liar) makeOffer(sim, me, ev.origin, 30); // a compromised fridge overstates its space
    else if (!!me.frozen === !!req.frozen && me.health === "normal" && me.security === "normal"
      && spare >= 6 && sim.trust[n][ev.origin] >= 0.45) makeOffer(sim, me, ev.origin, spare);
  }
}

// Gossip: each new event floods one hop per tick over links that are up.
function gossip(sim) {
  const current = sim.pending;
  sim.pending = [];
  sim.messages = [];
  for (const p of current) {
    for (const n of neighbours(sim, p.at)) {
      if (sim.known[n].has(p.id)) continue;
      sim.known[n].add(p.id);
      const ev = eventById(sim, p.id);
      if (sim.messages.length < 30) sim.messages.push({ id: ++sim.msgId, from: p.at, to: n, kind: ev.kind });
      sim.pending.push({ id: p.id, at: n });
      onReceive(sim, n, ev);
    }
  }
}

function updateFridge(sim, f) {
  const { rand } = sim;
  const cool = f.frozen ? 2.2 : 0.9;
  if (f.wearing) f.wear = Math.min(0.4, f.wear + 0.02);
  f.temp += LEAK * (AMBIENT - f.temp) + (f.door ? 0.8 : 0)
    - (f.compressor ? cool * (1 - f.wear * 0.6) : 0) + (rand() - 0.5) * 0.08;
  // A spoofed sensor reports "slightly cold", so the thermostat switches the compressor off.
  f.reported = f.spoofed ? f.target - 1 + (rand() - 0.5) * 0.2 : f.temp + (rand() - 0.5) * 0.12;
  // Independent estimate from compressor power draw and the thermal model.
  f.estimate = f.temp + (rand() - 0.5) * 0.3;
  const control = f.security === "protective" ? f.estimate : f.reported;
  if (control > f.target + 0.8) f.compressor = true;
  else if (control < f.target - 0.8) f.compressor = false;
  f.history.push({ t: sim.t, reported: f.reported, estimate: f.estimate, actual: f.temp });
  if (f.history.length > 60) f.history.shift();
}

// Local, deterministic checks that every fridge runs for itself. No LLM here.
function detect(sim, f) {
  const gap = Math.abs(f.reported - f.estimate);
  f.mismatch = gap > 2.5 ? f.mismatch + 1 : 0;
  if (f.mismatch === 2 && f.security !== "protective") {
    f.security = "suspicious";
    f.spoofDetected = true;
    emit(sim, f.id, "warning", f.id,
      `${f.id}'s sensor reads ${r1(f.reported)}°C but its power draw implies ${r1(f.estimate)}°C. Possible sensor spoofing`);
  }
  if (f.mismatch === 7) {
    f.security = "protective";
    emit(sim, f.id, "protective", f.id,
      `${f.id} switched to protective mode: ignoring its own sensor and cooling from its power-draw estimate`);
  }
  if (f.alertTicks > 0) {
    f.alertTicks--;
    if (f.alertTicks === 0 && f.mismatch === 0 && f.security === "suspicious") f.security = "normal";
  }
  const over = f.estimate - f.target;
  f.health = over > 3 ? "failing" : f.door ? "door_open" : f.wear > 0.25 ? "degrading" : "normal";
  if (f.wear > 0.25 && !f.earlyWarned) {
    f.earlyWarned = true;
    emit(sim, f.id, "early_warning", f.id,
      `${f.id}'s compressor is running about 30% harder than usual to hold temperature. Likely wear, inspect soon`);
  }
  if (f.health === "failing" && !f.requested && f.stock > 0) {
    f.requested = true;
    f.rescue = "at_risk";
    emit(sim, f.id, "rescue_request", f.id, `${f.id} requests space for ${f.stock} L of stock at risk`,
      { data: { litres: f.stock } });
  }
}

function computePlan(sim) {
  const req = IDS.map((id) => sim.fridges[id]).find((f) => f.rescue === "at_risk");
  if (!req) return null;
  const offers = new Map();
  for (const id of sim.known[req.id]) {
    const e = eventById(sim, id);
    if (e.kind === "offer" && e.subject === req.id) offers.set(e.origin, e.data.litres);
  }
  const usable = [];
  const excluded = [];
  for (const [id, litres] of offers) {
    if (sim.trust[req.id][id] < 0.45) excluded.push({ id, litres });
    else usable.push({ id, litres, dist: Math.hypot(CONFIG[id].x - req.x, CONFIG[id].y - req.y) });
  }
  usable.sort((a, b) => a.dist - b.dist);
  let need = req.stock;
  const allocations = [];
  for (const u of usable) {
    if (need <= 0) break;
    const take = Math.min(u.litres, need);
    allocations.push({ id: u.id, litres: take, metres: Math.round(u.dist / 10) });
    need -= take;
  }
  return { for: req.id, total: req.stock, allocations, excluded, shortfall: need, offers: offers.size };
}

function step(sim) {
  sim.t++;
  if (sim.script && SCRIPT[sim.t]) SCRIPT[sim.t](sim);
  for (const id of IDS) {
    const f = sim.fridges[id];
    updateFridge(sim, f);
    detect(sim, f);
    if (f.door && --f.doorTicks <= 0) f.door = false;
    if (f.receivingTicks > 0 && --f.receivingTicks === 0) f.rescue = "none";
  }
  sim.unverified = sim.unverified.filter((u) => {
    if (reachable(sim, u.observer, u.subject)) { contradict(sim, u.observer, u.origin); return false; }
    return true;
  });
  gossip(sim);
  if (sim.syncTicks > 0 && --sim.syncTicks === 0) for (const id of IDS) sim.fridges[id].connectivity = "connected";
  for (const id of IDS) {
    const c = trustCategory(sim, id);
    if (c !== sim.trustCat[id]) {
      sim.trustCat[id] = c;
      if (c === "distrusted") emit(sim, null, "system", id,
        `The network now distrusts ${id}: its claims were contradicted by independent evidence`, { observer: true });
      if (c === "unverified") emit(sim, null, "system", id,
        `${id}'s claims can't be checked from here, so neighbours treat them as unconfirmed`, { observer: true });
    }
  }
  sim.plan = computePlan(sim);
}

/* Actions: the attack panel and scripted scenario call these. */
function spoofSensor(sim, id) {
  const f = sim.fridges[id];
  if (f.spoofed) return;
  f.spoofed = true;
  emit(sim, null, "truth", id, `Attacker starts spoofing ${id}'s temperature sensor`, { truth: true });
}
function badSetpoint(sim, id) {
  const f = sim.fridges[id];
  f.alertTicks = 5;
  f.rejectedAt = sim.t;
  if (f.security === "normal") f.security = "suspicious";
  emit(sim, null, "truth", id, `Attacker sends ${id} a setpoint of 15°C`, { truth: true });
  emit(sim, id, "command_rejected", id,
    `${id} rejected a setpoint of 15°C (safe range ${f.target - 2} to ${f.target + 3}°C)`);
}
function openDoor(sim, id) {
  const f = sim.fridges[id];
  f.door = true; f.doorTicks = 2;
  emit(sim, id, "door", id, `${id}'s door was left open`);
}
function compromise(sim, id) {
  sim.fridges[id].liar = true;
  emit(sim, null, "truth", id, `Attacker takes control of ${id}`, { truth: true });
}
function lie(sim) {
  if (sim.fridges.F3.liar) emit(sim, "F3", "claim", "F5", "F3 claims F5 is overheating at 8.4°C");
}
function toggleCentral(sim) {
  sim.centralOnline = !sim.centralOnline;
  emit(sim, null, "network", null, sim.centralOnline
    ? "Central management is back online"
    : "Central management locked out by ransomware. Fridges carry on locally", { observer: true });
}
function setPartition(sim, on) {
  if (on === sim.partitioned) return;
  sim.partitioned = on;
  if (on) {
    for (const id of IDS) sim.fridges[id].connectivity = "isolated";
    emit(sim, null, "network", null, "Network partitioned: the two halves of the store can't reach each other", { observer: true });
    return;
  }
  // Reconciliation: neighbours across the healed links swap the events the other side is missing.
  const restored = emit(sim, null, "network", null, "", { observer: true });
  let count = 0;
  for (const l of LINKS.filter(crosses)) {
    for (const [x, y] of [l, [l[1], l[0]]]) {
      for (const id of [...sim.known[x]]) {
        if (sim.known[y].has(id)) continue;
        sim.known[y].add(id);
        count++;
        sim.pending.push({ id, at: y });
        onReceive(sim, y, eventById(sim, id));
      }
    }
  }
  sim.reconciled += count;
  sim.syncTicks = 3;
  for (const id of IDS) sim.fridges[id].connectivity = "syncing";
  restored.text = `Network restored. Fridges compared version vectors and exchanged ${count} missing events`;
}
function approvePlan(sim) {
  const p = sim.plan;
  if (!p) return;
  const req = sim.fridges[p.for];
  const moved = p.allocations.reduce((s, a) => s + a.litres, 0);
  for (const id of IDS) if (sim.fridges[id].rescue === "donor") sim.fridges[id].rescue = "none";
  for (const a of p.allocations) {
    const f = sim.fridges[a.id];
    f.stock += a.litres; f.rescue = "receiving"; f.receivingTicks = 5;
  }
  req.stock -= moved;
  req.rescue = "none";
  emit(sim, null, "system", p.for, `Staff approved the rescue plan: ${moved} L moved out of ${p.for}`
    + (p.shortfall > 0 ? `, ${p.shortfall} L still needs somewhere cold` : ""), { observer: true });
  sim.plan = null;
}

// The demo scenario, keyed by simulated minute.
const SCRIPT = {
  1: (s) => { s.fridges.F2.wearing = true; },
  2: (s) => openDoor(s, "F1"),
  3: (s) => { if (s.centralOnline) toggleCentral(s); },
  5: (s) => spoofSensor(s, "F4"),
  7: (s) => setPartition(s, true),
  9: (s) => compromise(s, "F3"),
  10: lie,
  14: lie,
  18: lie,
  25: (s) => setPartition(s, false),
};

/* ------------------------------------------------------------------ */
/* Mock investigator: swap investigate() for your LLM call             */
/* ------------------------------------------------------------------ */

function investigate(sim, question, selected) {
  const m = question.match(FRIDGE_REF);
  const id = m ? `F${m[1]}` : selected;
  if (!m && /rescue|plan|move|space|food/i.test(question)) return planAnswer(sim);
  return fridgeAnswer(sim, id);
}
function fridgeAnswer(sim, id) {
  const f = sim.fridges[id];
  const cat = trustCategory(sim, id);
  const parts = [];
  if (f.spoofDetected) {
    if (Math.abs(f.reported - f.estimate) > 2.5)
      parts.push(`${id}'s sensor reports ${r1(f.reported)}°C, but its power draw implies about ${r1(f.estimate)}°C.`);
    parts.push("A compressor fault wouldn't keep the sensor this steady while the cabinet warms, so falsified sensor readings are the most likely cause (confidence: medium).");
    if (f.security === "protective")
      parts.push("It has switched to protective mode and is cooling from its power draw instead of its sensor.");
    parts.push("Check it with a handheld thermometer before trusting its display.");
  } else if (f.rejectedAt !== null && sim.t - f.rejectedAt < 10) {
    parts.push(`${id} recently rejected a setpoint command outside its safe range. Its own safety check blocked it, but the command suggests someone may be tampering with the controls.`);
  }
  if (f.health === "degrading")
    parts.push("Early warning: its compressor is working about 30% harder than usual to hold temperature, a common sign of wear before a failure. Book an inspection.");
  if (f.door) parts.push("Its door is open, which explains a short temperature rise.");
  if (cat === "distrusted")
    parts.push("Its neighbours no longer trust it: its claims about other fridges were contradicted by independent evidence, so treat its reports and any offers of space as unreliable.");
  else if (cat === "unverified")
    parts.push("Some of its claims can't be checked yet because the evidence is on the other side of the partition, so neighbours treat them as unconfirmed.");
  if (f.rescue === "at_risk") parts.push(`About ${f.stock} L of stock is at risk. Ask me about the rescue plan.`);
  if (!parts.length)
    parts.push(`${id} looks healthy. It reads ${r1(f.reported)}°C against a target of ${f.target}°C, and nothing unusual shows up in its recent events.`);
  if (sim.partitioned) parts.push(`The network is partitioned, so I can only see what ${id}'s side of the store knows.`);
  return { text: parts.join(" "), basis: `Based on ${sim.known[id].size} events known to ${id}` };
}
function planAnswer(sim) {
  const p = sim.plan;
  if (!p) return { text: "There's no rescue in progress. If a fridge's stock is at risk, I'll explain the plan its neighbours put together.", basis: "Based on the current rescue state" };
  const parts = [`${p.for} has ${p.total} L of stock at risk.`];
  if (p.allocations.length)
    parts.push(`The best plan from the offers it has received: ${p.allocations.map((a) => `${a.litres} L to ${a.id} (${a.metres} m away)`).join(", ")}.`);
  if (p.excluded.length)
    parts.push(`${p.excluded.map((e) => e.id).join(" and ")} offered space, but ${p.for} doesn't trust its claims, so it's left out.`);
  if (p.shortfall > 0)
    parts.push(`${p.shortfall} L has nowhere to go yet${sim.partitioned ? " because fridges on the other side of the partition haven't heard the request" : ""}.`);
  parts.push("Approve the plan to move the stock, and check moved items against your food safety rules.");
  return { text: parts.join(" "), basis: `Based on ${p.offers} offers known to ${p.for}` };
}

/* ------------------------------------------------------------------ */
/* Real investigator: what gets sent to the backend                    */
/* ------------------------------------------------------------------ */

const FRIDGE_REF = /\bf(?:ridge)?\s?([1-8])\b/i;

// Which fridge a question is about: one it names, the fridge needing rescue, or the selected one.
function targetFridge(sim, question, selected) {
  const m = question.match(FRIDGE_REF);
  if (m) return `F${m[1]}`;
  if (/rescue|plan|move|space|food/i.test(question) && sim.plan) return sim.plan.for;
  return selected;
}

// The situation packet: the facts the LLM is allowed to reason over. When the backend runs the
// simulation, it should build this itself instead of the browser.
function buildPacket(sim, id) {
  const f = sim.fridges[id];
  const events = sim.events
    .filter((e) => sim.known[id].has(e.id))
    .slice(-15)
    .map((e) => ({ id: e.id, time: fmtTime(e.t), from: e.origin, kind: e.kind, about: e.subject, text: e.text }));
  return {
    store_time: fmtTime(sim.t),
    fridge: {
      id, section: f.section, target_c: f.target,
      sensor_reading_c: +f.reported.toFixed(1),
      power_draw_estimate_c: +f.estimate.toFixed(1),
      compressor_on: f.compressor, door_open: f.door,
      stock_litres: f.stock, capacity_litres: f.capacity,
      health: f.health, security: f.security, network: f.connectivity, rescue: f.rescue,
    },
    how_other_fridges_rate_it: {
      status: trustCategory(sim, id),
      scores: Object.fromEntries(IDS.filter((o) => o !== id).map((o) => [o, +sim.trust[o][id].toFixed(2)])),
    },
    neighbours_it_can_reach: neighbours(sim, id),
    network_partitioned: sim.partitioned,
    central_management_online: sim.centralOnline,
    rescue_plan: sim.plan,
    events,
  };
}

/* ------------------------------------------------------------------ */
/* UI                                                                  */
/* ------------------------------------------------------------------ */

const HEALTH = {
  normal: { fill: "#0f2a1d", stroke: "#4ade80", text: "#dcfce7", label: "Normal", tone: "text-emerald-300" },
  door_open: { fill: "#2a2410", stroke: "#facc15", text: "#fef9c3", label: "Door open", tone: "text-yellow-300" },
  degrading: { fill: "#2d1a0c", stroke: "#fb923c", text: "#ffedd5", label: "Degrading", tone: "text-orange-300" },
  failing: { fill: "#300f12", stroke: "#f87171", text: "#fee2e2", label: "Failing", tone: "text-red-300" },
};
const SECURITY = {
  normal: { label: "Normal", tone: "text-emerald-300" },
  suspicious: { label: "Suspicious", tone: "text-amber-300" },
  protective: { label: "Protective mode", tone: "text-sky-300" },
};
const TRUST = {
  trusted: { label: "Trusted", tone: "text-emerald-300" },
  unverified: { label: "Unverified", tone: "text-amber-300" },
  distrusted: { label: "Distrusted", tone: "text-red-300" },
};
const CONNECTIVITY = {
  connected: { label: "Connected", tone: "text-emerald-300" },
  isolated: { label: "Partitioned", tone: "text-red-300" },
  syncing: { label: "Syncing", tone: "text-sky-300" },
};
const RESCUE = {
  none: { label: "None", tone: "text-slate-300" },
  at_risk: { label: "Stock at risk", tone: "text-red-300" },
  donor: { label: "Offering space", tone: "text-sky-300" },
  receiving: { label: "Receiving stock", tone: "text-emerald-300" },
};
const MSG_COLOR = {
  warning: "#fbbf24", protective: "#38bdf8", claim: "#f87171", offer: "#7dd3fc",
  rescue_request: "#fca5a5", command_rejected: "#fcd34d", early_warning: "#fb923c", door: "#fde047",
};
const FEED_DOT = {
  warning: "bg-amber-400", protective: "bg-sky-400", claim: "bg-red-400", offer: "bg-sky-300",
  rescue_request: "bg-red-300", command_rejected: "bg-amber-300", early_warning: "bg-orange-400",
  door: "bg-yellow-300", network: "bg-slate-400", system: "bg-slate-200", truth: "bg-fuchsia-400",
};

const btn = "inline-flex items-center gap-2 rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 focus:outline-none focus:ring-2 focus:ring-sky-400";
const card = "rounded-xl border border-slate-800 bg-slate-900 p-4";

function FloorPlan({ sim, selected, onSelect, showTruth }) {
  const W = 84, H = 52;
  return (
    <svg viewBox="0 0 680 300" className="h-auto w-full" role="img" aria-label="Store floor plan showing fridges and network links">
      <defs>
        <pattern id="fs-grid" width="20" height="20" patternUnits="userSpaceOnUse">
          <circle cx="1" cy="1" r="1" fill="#1e293b" />
        </pattern>
      </defs>
      <rect x="10" y="10" width="660" height="280" rx="14" fill="url(#fs-grid)" stroke="#1e293b" />
      <text x="28" y="34" fontSize="12" fill="#64748b">Chilled aisle</text>
      <text x="28" y="282" fontSize="12" fill="#64748b">Meat, meals and frozen aisle</text>

      {LINKS.map(([a, b]) => {
        const A = CONFIG[a], B = CONFIG[b];
        const up = linkUp(sim, [a, b]);
        return (
          <line key={a + b} x1={A.x} y1={A.y} x2={B.x} y2={B.y}
            stroke={up ? "#334155" : "#ef4444"} strokeWidth={up ? 2 : 1.5}
            strokeDasharray={up ? undefined : "4 5"} opacity={up ? 1 : 0.7} />
        );
      })}

      {sim.partitioned && (
        <g>
          <line x1="340" y1="44" x2="340" y2="262" stroke="#ef4444" strokeDasharray="6 4" />
          <text x="348" y="56" fontSize="12" fill="#f87171">Partition</text>
        </g>
      )}

      {sim.messages.map((m) => {
        const A = CONFIG[m.from], B = CONFIG[m.to];
        return (
          <circle key={m.id} cx={A.x} cy={A.y} r="4" className="fs-msg"
            fill={MSG_COLOR[m.kind] || "#cbd5e1"}
            style={{ "--dx": `${B.x - A.x}px`, "--dy": `${B.y - A.y}px` }} />
        );
      })}

      {IDS.map((id) => {
        const f = sim.fridges[id];
        const h = HEALTH[f.health];
        const cat = trustCategory(sim, id);
        const top = f.y < 150;
        const labelY = top ? f.y - H / 2 - 10 : f.y + H / 2 + 18;
        return (
          <g key={id} onClick={() => onSelect(id)} className="cursor-pointer" role="button" aria-label={`Select ${id}`}>
            {selected === id && (
              <rect x={f.x - W / 2 - 6} y={f.y - H / 2 - 6} width={W + 12} height={H + 12} rx="14"
                fill="none" stroke="#e2e8f0" strokeWidth="1.5" />
            )}
            {f.connectivity === "syncing" && (
              <rect x={f.x - W / 2 - 4} y={f.y - H / 2 - 4} width={W + 8} height={H + 8} rx="12"
                fill="none" stroke="#38bdf8" strokeWidth="2" className="animate-pulse" />
            )}
            <rect x={f.x - W / 2} y={f.y - H / 2} width={W} height={H} rx="10"
              fill={h.fill} stroke={h.stroke} strokeWidth="2"
              strokeDasharray={f.connectivity === "isolated" ? "6 4" : undefined} />
            <text x={f.x} y={f.y - 5} textAnchor="middle" fontSize="14" fontWeight="600" fill={h.text}>{id}</text>
            <text x={f.x} y={f.y + 14} textAnchor="middle" fontSize="12" fill={h.text}>{r1(f.reported)}°C</text>
            <text x={f.x} y={labelY} textAnchor="middle" fontSize="11" fill="#94a3b8">{f.section}</text>

            {f.security !== "normal" && (
              <g>
                <circle cx={f.x + W / 2} cy={f.y - H / 2} r="10" fill={f.security === "protective" ? "#0c4a6e" : "#78350f"}
                  stroke={f.security === "protective" ? "#38bdf8" : "#fbbf24"} />
                {f.security === "protective"
                  ? <ShieldCheck x={f.x + W / 2 - 6} y={f.y - H / 2 - 6} width={12} height={12} color="#bae6fd" />
                  : <ShieldAlert x={f.x + W / 2 - 6} y={f.y - H / 2 - 6} width={12} height={12} color="#fde68a" />}
              </g>
            )}
            {cat !== "trusted" && (
              <g>
                <circle cx={f.x - W / 2} cy={f.y - H / 2} r="10" fill={cat === "distrusted" ? "#7f1d1d" : "#78350f"}
                  stroke={cat === "distrusted" ? "#f87171" : "#fbbf24"} />
                <text x={f.x - W / 2} y={f.y - H / 2 + 4} textAnchor="middle" fontSize="12" fontWeight="700"
                  fill={cat === "distrusted" ? "#fecaca" : "#fde68a"}>{cat === "distrusted" ? "✕" : "?"}</text>
              </g>
            )}
            {f.rescue !== "none" && (
              <g>
                <rect x={f.x + W / 2 - 34} y={f.y + H / 2 - 9} width="48" height="18" rx="9"
                  fill={f.rescue === "at_risk" ? "#7f1d1d" : f.rescue === "donor" ? "#0c4a6e" : "#14532d"} />
                <text x={f.x + W / 2 - 10} y={f.y + H / 2 + 4} textAnchor="middle" fontSize="11" fill="#f1f5f9">
                  {f.rescue === "at_risk" ? `${f.stock} L` : f.rescue === "donor" ? `+${f.offer} L` : "In"}
                </text>
              </g>
            )}
            {showTruth && (f.spoofed || f.liar) && (
              <g>
                <circle cx={f.x - W / 2} cy={f.y + H / 2} r="10" fill="#4a044e" stroke="#e879f9" />
                <Zap x={f.x - W / 2 - 6} y={f.y + H / 2 - 6} width={12} height={12} color="#f5d0fe" />
              </g>
            )}
          </g>
        );
      })}
    </svg>
  );
}

function Legend({ showTruth }) {
  const item = (swatch, label) => (
    <span className="flex items-center gap-2">{swatch}{label}</span>
  );
  const box = (border, extra = "") => <span className={`inline-block h-3 w-5 rounded ${border} ${extra}`} />;
  return (
    <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-xs text-slate-400">
      {item(box("border-2 border-emerald-400"), "Normal")}
      {item(box("border-2 border-orange-400"), "Degrading")}
      {item(box("border-2 border-red-400"), "Failing")}
      {item(box("border-2 border-dashed border-slate-400"), "Cut off by partition")}
      {item(<span className="inline-block h-3 w-3 rounded-full border border-amber-400 bg-amber-900" />, "Suspicious or unverified")}
      {item(<span className="inline-block h-3 w-3 rounded-full border border-red-400 bg-red-900" />, "Distrusted")}
      {item(<span className="inline-block h-2 w-2 rounded-full bg-amber-300" />, "Message in flight")}
      {showTruth && item(<span className="inline-block h-3 w-3 rounded-full border border-fuchsia-400 bg-fuchsia-900" />, "Actually attacked")}
    </div>
  );
}

function StateRow({ label, value }) {
  return (
    <div className="flex items-center justify-between rounded-lg bg-slate-800 px-3 py-1.5 text-sm">
      <span className="text-slate-400">{label}</span>
      <span className={value.tone}>{value.label}</span>
    </div>
  );
}

function FridgeDetail({ sim, id, showTruth, onAction }) {
  const f = sim.fridges[id];
  const data = f.history.map((h) => ({
    time: fmtTime(h.t), reported: +h.reported.toFixed(2), estimate: +h.estimate.toFixed(2), actual: +h.actual.toFixed(2),
  }));
  const others = IDS.filter((o) => o !== id);
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-baseline justify-between">
        <h2 className="text-lg font-semibold">Fridge {id.slice(1)} <span className="text-sm font-normal text-slate-400">{f.section}</span></h2>
        <span className="text-sm text-slate-400">Target {f.target}°C</span>
      </div>
      <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
        <StateRow label="Health" value={HEALTH[f.health]} />
        <StateRow label="Security" value={SECURITY[f.security]} />
        <StateRow label="Trust" value={TRUST[trustCategory(sim, id)]} />
        <StateRow label="Network" value={CONNECTIVITY[f.connectivity]} />
        <StateRow label="Rescue" value={RESCUE[f.rescue]} />
        <div className="flex items-center justify-between rounded-lg bg-slate-800 px-3 py-1.5 text-sm">
          <span className="text-slate-400">Stock</span>
          <span className="tabular-nums text-slate-200">{f.stock} of {f.capacity} L</span>
        </div>
      </div>

      <div className="h-40">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 6, right: 6, bottom: 0, left: -18 }}>
            <ReferenceArea y1={f.target - 1} y2={f.target + 1} fill="#22c55e" fillOpacity={0.08} />
            <XAxis dataKey="time" tick={{ fill: "#64748b", fontSize: 11 }} interval={14} stroke="#334155" />
            <YAxis tick={{ fill: "#64748b", fontSize: 11 }} domain={["dataMin - 1", "dataMax + 1"]}
              tickFormatter={(v) => Math.round(v)} stroke="#334155" />
            <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155", fontSize: 12 }}
              formatter={(v) => `${v.toFixed(1)}°C`} />
            <Line type="monotone" dataKey="reported" name="Sensor" stroke="#38bdf8" strokeWidth={2} dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="estimate" name="Power-draw estimate" stroke="#f87171" strokeWidth={2} strokeDasharray="5 4" dot={false} isAnimationActive={false} />
            {showTruth && <Line type="monotone" dataKey="actual" name="Actual" stroke="#e879f9" strokeWidth={1.5} strokeDasharray="2 3" dot={false} isAnimationActive={false} />}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="flex flex-wrap gap-4 text-xs text-slate-400">
        <span className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-4 bg-sky-400" />Sensor {r1(f.reported)}°C</span>
        <span className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-4 bg-red-400" />Power-draw estimate {r1(f.estimate)}°C</span>
        {showTruth && <span className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-4 bg-fuchsia-400" />Actual {r1(f.temp)}°C</span>}
        <span>Compressor {f.compressor ? "on" : "off"}</span>
      </div>

      <div>
        <p className="mb-1.5 text-sm text-slate-400">How other fridges rate {id}</p>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1">
          {others.map((o) => {
            const v = sim.trust[o][id];
            const color = v < 0.45 ? "bg-red-400" : v < 0.75 ? "bg-amber-400" : "bg-emerald-400";
            return (
              <div key={o} className="flex items-center gap-2 text-xs">
                <span className="w-5 text-slate-400">{o}</span>
                <div className="h-1.5 flex-1 rounded bg-slate-800">
                  <div className={`h-1.5 rounded ${color}`} style={{ width: `${Math.round(v * 100)}%` }} />
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="flex flex-wrap gap-2 border-t border-slate-800 pt-3">
        <button className={btn} onClick={() => onAction((s) => spoofSensor(s, id))}><Zap className="h-4 w-4" />Spoof {id}'s sensor</button>
        <button className={btn} onClick={() => onAction((s) => badSetpoint(s, id))}><ShieldAlert className="h-4 w-4" />Send {id} a bad setpoint</button>
      </div>
    </div>
  );
}

function EventFeed({ sim, selected, showTruth, mode, setMode }) {
  const events = sim.events
    .filter((e) => (mode === "known" ? sim.known[selected].has(e.id) : !e.truth || showTruth))
    .slice(-40)
    .reverse();
  const seg = (value, label) => (
    <button onClick={() => setMode(value)}
      className={`rounded-md px-2.5 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-sky-400 ${mode === value ? "bg-slate-700 text-white" : "text-slate-400 hover:text-slate-200"}`}>
      {label}
    </button>
  );
  return (
    <div className="flex h-full flex-col">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="font-semibold">Event log</h2>
        <div className="flex rounded-lg border border-slate-700 p-0.5">
          {seg("all", "Everything")}
          {seg("known", `What ${selected} knows`)}
        </div>
      </div>
      <ul className="max-h-80 space-y-2 overflow-y-auto pr-1">
        {events.length === 0 && (
          <li className="text-sm text-slate-400">
            {mode === "known" ? `${selected} hasn't heard about any events yet.` : "All quiet. Run the demo scenario or launch an attack to see events here."}
          </li>
        )}
        {events.map((e) => (
          <li key={e.id} className="flex gap-2 text-sm leading-snug">
            <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${FEED_DOT[e.kind] || "bg-slate-400"}`} />
            <span className="w-10 shrink-0 tabular-nums text-slate-500">{fmtTime(e.t)}</span>
            <span className={e.truth ? "text-fuchsia-300" : "text-slate-200"}>
              {e.truth && <span className="font-medium">Ground truth: </span>}{e.text}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

const LIKELIHOOD_TONE = { high: "text-red-300", medium: "text-amber-300", low: "text-slate-400" };
const PRE = "mt-1 max-h-60 overflow-auto whitespace-pre-wrap rounded bg-slate-950 p-2 text-slate-300";

function LlmAnswer({ m }) {
  const d = m.diagnosis;
  const section = (title, items) => items?.length > 0 && (
    <div className="mt-2">
      <p className="mb-1 text-xs text-slate-400">{title}</p>
      <ul className="list-disc space-y-0.5 pl-5">{items.map((x, i) => <li key={i}>{x}</li>)}</ul>
    </div>
  );
  return (
    <div className="text-sm leading-relaxed text-slate-200">
      {d ? (
        <>
          <p>{d.summary}</p>
          {d.hypotheses?.length > 0 && (
            <div className="mt-2">
              <p className="mb-1 text-xs text-slate-400">Possible causes</p>
              <ul className="space-y-1.5">
                {d.hypotheses.map((h, i) => (
                  <li key={i} className="rounded-lg bg-slate-800 px-3 py-2">
                    <div className="flex justify-between gap-3">
                      <span>{h.cause}</span>
                      <span className={`shrink-0 ${LIKELIHOOD_TONE[h.likelihood] || "text-slate-400"}`}>{h.likelihood}</span>
                    </div>
                    {(h.supporting_events?.length > 0 || h.contradicting_events?.length > 0) && (
                      <p className="mt-1 text-xs text-slate-500">
                        {h.supporting_events?.length > 0 && `Supported by events ${h.supporting_events.join(", ")}. `}
                        {h.contradicting_events?.length > 0 && `Contradicted by events ${h.contradicting_events.join(", ")}.`}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {section("Recommended actions", d.recommended_actions)}
          {section("Still unknown", d.unknowns)}
        </>
      ) : (
        <p className="text-red-300">{m.error}</p>
      )}
      {m.invalid_citations?.length > 0 && (
        <p className="mt-2 text-xs text-amber-300">Cited events that don't exist: {m.invalid_citations.join(", ")}</p>
      )}
      <p className="mt-1 text-xs text-slate-500">{m.model || "Model"} answered in {(m.latency_ms / 1000).toFixed(1)} s</p>
      <details className="mt-2 rounded-lg border border-slate-800 px-3 py-2 text-xs">
        <summary className="cursor-pointer text-slate-400 hover:text-slate-200">See prompt and raw response</summary>
        <p className="mt-2 text-slate-400">Raw reply from the model</p>
        <pre className={PRE}>{m.raw || "(empty)"}</pre>
        <p className="mt-2 text-slate-400">Question and situation sent</p>
        <pre className={PRE}>{m.prompt?.user}</pre>
        <p className="mt-2 text-slate-400">System prompt</p>
        <pre className={PRE}>{m.prompt?.system}</pre>
      </details>
    </div>
  );
}

function Investigator({ chat, draft, setDraft, ask, selected, pending, backend }) {
  const endRef = useRef(null);
  useEffect(() => { endRef.current?.scrollIntoView({ block: "nearest" }); }, [chat.length, pending]);
  const presets = [`What's wrong with ${selected}?`, "Why is F3 not trusted?", "What's the rescue plan?"];
  const status = backend === null
    ? "Looking for the backend…"
    : backend
      ? `Connected to the backend, using ${backend.model}`
      : "Backend not running, so answers are mock. Start it with uvicorn main:app --reload.";
  return (
    <div className="flex h-full flex-col">
      <div className="mb-1 flex items-center gap-2">
        <Bot className="h-5 w-5 text-sky-400" />
        <h2 className="font-semibold">AI investigator</h2>
      </div>
      <p className={`mb-3 text-xs ${backend ? "text-emerald-400" : "text-slate-500"}`}>{status}</p>
      <div className="max-h-96 flex-1 space-y-3 overflow-y-auto pr-1">
        {chat.length === 0 && <p className="text-sm text-slate-400">Ask about any fridge, or about the rescue plan.</p>}
        {chat.map((m, i) => m.role === "user" ? (
          <div key={i} className="ml-auto max-w-xs rounded-lg bg-slate-800 px-3 py-2 text-sm">{m.text}</div>
        ) : m.source === "llm" ? (
          <LlmAnswer key={i} m={m} />
        ) : (
          <div key={i} className="text-sm leading-relaxed text-slate-200">
            {m.text}
            <div className="mt-1 text-xs text-slate-500">{m.basis}</div>
            {m.error && <div className="mt-1 text-xs text-amber-300">{m.error}</div>}
          </div>
        ))}
        {pending && <p className="animate-pulse text-sm text-slate-400">Investigating…</p>}
        <div ref={endRef} />
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        {presets.map((p) => (
          <button key={p} onClick={() => ask(p)}
            className="rounded-full border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:bg-slate-800 focus:outline-none focus:ring-2 focus:ring-sky-400">{p}</button>
        ))}
      </div>
      <div className="mt-2 flex gap-2">
        <input value={draft} onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") ask(draft); }}
          placeholder="Is Fridge 5 really overheating?"
          className="flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-sky-400" />
        <button className={btn} onClick={() => ask(draft)} aria-label="Send question"><Send className="h-4 w-4" /></button>
      </div>
    </div>
  );
}

function RescuePanel({ sim, onApprove }) {
  const p = sim.plan;
  return (
    <div className="flex h-full flex-col">
      <h2 className="mb-3 font-semibold">Food rescue</h2>
      {!p ? (
        <p className="text-sm text-slate-400">
          No stock at risk. When a fridge fails, its request for space and the offers from its neighbours appear here.
        </p>
      ) : (
        <div className="flex flex-col gap-2 text-sm">
          <p><span className="font-medium text-red-300">{p.total} L</span> at risk in {p.for}</p>
          {p.allocations.length === 0 && <p className="text-slate-400">Waiting for offers from neighbours.</p>}
          {p.allocations.map((a) => (
            <div key={a.id} className="flex justify-between rounded-lg bg-slate-800 px-3 py-1.5">
              <span>Move {a.litres} L to {a.id}</span>
              <span className="text-slate-400">{a.metres} m away</span>
            </div>
          ))}
          {p.excluded.map((e) => (
            <div key={e.id} className="flex justify-between rounded-lg border border-red-900 px-3 py-1.5 text-red-300">
              <span>{e.id} offered {e.litres} L</span>
              <span>Left out, not trusted</span>
            </div>
          ))}
          {p.shortfall > 0 && (
            <p className="text-amber-300">
              {p.shortfall} L has nowhere to go yet{sim.partitioned ? ". The other half of the store can't hear the request." : "."}
            </p>
          )}
          <button onClick={onApprove}
            className="mt-1 self-start rounded-lg bg-sky-500 px-3 py-1.5 text-sm font-medium text-slate-950 hover:bg-sky-400 focus:outline-none focus:ring-2 focus:ring-sky-300">
            Approve and move stock
          </button>
        </div>
      )}
    </div>
  );
}

function Metric({ label, value, sub }) {
  return (
    <div className="rounded-xl bg-slate-900 px-4 py-3">
      <p className="text-sm text-slate-400">{label}</p>
      <p className="text-2xl font-semibold tabular-nums">{value}</p>
      {sub && <p className="text-xs text-slate-500">{sub}</p>}
    </div>
  );
}

export default function FridgeShieldDashboard() {
  const simRef = useRef(null);
  if (!simRef.current) simRef.current = createSim(false);
  const [, setVersion] = useState(0);
  const [running, setRunning] = useState(true);
  const [selected, setSelected] = useState("F4");
  const [showTruth, setShowTruth] = useState(false);
  const [feedMode, setFeedMode] = useState("all");
  const [chat, setChat] = useState([]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [backend, setBackend] = useState(null); // null = checking, false = offline, object = online

  useEffect(() => {
    fetch(`${API_URL}/health`).then((r) => r.json()).then(setBackend).catch(() => setBackend(false));
  }, []);

  const refresh = () => setVersion((v) => v + 1);
  useEffect(() => {
    if (!running) return undefined;
    const handle = setInterval(() => { step(simRef.current); refresh(); }, 900);
    return () => clearInterval(handle);
  }, [running]);

  const act = (fn) => { fn(simRef.current); refresh(); };
  const restart = (script) => { simRef.current = createSim(script); setChat([]); setRunning(true); refresh(); };
  const ask = async (q) => {
    if (!q.trim() || pending) return;
    const s = simRef.current;
    const packet = buildPacket(s, targetFridge(s, q, selected));
    setDraft("");
    setChat((c) => [...c, { role: "user", text: q }]);
    setPending(true);
    try {
      const res = await fetch(`${API_URL}/investigate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q, packet }),
      });
      if (!res.ok) throw new Error(`the backend replied with status ${res.status}`);
      const data = await res.json();
      setBackend({ ok: true, model: data.model });
      setChat((c) => [...c, { role: "ai", source: "llm", ...data }]);
    } catch (err) {
      setBackend(false);
      const mock = investigate(s, q, selected);
      setChat((c) => [...c, { role: "ai", source: "mock", ...mock,
        error: `Couldn't reach the backend (${err.message}), so this is a mock answer.` }]);
    } finally {
      setPending(false);
    }
  };

  const sim = simRef.current;
  const fridges = IDS.map((id) => sim.fridges[id]);
  const atRisk = fridges.filter((f) => f.rescue === "at_risk").reduce((s, f) => s + f.stock, 0);
  const cats = IDS.map((id) => trustCategory(sim, id));
  const distrusted = cats.filter((c) => c === "distrusted").length;
  const unverified = cats.filter((c) => c === "unverified").length;
  const network = sim.partitioned ? "Partitioned" : sim.syncTicks > 0 ? "Syncing" : "Connected";

  return (
    <div className="min-h-screen bg-slate-950 p-4 text-slate-100 md:p-6">
      <style>{`
        @keyframes fs-travel { from { transform: translate(0px, 0px); opacity: 1; } to { transform: translate(var(--dx), var(--dy)); opacity: 0.3; } }
        .fs-msg { animation: fs-travel 0.75s ease-out forwards; }
        @media (prefers-reduced-motion: reduce) { .fs-msg { animation: none; opacity: 0; } }
      `}</style>
      <div className="mx-auto max-w-7xl space-y-4">
        <header className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-6 w-6 text-sky-400" />
            <h1 className="text-xl font-semibold tracking-tight">FridgeShield</h1>
          </div>
          <span className="rounded-md bg-slate-800 px-2 py-1 text-xs text-slate-300">Prototype on simulated data</span>
          <span className="text-sm tabular-nums text-slate-400">Store time {fmtTime(sim.t)}</span>
          <span className={`rounded-md px-2 py-1 text-xs ${sim.centralOnline ? "bg-slate-800 text-slate-300" : "bg-red-950 text-red-300"}`}>
            Central management {sim.centralOnline ? "online" : "locked out"}
          </span>
          <div className="ml-auto flex flex-wrap gap-2">
            <button onClick={() => restart(true)}
              className="inline-flex items-center gap-2 rounded-lg bg-sky-500 px-3 py-1.5 text-sm font-medium text-slate-950 hover:bg-sky-400 focus:outline-none focus:ring-2 focus:ring-sky-300">
              <Clapperboard className="h-4 w-4" />Run demo scenario
            </button>
            <button className={btn} onClick={() => setRunning((r) => !r)}>
              {running ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}{running ? "Pause" : "Resume"}
            </button>
            <button className={btn} onClick={() => restart(false)}><RotateCcw className="h-4 w-4" />Reset</button>
            <button className={btn} onClick={() => act((s) => setPartition(s, !s.partitioned))}>
              {sim.partitioned ? <Plug className="h-4 w-4" /> : <Unplug className="h-4 w-4" />}
              {sim.partitioned ? "Restore network" : "Cut network"}
            </button>
            <button className={btn} onClick={() => act(toggleCentral)}>
              <Lock className="h-4 w-4" />{sim.centralOnline ? "Lock out central" : "Bring central back"}
            </button>
            <button className={btn} onClick={() => setShowTruth((v) => !v)} aria-pressed={showTruth}>
              {showTruth ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              {showTruth ? "Hide ground truth" : "Show ground truth"}
            </button>
          </div>
        </header>

        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Metric label="Fridges still monitoring" value="8 of 8"
            sub={`Central-only system: ${sim.centralOnline ? 8 : 0} of 8`} />
          <Metric label="Stock at risk" value={`${atRisk} L`} />
          <Metric label="Fridges not trusted" value={distrusted + unverified}
            sub={`${distrusted} distrusted, ${unverified} unverified`} />
          <Metric label="Network" value={network} sub={`${sim.reconciled} events reconciled`} />
        </div>

        <div className="grid gap-4 lg:grid-cols-3">
          <section className={`${card} lg:col-span-2`}>
            <FloorPlan sim={sim} selected={selected} onSelect={setSelected} showTruth={showTruth} />
            <Legend showTruth={showTruth} />
          </section>
          <section className={card}>
            <FridgeDetail sim={sim} id={selected} showTruth={showTruth} onAction={act} />
          </section>
        </div>

        <div className="grid gap-4 lg:grid-cols-3">
          <section className={card}>
            <EventFeed sim={sim} selected={selected} showTruth={showTruth} mode={feedMode} setMode={setFeedMode} />
          </section>
          <section className={card}>
            <Investigator chat={chat} draft={draft} setDraft={setDraft} ask={ask} selected={selected}
              pending={pending} backend={backend} />
          </section>
          <section className={card}>
            <RescuePanel sim={sim} onApprove={() => act(approvePlan)} />
          </section>
        </div>
      </div>
    </div>
  );
}
