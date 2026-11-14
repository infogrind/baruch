# NAS Backup Plan

2026-11-13

## Context

The backup to the single 20 TB USB drive is about to run out of space: the backed-up data is already ~19 TB. Plan: replace it with a rule-based rsync backup that spreads data over several drives, docked one at a time.

- **NAS:** mynas, DS1522+, SHR-1 with 5×6 TB (~24 TB usable), 83% used. Planned: two disks replaced by 12 TB (~30 TB usable), which frees two 6 TB disks for backup.
- **Current backup:** one Hyper Backup task, "Main Backup", single-version (a plain mirror, deletions carried over). It backs up `/archive`, `/homes/alex` and `/projects` to the 20 TB drive.
- **Drives:** connected one at a time through a USB dock that takes a bare 3.5" disk.
- **Important data** also has an offsite copy in a cloud git repo, so this plan doesn't treat it separately.

| Folder in /archive | Size (TB) |
|---|---|
| tv | 10.0 |
| movies | 8.3 |
| clips | 0.24 |
| everything else (music, documents, software…) | ~0.3 |
| **total** | **~18.9** |

`/homes/alex` and `/projects` add at most a few hundred GB (not measured exactly).

## Concepts

The system knows nothing about TV, movies or particular drive sizes. Those details live only in the configuration.

| Term | Meaning |
|---|---|
| Drive | A backup disk, identified by an ID stored on the disk (e.g. `BK-20A`), not by its DSM mount name |
| Rule | "This source folder goes there": source, optional excludes, placement |
| Unit | The smallest thing assigned to a drive: a rule's whole source, or each of its subfolders (e.g. each show) |
| Registry | Records where each unit should be and where copies exist; managed by the system, editable by hand |

## Requirements

### Drives

1. Only drives with a registered ID are written to. Any other disk in the dock is ignored.
1. Each drive has two limits: at the **reserve**, it stops taking new units; at the **move limit**, the status report suggests moving units off it.
1. Only one drive needs to be connected at a time. A run works on whatever is docked.

### Rules

4. A rule has a source folder, optional excludes and a placement:

   - **pin**: the whole source goes to one named drive, or to several (a full copy on each);
   - **spread**: each subfolder is a unit and goes to exactly one drive from an ordered list.

1. Rules don't depend on what the folders contain.

1. On each drive, every rule gets its own folder, `/<rule name>/…`, so rules never collide.

### Placement (spread rules)

7. A new unit is assigned the first time it's seen, to the docked drive, if that drive has room above its reserve.
1. The list is in order of preference. A drive lower in the list only takes new units once the drives above it were full at their last backup.
1. Assignments are permanent. They change only by editing the registry, by a confirmed move, or when a drive is retired (its units become unassigned again).
1. A unit that fits nowhere is reported, not split.
1. On a shared drive, pinned rules get the space first. Spread units only use what's left beyond the reserve.

### Copying

12. Each drive is an exact mirror of the units whose home it is. New, replaced and deleted files on the NAS are carried over when that drive is docked.
01. A moved unit is only deleted from its old drive after the new drive has a confirmed copy (copy first, then delete).
01. If a drive runs out of space during a run, the units that don't fit are skipped and reported; their previous copy stays. A unit is never left half-copied, and other units are never deleted to make room.
01. The registry snapshot and a list of contents are written to every drive.

### Operation

16. Docking a registered drive starts a run. There's a notification at the start and at the end, and the drive is ejected when the run is done.
01. Status report: last backup per drive, free space, units not yet backed up or outdated, suggested moves, and drives not docked for longer than `stale_after`.
01. A dry run shows what would be copied, deleted and assigned, without doing it.
01. Setting `rebalance: suggest | auto`. With *suggest* (the default), each move is confirmed by hand. With *auto*, suggested moves are carried out automatically, still copy-first.

### Drive failure

20. Each drive's label holds its name and a random **disk ID** generated at registration. A disk whose ID doesn't match the registry is never written to, and you're notified.
01. A run that hits read or write errors stops for that drive and reports it.
01. A drive is only declared **lost** by hand (`baruch drive lost <drive>`). This removes all its copies from the registry and lists the units that now have no backup, until they're copied again.
01. **Replacement:** a new disk can take over a lost drive's name (`baruch drive register <drive> --replace`), so the configuration stays unchanged. All units whose home is that drive become *copy pending*.
01. **Retiring:** a retired drive's units become *waiting* and are reassigned through normal placement.
01. **Verify run:** `verify` reads every file on the docked drive and compares it with the NAS; differing files are copied again and errors are counted in the status. With `verify_every` (e.g. 90d), a normal run also verifies once that interval has passed.

