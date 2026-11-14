# Baruch

Rule-based backup of the Synology NAS to several external drives, docked one at a
time. Named after Jeremiah's scribe, who wrote the burned scroll out again
(Jeremiah 36).

## Source of truth

The full spec is `docs/spec.md`. Read it before designing or changing
behaviour. It holds the requirements (numbered 1–25), the registry format and
move states, the failsafes, the example configuration, the scenarios (drive
outgrows its shows, drive breaks), tech stack and testing approach. If a decision
changes the spec, update `docs/spec.md` in the same change. `README.md` is the
user documentation and must agree with the spec.

## Core model (short version)

- **Drive:** backup disk identified by a label file on it (name such as `BK-6A`
  plus a random disk ID), never by DSM mount path.
- **Rule:** source folder + excludes + placement: `pin` (whole source to named
  drive(s)) or `spread` (each subfolder is a unit, assigned to one drive from an
  ordered list).
- **Registry:** per unit `home` (where it should be) and `copies` (where it is,
  written only after a successful sync). Move states are derived from the two,
  never stored. Moves are copy first, then delete.
- **Failsafes:** source marker file `.backup-source` must exist; deletions above
  `max_delete` are held back until approved.
- Planned real setup: everything except `archive/tv` pinned to `BK-20A`; tv
  spread over `BK-6A`, `BK-6B`, `BK-20A`.

## Target environment (NAS "mynas")

- DS1522+, DSM 7.2.2, x86_64. SSH: `ssh mynas`.
- System Python is 3.8: don't use it. Homebrew lives in
  `/home/linuxbrew/.linuxbrew` (Python 3.15). It isn't on the PATH for
  non-interactive ssh, so call `/home/linuxbrew/.linuxbrew/bin/brew` directly.
- USB drives mount as `/volumeUSB*/usbshare*`.
- Data sources: `/volume1/archive` (~18.9 TB, tv 10.0 TB, movies 8.3 TB),
  `/volume1/homes/alex`, `/volume1/projects`. Full `du` over ssh is very slow.
- Trigger: DSM Task Scheduler runs `baruch run` every few minutes; it's a no-op
  when no registered drive is docked.

## Tech stack

- Python ≥ 3.12, uv, hatchling build backend, ruff, pytest. Only Python
  dependency: PyYAML.
- rsync is called as a subprocess and must be Homebrew's rsync (declared as a
  formula dependency). Never use `/usr/bin/rsync`: on macOS it's openrsync.
- Release: tag, then publish to `infogrind/homebrew-tap` with `pythonformula`.
  NAS: `brew tap infogrind/tap && brew install baruch`.
- Dev version on the NAS: `uv tool install --force git+https://github.com/infogrind/baruch@<branch>`.

## Testing

- Everything must run on the Mac with `uv run pytest`; GitHub Actions also runs
  it on Linux.
- NAS-specific parts (drive discovery, free space, eject, notifications) sit
  behind small replaceable interfaces with test fakes.
- Logic tests: config + registry + drive state in, planned actions out.
- End-to-end tests: synthetic data in a temp dir, real rsync, fake drives are
  temp folders with a label file and a fake capacity.
- Test data: YAML descriptions in `tests/data/` describe only the starting
  state (scale 1 MB = 1 TB). Content is generated per test from path + version
  number. Nothing generated is committed, and no zero-filled files.
- Changes happen in test code via helpers (`nas.replace`, `nas.add`,
  `nas.delete`, `nas.rename`, `nas.add_show`, `nas.remove_marker`).
- Odd file names (NFC/NFD umlauts, invalid UTF-8, special characters, `@eaDir`,
  symlinks, unreadable files): tests the Mac's file system can't represent are
  skipped on macOS and run on Linux CI.

## Open points

- Does ejecting a USB drive via DSM need root? Decides whether baruch runs as
  alex or root. Test early on the NAS.
- GitHub repo `infogrind/baruch` not created yet. Don't push or create remote
  resources without asking.
- Check free space on the 20 TB drive next time it's docked.
- Notification channel: DSM or email.
