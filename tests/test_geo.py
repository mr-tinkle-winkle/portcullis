import gzip
import io
import json
from datetime import date

import pytest

from portcullis import geo


@pytest.fixture
def mmdb(tmp_path):
    from mmdb_writer import MMDBWriter
    w = MMDBWriter(ip_version=6, ipv4_compatible=True, database_type="DBIP-City-Lite", languages=["en"])
    w.insert_network(__import__("netaddr").IPSet(["8.8.8.0/24"]),
                     {"location": {"latitude": 37.4, "longitude": -122.1},
                      "city": {"names": {"en": "Mountain View"}}, "country": {"iso_code": "US", "names": {"en": "United States"}}})
    w.insert_network(__import__("netaddr").IPSet(["1.1.1.0/24"]), {"country": {"iso_code": "AU", "names": {"en": "Australia"}}})
    p = tmp_path / "db.mmdb"
    w.to_db_file(str(p))
    return p


def test_lookup_returns_a_place_for_public_addresses_only(mmdb):
    g = geo.Geo(mmdb)
    assert g.available
    p = g.lookup("8.8.8.8")
    assert (p.lat, p.lon, p.city, p.code) == (37.4, -122.1, "Mountain View", "US") and p.label == "Mountain View, United States"
    assert g.lookup("1.1.1.1") is None                      # no coordinates in the record
    assert g.lookup("9.9.9.9") is None                      # not in the database
    for private in ("192.168.1.5", "10.0.0.1", "127.0.0.1", "169.254.1.1", "224.0.0.1", "fe80::1", "garbage"):
        assert g.lookup(private) is None


def test_no_database_means_no_lookups_not_an_error(tmp_path):
    g = geo.Geo(tmp_path / "missing.mmdb")
    assert not g.available and g.lookup("8.8.8.8") is None


def test_a_database_that_appears_later_is_picked_up_by_reload(tmp_path, mmdb):
    target = tmp_path / "later.mmdb"
    g = geo.Geo(target)
    assert not g.available
    target.write_bytes(mmdb.read_bytes())
    g.reload()
    assert g.available and g.lookup("8.8.8.8")


def test_update_downloads_unzips_and_falls_back_to_earlier_months(tmp_path, mmdb):
    payload = gzip.compress(mmdb.read_bytes())
    asked = []

    def opener(url, timeout=0):
        asked.append(url)
        if "2026-10" in url or "2026-09" in url:
            raise OSError("404")
        return io.BytesIO(payload)

    dest = tmp_path / "out" / "db.mmdb"
    geo.update(dest, opener=opener, today=date(2026, 10, 3))
    assert [u.split("lite-")[1][:7] for u in asked] == ["2026-10", "2026-09", "2026-08"]
    assert geo.Geo(dest).lookup("8.8.8.8").city == "Mountain View"
    assert not list(dest.parent.glob("*.part")) and not list(dest.parent.glob("*.new"))


def test_update_failure_leaves_the_old_database_alone(tmp_path, mmdb):
    dest = tmp_path / "db.mmdb"
    dest.write_bytes(mmdb.read_bytes())
    before = dest.read_bytes()
    with pytest.raises(RuntimeError, match="couldn't download"):
        geo.update(dest, opener=lambda *a, **k: (_ for _ in ()).throw(OSError("offline")), today=date(2026, 1, 2))
    assert dest.read_bytes() == before


def test_world_map_data_is_packaged_and_sane():
    import importlib.resources as r
    data = json.loads(r.files("portcullis").joinpath("data/world.json").read_text())
    assert len(data["rings"]) > 200
    for ring in data["rings"][:50]:
        assert all(-180 <= x <= 180 and -90 <= y <= 90 for x, y in ring)


def test_the_default_opener_sends_a_real_user_agent(monkeypatch):
    """db-ip.com answers 403 to Python's default User-Agent (the failure seen on a real machine)."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["ua"], seen["url"] = req.get_header("User-agent"), req.full_url
        return io.BytesIO(b"")
    monkeypatch.setattr(geo.urllib.request, "urlopen", fake_urlopen)
    geo._open("https://download.db-ip.com/free/x.mmdb.gz")
    assert seen["ua"].startswith("Mozilla/5.0") and "portcullis" in seen["ua"] and "urllib" not in seen["ua"].lower()


def test_failure_message_points_to_the_manual_route(tmp_path):
    def refuse(url, timeout=None):
        raise OSError("HTTP Error 403: Forbidden")
    with pytest.raises(RuntimeError) as e:
        geo.update(tmp_path / "x.mmdb", opener=refuse, today=date(2026, 10, 3))
    assert "403" in str(e.value) and "Use a file I downloaded" in str(e.value) and "--file" in str(e.value)


def test_install_file_accepts_mmdb_and_gz_and_rejects_junk(tmp_path, mmdb):
    dest = tmp_path / "dest" / "db.mmdb"
    geo.install_file(mmdb, dest)
    assert geo.Geo(dest).lookup("8.8.8.8").city == "Mountain View"
    gz = tmp_path / "db.mmdb.gz"
    gz.write_bytes(gzip.compress(mmdb.read_bytes()))
    dest2 = tmp_path / "dest2.mmdb"
    geo.install_file(gz, dest2)
    assert geo.Geo(dest2).available
    junk = tmp_path / "junk.mmdb"
    junk.write_bytes(b"not a database")
    with pytest.raises(RuntimeError, match="isn't a usable"):
        geo.install_file(junk, dest)
    assert geo.Geo(dest).lookup("8.8.8.8").city == "Mountain View"          # the old one survived
    assert not list(dest.parent.glob("*.new"))
