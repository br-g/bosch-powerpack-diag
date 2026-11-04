# code.py - passive CAN logger (binary + variable-length ID + dedup)
#           + text journal (.log) + RTR + controller states + bounded FIFO drain
#
# Target: Adafruit Feather M4 CAN Express / CircuitPython + canio
#
# The logger is strictly passive (silent=True): it never transmits on the bus.
# It records every CAN frame it sees to a compact binary file, and writes a
# human-readable journal next to it. Decode the .bin on a PC with tools/decode.py.
import struct
import sys
import time

import board
import canio
import digitalio
import storage

boost = digitalio.DigitalInOut(board.BOOST_ENABLE)
boost.switch_to_output(True)
standby = digitalio.DigitalInOut(board.CAN_STANDBY)
standby.switch_to_output(False)
led = digitalio.DigitalInOut(board.LED)
led.switch_to_output()

can = canio.CAN(
    rx=board.CAN_RX, tx=board.CAN_TX, baudrate=500000, silent=True, auto_restart=True
)
listener = can.listen(timeout=0.1)

# --- sequence number shared between the .bin and .log files ---
try:
    with open("/seq.txt") as fp:
        n = int(fp.read())
except (OSError, ValueError):
    n = 0
n += 1
try:
    with open("/seq.txt", "w") as fp:
        fp.write(str(n))
except OSError:
    pass

LOGFILE = "/can_%04d.bin" % n
LOGTXT = "/can_%04d.log" % n
MAGIC = b"CANL\x05"  # v5: bit6 = remote frame (RTR)

# --- settings ---
CONSOLE = False  # True to also mirror the .log on the USB console
DRAIN_CAP = 64  # max frames handled per loop before a flush/poll
STATE_POLL_MS = 50
HEARTBEAT_MS = 60000
NEAR_FULL = 8  # in_waiting() >= threshold => suspected frame loss (tune to taste)
FLUSH_BYTES = 1024
FLUSH_MS = 1000

STATE_NAMES = {
    canio.BusState.ERROR_ACTIVE: "ERROR_ACTIVE",
    canio.BusState.ERROR_WARNING: "ERROR_WARNING",
    canio.BusState.ERROR_PASSIVE: "ERROR_PASSIVE",
    canio.BusState.BUS_OFF: "BUS_OFF",
}

t0 = time.monotonic_ns()

try:
    lf = open(LOGTXT, "w")
except OSError:
    lf = False


def log(s):
    ms = (time.monotonic_ns() - t0) // 1_000_000
    line = "[%d.%03d] %s\n" % (ms // 1000, ms % 1000, s)
    if CONSOLE:
        sys.stdout.write(line)
    if lf:
        try:
            lf.write(line)
            lf.flush()
        except OSError:
            pass


log("start  bin=%s  log=%s  fmt=%r" % (LOGFILE, LOGTXT, MAGIC))
log("baudrate=500000 silent=True auto_restart=True")
log("note: overload frames and individual error frames are not exposed by canio")

f = None
buf = bytearray()
last = {}
prev_ns = 0
written = 0

frames = 0
frames_data = 0
frames_rtr = 0
hwm = 0
near_active = False

prev_state = can.state
last_state_ms = 0
last_hb_ms = 0
last_flush_ms = 0
log(
    "state init %s (tec=%d rec=%d)"
    % (
        STATE_NAMES.get(prev_state, "?"),
        can.transmit_error_count,
        can.receive_error_count,
    )
)

while True:
    msg = listener.receive()  # blocking wait <=0.1s (idle)
    now_ns = time.monotonic_ns()

    # --- FIFO / loss monitoring (in_waiting IS A METHOD -> parentheses) ---
    waiting = listener.in_waiting()
    if waiting > hwm:
        hwm = waiting
    if waiting >= NEAR_FULL and not near_active:
        near_active = True
        log(
            "WARN rx fifo load: in_waiting=%d hwm=%d (possible loss)"
            % (waiting, hwm)
        )
    elif waiting <= NEAR_FULL // 2:
        near_active = False

    # --- handle the first frame then drain the queue, bounded by DRAIN_CAP ---
    # the cap guarantees we come back to flush/poll even on a saturated bus
    drained = 0
    while msg is not None:
        frames += 1
        if f is None:
            try:
                f = open(LOGFILE, "wb")
                f.write(MAGIC)
                prev_ns = now_ns
                log("bin opened: %s" % LOGFILE)
            except OSError:
                f = False
                log("ERROR opening/writing bin")
        if f:
            dt = (now_ns - prev_ns) // 1_000_000
            if dt > 65535:
                dt = 65535
            prev_ns = now_ns
            ext = 1 if msg.extended else 0
            if isinstance(msg, canio.RemoteTransmissionRequest):
                frames_rtr += 1
                flags = (msg.length & 0x0F) | (ext << 4) | (1 << 6)
                if ext:
                    buf += struct.pack("<BIH", flags, msg.id, dt)
                else:
                    buf += struct.pack("<BHH", flags, msg.id, dt)
            else:
                frames_data += 1
                d = bytes(msg.data)
                rpt = 1 if last.get(msg.id) == d else 0
                flags = (len(d) & 0x0F) | (ext << 4) | (rpt << 5)
                if ext:
                    buf += struct.pack("<BIH", flags, msg.id, dt)
                else:
                    buf += struct.pack("<BHH", flags, msg.id, dt)
                if not rpt:
                    buf += d
                    last[msg.id] = d
        drained += 1
        if drained >= DRAIN_CAP or not listener.in_waiting():
            break
        msg = listener.receive()  # immediate: in_waiting() > 0
        now_ns = time.monotonic_ns()

    now_ms = (now_ns - t0) // 1_000_000  # recompute after the burst

    # --- CAN controller state (rate-limited) ---
    if now_ms - last_state_ms >= STATE_POLL_MS:
        last_state_ms = now_ms
        st = can.state
        if st is not prev_state:
            tec = can.transmit_error_count
            rec = can.receive_error_count
            log(
                "STATE %s -> %s (tec=%d rec=%d)"
                % (
                    STATE_NAMES.get(prev_state, "?"),
                    STATE_NAMES.get(st, "?"),
                    tec,
                    rec,
                )
            )
            if st is canio.BusState.BUS_OFF:
                log("BUS_OFF (auto_restart=%s)" % can.auto_restart)
            elif prev_state is canio.BusState.BUS_OFF:
                log("controller restarted")
            prev_state = st

    # --- periodic status ---
    if now_ms - last_hb_ms >= HEARTBEAT_MS:
        last_hb_ms = now_ms
        log(
            "hb frames=%d (data=%d rtr=%d) bytes=%d hwm=%d state=%s tec=%d rec=%d"
            % (
                frames,
                frames_data,
                frames_rtr,
                written,
                hwm,
                STATE_NAMES.get(can.state, "?"),
                can.transmit_error_count,
                can.receive_error_count,
            )
        )

    # --- binary flush ---
    if f and buf and (len(buf) >= FLUSH_BYTES or now_ms - last_flush_ms > FLUSH_MS):
        try:
            f.write(buf)
            f.flush()
            written += len(buf)
            buf = bytearray()
            last_flush_ms = now_ms
        except OSError:
            log("WARN bin write failed, %d bytes lost" % len(buf))
            buf = bytearray()
            last_flush_ms = now_ms
