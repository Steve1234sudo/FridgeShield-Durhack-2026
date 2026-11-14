"""Shared physical cabinet and Kalman observer used by both backend modes."""
from __future__ import annotations
import random
from dataclasses import dataclass
from typing import Optional
AMBIENT_TEMP_C = 19.0
DT_SECONDS = 60.0

@dataclass
class PhysicalCabinet:
    """
    Ground-truth thermodynamic cabinet and physical sensor transducers.
    The edge controller NEVER reads `true_temp` or `wear` directly—only
    sensor outputs (`read_thermistor`, `read_power_watts`, `door_open`).
    """

    fridge_id: str
    target: float
    frozen: bool
    capacity: int
    stock: int
    rng: random.Random
    true_temp: float = 0.0
    compressor_on: bool = False
    door_open: bool = False
    door_ticks: int = 0
    wear: float = 0.0          # [0.0, 0.4] mechanical/refrigerant degradation
    wearing: bool = False
    spoofed: bool = False
    external_override: bool = False
    last_power_watts: float = 0.0
    last_reported_temp: float = 0.0

    # Thermodynamic parameters
    thermal_cap_kj_per_k: float = 120.0   # Cabinet air + product thermal mass
    ua_w_per_k: float = 60.0              # Insulation heat transfer coefficient (W/K)
    nominal_power_w: float = 450.0        # Rated compressor electrical draw (W)
    nominal_cop: float = 4.0              # Rated Coefficient of Performance

    def __post_init__(self) -> None:
        if self.frozen:
            self.nominal_power_w = 850.0
            self.nominal_cop = 5.1765
        self.true_temp = self.target + self.rng.uniform(-0.3, 0.3)
        self.last_reported_temp = self.true_temp

    def step_physics(self, ambient_temp: float = AMBIENT_TEMP_C) -> tuple[float, float, bool]:
        """Advance physical heat transfer by DT_SECONDS and sample hardware sensors."""
        if self.wearing:
            self.wear = min(0.4, self.wear + 0.02)

        # Heat ingress through insulation + convective infiltration if door is open
        q_leak_w = self.ua_w_per_k * (ambient_temp - self.true_temp)
        q_door_w = (0.8 * self.thermal_cap_kj_per_k * 1000.0 / DT_SECONDS) if self.door_open else 0.0

        # Compressor electrical draw rises with mechanical friction, while effective cooling drops
        if self.compressor_on:
            power_watts = self.nominal_power_w * (1.0 + 0.45 * self.wear) + self.rng.uniform(-8.0, 8.0)
            q_cool_w = self.nominal_cop * self.nominal_power_w * (1.0 - 0.6 * self.wear)
        else:
            power_watts = max(0.0, self.rng.uniform(0.0, 4.0))  # Standby control board draw
            q_cool_w = 0.0

        net_heat_joules = (q_leak_w + q_door_w - q_cool_w) * DT_SECONDS
        delta_t = net_heat_joules / (self.thermal_cap_kj_per_k * 1000.0)
        self.true_temp += delta_t + self.rng.uniform(-0.04, 0.04)

        # Sample thermistor (subject to MITM/firmware sensor spoofing)
        if self.spoofed:
            reported = self.target - 1.0 + self.rng.uniform(-0.1, 0.1)
        else:
            reported = self.true_temp + self.rng.uniform(-0.06, 0.06)

        self.last_power_watts = power_watts
        self.last_reported_temp = reported
        return reported, power_watts, self.door_open


# ---------------------------------------------------------------------------
# Edge Controller: 1D Kalman Thermal Observer + Online COP Wear Estimator
# ---------------------------------------------------------------------------


class ThermalKalmanObserver:
    """
    Estimates internal cabinet temperature strictly from electrical power draw,
    door reed state, and ambient temperature—independent of the thermistor.
    """

    def __init__(
            self,
            initial_temp: float,
            frozen: bool,
            thermal_cap_kj_per_k: float = 120.0,
            ua_w_per_k: float = 60.0,
    ) -> None:
        self.x_hat: float = initial_temp
        self.p: float = 0.25
        self.q: float = 0.02   # Process noise covariance
        self.r: float = 0.04   # Thermistor measurement noise covariance
        self.c_j_per_k: float = thermal_cap_kj_per_k * 1000.0
        self.ua_w_per_k: float = ua_w_per_k
        self.nominal_power_w: float = 850.0 if frozen else 450.0
        self.nominal_cop: float = 5.1765 if frozen else 4.0

        # Online COP degradation tracking
        self.cop_estimate: float = self.nominal_cop
        self.estimated_wear: float = 0.0
        self._prev_trusted_temp: Optional[float] = None

    def update(
            self,
            reported_temp: float,
            power_watts: float,
            door_open: bool,
            ambient_temp: float,
            trust_sensor: bool,
    ) -> tuple[float, float]:
        """
        Runs Kalman predict step from electrical power & thermal model.
        Fuses `reported_temp` ONLY when `trust_sensor` is True and innovation is within gate.
        Returns (estimated_temp, innovation_residual).
        """
        # 1. Process Model Prediction (purely from power draw & thermodynamics)
        q_leak_w = self.ua_w_per_k * (ambient_temp - self.x_hat)
        q_door_w = (0.8 * self.c_j_per_k / DT_SECONDS) if door_open else 0.0

        # Active compressor cooling estimated from electrical wattage above standby (> 50 W)
        active_watts = max(0.0, power_watts - 10.0) if power_watts > 50.0 else 0.0
        q_cool_w = self.cop_estimate * active_watts

        x_pred = self.x_hat + ((q_leak_w + q_door_w - q_cool_w) * DT_SECONDS) / self.c_j_per_k
        p_pred = self.p + self.q

        innovation = abs(reported_temp - x_pred)

        # 2. Gated Measurement Fusion
        if trust_sensor and innovation <= 2.5:
            k_gain = p_pred / (p_pred + self.r)
            self.x_hat = x_pred + k_gain * (reported_temp - x_pred)
            self.p = (1.0 - k_gain) * p_pred

            # Online COP estimation during closed-door active cooling cycles
            if not door_open and active_watts > 100.0 and self._prev_trusted_temp is not None:
                actual_net_w = self.c_j_per_k * (reported_temp - self._prev_trusted_temp) / DT_SECONDS
                implied_cool_w = q_leak_w - actual_net_w
                instant_cop = max(0.5, min(self.nominal_cop * 1.2, implied_cool_w / active_watts))
                self.cop_estimate = 0.85 * self.cop_estimate + 0.15 * instant_cop
                cop_ratio = self.cop_estimate / self.nominal_cop
                # Map COP drop into normalized wear [0, 0.4]
                self.estimated_wear = max(0.0, min(0.4, (1.0 - cop_ratio) / 0.85))

            self._prev_trusted_temp = reported_temp
        else:
            # Open-loop observer mode: rely solely on thermodynamic power model
            self.x_hat = x_pred
            self.p = p_pred
            self._prev_trusted_temp = None

        return self.x_hat, innovation


