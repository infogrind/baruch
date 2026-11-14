# Baruch

Rule-based backup of a Synology NAS to several external drives, docked one at a
time.

Baruch was Jeremiah's scribe, who wrote the burned scroll out again (Jeremiah
36).

## What it does

You have more data than fits on one backup disk, and a USB dock that holds one
disk at a time. Baruch decides which data goes to which disk, and keeps each
disk an exact mirror of its share whenever you dock it.

- **Rules** say what goes where. A rule is a source folder, optional excludes,
  and a placement:
  - `pin`: the whole source goes to one named drive, or to several (a full copy
    on each).
  - `spread`: each subfolder of the source (for example each TV show) is a
    *unit* that lives on exactly one drive, picked from an ordered list.
- **Drives** are identified by a label file on the disk, never by where DSM
  happens to mount them. A disk without a valid label is never written to.
- **The registry** records, per unit, where it *should* be (`home`) and where
  copies *actually* exist (`copies`). Baruch derives everything else from those
  two.
- **Docking a registered drive starts a backup.** You get a notification at the
  start and at the end, and the drive is ejected when it's done.
- **Two failsafes** protect the backup: a source must contain a marker file
  `.backup-source`, and unusually large deletions wait for your approval.

Baruch knows nothing about TV shows, movies or disk sizes. All of that lives in
the configuration.

## Contents