## Registry and move states

Each unit stores where it **should be** (`home`) and where copies **actually exist** (`copies`). Every state follows from comparing the two; there's no separate state field that could fall out of step.

```yaml
units:
  tv/The Wire:
    home: BK-6B                 # where it should be (you or the planner set this)
    copies:                     # where it actually is (only a completed run writes here)
      BK-6A: { synced: 2026-11-02, size: 410G }
      BK-6B: { synced: 2026-11-20, size: 410G }

  tv/Lost:
    home: BK-6A
    proposed: BK-6B             # suggestion, not confirmed yet
    copies:
      BK-6A: { synced: 2026-11-02, size: 380G }

  tv/Severance:
    home: null                  # new show, no drive had room yet
    first_seen: 2026-11-15
    copies: {}

  tv/Firefly:
    home: BK-6A
    gone: 2026-11-10            # folder no longer on the NAS
    copies:
      BK-6A: { synced: 2026-10-01, size: 95G }

drives:
  BK-6A:  { disk_id: 7f3c…9a, last_seen: 2026-11-02, capacity: 5.45T, free: 0.21T }
  BK-6B:  { last_seen: 2026-11-20, capacity: 5.45T, free: 1.02T }
  BK-20A: { last_seen: 2026-11-18, capacity: 18.2T, free: 8.90T }
```

| State | Condition | What happens next |
|---|---|---|
| Waiting | `home` is empty | The next docked drive with room becomes `home` |
| In place | `copies` holds `home` only | Normal sync |
| Suggested | `proposed` is set | Nothing until confirmed, which sets `home` to the proposed drive |
| Copy pending | `home` isn't in `copies` | Copied when the `home` drive is docked |
| Cleanup pending | `home` is in `copies`, plus other drives | Deleted from the other drives when they're docked |
| Outdated | Copy on `home` is older than the last change on the NAS, after a run skipped it | Reported; synced once there's room |
| Gone | `gone` is set | Deleted from every drive in `copies`, then the entry is removed |

A move is just setting `home` to the new drive. It then passes through *copy pending* and *cleanup pending* back to *in place*.

- `copies` is only updated after a successful sync of that unit, so an interrupted run leaves the old state and the next run repeats the step.
- The registry on the NAS is the one that counts. The snapshot on each drive is for recovery, and the contents lists let you rebuild the registry.
- By hand you only ever edit `home` and `proposed`. Everything else is written by runs.

## Failsafes

Two checks protect the backup from a missing source and from mass deletion.

**1. Source check** (before anything else)

- Each source folder must exist and contain a marker file, `.backup-source`.
- If the marker is missing (volume not mounted, folder renamed), that rule is skipped and reported. Nothing on the drive is touched for it.

**2. Delete limit** (measured in the dry-run step)

- Counts everything the run would delete or overwrite on the drive. Above `max_delete` of the backed-up data, deletions are held back.
- Planned removals don't count: confirmed moves (*cleanup pending*) and *gone* units.
- New and changed files are still copied; only the deletions wait.
- On trigger: a notification with the amount and the largest folders, plus the full list in a file. You approve once (a command, or a flag on the next dock) and the deletions go ahead. Without approval, the old files simply stay.

```yaml
defaults:
  max_delete: 5%
rules:
  - name: tv
    max_delete: 10%     # looser for TV: quality upgrades replace a lot of data
```

## Configuration

Everything except TV is pinned to the 20 TB drive. TV is spread over the two 6 TB drives, with the 20 TB drive as the last choice.

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
  notify: dsm          # or email

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
    spread: [BK-6A, BK-6B, BK-20A]   # preference order
    max_delete: 10%
