# zwift-rgb

Make your PC's RGB lighting react to your Zwift ride on Linux: **color follows your
power zone**, and **brightness pulses with your heart rate**.

zwift-rgb doesn't connect to your trainer, heart-rate strap, or Zwift account. It
passively reads sensor traffic that already passes through your PC and drives your
lights through [OpenRGB](https://openrgb.org).

## Features

- **Power-zone colors.** It uses Zwift's zone colors (grey, blue, green, yellow,
  orange, red), based on your FTP.
- **Smooth gradient.** Colors blend across zones and ease toward the new color,
  so a sprint sweeps through the palette instead of jumping straight to red.
- **Heart-rate pulse (optional).** Brightness swells and fades once every *N*
  heartbeats. It never drops to black.
- **Per-zone color correction.** This matches LED strips whose white balance
  differs, such as green-heavy ARGB fans.
- **Automatic start and stop.** It takes over the lights when power data appears,
  and restores your normal OpenRGB profile about 10 s after the ride ends.
- **No dependencies.** It needs only Python 3 and `tcpdump`. The OpenRGB SDK client
  is built in.

## How it works

```
 KICKR ──Direct Connect (TCP, LAN)──▶ Zwift          H10 ──BLE──▶ zwift-ble-daemon ──TCP (loopback)──▶ Zwift
           │                                                            │
     tcpdump (read-only)                                         tcpdump (read-only)
           │  Cycling Power 0x2A63                                      │  Heart Rate 0x2A37
           └─────────────────────────▶  zwift-rgb  ◀────────────────────┘
                                            │
                                   OpenRGB SDK (localhost:6742)
                                            │
                                     RAM · GPU · motherboard · fans
```

- **Power:** Wahoo's Direct Connect (DirCon) protocol sends Bluetooth trainer data
  over TCP without encryption. zwift-rgb decodes the trainer's once-per-second
  Cycling Power notifications.