- [Getting started](#getting-started)
- [Concepts](#concepts)
- [Configuration](#configuration)
- [Use cases](#use-cases)
- [Command reference](#command-reference)
- [Files and locations](#files-and-locations)
- [Development](#development)

## Getting started

This tutorial sets up Baruch on a Synology NAS (DSM 7.2) with three backup
drives: a 20 TB drive `BK-20A` and two 6 TB drives `BK-6A` and `BK-6B`. It backs
up `/volume1/archive`, `/volume1/homes/alex` and `/volume1/projects`. TV shows
(`/volume1/archive/tv`) are spread over the 6 TB drives, and everything else is
pinned to the 20 TB drive.

### 1. Install

Baruch needs Homebrew on the NAS. Homebrew isn't on the `PATH` for
non-interactive SSH sessions, so call it by its full path:

```sh
ssh mynas
/home/linuxbrew/.linuxbrew/bin/brew tap infogrind/tap
/home/linuxbrew/.linuxbrew/bin/brew install baruch
baruch --version
```

This also installs Homebrew's `rsync`, which Baruch always uses. It never uses
the system `/usr/bin/rsync`.

### 2. Write the configuration

Create `~/.config/baruch/config.yaml`:

```yaml
drives:
  BK-20A: { reserve: 5% }
  BK-6A:  { reserve: 10% }
  BK-6B:  { reserve: 10% }

defaults:
  exclude: ["@eaDir", "#recycle", ".DS_Store", ".TemporaryItems", ".apdisk"]
  max_delete: 5%
  stale_after: 30d
  rebalance: suggest
  notify: dsm

rules:
  - name: archive
    source: /volume1/archive
    exclude: [tv]
    pin: BK-20A

  - name: home
    source: /volume1/homes/alex
    pin: BK-20A

  - name: projects
    source: /volume1/projects
    pin: BK-20A

  - name: tv
    source: /volume1/archive/tv
    spread: [BK-6A, BK-6B, BK-20A]   # order of preference
    max_delete: 10%
```

Check it:

```sh
baruch config check
```

### 3. Mark the sources

Each source folder must contain a marker file. Without it, Baruch skips the
rule, so an unmounted volume or a renamed folder never looks like "everything
was deleted".

```sh
touch /volume1/archive/.backup-source
touch /volume1/archive/tv/.backup-source
touch /volume1/homes/alex/.backup-source
touch /volume1/projects/.backup-source
```

### 4. Register the drives

Dock one drive, find where DSM mounted it, and register it. This writes a label
file with the drive's name and a random disk ID to the drive.

```sh
ls -d /volumeUSB*/usbshare*        # e.g. /volumeUSB1/usbshare
baruch drive register BK-6A --path /volumeUSB1/usbshare
```

Undock it, then repeat for `BK-6B` and `BK-20A`. Before registering a used disk,
run an extended SMART test on it in DSM (Storage Manager → HDD/SSD).

### 5. Do a dry run

Dock a drive and see what Baruch would do, without changing anything:

```sh
baruch run --dry-run
```

The output lists the units that would be assigned to this drive, and what would
be copied and deleted, with sizes.

### 6. Run the first backup

```sh
baruch run
```

Baruch assigns new TV shows to the docked drive while it has room above its
reserve, copies every unit whose home is this drive, writes a registry snapshot
and a contents list to the drive, and ejects it. A first run over USB copies
about 0.6–0.7 TB an hour. You can stop it (Ctrl-C) and run it again later: a
unit is only recorded once it's completely copied.

### 7. Start backups automatically

In DSM, open Control Panel → Task Scheduler → Create → Scheduled Task →
User-defined script:

- **User:** `alex` (or `root`, see [Running as root](#running-as-root-or-as-a-user))
- **Schedule:** daily, every 5 minutes
- **Script:** `/home/linuxbrew/.linuxbrew/bin/baruch run`

From now on, docking a registered drive is enough. A run with no registered
drive docked does nothing and exits at once.

### 8. Check the status

```sh
baruch status
```

The status shows, per drive, when it was last backed up and how much space is
free. It also lists units that have no backup yet or are outdated, suggested
moves, and drives that haven't been docked for longer than `stale_after`.

## Concepts

### Drives

A drive is a backup disk with a label file in its root (`.baruch-label`). The
label holds the drive's **name** (such as `BK-6A`) and a random **disk ID**
generated at registration. Baruch writes to a disk only if its label's name and
disk ID both match the registry. Any other disk in the dock is ignored. If a
label is present but its ID doesn't match, you're notified.

Each drive has two limits, both expressed as free space:

- **Reserve** (`reserve`): when free space falls to the reserve, the drive stops
  taking *new* units. Units already assigned to it keep growing into the
  reserve.
- **Move limit** (`move_limit`): when free space falls to the move limit, the
  status suggests moving units off the drive. The default is half the reserve.

### Rules and units

A rule turns a source folder into one or more units:

- A `pin` rule is a single unit, named after the rule (e.g. `archive`). It goes
  to every drive in its `pin` list.
- A `spread` rule makes each direct subfolder of the source its own unit, named
  `<rule>/<subfolder>` (e.g. `tv/The Wire`). Each unit lives on exactly one
  drive. Files directly in the source folder (not in a subfolder) aren't backed
  up by a spread rule.

On each drive, every rule gets its own top-level folder named after the rule:
`/archive/…`, `/tv/The Wire/…`. Rules never collide, even when their sources
overlap.

### Placement

Units of a spread rule are placed like this:

1. A new unit is assigned the first time it's seen, to the docked drive, if that
   drive has room above its reserve and the unit fits.
1. The `spread` list is in order of preference. A drive lower in the list only
   takes new units once every drive above it was full at its last backup.
1. Assignments are permanent. They change only by a confirmed move, by editing
   the registry, or when a drive is retired or replaced.
1. A unit that fits on no drive is reported, never split.
1. On a drive shared by pin and spread rules, the pinned rules get the space
   first. Spread units only use what's left beyond the reserve.

### The registry and unit states

The registry stores for each unit where it should be (`home`) and where copies
exist (`copies`). The state of a unit is derived from those two fields:

| State | Condition | What happens next |
| --------------- | ------------------------------------------------- | --------------------------------------------------------- |
| Waiting | `home` is empty | The next docked drive with room becomes `home` |
| In place | `copies` holds `home` only | Normal sync |
| Suggested | `proposed` is set | Nothing until you confirm it |
| Copy pending | `home` isn't in `copies` | Copied when the `home` drive is docked |
| Cleanup pending | `home` is in `copies`, plus other drives | Deleted from the other drives when they're docked |
| Outdated | The copy on `home` was skipped for lack of space | Reported; synced once there's room |
| Gone | The folder no longer exists on the NAS | Deleted from every drive in `copies`, then forgotten |

A move is just setting `home` to another drive. The unit then passes through
*copy pending* and *cleanup pending* back to *in place*. The old copy is only
deleted once the new drive holds a complete copy.

`copies` is only updated after a successful sync of that unit, so an interrupted
run leaves the old state and the next run repeats the step.

### Failsafes

**Source check.** Before anything else, each rule's source folder must exist and
contain `.backup-source`. If it doesn't, the rule is skipped and reported, and
nothing on the drive is touched for it.

**Delete limit.** Before copying, Baruch counts everything the run would delete
or overwrite on the drive. If that's more than `max_delete` of the data backed
up on the drive, deletions are held back. New and changed files are still
copied; only the deletions wait. Planned removals (cleanup after a confirmed
move, and units that are gone) don't count towards the limit. You get a
notification with the amount and the largest folders, and the full list is
written to a file. See [Approving held-back deletions](#approving-held-back-deletions).

## Configuration

The configuration file is YAML, at `~/.config/baruch/config.yaml` by default
(override with `--config` or `BARUCH_CONFIG`).

### `drives`

One entry per drive name. Every drive a rule refers to must be listed here.

| Key | Default | Meaning |
| ------------ | --------------- | -------------------------------------------------------------- |
| `reserve` | `10%` | Free space at which the drive stops taking new units |
| `move_limit` | half of reserve | Free space at which the status suggests moving units off it |

Both take a percentage of the drive's capacity (`10%`) or an absolute size
(`500G`, `1.5T`).

### `defaults`

Settings that apply to every rule unless the rule overrides them.

| Key | Default | Meaning |
| -------------- | --------- | ------------------------------------------------------------------------ |
| `exclude` | `[]` | rsync exclude patterns, relative to the source |
| `max_delete` | `5%` | Delete limit, as a share of the data backed up on the drive |
| `stale_after` | `30d` | Report drives that haven't been docked for longer than this |
| `rebalance` | `suggest` | `suggest`: moves wait for confirmation. `auto`: moves happen by themselves |
| `verify_every` | off | Make a normal run also verify the drive once this interval has passed |
| `notify` | `dsm` | `dsm` (DSM notification) or `email` |

`exclude` in a rule is added to the default excludes, not a replacement.

### `rules`

| Key | Required | Meaning |
| ------------ | -------------------- | ------------------------------------------------------------ |
| `name` | yes | Unique name; also the rule's folder on each drive |
| `source` | yes | Absolute path of the source folder on the NAS |
| `pin` | one of pin or spread | A drive name, or a list of drives that each get a full copy |
| `spread` | one of pin or spread | Ordered list of drives; each subfolder goes to one of them |
| `exclude` | no | Extra exclude patterns |
| `max_delete` | no | Overrides the default delete limit for this rule |

Durations are written as `30d`, `12h` or `90d`.

## Use cases

### Routine backup

Dock a drive. Within a few minutes you get a "backup started" notification.
When it's done you get a summary (copied, deleted, skipped, errors), and the
drive is ejected. Undock it and put it away. Rotate the drives as often as you
like; each one is brought up to date for its own units when it's docked.

To run by hand instead of waiting for the scheduler: `baruch run`.

### A new TV show appears

Nothing to do. The next time a drive from the `spread` list is docked, the show
is assigned to it if that drive is the first in the list with room. Until then
the status lists it as *waiting* (no backup yet).

### Shows outgrow their drive

Shows already on `BK-6A` keep getting new seasons. Assignments don't move on
their own, so the growth lands on `BK-6A`.

1. **Reserve reached:** `BK-6A` stops taking new shows; they go to `BK-6B` or
   `BK-20A`. The status flags "existing units growing into the reserve".
1. **Move limit reached:** the status suggests moving the fewest shows that
   bring `BK-6A` back above its reserve, preferring shows that haven't changed
   for a long time. The suggestion is recorded as `proposed`.
1. **Confirm:** `baruch move confirm "tv/Lost"` (or `baruch move confirm --all`).
   The show's `home` is now the new drive (*copy pending*).
1. **Dock the new drive:** the show is copied there (*cleanup pending*).
1. **Dock `BK-6A`:** the old copy is deleted first, then the remaining shows are
   synced in the freed space. If `BK-6A` is docked before step 4, nothing is
   deleted from it.

With `rebalance: auto`, step 3 happens by itself.

If a drive runs out of space during a run anyway, the units that don't fit are
skipped and reported. Their previous copy stays on the drive and they're marked
*outdated* until there's room. A unit is never left half-copied, and no other
unit is deleted to make room.

### Moving a unit by hand

```sh
baruch move set "tv/The Wire" --to BK-20A
```

This sets `home` directly. You can also edit `home` and `proposed` in the
registry file by hand; Baruch writes all other fields itself.

To discard a suggestion: `baruch move reject "tv/Lost"`.

### Adding a drive

When every drive in a `spread` list is full:

1. SMART-test the new disk, dock it and register it:
   `baruch drive register BK-6C --path /volumeUSB1/usbshare`.
1. Add it to `drives` and to the rule's `spread` list.
1. New shows go there from now on, and confirmed moves free up the old drives.

### Approving held-back deletions

When the delete limit triggers, the run copies new and changed files, keeps the
old ones, and notifies you with the amount and the largest folders. The full
list is in `~/.local/state/baruch/held/<drive>-<date>.txt`.

If the deletions are expected (for example Sonarr replaced a whole season with
a better quality), approve them:

```sh
baruch approve BK-6A
```

The deletions are carried out the next time `BK-6A` is docked. If the drive is
still docked, run `baruch run` again. To approve during the run itself, use
`baruch run --approve-deletes`. Without approval, the old files simply stay.

### A source folder is missing

If a volume isn't mounted or a source folder was renamed, its marker is missing.
The rule is skipped, nothing on the drive is touched for it, and the run's
summary says so. Fix the mount or the path, and the next run picks it up again.

If you rename a source folder on purpose, update `source` in the configuration
and keep the marker in the folder.

### A unit is deleted on the NAS

When a show's folder disappears from the NAS, the unit becomes *gone*. Its
copies are deleted from each drive the next time that drive is docked, and then
the unit is forgotten. Gone units don't count towards the delete limit.

### An unknown disk is docked

A disk without a label is ignored. Baruch doesn't touch it and doesn't eject it.

A disk with a label whose disk ID doesn't match the registry (for example an old
drive that was replaced) is never written to. You're notified and asked to
either wipe it or register it as a new drive.

### A drive fails

Baruch can't tell a broken drive from one lying in a drawer, so it never
declares a drive lost by itself. Signs of trouble: a run reports read or write
errors and stops for that drive; the drive isn't recognised when docked; or the
status says it hasn't been docked for a long time.

1. **Declare it lost:** `baruch drive lost BK-6A`. Its copies are removed from
   the registry, and the status lists what now exists only on the NAS (e.g. "61
   units, 4.9 TB have no backup").
1. **Then either replace it:** SMART-test a new disk, dock it, and run
   `baruch drive register BK-6A --replace --path /volumeUSB1/usbshare`. The new
   disk takes over the name, so the configuration stays the same. It may be
   larger. All units whose home is `BK-6A` become *copy pending* and are copied
   on its first run.
1. **Or retire it:** `baruch drive retire BK-6A`, then remove it from the
   configuration. Its units become *waiting* and go to other drives with room as
   they're docked.

Restoring about 5 TB over USB takes roughly 7–9 hours. The run can be stopped
and continued.

### A drive is failing slowly

A drive can still mount while some files can no longer be read. A verify run
catches this: it reads every file on the docked drive and compares it with the
NAS.

```sh
baruch verify
```

Files that differ are copied again, and errors are counted in the status. Set
`verify_every: 90d` to have normal runs verify automatically. If a drive shows
many errors, the status recommends retiring it.

### Retiring a healthy drive

To take a working drive out of service, move all its units off it first:

```sh
baruch drive retire BK-6A
```

On a drive that is still healthy, this marks its units as *waiting*. They are
assigned to other drives as those are docked. `BK-6A` keeps its copies until
each unit has a confirmed copy elsewhere; dock it once more at the end to clean
it up, then remove it from the configuration.

### Restoring files

The drives hold plain files, so you don't need Baruch to restore. Each rule's
data is in `/<rule name>/` on the drive, with the same structure as the source.
Look up which drive holds a unit:

```sh
baruch status --unit "tv/The Wire"
```

Then dock that drive and copy back with Homebrew's rsync, for example:

```sh
rsync -a "/volumeUSB1/usbshare/tv/The Wire/" "/volume1/archive/tv/The Wire/"
```

Each drive also holds `baruch/contents.txt`, a list of all files on it, so you
can search for a file without docking every drive.

### The NAS registry is lost

The registry on the NAS is the one that counts. Each drive holds a snapshot of
it (`baruch/registry.yaml`) and a list of its contents. To rebuild, dock the
drives one by one and run:

```sh
baruch registry rebuild
```

The newest snapshot found is used as the base, and each drive's contents list
confirms what is actually on it.

### Switching over from Hyper Backup

If an existing drive already holds a plain mirror (such as a single-version
Hyper Backup target), you don't need to copy everything again:

1. Register the drive (`baruch drive register BK-20A --path …`).
1. Move its existing folders into the `/<rule name>/…` layout with `mv`. This is
   instant, because it's the same disk. For the example configuration:
   `archive/` (without `tv`), `home/`, `projects/`, and `tv/` for the shows.
1. Do a dry run, then a real run. rsync only transfers what changed.
1. As shows get assigned to the 6 TB drives, their old copies are removed from
   `BK-20A` like any move (cleanup pending).
1. Turn off the Hyper Backup task once every drive has had a complete run.

### Running as root or as a user

Baruch can run as a normal user as long as that user can read all sources and
write to the USB drives. Ejecting a USB drive in DSM may need root; if it does,
run the scheduled task as `root` and set `BARUCH_HOME` so that root uses the
same configuration and registry.

## Command reference

Global options (for every command):

| Option | Meaning |
| ----------------- | -------------------------------------------------------- |
| `--config PATH` | Configuration file (default `~/.config/baruch/config.yaml`) |
| `--home PATH` | State directory with registry and logs (default `~/.local/state/baruch`) |
| `-v`, `--verbose` | More output, including the rsync commands |
| `--version` | Print the version and exit |
| `-h`, `--help` | Help for any command |

Exit codes: `0` success (including "no drive docked"), `1` run finished with
warnings (skipped units, held-back deletions), `2` errors (I/O errors, invalid
configuration), `3` another run is already in progress.

### `baruch run`

Back up the docked drive.

```text
baruch run [--dry-run] [--drive NAME] [--approve-deletes] [--no-eject]
```

Finds docked drives with a valid label, picks the first one, and for it:
checks the source markers, assigns new units, works out what to copy and
delete, applies the delete limit, syncs each unit with rsync, updates the
registry after each successful unit, writes the registry snapshot and contents
list to the drive, sends the end notification, and ejects the drive.

Only one run happens at a time; a second `baruch run` while one is in progress
exits with code 3. Does nothing if no registered drive is docked.

| Option | Meaning |
| ------------------- | ----------------------------------------------------------------- |
| `--dry-run` | Show what would be assigned, copied and deleted; change nothing |
| `--drive NAME` | Only consider this drive, if several are docked |
| `--approve-deletes` | Carry out deletions even if they exceed the delete limit |
| `--no-eject` | Leave the drive mounted afterwards |

### `baruch status`

Show the state of the backup.

```text
baruch status [--drive NAME] [--unit UNIT] [--json]
```

Per drive: last backup, capacity, free space, reserve and move limit, verify
errors. Per unit, only those that need attention: waiting, copy pending,
cleanup pending, outdated, suggested moves, units that fit on no drive, and
units with no backup at all. Drives not docked for longer than `stale_after`
are flagged.

| Option | Meaning |
| ------------- | --------------------------------------------- |
| `--drive NAME`| Only this drive and its units |
| `--unit UNIT` | Details for one unit: home, copies, sizes, dates |
| `--json` | Machine-readable output |

### `baruch verify`

Read every file on the docked drive and compare it with the NAS.

```text
baruch verify [--drive NAME] [--no-eject]
```

Files that differ are copied again. Read errors are counted and shown in the
status. Takes as long as reading the whole drive.

### `baruch drive register`

Register a docked disk as a backup drive.

```text
baruch drive register NAME --path PATH [--replace]
```

Writes a label with `NAME` and a new random disk ID to the disk at `PATH`, and
adds the drive to the registry. `NAME` must be listed in the configuration's
`drives`. Refuses if the disk already has a label, or if `NAME` is already
registered.

| Option | Meaning |
| ----------- | ----------------------------------------------------------------------- |
| `--path` | Mount point of the docked disk, e.g. `/volumeUSB1/usbshare` |
| `--replace` | Take over the name of a drive declared lost; its units become copy pending |

### `baruch drive list`

List registered drives with their disk ID, last backup, capacity and free space.

### `baruch drive lost`

```text
baruch drive lost NAME
```

Declare a drive lost. Removes all its copies from the registry. Units whose home
is the drive keep it as home (so a replacement can take over) and are listed as
having no backup. Asks for confirmation.

### `baruch drive retire`

```text
baruch drive retire NAME
```

Take a drive out of service. Its units become waiting and are reassigned through
normal placement. Afterwards, remove the drive from the configuration.

### `baruch move`

Manage moves of units between drives.

```text
baruch move list
baruch move confirm UNIT... | --all
baruch move reject UNIT... | --all
baruch move set UNIT --to DRIVE
```

| Subcommand | Meaning |
| ---------- | ---------------------------------------------------------- |
| `list` | Show suggested moves with sizes and reasons |
| `confirm` | Accept suggested moves: `home` becomes the proposed drive |
| `reject` | Discard suggested moves |
| `set` | Move a unit to a drive of your choice |

### `baruch approve`

```text
baruch approve DRIVE
```

Approve the deletions held back by the delete limit on `DRIVE`. They are carried
out on the next run on that drive. The approval covers exactly the list from the
last held-back run; if more is to be deleted by then, the limit applies again.

### `baruch config check`

Validate the configuration: YAML syntax, required keys, drive names used in
rules are defined, rule names are unique, sources exist and contain the marker.

### `baruch registry rebuild`

```text
baruch registry rebuild [--drive NAME]
```

Rebuild the NAS registry from the snapshot and contents list on the docked
drive. Run it once per drive. Keeps the newer information when drives disagree.

## Files and locations

| Path | What |
| -------------------------------------------- | ------------------------------------------------- |
| `~/.config/baruch/config.yaml` | Configuration |
| `~/.local/state/baruch/registry.yaml` | Registry (the one that counts) |
| `~/.local/state/baruch/held/` | Lists of held-back deletions |
| `~/.local/state/baruch/logs/` | One log file per run |
| `<source>/.backup-source` | Source marker |
| `<drive>/.baruch-label` | Drive label: name and disk ID |
| `<drive>/baruch/registry.yaml` | Registry snapshot, written at the end of each run |
| `<drive>/baruch/contents.txt` | List of all files on the drive |
| `<drive>/<rule name>/…` | The backed-up data |

`BARUCH_HOME` sets the state directory; `BARUCH_CONFIG` sets the configuration
file.

Example registry:

```yaml
units:
  tv/The Wire:
    home: BK-6B
    copies:
      BK-6A: { synced: 2026-11-02, size: 410G }
      BK-6B: { synced: 2026-11-20, size: 410G }

  tv/Lost:
    home: BK-6A
    proposed: BK-6B
    copies:
      BK-6A: { synced: 2026-11-02, size: 380G }

  tv/Severance:
    home: null
    first_seen: 2026-11-15
    copies: {}

drives:
  BK-6A:  { disk_id: 7f3c…9a, last_seen: 2026-11-02, capacity: 5.45T, free: 0.21T }
  BK-6B:  { disk_id: 2b81…c4, last_seen: 2026-11-20, capacity: 5.45T, free: 1.02T }
  BK-20A: { disk_id: e09d…17, last_seen: 2026-11-18, capacity: 18.2T, free: 8.90T }
```

## Development

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/). Install Homebrew's
rsync on the Mac (`brew install rsync`); macOS's `/usr/bin/rsync` is openrsync
and behaves differently.

```sh
uv sync
uv run pytest
uv run ruff check .
```

Tests run on the Mac and on Linux (GitHub Actions). NAS-specific parts (drive
discovery, free space, eject, notifications) sit behind small interfaces with
test fakes. End-to-end tests build a fake NAS and fake drives in a temporary
folder from YAML descriptions in `tests/data/` (1 MB stands for 1 TB) and run
real rsync. Tests for file names the Mac can't represent are skipped on macOS.

Install a development version on the NAS:

```sh
/home/linuxbrew/.linuxbrew/bin/brew install uv
uv tool install --force git+https://github.com/infogrind/baruch@<branch>
```

Release: tag a version, then publish to the `infogrind/homebrew-tap` tap with
`pythonformula`.
