"""Minimal dependency-free OpenRGB SDK client.

Negotiates protocol version 3 when available (for mode brightness support,
falling back to version 0 on older servers) to read LED counts per controller,
switch modes, and push colors.
"""
import socket
import struct

HDR = struct.Struct("<4sIII")  # magic, device idx, packet id, data size

REQUEST_CONTROLLER_COUNT = 0
REQUEST_CONTROLLER_DATA = 1
REQUEST_PROTOCOL_VERSION = 40
SET_CLIENT_NAME = 50
UPDATELEDS = 1050
SETCUSTOMMODE = 1100
UPDATEMODE = 1101

MODE_FLAG_HAS_BRIGHTNESS = 0x10


class Controller:
    def __init__(self, idx, name, num_leds, modes, active_mode, zones):
        self.idx, self.name, self.num_leds = idx, name, num_leds
        self.zones = zones  # list of (zone name, led count), in LED order
        self.modes = modes  # list of (name, raw mode description bytes)
        self.active_mode = active_mode

    @property
    def has_direct(self):
        return any(n == "Direct" for n, _ in self.modes)

    def __repr__(self):
        return f"<{self.idx}: {self.name} ({self.num_leds} LEDs)>"


class _Reader:
    def __init__(self, b):
        self.b, self.i = b, 0

    def take(self, fmt):
        v = struct.unpack_from(fmt, self.b, self.i)
        self.i += struct.calcsize(fmt)
        return v[0] if len(v) == 1 else v

    def string(self):
        n = self.take("<H")
        s = self.b[self.i : self.i + n].rstrip(b"\0").decode(errors="replace")
        self.i += n
        return s

    def skip(self, n):
        self.i += n


def _parse_controller(idx, data, proto):
    r = _Reader(data)
    r.take("<I")  # data size
    r.take("<i")  # type
    name = r.string()
    if proto >= 1:
        r.string()  # vendor
    r.string()  # description
    r.string()  # version
    r.string()  # serial
    r.string()  # location
    num_modes = r.take("<H")
    active_mode = r.take("<i")
    mode_u32s = 12 if proto >= 3 else 9
    modes = []
    for _ in range(num_modes):
        start = r.i
        mname = r.string()
        r.skip(4 * mode_u32s)
        r.skip(4 * r.take("<H"))  # mode colors
        modes.append((mname, bytes(data[start : r.i])))
    num_zones = r.take("<H")
    zones = []
    for _ in range(num_zones):
        zname = r.string()
        r.skip(4 * 3)  # type, leds_min, leds_max
        zones.append((zname, r.take("<I")))
        r.skip(r.take("<H"))  # matrix blob
    num_leds = r.take("<H")
    return Controller(idx, name, num_leds, modes, active_mode, zones)


def _with_max_brightness(raw_mode):
    """Set mode brightness to brightness_max in a protocol v3 mode blob."""
    nlen = struct.unpack_from("<H", raw_mode, 0)[0]
    off = 2 + nlen
    vals = list(struct.unpack_from("<12I", raw_mode, off))
    flags, br_max = vals[1], vals[5]
    if (flags & MODE_FLAG_HAS_BRIGHTNESS) and br_max > 0:
        vals[9] = br_max
        buf = bytearray(raw_mode)
        struct.pack_into("<12I", buf, off, *vals)
        return bytes(buf)
    return raw_mode


