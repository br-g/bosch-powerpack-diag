# Sniffer — passive CAN logger

Records every CAN frame on the bus to a compact binary file. Strictly passive
(`silent=True`): it never transmits, so it is safe to leave inline on a live
bus.

## Flash it

CircuitPython exposes the board as a USB drive named `CIRCUITPY`.

```sh
cp boot.py /Volumes/CIRCUITPY/boot.py   # mounts the FS read-write
cp code.py /Volumes/CIRCUITPY/code.py
```

Then unplug/replug so `boot.py` takes effect. The board starts logging on power-up.

## Record

Watch the live journal over USB serial (optional):

```sh
ls /dev/tty.usbmodem*
screen /dev/tty.usbmodem101 115200
```

Each run writes two files to the board, numbered by a shared sequence counter:

- `can_XXXX.bin` — the compact binary capture
- `can_XXXX.log` — a text journal (start params, bus state, heartbeats)

## Retrieve & decode

```sh
cp /Volumes/CIRCUITPY/can_0001.bin .
python3 ../../tools/decode.py can_0001.bin can_0001.csv
```

## Binary format (v5)

Magic `CANL\x05`, then one record per frame:

```
flags (1 byte)  : 0x0F DLC | 0x10 extended | 0x20 repeat(dedup) | 0x40 RTR
id    (2 or 4)  : little-endian, 4 bytes if extended
dt    (2 bytes) : little-endian ms since previous frame (clamped to 65535)
data  (DLC)     : omitted when repeat bit set (identical to previous same-ID payload)
```

The repeat bit deduplicates back-to-back identical payloads per ID, which is
what keeps the file small on a chatty bus. `tools/decode.py` rebuilds the full
payloads and timestamps.

## Notes

- Bus: 500 kbit/s.
- `canio` does not expose overload frames or individual error frames; the log
  only reports controller state transitions and TEC/REC counters.
- Free space on the board: `df -h /Volumes/CIRCUITPY`. Delete old captures with
  `rm /Volumes/CIRCUITPY/can_*.bin`.
