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

**The drives are the truth.** What's on a drive, and how current it is, is
recorded on that drive. The NAS keeps the intentions (where each unit should
go) and a cache of each drive's records as last seen. The cache can be out of
date, but never contradicts its drive: it's only ever written from the drive.
Nothing a restore needs depends on the NAS's state.

## Concepts

The system knows nothing about TV, movies or particular drive sizes. Those
details live only in the configuration.

| Term | Meaning |
| -------- | ---------------------------------------------------------------------------------------------------------- |
| Drive | A backup disk, identified by a label file on the disk (name such as `BK-20A` + disk ID), not by its mount path |
| Rule | "This source folder goes there": name, source, and an ordered list of drives |
| Unit | The smallest thing assigned to a drive: one direct subfolder of a rule's source, or the loose files directly in it. Named `<rule>/<folder>` (`tv/The Wire`) or `<rule>/(files)` (`home/(files)`); commands take this name |
| Home | The drive a unit belongs on. Stored in the registry on the NAS |
| Record | Per unit on a drive: when its last sync there started and finished, and with what result. Stored on the drive (`/.baruch/units.yaml`), cached on the NAS |
| Stale data | Data of a unit on a drive that a run doesn't sync: the unit's home is another drive, it has no home, or it's gone on the NAS. Only `prune` removes it |
| Unexplained data | Data on a drive that no current unit explains (a rule not in the config, a folder that isn't a unit). Baruch never deletes it; it shows the command |
| Plan | Everything a run would do on the docked drive, computed and shown before anything happens |

## Requirements

### Drives

1. Only registered drives are written to: the label's name and disk ID must
   match the registry. A disk without a label is ignored. A disk whose label
   doesn't match is never written to and is reported. A registered drive without
   an entry in the config's `drives` takes no units, but can still be docked for
   `prune`. If no usable drive is docked, `run` lists the mounted USB volumes
   and why each isn't used.
1. Each drive has a **reserve** (free space). When free space is at the reserve,
   the drive takes no new units. Units already on it may grow into the reserve;
   the status then reports how much of the reserve is used (requirement 18).
1. Only one drive needs to be connected at a time. A run works on the docked
   drive. If several registered drives are docked, Baruch asks which one (or
   takes `--drive NAME`).
1. Backup drives must be ext4. `drive register` refuses other filesystems.

### Rules and units

5. A rule has a name, a source folder and `drives`: an ordered list of drive
   names, most preferred first. Each unit goes to exactly one drive of the
   list. A list with one drive sends everything there.

   ```yaml
   drives: [BK-20A]                  # everything on BK-20A
   drives: [BK-6A, BK-6B, BK-20A]    # each unit on one of these, in this order
   ```

   Rule names must be unique. The list must not be empty or name a drive
   twice, and every drive in it must be listed under the config's `drives`. Excludes are
   global only (in `settings`); there are no per-rule excludes.

1. A rule's units are the direct subfolders of its source, for every rule
   alike (`movies/Heat (1995)`, `tv/The Wire`). Files directly in the
   source folder form one extra unit, `<rule>/(files)`; so do symlinks there
   (copied as links). Special files (sockets, pipes, devices) are skipped with
   a count. Excluded folders are not units. A real folder named `(files)`
   can't be a unit: it's reported as a scan error (rename it). An empty
   folder is a unit of size 0. The `.backup-source` marker is a loose file, so
   it belongs to `(files)`, which therefore exists in every rule that isn't
   skipped. If a name switches between a unit folder
   and a loose entry (folder `X` replaced by a file `X`, or the reverse) while
   the drive still holds the other kind at that path, the new entry is skipped
   in this run and reported ("skipped: name collides with data on the drive,
   see the plan"). The old entry follows its normal rules: a loose file
   deleted on the NAS is deleted by its `(files)` sync, so a new folder `X` is
   picked up on the next run; a folder that became a gone unit stays until
   `prune`. A unit missing an entry this way ends with `result: errors`, the
   entry listed. A renamed folder is a gone unit plus a new
   unit, so it's copied again (rename detection is listed under Later).

1. A unit's name is the folder name exactly as on disk: the NFC and NFD forms of
   a name are different units. In the registry, in records, in contents lists
   and in output, bytes that aren't valid UTF-8 and control characters (tab,
   newline …) are written as `\xNN`, and a backslash as `\\`, so every name
   maps to exactly one written form and back. On the command line, a unit name
   is matched exactly; if nothing matches, Baruch shows similar units (e.g. the
   other Unicode form) and the user picks one; `--skip` refuses instead,
   suggesting them.

1. Rules don't depend on what the folders contain.

1. On each drive, every rule gets its own folder, `/<rule name>/…`, so rules
   never collide. Baruch's own files (label, records, contents list,
   `config.yaml`, `restore.sh`, partial files) live in `/.baruch/`. Rule names
   may only use `A–Z a–z 0–9 _ -`.

1. Sources are absolute paths and never overlap: no rule's source is inside
   another's, also not through a symlink or `..`. Every run and `config check`
   refuse a configuration where they do. Baruch only looks
   inside rule sources: a folder that no rule covers (e.g. a new folder in
   `/volume1/archive`) isn't backed up and isn't reported until a rule is added
   for it.

### Placement

11. A unit gets its home the first time a run sees it without one, whichever
    drive is docked. The new home is a registry change in that run's plan
    (requirement 24). From then on the unit is *copy pending* until its home
    is docked.

01. The home is the first of these that applies:

    1. a drive in the rule's list that already holds data of the unit
       (recorded or not, from the drive's cached contents). If several do, the
       one whose record has the newest `synced` (data without a record ranks
       below any record), then the one earliest in the list. The data of unit `<rule>/<folder>` is the folder
       `/<rule>/<folder>` on the drive (only a folder, never a file of that
       name), matched by exact name (requirements 7 and 9); that of
       `<rule>/(files)` is the entries directly in `/<rule>/` that aren't
       folders.
       Baruch never guesses from the contents: a copy under another name isn't
       found, the unit gets a first sync, and the copy is unexplained data;
    1. the first drive in the list, in order, where the unit fits
       (requirement 13);
    1. none: the unit is **unplaced** ("fits on no drive of rule tv"),
       reported in every plan and in the status, and tried again on every run.

    Drives in the list that aren't registered yet are skipped.

01. A unit fits on a drive if `free − size(U) ≥ reserve`. For the docked
    drive, `free` is its current free space, less what the run's other units
    will take. For a drive that isn't docked, it's the free space recorded at
    its last run, `prune`, `verify` or registration, less what is still to be written there: for
    each unit whose home it is (including units given that home earlier in the
    same plan), its size on the NAS less its size in the
    drive's cached contents (the whole size for a unit with no data there). It's checked again when the drive is docked (requirement 28).
    `size` is the sum of the unit's file sizes on the NAS after excludes (each
    hard-linked file once), measured on every run. The reserve absorbs the
    filesystem's overhead.

01. New units are placed largest first (ties alphabetical), for the best
    packing.

01. Homes are permanent. They change only by `move set` (requirement 19) or
    because a config change takes the home drive out of the rule's list
    (requirement 38); the unit is then placed again by requirement 12.

01. A unit is never split. A unit that doesn't fit on its home when the home is
    docked is skipped and reported, with the `move set` command
    (requirement 28).

