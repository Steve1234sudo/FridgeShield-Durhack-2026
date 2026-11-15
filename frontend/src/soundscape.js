/*
  FridgeShield soundscape, made with Strudel (https://strudel.cc)
  ===============================================================
  Two layers, each telling you something different:
  - The piano chord tells you WHICH fridge is in trouble. Every fridge plays one note and they all
    strike together. When a fridge is attacked it switches to its "attacked" note, which clashes.
  - The rhythm tells you HOW BAD things are overall. A steady hi-hat ticks while all is well; as
    more of the network is attacked the hi-hat stumbles and a deep, uneasy bass comes in.
  You can hear that something is wrong without looking at the dashboard.

  Everything you'd want to change is in the "YOUR MUSIC" section below. Try ideas in the Strudel
  REPL at https://strudel.cc first. The track is rebuilt from the fridges' state on every update,
  but only re-played when the music actually changes.
*/

// ======================= YOUR MUSIC =======================

// Each fridge's note: [when healthy, when attacked]. Fridge 1 uses the first row, and so on;
// with more fridges than rows, the list starts again. The healthy notes are ordered so the chord
// sounds good at any size: 2 fridges = C and G, 3 = C major, 4 = C major 7, more = C major 9.
// Each attacked note is a semitone away and outside the key of C major, so it clashes.
export const FRIDGE_NOTES = [
  ["c3", "c#3"],  // fridge 1
  ["g3", "g#3"],  // fridge 2
  ["e4", "eb4"],  // fridge 3
  ["b4", "bb4"],  // fridge 4
  ["d5", "eb5"],  // fridge 5
  ["g4", "g#4"],  // fridge 6
  ["c5", "c#5"],  // fridge 7
  ["e5", "eb5"],  // fridge 8
  ["a4", "bb4"],  // fridge 9
  ["g5", "g#5"],  // fridge 10
  ["b5", "bb5"],  // fridge 11
  ["d6", "c#6"],  // fridge 12
];

// How often the chord is struck, in Strudel cycles (one cycle is about 2 seconds).
export const CHORD_EVERY = 1;

// The instrument. "piano" downloads Strudel's grand piano samples when you press play (needs
// internet); if that fails it falls back to FALLBACK_SOUND, a built-in synth that always works.
export const SOUND = "piano";
export const FALLBACK_SOUND = "triangle";
const PIANO_SAMPLES = "https://raw.githubusercontent.com/felixroos/dough-samples/main/piano.json";

// ---- The rhythm: how the network is doing overall ----
// Both are built from Strudel's own synths, so they work without internet (unlike the piano).

// When all is well: a steady hi-hat, eight light ticks per cycle (short bursts of filtered noise).
// As fridges are attacked, ticks randomly drop out in proportion: with 1 of 4 attacked, about a
// quarter go missing, so the beat stumbles more the worse things get.
export const HIHAT = `s("white*8").decay(0.04).sustain(0).hpf(8000).gain(0.08)`;

// When fridges are attacked: a deep bass on F#, the note furthest from C, so it sits uneasily
// under the chord. One pattern per level of trouble (see tensionLevel): more insistent each time.
// (A sawtooth through a low filter, so it's audible on laptop speakers that can't play deep sine waves.)
export const BASS = [
  `note("f#1").s("sawtooth").lpf(400).decay(1.5).sustain(0).gain(0.3)`,      // one fridge attacked: once a cycle
  `note("f#1*2").s("sawtooth").lpf(450).decay(0.9).sustain(0).gain(0.35)`,   // two attacked: twice
  `note("f#1*4").s("sawtooth").lpf(500).decay(0.45).sustain(0).gain(0.4)`,   // three or more: pulsing
];

// How bad things are, for the bass: one level per attacked fridge, up to 3.
// (The hi-hat separately thins out by the share of the network that's attacked.)
export function tensionLevel(attacked) {
  return Math.min(3, attacked);
}

// When fridges are attacked: crows, from Strudel's classic sample pack (four different calls,
// picked at random). Louder, and more of them, with every extra attack. They land between the
// chords rather than on them. Needs internet to download, like the piano.
export const CROWS = [
  `s("<~ crow>").late(0.5).n(irand(4)).gain(0.3).room(0.5)`,           // one fridge attacked: an occasional quiet call
  `s("~ crow").n(irand(4)).gain(0.5).room(0.5)`,                        // two attacked: every cycle, louder
  `s("~ crow ~ crow").n(irand(4)).gain(0.75).room(0.5).pan(rand)`,      // three or more: twice a cycle, loudest, from all sides
];
const DIRT_SAMPLES = "https://raw.githubusercontent.com/felixroos/dough-samples/main/Dirt-Samples.json";

// Optional backing track that plays the whole time, e.g. `note("c2").s("sawtooth").lpf(300).gain(0.1)`.
// Leave as "" for none. Keep it in C major so it suits the chord.
export const BACKING = "";

// =================== HOW STATES SOUND ===================

