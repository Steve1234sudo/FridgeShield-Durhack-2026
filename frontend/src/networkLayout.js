// Deterministic scattered positions, with clearance for cabinets, open doors and labels.
// Existing nodes keep their coordinates when membership changes; telemetry never moves them.
function randomFor(id) {
  let seed = 2166136261;
  for (const character of id) seed = Math.imul(seed ^ character.charCodeAt(0), 16777619);
  return () => {
    seed += 0x6d2b79f5;
    let value = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    value ^= value + Math.imul(value ^ (value >>> 7), 61 | value);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

export function arrangeFridges(ids, previous) {
  if (!ids.length) return { positions: {}, width: 560, height: 420 };
  let width = previous?.width || 560;
  let height = previous?.height || 420;
  const positions = Object.fromEntries(ids.filter((id) => previous?.positions[id])
    .map((id) => [id, previous.positions[id]]));
  for (const id of [...ids].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))) {
    if (positions[id]) continue;
    const random = randomFor(id);
    let placed = false;
    while (!placed) {
      for (let attempt = 0; attempt < 256; attempt++) {
        const point = { x: 70 + random() * (width - 140), y: 80 + random() * (height - 160) };
        if (Object.values(positions).every((other) => Math.abs(point.x - other.x) >= 100 || Math.abs(point.y - other.y) >= 125)) {
          positions[id] = point;
          placed = true;
          break;
        }
      }
      if (!placed) {
        if (width / height < 1.4) width += 160;
        else height += 160;
      }
    }
  }
  return { positions, width, height };
}
