import test from "node:test";
import assert from "node:assert/strict";
import { investigationPacket } from "../src/investigationPacket.js";

test("investigation uses selected-node evidence without hidden simulation state", () => {
  const node = {
    id: "F2", neighbours: ["F1"], connected: ["F1"], discovery_online: true,
    local: { t: 12, reported: 2, estimate: 6, actual: 9, spoofed: true, wearing: true,
      health: "failing", history: [{ t: 12, reported: 2, estimate: 6, actual: 9 }] },
    events: [{ kind: "alert", origin: "F2", session: "session", seq: 2, data: { security: "protective" } },
      { kind: "telemetry", data: { actual: 9 } }],
  };
  const packet = investigationPacket(node);
  assert.equal(packet.fridge_id, "F2");
  assert.equal(packet.readings.estimate, 6);
  assert.equal(packet.events.length, 1);
  assert.equal(packet.events[0].id, 1);
  for (const hidden of ["actual", "spoofed", "wearing"]) assert.equal(JSON.stringify(packet).includes(`"${hidden}"`), false);
  assert.equal(node.events[0].id, undefined);
});
