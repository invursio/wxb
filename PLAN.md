# WXB Input Lab and Visualizers Implementation Plan

> **For agentic workers:** Work task-by-task. Each task ends with focused characterization/behavior tests and a full affected-suite check before moving on. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Extend the existing SOCD Cleaner tab with an optional Input Lab for live input inspection, keyboard remap profiles, controller testing and opt-in keystroke/click visualizers, without replacing the cleaner, rebuilding the app, or changing existing defaults.

**Architecture:** Keep the current `SocdEngine` and `SocdPipeline` in place. Add a pure-Python `wxb_input_lab` module for validated settings, remap state, conflict checks, controller monitoring, visualizer capture and capability detection; extend the existing pipeline with an optional remap table and bounded direction-event timeline; keep GTK presentation in `wxb_studio.py` with a separate transparent `wxb_overlay.py`. The existing SOCD tab gets one collapsed Input Lab expander, so no duplicate tab or replacement cleaner is introduced.

**Tech Stack:** Python 3.10+, GTK 4/libadwaita, PyGObject, existing `pynput`, optional `evdev`, optional `python-xlib`, Python `unittest`, Cairo, Xvfb.

**Spec:** The user request in this thread and this plan.

## Audit (before changes)

- **Existing SOCD tab:** `wxb_studio.py` has one `socd_card` inside the current `socd` page. It already exposes separate Horizontal/Vertical mode dropdowns, four presets, live held/sent/suppressed text, conflict count, access text, errors and the Enable/Disable cleaner button. This is the extension point; do not add a page or replace these controls.
- **Engine:** `wxb_engine.SocdEngine` is a pure state machine that resolves `LEFT/RIGHT` and `UP/DOWN` with Neutral, Last win, First win and Priority modes. `wxb_engine.SocdPipeline` grabs qualifying evdev keyboards, passes non-direction keys through, writes to one uinput keyboard, and releases everything in `_finish()`.
- **Service:** `StudioService.start_socd()` creates the engine/pipeline and saves only `mode_h`/`mode_v` under `settings["socd"]`; `socd_snapshot()` merges the pipeline snapshot with permission details. `stop_all()` stops clicker, rotation, SOCD, macro and recording.
- **Hotkeys:** F6/F7/F8/F9 and Esc are registered through the existing global listener. SOCD is F8 when idle; the clicker uses F8 as a local panic path while live. Input Lab must not add or steal global bindings.
- **Tests:** Baselines before this change: `test_studio.py` 73 passed, `test_studio_ui.py` 41 passed, `test_recorder.py` 25 passed. The global-hotkey X11 test is conditional on a live display plus `xdotool`.
- **Backends/permissions:** `pynput` is required for existing injection/hotkeys; `evdev` is optional and needs readable `/dev/input/event*` plus writable `/dev/uinput` for the cleaner. `python-xlib` is installed here but must remain optional. Native Wayland global capture is restricted; the existing app documents that limitation.
- **Visualizers:** There is no desktop keyboard/click overlay. Existing `Adw.ToastOverlay` and the command palette overlay are internal UI and must remain untouched.
- **Existing work:** No Git repository is present. A rollback archive and config copy were created at `backups/wxb-pre-input-lab-20261115-141047/` before any edit. The prior `PLAN.md` is inside that archive.

## Global Constraints

- Do not create a second SOCD tab, replace `SocdEngine`/`SocdPipeline`, or remove any existing control.
- Existing defaults remain: Neutral horizontal and vertical; no remaps; no visualizer; no controller monitor; no new global hotkey.
- Every new persisted setting is bounded, normalized on load, and absent from old configs without changing their behavior.
- Raw key/mouse events are never written to disk, logged or uploaded. Only mappings and visual preferences are persisted.
- Input capture starts only for an explicit SOCD run, an explicit controller test, or an enabled visualizer. The UI must show which is active.
- Do not elevate, run sudo, or change permissions from this feature. Explain missing access instead.
- Remapping is supported only while the SOCD cleaner owns the keyboard; mouse-button remap is explicitly unavailable rather than faked.
- External-conflict detection is best effort and X11-only; absence of detection is not a guarantee.
- New file writes are `0600`, directories `0700`, symlink-refusing, and atomically replaced.
- The game/competition-rules warning is always visible in the Input Lab, not hidden in docs.
- Preserve the existing recording and both user-owned recording folders.

