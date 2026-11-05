# ============================================================================
# code.py - read-only Bosch PowerPack BMS diagnostic reader
# Target: Adafruit Feather M4 CAN Express / CircuitPython + canio
#
# READ-ONLY reader for the diagnostic data observed in the capture
# (data/can_0066.*). It replays the minimal UDS exchange the official Bosch
# tool performs, without authentication.
#
# This program NEVER sends:
#   - SecurityAccess 0x27
#   - ClearDiagnosticInformation 0x14
#   - WriteDataByIdentifier 0x2E
#   - RoutineControl 0x31
#
# It reads:
#   - SOC (state of charge)
#   - pack voltage
#   - current temperature
#   - historical min/max temperature     (locked: needs SecurityAccess)
#   - Bosch DTCs (e.g. 606011 / 655003 / 61000E)
#   - life counters DID 0x0313 / 0x0312  (locked: needs SecurityAccess)
#   - total-Ah candidate via 0x20/0x21 DID 0x0210 (optional)
#
# Fields marked "locked" return NRC 0x33 (securityAccessDenied) on this pack;
# they are only read after a successful 0x27 handshake, whose key algorithm is
# unknown. See docs/protocol.md.
# ============================================================================

import time

import board
import canio
import digitalio

# ---------------- General configuration ----------------

BAUDRATE = 500000
BOOT_DELAY = 10.0

# Keep-alive observed as necessary to wake/keep the BMS awake.
USE_KEEP_ALIVE = True
KA_ID = 0x061
KA_DATA = b"\x00"
KA_INTERVAL = 0.5

# Battery UDS diagnostic addressing, from the Bosch capture.
BATT_REQ_ID = 0x22
BATT_RESP_ID = 0x23

# Auxiliary endpoint seen in the capture.
# Used for the exact 2500 Ah candidate via DID 0x0210.
# Set to False to query only the BMS on 0x22/0x23.
READ_AH_CANDIDATE_FROM_20_21 = True
AUX_REQ_ID = 0x20
AUX_RESP_ID = 0x21

# The Bosch capture enters the extended session 10 03 before the reads.
# This is not an authentication step.
ENTER_EXTENDED_SESSION = True

# A single UDS burst, then passive listening.
# Set to False to repeat the UDS reads every DIAG_PERIOD seconds.
RUN_DIAG_ONCE = True
DIAG_PERIOD = 60.0

PRINT_RAW_UDS = True
PASSIVE_PRINT_PERIOD = 5.0

# ---------------- Identified DIDs ----------------

DID_SOC = 0x0415          # data[0] = SOC %
DID_TEMP_NOW = 0x0433     # uint16 BE, centiKelvin
DID_TEMP_MAX = 0x0222     # uint16 BE, centiKelvin
DID_TEMP_MIN = 0x0224     # uint16 BE, centiKelvin
DID_LIFE_HI = 0x0313      # life counter, high word
DID_LIFE_LO = 0x0312      # life counter, low word
DID_AH_AUX = 0x0210       # on 0x20/0x21: 0x09C4 = 2500 in the capture

# ---------------- Hardware init ----------------

print("# --- code.py: read-only Bosch BMS diagnostic reader ---")
time.sleep(BOOT_DELAY)

boost = digitalio.DigitalInOut(board.BOOST_ENABLE)
boost.switch_to_output(True)

standby = digitalio.DigitalInOut(board.CAN_STANDBY)
standby.switch_to_output(False)

can = canio.CAN(
    rx=board.CAN_RX,
    tx=board.CAN_TX,
    baudrate=BAUDRATE,
    auto_restart=True,
)

listener = can.listen(timeout=0.01)
keep_alive = canio.Message(id=KA_ID, data=KA_DATA, extended=False)

print("# CAN bus OK @ %d baud" % BAUDRATE)
print("# No SecurityAccess 0x27, no clear/write/routine.")

# ---------------- Global state ----------------

t0 = time.monotonic()
last_ka = 0.0
last_passive_print = 0.0

passive = {
    "soc_203": None,
    "voltage_v_2aa": None,
    "temp_c_2aa": None,
    "ah_raw_108": None,
}


# ---------------- Utilities ----------------

def now_ms():
    return int((time.monotonic() - t0) * 1000)


def hex_bytes(data):
    s = ""
    for b in data:
        s += "%02X" % b
    return s


def pad8(data):
    if len(data) >= 8:
        return data[:8]
    return data + (b"\x00" * (8 - len(data)))


def u16be(data, offset):
    return (data[offset] << 8) | data[offset + 1]


def centikelvin_to_c(raw):
    return raw / 100.0 - 273.15


def fmt_none(value, pattern):
    if value is None:
        return "NA"
    return pattern % value


