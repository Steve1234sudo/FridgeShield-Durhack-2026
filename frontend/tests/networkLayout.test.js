import test from "node:test";
import assert from "node:assert/strict";
import { arrangeFridges } from "../src/networkLayout.js";

test("layout is deterministic and independent of incoming ordering", () => {
  assert.deepEqual(arrangeFridges(["F1", "F2", "F3"]), arrangeFridges(["F3", "F1", "F2"]));
});

test("adding and removing fridges preserves surviving coordinates", () => {
  const original = arrangeFridges(["F1", "F2", "F3"]);
  const added = arrangeFridges(["F0", "F1", "F2", "F3"], original);
  for (const id of ["F1", "F2", "F3"]) assert.deepEqual(added.positions[id], original.positions[id]);
  const removed = arrangeFridges(["F0", "F1", "F3"], added);
  assert.equal(removed.positions.F2, undefined);
  assert.deepEqual(removed.positions.F1, original.positions.F1);
});

test("growing fleets keep cabinets and labels separated inside the canvas", () => {
  let layout = arrangeFridges([]);
  for (let count = 1; count <= 64; count++) {
    const ids = Array.from({ length: count }, (_, index) => `F${index + 1}`);
    layout = arrangeFridges(ids, layout);
    const points = Object.values(layout.positions);
    for (const point of points) {
      assert.ok(point.x >= 70 && point.x <= layout.width - 70);
      assert.ok(point.y >= 80 && point.y <= layout.height - 80);
    }
    for (let a = 0; a < points.length; a++) for (let b = a + 1; b < points.length; b++) {
      assert.ok(Math.abs(points[a].x - points[b].x) >= 100 || Math.abs(points[a].y - points[b].y) >= 125);
    }
  }
});