## Review Focus

The input classes most likely to break expectations:

1. **Simultaneous directions:** A physical Left+Right pair must still produce the selected mode's exact output, and releasing one key must restore the other without a stuck direction.
2. **Remap release:** A held remap, a remap target shared by two physical keys, or a device unplug must never leave the target down.
3. **Safe reset:** Reset must disable remaps and release every simulated key/button even if one engine is mid-event or already failed.
4. **Controller disconnect:** Removing the controller during a test must drop its state, show a reconnect message, and leave the monitor stoppable.
5. **Capture privacy:** Typing ordinary text must not create a persisted event, log line or text buffer; turning visualizers off must stop their listeners.
6. **Permissions/session:** Missing `/dev/input`, missing `/dev/uinput`, Wayland or root must disable the affected controls with a readable reason, not crash or request elevation.
7. **Existing behavior:** The SOCD tab, presets, F8 path, settings shape, named profiles and recorder/launcher paths must stay working.

---

### Task 0: Preserve rollback and baseline

**Files:**
- `backups/wxb-pre-input-lab-20261115-141047/wxb-source.tar.gz`
- `backups/wxb-pre-input-lab-20261115-141047/config-wxb/`
- `test_studio.py`, `test_studio_ui.py`, `test_recorder.py`

- [x] Create a source archive and config copy before edits.
- [x] Record baseline: 73 service, 41 UI, 25 recorder tests passing.
- [x] Verify the archive exists and can be listed.

### Task 1: Characterization tests first

**Files:**
- Modify: `test_studio.py`
- Modify: `test_studio_ui.py`
- Create: `test_input_lab.py`

Interfaces:
- Consumes: existing `wxb_engine.SocdEngine`, `StudioService.socd_snapshot()`, current SOCD page widgets.
- Produces: regression tests for defaults, press/release sequences, presets, F8 dispatch, existing page/widget presence, and capture-not-running.

- [ ] Write tests that pin Neutral/Last/First/Priority press and release behavior, including WASD/arrow unification and one conflict episode.
- [ ] Write a test that the default modes are still `Neutral`/`Neutral` and existing presets set each axis independently.
- [ ] Write UI tests that the existing `socd` page, four preset buttons, live labels and enable button remain present.
- [ ] Write a service test that a fresh service reports SOCD idle and no remap/profile error.
- [ ] Run the focused tests against the untouched behavior and confirm they pass.

### Task 2: Input Lab settings and pure remap model

**Files:**
- Create: `wxb_input_lab.py`
- Create: `test_input_lab.py`
- Modify: `test_studio.py`

Interfaces:
- `normalize_input_lab(raw: Mapping) -> dict` returns:
  `{"timeline": bool, "deadzone": float, "active_profile": str, "visualizer": {"keys": bool, "clicks": bool, "size": int, "color": "#rrggbb", "opacity": int}}`.
- `INPUT_KEY_CHOICES: tuple[tuple[str, int], ...]`, `INPUT_KEY_CODES`, `INPUT_KEY_NAMES`, `DIRECTION_CODES`.
- `normalize_profile(raw: Mapping, require_name=False) -> tuple[dict | None, str]` returns a `{"version": 1, "name": str, "mode": "hold"|"toggle", "mappings": [{"source": token, "target": token}]}` document.
- `profile_conflicts(profile, hotkeys=None) -> list[str]`.
- `RemapEngine(profile)` with `handles(code)`, `press(source, code)`, `release(source, code)`, `forget_source(source)`, `release_all()`, `direction_output(code)`, `snapshot()`.

