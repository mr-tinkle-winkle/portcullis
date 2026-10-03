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
portcullis apps          # running apps and the profile covering each
portcullis status        # packet counters
portcullis doctor        # kernel / tool checks
portcullis gui           # window with "Add from running app..."
portcullis launch NAME -- some-command   # for apps with no unit of their own
```

Match forms: `flatpak:ID`, `app:NAME`, `unit:GLOB`, or a bare word. Block beats delay for the
same direction. Max delay 5000 ms. Loopback is never touched.

## How it works

The daemon (system service, only `CAP_NET_ADMIN`) scans `app-*` cgroups under your user slice
once a second, matches them to profiles, and loads an `nft` table `inet portcullis` with
`socket cgroupv2` rules: `drop` for block, `queue` for delay. Delayed packets go through a
userspace NFQUEUE listener that holds each packet for its due time (FIFO, order preserved).
New or relaunched apps are picked up within about a second. Stopping the service removes the table.
If the delay listener dies, packets are accepted (fail-open), never stranded.

## Verified vs not

Verified on a real kernel in network namespaces (54 tests): blocking each direction independently,
relaunch with a new unit name, counters, switching off restores traffic, loopback exempt,
delay out/in independent and in order with no loss, fail-open, flush on stop.

Not verified:
- the nft `queue` statement itself (the test kernel lacked `NFT_QUEUE`); the delay path was tested with
  iptables NFQUEUE feeding the same listener
- the Nix build (flake is parse-checked only; the `netfilterqueue` hash is computed from the sdist)
- the systemd service hardening and the GUI on a real Plasma session

## Caveats

- Apps started from a terminal share the terminal's unit and cannot be told apart; use `portcullis launch`.
- Delay is in userspace: fine for game-sized traffic, a few ms of jitter, not for bulk transfers.
- Members of group `portcullis` can alter the `inet portcullis` firewall table.
- Kernel needs `NFT_SOCKET`, `NFT_QUEUE`, `NETFILTER_NETLINK_QUEUE` (NixOS kernels have them).
