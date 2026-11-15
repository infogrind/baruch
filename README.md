# Baruch

Rule-based backup of a Synology NAS to several external drives, docked one at a
time and backed up by hand.

Baruch was Jeremiah's scribe, who wrote the burned scroll out again (Jeremiah
36).

> **Status:** design phase. This README describes the planned behaviour; the
> full specification is [`docs/spec.md`](docs/spec.md), which wins if the two
> disagree.

## What it does

You have more data than fits on one backup disk, and a USB dock that holds one
disk at a time. Baruch decides which data goes to which disk and keeps each disk
a mirror of its share. It never acts on its own: you dock a drive, run
`baruch run`, read the plan, and answer y or n.

- **Rules** say what goes where. A rule is a source folder and a placement:
  - `pin`: everything in the source goes to one named drive;
  - `spread`: each subfolder (for example each TV show) goes to exactly one
    drive from an ordered list of preferences.
- **Units** are what gets placed: each direct subfolder of a rule's source,
  plus one unit for the loose files in it. They're named `<rule>/<folder>`
  (`tv/The Wire`, `movies/Heat (1995)`) or `<rule>/(files)`.
- **Drives** are identified by a label file on the disk, never by where DSM
  mounts them. A disk without a valid label is never written to.
- **The registry** records, per unit, where it *should* be (`home`) and where
  copies *actually* exist (`copies`). Every state you see in the status is
  derived from those two.
- **Plan first:** each run shows everything it would copy, overwrite and delete,
  with warnings for large changes, and does nothing until you answer `y`.

Baruch knows nothing about TV shows, movies or disk sizes. All of that lives in
the configuration. When something needs a decision, Baruch shows what it found
and the command to act on it, and leaves the decision to you.

## Contents