- **Heart rate:** on Linux, Zwift reaches Bluetooth sensors through
  [zwift-linux-ble](https://github.com/davidpfa/zwift-linux-ble), a daemon that
  passes them to Zwift over a local TCP connection. zwift-rgb reads the Heart Rate
  Measurement data from that connection.
- **Lights:** zwift-rgb sends colors to OpenRGB's SDK server about 30 times a
  second, and only when the color actually changes.

Packet capture is read-only. zwift-rgb never sends anything to the trainer,
the strap, or Zwift.

## Requirements

- Linux with systemd
- Python 3.8+
- `tcpdump`
- [OpenRGB](https://openrgb.org) running as an SDK server (`openrgb --server`), with
  your devices detected and a saved profile to restore after rides
- **For power:** a trainer connected to Zwift over **Wahoo Direct Connect** (KICKR
  with built-in DirCon, or the KICKR Direct Connect module)
- **For heart rate (optional):** a BLE heart-rate strap connected through
  [zwift-linux-ble](https://github.com/davidpfa/zwift-linux-ble)

Trainers connected over Bluetooth or ANT+ are not supported as a power source.

## Installation

1. **Install the dependencies.** This example is for Arch; use your distro's
   package manager.
   ```sh
   sudo pacman -S --needed tcpdump python
   ```

2. **Clone the repository.**
   ```sh
   git clone https://github.com/velomatt/zwift-rgb.git ~/git/zwift-rgb
   ```

3. **Check that OpenRGB can see your devices.**
   ```sh
   python3 ~/git/zwift-rgb/orgb.py
   ```

4. **Create your own unit file from the template.** `*.local.service` is
   gitignored, so your settings stay out of the repository.
   ```sh
   cd ~/git/zwift-rgb
   cp zwift-rgb.service zwift-rgb.local.service
   ```
   Edit `zwift-rgb.local.service`: set `User=` to your username, point
   `ExecStart=` at your clone, and fill in the required `ZRGB_FTP`,
   `ZRGB_TRAINER` and `ZRGB_PROFILE` (see [Configuration](#configuration)).

5. **Install and start the service.**
   ```sh
   sudo cp ~/git/zwift-rgb/zwift-rgb.local.service /etc/systemd/system/zwift-rgb.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now zwift-rgb
   journalctl -u zwift-rgb -f
   ```

The service runs as your user with only `CAP_NET_RAW`/`CAP_NET_ADMIN`, the
permissions packet capture needs. It does not run as root.

When you start riding, the log shows something like:

```
watching 192.168.1.50 on eth0, FTP 250 W, tint -, pulse 1:5
ride detected: 161 W (Z2)
HR pulse on (119 bpm)
OpenRGB connected: 6 fast, 0 slow (-)
```

## Configuration

All settings are environment variables, set as `Environment=` lines in the
`[Service]` section of `zwift-rgb.local.service`. After changing them, copy the
file over again (step 5), then run
`sudo systemctl daemon-reload && sudo systemctl restart zwift-rgb`.

| Variable | Default | Description |
|---|---|---|
| `ZRGB_FTP` | *required* | Your FTP in watts; sets the zone boundaries |
| `ZRGB_TRAINER` | *required* | Trainer's IP address (see the Zwift pairing screen or your router) |
| `ZRGB_IFACE` | auto | Network interface that reaches the trainer (detected from the route) |
| `ZRGB_PROFILE` | *required* | OpenRGB profile to restore when the ride ends |
| `ZRGB_IDLE` | `10` | Seconds without power data before restoring the profile |
| `ZRGB_AVG` | `3` | Power averaging window, seconds |
| `ZRGB_EASE` | `1.0` | Color easing time constant, seconds |
| `ZRGB_FPS` | `30` | Maximum light update rate |
| `ZRGB_TINT` | *(none)* | Per-zone color correction (see below) |
| `ZRGB_PULSE_RATIO` | `5` | Heartbeats per light pulse; `0` turns the pulse off |
| `ZRGB_PULSE_FLOOR` | `0.35` | Brightness at the bottom of a pulse (0–1) |
| `ZRGB_PULSE_ATTACK` | `0.15` | Fraction of each pulse spent rising (about 0.5 gives an even "breathing" fade) |
| `ZRGB_HR_PORT` | `35199` | TCP port used by zwift-linux-ble |
| `ZRGB_SLOW_INTERVAL` | `0` | Minimum seconds between writes to devices without a Direct mode; `0` skips them |
| `ZRGB_MODE` | *(Direct)* | OpenRGB mode to use instead of `Direct`, at full brightness, as `controller=mode` pairs separated by `;` (e.g. `HP Omen 30L=Static`). A bare mode name applies to every device with a `Direct` mode |

### Power zones

| Zone | % of FTP | Color |
|---|---|---|
| Z1 | < 60% | Grey |
| Z2 | 60–75% | Blue |
| Z3 | 76–89% | Green |
| Z4 | 90–104% | Yellow |
| Z5 | 105–118% | Orange |
| Z6 | ≥ 119% | Red |

Each color is pure in the middle of its zone and blends into its neighbors toward
the zone edges.

### Color correction (`ZRGB_TINT`)

Some LED strips don't match the rest of your lights. A common case is ARGB fans
that look turquoise when everything else is blue. `ZRGB_TINT` scales the red,
green and blue channels for specific OpenRGB **zones**:

```ini
Environment="ZRGB_TINT=Addressable RGB Header 1=1,0.45,1"
# several zones:
Environment="ZRGB_TINT=Addressable RGB Header 1=1,0.45,1;GPU Zone=0.9,1,1"
```

To find your zone names, run `python3 orgb.py`, or check OpenRGB's GUI.

## Tools

You can also run both modules directly:

```sh
# List OpenRGB controllers
python3 orgb.py

# Decode a capture of Direct Connect traffic
sudo timeout 30 tcpdump -i <iface> -U -w dircon.pcap host <trainer-ip>
python3 dircon.py dircon.pcap

# Replay a capture through the full pipeline
ZRGB_PCAP=dircon.pcap python3 zwift_rgb.py
```

## Troubleshooting

- **Nothing happens when I ride.** Make sure the trainer is paired as
  `[BLE (LAN)]` (Direct Connect) in Zwift, and that `ZRGB_TRAINER` is
  correct (and `ZRGB_IFACE`, if you set it). Run the `dircon.py` capture above to confirm power
  data is coming through.
- **No heart-rate pulse.** The pulse needs HR data from zwift-linux-ble on
  `ZRGB_HR_PORT`. A strap paired any other way won't be seen. The log shows
  `HR pulse on/off` as data appears and stops.
- **Lights stay off even though `ride detected` and zones appear in the log.**
  Some OEM RGB controllers (for example, HP Omen 30L) advertise a `Direct` mode
  that does not actually drive the LEDs, while `Static` mode works normally with
  live `UPDATELEDS` updates. Set `ZRGB_MODE=Static` so zwift-rgb initializes the
  controller in `Static` mode at full brightness instead of `Direct`. Name the
  controller (as `python3 orgb.py` prints it), e.g.
  `ZRGB_MODE="HP Omen 30L=Static"`, so your other devices stay in `Direct`;
  many controllers ignore live color updates in `Static` mode.
- **A device gets stuck on one color.** Devices without a **Direct** mode get a
  full mode update for every color change. Some firmware can't keep up and locks
  up; replugging the device and restarting OpenRGB usually fixes it. These
  devices are skipped by default (`ZRGB_SLOW_INTERVAL=0`). If you opt in, keep
  the interval at 10 s or more.
- **Colors stutter.** Lower `ZRGB_FPS` (try `20`). SMBus devices such as RAM
  can be slow to update.
- **Duplicate devices in OpenRGB.** Don't run the OpenRGB CLI with `--client`
  while the server is running.

## Limitations

- Power is only available from trainers connected over Wahoo Direct Connect.
- It relies on DirCon staying unencrypted, and on zwift-linux-ble's frame format.
- It reacts to sensor data, not to anything happening in the game.

## Disclaimer

This is an unofficial hobby project. It is not affiliated with or endorsed by
Zwift, Wahoo Fitness, Polar, or the OpenRGB project. All trademarks belong to
their respective owners.

## License

[MIT](LICENSE)
