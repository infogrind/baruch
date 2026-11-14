# Baruch: NAS backup plan

## Context

The backup to the single 20 TB USB drive is about to run out of space. Plan:
replace it with a rule-based rsync backup that spreads data over several drives,
docked one at a time and backed up by hand.

- **NAS:** mynas, DS1522+, DSM 7.2.2, SHR-1 with 5×6 TB. `/volume1` is ext4,
  21.5 TiB, 17.7 TiB used (measured 2026-11-14). Planned: two disks replaced by
  12 TB (~27.3 TiB usable), which frees two 6 TB disks for backup.
- **Current backup:** one Hyper Backup task, "Main Backup", single-version (a
  plain mirror), started by hand from time to time. It backs up `/archive`,
  `/homes/alex` and `/projects` to the 20 TB drive. It is retired once Baruch is in
  use; the two never run side by side.
- **Drives:** connected one at a time through a USB dock that takes a
  bare 3.5" disk. The drives stay at home.
- **Important data** also has an offsite copy in a cloud git repo, so this plan
  doesn't treat it separately.

| Folder in /archive | Size (TiB) |
| -------------------------------------------------------------------- | ---------- |
| tv | 9.1 |
| movies | 7.5 |
| clips | 0.22 |
| everything else (music, documents, software…) | ~0.3 |
| **total** | **~17.1** |

`/homes/alex` and `/projects` add at most a few hundred GiB (not measured exactly).

**Units of measure:** all sizes in this document and in Baruch's output are
binary (TiB, GiB), as `df -h` and DSM show them. A "6 TB" disk holds 5.46 TiB, a
"20 TB" disk 18.2 TiB. In the configuration, `500G` means 500 GiB.

## Design principle

Baruch is always run by hand, by someone looking at its output. When a
situation needs a decision or is complicated, Baruch surfaces the information
(what it found, what it would do, the command to act on it) and leaves the
judgment to the user, rather than deciding and acting automatically. Automatic
behaviour is limited to the plain, predictable cases, and even those are shown
in the plan before anything happens.

## Concepts

The system knows nothing about TV, movies or particular drive sizes. Those
details live only in the configuration.

| Term | Meaning |
| -------- | ---------------------------------------------------------------------------------------------------------- |
| Drive | A backup disk, identified by a label file on the disk (name such as `BK-20A` + disk ID), not by its mount path |
| Rule | "This source folder goes there": name, source, placement |
| Unit | The smallest thing assigned to a drive: one direct subfolder of a rule's source, or the loose files directly in it. Named `<rule>/<folder>` (`tv/The Wire`) or `<rule>/(files)` (`home/(files)`); commands take this name |
| Registry | Records where each unit should be (`home`) and where copies exist (`copies`) |
| Plan | Everything a run would do on the docked drive, computed and shown before anything happens |

## Requirements

### Drives

1. Only registered drives are written to: the label's name and disk ID must
   match the registry. A disk without a label is ignored. A disk whose label
   doesn't match is never written to and is reported. A registered drive without
   an entry in the config's `drives` takes no units. If no usable drive is
   docked, `run` lists the mounted USB volumes and why each isn't used.
1. Each drive has a **reserve** (free space). When free space is at the reserve,
   the drive takes no new units. Units already on it may grow into the reserve;
   the status then reports how much of the reserve is used and suggests moves
   (requirement 18).
1. Only one drive needs to be connected at a time. A run works on the docked
   drive. If several registered drives are docked, Baruch asks which one (or
   takes `--drive NAME`).
1. Backup drives must be ext4. `drive register` refuses other filesystems.

### Rules and units

5. A rule has a name, a source folder and a placement. Excludes are global
   only (in `settings`); there are no per-rule excludes:
   - **pin:** all units go to one named drive;
   - **spread:** each unit goes to exactly one drive from an ordered list.
1. A rule's units are the direct subfolders of its source, for pin and spread
   rules alike (`movies/Heat (1995)`, `tv/The Wire`). Files directly in the
   source folder form one extra unit, `<rule>/(files)`; so do symlinks there
   (copied as links). Special files (sockets, pipes, devices) are skipped with
   a count. Excluded folders are not units. An empty
   folder is a unit of size 0. A renamed folder is a gone unit plus a new
   unit, so it's copied again (rename detection is listed under Later).
1. A unit's name is the folder name exactly as on disk: the NFC and NFD forms of
   a name are different units, and names that aren't valid UTF-8 are stored
   escaped (reversibly) in the registry. On the command line, a unit name is
   matched exactly; if nothing matches, Baruch shows similar units (e.g. the
   other Unicode form) and the user picks one. Unprintable characters are
   escaped in all output.
1. Rules don't depend on what the folders contain.
1. On each drive, every rule gets its own folder, `/<rule name>/…`, so rules
   never collide. Baruch's own files (label, registry snapshot, `config.yaml`,
   contents list, partial files) live in `/.baruch/`. Rule names may only use
   `A–Z a–z 0–9 _ -`.
