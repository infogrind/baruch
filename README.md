# Baruch

Rule-based backup of a Synology NAS to several external drives, docked one at a
time and backed up by hand.

Baruch was Jeremiah's scribe, who wrote the burned scroll out again (Jeremiah
36).

> **Status:** v0.2.0, not released yet; it hasn't been used on the real NAS
> yet. The full specification is [`docs/spec.md`](docs/spec.md), which wins if
> the two disagree.

## What it does

You have more data than fits on one backup disk, and a USB dock that holds one
disk at a time. Baruch decides which data goes to which disk and keeps each disk
a mirror of its share. It never acts on its own: you dock a drive, run
`baruch run`, read the plan, and answer y or n.

- **Rules** say what goes where. A rule is a source folder and an ordered list
  of drives. Each subfolder (for example each TV show) goes to one drive of the
  list: one that already holds its data, otherwise the first one with room. A
  list with one drive sends everything there.
- **Units** are what gets placed: each direct subfolder of a rule's source,
  plus one unit for the loose files in it. They're named `<rule>/<folder>`
  (`tv/The Wire`, `movies/Heat (1995)`) or `<rule>/(files)`.
- **Drives** are identified by a label file on the disk, never by where DSM
  mounts them. A disk without a valid label is never written to, except by
  `baruch drive register`.
- **The drives are the truth.** Each drive records, per unit, when it was last
  synced there and whether that sync finished. The NAS keeps where each unit
  *should* go (its **home**) and a cache of each drive's records as last seen.
- **Plan first:** each run shows everything it would copy, overwrite and delete,
  lists every deletion, warns about large changes, and does nothing until you
  answer `y`.
- **Runs only touch a drive's own units, and never delete a whole unit.** Data
  that doesn't belong on a drive any more (after a move, a config change, or a
  folder deleted on the NAS) is *stale data*, and only `baruch prune` removes
  it, once its home has a newer copy synced without errors, or once the unit is gone on the
  NAS. Data no rule explains is never deleted by Baruch at all.

Baruch knows nothing about TV shows, movies or disk sizes. All of that lives in
the configuration. When something needs a decision, Baruch shows what it found
and the command to act on it, and leaves the decision to you.

## Contents