// Under attack: its sensor is in doubt (suspicious, or defending itself in protective mode),
// or its target temperature has been overridden.
export const underAttack = (l) => l.security !== "normal" || l.target_overridden;

let sound = SOUND;
let crowsLoaded = true; // set to false if the crow samples can't be downloaded

// How warped an attacked fridge's note gets, by level (1, 2, 3+ fridges attacked). The note
// warbles harder, drifts further out of tune, stumbles in later than the chord, and at the
// highest level gets a lo-fi crunch. Each fridge is offset a little differently so several
// attacked notes smear apart instead of landing together.
export const WARP = [
  { wobbleSpeed: 6, wobbleDepth: 0.3, sourer: 0, late: 0.05, crush: 0 },       // one fridge attacked
  { wobbleSpeed: 7, wobbleDepth: 0.5, sourer: 0.15, late: 0.1, crush: 0 },     // two
  { wobbleSpeed: 8, wobbleDepth: 0.7, sourer: 0.3, late: 0.15, crush: 6 },     // three or more
];

// One fridge's note. Returns a line of Strudel code.
export function voice(node, index, count, level = 0) {
  const l = node.local;
  const [healthy, attacked] = FRIDGE_NOTES[index % FRIDGE_NOTES.length];
  let pitch = `"${healthy}"`;
  let gain = Math.min(0.6, 0.9 / Math.sqrt(Math.max(1, count)));
  let extra = "";
  if (node.isolated) {
    // Cut off and in safe mode: its note is fine, but it sounds distant and arrives late,
    // like an echo that's lost touch with the others.
    gain *= 0.4;
    extra = ".room(0.9).late(0.25)";
  } else if (underAttack(l)) {
    pitch = `"${attacked}"`;
    gain *= 1.2; // a touch louder, so the wrong note stands out
    const w = WARP[Math.min(WARP.length, Math.max(1, level)) - 1];
    const late = w.late + 0.02 * (index % 3); // a slightly different stumble for each fridge
    extra = `.vib(${w.wobbleSpeed}).vibmod(${w.wobbleDepth})`   // warble, like a warped tape
      + (w.sourer ? `.add(note(${w.sourer}))` : "")              // drifting further out of tune
      + `.late(${late.toFixed(2)})`                               // stumbling in behind the chord
      + (w.crush ? `.crush(${w.crush})` : "");                    // lo-fi crunch
  } else if (l.door) {
    // A door left open isn't an attack: the right note, just slightly flat.
    extra = ".add(note(-0.2))";
  }
  return `note(${pitch}).s("${sound}").gain(${gain.toFixed(3)}).room(0.3)${extra}`;
}

const fridgeNumber = (id) => Number(String(id).replace(/\D/g, "")) || 0;

// The whole track: the chord (one note per fridge, struck together), the rhythm, and the backing.
export function buildTrack(nodes) {
  const ordered = [...nodes].sort((a, b) => fridgeNumber(a.id) - fridgeNumber(b.id));
  // Cut-off fridges are safe (safe mode), so only connected fridges count as attacked.
  const live = ordered.filter((n) => n.local && !n.local.paused);
  const attacked = live.filter((n) => !n.isolated && underAttack(n.local)).length;
  const level = tensionLevel(attacked);
  const voices = ordered
    .map((node, index) => (node.local && !node.local.paused ? voice(node, index, ordered.length, level) : null))
    .filter(Boolean);
  const chord = `stack(\n    ${voices.join(",\n    ") || "silence"}\n  )${CHORD_EVERY !== 1 ? `.slow(${CHORD_EVERY})` : ""}`;

  const share = attacked / Math.max(1, live.length);
  const hihat = share > 0 ? `${HIHAT}.degradeBy(${share.toFixed(2)})` : HIHAT;
  const bass = level > 0 ? [BASS[level - 1]] : [];
  const crows = level > 0 && crowsLoaded ? [CROWS[level - 1]] : [];

  const layers = [chord, hihat, ...bass, ...crows, ...(BACKING ? [BACKING] : [])];
  return `stack(\n  ${layers.join(",\n  ")}\n)`;
}

// ======================= PLAYBACK =======================
// Strudel is only downloaded when you first press play.
let strudel = null;
let playing = false;
let current = "";

export async function startSoundscape(nodes) {
  if (!strudel) {
    const module = await import("@strudel/web");
    // Must run from a click: browsers block audio until the user interacts.
    await module.initStrudel({
      prebake: async () => {
        await Promise.all([
          SOUND === "piano" && module.samples(PIANO_SAMPLES).catch((error) => {
            console.warn("Soundscape: couldn't load the piano, using a synth instead", error);
            sound = FALLBACK_SOUND;
          }),
          module.samples(DIRT_SAMPLES).catch((error) => {
            console.warn("Soundscape: couldn't load the crow sounds, carrying on without them", error);
            crowsLoaded = false;
          }),
        ]);
      },
    });
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