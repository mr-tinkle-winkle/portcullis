"""Fake daemon data + fake location lookup for exercising the window offscreen."""
import time

from portcullis.geo import Place

PLACES = {
    "142.250.80.46": Place(37.4, -122.1, "Mountain View", "United States", "US"),
    "151.101.1.69": Place(37.77, -122.42, "San Francisco", "United States", "US"),
    "104.18.2.35": Place(51.5, -0.12, "London", "United Kingdom", "GB"),
    "35.186.224.25": Place(50.11, 8.68, "Frankfurt", "Germany", "DE"),
    "128.116.21.3": Place(41.88, -87.63, "Chicago", "United States", "US"),
    "128.116.21.4": Place(41.88, -87.63, "Chicago", "United States", "US"),
    "13.107.42.14": Place(35.68, 139.69, "Tokyo", "Japan", "JP"),
    "18.65.1.9": Place(-33.87, 151.2, "Sydney", "Australia", "AU"),
    "52.95.1.1": Place(1.35, 103.82, "Singapore", "Singapore", "SG"),
    "200.1.2.3": Place(-23.55, -46.63, "São Paulo", "Brazil", "BR"),
}


class FakeGeo:
    available = True
    path = "/nonexistent"

    def lookup(self, ip):
        return PLACES.get(ip)

    def reload(self):
        pass


def remote(ip, direction="out", active=True, rule=None, ports=((443, "tcp"),), count=3, temp=False):
    return {"ip": ip, "direction": direction, "first": time.time() - 60, "last": time.time(), "count": count,
            "active": active, "ports": [{"proto": pr, "port": p, "count": 1, "active": active, "rule": None, "last": time.time()} for p, pr in ports],
            "units": ["u"], "rule": rule, "blocked": rule == "block", "temp_allowed": temp}


def overview(pending=()):
    sober = {"identity": "flatpak:org.vinegarhq.Sober", "units": ["app-flatpak-org.vinegarhq.Sober-1.scope"], "running": True,
             "profile": "Sober", "ask": False,
             "settings": {"enabled": True, "block_in": False, "block_out": False, "delay_in_ms": 0, "delay_out_ms": 120, "ask": "default"},
             "rules": [], "ports": [
                 {"name": "voice", "port": 3478, "proto": "udp", "direction": "out", "enabled": True},
                 {"name": "host", "port": 7777, "proto": "both", "direction": "in", "enabled": False}], "remotes": [
                 remote("128.116.21.3", ports=((53, "udp"), (443, "tcp"))), remote("128.116.21.4", ports=((53, "udp"),)),
                 remote("35.186.224.25", rule="block"), remote("104.18.2.35", direction="in", active=False, ports=((3478, "udp"),)),
                 remote("192.168.1.1", ports=((53, "udp"),))]}
    firefox = {"identity": "app:firefox", "units": ["app-firefox-2.scope"], "running": True, "profile": None, "ask": False,
               "remotes": [remote("142.250.80.46"), remote("151.101.1.69"), remote("104.18.2.35"), remote("13.107.42.14"),
                           remote("18.65.1.9", active=False)]}
    steam = {"identity": "app:steam", "units": ["app-steam-3.scope"], "running": True, "profile": "steam", "ask": True,
             "settings": {"enabled": True, "block_in": True, "block_out": True, "delay_in_ms": 0, "delay_out_ms": 0, "ask": "ask"},
             "rules": [], "remotes": [remote("52.95.1.1"), remote("200.1.2.3", rule="allow")]}
    old = {"identity": "app:discord", "units": [], "running": False, "profile": None, "ask": False, "remotes": []}
    return {"ok": True, "apps": [sober, firefox, steam, old], "pending": list(pending),
            "settings": {"ask_default": False, "ask_per_port": False, "track_flows": True, "temp_allow_minutes": 10,
                         "hold_seconds": 20, "quiet_seconds": 5}, "error": "", "flow_errors": {}, "temp_allows": []}
