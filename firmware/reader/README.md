# Reader — read-only diagnostic session

Replays the minimal UDS exchange the official Bosch tool performs, **without
authentication**, and prints what the BMS returns. It only reads: no clear, no
write, no routine, no SecurityAccess key.

## Flash it

```sh
cp code.py /Volumes/CIRCUITPY/code.py
```

The reader needs the same `boot.py` as the sniffer if you want it to write
files; by default it only prints to the USB serial console, so `boot.py` is
optional here.

## Watch it

```sh
ls /dev/tty.usbmodem*
screen /dev/tty.usbmodem101 115200
```

On start it waits `BOOT_DELAY` (10 s) so you can attach the console, sends a
keep-alive (`0x061 = 00` every 0.5 s) to keep the pack awake, then runs one
diagnostic round. See `../../data/reader_output_example.txt` for a sample.

## What it reads

| Works unauthenticated | Locked behind SecurityAccess |
|-----------------------|------------------------------|
| Pack voltage (`0x2AA`) | Charge-cycle count |
| Temperature (`0x2AA`) | Total Ah discharged |
| Fault codes (DTC `606011`, `655003`) | Min/max recorded temperature |
| Status DID `0x0501` | Life energy counters |

Locked DIDs answer `7F 22 33` (NRC `0x33`, securityAccessDenied). The seed/key
algorithm behind `27 11`/`27 12` was not recovered. Full details in
[`../../docs/protocol.md`](../../docs/protocol.md).

## Wiring

Tap CAN-H / CAN-L on the pack's bus. The pack must be powered (keep-alive alone
does not power it). See the top-level README for the bench setup.
