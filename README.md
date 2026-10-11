# Dog tracker · Mictrack MT710

A small self-hosted server to locate your dog from a phone browser and configure
the tracker over its cellular TCP connection. Python 3.11+; **no pip install or
frontend build required**. The map uses Leaflet and OpenStreetMap.

## Features

- Mobile-friendly live map, Google Maps link, and a 24-hour GPS trail (up to 2,000 points).
- GPS timestamp, last report age, connection status, speed, battery **voltage**,
  and device events. Voltage is not an estimated battery percentage.
- Real-time, auto, motion, smart, periodic GPS, and deep-sleep modes.
- Durable command queue, cancellation, device OK/FS replies, and configuration queries.
- SQLite history retained for 30 days, plus the last location; data survives restarts.
- One registered IMEI, access-token-protected APIs, and a separate simulated demo.

## Try the demo

```sh
python3 server.py --demo
```

Open `http://localhost:8080` and paste the access token printed at startup.
From a phone on the same Wi-Fi, use `http://YOUR_COMPUTER_LAN_IP:8080`.
The page prominently labels the demo; it simulates a dog walking in Prague and
acknowledges commands. It does not change a real tracker. Demo data is stored in
`demo-data/`, separately from real data. Stop with Ctrl+C.

## Run with your MT710

Run this from the project directory, substituting the IMEI printed on your device:

```sh
TRACKER_IMEI=862255061947757 DOG_NAME="My dog" python3 server.py
```

The server prints an access token on first and subsequent starts when
`DOGTRACK_TOKEN` is unset. It is saved privately in `data/access-token`.
Alternatively, set your own long random `DOGTRACK_TOKEN` (at least 12 ASCII
characters). This is the dashboard password, unrelated to the tracker's SMS password.

For repeatable configuration, copy `.env.example` to `.env`, edit it, and run:

```sh
set -a
. ./.env
set +a
python3 server.py
```

`.env` must be trusted shell configuration. Python does not load it automatically.
Defaults: web `0.0.0.0:8080`, tracker **TCP** `0.0.0.0:7700`.
Options: `--imei`, `--host` (web only), `--web-port`, `--tracker-port`, `--data-dir`.
A data directory belongs to one IMEI; choose a new directory to use another tracker.

## Configure the hardware

The MT710 needs a SIM with mobile data, the correct APN, and LTE-M/NB-IoT/GSM
coverage supported by your operator. The tracker connects to this server;
it does not host the browser page itself.

1. Deploy the server on an always-on machine with an address reachable from the
   mobile network (a VPS is straightforward). At home, forward **TCP 7700** to
   your server and allow it through the firewall. A private LAN address alone
   is not reachable by the tracker. Ordinary port forwarding will not work
   behind carrier-grade NAT without another publicly reachable endpoint.
