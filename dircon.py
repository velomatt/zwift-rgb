#!/usr/bin/env python3
"""Decode Wahoo DirCon (BLE-GATT-over-TCP) traffic from a pcap file or stdin.

Usage:
  dircon.py capture.pcap                 # offline
  tcpdump -U -w - host <trainer-ip> | dircon.py -   # live

No third-party deps. Handles classic pcap with Ethernet / Linux SLL / SLL2.
"""
import struct
import sys

MSG_NAMES = {
    0x01: "DiscoverServices",
    0x02: "DiscoverChars",
    0x03: "ReadChar",
    0x04: "WriteChar",
    0x05: "EnableNotify",
    0x06: "Notification",
}
CHAR_NAMES = {
    0x2A63: "CyclingPower",
    0x2AD2: "IndoorBikeData",
    0x2A5B: "CSCMeasurement",
    0x2A37: "HeartRate",
    0x2AD9: "FTMSControlPoint",
    0x2ADA: "FTMSStatus",
    0x2AD3: "TrainingStatus",
    0x2A66: "CPControlPoint",
}
BLE_BASE_TAIL = bytes.fromhex("00001000800000805f9b34fb")


def uuid_str(b):
    if len(b) == 16 and b[0:2] == b"\x00\x00" and b[4:] == BLE_BASE_TAIL:
        short = struct.unpack(">H", b[2:4])[0]
        return f"{short:04X}", short
    return b.hex(), None


def decode_cycling_power(d):
    if len(d) < 4:
        return {}
    flags, power = struct.unpack_from("<Hh", d, 0)
    out = {"power": power}
    i = 4
    if flags & 0x01:  # pedal power balance
        i += 1
    if flags & 0x04:  # accumulated torque
        i += 2
    if flags & 0x10:  # wheel revs
        i += 6
    if flags & 0x20 and len(d) >= i + 4:  # crank revs
        revs, t = struct.unpack_from("<HH", d, i)
        out["crank_revs"] = revs
        out["crank_t"] = t
    return out


def decode_indoor_bike(d):
    if len(d) < 2:
        return {}
    flags = struct.unpack_from("<H", d, 0)[0]
    i = 2
    out = {}

    def take(fmt):
        nonlocal i
        if len(d) < i + struct.calcsize(fmt):
            return None
        v = struct.unpack_from(fmt, d, i)[0]
        i += struct.calcsize(fmt)
        return v

    if not flags & 0x0001:
        v = take("<H")
        if v is not None:
            out["speed_kmh"] = v / 100
    if flags & 0x0002:
        take("<H")
    if flags & 0x0004:
        v = take("<H")
        if v is not None:
            out["cadence"] = v / 2
    if flags & 0x0008:
        take("<H")
    if flags & 0x0010:
        if len(d) >= i + 3:
            i += 3
    if flags & 0x0020:
        take("<h")
    if flags & 0x0040:
        v = take("<h")
        if v is not None:
            out["power"] = v
    if flags & 0x0080:
        take("<h")
    return out


def decode_hr(d):
    if len(d) < 2:
        return {}
    flags = d[0]
    hr = struct.unpack_from("<H", d, 1)[0] if flags & 1 and len(d) >= 3 else d[1]
    return {"hr": hr}


DECODERS = {0x2A63: decode_cycling_power, 0x2AD2: decode_indoor_bike, 0x2A37: decode_hr}


class Stream:
    def __init__(self):
        self.buf = bytearray()
        self.next_seq = None

    def feed(self, seq, payload):
        if not payload:
            return
        if self.next_seq is None:
            self.next_seq = seq
        if seq < self.next_seq:  # retransmit / overlap
            skip = self.next_seq - seq
            if skip >= len(payload):
                return
            payload = payload[skip:]
            seq = self.next_seq
        if seq > self.next_seq:  # gap (lost capture); resync
            self.buf.clear()
        self.buf += payload
        self.next_seq = seq + len(payload)

    def messages(self):
        while len(self.buf) >= 6:
            ver, mid, sq, rc, ln = struct.unpack_from(">BBBBH", self.buf, 0)
            if ver != 1 or mid not in MSG_NAMES:
                # lost framing: drop a byte and try to resync
                del self.buf[0]
                continue
            if len(self.buf) < 6 + ln:
                return
            body = bytes(self.buf[6 : 6 + ln])
            del self.buf[: 6 + ln]
            yield mid, sq, rc, body