1. Sources never overlap: no rule's source is inside another's. Every run and
   `config check` refuse a configuration where they do. Baruch only looks
   inside rule sources: a folder that no rule covers (e.g. a new folder in
   `/volume1/archive`) isn't backed up and isn't reported until a rule is added
   for it.

### Placement

11. A new unit is assigned when it's first seen, and only to the docked drive.
    A pinned unit goes to its pin drive.
01. For spread rules, the list is in order of preference. A drive is **closed**
    for unit U if, at its last backup, `free − reserve < size(U)`. A drive lower
    in the list takes U only if every drive above it is closed for U. Drives in
    the list that aren't registered yet are skipped. Registering a drive
    records its free space, like a backup does. For a drive that isn't docked,
    Baruch uses that recorded free space; space that pending moves will need
    isn't counted. It's checked again when the drive is docked.
01. A unit only goes to a drive where it fits completely:
    `free − size(U) ≥ reserve`, where `free` already accounts for the growth of
    the units on that drive in this run. For a unit that already has data on the
    drive, recorded or not, the check uses the net bytes the run would write
    instead of `size(U)`. `size` is the sum of the unit's file sizes on the NAS
    after excludes (each hard-linked file once), measured on every run. The
    reserve absorbs the filesystem's overhead.
01. New units are placed largest first (ties alphabetical), for the best
    packing.
01. Assignments are permanent. They change only by `move set`, by
    `drive forget`, or by a config change (requirement 38).
01. A unit that fits on no drive is reported, never split.
01. If a drive higher in the list hasn't been docked for a long time, new units
    wait for it. The plan and the status report "N new units waiting for X
    (last docked …)" with the `move set` command that places them on the docked
    drive instead.

### Moves

18. When units grow into a drive's reserve, the end of each run and the status
    show how much of the reserve is used and suggest which units to move: the
    fewest units that bring the drive back to its reserve, preferring those that
    haven't changed for the longest time. The destination follows the placement
    rules. The suggestion includes the command to run. Suggestions are computed
    on the fly and not stored.