- [ ] Write failing tests for bounded normalization, unknown keys, self-maps, duplicate sources/targets, direction targets, toggle restrictions and target/source cascades.
- [ ] Write failing remap tests for hold press/release, toggle press/release, two keyboards, target release on device loss and `release_all`.
- [ ] Write failing conflict tests for WXB hotkey collisions and duplicate mapping conflicts.
- [ ] Implement the pure module and make focused tests pass.

### Task 3: Pipeline timeline and remap application

**Files:**
- Modify: `wxb_engine.py:SocdPipeline`
- Modify: `test_studio.py`
- Modify: `test_input_lab.py`

Interfaces:
- Extend `SocdPipeline.__init__(..., remap=None, timeline=False, on_event=None)` without changing existing positional arguments.
- `SocdPipeline.snapshot()` adds `"timeline": [{"at": float, "kind": "down"|"up", "key": str, "direction": str, "out": str}]`.
- `SocdPipeline.release_now() -> None` performs the same release path as stop but does not require stopping the thread.

- [ ] Write failing pipeline tests using fake evdev devices/uinput for mapped key presses, releases, direction remap output, duplicate target press, seed-held and device removal.
- [ ] Write failing timeline bounds/privacy tests: only direction events are stored, at most 12, and no text fields exist.
- [ ] Implement the optional remap table and timeline with default behavior unchanged when both are absent.
- [ ] Run `test_studio.py` and `test_input_lab.py`.

### Task 4: Controller monitor and capability detection

**Files:**
- Modify: `wxb_input_lab.py`
- Create: tests in `test_input_lab.py`

Interfaces:
- `detect_input_lab_capabilities(environ=None, evdev=None) -> dict` with `session`, `x11`, `wayland`, `read_input`, `uinput`, `remap`, `controller`, `visualizer`, `external_conflicts`, `root`, `reason`.
- `ControllerMonitor(evdev=None, on_change=None, deadzone=0.12)` with `start()`, `stop()`, `snapshot()`.
- Snapshot includes controller name, `connected`, `buttons` (pressed names), `axes` (`LX/RX/LY/RY/LT/RT` with raw/normalized/deadzone values), `deadzone`, `error`.

- [ ] Write failing tests for controller detection, button/stick/trigger updates, dead-zone math and disconnect recovery.
- [ ] Write failing capability tests for X11, Wayland, missing evdev, missing `/dev/input`, missing uinput and root.
- [ ] Implement the read-only controller monitor; it must never grab or inject.
- [ ] Run focused tests.

### Task 5: Service profiles, visualizer capture and safe reset

**Files:**
- Modify: `wxb_studio_service.py`
- Modify: `test_studio.py`
- Modify: `test_input_lab.py`

Interfaces:
- `StudioService.input_lab_profile_dir()`, `list_input_lab_profiles()`, `save_input_lab_profile(name, mappings, mode)`, `load_input_lab_profile(name)`, `delete_input_lab_profile(name)`, `activate_input_lab_profile(name)`.
- `StudioService.set_input_lab_settings(**values)` and `input_lab_snapshot()`.
- `StudioService.start_controller_test()` / `stop_controller_test()`.
- `StudioService.set_visualizer_settings(keys=None, clicks=None, size=None, color=None, opacity=None)`.
- `StudioService.safe_input_reset() -> tuple[bool, str]`.
- `StudioService.add_visualizer_listener(callback) -> unsubscribe`.
- Snapshot adds `"input_lab"` with capabilities, active profile, controller and visualizer status.

- [ ] Write failing tests for profile save/load/delete, corrupt and symlink refusal, mode persistence and no raw input fields.
- [ ] Write failing tests for safe reset: stops every input engine, clears active profile, stops controller/visualizer and reports success even when idle.
- [ ] Write failing visualizer tests with fake listeners: no listener when both switches are off; two independent switches; key/click callbacks; stop releases/stops listeners; no text buffer or persistence.
- [ ] Wire the service, start SOCD with the active remap profile, and implement the status/error reporting.
- [ ] Run `test_studio.py` and `test_input_lab.py`.