01. Units that are copy pending on a drive not docked for longer than
    `stale_after` are listed in the plan and the status ("12 units copy
    pending on BK-6A, last docked …"), with the `move set` command that gives
    them a drive that is docked instead.

### Moves

18. When units grow into a drive's reserve, the end of each run on it and the
    status show how much of the reserve is used, the drive's largest units
    with the date each last changed (the newest modification time in the
    drive's cached contents), the room above the reserve on the other drives
    of their rules, and the command to move one
    (`baruch move set UNIT --to DRIVE`). The user picks; Baruch doesn't
    suggest a unit.

    ```
    BK-6A: existing units have grown into the reserve: 212 GiB of 559 GiB used.
      Largest units on BK-6A:
        tv/The Wire     410 GiB   changed 2026-11-02
        tv/Lost         380 GiB   unchanged since 2025-03
        …
      Room above reserve in rule tv: BK-6B 340 GiB, BK-20A 1.2 TiB
      To move one: baruch move set UNIT --to DRIVE
    ```

01. A move is `baruch move set UNIT --to DRIVE`, which sets the unit's home.
    The new home gets the data when it's docked (a first sync, or an
    incremental one if it already holds older data of the unit). The data on
    the old drive stays and becomes stale data: it isn't updated, and only
    `prune` removes it (requirement 38), once the new home holds a newer
    copy synced without errors (copy first, then delete). `move set` also gives an unplaced
    unit a home, the usual answer to "fits on no drive".

01. `move set` refuses targets that aren't registered, that aren't in the
    unit's rule's list (to use another drive, add it to the list first), the
    unit's current home, and gone units. It warns if the unit doesn't fit.

### Running a backup

21. Backups are always started by hand with `baruch run`. Docking a drive does
    nothing by itself, and there's no scheduled run. Long runs are meant to run
    in tmux or similar.

01. **Plan first.** A run first computes everything it would do on the docked
    drive: new homes, files to copy, overwrite and delete, skipped units. It
    then shows the plan:

    - a home unit that has no data on this drive yet (a **first sync**: new
      unit, move, replacement drive) is one summary line: files and size;
    - for every other home unit, each new, changed and deleted file is listed.
      This includes data without a record on the drive (after the
      switch-over or `register --force`), which is labelled "existing folder,
      no record yet" and counts towards the warnings like any other change.

    The plan compares what's actually on the drive with the NAS; a missing or
    old record never changes what gets copied or deleted.

01. **Warnings** appear at the end of the plan, right above the question
    (requirement 24), where the terminal shows them first, if on this drive:

    - more than `warn_changed_files` (10%) of the files, or
    - more than `warn_changed_bytes` (10%) of the bytes would be overwritten or
      deleted, or
    - any unit would lose more than `warn_unit_loss` (50%) of its bytes to
      overwrites and deletions. This includes unit folders emptied on the NAS,
      and files encrypted in place (same names, new content).

    Baruch's own state folder (`~/.local/state/baruch/`, inside the `home`
    rule) changes during every run, so its changes don't count towards the
    warnings; the plan shows them as one line, also among the deletions
    ("Baruch's own state: 14 changed, 2 deleted"), with the full list in the
    plan file. Its copy on the drive is taken during the run, so it
    lacks that run's final writes ("Losing the NAS state").

    The shares are of the files and bytes on the drive before the run.
    Overwritten files count with their full size.

