#!/usr/bin/env python3
"""
Minimal HMI over S7comm (reads/writes PLC DB1), plus fault injection
straight into the chiller simulation over Modbus.

  hmi.py status [--watch]
  hmi.py enable on|off
  hmi.py setpoint 7.5
  hmi.py demand 80
  hmi.py spreset on|off
  hmi.py reset
  hmi.py fault tower|pump|refrigerant on|off
"""

import argparse
import os
import sys
import time

import snap7
from snap7 import util

PLC_HOST = os.getenv("PLC_HOST", "plc")
PLC_PORT = int(os.getenv("PLC_PORT", "102"))
CHILLER_HOST = os.getenv("CHILLER_HOST", "chiller")
CHILLER_PORT = int(os.getenv("CHILLER_PORT", "502"))
DB = 1
STATES = ["OFF", "START_PUMP", "PROVE_FLOW", "RUNNING", "STOPPING", "FAULT"]
FAULT_BITS = {"tower": 0, "pump": 1, "refrigerant": 2}
ALARMS = {0: "-", 1: "evap flow loss", 2: "low evap temp", 3: "high cond pressure",
          101: "pump/no flow", 102: "Modbus comm loss", 103: "LOCKOUT (HMI reset needed)"}


def plc():
    c = snap7.Client()
    c.connect(PLC_HOST, 0, 1, PLC_PORT)
    return c


def read_status(c):
    b = c.db_read(DB, 0, 80)
    r = lambda o: util.get_real(b, o)  # noqa: E731
    bit = lambda o, i: util.get_bool(b, o, i)  # noqa: E731
    return {
        "plant_enable": bit(0, 0), "sp_reset": bit(0, 2),
        "sp_base": r(2), "demand_limit": r(6),
        "chws": r(10), "chwr": r(14), "cwe": r(18), "cwl": r(22), "oat": r(26),
        "capacity": r(30), "power": r(34), "cooling": r(38), "cop": r(42),
        "evap_p": r(46), "cond_p": r(50), "active_sp": r(54),
        "pump": bit(58, 0), "flow_ok": bit(58, 1), "running": bit(58, 2),
        "chiller_alarm": bit(58, 3), "comm_fault": bit(58, 4), "lockout": bit(58, 5),
        "local": bit(58, 6),
        "state": STATES[util.get_int(b, 60)], "alarm": util.get_int(b, 62),
        "trips": util.get_int(b, 64), "heartbeat": util.get_int(b, 66),
        "run_hours": util.get_int(b, 68), "starts": util.get_int(b, 70),
        "load": r(72), "flow": r(76),
    }


def fmt(s):
    onoff = lambda v: "ON " if v else "off"  # noqa: E731
    return (
        f"state {s['state']:<10} enable {onoff(s['plant_enable'])}  pump {onoff(s['pump'])} "
        f"flow {s['flow']:4.1f} l/s  comp {onoff(s['running'])}  "
        f"CHWS {s['chws']:5.2f} (SP {s['active_sp']:4.1f})  CHWR {s['chwr']:5.2f}  "
        f"CW {s['cwe']:4.1f}/{s['cwl']:4.1f}  OAT {s['oat']:4.1f}  "
        f"cap {s['capacity']:5.1f}%  Q {s['cooling']:5.1f} kW  load {s['load']:5.1f} kW  "
        f"P {s['power']:5.1f} kW  COP {s['cop']:4.2f}  Pe/Pc {s['evap_p']:3.0f}/{s['cond_p']:4.0f} kPa  "
        f"alarm {s['alarm']} {ALARMS.get(s['alarm'], '?')}  trips {s['trips']}  starts {s['starts']}"
    )


def set_bit(c, byte, bit, value):
    b = c.db_read(DB, byte, 1)
    util.set_bool(b, 0, bit, value)
    c.db_write(DB, byte, b)


def set_real(c, offset, value):
    b = bytearray(4)
    util.set_real(b, 0, value)
    c.db_write(DB, offset, b)


def fault(name, on):
    from pymodbus.client import ModbusTcpClient
    m = ModbusTcpClient(CHILLER_HOST, port=CHILLER_PORT)
    m.connect()
    v = m.read_holding_registers(10, count=1, slave=1).registers[0]
    v = v | (1 << FAULT_BITS[name]) if on else v & ~(1 << FAULT_BITS[name])
    m.write_register(10, v, slave=1)
    m.close()
    print(f"fault {name} {'ON' if on else 'off'} (mask {v:#05b})")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status")
    st.add_argument("--watch", type=float, nargs="?", const=2.0)
    for name in ("enable", "spreset"):
        sub.add_parser(name).add_argument("value", choices=["on", "off"])
    sub.add_parser("setpoint").add_argument("value", type=float)
    sub.add_parser("demand").add_argument("value", type=float)
    sub.add_parser("reset")
    f = sub.add_parser("fault")
    f.add_argument("name", choices=list(FAULT_BITS))
    f.add_argument("value", choices=["on", "off"])
    a = p.parse_args()

    if a.cmd == "fault":
        return fault(a.name, a.value == "on")

    c = plc()
    if a.cmd == "status":
        while True:
            print(fmt(read_status(c)), flush=True)
            if not a.watch:
                break
            time.sleep(a.watch)
    elif a.cmd == "enable":
        set_bit(c, 0, 0, a.value == "on")
    elif a.cmd == "spreset":
        set_bit(c, 0, 2, a.value == "on")
    elif a.cmd == "reset":
        set_bit(c, 0, 1, True)
    elif a.cmd == "setpoint":
        set_real(c, 2, a.value)
    elif a.cmd == "demand":
        set_real(c, 6, a.value)
    if a.cmd != "status":
        print("ok")
    c.disconnect()


if __name__ == "__main__":
    sys.exit(main())