### Task 6: Existing SOCD tab gets Input Lab

**Files:**
- Modify: `wxb_studio.py` around the existing `socd_card`
- Modify: `test_studio_ui.py`

Interfaces:
- Existing widgets remain: `socd_h`, `socd_v`, `socd_status`, `socd_start`, `socd_error`, `socd_conflicts`, `socd_live`, `socd_access`.
- New widgets are under one collapsed `lab_fold`: live side-by-side rows, timeline switch/label, preset note, remap profile editor/list, controller test, visualizer controls, safe reset and capability/legal warnings.
- `InputOverlayWindow` in `wxb_overlay.py` with `apply_settings`, `show_key`, `show_click`, `start`/`stop` and `set_enabled`.

- [ ] Write failing UI tests for the one Input Lab expander, no duplicate `socd` page, existing controls intact, axis modes, profile add/save/load and safe-reset wiring.
- [ ] Write failing UI tests for separate keystroke/click switches, size/color/opacity bounds, capability-disabled controls and the visible capture indicator.
- [ ] Add the Input Lab UI without moving/removing existing SOCD controls.
- [ ] Run `test_studio_ui.py` under the existing display and confirm no page/geometry regression.

### Task 7: Keystroke overlay and click rings

**Files:**
- Create: `wxb_overlay.py`
- Modify: `wxb_studio.py`
- Modify: `test_studio_ui.py`
- Modify: `wxb_input_lab.py`

Interfaces:
- `InputOverlayWindow` is transparent, non-interactive where the compositor allows it, shows transient keycaps and animated click rings.
- Visualizer capture uses `pynput` listeners only while a switch is enabled; no text buffer, no event log, no persistence.

- [ ] Write failing tests for overlay settings (size/color/opacity), keycap label, click-ring state expiry, separate visibility and listener stop.
- [ ] Implement the overlay and wire the service capture callbacks through `GLib.idle_add`.
- [ ] Verify the overlay does not consume pointer input by setting an empty GDK input region; if the environment refuses, disable the overlay with an explicit reason rather than leaving a blocking window.
- [ ] Run the UI suite.

### Task 8: Docs, full verification and manual limits

**Files:**
- Modify: `README.md`
- Modify: `PLAN.md` execution record
- Tests: all affected suites

- [ ] Document the Input Lab, its defaults, X11/Wayland and permission limits, profile storage, privacy behavior and competition-rules warning.
- [ ] Run `python3 test_studio.py`, `python3 test_studio_ui.py`, `python3 test_input_lab.py`, `python3 test_recorder.py`, `python3 test_macros.py`, `python3 test_clicker_hardening.py`, `python3 -m compileall -q .`, and `python3 -m pyflakes *.py`.
- [ ] Run the real X11 hotkey test if `DISPLAY` and `xdotool` are available; report skips honestly.
- [ ] Verify the kept recording and both folders are untouched.
- [ ] Confirm no test process, overlay listener or simulated key remains running.
- [ ] Record exact outputs and untested limits in the final report.

## Verification Commands

```bash
python3 test_studio.py
python3 test_input_lab.py
python3 test_studio_ui.py
python3 test_recorder.py
python3 test_macros.py
python3 test_clicker_hardening.py
python3 -m compileall -q .
python3 -m pyflakes *.py
```

## Known Limits To State Plainly

- Keyboard remapping and controller monitoring require the Linux evdev backend and readable input nodes. Missing access produces a disabled control and a permission explanation; no elevation is attempted.
- Mouse-button remapping is not supported by the SOCD keyboard backend. The UI must say so instead of presenting a control that cannot suppress the original click.
- Global keystroke/click visualizers require X11 and `pynput`; native Wayland global capture may be unavailable. The overlay can be shown without capturing, but it must be disabled with a reason if the backend cannot capture.
- External shortcut conflict detection is best effort and only possible on X11 with `python-xlib`; it cannot see compositor-private or Wayland shortcuts.
- The real controller, real overlay click-through and live physical-key paths need a live desktop session. Headless/Xvfb tests use fakes and cover logic only.
- No raw input history is written or uploaded; the bounded timeline is direction-only and lives in memory.