```

**Why BK-20A is in the TV list:** TV is 10.0 TB, but two 6 TB drives hold ~10.9 TB formatted, or ~9.8 TB after the 10% reserve, so TV doesn't fit on them even today. Without TV, the 20 TB drive holds ~9 TB and takes the overflow.

**Capacity:** ~31 TB of backup space for ~19 TB of data, roughly the NAS's ~30 TB after the 2×12 TB upgrade. When the NAS grows beyond that, add a drive to `drives` and to `spread`; the rules stay the same.

## Scenario: a drive's shows outgrow it

Example: shows already on BK-6A keep getting new seasons. Assignments don't move on their own, so the growth lands on BK-6A.

1. **Reserve reached:** BK-6A stops taking new shows; they go to BK-6B or BK-20A. The report flags "existing shows growing into the reserve".
1. **Move limit reached:** the report suggests moving the fewest shows that bring BK-6A back under its reserve, preferring finished shows. The suggestion is recorded as `proposed`.
1. **Move confirmed:** `home` is set to the new drive (*copy pending*).
1. **New drive docked:** the shows are copied there (*cleanup pending*).
1. **BK-6A docked:** the old copies are deleted first, then the remaining shows are synced in the freed space. If BK-6A is docked before step 4, nothing is deleted.
1. **Ran out of space anyway:** shows that don't fit are skipped and stay *outdated* until there's room (requirement 14).
1. **Every drive in the list full:** register a new drive, add it to `drives` and `spread`; new shows go there, and moves free up the old drives. To retire a drive, move all its shows off it, then remove it from the configuration.

## Scenario: a drive breaks

Example: BK-6A fails physically and has to be replaced. The system can't tell "broken" from "in a drawer", so it never declares a drive lost by itself.

1. **Notice:** a run reports I/O errors and aborts, the drive isn't recognised when docked, or the status reports it hasn't been docked for a long time.
1. **Declare it lost:** `drive lost BK-6A`. Its copies are dropped from the registry, and the status lists what now exists only on the NAS (e.g. "61 shows, 4.9 TB have no backup").
1. **Either replace it:** SMART-test the new disk, dock it, `drive register BK-6A --replace`. It takes over the name (configuration unchanged), may be larger, and all of BK-6A's shows become *copy pending*.
1. **Or retire it:** `drive retire BK-6A` and remove it from the configuration. Its shows become *waiting* and go to other drives with room as they're docked.
1. **First run:** about 5 TB over USB takes roughly 7–9 hours. It can be stopped and continued, since each show is only recorded once fully copied.
1. **Old disk turns up again:** its disk ID no longer matches, so nothing is written and you're asked to wipe it or register it as a new drive.

Slow failure (the disk mounts but some files can't be read) is caught by the verify run (requirement 25). Many errors there means the status recommends retiring the drive early.

Tests: a lost drive is simulated by deleting its temp folder, read errors by a file that can't be read.

## Tech stack

The tool is called **Baruch**, after Jeremiah's scribe, who wrote the burned scroll out again (Jeremiah 36). Python, kept on GitHub and released to the NAS through the personal Homebrew tap (`infogrind/homebrew-tap`, published with `pythonformula`).

| | NAS (DSM 7.2.2, x86_64) | MacBook |
|---|---|---|
| System Python | 3.8 (too old, not used) | – |
| Homebrew Python | 3.15 | 3.11–3.14 |
| rsync | DSM 3.1.2 | `/usr/bin/rsync` is openrsync (behaves differently) |
| uv | not installed yet | installed |

- **Language and tooling:** Python ≥ 3.12, uv with hatchling as build backend, ruff, pytest.
- **Dependencies:** PyYAML is the only Python library. rsync runs as a separate program: the formula depends on Homebrew's `rsync`, so the Mac and the NAS use the same version, never `/usr/bin/rsync`.
- **Released version:** tag a release, run `pythonformula`. On the NAS, once `brew tap infogrind/tap`, then `brew install` / `brew upgrade`.
- **Development version on the NAS:** `brew install uv` once, then `uv tool install --force git+https://github.com/infogrind/baruch@<branch>`. It's separate from the Homebrew release, so switching back is easy.
- **Automatic start:** a DSM Task Scheduler entry runs `baruch run` every few minutes; it does nothing if no registered drive is docked.

NAS-specific parts sit behind small replaceable pieces, so everything else runs on the Mac:

