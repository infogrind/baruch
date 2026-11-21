"""The commented starter configuration that `baruch init` writes."""

STARTER = """\
# Baruch configuration. Edit it by hand; check it with `baruch config check`.
# Sizes are binary: 500G means 500 GiB. Shares are percentages.

settings:
  # Names or * patterns, matched against every file and folder name at any
  # depth, in all rules.
  exclude: ["@eaDir", "#recycle", ".DS_Store", ".TemporaryItems", ".apdisk"]
  stale_after: 30d          # status flags drives not docked for longer
  warn_changed_files: 10%   # warn if more of a drive's files would change
  warn_changed_bytes: 10%   # the same for bytes
  warn_unit_loss: 50%       # warn if a unit would lose more of its content

# One entry per backup drive. The reserve is the free space at which a drive
# takes no new units, as a share (10%) or a size (500G).
drives: {}
#  BK-20A: { reserve: 5% }
#  BK-6A:  { reserve: 10% }

# One rule per folder to back up. Its source must contain a .backup-source
# marker file, and no source may lie inside another. `drives` is a list in
# order of preference: each subfolder (unit) goes to one of them, the first
# with room. A list with one drive sends everything there.
rules: []
#  - name: movies
#    source: /volume1/archive/movies
#    drives: [BK-20A]
#
#  - name: tv
#    source: /volume1/archive/tv
#    drives: [BK-6A, BK-6B, BK-20A]
"""
