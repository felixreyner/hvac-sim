#!/usr/bin/env python3
"""
Soft Siemens S7 PLC: chiller plant supervisory controller.

* S7comm server on TCP 102 (rack 0 / slot 1) via python-snap7. DB1 holds
  HMI commands and plant status, so any S7 client (snap7, Node-RED S7 node,
  HMI/SCADA S7 driver) can read and write it like a real S7-1200/1500 with
  optimized access disabled.
* Modbus TCP client to the chiller (like MB_CLIENT on a real S7), polled
  every scan.
* Scan cycle runs the plant sequence: CHW pump start, flow proving,
  chiller enable, OAT setpoint reset, alarm auto-reset with lockout.

DB1 layout -> see README.md
"""

import logging
import os
import struct
import time
from collections import deque

from pymodbus.client import ModbusTcpClient
from snap7.server import Server
from snap7.type import SrvArea
from snap7 import util

log = logging.getLogger("plc")

SCAN_S = 0.5
CHILLER_HOST = os.getenv("CHILLER_HOST", "chiller")
CHILLER_PORT = int(os.getenv("CHILLER_PORT", "502"))
UNIT = 1
FLOW_PROVE_S = 5.0
FLOW_TIMEOUT_S = 20.0
PUMP_RUN_ON_S = float(os.getenv("PUMP_RUN_ON_S", "15"))
AUTO_RESET_DELAY_S = 30.0
MAX_TRIPS_PER_HOUR = 3
COMM_FAULT_S = 5.0

# ---- DB1 offsets (bytes) ----------------------------------------------
DB_NUM = 1
DB_SIZE = 128
# commands (HMI -> PLC)
CMD_BITS = 0          # .0 Plant_Enable  .1 Alarm_Reset  .2 SP_Reset_Enable
CMD_SP_BASE = 2       # REAL degC
CMD_DEMAND_LIMIT = 6  # REAL %
# status (PLC -> HMI)
ST_CHWS = 10
ST_CHWR = 14
ST_CWE = 18
ST_CWL = 22
ST_OAT = 26
ST_CAPACITY = 30
ST_POWER = 34
ST_COOLING = 38
ST_COP = 42
ST_EVAP_P = 46
ST_COND_P = 50
ST_ACTIVE_SP = 54
ST_BITS = 58          # .0 Pump_Running .1 Flow_Proven .2 Chiller_Running .3 Chiller_Alarm
                      # .4 Comm_Fault .5 Lockout .6 Chiller_Local
ST_STATE = 60         # INT
ST_ALARM = 62         # INT
ST_TRIPS = 64         # INT trips in last hour
ST_HEARTBEAT = 66     # INT
ST_RUN_HOURS = 68     # INT
ST_STARTS = 70        # INT
ST_LOAD = 72          # REAL kW building load demand
ST_FLOW = 76          # REAL l/s

STATES = {0: "OFF", 1: "START_PUMP", 2: "PROVE_FLOW", 3: "RUNNING", 4: "STOPPING", 5: "FAULT"}
OFF, START_PUMP, PROVE_FLOW, RUNNING, STOPPING, FAULT = range(6)

# PLC-level alarm codes (chiller codes 1..99 pass through)
ALM_PUMP_NO_FLOW = 101
ALM_COMM = 102
ALM_LOCKOUT = 103


def s16(v):
    return v - 0x10000 if v & 0x8000 else v


