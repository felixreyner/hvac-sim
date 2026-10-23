# HVAC chiller plant simulation with a Dockerized S7 PLC

Two containers talk Modbus TCP to each other, and the PLC also serves S7comm
so any Siemens-style client (HMI, SCADA, Node-RED, snap7) can read and write it.

```
  HMI / SCADA / Node-RED / hmi.py
            |  S7comm (ISO-on-TCP, port 102, rack 0 slot 1), DB1
            v
  +---------------------+      Modbus TCP (502, unit 1)      +--------------------------+
  |  plc  (soft S7 PLC) | ---------------------------------> |  chiller (plant sim)     |
  |  python-snap7 S7    |  writes HR 0-5: enable, setpoint,  |  unit controller (PI)    |
  |  server + Modbus    |  demand limit, reset, pump, HB     |  R134a cycle, CHW loop,  |
  |  client, 500 ms scan|  reads  IR 0-16: temps, kW, alarms|  pump, tower, bldg load  |
  +---------------------+                                    +--------------------------+
```

The roles match a real plant: the chiller has its own unit controller
(capacity control, anti-recycle, safeties) behind a Modbus gateway, and the
S7 PLC is the plant supervisory controller (like an S7-1200/1500 using
`MB_CLIENT`).

## Why not an off-the-shelf S7 image?

No genuine Siemens S7 runtime runs in Linux Docker (PLCSIM Advanced is
Windows-only and licensed). The Docker Hub images are all protocol emulators:

| Image | What it is | Modbus | Verdict |
|---|---|---|---|
| `shamaan0086/s7-plc-sim` (~6k pulls, Nov 2025) | python-snap7 2.0 server with DB1/M/I/Q and its own random value generator | no | closest fit, but no hooks for custom logic |
| `colortokenslabs/s7-plc-simulator` | bare snap7 server for discovery demos, no data areas | no | too thin |
| `edgenesis/siemens-s7` | Shifu device-shim for Kubernetes, not a PLC | no | wrong tool |
| `honeynet/conpot` (2019) | ICS honeypot, Siemens S7-200 template with S7comm + Modbus | yes | S7comm only answers identity (SZL) queries, no DB read/write, no logic, Python 3.6 |

So this project builds the PLC on the same library those images use
(`python-snap7`, now pure Python), adds a Modbus client and a real scan
cycle with plant logic. It behaves like an S7 to S7 clients and is fully
scriptable.

## Run

```bash
docker compose up -d --build          # chiller + plc
docker compose logs -f                # watch both
docker compose run --rm hmi python tools/hmi.py status --watch   # live HMI over S7comm
./tools/scenario_test.sh              # end-to-end fault scenarios (~12 min)
```

Behind a TLS-intercepting proxy, pass its CA to the build:
`docker build --secret id=ca,src=/path/ca.crt -t hvac-sim:latest .`

HMI commands (all over S7comm except `fault`, which goes to the simulator):

```bash
hmi() { docker compose run --rm hmi python tools/hmi.py "$@"; }
hmi enable on|off          # DB1.DBX0.0 Plant_Enable
hmi setpoint 7.5           # DB1.DBD2  CHWS base setpoint
hmi spreset on|off         # DB1.DBX0.2 OAT setpoint reset
hmi demand 80              # DB1.DBD6  demand limit %
hmi reset                  # DB1.DBX0.1 alarm reset / clear lockout
hmi fault tower|pump|refrigerant on|off   # inject plant faults (Modbus HR10)
```

From the host: S7 on `localhost:102` (rack 0, slot 1), chiller Modbus on `localhost:5020`.

## PLC sequence

`OFF -> START_PUMP -> PROVE_FLOW (5 s) -> RUNNING -> STOPPING (pump run-on 15 s) -> OFF`

* Setpoint reset: +2.3 K at OAT <= 15 degC, none at >= 30 degC, clamped 5-12 degC.
* Chiller trips are auto-reset after 30 s; 3 trips in an hour locks the chiller out (alarm 103) until an HMI reset.
* Pump fails to prove flow in 20 s, or chiller reports flow loss -> FAULT 101, pump off.
* Modbus comms lost for 5 s -> alarm 102. The chiller sees the PLC heartbeat stop and after 20 s runs on its local fallback setpoint (7.0 degC).

## S7 DB1 (non-optimized, big-endian)

| Offset | Type | Name | Dir |
|---|---|---|---|
| 0.0 | BOOL | Plant_Enable | HMI -> PLC |
| 0.1 | BOOL | Alarm_Reset (PLC clears) | HMI -> PLC |
| 0.2 | BOOL | SP_Reset_Enable | HMI -> PLC |
| 2 | REAL | CHWS_SP_Base degC | HMI -> PLC |
| 6 | REAL | Demand_Limit % | HMI -> PLC |
| 10 / 14 | REAL | CHWS / CHWR degC | status |
| 18 / 22 | REAL | Condenser water entering / leaving degC | status |
| 26 | REAL | Outdoor air temp degC | status |
| 30 | REAL | Capacity % | status |
| 34 | REAL | Compressor power kW | status |
| 38 | REAL | Cooling delivered kW | status |
| 42 | REAL | COP | status |
| 46 / 50 | REAL | Evap / cond pressure kPa | status |
| 54 | REAL | Active setpoint degC | status |
| 58.0-58.6 | BOOL | Pump_Running, Flow_Proven, Chiller_Running, Chiller_Alarm, Comm_Fault, Lockout, Chiller_Local | status |
| 60 | INT | Plant_State (0 OFF, 1 START_PUMP, 2 PROVE_FLOW, 3 RUNNING, 4 STOPPING, 5 FAULT) | status |
| 62 | INT | Alarm code | status |
| 64 | INT | Trips in last hour | status |
| 66 | INT | PLC heartbeat | status |
| 68 / 70 | INT | Run hours / starts | status |
| 72 | REAL | Building load demand kW | status |
| 76 | REAL | CHW flow l/s | status |

## Chiller Modbus map (unit 1, 0-based addresses)

Holding registers (PLC writes): 0 enable, 1 CHWS setpoint degC x10, 2 demand
limit %, 3 alarm reset (write 1), 4 CHW pump command, 5 heartbeat,
10 fault injection bits (0 tower, 1 pump, 2 low refrigerant).

Input registers (PLC reads, x10 unless noted, signed): 0 CHWS, 1 CHWR, 2 CWE,
3 CWL, 4 OAT, 5 capacity %, 6 power kW, 7 cooling kW, 8 COP x100, 9 evap kPa,
10 cond kPa, 11 status word (bit0 running, 1 alarm, 2 flow ok, 3 pump on,
4 local mode, 5 anti-recycle, 6 enabled), 12 alarm code (1 flow loss,
2 low evap temp, 3 high cond pressure), 13 run hours, 14 starts, 15 flow l/s,
16 building load kW.

## Model

500 kW water-cooled R134a chiller: two-node chilled water loop (evaporator
and distribution), building load following a compressed daily profile
(`DAY_LENGTH_S`, default 24 min per day), cooling tower tracking outdoor wet
bulb, COP from lift (about 45 % of Carnot with part-load derating),
saturation pressures from a Clausius-Clapeyron fit, PI capacity control with
20 % minimum load, ramp limit, low-load cycling and 60 s anti-recycle.
