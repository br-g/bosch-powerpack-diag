# Bosch PowerPack 400 (BES2) — CAN diagnostics

Reverse-engineering notes and tools for reading diagnostic data from a **Bosch
PowerPack Frame 400** e-bike battery over its CAN bus, using cheap hobbyist
hardware instead of the official Bosch dealer tool.

Two things live here:

1. A **passive CAN sniffer** that captured a real diagnostic session performed
   with the official Bosch software (battery on a compatible new bike).
2. A **read-only reader** that replays part of that session on its own, with no
   Bosch tool, reading fault codes, temperature and pack voltage directly from
   the BMS.

This is an archived, as-is dump of a finished personal project, shared so others
can pick it up. It is **not** affiliated with or endorsed by Bosch.

## What works

Without any authentication you can read:

- **Fault codes** (for my battery, DTCs `606011` and `655003`)
- **Measured temperature inside the pack**
- **Pack voltage** (and state of charge, passively)

## What does not

The BMS gates its history behind **UDS SecurityAccess (0x27)**. These stay
locked (`NRC 0x33`, securityAccessDenied) and were **not** recovered:

- Charge-cycle count
- Total Ah discharged
- Min / max recorded temperatures

Requesting a seed (`27 11`) returns a 16-byte challenge; the seed→key algorithm
is unknown, so the matching key (`27 12`) was never sent. If you crack that, the
rest of the DIDs listed in [`docs/protocol.md`](docs/protocol.md) should open up.

## Hardware

- **Adafruit Feather M4 CAN Express** (ATSAME51 + built-in CAN transceiver)
- CircuitPython, `canio` module
- CAN bus at **500 kbit/s**, tapped on the pack's CAN-H / CAN-L

No extra transceiver or shield is needed, the Feather M4 CAN has one onboard.

## Layout

```
firmware/
  sniffer/   passive CAN logger (never transmits)     → firmware/sniffer/README.md
  reader/    read-only diagnostic replay              → firmware/reader/README.md
tools/
  decode.py  decode sniffer .bin captures to CSV
data/
  can_0066.*            the captured Bosch diagnostic session (bin + log + csv)
  official_diag_report.pdf   official Bosch report for the same pack (anonymized)
  reader_output_example.txt  sample serial output of the reader
docs/
  protocol.md   CAN IDs, UDS DIDs, DTCs, the security gate, reference values
```

## Reproduce

Sniff a session, or just decode the included capture:

```sh
python3 tools/decode.py data/can_0066.bin /tmp/out.csv
```

To talk to a pack yourself, flash `firmware/reader/code.py` and open the serial
console - see [`firmware/reader/README.md`](firmware/reader/README.md). Start
from the sniffer if you want to capture your own reference session first.

## Safety & scope

- The sniffer is strictly passive and safe to leave on a live bus.
- The reader only reads. It never writes, clears, resets, or sends a
  SecurityAccess key.
- All findings come from **one** pack / firmware / bike. IDs and offsets may
  differ on yours. Verify before trusting any value.
- Work on your own battery, at your own risk.

## License

[MIT](LICENSE) © 2026 Bruno Godefroy
