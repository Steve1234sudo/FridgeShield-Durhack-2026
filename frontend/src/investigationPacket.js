// Only information visible to this fridge; never send simulation ground truth.
export function investigationPacket(node) {
  const local = node.local || {};
  const fields = ["t", "target", "reported", "estimate", "power_watts", "door", "compressor",
    "capacity", "stock", "frozen", "health", "security", "rescue", "control_source", "sensor_gap"];
  return {
    fridge_id: node.id,
    readings: Object.fromEntries(fields.filter((key) => local[key] !== undefined).map((key) => [key, local[key]])),
    neighbours: node.neighbours,
    connected: node.connected,
    discovery_online: node.discovery_online,
    history: (local.history || []).map(({ t, reported, estimate }) => ({ t, reported, estimate })),
    events: (node.events || []).filter((event) => event.kind !== "telemetry").map((event, index) => ({
      id: index + 1, origin: event.origin, session: event.session, seq: event.seq,
      kind: event.kind, data: event.data,
    })),
  };
}
