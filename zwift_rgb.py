#!/usr/bin/env python3
"""Color the PC's RGB by Zwift power zone, using KICKR Direct Connect traffic.

Passively sniffs the DirCon TCP stream between Zwift and the trainer (tcpdump,
needs CAP_NET_RAW), decodes Cycling Power notifications, maps smoothed power
onto a continuous gradient through the Zwift zone colors (easing the displayed
watts, so transitions always travel along the zone palette). When power data stops (ride over / Zwift closed) it
reloads the everyday OpenRGB profile.

Env config:
  ZRGB_FTP                  FTP in watts (required)
  ZRGB_TRAINER              trainer IP address (required)
  ZRGB_PROFILE              OpenRGB profile restored when idle (required)
  ZRGB_IFACE                capture interface (default: route to the trainer)
  ZRGB_IDLE=10              seconds without power data before restoring
  ZRGB_AVG=3                power rolling-average window, seconds
  ZRGB_EASE=1.0             color easing time constant, seconds
  ZRGB_FPS=30               color update rate
  ZRGB_PULSE_RATIO=5        heartbeats per light pulse (0 = no pulse)
  ZRGB_PULSE_FLOOR=0.35     brightness at the bottom of a pulse (0..1)
  ZRGB_PULSE_ATTACK=0.15    fraction of each pulse spent rising to full
  ZRGB_HR_PORT=35199        zwift-ble-daemon TCP port carrying H10 data (lo)
  ZRGB_TINT="Addressable RGB Header 1=1,0.45,1"
                            (example) per-zone channel factors (r,g,b) to color-match
                            LED strips; several zones separated by ';'
  ZRGB_SLOW_INTERVAL=0      min seconds between writes to devices without a
                            Direct mode (Akko keyboard); 0 = leave them alone
  ZRGB_PCAP=-               read pcap from a file instead of tcpdump (testing)
"""
import collections
import math
import os
import queue
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dircon  # noqa: E402
from orgb import OpenRGB  # noqa: E402

FTP = float(os.environ.get("ZRGB_FTP") or 0)
TRAINER = os.environ.get("ZRGB_TRAINER", "")
IFACE = os.environ.get("ZRGB_IFACE", "")
PROFILE = os.environ.get("ZRGB_PROFILE", "")
IDLE = float(os.environ.get("ZRGB_IDLE", "10"))
AVG = float(os.environ.get("ZRGB_AVG", "3"))
EASE = float(os.environ.get("ZRGB_EASE", "1.0"))
FPS = float(os.environ.get("ZRGB_FPS", "30"))
PULSE_RATIO = float(os.environ.get("ZRGB_PULSE_RATIO", "5"))
PULSE_FLOOR = float(os.environ.get("ZRGB_PULSE_FLOOR", "0.35"))
PULSE_ATTACK = float(os.environ.get("ZRGB_PULSE_ATTACK", "0.15"))
HR_PORT = int(os.environ.get("ZRGB_HR_PORT", "35199"))
HR_STALE = 5.0  # seconds without HR before pulsing stops
TINT = {}
for _item in os.environ.get("ZRGB_TINT", "").split(";"):
    if "=" in _item:
        _zone, _f = _item.rsplit("=", 1)
        TINT[_zone.strip()] = tuple(float(x) for x in _f.split(","))
SLOW_INTERVAL = float(os.environ.get("ZRGB_SLOW_INTERVAL", "0"))
PCAP = os.environ.get("ZRGB_PCAP")

# Zwift power zones: upper bound as fraction of FTP (used for logging).
ZONE_BOUNDS = [0.60, 0.76, 0.90, 1.05, 1.19, math.inf]

