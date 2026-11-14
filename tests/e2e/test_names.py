"""Odd file names, based on real cases on the NAS."""

import os
import unicodedata

from support import needs_linux, spread

TV = "archive/tv"


def setup(env):
    env.nas.mark(TV)
    env.config(rules=[spread("tv", TV, ["BK-6A"])], drives={"BK-6A": "10%"})
    env.ok("init")
    return env.register("BK-6A")


def test_spaces_quotes_and_brackets(env):
    a = setup(env)
    name = "It's Always Sunny [HD] & More"
    env.nas.add_show(TV, name, episodes=2, size=2000)

    r = env.backup(a)

    assert r.code == 0, r.out
    assert a.exists(f"tv/{name}/S01E01.mkv")
    assert f"tv/{name}" in env.registry()["units"]


def test_very_long_names(env):
    a = setup(env)
    name = "x" * 200
    env.nas.add_show(TV, name, episodes=1, size=100)

    env.backup(a)

    assert a.exists(f"tv/{name}/S01E01.mkv")


@needs_linux
def test_nfc_and_nfd_forms_are_different_units(env):
    a = setup(env)
    nfc = unicodedata.normalize("NFC", "Bärgfilm")
    nfd = unicodedata.normalize("NFD", "Bärgfilm")
    env.nas.add_show(TV, nfc, episodes=1, size=100)
    env.nas.add_show(TV, nfd, episodes=1, size=200)

    env.backup(a)

    units = env.registry()["units"]
    assert f"tv/{nfc}" in units and f"tv/{nfd}" in units
    assert a.exists(f"tv/{nfc}/S01E01.mkv") and a.exists(f"tv/{nfd}/S01E01.mkv")


@needs_linux
def test_names_that_are_not_valid_utf8(env):
    a = setup(env)
    raw = os.fsdecode(b"Caf\xe9")
    env.nas.add_show(TV, raw, episodes=1, size=100)

    r = env.backup(a)

    assert r.code == 0, r.out
    assert os.path.exists(os.fsencode(a.root) + b"/tv/Caf\xe9/S01E01.mkv")
    assert "tv/Caf\\xe9" in env.registry()["units"]
    r = env.backup(a)
    assert "nothing to do" in r.out.lower()
