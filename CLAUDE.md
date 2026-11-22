# Baruch

Rule-based backup of the Synology NAS to several external drives, docked one at a
time. Named after Jeremiah's scribe, who wrote the burned scroll out again
(Jeremiah 36).

## Source of truth

The full spec is `docs/spec.md`. Read it before designing or changing
behaviour. It holds the design principle, the requirements (numbered 1–39), the
registry format and unit states, the safety rules, the example configuration, the scenarios (drive
outgrows its shows, drive breaks), tech stack and testing approach. If a decision
changes the spec, update `docs/spec.md` in the same change. `README.md` is the
user documentation and must agree with the spec.

## Design principle

Baruch is run by hand by someone looking at its output. When something needs a
decision or is complicated, prefer surfacing the information (and the command to
act on it) over having the tool decide and act automatically. Apply this when
proposing designs.

## Core model (short version)

- **Drive:** backup disk identified by a label file on it (name such as `BK-6A`
  plus a random disk ID), never by DSM mount path. Must be ext4.
- **Rule:** source folder + `drives`, an ordered list (most preferred first;
  one drive sends everything there). Excludes are global only. A rule's units
  are its direct subfolders, plus one unit for loose files. One copy per unit.
- **State:** the drives are the truth. Each drive has per-unit records in
  `/.baruch/units.yaml` (`synced`, `result`: ok / errors / in-progress, set
  before each unit starts). The NAS registry holds only intentions (`home` per
  unit) and drive entries, plus a cache of each drive's records and contents,
  written only from that drive. States are derived, never stored.
- **Placement:** a new unit's home is a listed drive that already holds its
  data, else the first listed drive with room (recorded free space), else
  unplaced. Homes change only by `move set` or a config change.
- **Stale data** (a unit's data on a drive that isn't its home, or of a unit
  gone on the NAS) is never deleted by `run`, only by `prune`: once the home
  has a newer complete copy (`result: ok`, `synced` later than the stale
  copy's `synced` and `started`), or for a gone unit, with "files only here"
  shown. Runs never delete a whole unit. **Unexplained data** (no rule or unit
  explains it) is never deleted by Baruch; it shows the `rm` command.
- **Runs are manual and plan-first:** `baruch run` computes the full plan, shows
  it (with warnings for large deletions/overwrites), and asks y/n. `y` executes
  exactly the plan. No scheduler, no eject, no notifications.
- **Safety:** source marker `.backup-source` must exist; nothing outside the
  plan is changed; every deletion is listed right above the question.
- Sources never overlap (one inside another), and Baruch never looks outside
  rule sources. Two rules may share a source if their drive lists have no drive
  in common: independent copies (e.g. `home` on `BK-20A`, `home-1t` on `BK-1T`).
- Planned real setup: one rule per folder in `/volume1/archive`, plus `home` and
  `projects`. Everything goes to `BK-20A` except `tv`, which is spread over
  `BK-6A`, `BK-6B`, `BK-20A`.

## Target environment (NAS "mynas")

- DS1522+, DSM 7.2.2, x86_64. SSH: `ssh mynas`. `/volume1` is ext4.
- System Python is 3.8: don't use it. Homebrew lives in
  `/home/linuxbrew/.linuxbrew` (Python 3.15). It isn't on the PATH for
  non-interactive ssh, so call `/home/linuxbrew/.linuxbrew/bin/brew` directly.
- USB drives mount as `/volumeUSB*/usbshare*`. The system partition `/` has
  under 1 GiB free: never write to an unmounted mount point.
- Data sources: `/volume1/archive` (~17.1 TiB, tv 9.1 TiB, movies 7.5 TiB),
  `/volume1/homes/alex`, `/volume1/projects`. Full `du` over ssh is very slow.
  All sizes are binary units (TiB/GiB).
- Baruch runs as the normal user (no root). Config in `~/.config/baruch/`, state in
  `~/.local/state/baruch/`.

## Tech stack

- Python ≥ 3.12, uv, hatchling build backend, ruff, ty, pytest. Only Python
  dependency: PyYAML.
- rsync is called as a subprocess: Homebrew's (`<prefix>/opt/rsync/bin/rsync`),
  else the one on the `PATH` (Linux CI: the distribution's). No config setting.
  Baruch refuses openrsync and rsync < 3.2, so `/usr/bin/rsync` on macOS
  (openrsync) and DSM (3.1.2) is never used.
- Release: tag, then publish to `infogrind/homebrew-tap` with `pythonformula`.
  The formula has two hand-added lines, `depends_on "rsync"` and
  `depends_on "libyaml"` (for PyYAML, required by `brew audit`);
  `pythonformula` keeps them. Released: v0.1.0. NAS: `brew tap infogrind/tap && brew install baruch`.
- Dev version on the NAS: `uv tool install --force git+https://github.com/infogrind/baruch@<branch>`.

## Testing

- Everything runs with `uv run pytest` on the Mac (except tests for names the
  Mac can't represent) and on Linux in GitHub Actions (distro rsync ≥ 3.2).
- NAS-specific parts (drive discovery, free space, mount check) sit behind small
  replaceable interfaces with test fakes.
- Logic tests (`tests/unit/`): config + registry + file listings in, plan out.
- End-to-end tests (`tests/e2e/`, harness in `tests/support.py`): a fake NAS in
  a temp dir, real rsync, the CLI called in-process. Fake drives are temp
  folders on a "shelf", docked by symlinking them into the USB search folder;
  free space is computed from their files against a fake capacity. Running out
  of space mid-unit is simulated by replacing `execute.run_rsync`.
- Every `env.run` in a test is followed by the lost-data check (`Env.run` in
  `tests/support.py`): NAS unchanged, no write into an empty mount point, no
  file version or last backup lost unless the output named it. Opt out only
  for tests that change the NAS mid-command themselves (`env.check_loss`).
- `restore.sh` runs under plain `sh` in the tests and must pass `shellcheck`
  (dev dependency `shellcheck-py`).
- Fuzzing (`tests/fuzz/`, hypothesis): random sequences of user actions and
  faults, each ending with a disaster and a restore with every drive's
  `restore.sh`, derandomized by default; `BARUCH_FUZZ=explore` for more. Turn any
  failure it finds into an ordinary regression test as well.
- Test data is built in test code (`env.nas.add_show`, `add`, `replace`,
  `delete`, `rename`, `mark`, `remove_marker`). Content is derived from path +
  version number, never zero-filled; nothing generated is committed.
- Odd file names (NFC/NFD umlauts, invalid UTF-8, special characters, `@eaDir`,
  symlinks, unreadable files): tests the Mac's file system can't represent are
  skipped on macOS and run on Linux CI.

## Open points

- GitHub repo: `infogrind/baruch` (public). Don't push or create releases
  without asking.
- Check free space of the 20 TB drive (ext4, confirmed) and the layout of
  Hyper Backup's task folder on it.
