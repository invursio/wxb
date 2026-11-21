# Vendored code: Lution

The modules in this folder are copied from **Wookhq/Lution**
(https://github.com/Wookhq/Lution), commit `303be659833a`, under the MIT licence
(see `LICENSE.lution`):

    Copyright (c) 2026 WookHQ

Local changes, all mechanical or bug-fix only:

* a provenance header on each file;
* `import x` -> `from . import x` for the package's own modules;
* `sober.py`: the duplicated `remote_commit` was removed, `_run` returns None
  on a missing `flatpak` binary instead of raising FileNotFoundError into the
  UI, and the remaining `remote_commit` treats that None (also a timeout) as
  "offline" instead of reading `.returncode` off it;
* `launcher.py`: `launch_plain` takes the same `stdout`/`stderr` arguments as
  `launch`, so the bridge can hand the game null streams;
* `fonts.py`: adapted from upstream `fonts.py` (the upstream file imports the
  `emoji` package only for an exclusion list, which is inlined here). The
  font-file checks (suffix, sfnt header, 12 MB cap), hardlink placement and
  clearing the kept copy on restore are WXB's; the APK font list and the
  overlay layout are upstream's;
* `cli.py` (not upstream) is WXB's JSON bridge to these modules. Its `launch`
  gives the game DEVNULL streams: the UI reads the bridge's stdout until EOF,
  and a game inheriting that pipe would keep every launch looking stuck.

Everything else - Sober install/update, the bubblewrap overlay launch,
the fflag allowlist and the config bind-mount - is upstream logic.
