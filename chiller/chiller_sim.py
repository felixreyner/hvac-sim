#!/usr/bin/env python3
"""
Chiller plant simulation exposing a Modbus TCP slave.

Models one water-cooled chiller (500 kW nominal) with its own unit
controller, the chilled-water pump, the building cooling load and the
cooling tower. The S7 PLC is the supervisory controller and talks to this
process over Modbus TCP, the same way a real PLC talks to a chiller's
BMS/Modbus gateway.

Register map (unit id 1, 0-based protocol addresses) -> see README.md
"""

import asyncio
import logging
import math
import os
import random
import time

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer

log = logging.getLogger("chiller")

# ---- Modbus map --------------------------------------------------------
# Holding registers (written by the PLC)
HR_ENABLE = 0          # 0/1 chiller enable
HR_SETPOINT = 1        # CHWS setpoint, degC x10
HR_DEMAND_LIMIT = 2    # % (10..100)
HR_ALARM_RESET = 3     # write 1 to reset, sim writes back 0
HR_PUMP_CMD = 4        # 0/1 chilled water pump command
HR_HEARTBEAT = 5       # PLC increments every scan
HR_FAULT_INJECT = 10   # bit0 tower failure, bit1 pump/flow failure, bit2 low refrigerant

# Input registers (read by the PLC). Signed values are two's complement.
IR_CHWS = 0            # chilled water supply temp, degC x10
IR_CHWR = 1            # chilled water return temp, degC x10
IR_CWE = 2             # condenser water entering, degC x10
IR_CWL = 3             # condenser water leaving, degC x10
IR_OAT = 4             # outdoor air temp, degC x10
IR_CAPACITY = 5        # compressor capacity, % x10
IR_POWER = 6           # compressor power, kW x10
IR_COOLING = 7         # cooling delivered, kW x10
IR_COP = 8             # COP x100
IR_EVAP_P = 9          # evaporator pressure, kPa
IR_COND_P = 10         # condenser pressure, kPa
IR_STATUS = 11         # status word, see STS_*
IR_ALARM = 12          # active alarm code, see ALARMS
IR_RUN_HOURS = 13      # compressor run hours (sim time)
IR_STARTS = 14         # compressor starts
IR_FLOW = 15           # chilled water flow, l/s x10
IR_LOAD = 16           # building load demand, kW x10
IR_COUNT = 17

STS_RUNNING = 1 << 0
STS_ALARM = 1 << 1
STS_FLOW_OK = 1 << 2
STS_PUMP_ON = 1 << 3
STS_LOCAL = 1 << 4         # PLC heartbeat lost, running on local fallback
STS_ANTI_RECYCLE = 1 << 5
STS_ENABLED = 1 << 6

ALARMS = {
    0: "none",
    1: "evaporator flow loss",
    2: "low evaporator temperature (freeze protection)",
    3: "high condenser pressure",
}

# ---- Plant constants ---------------------------------------------------
CP = 4.186                  # kJ/kg.K
Q_RATED = 500.0             # kW cooling
CHW_FLOW = 24.0             # kg/s design (~ l/s)
CW_FLOW = 30.0              # kg/s condenser water
C_EVAP = 1500 * CP          # kJ/K, water in evaporator + headers
C_DIST = 8000 * CP          # kJ/K, chilled water distribution loop
MIN_PLR = 0.20
RAMP_PER_S = 0.5 / 100.0    # 0.5 %/s capacity ramp
KP, TI = 0.25, 90.0         # unit PI (fraction per K, s)
ANTI_RECYCLE_S = float(os.getenv("ANTI_RECYCLE_S", "60"))
HEARTBEAT_TIMEOUT_S = 20.0
FALLBACK_SP = 7.0
COND_P_TRIP = 1400.0        # kPa
EVAP_T_TRIP = -0.5          # degC refrigerant
DAY_LENGTH_S = float(os.getenv("DAY_LENGTH_S", "1440"))  # one sim day = 24 min
DT = 0.2


def sat_pressure_r134a(t_c):
    """Rough R134a saturation pressure in kPa (Clausius-Clapeyron fit)."""
    return math.exp(15.42 - 2660.0 / (t_c + 273.15))


def to_u16(x):
    return int(round(x)) & 0xFFFF


