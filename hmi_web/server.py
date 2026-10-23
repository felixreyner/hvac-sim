#!/usr/bin/env python3
"""
Web HMI for the chiller plant.

Talks to the soft S7 PLC only over S7comm (ISO-on-TCP, port 102, rack 0 /
slot 1, DB1) with a snap7 client, and serves a browser dashboard that gets
live values over a WebSocket.

* Polls DB1 every POLL_S, keeps one hour of 1 s trend samples and an event
  log (state changes, alarms, trips) so a newly opened browser has history.
* Browser commands (start/stop, setpoints, alarm reset) become DB1 writes.
* Optional simulator panel (SIM_FAULTS=1) writes the chiller simulator's
  fault-injection register over Modbus. That is a test tool for the plant
  model, not part of the PLC, and is labelled as such in the UI.

DB1 layout -> see README.md
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from pathlib import Path

import snap7
from aiohttp import WSMsgType, web
from snap7 import util

log = logging.getLogger("hmi-web")

PLC_HOST = os.getenv("PLC_HOST", "plc")
PLC_PORT = int(os.getenv("PLC_PORT", "102"))
PLC_RACK = int(os.getenv("PLC_RACK", "0"))
PLC_SLOT = int(os.getenv("PLC_SLOT", "1"))
CHILLER_HOST = os.getenv("CHILLER_HOST", "chiller")
CHILLER_PORT = int(os.getenv("CHILLER_PORT", "502"))
SIM_FAULTS = os.getenv("SIM_FAULTS", "1") == "1"
HTTP_PORT = int(os.getenv("HTTP_PORT", "8080"))
POLL_S = float(os.getenv("POLL_S", "0.5"))
HISTORY_S = 3600
STATIC = Path(__file__).parent / "static"

DB = 1
DB_LEN = 80
STATES = ["OFF", "START_PUMP", "PROVE_FLOW", "RUNNING", "STOPPING", "FAULT"]
ALARMS = {0: "No alarm", 1: "Evaporator flow loss", 2: "Low evaporator temperature",
          3: "High condenser pressure", 101: "CHW pump failed to prove flow",
          102: "Modbus comms to chiller lost", 103: "Chiller LOCKOUT, HMI reset required"}
FAULT_BITS = {"tower": 0, "pump": 1, "refrigerant": 2}
SP_RANGE = (5.0, 12.0)
DEMAND_RANGE = (10.0, 100.0)
TREND_KEYS = ("chws", "chwr", "active_sp", "cwe", "cwl", "oat", "capacity",
              "power", "cooling", "load", "cop", "flow")


def decode(b):
    r = lambda o: round(util.get_real(b, o), 3)  # noqa: E731
    bit = lambda o, i: util.get_bool(b, o, i)  # noqa: E731
    state = util.get_int(b, 60)
    return {
        "plant_enable": bit(0, 0), "alarm_reset": bit(0, 1), "sp_reset": bit(0, 2),
        "sp_base": r(2), "demand_limit": r(6),
        "chws": r(10), "chwr": r(14), "cwe": r(18), "cwl": r(22), "oat": r(26),
        "capacity": r(30), "power": r(34), "cooling": r(38), "cop": r(42),
        "evap_p": r(46), "cond_p": r(50), "active_sp": r(54),
        "pump": bit(58, 0), "flow_ok": bit(58, 1), "running": bit(58, 2),
        "chiller_alarm": bit(58, 3), "comm_fault": bit(58, 4), "lockout": bit(58, 5),
        "local": bit(58, 6),
        "state": state, "state_name": STATES[state] if 0 <= state < len(STATES) else "?",
        "alarm": util.get_int(b, 62), "trips": util.get_int(b, 64),
        "heartbeat": util.get_int(b, 66), "run_hours": util.get_int(b, 68),
        "starts": util.get_int(b, 70), "load": r(72), "flow": r(76),
    }


class Gateway:
    """Single snap7 connection shared by the poller and command handlers."""

    def __init__(self):
        self.client = snap7.Client()
        self.lock = asyncio.Lock()
        self.connected = False
        self.last = None
        self.last_ok = 0.0
        self.hb_changed = 0.0
        self.faults = None
        self.history = deque(maxlen=HISTORY_S)
        self.events = deque(maxlen=300)
        self.sockets = set()
        self._last_sample = 0.0

    # ---- S7 ------------------------------------------------------------
    def _connect(self):
        try:
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.client.connect(PLC_HOST, PLC_RACK, PLC_SLOT, PLC_PORT)

    async def s7(self, fn, *args):
        async with self.lock:
            if not self.connected:
                await asyncio.to_thread(self._connect)
                self.connected = True
                self.event("info", f"S7 link up {PLC_HOST}:{PLC_PORT} rack {PLC_RACK} slot {PLC_SLOT}")
            try:
                return await asyncio.to_thread(fn, *args)
            except Exception:
                self.connected = False
                raise

    def _write_cmd_bit(self, bit, value):
        b = self.client.db_read(DB, 0, 1)
        util.set_bool(b, 0, 1, False)  # never re-assert a reset pulse the PLC already cleared
        util.set_bool(b, 0, bit, value)
        self.client.db_write(DB, 0, b)

    def _write_real(self, offset, value):
        b = bytearray(4)
        util.set_real(b, 0, value)
        self.client.db_write(DB, offset, b)

    # ---- simulator (Modbus, optional) ---------------------------------
    def _mb_faults(self, name=None, on=None):
        from pymodbus.client import ModbusTcpClient
        m = ModbusTcpClient(CHILLER_HOST, port=CHILLER_PORT, timeout=1)
        try:
            if not m.connect():
                raise IOError("chiller simulator unreachable")
            rr = m.read_holding_registers(10, count=1, slave=1)
            if rr.isError():
                raise IOError(rr)
            v = rr.registers[0]
            if name is not None:
                mask = 1 << FAULT_BITS[name]
                v = v | mask if on else v & ~mask
                m.write_register(10, v, slave=1)
            return v
        finally:
            m.close()

    # ---- events / broadcast -------------------------------------------
    def event(self, level, text):
        ev = {"t": time.time(), "level": level, "text": text}
        self.events.append(ev)
        log.info("%s: %s", level, text)
        self.broadcast({"type": "event", "event": ev})

    def broadcast(self, msg):
        data = json.dumps(msg)
        for ws in list(self.sockets):
            if ws.closed:
                self.sockets.discard(ws)
            else:
                asyncio.ensure_future(ws.send_str(data))

    def diff_events(self, prev, cur):
        if prev is None:
            return
        if prev["state"] != cur["state"]:
            lvl = "alarm" if cur["state_name"] == "FAULT" else "info"
            self.event(lvl, f"Plant state {prev['state_name']} → {cur['state_name']}")
        if prev["alarm"] != cur["alarm"]:
            if cur["alarm"]:
                self.event("alarm", f"ALARM {cur['alarm']}: {ALARMS.get(cur['alarm'], 'unknown')}")
            else:
                self.event("clear", f"Alarm {prev['alarm']} cleared")
        if cur["trips"] > prev["trips"]:
            self.event("warn", f"Chiller trip recorded ({cur['trips']} in last hour)")
        for key, label in (("lockout", "Chiller lockout"), ("comm_fault", "PLC↔chiller comm fault"),
                           ("local", "Chiller in local fallback mode")):
            if cur[key] != prev[key]:
                self.event("alarm" if cur[key] else "clear", f"{label} {'ACTIVE' if cur[key] else 'cleared'}")
        if cur["running"] != prev["running"]:
            self.event("info", f"Compressor {'started' if cur['running'] else 'stopped'}")
        if cur["pump"] != prev["pump"]:
            self.event("info", f"CHW pump {'running' if cur['pump'] else 'stopped'}")

    def snapshot(self):
        now = time.time()
        return {
            "type": "status", "t": now,
            "link": {"s7": self.connected, "host": f"{PLC_HOST}:{PLC_PORT}",
                     "rack": PLC_RACK, "slot": PLC_SLOT,
                     "plc_alive": self.connected and now - self.hb_changed < 3.0,
                     "age": round(now - self.last_ok, 1) if self.last_ok else None},
            "plc": self.last, "faults": self.faults, "sim_faults": SIM_FAULTS,
            "alarm_text": ALARMS.get(self.last["alarm"], "Unknown alarm") if self.last else None,
        }

    # ---- loops ----------------------------------------------------------
    async def poll_loop(self):
        backoff = 1.0
        while True:
            t0 = time.monotonic()
            try:
                raw = await self.s7(self.client.db_read, DB, 0, DB_LEN)
                cur = decode(raw)
                now = time.time()
                if self.last is None or cur["heartbeat"] != self.last["heartbeat"]:
                    self.hb_changed = now
                self.diff_events(self.last, cur)
                self.last, self.last_ok = cur, now
                if now - self._last_sample >= 1.0:
                    self._last_sample = now
                    self.history.append([round(now, 1)] + [cur[k] for k in TREND_KEYS])
                backoff = 1.0
            except Exception as e:  # noqa: BLE001
                if backoff == 1.0:
                    self.event("alarm", f"S7 link down: {e}")
                self.broadcast(self.snapshot())
                await asyncio.sleep(backoff)
                backoff = min(10.0, backoff * 2)
                continue
            self.broadcast(self.snapshot())
            await asyncio.sleep(max(0.0, POLL_S - (time.monotonic() - t0)))

    async def fault_loop(self):
        while SIM_FAULTS:
            try:
                self.faults = await asyncio.to_thread(self._mb_faults)
            except Exception:  # noqa: BLE001
                self.faults = None
            await asyncio.sleep(2.0)

    # ---- commands -------------------------------------------------------
    async def command(self, msg):
        cmd = msg.get("cmd")
        val = msg.get("value")
        if cmd == "enable":
            await self.s7(self._write_cmd_bit, 0, bool(val))
            self.event("cmd", f"Operator: plant {'START' if val else 'STOP'} (DB1.DBX0.0 := {int(bool(val))})")
        elif cmd == "spreset":
            await self.s7(self._write_cmd_bit, 2, bool(val))
            self.event("cmd", f"Operator: OAT setpoint reset {'ON' if val else 'OFF'} (DB1.DBX0.2)")
        elif cmd == "reset":
            await self.s7(self._write_cmd_bit, 1, True)
            self.event("cmd", "Operator: alarm reset (DB1.DBX0.1)")
        elif cmd == "setpoint":
            v = float(val)
            if not SP_RANGE[0] <= v <= SP_RANGE[1]:
                raise ValueError(f"setpoint must be {SP_RANGE[0]}..{SP_RANGE[1]} °C")
            await self.s7(self._write_real, 2, v)
            self.event("cmd", f"Operator: CHWS base setpoint {v:.1f} °C (DB1.DBD2)")
        elif cmd == "demand":
            v = float(val)
            if not DEMAND_RANGE[0] <= v <= DEMAND_RANGE[1]:
                raise ValueError(f"demand limit must be {DEMAND_RANGE[0]:.0f}..{DEMAND_RANGE[1]:.0f} %")
            await self.s7(self._write_real, 6, v)
            self.event("cmd", f"Operator: demand limit {v:.0f} % (DB1.DBD6)")
        elif cmd == "fault":
            if not SIM_FAULTS:
                raise ValueError("simulator fault injection disabled (SIM_FAULTS=0)")
            name = msg.get("name")
            if name not in FAULT_BITS:
                raise ValueError(f"unknown fault {name!r}")
            self.faults = await asyncio.to_thread(self._mb_faults, name, bool(val))
            self.event("warn", f"SIM: {name} fault {'INJECTED' if val else 'removed'}")
        else:
            raise ValueError(f"unknown command {cmd!r}")


async def ws_handler(request):
    gw = request.app["gw"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    gw.sockets.add(ws)
    await ws.send_str(json.dumps({
        "type": "hello", "trend_keys": TREND_KEYS, "history": list(gw.history),
        "events": list(gw.events), "alarms": ALARMS, "sp_range": SP_RANGE,
        "demand_range": DEMAND_RANGE,
    }))
    await ws.send_str(json.dumps(gw.snapshot()))
    try:
        async for m in ws:
            if m.type != WSMsgType.TEXT:
                continue
            msg = {}
            try:
                msg = json.loads(m.data)
                await gw.command(msg)
                await ws.send_str(json.dumps({"type": "ack", "id": msg.get("id"), "ok": True}))
            except Exception as e:  # noqa: BLE001
                await ws.send_str(json.dumps({"type": "ack", "id": msg.get("id") if isinstance(msg, dict) else None,
                                              "ok": False, "error": str(e)}))
    finally:
        gw.sockets.discard(ws)
    return ws


async def index(_):
    return web.FileResponse(STATIC / "index.html")


async def health(request):
    gw = request.app["gw"]
    return web.json_response({"s7": gw.connected, "age": time.time() - gw.last_ok if gw.last_ok else None},
                             status=200 if gw.connected else 503)


async def on_startup(app):
    gw = app["gw"]
    app["tasks"] = [asyncio.create_task(gw.poll_loop()), asyncio.create_task(gw.fault_loop())]


async def on_cleanup(app):
    for t in app["tasks"]:
        t.cancel()


def make_app():
    app = web.Application()
    app["gw"] = Gateway()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/health", health)
    app.router.add_static("/static/", STATIC)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("snap7").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("pymodbus").setLevel(logging.CRITICAL)
    log.info("web HMI on :%d, S7 -> %s:%d rack %d slot %d, sim faults %s",
             HTTP_PORT, PLC_HOST, PLC_PORT, PLC_RACK, PLC_SLOT, "on" if SIM_FAULTS else "off")
    web.run_app(make_app(), port=HTTP_PORT, print=None)