01. **Confirm.** Before the question, the full plan is saved to the run's log
    file, and a short summary is shown right above the question: the totals
    per unit (copy / overwrite / delete: files and size), the path of the log
    file, the number of units gone on the NAS, per rule ("400 units of rule tv
    gone on the NAS: nothing is deleted, see the plan"), the
    deletions and, last, the warnings. **Deletions** are listed in full,
    whatever their size, grouped by unit: every file or folder the plan
    deletes on the drive. A deleted file whose folder gets a new file in the
    same plan is marked "replaced in the same folder" (e.g. a quality upgrade
    by Sonarr/Radarr). Files deleted because an exclude now matches them are
    one line per unit ("12 excluded files"), listed in the plan file. Past 50
    lines, the rest is counted, with a pointer to the plan file. So a single
    deleted file shows even when no warning fires. The plan ends with a y/n
    question. `n` aborts without changing anything except the NAS's cache of
    the docked drive (requirement 31); the user investigates (e.g. restores
    wrongly changed files) and runs again. `y` carries out exactly the plan:
    Baruch computes the plan itself (both sides listed, compared by size and
    modification time, excludes applied by Baruch), rsync gets exactly the
    planned file list (`--files-from`), and Baruch carries out the planned
    deletions itself. A planned file that changes before it's copied is copied
    as it is at copy time. Files added or deleted on the NAS after the plan was
    made are not handled in this run; they're reported and picked up next
    time. There's no `--yes`, and `run`, like every command that asks y/n,
    refuses to run without a terminal.

    **Registry changes** that touch no files on this drive (a new home, a home
    re-placed after a config edit, units dropped because their rule was
    removed or their folder is now excluded, gone units' entries no drive's
    cache holds data for) are listed in the plan and also need a `y`. They're
    saved right after `y`, before the first unit starts. Only when there's
    neither a file operation nor a registry change does the run skip the
    question. It still does everything else a run does after `y`: the records
    of the units it covers are refreshed, the end-of-run writes happen
    (requirement 31), and whether it's complete follows requirement 31.

    Every home unit the plan covers gets a new record when its sync finishes,
    also when nothing in it changed, so `synced` means "last found complete
    and current", not "last changed".

    `baruch run --skip UNIT` (repeatable) leaves units out of the plan: they're
    shown as "skipped on request" and nothing of them on the drive changes,
    e.g. while the user looks into a suspicious change. Unknown names are
    refused, with similar ones suggested.

01. `baruch run --dry-run` shows the plan and exits. It writes nothing, not even
    `last_seen` or the cache.

01. A run only syncs and deletes inside units whose home is the docked drive,
    and it never deletes a whole unit. Stale data on the drive (requirement
    38: units whose home is another drive, units without a home, units gone
    on the NAS) is left as it is. The end of the run
    reports it: "stale data on BK-6A: 3 units, 1.1 TiB can be pruned
    (`baruch prune BK-6A`); 1 unit held: home BK-6B has no complete copy yet;
    2 units gone on the NAS", and lists unexplained data (requirement 38). To
    free space for this run, `prune` first.

01. **Order** after confirmation: incomplete units → units that already have
    data on the drive → units without data on the drive (first syncs) →
    end-of-run writes. Within each group, largest first. Within a unit,
    deletions come first, then copies.

01. **Space:** right before each unit, the fit check is repeated with the
    drive's current free space: the unit's net bytes (what the run would write
    less what it would delete) must fit in the free space; existing units may
    use the reserve, new ones may not (requirement 13). A unit that doesn't fit
    is never started: it's skipped, its data and record stay as they were, and
    the run's report lists it ("doesn't fit"). If the disk fills up during a
    unit anyway, the unit is *incomplete* and the run stops. Other units are
    never deleted to make room.

01. **Records and errors:** before a unit's sync starts, its record on the
    drive is set to `result: in-progress` with a new `started` time, first on
    the drive, then in the NAS's cache. If either write fails, the unit isn't
    started and the run stops for that drive. When the
    sync finishes, the record becomes `ok` or `errors` with a new `synced`
    time. So a unit interrupted for any reason (Ctrl-C, crash, power cut,
    drive pulled, disk full) is *incomplete* on the drive itself and in the
    cache, and the next run finishes it. Files that vanished during copying,
    or were deleted on the NAS between the plan and the copy → warning, the
    unit counts as synced. Files that can't be read on the NAS (rsync's sending
    side) → `result: errors`, and the files are listed. Read or write errors on
    the drive (rsync's receiving side, a full disk) → the unit stays
    *incomplete* and the run stops for that drive. **Scan errors on the NAS**
    (an unreadable folder, an I/O error, also on a unit folder or loose file
    itself) → no deletions are planned under that path; the plan lists the
    path and the error, and everything else goes ahead; the unit's record gets
    `result: errors`, with the path listed. A unit whose folder
    can't be read is shown as *unreadable*, never gone. A folder on the drive
    that can't be read is reported and nothing in it is changed; the unit's
    record gets `result: errors`, with the path listed, and the run exits
    non-zero and doesn't count as complete. A `units.yaml` on a
    registered drive that is missing or can't be read stops the run before the
    plan; it's never taken as empty. The message names the way out:
    `baruch drive forget NAME`, then `baruch drive register NAME --force`,
    which discards the records; the drive's data then counts as unrecorded
    (never as a backup) until the next run syncs it incrementally. `units.yaml`
    has one record per line in a fixed form,
    `'<unit name>': {started: …, synced: …, result: …}`, with the name in
    single quotes (a `'` in it written as `''`) and the `\xNN`/`\\` form of
    requirement 7 as literal text, never a YAML escape; lines are never
    wrapped, so `restore.sh` can read it with plain `sh`. **Symlinks on the drive** are never followed, by scans or
    deletions: a symlink is deleted as a link, and Baruch refuses to write or
    delete through a folder on the drive that is a symlink. Before each unit,
    Baruch checks that the drive is still mounted and its label still matches;
    otherwise it stops and writes nothing more for that unit. (Otherwise rsync
    could write into the empty mount folder on the NAS's system partition,
    which has under 1 GiB free.)

01. **Ctrl-C** stops rsync. Units already finished stay recorded, the
    end-of-run writes happen, and the next run continues.

01. **The cache** of a drive always comes from what Baruch itself read from the
    drive: the records in its `units.yaml`, and the contents from its own scan
    of the drive at the start of every run, `prune`, `verify` and `register`
    (also when the answer is `n`). After each unit's sync, its folder on the
    drive is scanned again, its new record goes to the drive, and then its
    record and its contents go to the cache together, in one atomic write.
    `prune` does it the other way round: it removes a unit's record on the
    drive, then its record and contents in the cache (one atomic write), and
    only then deletes its data. So if anything is interrupted, the cache errs
    towards "not there", which can only make "files only here" (requirement
    38\) count too many, never too few; the next scan corrects it. The
    `/.baruch/contents` file on the drive is for people and restores; Baruch
    never reads it into the cache.

    At the end of a run, a contents list (all files on the drive), a copy of
    `config.yaml` as read at the start of the run, and a **restore script**
    are written to the drive. The registry and the drive's entry in it (last
    seen, free space, the run's report) are saved on the NAS. A run answered
    with `n` updates only the drive's cache.

    `/.baruch/restore.sh` copies the drive's data back to the NAS without
    Baruch, with `rsync -aH`, unit by unit, to the rule's source as configured
    when the script was written (the rule holding Baruch's own files first,
    since it brings back the configuration and the NAS state). It contains the
    list of units whose home was this drive then, reads `/.baruch/units.yaml`
    when it runs, and restores a unit only if that record says `ok` or
    `errors`; it prints each with its `synced` time. With `--stale` (in a
    second pass, once every drive's home units are restored) it restores
    instead every stale unit that `prune` would hold when the script is
    written (requirement 38: no home, home not registered, home without a
    newer copy synced without errors), with or without a record, labelled with
    `prune`'s reason and the home drive: its data may be the newest or the only
    copy. Because the home copies are already back by then, `--update` lets an
    older stale copy add only the files they lack. Units whose record
    here says `in-progress` are never restored automatically, stale or not. A `(files)` unit restores
    only the entries directly in `/<rule>/` that aren't folders, without the
    marker. If a rule's source folder doesn't exist on the NAS, the script
    creates it with a single-level `mkdir`, only if its parent (the share)
    exists; otherwise it reports the rule and skips it, so it never writes onto
    an unmounted volume. Names are written into the script as `printf` escapes, so plain
    `sh` turns `\xNN` back into the original bytes.
    Everything else it finds on the drive when it runs is listed, not
    restored, with the command to restore it by hand: incomplete units ("sync
    of 2026-11-02 didn't finish; an older complete copy may be on another
    drive"), home units with data but no record ("existing folder, no record
    yet: may be the only copy"), in the first pass the stale units `prune`
    would hold ("restored with `--stale`"), units gone on the NAS when the
    script was written, the stale units `prune` would delete ("home BK-6B has a newer complete copy,
    2026-11-20"),
    unexplained data, and anything newer than the script. Without `--run` it
    only lists what it would copy (`rsync -n`). It never deletes on the NAS
    and keeps files there that are newer than the drive's (`rsync --update`),
    but lists every file it skips for that reason, with a count, in the dry
    run and the real run; `--run --overwrite` copies those too. It never
    restores the `.backup-source` markers (it ends by saying to re-create them
    once every drive is restored, with the `touch` commands), finds the drive
    from its own location (the mount path may differ after a rebuild), and
    uses Homebrew's rsync if it's installed, otherwise the one on the `PATH`,
    refusing anything older than 3.2 or openrsync (the same rule as Baruch).
    If `units.yaml` can't be read, it restores nothing by itself and lists
    everything, with the commands. `prune` and `verify` rewrite it, the records,
    the contents list and the `config.yaml` copy too, from the config as read
    at their start.

    The run ends with a summary in the terminal; its last line is "safe to
    eject in DSM" once everything is written. There are no notifications, and
    the drive is not ejected by Baruch. A run is **complete** when every unit
    whose home is the drive, except gone ones, ended with `ok`, or with
    `errors` only for files on the NAS that couldn't be read (listed). A unit
    that doesn't fit, a unit left out with `--skip`, a rule skipped for a
    missing marker and a unit with an unreadable folder on the drive therefore
    keep the run from being complete; the run's report, kept with
    the drive's entry, says which and why, and the status shows it.

    The same mount check comes first: if the drive is gone, nothing is
    written to it, its cache on the NAS and its recorded free space stay as
    they were, and the run says so. Writes to the drive never create folders
    above `/.baruch/`. If a write fails (e.g. the drive is full), it's
    reported and the registry is still saved on the NAS. rsync writes
    half-copied files to `/.baruch/partial` so they never appear in a unit
    folder; leftovers there are removed at the end of every run.

01. Exit code 0 for success, also with warnings; non-zero only for errors. Each
    real run's log is its plan file (requirement 24) with the results added;
    `prune` and `verify` write one too. Logs are kept for 90 days.

### Status and search

33. `baruch status` shows, per drive: last complete run (and, if the last run
    wasn't complete, which units it left out and why), last seen, oldest unit
    sync, capacity, free space, reserve usage, last verify; across drives: the
    total free space above the reserves. It lists the units that need
    attention: unplaced, copy pending (grouped by drive, requirement 17),
    incomplete, synced with errors, gone (with the `prune` that removes their
    data), and units with no complete data on any drive. It also lists reserve
    usage (requirement 18), stale data per drive (what `prune` would remove
    and what it would hold, with the reason), unexplained data (paths on the
    drive only; `prune` with the drive docked shows the `rm` command), drives no longer in the config that still hold data, drives not
    docked for longer than `stale_after`, and drives whose last verify found
    read errors (with the recommendation to replace them). It works from the
    cache and the last run's scan, and shows how old they are.
01. `baruch find PATTERN` searches the cached contents lists and shows which
    drives hold matching files, each drive's `synced` time for the unit, and
    whether the drive is its home or holds stale data. Forgotten drives aren't
    searched: a file only they held is reported as on no drive.

### Drive lifecycle

35. `baruch drive register NAME` writes a label with the name and a new random
    disk ID to the docked disk, scans the disk into the cache (so existing
    data counts for placement, requirement 12, and leftovers show as stale or
    unexplained data) and records its free space. It changes no data on the
    disk. Records already on the disk (`/.baruch/units.yaml`) are discarded,
    and an old `restore.sh`, `contents` and `config.yaml` copy are removed:
    data without a record counts as older than any complete copy. It accepts
    names that aren't in the config yet (with a warning).
    - `--force` overwrites an existing unknown or invalid label, after showing
      it and asking. It never overwrites the valid label of a registered drive.
      Deleting old data is up to the user (or `prune`).
    - A name whose disk was forgotten (requirement 36) is simply registered
      again on the new disk. A name whose disk is still known can't be taken
      over: a new disk gets a new name (e.g. an upgrade, see "Scenario: a drive
      breaks").
    - **Taking over a disk after losing the NAS state:** if the docked disk's
      label already carries NAME, the registry has no drive of that name, and
      its disk ID isn't a forgotten one, `register` shows the disk ID, what
      the disk holds and its newest `synced` time, and asks to take it over as
      it is: the label and the records on the disk are kept.
01. `baruch drive forget NAME` tells Baruch that the data on the drive's disk
    no longer counts: it broke, got lost, or the user simply doesn't want to use
    it for backups anymore (its data may still be kept). Baruch can't tell these
    apart, so it never forgets a drive by itself. It first shows the units that
    would be left without complete data on any drive, and asks y/n. Then it
    removes the drive's entry and its cache, and adds its disk ID to the
    forgotten ones, so its label is invalid (if the disk turns up again it's an
    unknown disk). Homes don't change: if the config still names the drive, its
    units keep it as home and are copied when a new disk is registered under
    that name (until then they're *copy pending*); once the drive is removed
    from the config, its units are placed again. `forget` doesn't change the
    config.
01. **Taking a drive out of service** is a config change: remove it from all
    rules and from `drives`. Its units are placed again (requirement 38). Its
    data stays, isn't updated, and is stale data: `prune` removes it when the
    drive is docked, once each unit's new home holds a newer copy synced
    without errors. To
    skip that, `drive forget` it. A drive that's no longer in the config stays
    in the registry until it's forgotten; the status lists it while it holds
    data.

### Stale data, config changes and verify

38. **Stale data** is a unit's data that a run doesn't sync: the unit's home is
    another drive, it has no home, or it's gone on the NAS. Runs never delete
    it. `baruch prune DRIVE` removes it from the docked drive, after its own
    plan and y/n (all of it or nothing: to keep one unit, answer `n` and, if
    it's gone on the NAS by mistake, restore its folder there first):

    - **a unit whose home is another drive:** only if the home holds a newer
      complete copy: the home's record (from the cache) has `result: ok` and a
      `synced` time later than both `synced` and `started` of this drive's
      record of the unit (so a newer, interrupted attempt here isn't thrown
      away). Data without a record here counts as older than any complete
      copy. Otherwise it's held, and the plan says why: "home BK-6B has no
      copy yet", "… incomplete", "… synced with errors", "… has an older copy
      (2026-03-01) than this one (2026-11-02)", "BK-6B has no cache: dock it
      first" (the drive has a registry entry but no cache file), "home BK-6B
      isn't registered: no copy";
    - **a unit without a home:** held, "no home yet (`baruch run` places
      it)";
    - **a unit gone on the NAS:** removed, labelled "gone on the NAS". Right
      before deleting, `prune` checks again that the unit's folder is still
      missing on the NAS, and skips it if it's back. The unit's registry entry
      is removed once no drive's cached contents hold data of it (by `prune`,
      or by the next run's plan for a gone unit that never had data).

    `prune` follows the source marker rule (Safety) and the scan-error,
    symlink and mount-check rules of runs (requirement 29): for a rule whose marker is missing, or a
    path with a scan error, nothing under it is deleted, and the plan says
    why; before each unit it checks that the drive is still mounted with the
    same label and disk ID. For each unit it removes the record and the cached
    contents before the data (requirement 31), so an interrupted prune never
    leaves a record claiming a complete copy. If a gone unit's entry is then
    removed while part of its data is still on the drive, that data is
    unexplained data, which Baruch never deletes. Every unit line in `prune`'s plan shows
    "N files only here": files of the unit that have no other backup: they're
    in no other drive's cached contents and in no other unit on this drive.
    The NAS doesn't count, since it's what the backup is for: a renamed
    folder's old copy keeps its files "only here" until the new folder is
    backed up somewhere. A file counts as backed up elsewhere if a file with
    the same name, size and modification time is anywhere in the same rule
    there, so once the renamed folder is synced, its old copy counts as 0. Each unit line also says where Baruch believes the
    files are besides this drive, as last seen: the NAS, and each drive with
    when it was last seen ("also on BK-6B, last seen 2026-10-01", or "on no
    other drive"). The count comes from the cache, so it's only as current as
    each drive's last docking; naming the drives lets the user spot a copy on
    a drive that has broken or changed since. The summary above the
    question totals them ("3 units: 214 files, 12 GiB exist on no other
    drive"); `prune` doesn't use the percentage warnings. Its deletions are
    listed as in requirement 24.

    **Unexplained data** is data on a drive that no current unit explains: a
    folder of a rule that isn't in the config ("rule tv2 isn't in the
    config"), a folder or file under a rule folder that is neither a unit nor
    part of `(files)`, files outside rule folders. Baruch never deletes it, neither in
    runs nor in `prune`: `prune`'s plan and the status list it, with a
    shell-quoted command for the docked drive (`rm -rf -- '/volumeUSB1/usbshare/tv2'`)
    to delete it by hand once the user knows it's not needed. Never stale or
    unexplained: `/.baruch/` and `lost+found` at the drive's root, and
    excluded names outside unit folders (e.g. DSM's `@eaDir`), which are
    ignored; inside a unit folder that `prune` removes, they go with it.

    `baruch config check` validates the configuration without a drive: the
    rules of requirements 5, 9 and 10, sources exist and have markers, and no exclude
    matches `.backup-source`, `.baruch`, `lost+found` or a rule name.

    **Config changes** take effect on the next run, through the same rules:

    - drive removed from a rule's list (also the only drive of a one-drive
      list replaced by another) → the units whose home it was are placed again
      (requirement 12, so a listed drive that already holds their data is
      preferred); the old data becomes stale;
    - drive added to a list, or the list reordered → nothing changes for
      units that have a home; new units follow the new order;
    - source changed → units are matched by name as always: units missing in
      the new source are gone, new ones are placed;
    - rule removed → its units are dropped from the registry, a **warning** in
      the plan ("rule tv removed: 412 units dropped; their data becomes
      unexplained data"); if the rule comes back, requirement 12 puts its units
      back where their data is;
    - rule renamed → a new rule, copied again; the old rule's folder is
      unexplained data, for the user to delete once the new rule is backed up;
    - new exclude → files it matches are deleted in their unit's next sync on
      its home, one line per unit in the deletions, counted in the warnings. A
      unit folder it matches stops being a unit: dropped from the registry
      with a warning that lists its data on each drive (paths, from the
      caches), kept in that run's log. Afterwards the folder is an excluded
      name, ignored on the drives: delete it by hand.

01. `baruch verify` reads every file of the docked drive's home units (not its
    stale or unexplained data), compares
    checksums with the NAS, and reports:

    - read errors on the drive, including files or folders it can't open
      (the count is kept, and the status then recommends replacing the
      drive);
    - files changed on the NAS since the last sync (normal, the next backup
      takes care of them);
    - files whose content differs with the same size and time ("unknown which
      side is right"; `/volume1` is ext4, so the NAS has no checksums to tell).

    The only write it offers is for read errors: "copy these N unreadable
    files again from the NAS? y/n". Changed files are left to the next run.
    Files with unknown correct side are listed with both paths and the commands
    to copy in either direction (shell-quoted, safe to paste); the user
    decides each one. Verify only runs when started (the status shows
    each drive's last verify). It takes the lock, picks the drive like `run`
    (`--drive NAME`), checks the mount before each unit, and handles a re-copy
    like a run's sync, with one difference: the unit's record is set to
    `in-progress` first and gets its previous `result` back afterwards, but
    `synced` stays as it was, since files changed on the NAS weren't copied.
    The contents list, cache, restore script, last seen and free space are
    rewritten afterwards. `prune` records last seen and free space too; a run
    answered `n` doesn't. A full verify of a drive's home units takes ~16–25 hours for BK-20A (~8.3 TiB at
    100–160 MB/s), ~10–17 hours for a full 6 TB drive.

## State and unit states

State is kept in two places:

- **On each drive** (`/.baruch/`): the facts about that drive. `units.yaml`
  has one record per unit synced there (data that came without a sync, e.g.
  after the switch-over or `register --force`, has none), `contents` lists every file. They're written
  only by commands working on that drive (`run`, `prune`, `verify`,
  `register`).
- **On the NAS** (`~/.local/state/baruch/`): the **registry** with the
  intentions (each unit's home) and the drive entries, plus a **cache** of
  each drive's records and contents as last read from it (requirement 31).

```yaml
# /.baruch/units.yaml on BK-6A (cached on the NAS)
'tv/The Wire': { started: 2026-11-02T20:30Z, synced: 2026-11-02T21:14Z, result: ok }           # stale: home is BK-6B now
'tv/Lost':     { started: 2026-11-02T21:20Z, synced: 2026-10-20T18:40Z, result: in-progress }  # last attempt didn't finish
```

```yaml
# Registry on the NAS
units:
  tv/The Wire:   { home: BK-6B }      # moved from BK-6A with `move set`
  tv/Lost:       { home: BK-6A }
  clips/(files): { home: BK-20A }     # the loose files directly in /volume1/archive/clips
drives:
  BK-6A:  { disk_id: 7f3c…9a, last_seen: 2026-11-02T23:50Z, last_complete_run: 2026-10-20T19:02Z,
            capacity: 5.46T, free: 0.34T, last_verify: null, read_errors: 0,
            last_run: { left_out: { tv/Severance: doesn't fit } } }
  BK-6B:  { disk_id: 2b81…c4, last_seen: 2026-11-20T04:12Z, last_complete_run: 2026-11-20T04:12Z,
            capacity: 5.46T, free: 0.92T, last_verify: null, read_errors: 0 }
forgotten: [ 5d02…e1 ]                # disk IDs whose labels no longer count
```

A record's `result` is the outcome of the last attempt: `ok`, `errors` (some files on the
NAS or folders on the drive couldn't be read; they're listed) or `in-progress` (started, not finished). `started` is when
the last attempt began; `synced` is when the last sync that finished (`ok` or
`errors`) ended, and an attempt that doesn't finish leaves it as it was. A unit
that was skipped keeps its record unchanged. Units without a home (unplaced)
and units no rule has any more aren't in the registry.

**Gone** applies to the whole unit; otherwise each unit has exactly one of the
other states, in this order of precedence:

| State | Condition | What happens next |
| --------------- | ----------------------------------------------- | ---------------------------------------------------------------- |
| Gone | The unit is in the registry but its folder is missing on the NAS (an unreadable folder isn't gone, requirement 29) | Its data stays as stale data; `prune` removes it (requirement 38); then its entry is removed |
| Unplaced | No home: fits on no drive of the rule | Placed on a later run once a drive has room, or by `move set` |
| Copy pending | The home has no record of the unit (its data may already be there, e.g. after the switch-over or `register --force`: "existing folder, no record yet"), or a record without data | Copied when the home drive is docked (and it fits) |
| Incomplete | The home's record has `result: in-progress` | Finished on the next run on that drive |
| In place | Otherwise: the home has a complete copy | Normal sync |

"Synced with errors" (`result: errors`) is shown as a note, not a state of its
own. A **complete copy** is data with a record whose `result` is `ok` or
`errors`, where the drive's cached contents hold data of the unit; data
without a record never counts as a backup, and neither does a record without
data (e.g. after the folder was deleted by hand). Such a unit is copy pending,
in the status, in `forget`'s preview and in `prune` ("home BK-6B has no copy
yet"). A gone unit whose folder reappears keeps its home if its entry is still
there; otherwise it's a new unit, placed by requirement 12 (so on the drive
that still holds its data, if any).

- **Writes.** Of a run's changes, only those answered with `y` are written to
  the registry; `n` refreshes only the docked drive's cache, and `--dry-run`
  writes nothing. A run with no file operations and no registry changes needs
  no `y` (requirement 24). `move set` writes at once (it
  changes nothing on a drive); `register --force`, the takeover in
  `register`, `forget`, `prune` and `verify`'s re-copy write after their own
  y/n.

- **Integrity:** one lock per NAS, held by the process (`flock`), so a crash
  never leaves a stale lock. A command that changes the registry takes the
  lock before it reads the registry and keeps it until its last write,
  including while it waits for the y/n answer; another such command meanwhile
  is refused, naming the one that holds it. Read-only commands (`status`,
  `find`) don't take it and work during a run. The registry, the cache and
  the files on the drive are written atomically (temporary file, then
  rename). A version of the registry and the cached records (not the
  cached contents, which come back with the next scan of each drive) is saved
  before each command or run that changes them, and the last 10 are kept. `baruch init`
  creates the registry, and a commented starter configuration if there's none
  yet; it refuses if a registry exists. If the registry is missing or can't be read, Baruch refuses to do
  anything and names the saved versions and `init`; it never starts with an
  empty one by itself. Times are stored in UTC.

- Changes are made with Baruch's commands. Hand edits are possible for
  emergencies; every run validates the file, and a registry of another format is
  refused.

- **Losing the NAS state.** The state is under `/volume1/homes/alex`, so the
  `home` rule backs it up (configuration and state are both in it, on the
  same drive as long as `home` has a one-drive list). That copy is as old as
  the last run of the drive holding `home`, which can be weeks. Everything
  done since is missing from it: homes assigned or moved, drives registered or
  forgotten, verify results. A saved version is the same. Data on the drives
  is never affected, and most of it comes back by itself, since a drive's own
  records replace its cache when it's docked. Two rules for the user, which
  Baruch doesn't check:

  - **After restoring an older state, dock every drive for `baruch run` and
    answer `n` if it asks, before any `y` or `prune`, and `drive forget` every drive that
    can't be docked any more** (broken, given away, relabelled). Until then,
    the cache can describe a disk that has since been replaced, and `prune`
    could trust it. A disk registered since under a name the restored state
    knows with another disk shows as not matching: `drive forget` the name,
    then `drive register` it again, which takes it over as it is
    (requirement 35). A disk registered since under a new name is simply
    taken over.
  - **Don't clone a drive** (`dd` or similar): two disks with one label can't
    be told apart.

  If the state is lost altogether: `baruch init`, then take over every drive
  with `baruch drive register NAME` (requirement 35) **before the first run**.
  The next runs then give every unit its home back where its data is
  (requirement 12); a unit planned as a first sync although its data is on a
  drive not taken over yet means a drive is missing: answer `n`. The
  forgotten disk IDs are lost too, so wipe or `register --force` a forgotten
  disk that turns up again before it can be taken over by mistake; the
  takeover prompt shows each disk's ID and newest `synced` time. Don't run
  `baruch init` before restoring `home` from a drive: `restore.sh` keeps newer
  files on the NAS, so new empty ones would win over the backed-up state.

## Safety

- **Source marker:** each rule's source folder must contain `.backup-source`.
  If it's missing (volume not mounted, folder renamed), the rule is skipped and
  reported ("rule tv skipped: marker missing"), and nothing on the drive is
  touched for it, by runs or `prune`. Its units get no new states, homes or
  registry changes and don't count as gone; the status shows them as of the
  last scan that found the marker.
- **Every command that reads a registered drive** refuses a missing or
  unreadable `units.yaml` (the takeover in `register` too: it stops rather
  than take it as empty), and every command with a y/n refuses to run without a
  terminal.
  `restore.sh` doesn't restore the markers, so a half-restored NAS never looks
  complete.
- **Plan and confirmation** (requirements 22–24): nothing happens before the
  user has seen the full plan, with every deletion listed, and answered y.
  Large changes, including overwrites, are flagged.
- **Runs delete only inside home units, never a whole unit:** a run deletes
  only files that were deleted on the NAS or are now excluded, inside units
  whose home is the docked drive. No move, config edit or folder gone on the
  NAS deletes a unit's data in a run.
- **Copy first:** stale data is only removed by `prune`, and only once the
  unit's home holds a newer copy synced without errors (`result: ok`), or once the unit is gone on the
  NAS, with every file that exists nowhere else counted (requirement 38).
- **Nothing unexplained is deleted:** data no unit explains is only listed,
  with the command to delete it by hand.
- **Records on the drive:** an interrupted sync is recorded as such on the
  drive, and in the cache, before it starts, so incomplete data never looks
  complete. Data without a record never counts as a backup. An older NAS state
  restored from a backup can still describe a drive as it was; that's handled
  by the rule in "Losing the NAS state", not by a mechanism.
- **Symlinks** on the drives are never followed (requirement 29).
- **Mount check** before each unit and before the end-of-run writes, in every
  command that writes to a drive (requirements 29, 31, 38 and 39).

## Configuration

`~/.config/baruch/config.yaml`, edited by hand. `baruch init` writes a
commented starter (settings filled in, no drives or rules, examples in
comments) if no configuration exists yet. Apart from that, Baruch never changes
it: its commands only change the registry, the drives' records and labels.
Everything except TV goes to the 20 TB drive. TV is spread over the two 6 TB
drives, with the 20 TB drive as the last choice.

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
    drives: [BK-20A]

  - name: clips
    source: /volume1/archive/clips
    drives: [BK-20A]

  - name: music
    source: /volume1/archive/music
    drives: [BK-20A]

  # … documents, software the same way

  - name: home
    source: /volume1/homes/alex
    drives: [BK-20A]

  - name: projects
    source: /volume1/projects
    drives: [BK-20A]

  - name: tv
    source: /volume1/archive/tv
    drives: [BK-6A, BK-6B, BK-20A]   # order of preference
```

| Key | Where | Meaning |
| -------------------- | --------- | ----------------------------------------------------------------- |
| `exclude` | settings | Names or `*` patterns (`@eaDir`, `*.tmp`), matched against every file and folder name at any depth, in all rules; applied by Baruch, not rsync |
| `stale_after` | settings | Status flags drives not docked for longer than this, and units copy pending on them |
| `warn_changed_files` | settings | Warning if more than this share of the drive's files would be overwritten or deleted |
| `warn_changed_bytes` | settings | The same for bytes |
| `warn_unit_loss` | settings | Warning if a unit would lose more than this share of its content |
| `reserve` | drives | Free space at which the drive takes no new units (`%` or size) |
| `name`, `source` | rules | Rule name (also its folder on each drive) and source folder |
| `drives` | rules | Ordered list of drives, most preferred first; each unit goes to one of them |

**Why BK-20A is in the TV list:** TV is ~9.1 TiB. The two 6 TB drives hold 10.9
TiB. After ext4's own overhead (~1.5%) and above their 10% reserve, that's
~9.6 TiB if the root reserve is turned off (see "Preparing a drive"), ~9.2 TiB
if not. TV fits today with at most ~0.5 TiB to spare, which TV's growth uses up
within months, and whole shows don't pack perfectly. BK-20A, which holds ~8.3 TiB of the other rules' data, takes the overflow.

**Capacity:** above the reserves and ext4's overhead, the drives hold about
18.2 × 0.985 × 0.95 + 9.6 ≈ 26.6 TiB (root reserve turned off).
After the 2×12 TB upgrade, the NAS has ~27.3 TiB. So the backup is about as
large as the NAS, with no headroom: when the NAS is full, the backup is full
first. The status shows the total free space above the reserves.

When TV outgrows its drives, add a drive to `drives` and to TV's list. When the
rules on BK-20A outgrow it, add a drive to one of their lists (e.g. `movies`:
`drives: [BK-20A, BK-X]`). Its units keep their home on BK-20A, and new ones
can go to BK-X.

## Scenario: a drive's shows outgrow it

Example: shows already on BK-6A keep getting new seasons. Homes don't move on
their own, so the growth lands on BK-6A.

1. **Reserve reached:** BK-6A takes no new shows; they go to BK-6B or BK-20A.
1. **Growing into the reserve:** the end of each run on BK-6A and the status
   show "existing units have grown into the reserve: 38% of 559 GiB used",
   with BK-6A's largest shows and the room on BK-6B and BK-20A
   (requirement 18).
1. **Move:** the user picks one, `baruch move set "tv/Lost" --to BK-6B`. The
   unit is *copy pending*.
1. **BK-6B docked:** the plan shows "tv/Lost — first sync: 214 files,
   380 GiB". Afterwards BK-6A's copy is stale data.
1. **BK-6A docked:** `baruch prune BK-6A` shows "tv/Lost: home BK-6B has a
   newer complete copy (2026-11-20): 214 files, 380 GiB" and deletes it after
   y. Then `baruch run` syncs the remaining shows in the freed space. If BK-6A
   is docked before step 4, `prune` holds the data ("home BK-6B has no copy
   yet").
1. **Ran out of space anyway:** shows that don't fit are skipped and reported
   until there's room (requirement 28).
1. **Every drive in the list full:** register a new drive, add it to `drives` and
   TV's list. New shows go there, and moves free up the old drives.

## Scenario: a drive breaks

Example: BK-6A fails physically. Baruch can't tell "broken" from "in a drawer",
so it never forgets a drive by itself.

1. **Notice:** a run stops with read or write errors on the drive, the drive
   isn't recognised when docked, or the status shows it hasn't been docked for
   a long time.
1. **Forget it:** `baruch drive forget BK-6A`. Its preview shows what then
   exists only on the NAS (e.g. "61 shows, 4.5 TiB have no backup"); after y,
   its entry and cache are dropped and its label becomes invalid.
1. **Either replace it:** prepare the new disk ("Preparing a drive"), dock it,
   `baruch drive register BK-6A`. It takes over the name (the config stays the
   same) and may be larger. The shows whose home is BK-6A keep it (forgetting
   doesn't change the config or homes), so they're *copy pending* and appear
   as "first sync" lines in the plan.
1. **Or do without it:** remove BK-6A from the config (rules and `drives`). Its
   shows are placed again on other drives with room. A rule whose only drive
   was BK-6A needs another drive in its list.
1. **First run:** about 4.5 TiB over USB takes 9–14 hours (100–160 MB/s). It
   can be stopped and continued: each show's record on the drive says whether
   its sync finished.
1. **Old disk turns up again:** its label is invalid, so it's treated as an
   unknown disk. To reuse it: `baruch drive register NAME --force`.

Slow failure (the disk mounts but some files can't be read) is caught by
`baruch verify` (requirement 39). Many errors there make the status recommend
replacing the drive early.

**Upgrading a healthy drive** to a larger disk, e.g. BK-6A (6 TB) to BK-12A
(12 TB):

1. Prepare and register the new disk: `baruch drive register BK-12A`.
1. In the config, put BK-12A where BK-6A was (same place in each list) and
   add it to `drives`; remove BK-6A from the rules.
1. BK-6A's units are placed again (requirement 38). BK-12A has BK-6A's place
   in the preference order, so they go there and are copied from the NAS when
   it's docked.
1. Meanwhile BK-6A keeps its data as the backup (stale data). Dock it once
   BK-12A has had a complete run and `prune` it, or `baruch drive forget BK-6A`
   and remove it from `drives` once `forget`'s preview shows no unit that
   would be left without a backup. Then wipe or keep the disk.

## Scenario: the NAS is lost

Example: the volume crashes, a disk upgrade fails, or ransomware encrypts the
shares. Baruch isn't needed for the restore: the drives hold plain files.

1. **Set up the NAS** again (DSM, shared folders, your user, Homebrew rsync).
   Restore onto empty or freshly created shares: delete damaged data (e.g.
   encrypted files) first, since the restore keeps files on the NAS that look
   newer than the drive's.
1. **Dock BK-20A first** and run its restore script, which copies back `home`
   first. This also brings back Baruch's configuration and state
   (`~/.config/baruch/`, `~/.local/state/baruch/` are inside
   `/volume1/homes/alex`), as of the last run on BK-20A ("Losing the NAS
   state"). Don't run `baruch init` before this.
1. **Copy back each drive** with its restore script: `sh .baruch/restore.sh`
   shows what it would copy, `sh .baruch/restore.sh --run` copies. Edit the
   targets at the top first if the NAS has different paths now. By hand, it's
   one `rsync -aH` per unit folder, e.g.
   `rsync -aH "/volumeUSB1/usbshare/tv/The Wire/" "/volume1/archive/tv/The Wire/"`
   (create `/volume1/archive/tv` first if it doesn't exist).
   The script restores the complete units whose home was that drive, and
   prints each with its `synced` time; everything else (incomplete units,
   units gone on the NAS, stale units `prune` would delete, home units without
   a record, unexplained data) is listed with the
   command to restore it by hand, in case a home drive is lost too. For every
   unit a script lists as incomplete, restore it by hand from the drive with
   the newest complete copy (its `units.yaml`, or `baruch find` once the state
   is back) before re-creating the markers. It also
   lists every file it skipped because the NAS copy is newer;
   `--run --overwrite` copies those too.
1. **Second pass:** once every drive's home units are back, run each script
   again with `--run --stale`. It restores the stale units `prune` would have
   held (their data may be the newest or the only copy, e.g. a unit moved
   whose new home never got it). Where a newer home copy is already back,
   `--update` only adds files it lacks, which may include files deleted since
   the older copy; the script names each unit's home drive, so they can be
   checked.
1. **Without BK-20A:** each drive's `units.yaml` and `config.yaml` show its
   rules and the units it holds.
1. **Duration:** all ~17 TiB takes about 33–52 hours of copying (100–160 MB/s).
1. **Afterwards:** once every drive is restored, re-create the `.backup-source`
   markers (the scripts print the `touch` commands; they don't restore the
   markers, so no rule looks complete while it's half restored). `rsync -aH`
   keeps the modification times, so the next `baruch run` is incremental.
   After restoring an older NAS state, dock every drive for `baruch run` and
   answer `n` if it asks, before any `y` or `prune` ("Losing the NAS state").

## Restoring files

The drives hold plain files, so restoring doesn't need Baruch.

1. `baruch find "S01E01"` shows which drives hold matching files, when each
   drive's copy was synced, and whether it's the unit's home (from the cache on
   the NAS).
1. Dock that drive and copy the files back with Homebrew's rsync, e.g.
   `rsync -aH "/volumeUSB1/usbshare/tv/The Wire/" "/volume1/archive/tv/The Wire/"`.
1. Without the NAS state: each drive's `/.baruch/units.yaml` shows when each of
   its units was synced.

## Preparing a drive

Baruch runs as a normal user and doesn't check these; they're one-off steps by hand.

1. Run an extended SMART test.
1. Format it as ext4 in DSM, and make sure the user Baruch runs as has read/write access to its
   `usbshare` shared folder.
1. Turn off ext4's root reserve (5% by default, ~280 GiB on a 6 TB disk, which
   a normal user can't use): `sudo tune2fs -m 0 /dev/<partition>`.
1. On a disk that already holds data: `sudo chown -R $USER` on it.
1. `baruch drive register NAME`.

## Starting from an existing backup

A disk that already holds a copy of the data (Hyper Backup, an earlier rsync,
a copy by hand) is taken over without copying everything again. Nothing in
Baruch is specific to where the copy came from: it uses only `mv` on the disk
and Baruch's normal handling of data without a record (requirements 12, 22,
35 and 38).

**Prerequisite:** below each source, the copy has the same structure as on the
NAS (the copy of `/volume1/archive/tv/The Wire/S01/…` is at some
`…/tv/The Wire/S01/…` on the disk). Where on the disk it sits doesn't matter.
The disk is ext4.

1. Stop whatever wrote the old backup, for good.
1. `sudo chown -R $USER` on the disk: backup tools often write as root.
1. Write the config: a rule for each source on the disk, with its marker, and
   the drive under `drives` and in each of those rules' lists; then
   `baruch config check`. The rule's name links the two sides: rule `home`
   with source `/volume1/homes/alex` is `/home` on every drive.
1. For each rule, move the copy of its source to `/<rule name>` at the disk's
   root with `mv` (instant, same disk): the name of the rule, not of the source
   folder. Delete what's left of the old backup's own folders; if you leave
   them, `prune` and the status list them as unexplained data, with the `rm`
   command.
1. Prepare it like any disk ("Preparing a drive"; the root reserve can be
   turned off on a disk with data) and `baruch drive register NAME`. This
   writes the label and an empty `/.baruch/units.yaml`, scans the disk into the
   cache and records its free space; no data changes.
1. `baruch run --dry-run`, then `baruch run`. A unit without a home whose data
   is on this disk gets it as home (requirement 12, even if a drive earlier in
   the list has room). Its data is synced incrementally, listed file by file as
   "existing folder, no record yet". Files on the disk that the NAS no longer
   has are in the deletions (requirement 24). A unit that ends up at the wrong
   path shows as many deletions plus a first sync elsewhere, with warnings, so
   a wrong `mv` shows before anything happens. If the old backup didn't keep
   modification times, every file shows as changed: a full recopy, not a loss.
1. After `y`, each unit's record is written when its sync finishes, and the run
   ends with the contents list, `config.yaml` and `restore.sh` on the disk. From
   then on it's a drive like any other.
1. Data the run doesn't sync: units whose home is another drive (placed
   before this disk was registered) are stale data, which `baruch prune NAME`
   removes after its own plan; the data has no record here, so a home with a
   complete copy always counts as newer. Folders no rule or unit explains are
   unexplained data: the status lists them, and `prune` with the drive docked
   shows the `rm` command, for you to delete by hand.

### The switch-over from Hyper Backup

BK-20A stays untouched until the 6 TB drives hold their data, so a complete (if
older) backup exists the whole time.

1. A last manual Hyper Backup run (done 2026-11-14). BK-20A is ext4.
1. Upgrade the NAS disks to 2×12 TB, which frees the two 6 TB disks. Baruch
   isn't used until at least one 6 TB drive is ready (a spare one may already
   exist).
1. Prepare the 6 TB disk(s) (see "Preparing a drive") and register them as
   BK-6A (and BK-6B).
1. Back up to BK-6A, then BK-6B. TV shows get their homes in preference order
   and are copied from the NAS (~9 TiB in total, about a day). BK-20A isn't
   registered yet, so the other rules' units and shows that fit on neither
   6 TB drive stay unplaced.
1. Retire the Hyper Backup task.
1. Take over BK-20A as above: the folders in Hyper Backup's task folder move to
   the drive's root (`…/archive/movies` → `/movies`, `…/archive/tv` → `/tv`,
   `…/homes/alex` → `/home`), and the drive is registered as BK-20A.
1. Back up to BK-20A. The unplaced units get BK-20A as home, and since their
   data is already there, they're synced incrementally.
1. `baruch prune BK-20A` deletes the TV folders of shows whose home is a 6 TB
   drive.

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
  copied as links, and hard links are kept within a unit. Owners and Synology ACLs aren't kept, because Baruch runs as a normal user.
- **User and locations:** Baruch runs as a normal user, without root. Files
  it can't read are reported (requirement 29). Configuration in
  `~/.config/baruch/`, state (registry and its versions, the drives' cache,
  logs) in `~/.local/state/baruch/`. Both locations can be overridden (tests).
- **Released version:** tag a release, run `pythonformula`. After generating the
  formula for the first time, add `depends_on "rsync"` and
  `depends_on "libyaml"` (for PyYAML; `brew audit --strict` asks for it) to it
  by hand; `pythonformula` keeps them on later updates. On the NAS, once
  `brew tap infogrind/tap`, then `brew install` / `brew upgrade`.
- **Development version on the NAS:** `brew install uv` once, then
  `uv tool install --force git+https://github.com/infogrind/baruch@<branch>`.
  It's separate from the Homebrew release, so switching back is easy.

NAS-specific parts sit behind small replaceable pieces, so everything else runs
on the Mac:

| NAS-specific part | On the NAS | In tests |
| --------------------- | ---------------------------------------------- | ----------------------------------------------------------- |
| Finding docked drives | Scan `/volumeUSB*/usbshare*` for label files | Configurable search path; drives are temp folders with a label file |
| Free space | Read from the filesystem | Fake capacity per drive, computed from the drive's files |
| Mount check | Path is a mount point and the label matches | Temp folder exists and the label matches |

## Testing

All tests run with `uv run pytest`, on synthetic data of a few megabytes
generated per test, on the Mac (except the few listed below) and on Linux in
GitHub Actions on every push. CI uses the distribution's rsync (≥ 3.2), found on
the `PATH`.

**Two kinds of tests**

1. **Logic tests (most of them):** the planner takes the configuration, the
   registry, the drives' cached records and file listings (path, size,
   modification time) of the source and the drive, and returns the plan. They
   cover placement, moves, prune, the warnings, space and skipping, without
   touching files.
1. **End-to-end tests:** a generated fake NAS and two or three fake drives in a
   temp folder, with real rsync and the real scanner. Tests "dock" drives in
   sequence, answer the plan's question, and check the result.
1. **Fuzzing** (`tests/fuzz/`): `hypothesis` generates sequences of what a
   user and bad luck can do (edit the NAS, back up, prune, move, change the
   config, crash or interrupt rsync, fill the disk, forget a drive, roll back
   or lose Baruch's state) and shrinks any failure to the shortest sequence,
   printed as code. Derandomized by default, so every run and CI tries the
   same sequences; `BARUCH_FUZZ=explore` runs many more with random seeds,
   `--hypothesis-seed=N` a given one, and a printed `@reproduce_failure` line
   replays one exactly.

**The lost-data check.** Every Baruch command a test runs
(`tests/support.py`, `Env.run`) is followed by a check that fails the test if
the NAS changed, if anything was written into an empty mount point, if a file
version vanished from the NAS and every drive, or if the last backup of a file
the NAS still has vanished, unless the command's output named it beforehand
(listed as a deletion or overwrite in a run's plan, or counted as "files only
here" in prune's). Files are compared by content within their rule, so a
renamed folder copied again isn't a loss.

**Test data is built in the test code**, through helpers on the fake NAS
(`tests/support.py`), so each test shows its starting state and multi-step
scenarios read top to bottom. Nothing generated is stored in the repo.

```python
def test_second_run_lists_each_changed_file(env):
    drive = setup_movies(env)  # a few movies, a one-drive rule, BK-20A registered
    env.backup(drive)  # dock, run, answer y, undock

    env.nas.replace("archive/movies/Heat (1995)/S01E01.mkv")  # version 2
    env.nas.delete("archive/movies/Heat (1995)/S01E02.mkv")
    r = env.backup(drive)

    assert "~ Heat (1995)/S01E01.mkv" in r.out
    assert "- Heat (1995)/S01E02.mkv" in r.out
```

| Call | What changes | Purpose |
| ---------------------------------- | -------------------------------- | ------------------------------------------------------------------ |
| `nas.replace(f)` | Content and timestamp, same size | Normal case |
| `nas.replace(f, size=…)` | Size too | Quality upgrade by Radarr/Sonarr |
| `nas.replace(f, keep_mtime=True)` | Content only | Documents the limit of rsync's size-and-timestamp check: not detected by a run, found by verify |

Further helpers: `nas.add`, `nas.add_show`, `nas.delete`, `nas.rename`,
`nas.mkdir`, `nas.mark`, `nas.remove_marker`.

- File content is derived from path + version number and timestamps are fixed,
  so every version is really different content. No zero-filled files.
- Sizes are small (bytes to a few hundred KB). Drive capacities are set per
  test. Free space is computed from the files on the fake drive against its
  fake capacity, not from the real filesystem. Running out of space mid-unit
  is tested by replacing rsync's result with a disk-full error.

**Odd file names** (`tests/e2e/test_names.py`), based on real cases on the NAS:

- umlauts in both Unicode encodings (NFC and NFD), and a name that isn't valid
  UTF-8;
- spaces, `'`, `&`, `[ ]`, very long names;
- `@eaDir`, `.DS_Store`, `#recycle` (must be skipped);
- empty folders, symlinks, an unreadable file.

The Mac's file system can't create invalid UTF-8 names and handles the two
umlaut encodings differently from ext4, so those tests are skipped on macOS and
run only on Linux in GitHub Actions.

Other simulated failures: a forgotten or unknown drive, a drive vanishing
mid-run (between units and during rsync), unreadable files and folders on
both sides, rsync's error messages for drive write errors and vanished files,
Ctrl-C, and a lost NAS state (drives taken over again, homes derived from
their data).

## Later (not planned now)

- Rename detection: a gone unit and a new unit with exactly the same files and
  sizes are offered as a rename (instant on the drive) instead of a recopy.
- Rolling verify: each run also verifies a slice of the units not verified for
  the longest time.
- Several copies of a source, each on different drives. If it's ever needed:
  allow two rules with the same source and no drive in common, each
  independent (own folder on the drives, own units and homes). There's no room
  for it today: the backup is already about as large as the NAS.
- Notifications (DSM or email) at the end of a run.
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