2. Use [Mictrack's USB configuration tool](https://config.mictrack.com) or SMS
   to set APN, server hostname/IP, port **7700**, and protocol **TCP**.
   Initial provisioning must happen on the device before browser controls work.
3. Start with real-time mode at 30 seconds, power on, and take it outside for a GPS fix.
4. Open the dashboard, verify reports, then choose your preferred sleep/movement mode.

For USB configuration, use the tool's corresponding fields. The protocol's
server/downlink command equivalents are:

```text
803,YOUR_APN,,
804,tracker.example.com,7700
800,TCP
MODE,1,30
```

These are **TCP commands**, not SMS syntax. If provisioning by SMS and the
device password is still `0000`, the manufacturer's manual specifies:

```text
#803#0000#YOUR_APN##
#804#0000#tracker.example.com#7700##
MODE,1,0000,30
```

With APN credentials, the SMS format is
`#803#0000#YOUR_APN#APN_USERNAME#APN_PASSWORD##`.
Select TCP through the configuration tool; SMS command syntax differs by
command, so consult the manual for your firmware rather than sending the
TCP examples as SMS.

## Reporting and sleep controls

| Mode | Reporting interval | Behavior |
|---|---|---|
| Auto (0) | Moving: 10–600 seconds; still: 1–24 hours | Frequent movement tracking and sleeping while still |
| Real-time (1) | 10–600 seconds | GPS and TCP always on |
| GPS auto (2) | 10–60 minutes | GPS duty-cycled; this app keeps TCP on for commands |
| Deep sleep (3) | 1–24 hours | One report each wake cycle, then sleeps |
| Motion (4) | 10–600 seconds while moving | Sleeps after 10 minutes without vibration |
| Smart (7) | Moving: 10–1440 minutes; still: 1–24 hours | Lower-power movement tracking |

**Auto mode, 30 seconds moving / 1 hour still**, is a useful starting point for
walks. Tune it to your battery needs after measuring real usage.

There is no remote “wake now” over a disconnected TCP socket. Browser changes
remain **queued** until the device reconnects. A connected device receives one
command at a time; successful replies show **confirmed**, `FS` shows **failed**.
If it disconnects or gives no reply within 30 seconds, the result becomes
**unknown**, and the server does not automatically resend it. Read the device's
configuration pages before reissuing an uncertain change. The UI shows the last
acknowledged mode request, not an inferred current mode or sleep state.
Queued requests persist across restart and can be cancelled; sent commands cannot.

An `L` fix is the device's old location. An invalid GPS or Wi-Fi-only report
does not erase the last map position. The timestamp always shows when the
device says the position was recorded. Wi-Fi MAC addresses and cell IDs are
stored, but this app does not convert them to coordinates through a paid
geolocation service. No GPS indoors means the map can remain at the last outdoor fix.

## Access from your phone away from home

Serve the dashboard over **HTTPS** using a reverse proxy, and connect it to the
Python web port. For example, Caddy on the same server can use:

```caddyfile
dog.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

Point DNS at your server; allow ports 80/443 for Caddy and TCP 7700 for the
tracker. Run Python with `WEB_HOST=127.0.0.1` when using this proxy. Route TCP
7700 directly to Python: it is not HTTP and must not go through an HTTP proxy.
The tracker server hostname can point at the same IP as the dashboard domain.
An HTTP-only CDN proxy does not forward the tracker protocol.

The access token is entered once per browser tab and stored in session storage,
never in the URL. Use HTTPS for remote access so it and your dog's locations
are encrypted between browser and server. The MT710's own TCP protocol is
unencrypted and identifies devices by IMEI, as specified by the manufacturer.
The map library and map tiles need Internet access; an external Maps link
remains available if the embedded map cannot load.

## Keep the server running

Use your existing service/process manager, or adapt this systemd unit:

```ini
[Unit]
Description=Mictrack dog tracker
After=network.target

[Service]
User=jiri
WorkingDirectory=/home/jiri/Projects/dog-tracker
EnvironmentFile=/home/jiri/Projects/dog-tracker/.env
ExecStart=/usr/bin/python3 /home/jiri/Projects/dog-tracker/server.py
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
```

Adjust the user and paths for your server. Back up the `data/` directory;
for a live SQLite backup, use SQLite's backup API or stop the server before copying it.

## Verify

```sh
python3 -m unittest discover -s tests -v
node --check static/app.js
```

Tests exercise manufacturer report examples, checksums, fragmented TCP frames,
IMEI filtering, browser authentication, command validation and cancellation,
queued delivery, acknowledgement matching, disconnects, timeouts, restart
recovery, and keeping the last position after invalid fixes.

## Protocol references

Implemented against Mictrack's published V1.0 protocol:

- [MT710 / MT710L communication protocol](https://www.mictrack.com/downloads/protocols/Mictrack_Communication_Protocol_For_MT710_V1.0.pdf)
- [MT710 user manual](https://www.mictrack.com/downloads/user-manual/4g-gps-tracker/MT710_User_Manual_V1.0.pdf)

Reports end in `##`; the receiver validates NMEA XOR checksums and sends no
report ACK, as specified. TCP downlinks are plain command text followed by CRLF.
Hardware interoperability still needs checking against your device/firmware;
the demo and tests use simulated connections, not a physical MT710.