- [Requirements](#requirements)
- [Getting started](#getting-started)
- [Everyday use](#everyday-use)
- [Reading the plan](#reading-the-plan)
- [Use cases](#use-cases)
- [Configuration](#configuration)
- [Command reference](#command-reference)
- [Unit states](#unit-states)
- [Files and locations](#files-and-locations)
- [Development](#development)

## Requirements

- A Synology NAS with DSM 7.2 (x86_64), with [Homebrew](https://brew.sh)
  installed.
- rsync 3.2 or later from Homebrew (installed with Baruch). DSM's own rsync
  (3.1.2) and macOS's openrsync are refused.
- Backup drives formatted as **ext4**.
- Your normal user account on the NAS; Baruch doesn't need root. `sudo` is only
  needed for a few one-off steps when preparing a drive.

All sizes are binary: a "6 TB" disk holds 5.46 TiB. In the configuration, `500G`
means 500 GiB.

## Getting started

We'll walk through an example scenario: the NAS is backed up by Hyper Backup to
a single 20 TB disk (a plain single-version mirror), and we switch over to
Baruch with three drives: the 20 TB disk and two new 6 TB disks. `BK-6A` and
`BK-6B` (6 TB each) take the TV shows, and `BK-20A` everything else. Your own
drives, names and rules will differ; the steps stay the same.
The existing 20 TB backup stays untouched until the 6 TB drives hold their
copies, so a complete backup exists the whole time.

### 1. Install

```sh
brew tap infogrind/tap
brew install baruch
```

Homebrew isn't on the `PATH` for non-interactive ssh sessions. Log in
interactively, or call `/home/linuxbrew/.linuxbrew/bin/baruch` directly.

### 2. Create the registry and a starter configuration

```sh
baruch init
```

This creates an empty registry and, since there's no configuration yet, a
commented starter at `~/.config/baruch/config.yaml`: settings filled in, no
drives or rules yet, examples in the comments.

### 3. Write the configuration

Edit `~/.config/baruch/config.yaml`: one entry per drive, and one rule per
top-level folder you want backed up:

```yaml
settings:
  exclude: ["@eaDir", "#recycle", ".DS_Store", ".TemporaryItems", ".apdisk"]
  stale_after: 30d
  warn_changed_files: 10%
  warn_changed_bytes: 10%
  warn_unit_loss: 50%

drives:
  BK-20A: { reserve: 5% }
  BK-6A:  { reserve: 10% }
  BK-6B:  { reserve: 10% }

rules:
  - name: movies
    source: /volume1/archive/movies
    pin: BK-20A

  - name: music
    source: /volume1/archive/music
    pin: BK-20A

  - name: home
    source: /volume1/homes/alex
    pin: BK-20A

  - name: tv
    source: /volume1/archive/tv
    spread: [BK-6A, BK-6B, BK-20A]   # order of preference
```

Sources must not overlap: no source may lie inside another. Baruch only looks
inside rule sources, so a folder without a rule isn't backed up.

### 4. Mark the sources and check the configuration

Each source needs a marker file. If it's missing (volume not mounted, folder
renamed), Baruch skips the rule and touches nothing on the drive for it.

```sh
touch /volume1/archive/movies/.backup-source
touch /volume1/archive/music/.backup-source
touch /volume1/homes/alex/.backup-source
touch /volume1/archive/tv/.backup-source

baruch config check
```

### 5. Prepare and register the 6 TB drives

Baruch doesn't check these one-off steps. Do them for every new disk:

1. Run an extended SMART test.
1. Format the disk as ext4 in DSM, and make sure your user has read/write access to its
   `usbshare` shared folder.
1. Turn off ext4's root reserve, which your user can't use (~280 GiB on a 6 TB
   disk): `sudo tune2fs -m 0 /dev/<partition>`.

Then dock it and register it:

```sh
baruch drive register BK-6A
```

### 6. First backups to BK-6A and BK-6B

```sh
baruch run --dry-run   # look at the plan, change nothing
baruch run             # the same plan, then y/n
```

On a new drive every unit is one summary line ("first copy: 214 files,
380 GiB"). TV shows are assigned in order of preference, largest first, and only
where they fit completely. About 9 TiB takes roughly a day, so run it in tmux.
You can stop it with Ctrl-C at any time: finished shows stay recorded, and the
next run continues.

When the run says "safe to eject in DSM", eject the drive in DSM, dock BK-6B and
run again. Shows that fit on neither drive stay *waiting* for BK-20A.

### 7. Bring over the 20 TB drive

Stop using the Hyper Backup task. Then, on the 20 TB drive:

1. `sudo chown -R $USER` on the drive (Hyper Backup's files belong to root).
1. Move the folders out of Hyper Backup's task folder to the drive's root, one
   folder per rule, with `mv` (instant, same disk): e.g. `…/archive/movies` →
   `/movies`, `…/archive/tv` → `/tv`, `…/homes/alex` → `/home`.
1. Delete what's left of the task folder.
1. `baruch drive register BK-20A`

### 8. First backup to BK-20A

```sh
baruch run
```

The existing folders appear as **untracked copies** ("existing folder, not yet
recorded"), listed file by file like a normal sync. So if an `mv` went wrong,
you see deletions and warnings before anything happens. If the old backup
didn't keep modification times, every file shows as changed: that's a full
recopy, not a loss.

TV folders of shows that now live on BK-6A or BK-6B are reported as **orphaned
data** and left alone. Once you're happy with the result:

```sh
baruch prune BK-20A    # shows its own plan, then y/n
```

## Everyday use

1. Dock a drive. Nothing happens by itself.
1. `baruch run`, ideally in tmux.
1. Read the plan, especially the summary and warnings right above the question.
   Answer `y` to carry it out, or `n` to change nothing.
1. Wait for "safe to eject in DSM", then eject the drive in DSM.
1. Now and then, `baruch status` shows what needs attention: units without a
   backup, drives not docked for longer than `stale_after`, drives growing into
   their reserve, orphaned data.

Baruch has no scheduler and sends no notifications. To be reminded, run
`baruch status` from your own scheduled task (e.g. a monthly email).

## Reading the plan

A run first lists both sides, compares files by size and modification time, and
shows the plan for the docked drive:

- **First copy** (no data on this drive yet): one line per unit with files and
  size.
- **Every other unit:** each new, changed and deleted file.
- **Untracked copy:** the unit's folder is already on the drive, but the
  registry has no record of it. It's listed file by file if this drive is the
  unit's home, and reported as orphaned otherwise.
- **Cleanups:** old copies of units that now live elsewhere, labelled with the
  reason ("moved to BK-6B, copy confirmed there on …"). They're only planned
  once the new home has a confirmed copy.
- **Skipped units:** units that don't fit, units waiting for another drive, and
  units you left out with `--skip`.
- **Scan errors:** folders on the NAS that couldn't be read. Nothing under them
  is deleted on the drive, and a unit whose folder can't be read is shown as
  *unreadable*, not gone.

At the end, right above the question, comes a summary: the totals per unit
(copy / overwrite / delete, in files and bytes), the path of the saved plan file
(open it in a second tmux pane for long plans), and the **warnings**:

- more than `warn_changed_files` of the drive's files would be overwritten or
  deleted;
- more than `warn_changed_bytes` of its bytes would be overwritten or deleted;
- a unit would lose more than `warn_unit_loss` of its bytes, which includes a
  unit whose folder is gone on the NAS.

Shares are measured against the drive's content before the run, and overwritten
files count with their full size. Cleanups of moved units don't count.

`y` carries out exactly this plan. A planned file that changes before it's
copied is copied as it is then. Files added or deleted on the NAS after the plan
was made wait for the next run.

`n` changes nothing. Find out what happened (e.g. restore wrongly deleted files
on the NAS from the backup), then run again. If one unit needs investigating but
the rest should be backed up, leave it out:

```sh
baruch run --skip "home/Photos"
```

## Use cases

### Drives growing into their reserve

Assignments never change on their own, so new seasons of a show land on the
drive that already holds it. When a drive's free space reaches its reserve, it
takes no new units. When existing units grow into the reserve, the end of each
run and `baruch status` say how much of the reserve is used ("38% of 559 GiB
used") and suggest which units to move, with the command:

```sh
baruch move set "tv/Lost" --to BK-6B
```

The unit is now *copy pending*. The next time BK-6B is docked, it gets a first
copy. The next time BK-6A is docked after that, the old copy appears in the plan
as a labelled cleanup. Copy first, delete second: if you dock BK-6A before
BK-6B, nothing is deleted.

When every drive in a spread list is full, register a new drive and add it to
`drives` and to the rule's `spread` list.

When the pinned rules outgrow their drive, change one of them from a pin to a
spread list, e.g. `pin: BK-20A` → `spread: [BK-20A, BK-X]`. Its units stay on
BK-20A, and new ones can go to BK-X.

### New units waiting for another drive

A new show is placed only on the docked drive, and only if every drive higher in
its spread list is full for it. Otherwise it waits for the preferred drive. The
plan and the status show "N new units waiting for BK-6A (last docked …)", with
the `move set` command that places them on the docked drive instead.

### A unit that fits nowhere

A unit is never split. If it fits on no drive in its list, it's reported and has
no backup until you add a drive or free up space.

### Adding a folder to the backup

Add a rule to the configuration, put a `.backup-source` marker into its source,
and run `baruch config check`. Its units are placed on the next runs.

### Changing the configuration

You edit the configuration by hand; apart from the starter that `baruch init`
writes, Baruch never changes it. Changes take effect
on the next run, by the normal rules:

| Change | Effect |
| --- | --- |
| Pin moved to another drive | The units get the new home; their old copies are cleaned up once the new home has a confirmed copy |
| Drive removed from a rule | That rule's units on it are placed again, copy first |
| New global exclude | Matching files are deleted from the drives in the next normal syncs, and count towards the warnings |
| Rule removed or renamed | Its data on the drives becomes orphaned (a renamed rule is copied again under its new name); remove it with `baruch prune` |

### Renamed folders

A renamed show or movie folder is a removed unit plus a new one. The old copy is
deleted with a warning, and the new folder is copied again. If the drive holding
the old copy is docked before the new folder has a backup, there's a gap. The
warning shows it, and you can answer `n` or use `--skip`.

### A drive breaks

Baruch can't tell "broken" from "in a drawer", so it never gives up on a drive
by itself.

1. You notice: a run stops with read or write errors, the drive isn't recognised
   any more, or the status shows it hasn't been docked for a long time.
1. `baruch drive forget BK-6A`. Its copies are dropped from the registry and its
   label becomes invalid. The status lists what now exists only on the NAS
   ("61 shows, 4.5 TiB have no backup").
1. Either **replace it:** prepare a new disk and `baruch drive register BK-6A`.
   It takes over the name, so the configuration stays the same, and all of
   BK-6A's units appear as first copies. About 4.5 TiB takes 9–14 hours.
1. Or **do without it:** remove BK-6A from the rules and from `drives`. Its
   units are placed on other drives with room as those are docked.

If the old disk turns up again, its label is invalid and it's treated as an
unknown disk. To reuse it: `baruch drive register NAME --force`.

A drive that fails slowly (it mounts, but some files can't be read) is found by
`baruch verify`.

### Upgrading a drive to a larger disk

Give the new disk a new name, e.g. BK-6A (6 TB) → BK-12A (12 TB):

1. Prepare the new disk and `baruch drive register BK-12A`.
1. In the configuration, put BK-12A wherever BK-6A was (same pins, same place in
   each spread list), add it to `drives`, and remove BK-6A from the rules.
1. BK-6A's units are placed again. BK-12A has BK-6A's place in the preference
   order, so they wait for it and are copied there from the NAS.
1. Until then, BK-6A remains a valid backup. Dock it afterwards to clean up its
   old copies. Or, once BK-12A has had a complete run,
   `baruch drive forget BK-6A`, remove it from `drives`, and wipe or keep the
   disk.

### Taking a drive out of service

Remove it from all rules and from `drives`. Its units are placed elsewhere, copy
first. Its old copies are cleaned up when you dock it after each unit has a
confirmed copy on its new home. If you won't dock it again,
`baruch drive forget` it. A drive that's no longer configured and holds no
copies disappears from the registry.

### Restoring files

The drives hold plain files in one folder per rule, so restoring doesn't need
Baruch:

```sh
baruch find "S01E01"     # which drives hold matching files, and how current
```

Dock that drive and copy the files back with Homebrew's rsync:

```sh
rsync -a "/volumeUSB1/usbshare/tv/The Wire/" "/volume1/archive/tv/The Wire/"
```

`find` searches the contents lists that every run keeps on the NAS. Files that
were only on a forgotten drive are reported as on no drive.

### The NAS is lost

1. Set up the NAS again: DSM, shared folders, the user account, Homebrew and
   rsync.

1. Dock the drive that holds the `home` rule first and restore it (below).
   That also brings back Baruch's configuration and registry, which live in the
   home folder.

1. Copy back each drive with the restore script that every run writes to it:

   ```sh
   sh /volumeUSB1/usbshare/.baruch/restore.sh         # shows what it would copy
   sh /volumeUSB1/usbshare/.baruch/restore.sh --run   # copies
   ```

   It needs only rsync, not Baruch: one `rsync -a` per rule folder on the
   drive, back to the rule's source as configured at the last run. The rule
   holding Baruch's own files goes first. It never deletes anything on the
   NAS, and works wherever the drive is mounted. If the NAS has different paths
   now, edit the targets in the script first.

1. Without that drive: every drive carries `/.baruch/` with a registry snapshot
   and a copy of the configuration. They show its rules, the units it holds,
   and which drives hold the rest.

All ~17 TiB take about 33–52 hours of copying. The `.backup-source` markers come
back with the files, and `rsync -a` keeps modification times, so the next
`baruch run` is incremental.

### Verifying a drive

```sh
baruch verify
```

This reads every file of the docked drive's units, compares checksums with the
NAS, and reports three kinds of mismatch:

- **Read errors on the drive:** here Baruch offers its only fix, "copy these N
  unreadable files again from the NAS? y/n". Many errors are a sign to replace
  the drive.
- **Changed on the NAS since the last sync:** normal; the next run takes care of
  them.
- **Same size and time, different content:** Baruch can't tell which side is
  right. It lists both paths and the commands to copy in either direction, and
  you decide each one.

A full verify reads the whole drive: ~16–25 hours for 8.3 TiB. The status shows
each drive's last verify.

### Lost or damaged registry

If the registry is missing or unreadable, Baruch refuses to do anything. Dock
any drive and run `baruch registry restore`, which takes that drive's snapshot.
Copies on other drives are marked *unverified* until each is docked and its
contents confirm them. Nothing is deleted on the strength of an unverified copy.

## Configuration

`~/.config/baruch/config.yaml` has three sections:

| Key | Section | Meaning |
| --- | --- | --- |
| `exclude` | `settings` | Names or `*` patterns (`@eaDir`, `*.tmp`), matched against every file and folder name at any depth, in all rules. There are no per-rule excludes. |
| `stale_after` | `settings` | `status` flags drives not docked for longer than this (e.g. `30d`) |
| `warn_changed_files` | `settings` | Warning if more than this share of the drive's files would be overwritten or deleted |
| `warn_changed_bytes` | `settings` | The same for bytes |
| `warn_unit_loss` | `settings` | Warning if a unit would lose more than this share of its bytes |
| `reserve` | `drives` | Free space at which the drive takes no new units: a share (`10%`) or a size (`500G`) |
| `name` | `rules` | Rule name, also its folder on each drive. Only `A–Z a–z 0–9 _ -`. |
| `source` | `rules` | Source folder; must contain `.backup-source` |
| `pin` | `rules` | One drive for all units |
| `spread` | `rules` | Ordered list of drives; each unit goes to exactly one |

A registered drive that isn't listed under `drives` takes no units.

## Command reference

`run`, `prune`, `drive forget` and `drive register --force` show what they'll
do and ask y/n first. There's no `--yes`, and `run` refuses to run without a
terminal. Exit code 0 means success (also with warnings); anything else means an
error.

### `baruch run`

Backs up the docked drive: computes the plan, shows it, asks y/n, and carries it
out.

| Option | Meaning |
| --- | --- |
| `--dry-run` | Show the plan and exit. Writes nothing at all. |
| `--drive NAME` | Use this drive when several registered drives are docked (otherwise Baruch asks) |
| `--skip UNIT` | Leave a unit out of this run; it stays *outdated*. Repeatable. |

Without a usable drive, `run` lists the mounted USB volumes and why each isn't
used. Before each unit it checks that the drive is still mounted and its label
still matches, and stops otherwise. Ctrl-C stops the run; finished units stay
recorded. Each real run writes a log with the full plan, kept for 90 days.

### `baruch status`

Shows, per drive: last complete run, last seen, oldest unit sync, capacity, free
space, reserve usage and last verify. Also the total free space above all
reserves. It lists what needs attention:

- units that are waiting, copy pending, outdated, incomplete, synced with
  errors, cleanup pending, gone, or without any backup;
- move suggestions;
- orphaned data;
- drives no longer configured that still hold copies;
- drives not docked for longer than `stale_after`.

It uses the last run's scan of the NAS and shows how old that is. It works
while a run is in progress.

### `baruch find PATTERN`

Searches the contents lists on the NAS and shows which drives hold matching
files and how current each copy is. Works while a run is in progress.

### `baruch move set UNIT --to DRIVE`

Gives a unit of a spread rule a new home. The new drive gets a copy when it's
docked, and the old copy is deleted when the old drive is docked after that.
Refuses pinned units (change the pin instead), drives that aren't registered,
and drives not in the unit's spread list. Warns if the unit doesn't fit.

If the name you type doesn't match exactly (e.g. a different Unicode form of an
umlaut), Baruch shows similar units to pick from.

### `baruch drive register NAME`

Writes a label with the name and a new random disk ID to the docked disk, and
records its free space. Refuses disks that aren't ext4. Warns if the name isn't
in the configuration yet. A name whose disk was forgotten can be registered
again on a new disk; a name whose disk is still known can't be taken over.

| Option | Meaning |
| --- | --- |
| `--force` | Overwrite an unknown or invalid label, after showing it. Never overwrites the valid label of a registered drive. Old data on the disk is left for you to delete. |

### `baruch drive forget NAME`

Tells Baruch that the copies on this drive no longer count: it broke, got lost,
or you just don't want to use it any more. Removes its copies from the registry,
invalidates its label, and lists the units that now have no backup. It doesn't
change the configuration: units whose home is this drive stay *copy pending*
until a new disk is registered under the name, or until you remove the drive
from the configuration.

### `baruch prune DRIVE`

Deletes orphaned data on the docked drive: data that no current unit explains
(a removed or renamed rule, leftover folders of units that live elsewhere).
Shows its own plan with warnings and asks y/n. Keeps an untracked copy as long
as the unit's home has no confirmed copy, and says why. Never touches
`/.baruch/`, `lost+found`, or anything the global excludes match.

### `baruch verify`

Compares the docked drive's units with the NAS by checksum. See
[Verifying a drive](#verifying-a-drive).

### `baruch config check`

Checks the configuration without a drive: sources exist and have markers, no
source lies inside another, and every drive named in a rule is listed under
`drives`.

### `baruch init`

Creates an empty registry, needed once before the first run. If there's no
configuration yet, it also writes a commented starter to
`~/.config/baruch/config.yaml`; an existing configuration is kept as it is.
Refuses if a registry already exists.

### `baruch registry restore`

Replaces the registry with the snapshot on the docked drive. See
[Lost or damaged registry](#lost-or-damaged-registry).

## Unit states

Each unit is in exactly one state, checked in this order:

| State | Meaning | What happens next |
| --- | --- | --- |
| Gone | Its folder is missing on the NAS | Its copies are deleted, with a warning, when their drives are docked |
| Waiting | No home yet | Placed when a suitable drive is docked |
| Copy pending | Its home drive has no copy | Copied when the home drive is docked |
| Outdated | The last sync on its home was skipped or interrupted | Synced on the next run, once it fits |
| Cleanup pending | Old copies on other drives | Deleted when those drives are docked, once the home copy is confirmed |
| In place | Up to date as of the last run | Normal sync |

"Synced with errors" is a note on top of these: some files on the NAS couldn't
be read, and they're listed. A copy counts as **confirmed** only if its last
sync finished without errors.

## Files and locations

| Where | What |
| --- | --- |
| `~/.config/baruch/config.yaml` | Configuration, edited by hand (`baruch init` writes a starter) |
| `~/.local/state/baruch/` | Registry and its last 10 versions, contents lists, saved plans and logs |
| `/<rule>/…` on each drive | The backed-up files, as plain files |
| `/.baruch/` on each drive | Label, registry snapshot, copy of the configuration, contents list, `restore.sh`, partial files |

Both NAS locations are inside your home folder, so the `home` rule backs them
up too.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
```

Tests run on the Mac and on Linux in GitHub Actions. They use synthetic data
generated per test, real rsync, and fake drives in temp folders. See
[`docs/spec.md`](docs/spec.md#testing) for details.

To try a development version on the NAS:

```sh
brew install uv
uv tool install --force git+https://github.com/infogrind/baruch@<branch>
```