def send_keep_alive_if_due():
    global last_ka

    if not USE_KEEP_ALIVE:
        return

    now = time.monotonic()
    if now - last_ka >= KA_INTERVAL:
        try:
            can.send(keep_alive)
        except Exception as e:
            print("# keep-alive ERROR: %s" % e)
        last_ka = now


# ---------------- Passive decoding ----------------

def handle_passive(msg):
    if isinstance(msg, canio.RemoteTransmissionRequest):
        return

    if getattr(msg, "extended", False):
        return

    data = bytes(msg.data)
    mid = msg.id

    # 0x203: SOC observed in byte 4.
    # Capture example: 00 00 04 D3 62 00 00 00 -> 0x62 = 98 %
    if mid == 0x203 and len(data) >= 5:
        passive["soc_203"] = data[4]

    # 0x2AA:
    # bytes 0..1 = temperature in centiKelvin
    # bytes 2..3 = pack voltage in millivolts
    # Example: 75 9D A2 54 -> 27.94 C and 41.556 V
    elif mid == 0x2AA and len(data) >= 4:
        temp_raw = u16be(data, 0)
        volt_raw = u16be(data, 2)
        passive["temp_c_2aa"] = centikelvin_to_c(temp_raw)
        passive["voltage_v_2aa"] = volt_raw / 1000.0

    # 0x108: candidate observed for 2500 Ah -> 2.5 kAh, bytes 2..3.
    # Caution: depending on the setup, this frame may come from another ECU.
    elif mid == 0x108 and len(data) >= 4:
        passive["ah_raw_108"] = u16be(data, 2)


def drain_for(duration_s):
    end = time.monotonic() + duration_s

    while time.monotonic() < end:
        send_keep_alive_if_due()
        msg = listener.receive()
        if msg is not None:
            handle_passive(msg)


def print_passive_summary():
    print(
        "# passive: SOC_0x203=%s %% | V_0x2AA=%s V | T_0x2AA=%s C | Ah_0x108=%s Ah"
        % (
            fmt_none(passive["soc_203"], "%d"),
            fmt_none(passive["voltage_v_2aa"], "%.3f"),
            fmt_none(passive["temp_c_2aa"], "%.2f"),
            fmt_none(passive["ah_raw_108"], "%d"),
        )
    )


# ---------------- Minimal ISO-TP layer ----------------

def uds_request(payload, tx_id, rx_id, timeout_s=1.2):
    """
    Send a single-frame UDS request and return the reassembled UDS payload.

    Example TX payload: 22 04 15
    Example RX payload: 62 04 15 62

    Supports:
      - single-frame response
      - multi-frame response with FlowControl CTS
    """

    if len(payload) > 7:
        raise ValueError("UDS request too long for a single frame")

    # Light RX queue cleanup before the request.
    drain_for(0.05)

    req_frame = pad8(bytes([len(payload)]) + payload)

    if PRINT_RAW_UDS:
        print("# UDS TX %03X  %s" % (tx_id, hex_bytes(req_frame)))

    try:
        can.send(canio.Message(id=tx_id, data=req_frame, extended=False))
    except Exception as e:
        print("# UDS TX ERROR %03X: %s" % (tx_id, e))
        return None

    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        send_keep_alive_if_due()

        msg = listener.receive()
        if msg is None:
            continue

        handle_passive(msg)

        if isinstance(msg, canio.RemoteTransmissionRequest):
            continue
        if getattr(msg, "extended", False):
            continue
        if msg.id != rx_id:
            continue

        frame = bytes(msg.data)
        if len(frame) < 1:
            continue

        if PRINT_RAW_UDS:
            print("# UDS RX %03X  %s" % (rx_id, hex_bytes(frame)))

        pci_type = frame[0] >> 4

        # Single frame.
        if pci_type == 0x0:
            length = frame[0] & 0x0F
            return bytes(frame[1:1 + length])

        # First frame.
        if pci_type == 0x1:
            total_len = ((frame[0] & 0x0F) << 8) | frame[1]
            data = bytearray(frame[2:])

            # FlowControl CTS.
            # 30 = ContinueToSend, BS=0x10, STmin=5 ms.
            fc = bytes([0x30, 0x10, 0x05, 0, 0, 0, 0, 0])

            if PRINT_RAW_UDS:
                print("# UDS TX %03X  %s  # FlowControl" % (tx_id, hex_bytes(fc)))

            try:
                can.send(canio.Message(id=tx_id, data=fc, extended=False))
            except Exception as e:
                print("# FlowControl ERROR: %s" % e)
                return None

            expected_sn = 1

            while len(data) < total_len and time.monotonic() < deadline:
                send_keep_alive_if_due()

                msg2 = listener.receive()
                if msg2 is None:
                    continue

                handle_passive(msg2)

                if isinstance(msg2, canio.RemoteTransmissionRequest):
                    continue
                if getattr(msg2, "extended", False):
                    continue
                if msg2.id != rx_id:
                    continue

                cf = bytes(msg2.data)
                if len(cf) < 1:
                    continue

                if PRINT_RAW_UDS:
                    print("# UDS RX %03X  %s" % (rx_id, hex_bytes(cf)))

                if (cf[0] >> 4) != 0x2:
                    continue

                sn = cf[0] & 0x0F
                if sn != expected_sn:
                    print(
                        "# ISO-TP warning: expected sequence %X, received %X"
                        % (expected_sn, sn)
                    )

                expected_sn = (expected_sn + 1) & 0x0F
                data.extend(cf[1:])

            if len(data) >= total_len:
                return bytes(data[:total_len])

            print("# ISO-TP multi-frame timeout: %d/%d bytes" % (len(data), total_len))
            return None

    return None


