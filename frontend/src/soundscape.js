/*
  FridgeShield soundscape, made with Strudel (https://strudel.cc)
  ===============================================================
  Every fridge is a voice in one chord. Healthy fridges play in tune, each on its own rhythm,
  and together they sound harmonious. A fridge that's under attack drifts out of tune, so you
  can hear that something is wrong without looking at the dashboard.

  To make it your own:
  1. Compose a backing track in the Strudel REPL at https://strudel.cc and paste it into
     BACKING below. Keep it in C major (or change CHORD to your key) so the fridges harmonise.
  2. Change CHORD to give the fridges different notes.
  3. Change how each state sounds in voice().

  The track is rebuilt from the fridges' state on every update, but only re-played when the
  music actually changes, so it never stutters.
*/

// Your track: anything that works as a single pattern in the Strudel REPL works here.
export const BACKING = `note("[c2,g2]").s("sawtooth").lpf(350).gain(0.12).slow(4)`;

// Set to true if BACKING uses samples such as s("bd sd"): this downloads the classic
// Tidal sample pack when you press play (needs internet). Synths like "sawtooth" always work.
export const LOAD_SAMPLES = false;

// The fridges' notes (MIDI numbers: 60 is middle C). A spread C major 9 chord.
// More fridges than notes? The list repeats an octave higher.
export const CHORD = [60, 64, 67, 71, 74, 69, 72, 76];
// Each fridge gets its own rhythm: this many hits out of 8, rotated so they interlock.
const PULSES = [3, 5, 2, 4, 3, 2, 5, 4];

const OUT_OF_TUNE = 0.45; // a quarter-tone: clearly sour
const SLIGHTLY_OFF = 0.2;

const fridgeNumber = (id) => Number(String(id).replace(/\D/g, "")) || 0;

// One fridge's voice, from its state. Returns a line of Strudel code.
export function voice(node, index, count) {
  const l = node.local;
  const pitch = CHORD[index % CHORD.length] + 12 * Math.min(1, Math.floor(index / CHORD.length));
  let detune = 0;
  let gain = 0.5 / Math.sqrt(Math.max(1, count));
  let filter = 2400;
  let room = 0.3;
  let late = 0;
  if (node.isolated) {
    // Cut off and in safe mode: still in tune, but distant and drifting out of time.
    gain *= 0.45; room = 0.9; late = 0.07;
  } else if (l.security === "suspicious" || l.target_overridden) {
    detune = OUT_OF_TUNE;
  } else if (l.security === "protective") {
    // Defending itself: nearly back in tune, but muffled.
    detune = 0.12; filter = 700;
  } else if (l.door) {
    detune = SLIGHTLY_OFF;
  }
  const note = Number((pitch + detune).toFixed(2));
  const rhythm = `${note}(${PULSES[index % PULSES.length]},8,${index})`;
  return `note("${rhythm}").s("triangle").decay(0.5).sustain(0).gain(${gain.toFixed(3)})`
    + `.lpf(${filter}).room(${room})${late ? `.late(${late})` : ""}`;
}

// The whole track: your backing plus one voice per fridge (paused fridges fall silent).
export function buildTrack(nodes) {
  const ordered = [...nodes].sort((a, b) => fridgeNumber(a.id) - fridgeNumber(b.id));
  const voices = ordered
    .map((node, index) => (node.local && !node.local.paused ? voice(node, index, ordered.length) : null))
    .filter(Boolean);
  return `stack(\n  ${[BACKING, ...voices].join(",\n  ")}\n)`;
}

// Playback. Strudel is only downloaded when you first press play.
let strudel = null;
let playing = false;
let current = "";

export async function startSoundscape(nodes) {
  if (!strudel) {
    const module = await import("@strudel/web");
    // Must run from a click: browsers block audio until the user interacts.
    await module.initStrudel(LOAD_SAMPLES ? { prebake: () => module.samples("github:tidalcycles/dirt-samples") } : {});
    strudel = module;
  }
  playing = true;
  current = "";
  await updateSoundscape(nodes);
}

export async function updateSoundscape(nodes) {
  if (!playing || !strudel) return;
  const code = buildTrack(nodes);
  if (code === current) return; // nothing changed musically
  current = code;
  try {
    await strudel.evaluate(code);
  } catch (error) {
    console.error("Soundscape: Strudel couldn't play this track", error, code);
  }
}

export function stopSoundscape() {
  playing = false;
  current = "";
  strudel?.hush();
}
