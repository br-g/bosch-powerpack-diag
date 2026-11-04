#!/usr/bin/env python3
# decode.py - decoder for the CAN logger .bin files (format v5). Run on a PC.
#
# The sniffer firmware (firmware/sniffer/code.py) writes a compact binary stream
# with per-ID payload deduplication. This reconstructs full frames and emits
# either a readable listing or a CSV.
import sys
from collections import namedtuple

MAGIC = b"CANL\x05"

# flags byte: 0x0F=DLC  0x10=ext  0x20=rpt(dedup)  0x40=RTR
F_EXT = 0x10
F_RPT = 0x20
F_RTR = 0x40

Frame = namedtuple("Frame", "index t_ms id ext rtr rpt dlc data")


class TruncatedFile(Exception):
    pass


def _read(fp, n):
    b = fp.read(n)
    return b if len(b) == n else None


def iter_frames(fp):
    """Decode a .bin stream and yield Frame tuples.
    Rebuilds deduplicated payloads and accumulates dt -> relative timestamp (ms)."""
    magic = fp.read(len(MAGIC))
    if magic != MAGIC:
        raise ValueError("invalid magic: %r (expected %r)" % (magic, MAGIC))
    last = {}  # id -> last known payload (dedup)
    t_ms = 0
    index = 0
    while True:
        head = fp.read(1)
        if not head:
            return  # clean end (on a record boundary)
        flags = head[0]
        ext = bool(flags & F_EXT)
        rpt = bool(flags & F_RPT)
        rtr = bool(flags & F_RTR)
        dlc = flags & 0x0F

        idb = _read(fp, 4 if ext else 2)
        if idb is None:
            raise TruncatedFile("ID truncated at record %d" % index)
        can_id = int.from_bytes(idb, "little")

        dtb = _read(fp, 2)
        if dtb is None:
            raise TruncatedFile("dt truncated at record %d" % index)
        t_ms += int.from_bytes(dtb, "little")

        if rtr:
            data = b""  # remote frame: no payload, dlc = requested length
        elif rpt:
            data = last.get(can_id, b"")  # payload identical to the previous same ID
        else:
            data = _read(fp, dlc)
            if data is None:
                raise TruncatedFile("data truncated at record %d (dlc=%d)" % (index, dlc))
            last[can_id] = data

        yield Frame(index, t_ms, can_id, ext, rtr, rpt, dlc, data)
        index += 1


def fmt(fr):
    ts = "%11.3f" % (fr.t_ms / 1000.0)
    kind = "X" if fr.ext else "S"
    idstr = ("%08X" if fr.ext else "%03X") % fr.id
    if fr.rtr:
        body = "remote (len=%d)" % fr.dlc
    else:
        body = " ".join("%02X" % x for x in fr.data) or "-"
        if fr.rpt:
            body += "   (rpt)"
    return "%s  %s  %-8s  [%d]  %s" % (ts, kind, idstr, fr.dlc, body)


def main(argv):
    import argparse

    ap = argparse.ArgumentParser(description="CAN logger decoder (format v5)")
    ap.add_argument("file", help=".bin file to decode")
    ap.add_argument(
        "csv", nargs="?", help="path of the generated .csv (otherwise readable output)"
    )
    ap.add_argument("--limit", type=int, default=0, help="stop after N frames")
    args = ap.parse_args(argv)

    n = nd = nr = nrep = 0
    last_t = 0

    csv_w = csv_fp = None
    if args.csv:
        import csv

        csv_fp = open(args.csv, "w", newline="")
        csv_w = csv.writer(csv_fp)
        csv_w.writerow(["t_ms", "id_hex", "ext", "rtr", "rpt", "dlc", "data_hex"])

    try:
        with open(args.file, "rb") as fp:
            try:
                for fr in iter_frames(fp):
                    n += 1
                    if fr.rtr:
                        nr += 1
                    else:
                        nd += 1
                        nrep += fr.rpt
                    last_t = fr.t_ms
                    if csv_w:
                        csv_w.writerow(
                            [
                                fr.t_ms,
                                "%X" % fr.id,
                                int(fr.ext),
                                int(fr.rtr),
                                int(fr.rpt),
                                fr.dlc,
                                fr.data.hex(),
                            ]
                        )
                    else:
                        print(fmt(fr))
                    if args.limit and n >= args.limit:
                        break
            except TruncatedFile as e:
                print("warning: truncated file - %s" % e, file=sys.stderr)
    finally:
        if csv_fp:
            csv_fp.close()

    print(
        "summary: %d frames (data=%d, rtr=%d, of which %d deduplicated) duration~=%.3fs"
        % (n, nd, nr, nrep, last_t / 1000.0),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main(sys.argv[1:])