def read_pcap(f):
    hdr = f.read(24)
    if len(hdr) < 24:
        return
    magic = hdr[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        e = "<"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        e = ">"
    else:
        sys.exit("not a classic pcap (use tcpdump -w, not pcapng)")
    nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    linktype = struct.unpack(e + "I", hdr[20:24])[0]
    while True:
        rh = f.read(16)
        if len(rh) < 16:
            return
        ts_s, ts_u, incl, _ = struct.unpack(e + "IIII", rh)
        data = f.read(incl)
        if len(data) < incl:
            return
        yield ts_s + ts_u / (1e9 if nano else 1e6), linktype, data


def ip_tcp(linktype, pkt):
    if linktype == 1:  # Ethernet
        off, etype = 14, struct.unpack_from(">H", pkt, 12)[0]
        if etype == 0x8100:
            etype = struct.unpack_from(">H", pkt, 16)[0]
            off = 18
    elif linktype == 113:  # Linux SLL
        off, etype = 16, struct.unpack_from(">H", pkt, 14)[0]
    elif linktype == 276:  # SLL2
        off, etype = 20, struct.unpack_from(">H", pkt, 0)[0]
    else:
        return None
    if etype != 0x0800 or len(pkt) < off + 20:
        return None
    ihl = (pkt[off] & 0x0F) * 4
    if pkt[off + 9] != 6:
        return None
    tot = struct.unpack_from(">H", pkt, off + 2)[0]
    src = ".".join(map(str, pkt[off + 12 : off + 16]))
    dst = ".".join(map(str, pkt[off + 16 : off + 20]))
    t = off + ihl
    sport, dport, seq = struct.unpack_from(">HHI", pkt, t)
    doff = (pkt[t + 12] >> 4) * 4
    payload = pkt[t + doff : off + tot]
    return src, sport, dst, dport, seq, payload


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "-"
    f = sys.stdin.buffer if path == "-" else open(path, "rb")
    streams = {}
    t0 = None
    counts = {}
    for ts, lt, pkt in read_pcap(f):
        r = ip_tcp(lt, pkt)
        if not r:
            continue
        src, sport, dst, dport, seq, payload = r
        if t0 is None:
            t0 = ts
        key = (src, sport, dst, dport)
        st = streams.setdefault(key, Stream())
        st.feed(seq, payload)
        for mid, sq, rc, body in st.messages():
            name = MSG_NAMES[mid]
            counts[name] = counts.get(name, 0) + 1
            rel = f"{ts - t0:8.3f}"
            arrow = f"{src}:{sport}->{dst}:{dport}"
            if mid == 0x06 and len(body) >= 16:
                u, short = uuid_str(body[:16])
                data = body[16:]
                dec = DECODERS.get(short)
                info = dec(data) if dec else {}
                label = CHAR_NAMES.get(short, u)
                print(f"{rel} NOTIFY {label:16} {data.hex():24} {info}", flush=True)
            else:
                extra = ""
                if len(body) >= 16 and mid in (0x02, 0x03, 0x04, 0x05):
                    u, short = uuid_str(body[:16])
                    extra = f"{CHAR_NAMES.get(short, u)} {body[16:].hex()}"
                elif mid == 0x01 and body:
                    extra = " ".join(uuid_str(body[i : i + 16])[0] for i in range(0, len(body) - 15, 16))
                print(f"{rel} {name:16} seq={sq} rc={rc} {arrow} {extra}", flush=True)
    print("summary:", counts, file=sys.stderr)


if __name__ == "__main__":
    main()