- [Requirements](#requirements)
- [Getting started](#getting-started)
- [Starting from an existing backup](#starting-from-an-existing-backup)
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
data, so a complete backup exists the whole time.

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
top-level folder you want backed up (a second rule for the same folder makes a
second copy on other drives, see
[A second copy of a folder](#a-second-copy-of-a-folder)):

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
    drives: [BK-20A]

  - name: music
    source: /volume1/archive/music
    drives: [BK-20A]

  - name: home
    source: /volume1/homes/alex
    drives: [BK-20A]

  - name: tv
    source: /volume1/archive/tv
    drives: [BK-6A, BK-6B, BK-20A]   # order of preference
```

Sources must not overlap: no source may lie inside another. Two rules may
have the same source, for a second copy on other drives (see
[A second copy of a folder](#a-second-copy-of-a-folder)). Baruch only looks
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

On a new drive every unit is one summary line ("first sync: 214 files,
380 GiB"). TV shows get their home in order of preference, largest first, and
only where they fit completely. About 9 TiB takes roughly a day, so run it in
tmux. You can stop it with Ctrl-C at any time: finished shows stay recorded,
and the next run continues.

When the run says "safe to eject in DSM", eject the drive in DSM, dock BK-6B and
run again. BK-20A isn't registered yet, so the other rules and the shows that
fit on neither drive stay *unplaced* for now.

### 7. Bring over the 20 TB drive

Stop using the Hyper Backup task. The 20 TB drive already holds a plain copy of
everything, so it's taken over as it is, without copying it again: follow
[Starting from an existing backup](#starting-from-an-existing-backup) and
register it as `BK-20A`. The folders in Hyper Backup's task folder move to the
drive's root (`…/archive/movies` → `/movies`, `…/archive/tv` → `/tv`,
`…/homes/alex` → `/home`).

The units still unplaced from step 6 get BK-20A as home, since their data is
already there. TV folders of shows whose home is BK-6A or BK-6B are stale data;
`baruch prune BK-20A` removes them at the end.

## Starting from an existing backup

If a disk already holds a copy of your data (from Hyper Backup, an earlier
rsync, or a copy by hand), Baruch takes it over without copying it all again.

**Prerequisite:** below each source, the copy has the same folders as on the
NAS. For example, the copy of `/volume1/archive/tv/The Wire/S01/` is at some
`…/tv/The Wire/S01/` on the disk. Where on the disk it is doesn't matter; you'll
move it. The disk must be ext4.

1. **Stop the old backup** for good, so nothing else writes to the disk.

1. **Make the files yours** (backup tools often write as root):

   ```sh
   sudo chown -R $USER /volumeUSB1/usbshare
   ```

1. **Write the rules:** one rule per source whose copy is on the disk, with its
   `.backup-source` marker, the drive under `drives`, and the drive in the list
   of each of these rules. Run `baruch config check`.

   The rule's **name** is what links the NAS to the disk: rule `home` with
   source `/volume1/homes/alex` lives in `/home` on every drive, and unit
   `home/Documents` is `/volume1/homes/alex/Documents` on the NAS and
   `/home/Documents` on the disk.

1. **Move each source's copy to the disk's root**, named exactly like its rule
   (not like the source folder). `mv` on the same disk is instant:

   ```sh
   cd /volumeUSB1/usbshare
   mv "Main Backup/archive/tv" tv
   mv "Main Backup/archive/movies" movies
   mv "Main Backup/homes/alex" home       # rule home, not "alex"
   ```

   `ls` should now show one folder per rule (plus `lost+found`). Delete what's
   left of the old backup's own folders. If you leave them, Baruch never
   deletes them; `baruch status` lists them as unexplained data, and
   `baruch prune` with the drive docked shows the `rm` command.

   Baruch finds existing data only by these names. A copy under another name
   isn't recognised: its units get a full first sync, and the misnamed folder
   is unexplained data. The plan shows this as first syncs instead of "existing
   folder", so answer `n`, fix the name and look again.

1. **Register it:** optionally turn off the root reserve
   (`sudo tune2fs -m 0 /dev/<partition>`), then

   ```sh
   baruch drive register BK-20A
   ```

   This writes the label, scans what's on the disk and records its free space.
   It changes none of your data.

1. **Look at the plan:** `baruch run --dry-run`. A unit whose data is on this
   disk gets it as its home, even if a drive earlier in its list has room. Its
   folder shows as "existing folder, no record yet", with only the real
   differences listed file by file. Check the deletions above the question:

   - a few files: things deleted on the NAS since the old backup, as expected;
   - many deletions in one unit and a first sync of another: a folder ended
     up at the wrong path (inside the first unit). Fix it with `mv` and look
     again;
   - every file changed: the old backup didn't keep modification times. That's
     a full recopy, not a loss.

1. **Run it:** `baruch run`, then `y`. Each unit gets its record on the disk
   once its sync finishes, and the run ends by writing the contents list, a copy
   of the configuration and `restore.sh`. From then on it's a drive like any
   other.

1. **Prune:** folders of units whose home is another drive are stale data:
   `baruch prune BK-20A` removes them after its own plan and y/n. Folders no
   rule explains are listed by `baruch status`; `prune` with the drive docked
   shows the `rm` command, for you
   to delete by hand.

## Everyday use

1. Dock a drive. Nothing happens by itself.
1. `baruch run`, ideally in tmux.
1. Read the plan, especially the deletions and warnings right above the
   question. Answer `y` to carry it out, or `n` to change nothing.
1. Wait for "safe to eject in DSM", then eject the drive in DSM.
1. Now and then, `baruch status` shows what needs attention: units without a
   backup, drives not docked for longer than `stale_after`, drives growing into
   their reserve, stale data to prune, units gone on the NAS.

Baruch has no scheduler and sends no notifications. To be reminded, run
`baruch status` from your own scheduled task (e.g. a monthly email).

## Reading the plan

A run first lists both sides, compares files by size and modification time, and
shows the plan for the docked drive. It only covers units whose home is this
drive:

- **First sync** (no data on this drive yet): one line per unit with files and
  size.
- **Every other unit:** each new, changed and deleted file. Data that's on the
  drive without a record (after the switch-over or `register --force`) is
  labelled "existing folder, no record yet" and listed the same way.
- **Registry changes:** units seen for the first time and units placed again
  after a config change, with the drive they go to; units dropped because
  their rule was removed or their folder is now excluded; entries of gone units
  that no drive holds data of any more. They need your `y`
  too, even when their home is another drive.
- **Skipped units:** units that don't fit, and units you left out with `--skip`.
- **Scan errors:** folders or files on the NAS that couldn't be read. Nothing
  under them is deleted on the drive, and a unit whose folder can't be read is
  shown as *unreadable*, never gone.
- **Read errors on the drive:** folders on the drive that can't be read are
  reported and left alone; the run then exits non-zero.

Only a run with neither file operations nor registry changes skips the
question.

At the end, right above the question, comes a summary: the totals per unit
(copy / overwrite / delete, in files and bytes), the path of the plan file,
which is also the run's log (open it in a second tmux pane for long plans), how
many units are gone on the NAS while a drive still holds their data ("400
units of rule tv gone on the NAS: nothing is deleted"), every deletion, and the warnings. A sudden
number of gone units, together with as many new units, usually means folders
were renamed, or worse: answer `n` and look.

**Deletions** are all listed, however small, grouped by unit:

```text
Deletions on BK-6B (15 files, 3 units):
  tv/The Wire
    - S01/S01E01.720p.mkv              replaced in the same folder
  home/Documents
    - tax-2025.pdf
  home/Photos
    13 excluded files                  matched by a new exclude; listed in the plan file
```

A file deleted in a folder that gets a new file in the same run (a quality
upgrade, for example) is marked "replaced in the same folder". Files deleted
because an exclude now matches them are one line per unit. After 50 lines, the
rest is counted, and the plan file has them all. A run never deletes a whole
unit: a show deleted on the NAS stays on the drive until you `prune` it.

The **warnings** come last:

- more than `warn_changed_files` of the drive's files would be overwritten or
  deleted;
- more than `warn_changed_bytes` of its bytes would be overwritten or deleted;
- a unit would lose more than `warn_unit_loss` of its bytes to overwrites and
  deletions. This catches files that were encrypted or garbled in place
  (same names, new content), not only deletions.

Shares are measured against the drive's content before the run, and overwritten
files count with their full size. Baruch's own state folder (`~/.local/state/baruch/`,
part of your `home` rule, and of any second rule for the same folder) changes
during every run, so its changes are one line in the plan and don't count
towards the warnings.

`y` carries out exactly this plan. A planned file that changes before it's
copied is copied as it is then. Files added or deleted on the NAS after the plan
was made wait for the next run.

`n` changes nothing (it only refreshes the NAS's cache of what's on the
drive). Find out what happened (e.g. restore wrongly deleted files
on the NAS from the backup), then run again. If one unit needs investigating but
the rest should be backed up, leave it out:

```sh
baruch run --skip "home/Photos"
```

A skipped unit is left exactly as it is on the drive. Unit names you give to `--skip` must exist; a typo is refused,
with similar names suggested.

The run ends with a report: units it left out and why, reserve usage, the
drive's stale data ("3 units, 1.1 TiB can be pruned: `baruch prune BK-6A`"),
units gone on the NAS, and unexplained data.

## Use cases

### Drives growing into their reserve

Homes never change on their own, so new seasons of a show land on the drive
that already holds it. When a drive's free space reaches its reserve, it takes
no new units. When existing units grow into the reserve, the end of each run and
`baruch status` show how much of the reserve is used, the drive's largest
units, and the room on the other drives of their rules:

```text
BK-6A: existing units have grown into the reserve: 212 GiB of 559 GiB used.
  Largest units on BK-6A:
    tv/The Wire     410 GiB   changed 2026-11-02
    tv/Lost         380 GiB   unchanged since 2025-03
  Room above reserve in rule tv: BK-6B 340 GiB, BK-20A 1.2 TiB
  To move one: baruch move set UNIT --to DRIVE
```

Pick one and move it:

```sh
baruch move set "tv/Lost" --to BK-6B
```

The unit is now *copy pending*. The next time BK-6B is docked, it gets a first
sync. After that, its old data on BK-6A is stale. Next time BK-6A is docked,
`baruch prune BK-6A` deletes it, then `baruch run` uses the freed space. Copy
first, delete second: if you dock BK-6A before BK-6B, `prune` keeps the data
and says why ("home BK-6B has no copy yet").

When every drive in a list is full, register a new drive and add it to `drives`
and to the list. The same works for a rule with a single drive:
`drives: [BK-20A]` → `drives: [BK-20A, BK-X]` keeps its units on BK-20A, and new
ones can go to BK-X.

### New units on a drive that's rarely docked

A new unit gets its home right away, the first drive in its list with room, even
if that drive isn't docked. If that drive hasn't been docked for longer than
`stale_after`, the plan and the status say so ("12 units copy pending on BK-6A,
last docked …"), with the `move set` command that gives them the docked drive
instead.

### A unit that fits nowhere

A unit is never split. If it fits on no drive of its list, it's *unplaced*:
reported in every plan and in the status, and placed once a drive has room
(after a prune, or once you add a drive). `baruch move set` can also give it a
home by hand.

### Adding a folder to the backup

Add a rule to the configuration, put a `.backup-source` marker into its source,
and run `baruch config check`. Its units get their homes on the next run.

### A second copy of a folder

To keep a folder on two drives, give it a second rule with the same source and
a drive list that has no drive in common with the first:

```yaml
  - name: home
    source: /volume1/homes/alex
    drives: [BK-20A]

  - name: home-1t
    source: /volume1/homes/alex
    drives: [BK-1T]
```

The two rules are independent: each has its own folder on its drives
(`/home`, `/home-1t`), its own units and homes, and each copy is as current as
its drive's last run. They share the source's marker. `config check` refuses
two rules with the same source and a drive in common, since two copies on one
disk protect against nothing. Keep the existing rule's name when you add the
second one: a renamed rule is a new rule, copied again from scratch.

### Changing the configuration

You edit the configuration by hand; apart from the starter that `baruch init`
writes, Baruch never changes it. Changes take effect on the next run. A run
never deletes a unit's data because of a config change: what no longer belongs
on a drive becomes stale data, for `prune`, or unexplained data, for you. The
one exception is a new exclude, whose matching files are deleted inside units
(listed with the deletions).

| Change | Effect |
| --- | --- |
| Drive removed from a rule's list (or a single drive replaced by another) | Its units of that rule get a new home, preferring a listed drive that already holds their data. The old data becomes stale |
| Drive added to a list, or list reordered | Nothing changes for units that have a home; new units follow the new list |
| Source changed | Units are matched by folder name: those missing in the new source are gone, new ones are placed |
| New global exclude | Matching files are deleted from the drives in the next normal syncs, one line per unit with the deletions. A unit folder it matches stops being a unit, with a warning that lists where its data is on each drive (kept in the run's log). Baruch ignores excluded names on the drives from then on, so delete that data by hand |
| Rule removed | A warning in the plan ("rule tv removed: 412 units dropped"). Its data becomes unexplained data and is never deleted by Baruch. If you add the rule back, its units go back to where their data is |
| Rule renamed (or a typo in its name) | Both a removed rule and a new one: the data is copied again under the new name. The old folder is unexplained data: delete it by hand (`prune` shows the `rm` command with the drive docked) once the new rule has been backed up |

### Renamed folders

A renamed show or movie folder is a gone unit plus a new one. The new folder is
copied again; the old data stays on its drive, shown as gone, until you
`prune` it. So there's no moment without a backup, but the space is used twice
until then. The same goes for a folder deleted on the NAS: its backup stays
until you prune, and `prune` shows how many of its files exist on no other
drive.

### A drive breaks

Baruch can't tell "broken" from "in a drawer", so it never gives up on a drive
by itself.

1. You notice: a run stops with read or write errors, the drive isn't recognised
   any more, or the status shows it hasn't been docked for a long time.
1. `baruch drive forget BK-6A`. It first shows what would then exist only on the
   NAS ("61 shows, 4.5 TiB have no backup") and asks y/n. Then Baruch drops
   the drive's entry and its cache, and its label becomes invalid.
1. Either **replace it:** prepare a new disk and `baruch drive register BK-6A`.
   It takes over the name, so the configuration stays the same, and all of
   BK-6A's units appear as first syncs. About 4.5 TiB takes 9–14 hours.
1. Or **do without it:** remove BK-6A from the rules and from `drives`. Its
   units get homes on other drives with room. A rule whose only drive was BK-6A
   needs another one.

If the old disk turns up again, its label is invalid and it's treated as an
unknown disk. To reuse it: `baruch drive register NAME --force`.

A drive that fails slowly (it mounts, but some files can't be read) is found by
`baruch verify`.

### Upgrading a drive to a larger disk

Give the new disk a new name, e.g. BK-6A (6 TB) → BK-12A (12 TB):

1. Prepare the new disk and `baruch drive register BK-12A`.
1. In the configuration, put BK-12A wherever BK-6A was (same place in each
   list), add it to `drives`, and remove BK-6A from the rules.
1. BK-6A's units get BK-12A as home (it has BK-6A's place in the preference
   order) and are copied there from the NAS.
1. Until then, BK-6A remains a valid backup. Once BK-12A has had a complete
   run, dock BK-6A and `prune` it. Or `baruch drive forget BK-6A` and remove it
   from `drives`, but only once `forget`'s preview shows no unit that would be
   left without a backup. Then wipe or keep the disk.

### Taking a drive out of service

Remove it from all rules and from `drives`. Its units get homes elsewhere, and
its data becomes stale. Dock it once their new homes have complete copies and
`prune` it. If you won't dock it again, `baruch drive forget` it. A drive that's
no longer configured stays in the registry, listed by the status while it holds
data, until you forget it.

### Restoring files

The drives hold plain files in one folder per rule, so restoring doesn't need
Baruch:

```sh
baruch find "S01E01"     # which drives hold matching files, and how current
```

```text
tv/The Wire/S01/S01E01.mkv  1.4 GiB
  BK-6B   home    synced 2026-11-20
  BK-6A   stale   synced 2026-11-02
```

Dock that drive and copy the files back with Homebrew's rsync:

```sh
rsync -aH "/volumeUSB1/usbshare/tv/The Wire/" "/volume1/archive/tv/The Wire/"
```

`find` searches the cache of each drive's contents that the NAS keeps. Files
that were only on a forgotten drive are reported as on no drive.

### The NAS is lost

1. Set up the NAS again: DSM, shared folders, the user account, Homebrew and
   rsync. Restore onto empty or freshly created shares: delete damaged data
   (for example files encrypted by ransomware) first, because the restore keeps
   files on the NAS that look newer than the drive's.

1. Dock the drive that holds the `home` rule first and restore it (below).
   That also brings back Baruch's configuration and state, which live in the
   home folder, as of that drive's last run. Don't run `baruch init` before
   this: the restore keeps newer files on the NAS, so new empty ones would win.

1. Copy back each drive with the restore script that every run writes to it:

   ```sh
   sh /volumeUSB1/usbshare/.baruch/restore.sh         # lists what it would copy
   sh /volumeUSB1/usbshare/.baruch/restore.sh --run   # copies
   ```

   It needs only rsync, not Baruch. It copies back the complete units whose
   home is this drive. It checks the
   drive's records when it runs, and prints each unit with the time it was
   synced. Units go to the rule's source as configured when the script
   was written; the rule holding Baruch's own files goes
   first. Everything else on the drive is only listed, with the command to
   restore it by hand if you need it: units whose last sync didn't finish (an
   older complete copy may be on another drive), units for the second pass
   (below), units gone on the NAS, older
   copies whose home has a newer copy synced without errors, home units with data
   but no record yet (they may be the only copy), folders no rule explains, and
   anything added after the script was written.
   It never deletes anything on the NAS. It keeps files there that are newer
   than the drive's, but lists every one it skips; if those are damaged, run it
   again with `--run --overwrite`. It works wherever the drive is mounted. If
   the NAS has different paths now, edit the targets in the script first. It
   uses Homebrew's rsync if it's installed. A missing rule folder (e.g.
   `/volume1/archive/tv`) is created if its share exists; otherwise the rule is
   skipped and reported. A unit that can't be restored (rsync fails, or its
   rule is skipped) is marked `!!` and counted at the end, and the script exits
   with status 1: fix the cause and run it again. Status 0 means everything it
   set out to copy was copied.

   For every unit a script lists as incomplete, restore it by hand from the
   drive with the newest complete copy (its `/.baruch/units.yaml`, or
   `baruch find` once Baruch's state is back) before you re-create the markers.

1. Once every drive's home units are back, run each script a second time:

   ```sh
   sh /volumeUSB1/usbshare/.baruch/restore.sh --run --stale
   ```

   This restores the other units on the drive that `prune` would have kept (no
   home, or a home without a newer copy synced without errors), since their
   data may be the newest or the only copy, e.g. a show moved to a drive that
   never got it. Where a newer copy is already back, it only adds files that
   are missing, which can include files deleted since; the script names each
   unit's home drive so you can check.

   A folder backed up by two rules (see
   [A second copy of a folder](#a-second-copy-of-a-folder)) is restored by
   both drives' scripts, in either order. Per file, the newer one wins, and
   files only the older copy holds come back too (which can include files
   deleted since); the script you run second lists every file it skips. To
   get exactly the newer copy, restore that folder only from its drive; the
   other script without `--run` shows what it would add.

1. Without that drive: every drive carries `/.baruch/` with its records (when
   each unit was synced) and a copy of the configuration.

1. Once **every** drive is restored, re-create the `.backup-source` markers;
   the scripts print the `touch` commands. They don't restore the markers on
   purpose: until they're back, Baruch skips those rules, so a half-restored
   NAS never looks like a NAS whose shows were deleted.

1. Dock every drive for `baruch run` and answer `n` if it asks, before any `y`
   or `prune`. See [Losing Baruch's state](#losing-baruchs-state) for why.

All ~17 TiB take about 33–52 hours of copying. `rsync -aH` keeps modification
times, so the next `baruch run` is incremental.

### Verifying a drive

```sh
baruch verify
```

This reads every file of the docked drive's home units, compares checksums with the
NAS, and reports three kinds of mismatch:

- **Read errors on the drive**, including files or folders it can't open: here
  Baruch offers its only fix, "copy these N unreadable files again from the
  NAS? y/n". This doesn't count as a new sync: the unit keeps its last sync
  time, since files changed on the NAS aren't copied. The count is kept, and `baruch status` then recommends replacing
  the drive.
- **Changed on the NAS since the last sync:** normal; the next run takes care of
  them.
- **Same size and time, different content:** Baruch can't tell which side is
  right. It lists both paths and the commands to copy in either direction
  (quoted, safe to paste), and you decide each one.

A full verify reads all of the drive's home units: ~16–25 hours for 8.3 TiB. The status shows
each drive's last verify.

### Losing Baruch's state

If the registry is missing or unreadable, Baruch refuses to do anything. Copy
back one of its saved versions (`~/.local/state/baruch/`); they're usually
newer than any backup. Otherwise restore just Baruch's folders from the drive
holding `home` (if a second rule backs up the same folder, e.g. `home-1t`,
use whichever of the two drives ran last; there the paths start with
`/home-1t/`), not the whole rule, and not with `--overwrite`, which would
also overwrite your newer documents:

```sh
rsync -aH /volumeUSB1/usbshare/home/.local/state/baruch/ ~/.local/state/baruch/
rsync -aH /volumeUSB1/usbshare/home/.config/baruch/ ~/.config/baruch/
```

Either way you get an **older** state: as old as the saved version, or as the
last run of the drive you took it from, which can be weeks. Your data on the
drives isn't affected, and each drive's own records replace the NAS's cache
when it's next docked. But until then the cache may describe a disk you've
replaced since, and `prune` would trust it. So:

- **After restoring an older state, dock every drive for `baruch run` and
  answer `n` if it asks, before any `y` or `prune`.** A drive that can't be docked any more
  (broken, given away, relabelled): `baruch drive forget` it first.
- A disk you registered after that state under a name the state knows with
  another disk shows as not matching. Run `baruch drive forget NAME`, then
  `baruch drive register NAME`, which takes the disk over as it is. A disk
  registered under a new name is simply taken over with `register`.
- Moves, registrations, forgotten drives and verify results since that state
  are missing; redo the moves and forgets.

If all of Baruch's state is gone: `baruch init`, then dock each drive and
`baruch drive register NAME` with its existing name, which takes the disk over
as it is. Do this for **every** drive before the first `baruch run`: the next
runs give every unit its home back where its data is, but only on drives
Baruch knows. If a plan shows first syncs for units you know are on another
drive, answer `n` and take that drive over first. The list of forgotten disks
is gone too: wipe a forgotten disk that turns up, or `register --force` it,
before it's taken over by mistake. The takeover prompt shows each disk's ID and
its newest sync, so you can tell an old disk from its replacement.

Don't clone a drive (with `dd` or similar): two disks with the same label can't
be told apart.

## Configuration

`~/.config/baruch/config.yaml` has three sections:

| Key | Section | Meaning |
| --- | --- | --- |
| `exclude` | `settings` | Names or `*` patterns (`@eaDir`, `*.tmp`), matched against every file and folder name at any depth, in all rules. There are no per-rule excludes. Matching files inside a unit are deleted from its drive by its next sync. |
| `stale_after` | `settings` | `status` flags drives not docked for longer than this (e.g. `30d`), and units copy pending on them |
| `warn_changed_files` | `settings` | Warning if more than this share of the drive's files would be overwritten or deleted |
| `warn_changed_bytes` | `settings` | The same for bytes |
| `warn_unit_loss` | `settings` | Warning if a unit would lose more than this share of its bytes |
| `reserve` | `drives` | Free space at which the drive takes no new units: a share (`10%`) or a size (`500G`) |
| `name` | `rules` | Rule name, also its folder on each drive. Only `A–Z a–z 0–9 _ -`. |
| `source` | `rules` | Source folder; must contain `.backup-source` |
| `drives` | `rules` | Ordered list of drives, most preferred first; each unit goes to one of them. Not empty, no drive twice. |

A registered drive that isn't listed under `drives` takes no units.

## Command reference

`run`, `prune`, `drive forget`, `drive register --force`, the takeover in
`drive register`, and `verify`'s re-copy show what they'll do and ask y/n
first, and refuse to run without a terminal; `move set` changes only the registry
and takes effect at once. There's no `--yes`. Exit code 0 means success (also with warnings); anything else means an
error.

### `baruch run`

Backs up the docked drive: computes the plan, shows it, asks y/n, and carries it
out. It only syncs and deletes inside the units whose home is this drive.

| Option | Meaning |
| --- | --- |
| `--dry-run` | Show the plan and exit. Writes nothing at all. |
| `--drive NAME` | Use this drive when several registered drives are docked (otherwise Baruch asks) |
| `--skip UNIT` | Leave a unit out of this run; nothing of it changes on the drive. Repeatable; unknown names are refused. |

First `run` reads the drive and scans the NAS, which can take minutes for a
large archive. On a terminal a status line shows which unit is being listed;
each finished scan leaves a line:

```
Read BK-6A: 214 units, 88120 files, 1.2 TiB
Scanned tv: 870 units, 412003 files, 9.1 TiB
```

Without a usable drive, `run` lists the mounted USB volumes and why each isn't
used. Before each unit it checks that the drive is still mounted and its label
still matches, and stops otherwise. Before it syncs a unit, it marks it as in
progress on the drive itself (and in the NAS's cache), so an interrupted unit
is never mistaken for a complete one. As each unit starts it prints where the
run stands, and a line again when the unit is done:

```
[3/12] 20:31 home/Photos: copying 1204 files, 48.2 GiB (61.0 GiB of 212.4 GiB copied so far)
home/Photos: done
```

There's no progress within a unit; `df -h` on the drive shows it growing. It
never deletes a whole unit. Ctrl-C
stops the run; finished units stay recorded. The plan file is the run's log,
with the results added; logs are kept for 90 days.

### `baruch status`

Shows, per drive: last complete run (and what the last run left out, and why),
last seen, oldest unit sync, capacity, free space, reserve usage and last
verify. Also the total free space above all reserves. It lists what needs
attention:

- units that are unplaced, copy pending, incomplete, synced with errors, gone
  (with the `prune` that removes their data), or without any complete backup;
- drives growing into their reserve, with their largest units;
- stale data per drive: what `prune` would remove, and what it would keep and
  why;
- unexplained data per drive (`prune` with that drive docked shows the `rm`
  command);
- drives no longer configured that still hold data;
- drives not docked for longer than `stale_after`, and units copy pending on
  them;
- drives whose last verify found read errors, with the recommendation to
  replace them.

It works from the NAS's cache and the last run's scan, and shows how old they
are. It works while a run is in progress.

### `baruch find PATTERN`

Searches the cached contents lists and shows which drives hold matching files,
when each drive's copy was synced, and whether it's the unit's home or stale
data. The pattern is matched against paths on the drives, which start with
the rule name (`home-1t/docs/a.txt`), so leave the rule name out to see both
copies of a folder backed up by two rules. Works while a run is in progress.

### `baruch move set UNIT --to DRIVE`

Gives a unit a new home. The new drive gets the data when it's docked; the old
data becomes stale, for `prune` to remove once the new home has a newer complete
copy. It also gives an unplaced unit a home. Refuses drives that aren't
registered, drives not in the unit's rule's list, the unit's current home, and
gone units. Warns if the unit doesn't fit.

If the name you type doesn't match exactly (e.g. a different Unicode form of an
umlaut), Baruch shows similar units to pick from.

### `baruch drive register NAME`

Writes a label with the name and a new random disk ID to the docked disk,
records its free space, and scans what's already on it. Refuses disks that
aren't ext4. Warns if the name isn't in the configuration yet. A name whose disk
was forgotten can be registered again on a new disk; a name whose disk is still
known can't be taken over.

If the disk already carries a label with this name that the registry doesn't
know (after Baruch's state was lost), it offers to take the disk over as it is,
keeping its label and records. If the disk has a label but no records
(`/.baruch/units.yaml`), it can't be taken over: `--force` gives it a new label
and starts its records afresh.

| Option | Meaning |
| --- | --- |
| `--force` | Overwrite an unknown or invalid label, after showing it, instead of taking the disk over. Never overwrites the valid label of a registered drive. Old data on the disk is left for you or `prune` to delete. |

### `baruch drive forget NAME`

Tells Baruch that the data on this drive no longer counts: it broke, got lost,
or you just don't want to use it any more. First shows the units that would be
left without any complete backup and asks y/n; then drops the drive's entry and
its cache, and invalidates its label. It doesn't change the configuration: units whose home
is this drive stay *copy pending* until a new disk is registered under the name,
or until you remove the drive from the configuration.

### `baruch prune DRIVE`

Deletes stale data on the docked drive, after its own plan and y/n:

- **units whose home is another drive**, but only if that home has a newer
  copy synced without errors. Otherwise they're kept, and the plan says why ("home BK-6B has
  no copy yet", "… synced with errors", "… has an older copy");
- **units gone on the NAS** (deleted or renamed folders). Right before
  deleting, it checks that the folder is still missing.
- **units without a home yet** are kept ("`baruch run` places it").

It's all or nothing. To keep one unit, answer `n`; if it's gone on the NAS by
mistake, restore its folder there first (then it isn't gone any more) and prune
again. Before each unit, `prune` checks that the same drive is still mounted.

Each unit in the plan shows how many of its files have no other backup, on
another drive or in another folder of this one ("214 files only here"). The NAS
doesn't count: it's what the backup is for. A file with the same name, size and
time anywhere in the same rule counts as backed up, so a renamed folder shows 0
once its new name has been backed up, and every file until then. The summary
above
the question adds them up. Below it, the plan says where Baruch believes the
other copies are, and when it last saw each of those drives:

```text
  - tv/Lost: gone on the NAS: 2 files, 20.0 KiB; 0 files only here
      (also on BK-6B, last seen 2026-10-01)
```

Read those lines before you answer. Baruch only knows other drives as they were
when last docked: if the drive named there has broken, been pruned or changed
by hand since, the count is wrong. Answer `n`, and dock that drive (or
`baruch drive forget` it if it's gone) first.

`prune` never deletes unexplained data (folders of a rule that isn't in the
config, folders that aren't units): it lists them with an `rm` command for you
to run if you're sure. It doesn't touch a rule whose `.backup-source` marker is
missing, `/.baruch/`, `lost+found`, or excluded names outside unit folders.

### `baruch verify`

Compares the docked drive's units with the NAS by checksum. See
[Verifying a drive](#verifying-a-drive).

### `baruch config check`

Checks the configuration without a drive: sources exist and have markers, no
source lies inside another, rules with the same source have no drive in
common, every drive named in a rule is listed under
`drives`, no list is empty or names a drive twice, and no exclude matches
`.backup-source`, `.baruch`, `lost+found` or a rule name.

### `baruch init`

Creates an empty registry, needed once before the first run. If there's no
configuration yet, it also writes a commented starter to
`~/.config/baruch/config.yaml`; an existing configuration is kept as it is.
Refuses if a registry already exists.

## Unit states

*Gone* applies to the whole unit. Otherwise each unit is in exactly one state,
checked in this order:

| State | Meaning | What happens next |
| --- | --- | --- |
| Gone | Its folder is missing on the NAS | Its data stays on the drives until `prune` removes it |
| Unplaced | Fits on no drive of its rule | Placed once a drive has room, or by `move set` |
| Copy pending | Its home drive has no record of it yet (its data may already be there, e.g. after the switch-over or `register --force`), or a record but no data (e.g. the folder was deleted by hand) | Copied when the home drive is docked and it fits |
| Incomplete | The last sync on its home didn't finish | Finished on the next run there |
| In place | Its home has a complete copy: a record of a finished sync (possibly with errors), and its data is there | Normal sync |

"Synced with errors" is a note on top of these: some files on the NAS, or
folders on the drive, couldn't be read, and they're listed. *Stale data* isn't a state of the unit but of a
drive: data of the unit on a drive that isn't its home (or of a gone unit),
removed only by `prune`.

## Files and locations

| Where | What |
| --- | --- |
| `~/.config/baruch/config.yaml` | Configuration, edited by hand (`baruch init` writes a starter) |
| `~/.local/state/baruch/` | Registry (homes, drives) and its last 10 versions, the cache of each drive's records and contents, saved plans and logs |
| `/<rule>/…` on each drive | The backed-up files, as plain files |
| `/.baruch/` on each drive | Label, records (`units.yaml`), contents list, copy of the configuration, `restore.sh`, partial files |

Both NAS locations are inside your home folder, so the `home` rule backs them
up too.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ty check
```

Tests run on the Mac and on Linux in GitHub Actions. They use synthetic data
generated per test, real rsync, and fake drives in temp folders. After every
Baruch command a test runs, a check fails the test if data or a backup was lost
without the output saying so. A fuzzer (`tests/fuzz/`) tries random sequences of
user actions and failures, each ending with a restore by every drive's
`restore.sh`; it's reproducible by default, and
`BARUCH_FUZZ=explore uv run pytest tests/fuzz` explores further. See
[`docs/spec.md`](docs/spec.md#testing) for details.

To try a development version on the NAS:

```sh
brew install uv
uv tool install --force git+https://github.com/infogrind/baruch@<branch>
```
