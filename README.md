# portcullis

Per-app network gate for NixOS / Linux. For each app you can, **separately for incoming and
outgoing** traffic:

- **block** it, and/or
- **add fake latency** (ms) to it.

Apps are found by **auto-detect**: a profile stores an app *identity* (e.g. `flatpak:org.vinegarhq.Sober`),
not a PID or a unit name, so every relaunch of the same app gets the same settings automatically.

## Setup (NixOS)

```nix
inputs.portcullis = {
  url = "github:mr-tinkle-winkle/portcullis";
  inputs.nixpkgs.follows = "nixpkgs-unstable";
};
# modules = [ inputs.portcullis.nixosModules.default ];
services.portcullis = {
  enable = true;
  user = "mrtw";
};
```

Log out/in once so the `portcullis` group applies to your session.

## Use

```
portcullis add sober --running sober --block-out --enable    # auto-detect from the running app
portcullis add lag   --app flatpak:org.vinegarhq.Sober --delay-in 120 --delay-out 250 --enable
portcullis set sober --block-in toggle
portcullis set lag   --delay-out 0
portcullis on|off|toggle NAME
portcullis --profile lag --blockIncoming --outgoingLatency=250   # one-shot form (also: --blockOutgoing,
#   --allowIncoming, --allowOutgoing, --toggleIncoming, --toggleOutgoing, --incomingLatency=MS; latency 0 = off)
portcullis --profile lag --addPort voice=3478/udp@out --addPort host=7777    # name ports for an app
portcullis --profile lag --disablePort voice --togglePort host --enablePort 7777  # by name or number
#   (--removePort NAME too; NAME=PORT[/tcp|udp][@in|out], default both protocols and both directions)
portcullis apps          # running apps and the profile covering each
portcullis status        # packet counters
portcullis doctor        # kernel / tool checks
portcullis gui [--hidden]   # the map window (--hidden = tray only; the NixOS module autostarts it)
portcullis geo-update    # download the offline DB-IP City Lite database (needed for the map pins)
portcullis geo-status
portcullis launch NAME -- some-command   # for apps with no unit of their own
```

Match forms: `flatpak:ID`, `app:NAME`, `unit:GLOB`, or a bare word. The `--profile` form switches the profile on if it ends up with any effect. Block beats delay for the
same direction. Max delay 5000 ms. Loopback is never touched.

## The window

World map with a pin per remote location, each joined to **You** by an arc. Drag your own pin (or set
lat/lon in Settings) to show any place on stream; private/LAN addresses collect in a "Local Network" pill.
Left: every app with connections. Select one (list or its pin) and the right panel lists its
incoming/outgoing remotes, each with an allow toggle (all on by default) plus a master **Allow**.
Rules are per remote IP, any port; **Advanced** in Settings adds per-port rows and a per-port ask toggle.

**Named ports** (purple box in the app panel, or the CLI above): give a port number a name for that app,
then switch it on or off. Off = that port is dropped in the kernel, for any remote address, on either end of
the connection (so it covers ports the app connects to and ports it listens on), per protocol and direction.

**Colours**, as in Puppetry: orange = input/incoming, blue = output/outgoing, purple = both directions or
special (named ports, questions waiting), green = on/allowed, red = off/blocked; everything else is black and
white. Editable under Settings -> Signal colors (reopen the app to apply everywhere).

**Never allow by default** (Settings, with a per-app override): a new connection is held, you get a
notification with *Allow always / Allow temporarily / Ignore / Block always*. Ignore keeps it blocked and asks
again on the next attempt. Held packets are dropped after the hold timeout. Locations come from the offline DB
(DB-IP.com, CC BY 4.0); no lookups leave your machine.

## How it works

The daemon (system service, only `CAP_NET_ADMIN`) scans `app-*` cgroups under your user slice
once a second, matches them to profiles, and loads an `nft` table `inet portcullis` with
`socket cgroupv2` rules: `drop` for block, `queue` for delay. Delayed packets go through a
userspace NFQUEUE listener that holds each packet for its due time (FIFO, order preserved).
New or relaunched apps are picked up within about a second. Stopping the service removes the table.
If the delay listener dies, packets are accepted (fail-open), never stranded.

## Verified vs not

Also verified on a real kernel: ask-hold then allow, block-always, hold timeout drop, incoming ask, and
per-address / per-port block rules in both directions, and named ports (disabled port drops only that port, protocol
and direction are honoured, incoming listen ports). UI: 41 offscreen tests, including live round-trips to a
real control server, and rendered screenshots checked by eye.

Verified on a real kernel in network namespaces (54 tests): blocking each direction independently,
relaunch with a new unit name, counters, switching off restores traffic, loopback exempt,
delay out/in independent and in order with no loss, fail-open, flush on stop.

Not verified:
- the nft `queue` statement itself (the test kernel lacked `NFT_QUEUE`); the delay path was tested with
  iptables NFQUEUE feeding the same listener
- the Nix build (flake is parse-checked only; the `netfilterqueue` hash is computed from the sdist)
- the systemd service hardening, the tray, and D-Bus notification action buttons on a real Plasma session
- the real DB-IP download (only a synthetic .mmdb was tested), and the Nix build with the new deps

## Caveats

- Apps started from a terminal share the terminal's unit and cannot be told apart; use `portcullis launch`.
- Delay is in userspace: fine for game-sized traffic, a few ms of jitter, not for bulk transfers.
- Members of group `portcullis` can alter the `inet portcullis` firewall table.
- Ask mode only sees *new* connections after you enable it; existing flows keep working. Apps inside one unit share a decision.
- Kernel needs `NFT_SOCKET`, `NFT_QUEUE`, `NETFILTER_NETLINK_QUEUE` (NixOS kernels have them).
