# Bosch PowerPack 400 (BES2) — CAN / UDS notes

Everything here was observed from a single diagnostic session captured on one
pack. IDs, DIDs and byte offsets are empirical and may differ on other packs,
firmware versions, or bike wiring. Treat it as a starting point, not a spec.

Pack under test: **PowerPack Frame 400**, Bosch part number **0275 007 512**,
drive unit **Performance Line BDU365**, display **Purion**.

## Bus

- **Bit rate:** 500 kbit/s, standard + extended IDs both present.
- The BMS goes to sleep. It needs a **keep-alive** to stay responsive:
  CAN ID `0x061`, payload `00`, every **0.5 s**. Without it the pack stops
  answering after a short while.

## Passive broadcast frames

These are emitted by the pack (or the bike) without any request:

| CAN ID | Bytes | Meaning | Decode |
|--------|-------|---------|--------|
| `0x203` | byte 4 | State of charge | `SOC % = data[4]` (e.g. `0x62` = 98 %) |
| `0x2AA` | 0..1 | Temperature | `centiKelvin`, `°C = raw/100 − 273.15` |
| `0x2AA` | 2..3 | Pack voltage | `mV`, big-endian (e.g. `0xA254` = 41.556 V) |
| `0x108` | 2..3 | Total-Ah candidate | big-endian; **unconfirmed**, may be another ECU |

Passive reads of SOC, voltage and temperature match the official report, so
`0x2AA` is reliable. `0x108` is a guess and was not reproduced on the bench.

## UDS diagnostic addressing

| Role | Request ID | Response ID |
|------|-----------|-------------|
| BMS (battery) | `0x022` | `0x023` |
| Auxiliary endpoint | `0x020` | `0x021` |

Transport is ISO-TP (single frame, first frame + flow control `30 10 05`).

### Session

The official tool opens an **extended session** before reading:

```
TX 022  02 10 03            # DiagnosticSessionControl, extended
RX 023  06 50 03 00 32 01 F4   # positive, P2/P2* timings
```

This is **not** authentication — it always succeeds.

### ReadDataByIdentifier (service 0x22)

```
TX 022  03 22 04 15         # read DID 0x0415
RX 023  04 62 04 15 62      # 0x62 = 98 % SOC
```

| DID | Meaning | Format | Access |
|-----|---------|--------|--------|
| `0x0415` | SOC | `data[0]` = % | **locked** (NRC 0x33) |
| `0x0433` | current temperature | u16 BE centiKelvin | **locked** |
| `0x0222` | max temperature (history) | u16 BE centiKelvin | **locked** |
| `0x0224` | min temperature (history) | u16 BE centiKelvin | **locked** |
| `0x0313` | life counter, high word | u16 BE | **locked** |
| `0x0312` | life counter, low word | u16 BE | **locked** |
| `0x0315` | total Ah (precise) | — | **locked** |
| `0x0501` | status indicator | `data[0]` (e.g. `0xA1`) | **readable** |
| `0x0210` | Ah candidate (on `0x020/0x021`) | u16 BE (`0x09C4` = 2500) | readable in capture |

### DTCs (service 0x19)

`reportDTCByStatusMask` and the extended/snapshot variants work **without
authentication**:

```
TX 022  06 19 04 60 60 11 FF   # DTC extended record, 606011, record FF
RX 023  ...                     # multi-frame, needs flow control
```

Two Bosch DTC roots show up: **`606011`** and **`655003`**. Bosch displays the
three DTC bytes as a plain hex string (`60 60 11` → `606011`).

## The security gate

Most of the interesting history is behind **SecurityAccess (0x27)**:

- Reading a locked DID returns **`7F 22 33`** → NRC **`0x33` securityAccessDenied**.
- `10 03` (extended session) then `27 11` (request seed) returns a **16-byte
  seed**. Sending the matching key with `27 12` would unlock the reads.
- The seed→key algorithm is **unknown**, so `27 12` was never sent and these
  DIDs stay locked.

NRC quick reference seen in captures:

| NRC | Name | Meaning here |
|-----|------|--------------|
| `0x31` | requestOutOfRange | DID/record not supported in this context |
| `0x33` | securityAccessDenied | known DID, needs the 0x27 key |

## What you can and cannot read without the key

**Readable** (passive + unauthenticated UDS):

- State of charge
- Pack voltage
- Current / measured temperature
- Fault codes (DTCs `606011`, `655003`)

**Locked** (needs the SecurityAccess key, not recovered):

- Full charge-cycle count
- Total Ah discharged (precise)
- Min / max recorded temperatures
- Life energy counters

## Reference values (from the official Bosch report)

Used to validate the decoding. The locked values below could **not** be read
back without the key — they come from the official tool only:

| Field | Official value | This project |
|-------|----------------|--------------|
| SOC | 98 % | readable (matches) |
| Pack voltage | 41.6 V | readable (≈41.4–41.6 V) |
| Current temperature | — | readable (≈29–30 °C bench) |
| Full charge cycles | 242 | **locked** |
| Total Ah discharged | 2.5 kAh | **locked** |
| Min temperature | −0.2 °C | **locked** |
| Max temperature | 40.2 °C | **locked** |
| Fault | 1 error | DTCs readable |

> The `cycles = 0x0312/10 + SOC/100` and `Ah via 0x0210 = 2500` relations in
> the reader are **curve-fits to these reference numbers**, not verified
> decodings. They are left in the code as hypotheses, clearly marked.