class ChillerPlant:
    def __init__(self):
        self.t0 = time.monotonic()
        self.chws = 10.0
        self.chwr = 10.5
        self.cwe = 26.0
        self.cwl = 26.0
        self.tower_fault_extra = 0.0
        self.plr = 0.0              # actual capacity fraction
        self.integral = 0.0
        self.running = False
        self.alarm = 0
        self.last_start = -1e9
        self.low_load_timer = 0.0
        self.no_flow_timer = 0.0
        self.flow = 0.0
        self.pump_speed = 0.0
        self.run_seconds = 0.0
        self.starts = 0
        self.power = 0.0
        self.q_chiller = 0.0
        self.q_load = 0.0
        self.oat = 25.0
        self.cop = 0.0
        self.evap_p = 0.0
        self.cond_p = 0.0
        self.last_hb = None
        self.last_hb_change = time.monotonic()
        self.local = False

    # Environment: daily outdoor temp and building load profiles
    def environment(self, now):
        phase = ((now - self.t0) / DAY_LENGTH_S + 0.33) % 1.0  # start mid-morning
        day = math.sin(2 * math.pi * (phase - 0.25))           # peak at 15:00-ish
        self.oat = 24.0 + 8.0 * day + random.gauss(0, 0.05)
        occupied = 0.6 + 0.4 * max(0.0, day)
        return 100.0 + 260.0 * occupied * max(0.3, (self.oat - 12) / 20)

    def step(self, hr, now):
        enable = hr[HR_ENABLE] == 1
        sp = hr[HR_SETPOINT] / 10.0 if hr[HR_SETPOINT] else FALLBACK_SP
        demand_limit = min(100, max(10, hr[HR_DEMAND_LIMIT] or 100)) / 100.0
        pump_cmd = hr[HR_PUMP_CMD] == 1
        faults = hr[HR_FAULT_INJECT]

        # PLC heartbeat supervision: on loss keep last commands but use local setpoint
        if hr[HR_HEARTBEAT] != self.last_hb:
            self.last_hb = hr[HR_HEARTBEAT]
            self.last_hb_change = now
        self.local = now - self.last_hb_change > HEARTBEAT_TIMEOUT_S
        if self.local:
            sp = FALLBACK_SP

        if hr[HR_ALARM_RESET] == 1:
            if self.alarm:
                log.info("alarm %d reset by PLC", self.alarm)
            self.alarm = 0

        # Pump and flow
        target = 1.0 if pump_cmd and not faults & 0b010 else 0.0
        self.pump_speed += (target - self.pump_speed) * min(1.0, DT / 3.0)
        self.flow = CHW_FLOW * self.pump_speed
        flow_ok = self.flow > 0.6 * CHW_FLOW

        # Cooling tower: CWE follows OAT-derived wet bulb + approach, or drifts up on failure
        wet_bulb = self.oat - 6.0
        if faults & 0b001:
            self.tower_fault_extra = min(35.0, self.tower_fault_extra + 0.1 * DT)
        else:
            self.tower_fault_extra = max(0.0, self.tower_fault_extra - 0.2 * DT)
        cwe_target = max(18.0, wet_bulb + 4.0) + self.tower_fault_extra
        self.cwe += (cwe_target - self.cwe) * min(1.0, DT / 30.0)

        # Unit controller: start/stop
        anti_recycle = now - self.last_start < ANTI_RECYCLE_S
        can_run = enable and flow_ok and self.alarm == 0
        if not self.running and can_run and not anti_recycle and self.chws > sp + 0.5:
            self.running = True
            self.last_start = now
            self.starts += 1
            self.plr = MIN_PLR
            self.integral = MIN_PLR
            log.info("compressor start (#%d), CHWS %.1f SP %.1f", self.starts, self.chws, sp)
        elif self.running and not (enable and self.alarm == 0):  # flow loss is handled by the trip below
            self.running = False
            log.info("compressor stop (enable=%s flow_ok=%s alarm=%d)", enable, flow_ok, self.alarm)

        # Unit controller: capacity PI on leaving water temp
        if self.running:
            err = self.chws - sp
            self.integral += KP * err * DT / TI
            self.integral = min(demand_limit, max(MIN_PLR, self.integral))
            want = min(demand_limit, max(MIN_PLR, KP * err + self.integral))
            step = RAMP_PER_S * DT
            self.plr += max(-step * 4, min(step, want - self.plr))
            # low-load cycling: at minimum load and still overcooling
            if self.plr <= MIN_PLR + 1e-3 and self.chws < sp - 1.5:
                self.low_load_timer += DT
            else:
                self.low_load_timer = 0.0
            if self.low_load_timer > 30.0:
                self.running = False
                log.info("compressor cycled off on low load")
            self.run_seconds += DT * (86400.0 / DAY_LENGTH_S)
        else:
            self.plr = max(0.0, self.plr - 0.05 * DT)
            if self.plr < 0.05:
                self.plr = 0.0

        # Refrigeration cycle
        approach_evap = 1.5 + 2.0 * self.plr + (6.0 if faults & 0b100 else 0.0)
        t_evap = self.chws - approach_evap
        self.q_chiller = self.plr * Q_RATED if self.running or self.plr > 0 else 0.0
        if not flow_ok:
            self.q_chiller *= 0.2  # little heat transfer without flow
        lift_k = max(5.0, (self.cwl + 2.0) - t_evap)
        part_load = 1.0 - 0.35 * (1.0 - self.plr) ** 2 if self.plr > 0 else 0.0
        self.cop = 0.47 * (t_evap + 273.15) / lift_k * part_load if self.q_chiller > 0 else 0.0
        self.power = self.q_chiller / self.cop if self.cop > 0 else 0.0
        q_cond = self.q_chiller + self.power
        self.cwl = self.cwe + q_cond / (CW_FLOW * CP)
        t_cond = self.cwl + 2.0
        self.evap_p = sat_pressure_r134a(t_evap if self.q_chiller > 0 else self.chws)
        self.cond_p = sat_pressure_r134a(t_cond if self.q_chiller > 0 else self.cwe)

        # Water side thermal model
        load_demand = self.environment(now)
        mcp = self.flow * CP
        self.q_load = load_demand * (self.flow / CHW_FLOW)        # AHUs need flow
        ambient_gain = (22.0 - self.chwr) * 0.5                   # kW/K pipe gains
        d_chws = (mcp * (self.chwr - self.chws) - self.q_chiller) / C_EVAP
        d_chwr = (mcp * (self.chws - self.chwr) + self.q_load + ambient_gain) / C_DIST
        if self.flow < 1.0:  # stagnant: evaporator drifts toward return
            d_chws += (self.chwr - self.chws) / 300.0
        self.chws += d_chws * DT
        self.chwr += d_chwr * DT

        # Safeties (only when compressor running)
        if self.running and self.alarm == 0:
            self.no_flow_timer = self.no_flow_timer + DT if not flow_ok else 0.0
            if self.no_flow_timer > 3.0:
                self.trip(1)
            elif t_evap < EVAP_T_TRIP:
                self.trip(2)
            elif self.cond_p > COND_P_TRIP:
                self.trip(3)

        status = 0
        status |= STS_RUNNING if self.running else 0
        status |= STS_ALARM if self.alarm else 0
        status |= STS_FLOW_OK if flow_ok else 0
        status |= STS_PUMP_ON if self.pump_speed > 0.5 else 0
        status |= STS_LOCAL if self.local else 0
        status |= STS_ANTI_RECYCLE if anti_recycle else 0
        status |= STS_ENABLED if enable else 0

        ir = [0] * IR_COUNT
        ir[IR_CHWS] = to_u16(self.chws * 10)
        ir[IR_CHWR] = to_u16(self.chwr * 10)
        ir[IR_CWE] = to_u16(self.cwe * 10)
        ir[IR_CWL] = to_u16(self.cwl * 10)
        ir[IR_OAT] = to_u16(self.oat * 10)
        ir[IR_CAPACITY] = to_u16(self.plr * 1000)
        ir[IR_POWER] = to_u16(self.power * 10)
        ir[IR_COOLING] = to_u16(self.q_chiller * 10)
        ir[IR_COP] = to_u16(self.cop * 100)
        ir[IR_EVAP_P] = to_u16(self.evap_p)
        ir[IR_COND_P] = to_u16(self.cond_p)
        ir[IR_STATUS] = status
        ir[IR_ALARM] = self.alarm
        ir[IR_RUN_HOURS] = to_u16(self.run_seconds / 3600)
        ir[IR_STARTS] = to_u16(self.starts)
        ir[IR_FLOW] = to_u16(self.flow * 10)
        ir[IR_LOAD] = to_u16(load_demand * 10)
        return ir

    def trip(self, code):
        self.alarm = code
        self.running = False
        log.warning("TRIP %d: %s", code, ALARMS[code])