## Execution Record (2026-11-15)

Implemented in the existing tree; the plan's original checklist is retained as
the design record rather than rewritten after the fact.

- **Task 0:** rollback backup created at
  `backups/wxb-pre-input-lab-20261115-141047/` (`wxb-source.tar.gz`,
  `config-wxb/`, `BACKUP-STAMP`); directory is `0700` and config copies are
  `0600`. Baseline tests passed before edits.
- **Tasks 1–3:** `test_input_lab.py` and the existing service tests cover
  defaults, presets, press/release sequences, remaps, device loss, timeline
  privacy and pipeline reset. `test_studio.py` grew from 73 to 82 tests and
  passed. The new preset test proves that a config with no `socd` block loads
  `Neutral`/`Neutral`, that partial configs set either axis independently,
  and that a `Last win` horizontal / `Neutral` vertical engine releases only
  the vertical pair without disturbing the horizontal winner.
- **Tasks 4–5:** read-only `ControllerMonitor`, capability/error reporting,
  private remap profile storage, visualizer listeners and `safe_input_reset()`
  are implemented. Service tests cover refusal, start/stop, switch errors and
  Safe Reset.
- **Tasks 6–7:** one collapsed Input Lab expander was added inside the existing
  SOCD card; no SOCD page or existing control was duplicated or removed.
  `wxb_overlay.py` provides bounded appearance settings and an empty GDK input
  region; the real X11 display reported a `1920×1080` surface with
  `click_through=True`. `test_studio_ui.py` grew from 41 to 46 tests and passed,
  including no-duplicate-page, visible capture badge, profile and overlay wiring
  checks, and independent horizontal/vertical preset selection.
- **Task 8:** README feature status, limits, architecture and rollback notes
  were updated. Verification on 2026-11-15:
  `test_input_lab.py` 17 passed; `test_studio.py` 82 passed;
  `test_studio_ui.py` 46 passed; `test_recorder.py` 25 passed;
  `test_macros.py` 21 passed; `test_clicker_hardening.py` 25 passed;
  `test_global_hotkeys_x11.py` 3 passed; `test_macros_x11.py` 3 passed;
  `test_recorder_x11.py` 6 passed; `compileall` and `pyflakes` exited 0.

**Rollback verification:** `gzip -t` passed on
`backups/wxb-pre-input-lab-20261115-141047/wxb-source.tar.gz`; it contains 80
entries, extracts to a clean scratch directory, and `compileall` succeeds there.
The extracted source has no `wxb_input_lab`, `Input Lab` or `RemapEngine`
symbols, and the archived config has no `input_lab` key. The backup directory
is `0700`, its config copy is `0700`, and its files are `0600`.

**Untested / manual limits:** no physical controller or physical remap pass was
available; controller event/disconnect paths use fake evdev devices. Full-screen
overlay positioning on a multi-monitor compositor was not exercised; only the
empty input region was checked on the live X11 display. The three live-X11
suites were rerun and passed; a physical keyboard/controller and real game
integration remain manual checks.

**Recording check:** `/home/<user>/Videos/WXB` is present at mode `0700`,
owned by `overwatch:overwatch`, and is currently empty. The previously audited
`WXB-20261115-121014.mkv` is unexplained: it was absent at the final check and
no file with that name exists in the home tree or Trash. `wxb_recorder.py` and
`wxb_macros.py` are byte-identical to the archived pre-change files. Recorder
cleanup unlinks only the `O_EXCL` output path it just reserved; it never
overwrites or removes an existing recording, and no changed file has a delete
path under `Videos/WXB`. No replacement or deletion was performed here.
