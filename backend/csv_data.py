"""Replay real refrigerated display case data for a fridge.

Each fridge picks a random number from 1 to 33 and loads backend/data/fridge_data_0XX.csv
(or FRIDGE_DATA_DIR), starting from a random row and looping at the end. If that file is
missing it picks from the files that exist; with no files at all, the simulator is used.

Columns (temperatures in °F, converted to °C; the timestamp is ignored):
  W_MT-COMP1        medium-temperature rack compressor power in kW (shared by several cases)
  T-MTCase-Sup      supply air blown into the case: used as the fridge's temperature sensor
  T-MTCase-Ret      return air from the case: an independent sensor in the same case
  T-MTCase-Suc-Srf  suction line surface temperature (not used yet)

The recording is the fridge's normal behaviour. Attacks and faults add extra warming on top,
which climbs quickly and levels off like a real cabinet that has lost cooling, and fades away
once the cause is fixed. Each fridge calibrates its spoofing detector from its own file: it
learns how supply air normally relates to return air, and how much that normally varies.
"""
import csv
import math
import os
import random
from pathlib import Path

DATA_DIR = Path(os.getenv("FRIDGE_DATA_DIR", Path(__file__).resolve().parent / "data"))
FILE_COUNT = 33
# Share of the remaining gap closed each tick while cooling is disrupted. Lower it to make
# attacks warm the fridge more slowly (e.g. 0.03 for a demo, so the problem lasts longer).
WARMING_RATE = float(os.getenv("FRIDGE_WARMING_RATE", "0.07"))
COOLING_RATE = 0.88  # extra warming kept each tick once the cause is fixed
DOOR_WARMING_C = 8.0
NO_COOLING_WARMING_C = 9.0


def to_celsius(fahrenheit):
    return (fahrenheit - 32) * 5 / 9


def choose_file(rng):
    """A random fridge_data_001..033.csv, or any existing data file, or None."""
    wanted = DATA_DIR / f"fridge_data_{rng.randint(1, FILE_COUNT):03d}.csv"
    if wanted.exists():
        return wanted
    existing = sorted(DATA_DIR.glob("fridge_data_*.csv")) if DATA_DIR.exists() else []
    return rng.choice(existing) if existing else None


def load(path):
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                rows.append((float(row["W_MT-COMP1"]), to_celsius(float(row["T-MTCase-Sup"])),
                             to_celsius(float(row["T-MTCase-Ret"]))))
            except (KeyError, TypeError, ValueError):
                continue  # skip blank or broken rows
    if len(rows) < 20:
        raise ValueError(f"{path.name} has too few usable rows")
    return rows


def percentile(values, share):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


class CsvCabinet:
    """Same interface as the simulator's PhysicalCabinet, driven by recorded data."""

    def __init__(self, path, rng):
        self.file = path.name
        self.rows = load(path)
        self.rng = rng
        self.i = rng.randrange(len(self.rows))
        supply = [r[1] for r in self.rows]
        returned = [r[2] for r in self.rows]
        # Fixed chart scale for this fridge: the full range of its recorded temperatures.
        self.chart_range = [math.floor(min(supply)), math.ceil(max(supply))]
        median = percentile(supply, 0.5)
        # Defrost: supply air far above normal running. Real controllers schedule their own
        # defrosts, so the fridge legitimately knows when one is happening.
        self.defrost_above = median + 6
        running = [s for s in supply if s < self.defrost_above]
        self.target = float(round(percentile(running, 0.5)))
        self.safe_target = self.target
        # Learn how supply air normally relates to return air (least squares fit).
        n = len(supply)
        mean_r, mean_s = sum(returned) / n, sum(supply) / n
        spread = sum((r - mean_r) ** 2 for r in returned) or 1.0
        self.slope = sum((r - mean_r) * (s - mean_s) for r, s in zip(returned, supply)) / spread
        self.intercept = mean_s - self.slope * mean_r
        errors = [abs(s - (self.slope * r + self.intercept)) for r, s in zip(returned, supply)]
        # Thresholds for this fridge, set just outside its normal behaviour.
        self.mismatch_c = max(2.5, percentile(errors, 0.995) + 0.75)
        self.agree_c = max(1.0, 1.5 * (sum(e * e for e in errors) / n) ** 0.5)
        ranges = [max(supply[i:i + 5]) - min(supply[i:i + 5]) for i in range(n - 4)]
        self.change_c = max(0.05, 0.5 * percentile(ranges, 0.1))
        self.frozen = False
        self.spoofed = False
        self.door_open = False
        self.compressor_on = False
        self.controller_uses_sensor = True
        self.offset = 0.0
        self.defrost = False
        self.true_temp = supply[self.i]
        self.return_air = returned[self.i]

    def step_physics(self):
        power_kw, supply, returned = self.rows[self.i]
        self.i = (self.i + 1) % len(self.rows)
        # Cooling is lost if the target was overridden upwards, or if a spoofed sensor is
        # fooling the controller into thinking the fridge is already cold enough.
        cooling_lost = self.target > self.safe_target + 2 or (self.spoofed and self.controller_uses_sensor)
        ceiling = max(DOOR_WARMING_C if self.door_open else 0.0, NO_COOLING_WARMING_C if cooling_lost else 0.0)
        if ceiling:
            self.offset += WARMING_RATE * (ceiling - self.offset)
        else:
            self.offset *= COOLING_RATE
        self.defrost = supply > self.defrost_above
        self.true_temp = supply + self.offset
        # Return air warms in the ratio seen in the data, so the independent estimate stays honest.
        self.return_air = returned + self.offset / (self.slope or 1.0)
        if self.spoofed:
            reported = self.target - 1.0 + self.rng.uniform(-0.01, 0.01)  # stuck "slightly cold" reading
        else:
            reported = self.true_temp
        return reported, power_kw * 1000, self.door_open

    def independent_estimate(self):
        """Supply air temperature predicted from the return air sensor in the same case."""
        return self.slope * self.return_air + self.intercept


def data_cabinet(fid):
    """A CsvCabinet for this fridge, or None to use the simulator."""
    if os.getenv("FRIDGE_USE_DATA", "1") == "0":
        return None
    seed = os.getenv("FRIDGE_DATA_SEED")
    rng = random.Random(f"{seed}:{fid}") if seed else random.Random()
    path = choose_file(rng)
    return CsvCabinet(path, rng) if path else None