| NAS-specific part | On the NAS | In tests |
|---|---|---|
| Finding docked drives | Scan `/volumeUSB*/usbshare*` for label files | Configurable search path; drives are temp folders with a label file |
| Free space | Read from the filesystem | Fake capacity per drive |
| Eject | DSM eject command | Recorded, nothing done |
| Notifications | DSM notification or email | Collected for the test to check |

## Testing

All tests run on the Mac with `uv run pytest`, on synthetic data of a few megabytes generated per test. GitHub Actions also runs them on Linux on every push.

**Two kinds of tests**

1. **Logic tests (most of them):** configuration + registry + drive state in, planned actions out. They cover assignment, moves, the delete limit and running out of space, without touching files.
1. **End-to-end tests:** a generated fake NAS and two or three fake drives in a temp folder, with real rsync. Tests "dock" drives in sequence and check the result.

**Test data descriptions** (in `tests/data/`) describe only the starting state. A helper builds the tree in a temp folder, in milliseconds. Nothing generated is stored in the repo.

```yaml
scale: 1MB = 1TB                 # same proportions as mynas
tree:
  archive/movies: { folders: 40, size: 8.3MB }
  archive/tv:
    - { name: "Show A", seasons: 5, episodes: 10, size: 1.2MB }
    - { name: "Show B", seasons: 1, episodes: 8,  size: 0.1MB }
    - { generate: 60, total: 8.7MB }        # shows with random sizes
  homes/alex: { files: 200, size: 0.2MB }
```

- Drive capacities are scaled the same way (BK-6A = 5.45 MB), so real situations carry over 1:1.
- File content is derived from path + version number and timestamps are fixed, so every version is really different content. No zero-filled files.
- A few descriptions cover the starting states, e.g. `base.yaml`, `nearly_full.yaml`, `odd_names.yaml`.

**Changes happen in the test code**, through helpers on the fake NAS, so multi-step scenarios read top to bottom:

```python
def test_replaced_episode_is_synced(nas, drives, run):
    nas.build("base.yaml")
    bk6a = drives.register("BK-6A", capacity="5.45MB")
    run(dock=bk6a)

    ep = "archive/tv/Show A/Season 1/S01E01.mkv"
    nas.replace(ep)                      # version 2: new content, new timestamp
    run(dock=bk6a)

    assert bk6a.read("tv", ep) == nas.read(ep)
```

| Call | What changes | Purpose |
|---|---|---|
| `nas.replace(ep)` | Content and timestamp, same size | Normal case |
| `nas.replace(ep, size="+30%")` | Size too | Quality upgrade by Radarr/Sonarr |
| `nas.replace(ep, keep_mtime=True)` | Content only | Documents the limit of rsync's size-and-timestamp check: not detected |

Further helpers: `nas.add`, `nas.delete`, `nas.rename`, `nas.add_show`, `nas.remove_marker`.

**Odd file names** (`odd_names.yaml`), based on real cases on the NAS:

- umlauts in both Unicode encodings (NFC and NFD), and a name that isn't valid UTF-8;
- spaces, `'`, `&`, `[ ]`, very long names;
- `@eaDir`, `.DS_Store`, `#recycle` (must be skipped);
- empty folders, symlinks, an unreadable file.

The Mac's file system can't create invalid UTF-8 names and handles the two umlaut encodings differently from ext4, so those tests are skipped on macOS and run only on Linux in GitHub Actions.

**Possible later:** a `snapshot` command that records the NAS's real structure (names and sizes, names anonymised) as a test data description.

## Switching over and open points

The 20 TB drive already holds a plain mirror, so nothing has to be copied again in full.

1. Register BK-20A and move its existing folders into the `/<rule>/…` layout. This is instant, because it's the same disk.
1. Run an extended SMART test on the two 6 TB disks freed by the 12 TB upgrade, then register them as BK-6A and BK-6B.
1. Do a dry run per drive, then real runs. As shows get assigned to the 6 TB drives, TV is removed from BK-20A.
1. Turn off the Hyper Backup task once all three drives have had a complete run.

**Open points**

- Measure `/homes/alex` and `/projects` exactly

- Check free space on the 20 TB drive the next time it's docked

- Choose a notification channel: DSM or email

- Decide whether it runs as alex or root (test early whether ejecting a USB drive in DSM needs root)

- Create the GitHub repo infogrind/baruch