async def sim_loop(context, plant):
    slave = context[1]
    last_log = 0.0
    while True:
        now = time.monotonic()
        hr = slave.getValues(3, 0, 16)
        ir = plant.step(hr, now)
        slave.setValues(4, 0, ir)
        if hr[HR_ALARM_RESET]:
            slave.setValues(3, HR_ALARM_RESET, [0])
        if now - last_log > 10:
            last_log = now
            log.info(
                "CHWS %.2f CHWR %.2f SP %.1f cap %3.0f%% Q %5.1f kW P %5.1f kW COP %.2f "
                "OAT %.1f Pc %4.0f kPa run=%s alarm=%d local=%s",
                plant.chws, plant.chwr, hr[HR_SETPOINT] / 10, plant.plr * 100, plant.q_chiller,
                plant.power, plant.cop, plant.oat, plant.cond_p, plant.running, plant.alarm, plant.local,
            )
        await asyncio.sleep(DT)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("pymodbus").setLevel(logging.WARNING)
    hr_init = [0] * 32
    hr_init[HR_SETPOINT] = 70
    hr_init[HR_DEMAND_LIMIT] = 100
    store = ModbusSlaveContext(
        hr=ModbusSequentialDataBlock(0, [0] + hr_init),  # pymodbus maps protocol addr N to block N+1
        ir=ModbusSequentialDataBlock(0, [0] * 33),
        co=ModbusSequentialDataBlock(0, [0] * 9),
        di=ModbusSequentialDataBlock(0, [0] * 9),
    )
    context = ModbusServerContext(slaves={1: store}, single=False)
    plant = ChillerPlant()
    port = int(os.getenv("MODBUS_PORT", "502"))
    asyncio.create_task(sim_loop(context, plant))
    log.info("chiller plant Modbus TCP slave on :%d unit 1", port)
    await StartAsyncTcpServer(context=context, address=("0.0.0.0", port))


if __name__ == "__main__":
    asyncio.run(main())
