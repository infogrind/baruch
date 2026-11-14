"""Drive lifecycle: register, forget, upgrade, taking a drive out of service."""

from support import spread

TV = "archive/tv"


def setup(env):
    env.nas.add_show(TV, "Lost", episodes=2, size=20_000)
    env.nas.add_show(TV, "Wire", episodes=2, size=10_000)
    env.nas.mark(TV)
    env.config(
        rules=[spread("tv", TV, ["BK-6A", "BK-6B"])],
        drives={"BK-6A": "10%", "BK-6B": "10%"},
    )
    env.ok("init")


def test_register_writes_a_label_and_records_free_space(env):
    setup(env)
    disk = env.drives.new("disk", capacity=100_000)
    env.drives.dock(disk)

    r = env.run("drive", "register", "BK-6A")

    assert r.code == 0, r.out
    assert disk.exists(".baruch/label.yaml")
    drive = env.registry()["drives"]["BK-6A"]
    assert drive["disk_id"]
    assert drive["capacity"] == 100_000
    assert drive["free"] > 90_000


def test_register_warns_about_names_not_in_the_config(env):
    setup(env)
    disk = env.drives.new("disk")
    env.drives.dock(disk)

    r = env.run("drive", "register", "BK-NEW")

    assert r.code == 0
    assert "not in the configuration" in r.out


def test_register_refuses_other_filesystems(env):
    setup(env)
    disk = env.drives.new("disk", fs_type="exfat")
    env.drives.dock(disk)

    r = env.run("drive", "register", "BK-6A")

    assert r.code != 0
    assert "ext4" in r.out
    assert not disk.exists(".baruch/label.yaml")


def test_register_refuses_a_name_whose_disk_is_still_known(env):
    setup(env)
    env.register("BK-6A")
    other = env.drives.new("other")
    env.drives.dock(other)

    r = env.run("drive", "register", "BK-6A")

    assert r.code != 0
    assert "already" in r.out


def test_register_needs_force_for_an_unknown_label(env):
    setup(env)
    disk = env.drives.new("disk")
    disk.path(".baruch").mkdir()
    disk.path(".baruch/label.yaml").write_text("name: OLD\ndisk_id: deadbeef\n")
    env.drives.dock(disk)

    r = env.run("drive", "register", "BK-6A")
    assert r.code != 0
    assert "--force" in r.out

    r = env.run("drive", "register", "BK-6A", "--force", answers="y")
    assert r.code == 0, r.out
    assert "OLD" in r.out  # the old label is shown before it's overwritten
    assert "BK-6A" in disk.path(".baruch/label.yaml").read_text()


def test_force_never_overwrites_the_label_of_a_registered_drive(env):
    setup(env)
    a = env.register("BK-6A")
    env.drives.dock(a)

    r = env.run("drive", "register", "BK-6B", "--force", answers="y")

    assert r.code != 0
    assert "BK-6A" in a.path(".baruch/label.yaml").read_text()


def test_forget_drops_copies_and_lists_units_without_backup(env):
    setup(env)
    a = env.register("BK-6A")
    env.backup(a)

    r = env.run("drive", "forget", "BK-6A", answers="y")

    assert r.code == 0, r.out
    assert "tv/Lost" in r.out and "no backup" in r.out
    assert env.unit("tv/Lost")["copies"] == {}
    assert env.unit("tv/Lost")["home"] == "BK-6A"  # config still names it


def test_forget_asks_first(env):
    setup(env)
    a = env.register("BK-6A")
    env.backup(a)

    r = env.run("drive", "forget", "BK-6A", answers="n")

    assert "BK-6A" in env.unit("tv/Lost")["copies"]
    assert "nothing changed" in r.out.lower()


def test_forgotten_disk_is_an_unknown_disk_and_the_name_can_be_reused(env):
    setup(env)
    a = env.register("BK-6A")
    env.backup(a)
    env.ok("drive", "forget", "BK-6A", answers="y")

    r = env.backup(a)
    assert r.code != 0
    assert "No usable drive" in r.out

    new = env.register("BK-6A", disk="new-disk")
    r = env.backup(new)
    assert r.code == 0, r.out
    assert "first copy" in r.out
    assert new.exists("tv/Lost/S01E01.mkv")


def test_old_disk_can_be_reused_with_force(env):
    setup(env)
    a = env.register("BK-6A")
    env.backup(a)
    env.ok("drive", "forget", "BK-6A", answers="y")
    env.drives.dock(a)

    r = env.run("drive", "register", "BK-6B", "--force", answers="y")

    assert r.code == 0, r.out


def test_upgrade_to_a_new_name_keeps_the_old_disk_until_the_new_one_has_copies(env):
    setup(env)
    old = env.register("BK-6A")
    env.backup(old)
    new = env.register("BK-12A")
    env.config(
        rules=[spread("tv", TV, ["BK-12A", "BK-6B"])],
        drives={"BK-12A": "10%", "BK-6B": "10%"},
    )

    env.backup(new)
    assert env.unit("tv/Lost")["home"] == "BK-12A"
    assert new.exists("tv/Lost/S01E01.mkv")
    assert old.exists("tv/Lost/S01E01.mkv")

    r = env.backup(old)
    assert r.code == 0, r.out
    assert "copy confirmed there on" in r.out
    assert not old.exists("tv/Lost")


def test_drive_out_of_service_still_holding_copies_shows_in_status(env):
    setup(env)
    old = env.register("BK-6A")
    env.backup(old)
    env.config(rules=[spread("tv", TV, ["BK-6B"])], drives={"BK-6B": "10%"})

    r = env.ok("status")

    assert "BK-6A" in r.out and "not in the configuration" in r.out


def test_drive_out_of_service_is_dropped_once_it_holds_no_copies(env):
    setup(env)
    old = env.register("BK-6A")
    env.backup(old)
    env.config(rules=[spread("tv", TV, ["BK-6B"])], drives={"BK-6B": "10%"})
    env.ok("drive", "forget", "BK-6A", answers="y")

    assert "BK-6A" not in env.registry()["drives"]