class OpenRGB:
    def __init__(self, host="127.0.0.1", port=6742, name="zwift-rgb", timeout=5):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self._send(0, SET_CLIENT_NAME, name.encode() + b"\0")
        self.proto = self._negotiate_proto(3)
        self.controllers = self._list()

    def _negotiate_proto(self, want):
        try:
            self._send(0, REQUEST_PROTOCOL_VERSION, struct.pack("<I", want))
            _, data = self._recv(REQUEST_PROTOCOL_VERSION)
            server_ver = struct.unpack("<I", data)[0]
            return min(want, server_ver) if server_ver >= 3 else 0
        except OSError:
            return 0

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _send(self, dev, pid, data=b""):
        self.sock.sendall(HDR.pack(b"ORGB", dev, pid, len(data)) + data)

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("OpenRGB server closed connection")
            buf += chunk
        return bytes(buf)

    def _recv(self, want_pid):
        while True:
            magic, dev, pid, size = HDR.unpack(self._recv_exact(HDR.size))
            if magic != b"ORGB":
                raise ConnectionError("bad magic from OpenRGB server")
            data = self._recv_exact(size)
            if pid == want_pid:
                return dev, data
            # ignore unsolicited packets (e.g. device list updated)

    def _list(self):
        self._send(0, REQUEST_CONTROLLER_COUNT)
        _, data = self._recv(REQUEST_CONTROLLER_COUNT)
        count = struct.unpack("<I", data)[0]
        req = struct.pack("<I", self.proto) if self.proto >= 1 else b""
        out = []
        for i in range(count):
            self._send(i, REQUEST_CONTROLLER_DATA, req)
            _, data = self._recv(REQUEST_CONTROLLER_DATA)
            out.append(_parse_controller(i, data, self.proto))
        return out

    def set_custom_mode(self, controllers=None):
        for c in controllers if controllers is not None else self.controllers:
            self._send(c.idx, SETCUSTOMMODE)
        self.controllers = self._list()  # pick up the resulting active modes

    def set_mode(self, mode_name, controllers=None, max_brightness=True):
        """Switch each controller to the named mode (case-insensitive), falling
        back to SETCUSTOMMODE if the controller has no matching mode. On SDK v3+,
        sets brightness to brightness_max when max_brightness is true."""
        want = mode_name.lower()
        for c in controllers if controllers is not None else self.controllers:
            match = next(
                ((i, raw) for i, (n, raw) in enumerate(c.modes) if n.lower() == want),
                None,
            )
            if match is None:
                self._send(c.idx, SETCUSTOMMODE)
                continue
            midx, raw = match
            if self.proto >= 3 and max_brightness:
                raw = _with_max_brightness(raw)
            body = struct.pack("<i", midx) + raw
            self._send(c.idx, UPDATEMODE, struct.pack("<I", len(body) + 4) + body)
        self.controllers = self._list()

    def reapply_mode(self, c):
        """Re-send the active mode. Devices without a Direct mode (e.g. the
        Akko keyboard in Static) only push LED colors to hardware on a mode
        update, so call this after set_all() for them."""
        raw = c.modes[c.active_mode][1]
        body = struct.pack("<i", c.active_mode) + raw
        self._send(c.idx, UPDATEMODE, struct.pack("<I", len(body) + 4) + body)

    def set_all(self, rgb, controllers=None, tint=None):
        """Set every LED to rgb. tint maps a zone name to per-channel factors
        (r, g, b), to correct LED strips whose white balance differs."""
        tint = tint or {}
        for c in controllers if controllers is not None else self.controllers:
            if not c.num_leds:
                continue
            leds = bytearray()
            for zname, count in c.zones:
                f = tint.get(zname, (1, 1, 1))
                px = bytes(min(255, round(v * k)) for v, k in zip(rgb, f)) + b"\0"
                leds += px * count
            if len(leds) != 4 * c.num_leds:  # zone info inconsistent; ignore tint
                leds = bytes((*rgb, 0)) * c.num_leds
            body = struct.pack("<H", c.num_leds) + bytes(leds)
            self._send(c.idx, UPDATELEDS, struct.pack("<I", len(body) + 4) + body)


if __name__ == "__main__":
    o = OpenRGB(name="zwift-rgb-probe")
    for c in o.controllers:
        mode = "Direct" if c.has_direct else "no Direct mode (skipped by default)"
        print(f"{c.idx}: {c.name} - {c.num_leds} LEDs, {mode}")
        for zname, count in c.zones:
            print(f"     zone '{zname}' ({count} LEDs)")
    o.close()
