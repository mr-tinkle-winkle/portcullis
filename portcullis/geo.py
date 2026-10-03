"""
Where is an IP address?  Entirely offline, from the free DB-IP "City Lite" database
(MaxMind-format, CC BY 4.0, https://db-ip.com), kept in ``~/.local/share/portcullis``.

``portcullis geo-update`` (or the Settings button) downloads it once -- from db-ip.com, from YOUR
machine, so no addresses are involved -- and can be repeated monthly.  Lookups never touch the
network, and the daemon (which sees every address) never needs the database at all.
"""
from __future__ import annotations

import gzip
import ipaddress
import os
import shutil
import time
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

URL = "https://download.db-ip.com/free/dbip-city-lite-{year:04d}-{month:02d}.mmdb.gz"
ATTRIBUTION = "IP geolocation by DB-IP.com (CC BY 4.0)"


@dataclass(frozen=True)
class Place:
    lat: float
    lon: float
    city: str = ""
    country: str = ""
    code: str = ""

    @property
    def label(self) -> str:
        return ", ".join(x for x in (self.city, self.country) if x) or "Unknown"


def db_path() -> Path:
    env = os.environ.get("PORTCULLIS_GEO_DB")
    if env:
        return Path(env)
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "portcullis" / "dbip-city-lite.mmdb"


def is_private(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_unspecified


class Geo:
    """Lookups with a cache.  ``available`` is False until a database has been downloaded."""

    def __init__(self, path: "Path | None" = None):
        self.path = Path(path) if path else db_path()
        self._reader = None
        self._mtime = 0.0
        self._cache: dict = {}
        self._open()

    def _open(self) -> None:
        try:
            import maxminddb
            m = self.path.stat().st_mtime
            if self._reader is not None and m == self._mtime:
                return
            self._reader, self._mtime, self._cache = maxminddb.open_database(str(self.path)), m, {}
        except Exception:  # noqa: BLE001 -- missing file / bad file / library missing: no database
            self._reader = None

    @property
    def available(self) -> bool:
        return self._reader is not None

    def reload(self) -> None:
        self._open()

    def lookup(self, ip: str) -> "Place | None":
        """The Place for a PUBLIC address; None for private/unknown ones or without a database."""
        if self._reader is None or is_private(ip):
            return None
        if ip in self._cache:
            return self._cache[ip]
        place = None
        try:
            rec = self._reader.get(ip) or {}
            loc = rec.get("location") or {}
            if "latitude" in loc and "longitude" in loc:
                names = lambda k: ((rec.get(k) or {}).get("names") or {}).get("en", "")      # noqa: E731
                place = Place(float(loc["latitude"]), float(loc["longitude"]), names("city"), names("country"),
                              (rec.get("country") or {}).get("iso_code", ""))
        except Exception:  # noqa: BLE001
            place = None
        self._cache[ip] = place
        return place


USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) portcullis/0.1 (+https://github.com/mr-tinkle-winkle/portcullis)"


def _open(url: str, timeout: float = 60):
    """urlopen with a real User-Agent: db-ip.com answers 403 to Python's default one."""
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}),
                                  timeout=timeout)


def install_file(src: "str | Path", dest: "Path | None" = None) -> Path:
    """Use a database you downloaded yourself (.mmdb or .mmdb.gz), after checking that it opens."""
    import maxminddb
    src, dest = Path(src), Path(dest) if dest else db_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".mmdb.new")
    try:
        if src.suffix == ".gz":
            with gzip.open(src, "rb") as gz, open(tmp, "wb") as out:
                shutil.copyfileobj(gz, out)
        else:
            shutil.copyfile(src, tmp)
        maxminddb.open_database(str(tmp)).close()
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{src.name} isn't a usable location database: {e}") from None
    os.replace(tmp, dest)
    return dest


def update(dest: "Path | None" = None, *, opener=_open, today: "date | None" = None,
           progress=lambda msg: None) -> Path:
    """Download the newest available monthly database (this month, else the last two).  Atomic."""
    dest = Path(dest) if dest else db_path()
    today = today or date.today()
    errors = []
    y, m = today.year, today.month
    for _ in range(3):
        url = URL.format(year=y, month=m)
        progress(f"downloading {url}")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            with opener(url, timeout=60) as resp, open(tmp, "wb") as out:
                shutil.copyfileobj(resp, out)
            with gzip.open(tmp, "rb") as gz, open(dest.with_suffix(".mmdb.new"), "wb") as out:
                shutil.copyfileobj(gz, out)
            tmp.unlink()
            os.replace(dest.with_suffix(".mmdb.new"), dest)
            return dest
        except Exception as e:  # noqa: BLE001
            errors.append(f"{y}-{m:02d}: {e}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    for p in (dest.with_suffix(".part"), dest.with_suffix(".mmdb.new")):
        try:
            p.unlink()
        except OSError:
            pass
    raise RuntimeError("couldn't download the location database (" + "; ".join(errors) + "). "
                       "You can download dbip-city-lite-YYYY-MM.mmdb.gz yourself from db-ip.com/db/download/ip-to-city-lite "
                       "and choose it with 'Use a file I downloaded' (or: portcullis geo-update --file FILE).")


def age_days(path: "Path | None" = None) -> "int | None":
    try:
        return int((time.time() - (Path(path) if path else db_path()).stat().st_mtime) // 86400)
    except OSError:
        return None