class PLC:
    def __init__(self):
        self.db = bytearray(DB_SIZE)
        util.set_real(self.db, CMD_SP_BASE, 6.7)
        util.set_real(self.db, CMD_DEMAND_LIMIT, 100.0)
        util.set_bool(self.db, CMD_BITS, 2, True)
        if os.getenv("AUTOSTART", "1") == "1":
            util.set_bool(self.db, CMD_BITS, 0, True)

        self.server = Server(log=False)
        self.server.register_area(SrvArea.DB, DB_NUM, self.db)
        self.mb = ModbusTcpClient(CHILLER_HOST, port=CHILLER_PORT, timeout=1)

        self.state = OFF
        self.state_since = time.monotonic()
        self.alarm = 0
        self.alarm_since = 0.0
        self.lockout = False
        self.trips = deque()
        self.heartbeat = 0
        self.last_comm_ok = time.monotonic()
        self.comm_fault = False
        self.chiller = {}
        self.pump_cmd = 0
        self.enable_cmd = 0
        self.reset_pulse = 0
        self.active_sp = 6.7

    def goto(self, state):
        if state != self.state:
            log.info("state %s -> %s", STATES[self.state], STATES[state])
            self.state = state
            self.state_since = time.monotonic()

    def in_state_for(self):
        return time.monotonic() - self.state_since

    # ---- I/O --------------------------------------------------------
    def read_chiller(self):
        if not self.mb.connected:
            self.mb.connect()
        rr = self.mb.read_input_registers(0, count=17, slave=UNIT)
        if rr.isError():
            raise IOError(rr)
        r = rr.registers
        status = r[11]
        return {
            "chws": s16(r[0]) / 10, "chwr": s16(r[1]) / 10, "cwe": s16(r[2]) / 10,
            "cwl": s16(r[3]) / 10, "oat": s16(r[4]) / 10, "capacity": r[5] / 10,
            "power": r[6] / 10, "cooling": r[7] / 10, "cop": r[8] / 100,
            "evap_p": r[9], "cond_p": r[10],
            "running": bool(status & 1), "alarm_active": bool(status & 2),
            "flow_ok": bool(status & 4), "pump_on": bool(status & 8), "local": bool(status & 16),
            "alarm": r[12], "run_hours": r[13], "starts": r[14], "flow": r[15] / 10,
            "load": r[16] / 10,
        }

    def write_chiller(self):
        demand = util.get_real(self.db, CMD_DEMAND_LIMIT)
        regs = [
            self.enable_cmd,
            int(round(self.active_sp * 10)),
            int(max(10, min(100, demand))),
            self.reset_pulse,
            self.pump_cmd,
            self.heartbeat,
        ]
        wr = self.mb.write_registers(0, regs, slave=UNIT)
        if wr.isError():
            raise IOError(wr)
        self.reset_pulse = 0

    # ---- logic ------------------------------------------------------
    def setpoint(self, oat):
        base = util.get_real(self.db, CMD_SP_BASE)
        if util.get_bool(self.db, CMD_BITS, 2):
            # OAT reset: +2.3 K at <= 15 degC OAT, none at >= 30 degC
            frac = min(1.0, max(0.0, (30.0 - oat) / 15.0))
            base += 2.3 * frac
        return min(12.0, max(5.0, base))

    def logic(self):
        now = time.monotonic()
        c = self.chiller
        enable = util.get_bool(self.db, CMD_BITS, 0)
        hmi_reset = util.get_bool(self.db, CMD_BITS, 1)

        while self.trips and now - self.trips[0] > 3600:
            self.trips.popleft()

        if hmi_reset:
            util.set_bool(self.db, CMD_BITS, 1, False)
            log.info("HMI alarm reset")
            self.lockout = False
            self.trips.clear()
            self.alarm = 0
            self.reset_pulse = 1
            if self.state == FAULT:
                self.goto(OFF)

        if self.comm_fault:
            self.alarm = ALM_COMM
            return  # outputs cannot be written; chiller falls back to local mode

        if self.alarm == ALM_COMM:
            self.alarm = 0

        if c and c["oat"] is not None:
            self.active_sp = self.setpoint(c["oat"])

        if self.state == OFF:
            self.pump_cmd, self.enable_cmd = 0, 0
            if enable and not self.lockout:
                self.goto(START_PUMP)

        elif self.state == START_PUMP:
            self.pump_cmd, self.enable_cmd = 1, 0
            if not enable:
                self.goto(STOPPING)
            elif c["flow_ok"]:
                self.goto(PROVE_FLOW)
            elif self.in_state_for() > FLOW_TIMEOUT_S:
                self.alarm = ALM_PUMP_NO_FLOW
                log.warning("pump failed to prove flow")
                self.goto(FAULT)

        elif self.state == PROVE_FLOW:
            self.pump_cmd, self.enable_cmd = 1, 0
            if not enable:
                self.goto(STOPPING)
            elif not c["flow_ok"]:
                self.goto(START_PUMP)
            elif self.in_state_for() > FLOW_PROVE_S:
                self.goto(RUNNING)

        elif self.state == RUNNING:
            self.pump_cmd, self.enable_cmd = 1, 1
            if not enable:
                self.goto(STOPPING)
            elif c["alarm_active"]:
                code = c["alarm"]
                if self.alarm != code:  # new trip
                    self.alarm = code
                    self.alarm_since = now
                    self.trips.append(now)
                    log.warning("chiller trip, code %d (%d in last hour)", code, len(self.trips))
                if code == 1:  # evaporator flow loss: treat as pump fault
                    self.alarm = ALM_PUMP_NO_FLOW
                    self.goto(FAULT)
                elif len(self.trips) >= MAX_TRIPS_PER_HOUR:
                    self.lockout = True
                    self.alarm = ALM_LOCKOUT
                    log.error("chiller locked out after %d trips, HMI reset required", len(self.trips))
                    self.goto(FAULT)
                elif now - self.alarm_since > AUTO_RESET_DELAY_S:
                    log.info("auto-reset of chiller alarm %d", code)
                    self.reset_pulse = 1
                    self.alarm_since = now
            elif self.alarm and not c["alarm_active"]:
                self.alarm = 0

        elif self.state == STOPPING:
            self.enable_cmd = 0
            if c["running"]:
                self.state_since = now  # run-on timer starts after compressor stops
            if enable and not self.lockout:
                self.goto(START_PUMP)
            elif self.in_state_for() > PUMP_RUN_ON_S:
                self.pump_cmd = 0
                self.goto(OFF)

        elif self.state == FAULT:
            self.enable_cmd = 0
            # keep CHW flowing through the evaporator unless the pump is the problem
            self.pump_cmd = 0 if self.alarm == ALM_PUMP_NO_FLOW else 1
            # leaving FAULT needs an HMI alarm reset (handled above)

    def publish(self):
        c = self.chiller or {}
        db = self.db
        for off, key in ((ST_CHWS, "chws"), (ST_CHWR, "chwr"), (ST_CWE, "cwe"), (ST_CWL, "cwl"),
                         (ST_OAT, "oat"), (ST_CAPACITY, "capacity"), (ST_POWER, "power"),
                         (ST_COOLING, "cooling"), (ST_COP, "cop"), (ST_EVAP_P, "evap_p"),
                         (ST_COND_P, "cond_p"), (ST_LOAD, "load"), (ST_FLOW, "flow")):
            util.set_real(db, off, float(c.get(key, 0.0)))
        util.set_real(db, ST_ACTIVE_SP, self.active_sp)
        util.set_bool(db, ST_BITS, 0, c.get("pump_on", False))
        util.set_bool(db, ST_BITS, 1, c.get("flow_ok", False))
        util.set_bool(db, ST_BITS, 2, c.get("running", False))
        util.set_bool(db, ST_BITS, 3, c.get("alarm_active", False))
        util.set_bool(db, ST_BITS, 4, self.comm_fault)
        util.set_bool(db, ST_BITS, 5, self.lockout)
        util.set_bool(db, ST_BITS, 6, c.get("local", False))
        util.set_int(db, ST_STATE, self.state)
        util.set_int(db, ST_ALARM, self.alarm)
        util.set_int(db, ST_TRIPS, len(self.trips))
        util.set_int(db, ST_HEARTBEAT, self.heartbeat)
        util.set_int(db, ST_RUN_HOURS, int(c.get("run_hours", 0)))
        util.set_int(db, ST_STARTS, int(c.get("starts", 0)))

    def scan(self):
        self.heartbeat = (self.heartbeat + 1) & 0x7FFF
        try:
            self.chiller = self.read_chiller()
            self.last_comm_ok = time.monotonic()
            if self.comm_fault:
                log.info("Modbus comms to chiller restored")
            self.comm_fault = False
        except Exception as e:  # noqa: BLE001 - any comms error counts
            if not self.comm_fault and time.monotonic() - self.last_comm_ok > COMM_FAULT_S:
                log.error("Modbus comms to chiller lost: %s", e)
                self.comm_fault = True
            self.mb.close()

        self.server.lock_area(SrvArea.DB, DB_NUM)
        try:
            if self.chiller:
                self.logic()
            self.publish()
        finally:
            self.server.unlock_area(SrvArea.DB, DB_NUM)

        if not self.comm_fault and self.chiller:
            try:
                self.write_chiller()
            except Exception as e:  # noqa: BLE001
                log.warning("Modbus write failed: %s", e)

    def run(self):
        port = int(os.getenv("S7_PORT", "102"))
        self.server.start(tcp_port=port)
        log.info("S7 server on :%d (rack 0 slot 1), DB%d %d bytes; Modbus client -> %s:%d",
                 port, DB_NUM, DB_SIZE, CHILLER_HOST, CHILLER_PORT)
        last_log = 0.0
        while True:
            t = time.monotonic()
            self.scan()
            if t - last_log > 10 and self.chiller:
                last_log = t
                c = self.chiller
                log.info("%-10s CHWS %.1f/%.1f SP  cap %3.0f%%  %5.1f kW  alarm %d  trips %d",
                         STATES[self.state], c["chws"], self.active_sp, c["capacity"],
                         c["power"], self.alarm, len(self.trips))
            time.sleep(max(0.0, SCAN_S - (time.monotonic() - t)))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("snap7").setLevel(logging.WARNING)
    logging.getLogger("pymodbus").setLevel(logging.CRITICAL)
    PLC().run()