def uds_negative(resp):
    return resp is not None and len(resp) >= 3 and resp[0] == 0x7F


def read_did(did, tx_id=BATT_REQ_ID, rx_id=BATT_RESP_ID):
    req = bytes([0x22, (did >> 8) & 0xFF, did & 0xFF])
    resp = uds_request(req, tx_id, rx_id)

    if resp is None:
        print("# DID %04X: no response" % did)
        return None

    if uds_negative(resp):
        print("# DID %04X: negative response 7F %02X %02X" % (did, resp[1], resp[2]))
        return None

    if (
        len(resp) < 3
        or resp[0] != 0x62
        or resp[1] != ((did >> 8) & 0xFF)
        or resp[2] != (did & 0xFF)
    ):
        print("# DID %04X: unexpected response %s" % (did, hex_bytes(resp)))
        return None

    return resp[3:]


def enter_extended_session():
    resp = uds_request(b"\x10\x03", BATT_REQ_ID, BATT_RESP_ID)

    if resp is None:
        print("# Extended session: no response")
        return False

    if uds_negative(resp):
        print("# Extended session refused: %s" % hex_bytes(resp))
        return False

    ok = len(resp) >= 2 and resp[0] == 0x50 and resp[1] == 0x03
    print("# Extended session 10 03: %s (%s)" % ("OK" if ok else "unexpected response", hex_bytes(resp)))
    return ok


def read_dtcs_battery():
    # Service 19 02 08:
    # ReadDTCInformation / reportDTCByStatusMask.
    resp = uds_request(b"\x19\x02\x08", BATT_REQ_ID, BATT_RESP_ID, timeout_s=1.5)

    if resp is None:
        print("# DTC: no response")
        return None

    if uds_negative(resp):
        print("# DTC: negative response %s" % hex_bytes(resp))
        return None

    if len(resp) < 3 or resp[0] != 0x59 or resp[1] != 0x02:
        print("# DTC: unexpected response %s" % hex_bytes(resp))
        return None

    status_availability = resp[2]
    dtcs = []

    i = 3
    while i + 3 < len(resp):
        b0 = resp[i]
        b1 = resp[i + 1]
        b2 = resp[i + 2]
        status = resp[i + 3]

        # Bosch displays the 3 DTC bytes as a hex string:
        # 60 60 11 -> 606011
        code = "%02X%02X%02X" % (b0, b1, b2)
        dtcs.append((code, status))

        i += 4

    return status_availability, dtcs


# ---------------- Full diagnostic read ----------------

