"""Minimal dependency-free OpenRGB SDK client.

Speaks protocol version 0 for controller data (the simplest wire format; the
server down-converts for old clients), which is all we need: LED counts per
controller, switch to direct/custom mode, and push colors.
"""
import socket
import struct

HDR = struct.Struct("<4sIII")  # magic, device idx, packet id, data size

REQUEST_CONTROLLER_COUNT = 0
REQUEST_CONTROLLER_DATA = 1
SET_CLIENT_NAME = 50
UPDATELEDS = 1050
SETCUSTOMMODE = 1100
UPDATEMODE = 1101


class Controller:
    def __init__(self, idx, name, num_leds, modes, active_mode, zones):
        self.idx, self.name, self.num_leds = idx, name, num_leds
        self.zones = zones  # list of (zone name, led count), in LED order
        self.modes = modes  # list of (name, raw v0 mode description bytes)
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


def _parse_controller_v0(idx, data):
    r = _Reader(data)
    r.take("<I")  # data size
    r.take("<i")  # type
    name = r.string()
    r.string()  # description
    r.string()  # version
    r.string()  # serial
    r.string()  # location
    num_modes = r.take("<H")
    active_mode = r.take("<i")
    modes = []
    for _ in range(num_modes):
        start = r.i
        mname = r.string()
        r.skip(4 * 9)  # value, flags, speed_min/max, colors_min/max, speed, direction, color_mode
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


class OpenRGB:
    def __init__(self, host="127.0.0.1", port=6742, name="zwift-rgb", timeout=5):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self._send(0, SET_CLIENT_NAME, name.encode() + b"\0")
        self.controllers = self._list()

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
        out = []
        for i in range(count):
            self._send(i, REQUEST_CONTROLLER_DATA)
            _, data = self._recv(REQUEST_CONTROLLER_DATA)
            out.append(_parse_controller_v0(i, data))
        return out

    def set_custom_mode(self, controllers=None):
        for c in controllers if controllers is not None else self.controllers:
            self._send(c.idx, SETCUSTOMMODE)
        self.controllers = self._list()  # pick up the resulting active modes

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