01. A move is `baruch move set UNIT --to DRIVE`, which sets the unit's `home`.
    The new drive gets the copy when it's docked; the old copy is deleted when
    the old drive is docked **and** the new home has a confirmed copy (copy
    first, then delete). In the plan, this deletion is labelled ("moved to
    BK-6B, copy confirmed there on …") and doesn't trigger a warning.
01. `move set` refuses targets that aren't registered or aren't in the unit's
    spread list (to use another drive, add it to the list first). It warns
    if the unit doesn't fit. It refuses pinned units: change the pin instead.

### Running a backup

21. Backups are always started by hand with `baruch run`. Docking a drive does
    nothing by itself, and there's no scheduled run. Long runs are meant to run
    in tmux or similar.

01. **Plan first.** A run first computes everything it would do on the docked
    drive: assignments, files to copy, overwrite and delete, move cleanups,
    skipped units. It then shows the plan:

    - a unit that has no data on this drive yet (first copy, new unit,
      replacement drive) is one summary line: files and size;
    - for every other unit, each new, changed and deleted file is listed;
    - an **untracked copy** (the unit's folder exists on this drive but the
      registry has no copy record for it, e.g. after the switch-over,
      `register --force` or a registry restore) is labelled "existing folder,
      not yet recorded". If this drive is the unit's home, it's listed file by
      file like any other unit and counts towards the warnings. Otherwise it's
      orphaned data (requirement 38).

01. **Warnings** appear at the end of the plan, right above the question
    (requirement 24), where the terminal shows them first, if on this drive:

    - more than `warn_changed_files` (10%) of the files, or
    - more than `warn_changed_bytes` (10%) of the bytes would be overwritten or
      deleted, or
    - any unit would lose more than `warn_unit_loss` (50%) of its content. This
      includes units removed on the NAS and emptied folders whose marker
      survives. Cleanups of moved units (requirement 19) don't count.

    The shares are of the files and bytes on the drive before the run.
    Overwritten files count with their full size. A unit's loss is the share
    of its bytes that would be deleted.

01. **Confirm.** Before the question, the full plan is saved to a file, and a
    short summary is shown right above the question: the warnings, the
    totals per unit (copy / overwrite / delete: files and size) and the path of
    the plan file. The plan ends with a y/n question. `n` aborts without changing
    anything; the user investigates (e.g. restores wrongly changed files) and
    runs again. `y` carries out exactly the plan: Baruch computes the plan
    itself (both sides listed, compared by size and modification time, excludes
    applied by Baruch), rsync gets exactly the planned file list
    (`--files-from`), and Baruch carries out the planned deletions itself. A
    planned file that changes before it's copied is copied as it is at copy
    time. Files added or deleted on the NAS after the plan was made are not
    handled in this run; they're reported and picked up next time. There's no `--yes`, and `run` refuses to run without a
    terminal. `baruch run --skip UNIT`
    (repeatable) leaves units out of the plan: they're shown as "skipped on
    request" and stay *outdated*, e.g. while the user looks into a suspicious
    change.

01. `baruch run --dry-run` shows the plan and exits. It writes nothing, not even
    `last_seen`.

01. A run only syncs units whose home is the docked drive. Copies on it of
    other units (cleanup pending, a drive taken out of service) are left as they are until
    they're deleted.

01. **Order** after confirmation: cleanups (frees space) → incomplete units →
    existing units, pinned before spread → copy pending → newly placed units →
    registry snapshot and contents list. Within each group, largest first.
    Within a unit, deletions come first, then copies.

01. **Space:** right before each unit, the fit check is repeated with the
    drive's current free space. A unit that doesn't fit is never started; it's
    skipped and stays *outdated*, and its previous copy stays. If the disk
    fills up during a unit anyway, the unit is *incomplete* and the run stops. Other units are never deleted to
    make room. A unit is only recorded as synced once it's complete. If a run is
    interrupted, the unit in progress is recorded as *incomplete* and the next
    run finishes it.

01. **Errors:** files that vanished during copying → warning, the unit counts as
    synced. Files that can't be read on the NAS → the unit is "synced with
    errors", and the files are listed. Read or write errors on the drive → the
    run stops for that drive. **Scan errors on the NAS** (an unreadable folder,
    an I/O error) → no deletions are planned under that path; the plan lists
    the path and the error, and everything else goes ahead. A unit whose folder
    can't be read is shown as *unreadable*, not gone. Before each unit, Baruch checks that the drive is
    still mounted and its label still matches; otherwise it stops and records
    nothing for that unit. (Otherwise rsync could write into the empty mount
    folder on the NAS's system partition, which has under 1 GiB free.)

01. **Ctrl-C** stops rsync. Units already finished stay recorded, the snapshot
    and contents list are written, and the next run continues.

01. At the end of a run, the registry snapshot, a copy of `config.yaml` and a
    contents list (all files on the drive) are written to the drive. The contents list is also kept on the
    NAS. The run ends with a summary in the terminal; its last line is "safe to
    eject in DSM" once everything is written. There are no notifications, and
    the drive is not ejected by Baruch. A run is **complete** when every home
    unit of the drive ended with `ok` or `errors`.

01. Exit code 0 for success, also with warnings; non-zero only for errors. Each
    real run writes a log with the full plan; logs are kept for 90 days.

### Status and search

33. `baruch status` shows, per drive: last complete run, last seen, oldest unit
    sync, capacity, free space, reserve usage, last verify; across drives: the total free space
    above the reserves. It lists the units that need attention (waiting, copy
    pending, outdated, incomplete, synced with errors, cleanup pending, gone,
    units with no backup at all), move suggestions, orphaned data (requirement
    38), drives no longer in the config that still hold copies, and drives not
    docked for longer than `stale_after`.
01. `baruch find PATTERN` searches the contents lists on the NAS and shows which
    drives hold matching files, and how current each copy is. Forgotten drives
    aren't searched: a file only they held is reported as on no drive.

### Drive lifecycle

35. `baruch drive register NAME` writes a label with the name and a new random
    disk ID to the docked disk. It accepts names that aren't in the config yet
    (with a warning).
    - `--force` overwrites an existing unknown or stale label, after showing it
      and asking. It never overwrites the valid label of a registered drive. Deleting old data is up to the user.
    - A name whose disk was forgotten (requirement 36) is simply registered
      again on the new disk. A name whose disk is still known can't be taken
      over: a new disk gets a new name (e.g. an upgrade, see "Scenario: a drive
      breaks").
01. `baruch drive forget NAME` tells Baruch that the copies on the drive's disk
    no longer count: it broke, got lost, or the user simply doesn't want to use
    it for backups anymore (its data may still be kept). Baruch can't tell these
    apart, so it never forgets a drive by itself. After showing what changes and
    asking y/n, it removes all the drive's copies from the registry, makes its
    label invalid (if the disk turns up again it's an unknown disk), and lists
    the units that now have no backup. Everything that was backed up there needs
    a new copy: if the config still names the drive, its units keep it as home
    and are copied when a new disk is registered under that name; once the
    drive is removed from the config, they're placed again. `forget` doesn't
    change the config.
01. **Taking a drive out of service** is a config change: remove it from all
    rules and from `drives`. Its units are placed again (requirement 38), copy
    first. Its copies stay, aren't updated, and are deleted as labelled
    cleanups when it's docked after each unit has a confirmed copy on its new
    home. To skip that, `drive forget` it. The registry drops a drive once it's
    not in the config and holds no copies.

### Config changes and verify

38. A run only deletes inside the units it syncs, plus labelled move cleanups.
    Data on a drive that no current unit explains (a removed or renamed rule,
    for example, including the units of a removed rule) is reported as
    **orphaned** and only deleted by
    `baruch prune DRIVE`, which shows its own plan with warnings and asks y/n.
    An untracked copy on a drive that isn't the unit's home is orphaned data
    too; the report names the home and whether it has a confirmed copy. `prune`
    keeps it while the home has no confirmed copy, and says why. Never
    orphaned: `/.baruch/`, `lost+found`, and anything the global excludes
    match.
    `baruch config check` validates the configuration without a drive: sources
    exist and have markers, every drive in a rule is in `drives`. Other config
    changes follow the existing rules:

    - pin moved to another drive → the units get the new home, copy first;
    - drive removed from a rule → its units of that rule are placed again;
      their old copies stay until the new home has a confirmed copy (cleanup
      pending);
    - new exclude → the excluded files are deleted in the unit's normal sync and
      count towards the warnings;
    - rule renamed → a new rule plus orphaned data (the data is copied again).

01. `baruch verify` reads every file of the docked drive's home units, compares
    checksums with the NAS, and reports:

    - read errors on the drive (counted; many errors make the status recommend
      replacing the drive);
    - files changed on the NAS since the last sync (normal, the next backup
      takes care of them);
    - files whose content differs with the same size and time ("unknown which
      side is right"; `/volume1` is ext4, so the NAS has no checksums to tell).

    The only write it offers is for read errors: "copy these N unreadable
    files again from the NAS? y/n". Changed files are left to the next run.
    Files with unknown correct side are listed with both paths and the commands
    to copy in either direction; the user decides each one. Verify only runs when started (the status shows
    each drive's last verify). It reads the whole drive: ~16–25 hours for BK-20A
    (~8.3 TiB at 100–160 MB/s), ~8–14 hours for a full 6 TB drive.

## Registry and unit states

The registry lives on the NAS and is the one that counts. Each unit stores where
it **should be** (`home`) and where copies **actually exist** (`copies`, each
with the time and result of its last sync). Each run's scan of the NAS adds
whether the unit's folder still exists. States are derived from these; there's
no stored state field.

```yaml
units:
  tv/The Wire:
    home: BK-6B                  # moved from BK-6A with `move set`
                                 # the copy on BK-6A is the old one; it's deleted
                                 # when BK-6A is next docked (cleanup pending,
                                 # derived from home ≠ BK-6A, not stored)
    first_seen: 2026-11-01
    copies:
      BK-6A: { synced: 2026-11-02T21:14, result: ok, size: 410G }
      BK-6B: { synced: 2026-11-20T03:40, result: ok, size: 410G }

  tv/Lost:
    home: BK-6A
    first_seen: 2026-11-01
    copies:
      BK-6A: { synced: 2026-11-02T22:05, result: skipped, size: 380G }

  tv/Severance:
    home: null                   # 520G, didn't fit on BK-6B on 2026-11-20
    first_seen: 2026-11-15
    copies: {}

  clips/(files):                 # the loose files directly in /volume1/archive/clips
    home: BK-20A
    first_seen: 2026-11-18
    copies:
      BK-20A: { synced: 2026-11-18T10:02, result: errors, size: 225G }

drives:
  BK-6A:  { disk_id: 7f3c…9a, last_seen: 2026-11-02T23:50, last_complete_run: 2026-11-02T23:50,
            capacity: 5.46T, free: 0.34T, last_verify: null }
  BK-6B:  { disk_id: 2b81…c4, last_seen: 2026-11-20T04:12, last_complete_run: 2026-11-20T04:12,
            capacity: 5.46T, free: 0.92T, last_verify: null }
  BK-20A: { disk_id: e09d…17, last_seen: 2026-11-18T11:30, last_complete_run: 2026-11-18T11:30,
            capacity: 18.2T, free: 8.10T, last_verify: null }
```

A copy's `result` is the outcome of the last attempt: `ok`, `errors` (some NAS
files unreadable), `skipped` (no space this time) or `incomplete` (interrupted).
`synced` is when the copy was last complete; it's only updated when a sync
finishes with `ok` or `errors`. A **confirmed copy** is one with `result: ok`;
moves, config changes and `prune` delete an old copy only once the home copy is
confirmed. Exactly one state applies, in
this order of precedence:

| State | Condition | What happens next |
| --------------- | ----------------------------------------------- | ---------------------------------------------------------------- |
| Gone | The unit's folder is missing on the NAS (an unreadable folder isn't gone, requirement 29) | Its copies appear as deletions (with a warning) in the next plans; entry removed once no copies remain |
| Waiting | `home` is empty | Placed when an eligible drive is docked |
| Copy pending | `home` isn't in `copies` | Copied when the home drive is docked |
| Outdated | Copy on home has `result: skipped` or `incomplete` | Synced once there's room / on the next run |
| Cleanup pending | Copies on other drives besides home | Deleted when those drives are docked, if home's copy is confirmed |
| In place | Otherwise | Normal sync |

"Synced with errors" (`result: errors`) is shown as a note, not a state of its
own. A gone unit whose folder reappears is a normal unit again, with its old
home and copies.

- `synced` and `size` change only when a unit's sync has finished, so an
  interrupted run leaves the last complete time, and the next run repeats the
  step.
- Only a confirmed run (`y`) writes to the registry: new units, sizes and gone
  units, for all rules. `n` and `--dry-run` write nothing. `baruch status` uses
  the last run's scan and shows how old it is.
- **Integrity:** one lock per NAS, held by the process (`flock`), so a crash
  never leaves a stale lock. Read-only commands (`status`, `find`) don't take
  it and work during a run. The registry is written atomically (temporary file,
  then rename). A version is saved before each command or run that changes it,
  and the last 10 are kept. `baruch init` creates the registry. If it's missing
  or can't be read, Baruch refuses to do anything and names `init` and
  `registry restore`; it never starts with an empty one. Times are stored in
  UTC.
- Changes are made with Baruch's commands. Hand edits are possible for
  emergencies; every run validates the file.
- **Restore of the registry:** `baruch registry restore` takes the newest
  snapshot from the docked drive. Copies on other drives are marked
  *unverified* until that drive is docked and its actual contents confirm them.
  Nothing is deleted on the strength of an unverified copy.
- The registry is under `/volume1/homes/alex`, so the `home` rule backs it up
  as well.

## Safety

- **Source marker:** each rule's source folder must contain `.backup-source`.
  If it's missing (volume not mounted, folder renamed), the rule is skipped and
  reported, and nothing on the drive is touched for it.
- **Plan and confirmation** (requirements 22–24): nothing happens before the
  user has seen the full plan and answered y, and large changes are flagged.
- **Copy first:** a copy is only deleted from a drive that isn't the unit's home
  once the home drive has a confirmed copy. Gone units are the exception: their
  data no longer exists on the NAS, so their copies are deleted in the plan,
  with a warning (requirement 23).
- **Nothing unknown is deleted:** data no unit explains is only removed by
  `prune` (requirement 38).
- **Mount check** before each unit (requirement 29).

## Configuration

`~/.config/baruch/config.yaml`, edited by hand. Baruch never changes it: its
commands only change the registry and drive labels. Everything except TV is pinned to the 20 TB
drive. TV is spread over the two 6 TB drives, with the 20 TB drive as the last
choice.

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
  # One rule per folder in /volume1/archive, each with its own marker.
  - name: movies
    source: /volume1/archive/movies
    pin: BK-20A

  - name: clips
    source: /volume1/archive/clips
    pin: BK-20A

  - name: music
    source: /volume1/archive/music
    pin: BK-20A

  # … documents, software the same way

  - name: home
    source: /volume1/homes/alex
    pin: BK-20A

  - name: projects
    source: /volume1/projects
    pin: BK-20A

  - name: tv
    source: /volume1/archive/tv
    spread: [BK-6A, BK-6B, BK-20A]   # order of preference
```

| Key | Where | Meaning |
| -------------------- | --------- | ----------------------------------------------------------------- |
| `exclude` | settings | Names or `*` patterns (`@eaDir`, `*.tmp`), matched against every file and folder name at any depth, in all rules; applied by Baruch, not rsync |
| `stale_after` | settings | Status flags drives not docked for longer than this |
| `warn_changed_files` | settings | Warning if more than this share of the drive's files would be overwritten or deleted |
| `warn_changed_bytes` | settings | The same for bytes |
| `warn_unit_loss` | settings | Warning if a unit would lose more than this share of its content |
| `reserve` | drives | Free space at which the drive takes no new units (`%` or size) |
| `name`, `source` | rules | Rule name (also its folder on each drive) and source folder |
| `pin` / `spread` | rules | One drive, or an ordered list of drives |

**Why BK-20A is in the TV list:** TV is ~9.1 TiB. The two 6 TB drives hold 10.9
TiB. After ext4's own overhead (~1.5%) and above their 10% reserve, that's
~9.6 TiB if the root reserve is turned off (see "Preparing a drive"), ~9.2 TiB
if not. TV fits today with at most ~0.5 TiB to spare, which TV's growth uses up
within months, and whole shows don't pack perfectly. BK-20A, which holds ~8.3 TiB of pinned data, takes the overflow.

**Capacity:** above the reserves and ext4's overhead, the drives hold about
18.2 × 0.985 × 0.95 + 9.6 ≈ 26.6 TiB (root reserve turned off).
After the 2×12 TB upgrade, the NAS has ~27.3 TiB. So the backup is about as
large as the NAS, with no headroom: when the NAS is full, the backup is full
first. The status shows the total free space above the reserves.

When TV outgrows its drives, add a drive to `drives` and to `spread`. Pinned
data can't spill over: when the pinned rules outgrow BK-20A, change one of them
(e.g. `movies`) from `pin: BK-20A` to `spread: [BK-20A, BK-X]`. Its units keep their home on BK-20A,
and new ones can go to BK-X.

## Scenario: a drive's shows outgrow it

Example: shows already on BK-6A keep getting new seasons. Assignments don't move
on their own, so the growth lands on BK-6A.

1. **Reserve reached:** BK-6A takes no new shows; they go to BK-6B or BK-20A.
1. **Growing into the reserve:** the end of each run on BK-6A and the status
   show "existing shows have grown into the reserve: 38% of 559 GiB used" and
   suggest moving, e.g., `tv/Lost` (380 GiB, unchanged since 2025-03) to
   BK-6B, with the command.
1. **Move:** `baruch move set "tv/Lost" --to BK-6B`. The unit is *copy pending*.
1. **BK-6B docked:** the plan shows "tv/Lost — first copy: 214 files, 380 GiB";
   after the run it's *cleanup pending*.
1. **BK-6A docked:** the plan shows the labelled cleanup ("moved to BK-6B, copy
   confirmed there on …"); the old copy is deleted first, then the remaining
   shows are synced in the freed space. If BK-6A is docked before step 4,
   nothing is deleted.
1. **Ran out of space anyway:** shows that don't fit are skipped and stay
   *outdated* until there's room (requirement 28).
1. **Every drive in the list full:** register a new drive, add it to `drives` and
   `spread`. New shows go there, and moves free up the old drives.

## Scenario: a drive breaks

Example: BK-6A fails physically. Baruch can't tell "broken" from "in a drawer",
so it never forgets a drive by itself.

1. **Notice:** a run stops with read or write errors on the drive, the drive
   isn't recognised when docked, or the status shows it hasn't been docked for
   a long time.
1. **Forget it:** `baruch drive forget BK-6A`. Its copies are dropped from
   the registry, its label becomes invalid, and the status lists what now
   exists only on the NAS (e.g. "61 shows, 4.5 TiB have no backup").
1. **Either replace it:** prepare the new disk ("Preparing a drive"), dock it,
   `baruch drive register BK-6A`. It takes over the name (the config stays the
   same) and may be larger. The shows whose home is BK-6A keep it (forgetting
   doesn't change the config or homes), so they're *copy pending* and appear
   as "first copy" lines in the plan.
1. **Or do without it:** remove BK-6A from the config (rules and `drives`). Its
   shows are placed again on other drives with room as those are docked.
1. **First run:** about 4.5 TiB over USB takes 9–14 hours (100–160 MB/s). It
   can be stopped and continued, because each show is only recorded once fully
   copied.
1. **Old disk turns up again:** its label is invalid, so it's treated as an
   unknown disk. To reuse it: `baruch drive register NAME --force`.

Slow failure (the disk mounts but some files can't be read) is caught by
`baruch verify` (requirement 39). Many errors there make the status recommend
replacing the drive early.

**Upgrading a healthy drive** to a larger disk, e.g. BK-6A (6 TB) to BK-12A
(12 TB):

1. Prepare and register the new disk: `baruch drive register BK-12A`.
1. In the config, put BK-12A where BK-6A was (same place in each spread list,
   same pins) and add it to `drives`; remove BK-6A from the rules.
1. BK-6A's units are placed again (requirement 38). BK-12A has BK-6A's place
   in the preference order, so they wait for it and go there when it's docked;
   they're copied from the NAS.
1. Meanwhile BK-6A keeps its copies as the backup (*cleanup pending*). When
   it's docked after BK-12A has confirmed copies, the plan shows them as
   cleanups. Or, once BK-12A has had a complete run, `baruch drive forget BK-6A` and remove it from `drives`; the old disk can be wiped or kept.

## Scenario: the NAS is lost

Example: the volume crashes, a disk upgrade fails, or ransomware encrypts the
shares. Baruch isn't needed for the restore: the drives hold plain files.

1. **Set up the NAS** again (DSM, shared folders, user alex, Homebrew rsync).
1. **Dock BK-20A first** and copy back `home`. This also brings back Baruch's
   configuration and state (`~/.config/baruch/`, `~/.local/state/baruch/` are
   inside `/volume1/homes/alex`), including the registry.
1. **Copy back each drive**, rule folder by rule folder, e.g.
   `rsync -a /volumeUSB1/usbshare/tv/ /volume1/archive/tv/`. If a unit has
   copies on several drives, the registry (or each drive's snapshot) shows which
   one is newest.
1. **Without BK-20A:** each drive's registry snapshot and `config.yaml` show its
   rules, which units it holds, and which drives hold the rest.
1. **Duration:** all ~17 TiB takes about 33–52 hours of copying (100–160 MB/s).
1. **Afterwards:** the `.backup-source` markers come back with the files (each
   is part of its rule's `(files)` unit), and `rsync -a` keeps the
   modification times, so the next `baruch run` is incremental.

## Restoring files

The drives hold plain files, so restoring doesn't need Baruch.

1. `baruch find "S01E01"` shows which drives hold matching files, and how
   current each copy is (from the contents lists on the NAS).
1. Dock that drive and copy the files back with Homebrew's rsync, e.g.
   `rsync -a "/volumeUSB1/usbshare/tv/The Wire/" "/volume1/archive/tv/The Wire/"`.
1. Without the NAS registry: each drive's snapshot shows which of its units have
   it as home and when they were synced. A copy on a drive that isn't the
   unit's home is an older one.

## Preparing a drive

Baruch runs as alex and doesn't check these; they're one-off steps by hand.

1. Run an extended SMART test.
1. Format it as ext4 in DSM, and give alex read/write access to its
   `usbshare` shared folder.
1. Turn off ext4's root reserve (5% by default, ~280 GiB on a 6 TB disk, which
   alex can't use): `sudo tune2fs -m 0 /dev/<partition>`.
1. On a disk that already holds data: `sudo chown -R alex` on it.
1. `baruch drive register NAME`.

## Switching over from Hyper Backup

The 20 TB drive already holds a plain mirror. BK-20A stays untouched until the
6 TB drives have their copies, so a complete (if older) backup exists the whole
time.

Nothing in Baruch is specific to Hyper Backup: the switch-over uses only
`mv` on the drive and Baruch's normal handling of untracked copies
(requirement 22).

1. A last manual Hyper Backup run (done 2026-11-14). BK-20A is ext4.
1. Upgrade the NAS disks to 2×12 TB, which frees the two 6 TB disks. Baruch
   isn't used until at least one 6 TB drive is ready (a spare one may already
   exist).
1. Prepare the 6 TB disk(s) (see "Preparing a drive") and register them as
   BK-6A (and BK-6B).
1. Back up to BK-6A, then BK-6B. TV shows get assigned in preference order and
   are copied from the NAS (~9 TiB in total, about a day). Shows that don't fit
   stay waiting.
1. Retire the Hyper Backup task.
1. Move the folders out of Hyper Backup's task folder on BK-20A into the
   `/<rule>/…` layout at the drive's root with `mv` (instant, same disk; e.g.
   `…/archive/movies` → `/movies`, `…/archive/tv` → `/tv`,
   `…/homes/alex` → `/home`), after `sudo chown -R alex` on it (Hyper
   Backup's files belong to root). Then delete what's left of the task folder
   and register the drive as BK-20A.
1. Back up to BK-20A. Pinned folders are synced incrementally. Overflow shows
   that get home BK-20A are already there, so they're synced incrementally too.
   They appear as untracked copies (requirement 22), listed file by file, so a
   wrong `mv` in step 6 shows up as deletions and warnings before anything
   happens. If Hyper Backup didn't keep modification times, every file shows
   as changed: a full recopy, not a loss. The TV folders of shows that are now on the 6 TB drives are
   reported as orphaned data.
1. Once the plan looks right, `baruch prune BK-20A` deletes those leftover TV
   folders, after its own plan and y/n.

## Tech stack

The tool is called **Baruch**, after Jeremiah's scribe, who wrote the burned
scroll out again (Jeremiah 36). Python, kept on GitHub and released to the NAS
through the personal Homebrew tap (`infogrind/homebrew-tap`, published with
`pythonformula`).

| | NAS (DSM 7.2.2, x86_64) | MacBook |
| --------------- | ----------------------- | --------------------------------------------- |
| System Python | 3.8 (too old, not used) | – |
| Homebrew Python | 3.15 | 3.11–3.14 |
| rsync | DSM 3.1.2 (not used) | `/usr/bin/rsync` is openrsync (not used) |
| uv | not installed yet | installed |

- **Language and tooling:** Python ≥ 3.12, uv with hatchling as build backend,
  ruff, pytest.
- **Dependencies:** PyYAML is the only Python library. rsync runs as a separate
  program. Baruch uses Homebrew's, `<Homebrew prefix>/opt/rsync/bin/rsync`
  (`/home/linuxbrew/.linuxbrew` on the NAS, `/opt/homebrew` on the Mac), and
  otherwise the `rsync` on the `PATH`. There's no setting for it. Baruch
  refuses openrsync and rsync older than 3.2, so DSM's and macOS's own rsync
  are never used.
- **rsync options:** `-a -H --partial-dir=… --files-from=…`; excludes are
  already applied in the file list. Symlinks are
  copied as links, hard links are kept within a unit, large interrupted files
  resume. Owners and Synology ACLs aren't kept, because Baruch runs as alex.
- **User and locations:** Baruch runs as alex, without root. Files alex
  can't read are reported (requirement 29). Configuration in
  `~/.config/baruch/`, state (registry and its versions, contents lists, logs)
  in `~/.local/state/baruch/`. Both locations can be overridden (tests).
- **Released version:** tag a release, run `pythonformula`. After generating the
  formula for the first time, add `depends_on "rsync"` to it by hand;
  `pythonformula` keeps it on later updates. On the NAS, once
  `brew tap infogrind/tap`, then `brew install` / `brew upgrade`.
- **Development version on the NAS:** `brew install uv` once, then
  `uv tool install --force git+https://github.com/infogrind/baruch@<branch>`.
  It's separate from the Homebrew release, so switching back is easy.

NAS-specific parts sit behind small replaceable pieces, so everything else runs
on the Mac:

| NAS-specific part | On the NAS | In tests |
| --------------------- | ---------------------------------------------- | ----------------------------------------------------------- |
| Finding docked drives | Scan `/volumeUSB*/usbshare*` for label files | Configurable search path; drives are temp folders with a label file |
| Free space | Read from the filesystem | Fake capacity per drive, also enforced on writes |
| Mount check | Path is a mount point and the label matches | Temp folder exists and the label matches |

## Testing

All tests run with `uv run pytest`, on synthetic data of a few megabytes
generated per test, on the Mac (except the few listed below) and on Linux in
GitHub Actions on every push. CI uses the distribution's rsync (≥ 3.2), found on
the `PATH`.

**Two kinds of tests**

1. **Logic tests (most of them):** the planner takes the configuration, the
   registry and file listings (path, size, modification time) of the source and
   the drive, and returns the plan. They cover placement, moves, the warnings,
   space and skipping, without touching files.
1. **End-to-end tests:** a generated fake NAS and two or three fake drives in a
   temp folder, with real rsync and the real scanner. Tests "dock" drives in
   sequence, answer the plan's question, and check the result.

**Test data descriptions** (in `tests/data/`) describe only the starting state.
A helper builds the tree in a temp folder, in milliseconds. Nothing generated is
stored in the repo.

```yaml
scale: 1MB = 1TiB                # same proportions as mynas
tree:
  archive/movies: { folders: 40, size: 7.5MB }
  archive/tv:
    - { name: "Show A", seasons: 5, episodes: 10, size: 1.2MB }
    - { name: "Show B", seasons: 1, episodes: 8,  size: 0.1MB }
    - { generate: 60, total: 7.8MB }        # shows with random sizes
  homes/alex: { files: 200, size: 0.2MB }
```

- Drive capacities are scaled the same way (BK-6A = 5.46 MB). Free space in
  tests is computed from the files' sizes against the fake capacity, not from
  the real filesystem, whose block overhead dominates at this scale. Running
  out of space mid-unit is tested in logic tests with a fake rsync runner, and
  end to end in one Linux CI test on a small real filesystem with a fixed
  size.
- File content is derived from path + version number and timestamps are fixed,
  so every version is really different content. No zero-filled files.
- A few descriptions cover the starting states, e.g. `base.yaml`,
  `nearly_full.yaml`, `odd_names.yaml`.

**Changes happen in the test code**, through helpers on the fake NAS, so
multi-step scenarios read top to bottom:

```python
def test_replaced_episode_is_synced(nas, drives, run):
    nas.build("base.yaml")
    bk6a = drives.register("BK-6A", capacity="5.46MB")
    run(dock=bk6a, answer="y")

    nas.replace("archive/tv/Show A/Season 1/S01E01.mkv")   # version 2
    run(dock=bk6a, answer="y")

    assert bk6a.read("tv/Show A/Season 1/S01E01.mkv") == nas.read(
        "archive/tv/Show A/Season 1/S01E01.mkv"
    )
```

| Call | What changes | Purpose |
| ---------------------------------- | -------------------------------- | ------------------------------------------------------------------ |
| `nas.replace(ep)` | Content and timestamp, same size | Normal case |
| `nas.replace(ep, size="+30%")` | Size too | Quality upgrade by Radarr/Sonarr |
| `nas.replace(ep, keep_mtime=True)` | Content only | Documents the limit of rsync's size-and-timestamp check: not detected by a run, found by verify |

Further helpers: `nas.add`, `nas.delete`, `nas.rename`, `nas.add_show`,
`nas.remove_marker`.

**Odd file names** (`odd_names.yaml`), based on real cases on the NAS:

- umlauts in both Unicode encodings (NFC and NFD), and a name that isn't valid
  UTF-8;
- spaces, `'`, `&`, `[ ]`, very long names;
- `@eaDir`, `.DS_Store`, `#recycle` (must be skipped);
- empty folders, symlinks, an unreadable file.

The Mac's file system can't create invalid UTF-8 names and handles the two
umlaut encodings differently from ext4, so those tests are skipped on macOS and
run only on Linux in GitHub Actions.

Other simulated failures: a broken drive by deleting its temp folder, a drive
vanishing mid-run by removing its label between units, read errors by a file
that can't be read.

## Later (not planned now)

- Rename detection: a gone unit and a new unit with exactly the same files and
  sizes are offered as a rename (instant on the drive) instead of a recopy.
- Rolling verify: each run also verifies a slice of the units not verified for
  the longest time.
- Pinning one rule to several drives (one unit per drive, e.g.
  `archive@BK-20A`).
- Notifications (DSM or email) at the end of a run.
- A `snapshot` command that records the NAS's real structure (names and sizes,
  names anonymised) as a test data description.
- Encryption of the drives (not needed while they stay at home).

## Open points

- Measure `/homes/alex` and `/projects` exactly.
- Check the free space of the 20 TB drive and the exact layout of Hyper
  Backup's task folder on it.
- Check whether a spare 6 TB disk exists.
- Check how `movies` is organised: one folder per movie makes each movie a
  unit; if it's grouped (by letter, year), the groups become the units.
- List the folders in `/volume1/archive`, one rule each.
- Check how DSM sets the root reserve when formatting a USB drive, and whether
  SMART tests work through the dock.
- Measure how long a full scan of the NAS takes (file count), since every run
  does one.
- Create the GitHub repo infogrind/baruch.