def run_diagnostic_round():
    print("")
    print("# ==================== BMS DIAGNOSTIC READ ====================")
    print("# t=%d ms" % now_ms())

    # Short passive capture for 0x203 / 0x2AA.
    drain_for(0.5)

    if ENTER_EXTENDED_SESSION:
        enter_extended_session()
        drain_for(0.05)

    # SOC via UDS.
    soc_uds = None
    d = read_did(DID_SOC)
    if d is not None and len(d) >= 1:
        soc_uds = d[0]

    # Current temperature.
    temp_now = None
    d = read_did(DID_TEMP_NOW)
    if d is not None and len(d) >= 2:
        temp_now = centikelvin_to_c(u16be(d, 0))

    # Max temperature.
    temp_max = None
    d = read_did(DID_TEMP_MAX)
    if d is not None and len(d) >= 2:
        temp_max = centikelvin_to_c(u16be(d, 0))

    # Min temperature.
    temp_min = None
    d = read_did(DID_TEMP_MIN)
    if d is not None and len(d) >= 2:
        temp_min = centikelvin_to_c(u16be(d, 0))

    # Life counters.
    life_hi = None
    life_lo = None

    d = read_did(DID_LIFE_HI)
    if d is not None and len(d) >= 2:
        life_hi = u16be(d, 0)

    d = read_did(DID_LIFE_LO)
    if d is not None and len(d) >= 2:
        life_lo = u16be(d, 0)

    life_counter = None
    life_kwh_est = None
    cycles_est = None
    cycles_round = None

    if life_hi is not None and life_lo is not None:
        life_counter = (life_hi << 16) | life_lo

        # Hypothesis from the capture:
        # 0x000E0968 = 919912 counts
        # 919912 * 0.1 Wh = 91.991 kWh
        life_kwh_est = life_counter * 0.1 / 1000.0

        # Experimental hypothesis that lands on 242 with:
        # DID 0x0312 = 0x0968 = 2408
        # SOC = 98 %
        # 2408 / 10 + 0.98 = 241.78 -> 242
        if soc_uds is not None:
            cycles_est = (life_lo / 10.0) + (soc_uds / 100.0)
            cycles_round = int(cycles_est + 0.5)

    # Total Ah discharged: exact candidate via 0x20/0x21 DID 0x0210.
    ah_aux = None
    if READ_AH_CANDIDATE_FROM_20_21:
        d = read_did(DID_AH_AUX, AUX_REQ_ID, AUX_RESP_ID)
        if d is not None and len(d) >= 2:
            ah_aux = u16be(d, 0)

    # DTCs.
    dtc_info = read_dtcs_battery()

    # Final short listen to refresh the passive voltage.
    drain_for(0.3)

    print("# -------------------- RESULTS --------------------")
    print("SOC UDS DID 0x0415              : %s %%" % fmt_none(soc_uds, "%d"))
    print("SOC passive ID 0x203            : %s %%" % fmt_none(passive["soc_203"], "%d"))
    print("Voltage passive ID 0x2AA       : %s V" % fmt_none(passive["voltage_v_2aa"], "%.3f"))
    print("Current temperature DID 0x0433 : %s C" % fmt_none(temp_now, "%.2f"))
    print("Temperature passive ID 0x2AA   : %s C" % fmt_none(passive["temp_c_2aa"], "%.2f"))
    print("Max temperature DID 0x0222     : %s C" % fmt_none(temp_max, "%.2f"))
    print("Min temperature DID 0x0224     : %s C" % fmt_none(temp_min, "%.2f"))

    if life_counter is None:
        print("Life counter DID 0x0313/0x0312 : NA")
    else:
        print(
            "Life counter DID 0x0313/0x0312 : 0x%04X%04X = %d"
            % (life_hi, life_lo, life_counter)
        )
        print(
            "Estimated life energy          : %.1f kWh  # hypothesis 0.1 Wh/count"
            % life_kwh_est
        )

    # Total Ah: prefer the UDS 0x20/0x21 value if available.
    if ah_aux is not None:
        print(
            "Ah discharged candidate        : %d Ah = %.3f kAh  # UDS 0x20/0x21 DID 0x0210"
            % (ah_aux, ah_aux / 1000.0)
        )
    elif passive["ah_raw_108"] is not None:
        print(
            "Ah discharged candidate        : %d Ah = %.3f kAh  # passive ID 0x108 bytes 2..3"
            % (passive["ah_raw_108"], passive["ah_raw_108"] / 1000.0)
        )
    else:
        print("Ah discharged candidate        : NA")

    if cycles_round is not None:
        print(
            "Cycles candidate (experimental): %.2f -> %d  # derived from DID 0x0312 + SOC"
            % (cycles_est, cycles_round)
        )
    else:
        print("Cycles candidate (experimental): NA")

    if dtc_info is None:
        print("Error codes (DTC)              : NA")
    else:
        status_availability, dtcs = dtc_info

        if len(dtcs) == 0:
            print("Error codes (DTC)              : none  # mask avail 0x%02X" % status_availability)
        else:
            codes = ""
            for n, item in enumerate(dtcs):
                code, status = item
                if n > 0:
                    codes += ", "
                codes += "%s(status=0x%02X)" % (code, status)

            print(
                "Error codes (DTC)              : %s  # mask avail 0x%02X"
                % (codes, status_availability)
            )

    try:
        print(
            "# bus=%s tec=%d rec=%d"
            % (can.state, can.transmit_error_count, can.receive_error_count)
        )
    except Exception:
        pass

    print("# =============================================================")


# ---------------- Main loop ----------------

print("# Starting passive listen / keep-alive...")
drain_for(1.0)
print_passive_summary()

next_diag = time.monotonic() + 0.2
ran_once = False

while True:
    now = time.monotonic()
    send_keep_alive_if_due()

    msg = listener.receive()
    if msg is not None:
        handle_passive(msg)

    if (not RUN_DIAG_ONCE or not ran_once) and now >= next_diag:
        run_diagnostic_round()
        ran_once = True
        next_diag = time.monotonic() + DIAG_PERIOD

    if now - last_passive_print >= PASSIVE_PRINT_PERIOD:
        print_passive_summary()
        last_passive_print = now