# Gradient stops: (fraction of FTP, color). Each zone's color is pure at the
# middle of the zone and blends into its neighbours toward the boundaries.
STOPS = [
    (0.45, (90, 90, 90)),    # Z1 grey (everything below is flat grey)
    (0.68, (0, 90, 255)),    # Z2 blue
    (0.83, (0, 220, 40)),    # Z3 green
    (0.975, (255, 200, 0)),  # Z4 yellow
    (1.12, (255, 80, 0)),    # Z5 orange
    (1.30, (255, 0, 0)),     # Z6 red (everything above is flat red)
]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def zone_of(watts):
    frac = watts / FTP
    return next(i for i, hi in enumerate(ZONE_BOUNDS, 1) if frac < hi)


# Blend in linear light so midpoints (e.g. blue->green) don't go muddy/dark.
def _lin(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _srgb(v):
    v = min(max(v, 0.0), 1.0)
    return 255 * (v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055)


def gradient(watts):
    """Linear-light color (floats 0..1) for a power value."""
    frac = watts / FTP
    if frac <= STOPS[0][0]:
        return tuple(map(_lin, STOPS[0][1]))
    for (f0, c0), (f1, c1) in zip(STOPS, STOPS[1:]):
        if frac <= f1:
            t = (frac - f0) / (f1 - f0)
            return tuple(_lin(a) * (1 - t) + _lin(b) * t for a, b in zip(c0, c1))
    return tuple(map(_lin, STOPS[-1][1]))


def to_rgb(lin):
    return tuple(round(_srgb(v)) for v in lin)


def pulse_envelope(phase):
    """Heartbeat-like brightness 0..1 over one pulse: quick smooth swell,
    then a long smooth fade back down (never a hard cut)."""
    a = min(max(PULSE_ATTACK, 0.01), 0.9)
    if phase < a:
        return math.sin(math.pi / 2 * phase / a) ** 2
    return 0.5 * (1 + math.cos(math.pi * (phase - a) / (1 - a)))


class DaemonStream(dircon.Stream):
    """zwift-ble-daemon <-> BleWin10Lib.dll framing: type u8, len u32 LE."""
    TYPES = {0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x81, 0x82, 0x83}

    def messages(self):
        while len(self.buf) >= 5:
            mtype, ln = struct.unpack_from("<BI", self.buf, 0)
            if mtype not in self.TYPES or ln > 1 << 20:
                del self.buf[0]  # lost framing; resync
                continue
            if len(self.buf) < 5 + ln:
                return
            body = bytes(self.buf[5 : 5 + ln])
            del self.buf[: 5 + ln]
            yield mtype, body


def parse_hr_update(body):
    """EVT_DEVICE_UPDATE (0x83) body -> bpm if it is a 2A37 notification."""
    r = dircon_reader(body)
    status = r("<i")
    if status != 2:
        return None
    r("<Q")  # address
    r("<h")  # rssi
    r.str()  # name
    for _ in range(r("<H")):
        r.str()  # service uuid
        for _ in range(r("<H")):
            ch = r.str()
            data = r.blob()
            if "2A37" in ch.upper() and len(data) >= 2:
                return struct.unpack_from("<H", data, 1)[0] if data[0] & 1 else data[1]
    return None


class dircon_reader:
    def __init__(self, b):
        self.b, self.i = b, 0

    def __call__(self, fmt):
        v = struct.unpack_from(fmt, self.b, self.i)[0]
        self.i += struct.calcsize(fmt)
        return v

    def str(self):
        n = self("<H")
        v = self.b[self.i : self.i + n].decode(errors="replace")
        self.i += n
        return v

    def blob(self):
        n = self("<I")
        v = self.b[self.i : self.i + n]
        self.i += n
        return v


def hr_reader(src, q):
    """Thread: parse loopback pcap of the BLE daemon link, put ("hr", bpm)."""
    streams = {}
    for _ts, lt, pkt in dircon.read_pcap(src):
        r = dircon.ip_tcp(lt, pkt)
        if not r:
            continue
        src_ip, sport, dst_ip, dport, seq, payload = r
        if sport != HR_PORT:  # only daemon -> DLL direction
            continue
        st = streams.setdefault((src_ip, sport, dst_ip, dport), DaemonStream())
        st.feed(seq, payload)
        for mtype, body in st.messages():
            if mtype != 0x83:
                continue
            try:
                bpm = parse_hr_update(body)
            except struct.error:
                continue
            if bpm is not None:
                q.put(("hr", bpm))
    q.put(None)


def power_reader(src, q):
    """Thread: parse pcap from file object, put ("power", watts) on q."""
    streams = {}
    for _ts, lt, pkt in dircon.read_pcap(src):
        r = dircon.ip_tcp(lt, pkt)
        if not r:
            continue
        src_ip, sport, dst_ip, dport, seq, payload = r
        st = streams.setdefault((src_ip, sport, dst_ip, dport), dircon.Stream())
        st.feed(seq, payload)
        for mid, _sq, _rc, body in st.messages():
            if mid != 0x06 or len(body) < 20:
                continue
            _, short = dircon.uuid_str(body[:16])
            if short == 0x2A63:
                q.put(("power", struct.unpack_from("<h", body, 18)[0]))
    q.put(None)  # EOF


class Lights:
    """Fast devices (with a Direct mode) get the smooth gradient every frame.
    Slow devices (no Direct mode, e.g. the Akko keyboard, whose firmware locks
    up if written to more than about once a second and may save each change to
    flash) get only the flat zone color, at most once per SLOW_INTERVAL."""

    def __init__(self):
        self.o = None
        self.fast = self.slow = []
        self.active = False
        self.sent = None
        self.slow_sent = None
        self.slow_last = -math.inf  # monotonic time of last write touching slow devices

    def _conn(self):
        if self.o is None:
            self.o = OpenRGB(name="zwift-rgb")
            self.fast = [c for c in self.o.controllers if c.has_direct]
            self.slow = [c for c in self.o.controllers if not c.has_direct] if SLOW_INTERVAL > 0 else []
            log(f"OpenRGB connected: {len(self.fast)} fast,",
                f"{len(self.slow)} slow ({', '.join(c.name for c in self.slow) or '-'})")
        return self.o

    def color(self, rgb, zone_rgb):
        for attempt in (1, 2):
            try:
                o = self._conn()
                if not self.active:
                    o.set_custom_mode(self.fast)  # slow devices stay in their Static mode
                    self.active = True
                if rgb != self.sent:
                    o.set_all(rgb, self.fast, TINT)
                    self.sent = rgb
                now = time.monotonic()
                if self.slow and zone_rgb != self.slow_sent and now - self.slow_last >= SLOW_INTERVAL:
                    o.set_all(zone_rgb, self.slow)
                    self.slow_sent, self.slow_last = zone_rgb, now
                return
            except OSError as e:  # server restarted (e.g. after resume)
                log("OpenRGB error:", e, "- reconnecting" if attempt == 1 else "- giving up")
                self.drop()

    def drop(self):
        if self.o:
            self.o.close()
        self.o = None
        self.active = False
        self.sent = self.slow_sent = None

    def restore(self):
        had_slow = bool(self.slow)
        self.drop()
        # Loading the profile writes the slow devices too; keep the gap.
        wait = self.slow_last + SLOW_INTERVAL - time.monotonic()
        if had_slow and wait > 0:
            time.sleep(wait)
        r = subprocess.run(["openrgb", "-p", PROFILE], capture_output=True, text=True)
        self.slow_last = time.monotonic()
        log(f"restored profile {PROFILE} (rc={r.returncode})")


def tcpdump(iface, bpf):
    return subprocess.Popen(
        ["tcpdump", "-i", iface, "-U", "-n", "-w", "-", bpf],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )


def route_iface(ip):
    """Interface the kernel would use to reach ip (via `ip route get`)."""
    out = subprocess.run(["ip", "route", "get", ip], capture_output=True, text=True).stdout.split()
    return out[out.index("dev") + 1] if "dev" in out else ""


def main():
    global IFACE
    missing = [n for n, v in (("ZRGB_FTP", FTP), ("ZRGB_PROFILE", PROFILE)) if not v]
    if not PCAP and not TRAINER:
        missing.append("ZRGB_TRAINER")
    if missing:
        sys.exit(f"zwift-rgb: set {', '.join(missing)} (see README)")
    if not PCAP and not IFACE:
        IFACE = route_iface(TRAINER)
        if not IFACE:
            sys.exit(f"zwift-rgb: no route to {TRAINER}; set ZRGB_IFACE")
    q = queue.Queue()
    procs = []
    if PCAP:
        threading.Thread(target=power_reader, args=(open(PCAP, "rb"), q), daemon=True).start()
    else:
        p = tcpdump(IFACE, f"host {TRAINER} and tcp")
        procs.append(p)
        threading.Thread(target=power_reader, args=(p.stdout, q), daemon=True).start()
        if PULSE_RATIO > 0:
            p = tcpdump("lo", f"tcp port {HR_PORT}")
            procs.append(p)
            threading.Thread(target=hr_reader, args=(p.stdout, q), daemon=True).start()
    log(f"watching {TRAINER} on {IFACE}, FTP {FTP:.0f} W, tint {TINT or '-'},",
        f"pulse 1:{PULSE_RATIO:g}" if PULSE_RATIO > 0 else "no pulse")

    lights = Lights()
    samples = collections.deque()  # (monotonic time, watts)
    target = cur = None  # watts: smoothed target and eased display value
    last_data = last_frame = 0.0
    logged_zone = None
    hr, hr_time, phase, pulsing = 0, -math.inf, 0.0, False
    frame = 1 / FPS
    while True:
        timeout = frame if cur is not None else 1.0
        try:
            item = q.get(timeout=timeout)
        except queue.Empty:
            item = "tick"
        if item is None:
            log("capture ended")
            break
        now = time.monotonic()
        w = "tick"
        if item != "tick":
            kind, val = item
            if kind == "hr":
                hr, hr_time = val, now
                item = "tick"  # HR alone doesn't drive a frame
            else:
                w = val
        if w != "tick":
            last_data = now
            samples.append((now, w))
            while samples[0][0] < now - AVG:
                samples.popleft()
            avg = sum(s[1] for s in samples) / len(samples)
            target = avg
            z = zone_of(avg)
            if cur is None:
                cur = target  # first sample: jump straight there
                last_frame = now
                log(f"ride detected: {avg:.0f} W (Z{z})")
            elif z != logged_zone:
                log(f"{avg:.0f} W: Z{z}")
            logged_zone = z

        if cur is None:
            continue
        if now - last_data > IDLE:
            log("power data stopped")
            lights.restore()
            target = cur = logged_zone = None
            samples.clear()
            continue
        dt = now - last_frame
        if dt >= frame or item == "tick":
            k = 1 - math.exp(-dt / EASE) if EASE > 0 else 1.0
            cur += (target - cur) * k
            last_frame = now
            rgb = to_rgb(gradient(cur))
            hr_ok = PULSE_RATIO > 0 and hr > 0 and now - hr_time < HR_STALE
            if hr_ok != pulsing:
                pulsing = hr_ok
                log(f"HR pulse {'on' if hr_ok else 'off'}" + (f" ({hr} bpm)" if hr_ok else ""))
            if pulsing:
                phase = (phase + dt * hr / 60 / PULSE_RATIO) % 1.0
                b = PULSE_FLOOR + (1 - PULSE_FLOOR) * pulse_envelope(phase)
                rgb = tuple(round(v * b) for v in rgb)
            lights.color(rgb, STOPS[logged_zone - 1][1])

    if cur is not None:
        lights.restore()
    for p in procs:
        p.terminate()
    return 1 if procs else 0  # live capture ending is abnormal -> systemd restarts


if __name__ == "__main__":
    sys.exit(main())
