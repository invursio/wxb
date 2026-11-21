# WXB Studio

**A quieter control room for Roblox on Linux.**

WXB Studio is a GTK 4 + libadwaita surface for four input tools — an auto
clicker, a slot-A/slot-B rotation macro, a TinyTask-style input macro
recorder/player and a system-wide SOCD cleaner — plus a local screen recorder,
with Sober quick play and Cordial alt profiles in the same window. The SOCD
card also contains one optional, collapsed **Input Lab** for remap profiles,
read-only controller testing and opt-in keystroke/click visualizers.

The interface is new. `wxb_engine.py` is engine-only code; the window is built
entirely in `wxb_studio.py`, `wxb_studio_service.py` and `wxb_studio.css`.

---

## Download

Prebuilt AppImages are attached to
[GitHub Releases](https://github.com/invursio/wxb/releases). Download
`WXB-<version>-x86_64.AppImage`, make it executable and run it:

```bash
chmod +x WXB-*.AppImage
./WXB-*.AppImage
```

The AppImage carries WXB and its pure-Python input dependencies. It uses your
distribution's GTK 4.14+, libadwaita 1.5+, PyGObject and pycairo, so install
those first:

```bash
# Ubuntu / Linux Mint
sudo apt install python3-gi python3-cairo gir1.2-gtk-4.0 gir1.2-adw-1

# Fedora
sudo dnf install python3-gobject python3-cairo gtk4 libadwaita

# Arch / Manjaro
sudo pacman -S python-gobject python-cairo gtk4 libadwaita
```

Optional features need `python3-pil` (cover art), `python3-evdev` (controller
test), `ffmpeg` (screen recorder), and `xdotool`/`wmctrl` (window helpers).
The AppImage runs with normal host access because WXB starts Sober through
Flatpak and bubblewrap; it is not sandboxed. On Wayland, global hotkeys need an
X11 session because pynput uses XRecord.

---

## Design language

Warm graphite canvas, one rose signal, generous negative space. Inter Display
carries headlines; JetBrains Mono carries machine facts.

| Reference | What it contributes |
| --- | --- |
| **Apple** | One clear primary action per screen and calm pacing. |
| **Duolingo** | A three-step welcome with real progress, not a manual. |
| **Linear** | Quiet dark surface, command palette, keyboard-first flow. |
| **Pentagram** | Oversized display type and editorial hierarchy. |
| **MUJI** | Restraint: few surfaces, thin borders, no decorative noise. |
| **Airbnb** | Scannable profile rows with the action at the row edge. |
| **Aesop** | Restrained orbital art and muted warmth. |
| **Notion** | Dashboard metrics, small labels and useful empty states. |
| **Stripe** | Precise dividers, 8px rhythm and consistent control heights. |
| **Framer** | Small transitions: page crossfades, revealers, hover states. |
| **Own** | Dieter Rams’ “less, but better”, Teenage Engineering’s mono micro-labels, Raycast’s `Ctrl K` command surface. |

The shell is adaptive. The four metric cards flow from four columns to two to
one; the three control cards from three to two to one. At narrow widths the
sidebar hides behind the header toggle and the system card steps out of the way. A single
window-manager resize to **900×700** allocates the collapsed layout
immediately, including the wrap and the sidebar toggle.

---

## Features

- **Overview** — system state, live engine counts, session clicks, alt count and
  Sober launch.
- **Automation** — independent clicker, rotation and SOCD controls with
  inline errors, value locks and SOCD presets; exact CPS/CDC entry stays beside
  the existing scales. The rotation has a persisted master switch that starts
  off, stops a live rotation and gates its hotkey and values; slot hints show
  which key a hotbar number taps (10 → `0`). Hold mode starts on the global
  hotkey press and stops when any key in the combo is released.
- **Input Lab (optional, inside SOCD Cleaner)** — live physical direction
  rows beside resolved outputs, an opt-in direction timeline, keyboard remap
  profiles with hold/toggle modes, internal/external conflict checks, a
  read-only evdev controller test with stick dead zone, and separate opt-in
  keystroke and click-ring visualizers with size, colour and opacity. It is
  collapsed and off by default; the SOCD presets, start/stop path and existing
  hotkeys are unchanged. See **The Input Lab stays inside the SOCD card**.
- **Macros** — a sidebar tab records mouse movement, mouse-button
  press/release, scroll, keyboard press/release and timing, then replays once,
  N times or continuously at `0.1–10x`. It includes a step editor, validated
  save/open/import/export, local versioned files and configurable record/play/
  stop hotkeys. Recording and playback run in `StudioService`, so switching
  tabs does not stop them. See **The Macros tab** below.
- **Recorder** — a sixth sidebar tab captures the X11 screen, one window or a
  rectangle to a recoverable local `.mkv` with FFmpeg. It has source,
  resolution, FPS, quality/load, cursor, save-folder, microphone and desktop
  audio controls; pause/resume; elapsed time and a live indicator; and a
  configurable stop hotkey. Capture runs in the service, not the GTK thread,
  so switching tabs does not stop it. Files are timestamped, never overwritten,
  finalized on stop/exit and written `0600`. See **The Recorder tab** below.
- **Privacy** — the header eye hides CPS/CDC, delays, slots and key hints,
  rotation limits, opacity, hotkeys, SOCD counters and live values without
  changing any setting or stopping an engine. Hidden values cannot be edited;
  turning privacy off restores the exact on-screen text. It is per-session and
  is never written to disk.
- **Cordial** — discovered Cordial profiles, including empty, single and large
  lists; long and Unicode names are ellipsized safely. The same page exposes
  Sober's installed version, crash-dump count, local playtime and an explicit
  **Install / update** action; launching still ensures the Flatpak on demand.
  The compact Cordial page uses a two-column profile grid and collapsed Sober
  tool folds. An **auto-tile** switch arranges launched Cordial windows into
  exact, gapless X11 work-area cells (on by default); layouts are **Side by
  side**, **Stack on top**, **Grid**, **Focus left** and **Focus top**.
  **Tile now** reapplies the selected layout and **Alt FPS cap** (15 by
  default) is written into every alt profile before its spawn, so a client
  cannot start without its hard cap. **Low graphics** writes the lightweight
  texture/MSAA flags and removes them again when switched off, and **CPU
  quota** (percent of one core, 0 = off) is
  applied to new clients by the launch manager; both are stored beside the
  cap. The Sober runtime card can stop a live session without closing WXB.
  The same card can add a named profile
  (which creates Cordial's real profile directory) and delete a stopped
  profile after a destructive-action confirmation. The **Ctrl+Shift+F10**
  global hotkey cycles all five layouts and applies the new one immediately;
  the dropdown follows the hotkey. PID-less Flatpak surfaces fall back only to
  Cordial/Roblox-class windows. The tiler keeps each
  launch's window id, waits until every live launch has a mapped surface, and
  never assigns one window to two alts.
- **Fast flags** — the Lution flag table is editable as `NAME = value` lines,
  checked against the bundled allowlist and refused while Sober is running.
  Named flag profiles save and load under `~/.config/wxb/flag-profiles/`.
- **Linux launch environment** — `KEY = value` variables are validated and
  passed to Sober on its next launch; invalid names are refused instead of
  silently dropped.
- **Sober visuals** — Roblox font apply/restore/reapply, cursor style, colour
  and size, plus the current framerate cap live in the existing Appearance
  card. The cursor maker also offers 18 outlined cursors from the CC0 Kenney
  Cursor Pack; the 427 KB ZIP is downloaded once, cached under Lution's
  installed cursors, and then works offline. Each control reports its own
  prerequisite (APK font list, Pillow, or Sober's settings XML) instead of
  doing nothing.
- **Settings** — five accents, window opacity, a **stop engines on close**
  behavior switch, editable **and captured** global keybindings (per-binding
  reset), named configs, onboarding replay and the settings path.
- **Welcome** — three steps on first run; replayable from Settings.
- **Command palette** — `Ctrl K` searches pages and actions; `Ctrl 1–9` jump
  between pages; `Esc` closes it before acting as the emergency stop. The
  recorder is `Ctrl 6`.
- **Global hotkeys** — defaults are `F6` clicker, `F7` rotation, `F8` SOCD,
  `F9` stop all and release inputs, `Ctrl+Shift+F10` cycle Cordial tiling,
  `Ctrl+Alt+X` stop and save a screen recording, and macro controls
  `Ctrl+Alt+R` record/stop, `Ctrl+Alt+P` play/stop and `Ctrl+Alt+S` stop macro
  only. **ESC** remains the reserved emergency stop in
  the window and through the global listener. Every action can be rebound to
  any Ctrl/Shift/Alt/Cmd combination,
  F1–F20, letter, digit or named key; ESC itself cannot be captured. Buttons
  remain usable if the listener cannot bind.
- **Single instance** — a second launch is handed to the running window, which
  is raised and focused instead of starting a second hotkey listener.
  A per-config clicker file lock also prevents two windows or a
  `WXB_STANDALONE` process from clicking at the same time; the kernel drops
  the lock if WXB is killed, so a crash cannot leave a stale lock.
- **Named configs** — save and load complete snapshots of clicker, rotation,
  appearance and keybindings under `~/.config/wxb/configs/`.
- **Full clicker range** — CPS accepts `0.01–1000` and CDC/hold accepts
  `0.01–99.99%`, with exact numeric entry rather than a 1–50 cap.
- **Clicker safety and behavior** — duration (0 = until stopped) or infinite,
  follow/fixed/jitter pointer modes, a top-left/top-right/bottom-left/
  bottom-right corner failsafe, and pause-on-lock using the desktop's
  `LockedHint`. Fixed points can be captured from the current cursor. Pointer
  failures, display loss and permission errors release the button and appear
  inline instead of stopping silently.
- **Clicker profiles** — save and load clicker-only profiles under
  `~/.config/autoclicker/` (one JSON file per profile, mode `0600`). These are
  separate from the complete named studio configs in `~/.config/wxb/configs/`.
- **Panic behavior** — `Esc` is the reserved emergency stop; while the clicker
  is running, `F8` also stops it immediately. With the clicker idle, `F8`
  keeps its normal SOCD toggle meaning.

Everything is local except the optional CC0 cursor-pack download, which runs
only when you press Download & apply. Recordings never leave this machine.
WXB never asks for an account password.

### Restored parity inventory

| Capability named by the old build notes | Status in this UI |
| --- | --- |
| TinyTask-style input macro recorder | Added as the fifth **Macros** tab: X11 global mouse/key/scroll/timing capture, timed replay, loops, speed, step editor, versioned local files and configurable control hotkeys. Recording/playback survives tab switches. Real-Xlib-XTEST acceptance covers capture/replay, timing, two loops at 2x, all three control hotkeys, stop interruption and conflict refusal under Xvfb. |
| Local screen recorder | Added as the sixth **Recorder** tab: FFmpeg X11 screen/window/region capture, resolution/FPS/quality/cursor, private save folder, optional mic and desktop audio, pause/resume, elapsed indicator, recoverable Matroska, configurable stop hotkey and explicit capability/load/limits. Recording survives tab switches. Real-X11 acceptance covers capture, decode, repeated start/stop, pause/resume, desktop audio and window geometry under Xvfb. Native Wayland portal capture is detected but deliberately disabled in this build rather than advertised untested. |
| Privacy eye / value masking | Already present: `wxb_studio.set_privacy`, `_mask_privacy_values`; per-session only. |
| Rotation macro master switch and hold modes | Already present: `StudioService.set_macro_enabled`, `_hotkey_activated` / `_hotkey_released`; UI rows in Automation. |
| Global hotkey capture, reset and reserved ESC | Already present: `_start_hotkey_capture`, `_reset_hotkey`, `hotkey_wire_mapping`. Real-X11 checks cover clicker/rotation/SOCD/stop-all, hold release and reserved ESC; macro chords are covered separately. |
| Fonts | Restored: `lution_fonts.status/apply_font/restore_fonts/reapply_fonts` via `StudioService.fonts_status`, `apply_font`, `restore_fonts`, `reapply_font`; Settings → Appearance. |
| Cursors | Restored: `lution_cursors.status/apply/restore` via `cursors_status`, `apply_cursor`, `restore_cursors`; six drawn styles plus 18 cached online cursors from the CC0 Kenney pack (`apply_online_cursor`); same card. |
| FPS display / framerate cap | Restored as the cap readout/editor: `lution_fps.load_framerate_cap/save_framerate_cap` via `fps_status`, `set_fps_cap`. The vendored core has no injected in-game FPS overlay, so the current cap is displayed in Appearance rather than inventing one. |
| Fast flags manager | Restored: allowlist-checked editor, clean action and profile files wired through `set_lution_flags_text`, `clean_lution_flags`, `save/load/delete_flag_profile`; Cordial card. |
| Linux-specific settings | Restored: `lution_envvars.load_vars/save_vars` through `lution_env`, `save_lution_env` and `save_lution_env_text`; Cordial → Linux launch environment. |
| Deployment / bootstrapper | Restored explicitly: `lution_sober.is_installed/get_version/ensure_sober` via `lution_runtime` and `ensure_sober`; install/update without launching. Launch-time ensure remains. |
| Crash state and playtime | Restored: `lution_launcher.crash_state` and `lution_playtime.scan` through `lution_runtime` and `lution_playtime`; shown in Cordial. |
| Recent-place history | Already present in the rebuilt UI: `_refresh_quick_places` and `StudioService.remember_place`; WXB keeps its own local list and does not fetch Roblox history names. |
| Stop engines on close | Restored: Settings → Behavior writes `stop_on_close`; `_on_close` stops the input engines and Cordial clients but leaves Sober running. |
| Cordial low graphics and CPU quota | Restored in the Cordial options row: `low_graphics` feeds `prepare_profile` and removes its managed flags when disabled, and `cpu_quota` is applied to the launch manager and every new client. |
| Stop a live Sober session | Restored as **Stop game** in the Sober runtime card, backed by `lution_core.sober`'s Flatpak stop path. |
| Destructive uninstall / delete game data | Deliberately not surfaced: the old notes describe install/update deployment, and the rebuilt UI never offered one-click data deletion. The vendored core still contains it for explicit future use. |

---

## The Input Lab stays inside the SOCD card

The Input Lab is one collapsed expander in the existing **SOCD Cleaner** page,
not a second SOCD page and not a replacement for it. Its defaults leave the
cleaner exactly as it was: Neutral horizontal and vertical pairs, no remap
profile, no controller monitor, no listeners and no overlay. The existing
presets, horizontal and vertical mode dropdowns, live SOCD counter, hotkeys
and safe stop
paths remain the primary controls above the fold.

* **Live rows and timeline** — each physical direction shows its held keyboard
  codes and its resolved output side by side. The timeline is optional, holds
  at most twelve direction events, and stores only direction names, press or
  release, and the resolved output. It never stores typed text and is cleared
  when switched off or when the cleaner stops.
* **Remap profiles** — choose a keyboard source and a non-direction target,
  then save a named profile as **Hold** or **Toggle**. Hold mirrors a target
  while every physical source is down; Toggle flips it on the first press and
  releases it on the second. Profiles live under
  `~/.config/wxb/input-lab-profiles/` as `0600` files in a `0700` directory.
  A profile is applied when the SOCD cleaner starts and is released by Safe
  Reset, stop, unplug or error. Mouse-button suppression is not advertised:
  the backend owns keyboards, and the UI says so instead of pretending a
  mouse mapping works. Cascading key-to-key remaps and direction targets are
  rejected because the SOCD resolver owns those keys.
* **Conflict detection** — WXB hotkey sources and targets are checked against
  the saved profile. **Check conflicts** additionally performs a best-effort
  X11 passive-grab probe for already-grabbed external keys; it is skipped on
  native Wayland and reports that the external check is unavailable rather
  than claiming a clean result. A conflict is a warning, not a claim about
  any particular game.
* **Controller test** — a read-only `evdev` monitor lists controller buttons,
  stick axes and triggers, and shows raw versus dead-zone-adjusted stick
  values. The 0–50% dead zone is a display adjustment; it is not written into
  any game or device. Disconnecting a controller clears its buttons/axes and
  shows a reconnect wait. This mode never grabs a device and never injects an
  event. A physical controller was not available for the final hardware pass;
  the monitor's event handling, disconnect and reconnect paths are exercised
  with fake evdev devices.
* **Visualizers** — **Keystrokes** and **Click rings** are independent
  switches. Each can be left off while the other runs. Size, colour and
  opacity apply to the same transparent, click-through overlay window; the
  overlay uses a full-screen surface with an empty input region, so it does
  not take focus or swallow desktop clicks. It shows one key name at a
  time and ephemeral click rings, never a typed-text sequence. The overlay is
  separate from WXB's existing toasts and hotkeys. The visualizer listener
  starts only while a switch is on; a capture badge above the collapsed lab
  (and the matching Input Lab status) shows `CAPTURE · VISUALIZER` (or `SOCD` /
  `CONTROLLER`) whenever input is being read. If the compositor refuses a click-through region, the overlay hides
  itself and reports the failure instead of blocking the desktop.
* **Safe Reset** — always available. It stops the macro, clicker, rotation and
  SOCD cleaner, releases simulated keys and mouse buttons, stops the
  controller and visualizer listeners, clears the active remap profile and
  saves that disabled state. Inline status reports an unavailable remap,
  controller or overlay backend, a profile error, or a capture error.

| Capability | X11 session | Native Wayland | Notes |
| --- | --- | --- | --- |
| SOCD cleaner | Yes, with readable `/dev/input` and writable `/dev/uinput` | Not claimed | Existing permission and root checks are unchanged. |
| Keyboard remap profiles | Yes, through the same SOCD grab and virtual keyboard | Not claimed | Needs `python3-evdev`, readable input nodes and `/dev/uinput`; disabled while WXB runs as root. |
| Controller test | Yes, when `evdev` can read the device | Not claimed | Read-only; no grab or injection. |
| Keystroke/click visualizers | Yes, through `pynput` on X11 | Disabled with a reason | Requires `pynput`, an X11 `DISPLAY`, and an unprivileged process. |
| External conflict probe | Best effort on X11 | Disabled | Also needs `python-xlib`; absence is reported, never treated as success. |

The Input Lab never uploads input, never writes raw events to its profiles,
and does not silently change permissions. Its capability report explains the
missing layer and leaves unsupported controls disabled. A capture badge and the
game/competition-rules warning stay visible on the SOCD page even while the
Input Lab is collapsed. As with the rest of WXB, use SOCD, remapping or
visualizers only where those rules allow them.

---

## The Macros tab records and replays input

The **Macros** tab is a TinyTask-style input recorder, separate from the
slot-A/slot-B rotation macro in **Automation**. It records global X11 input with
`pynput`: absolute mouse moves, left/right/middle press and release, scroll,
keyboard press/release, and each event's relative time. Recording and playback
run in `StudioService`; switching sidebar tabs only hides the page, so neither
stops.

* **Playback** — once, N times (up to 100000) or continuously, at `0.1–10x`.
  The status line shows the name, step count, duration, loop mode and live
  loop/step progress.
* **Control hotkeys** — defaults are `Ctrl+Alt+R` record/stop, `Ctrl+Alt+P`
  play/stop and `Ctrl+Alt+S` stop macro only. They are rebindable in Settings;
  duplicate bindings are rejected. The recorder drops complete WXB control
  chords, so pressing Stop does not become part of the macro; a plain `R`, `P`
  or `S` key remains recordable. Linux usually reserves `Ctrl+Alt+F1` through
  `Ctrl+Alt+F12` for virtual-console switching, so use a letter or another
  binding if the desktop consumes an F-key chord. The
  clicker, rotation and SOCD cannot run while a macro records or plays, and a
  macro cannot start while any of those engines is running.
* **Files** — **Save** writes JSON under `~/.config/autoclicker/macros/` with a
  `0700` directory and `0600` files. The document is `format: "wxb.macro"`,
  `version: 1`, a name and timestamped events. Import validates the whole file
  before copying it; corrupt, oversized, unsupported-version, non-macro and
  symlinked files are refused. Export writes the same versioned JSON to a
  location you choose. There is no TinyTask `.rec` compatibility, no standalone
  binary, and no upload path: move the JSON file to another Linux machine with
  WXB and `pynput` if you need to share one.
* **Step editor** — select a step to inspect it, change its relative time, or
  delete it. Save after editing to keep the change in the local file.
* **Privacy** — recordings can contain passwords, private messages, search
  terms and anything else typed while recording. WXB keeps them local, writes
  them privately and never uploads them, but it cannot redact a recording. Do
  not record secrets, and delete macro files you no longer need. The page warns
  about this before you press Record.
* **Safe stop** — **Stop**, the stop hotkey, `F9`/Stop all, the local `F8`
  panic path while a macro is live, or closing WXB all stop the worker. The
  player releases every key and mouse button it holds on normal completion,
  stop, error, lock, sleep/clock jump or lost access. A locked session, a
  suspended desktop, a recorder-listener exit or a gap over 10 seconds stops
  recording safely and saves the partial macro. Recording has a 100000-step and
  24-hour cap; a cap hit is reported, never silently ignored.

| Session | Record | Playback | Global macro hotkeys |
| --- | --- | --- | --- |
| X11 (Linux Mint default) | Yes, through `pynput` | Yes | Yes |
| Wayland with XWayland | No: native Wayland does not expose global capture | Only into XWayland windows; not a native-Wayland guarantee | XWayland/global listener only |
| No X11 `DISPLAY` | No | No | No |

`pynput` is already required by the clicker. WXB does not assume `xdotool`,
`ydotool` or `wtype` can record, adds no runtime `sudo`, and disables the
unsupported buttons with the reason shown on the page. Native Wayland global
recording is not claimed; use an X11 session for the complete feature.

---

## The Recorder tab captures the screen locally

The **Recorder** tab is a sixth page, not a second application. It starts only
when you press **Start recording** (or use a service action); it never starts
with WXB. Capture is an FFmpeg child owned by `StudioService`, with stdin
detached and a bounded monitor thread. The GTK page only reads snapshots and
sends commands, so changing tabs does not stop a recording.

* **Source** — the whole X11 screen, one window ID (use **Active window** to
  fill it), or an explicit positive X/Y/width/height region. Window capture
  reads the window's current rectangle and records that area; moving or
  resizing the window after capture starts is not followed. A closed or
  off-screen window is rejected with a message rather than producing an empty
  file.
* **Output** — timestamped `WXB-YYYYMMDD-HHMMSS.mkv` files in the chosen folder
  (default `~/Videos/WXB`). Matroska keeps a partial file recoverable if the
  process is interrupted. Each output path is reserved with `O_EXCL` at mode
  `0600` before FFmpeg starts, so an existing file cannot be chosen; FFmpeg's
  `-y` is limited to that newly reserved file. Newly created WXB folders use
  `0700` where the filesystem allows it. The folder is created on
  start, not silently somewhere else.
* **Video controls** — source resolution (or 720p/1080p/1440p), 15/30/60 FPS,
  Low/Balanced/High quality presets (x264 `ultrafast`/`veryfast`/`medium`, CRF
  28/23/18), cursor on/off and Auto/CPU/VAAPI/NVENC encoder. **Auto** uses CPU
  libx264; hardware choices appear only when the encoder and a render device
  are detected. If a hardware capture fails, the error is visible and the next
  start can use CPU. Odd window dimensions are cropped by one pixel to keep
  `yuv420p` valid.
* **Pause / resume** — pauses the FFmpeg process with `SIGSTOP` and resumes with
  `SIGCONT`; elapsed time excludes the paused interval. The indicator and
  elapsed clock stay live while paused, and finalization still sends `SIGINT`
  after waking the process.
* **Audio** — independent microphone and desktop-audio switches with device
  menus. Desktop audio uses a Pulse/PipeWire monitor source; both are mixed to
  one AAC track at 160 kbit/s in the same `.mkv`. If no monitor source exists,
  the start is refused with an explanation. Audio permissions come from the
  user's Pulse/PipeWire session; WXB never asks for `sudo`.
* **Stop and recovery** — the page button, the configurable `Ctrl+Alt+X` hotkey,
  `F9`/Stop everything, or closing WXB stops capture. FFmpeg gets `SIGINT` so
  the Matroska trailer is written; after 8 seconds it gets `SIGTERM`, then a
  bounded kill. The final output is probed and shown with dimensions, FPS and
  duration. A locked desktop, a disk below 64 MiB free, a vanished process, a
  denied device or an encoder error stops with a visible reason, never a
  silent empty success. `StudioService.shutdown()` joins the monitor and saves
  the partial file.

### Quality and resource tradeoffs

There is no “maximum quality at minimum load” setting. CPU x264 at 1080p60/High
is much heavier than 720p30/Low, and the UI says so rather than promising a
free lunch. Start with **Balanced, source resolution, 30 FPS**; drop to 15 FPS
or Low if the desktop stutters or the encoder falls behind, and use a detected
hardware encoder when available. The measured Xvfb run at 1280×720, 30 FPS,
Balanced/CPU used about **73% of one CPU core and 226 MiB RSS** for FFmpeg, with
a 6-second blank-screen file of about 9.6 KiB. Those numbers are a synthetic
blank-screen measurement, not a promise for a busy desktop, and they do not
cover VAAPI/NVENC. Quality is controlled by the preset/CRF; this project does
not measure a visual quality metric.

### Linux capabilities and limits

| Session / tools | Recorder behavior |
| --- | --- |
| X11 with FFmpeg `x11grab` and ffprobe | Screen, window and region capture; CPU and detected hardware encoders; Pulse audio when `pactl` reports it. This is the fully exercised path. |
| Wayland with a ScreenCast portal and PipeWire | Portal/PipeWire presence is detected and listed on the page, but this build has no tested PipeWire capture process, so **Start** is disabled with an explicit reason. It does not claim portal consent or capture that has not been exercised. |
| Wayland without a usable portal | Controls are disabled; the page says what is missing. Use an X11 session for the complete recorder. |
| FFmpeg missing or no `DISPLAY` | Recorder is unavailable; the reason is shown. It never falls back to an unverified capture path. |

Install the trusted external tools on Mint/Ubuntu with the distribution
packages (never by running WXB as root):

```bash
sudo apt install ffmpeg pulseaudio-utils x11-utils xdotool
ffmpeg -hide_banner -devices | grep x11grab   # capability check
pactl info                                   # Pulse/PipeWire check, optional audio
```

`x11-utils` supplies `xwininfo` for window rectangles and `xdotool` fills the
**Active window** button; screen/region recording works without the latter.

`ffmpeg`/`ffprobe` are invoked with a fixed argv, not a shell; no runtime
`sudo`, network upload or account password is used. Builds of FFmpeg differ in
license (LGPL or GPL depending on the distribution/configure flags); WXB does
not redistribute FFmpeg, and the installed package's license and notices apply.
GStreamer/portal support is only advertised, not started, until a tested path
ships.

**Privacy:** a screen recording can contain passwords, notifications, private
messages and everything else visible. WXB writes it locally and never uploads
it, but cannot redact it. Do not record secrets; delete files you no longer
need. The page repeats this warning before recording.

---

## Run it

Requirements: Python 3.10+, GTK 4.14+, libadwaita 1.5+, PyGObject and `pynput`.
The optional screen recorder additionally needs `ffmpeg` and `ffprobe` from the
distribution; `pactl`/Pulse-PipeWire is needed only for audio.

Install `python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1` and `python3-pynput` with
your package manager, then install the Python extras from `requirements.txt`.

```bash
python3 wxb.py
```

From the desktop icon, `WXB.sh` can elevate for `/dev/input` access. The studio
also starts unprivileged; an unavailable SOCD cleaner reports the permission
problem instead of failing silently. An elevated studio launches Cordial as
the desktop owner with that owner's D-Bus session and `XDG_RUNTIME_DIR`, so a
Flatpak client can still open a window.

```bash
WXB_NO_ELEVATE=1 ./WXB.sh        # explicitly unprivileged
python3 wxb.py --no-onboarding   # skip the welcome for a preview
python3 wxb.py --page alts       # open a specific page
python3 wxb.py --page macros     # open the macro recorder
python3 wxb.py --page recorder   # open the local screen recorder
python3 wxb.py --config /tmp/wxb-preview.json
python3 wxb.py --command         # open the command palette on launch
```

SOCD needs `python3-evdev` and access to `/dev/uinput`. Sober quick play uses
the Lution overlay when available. Cordial profiles are optional.

### Optional GameMode launch effects

The **GameMode** page can add MangoHud, Linux GameMode and Gamescope to one
Sober launch. Gamescope is **off by default** because it adds a nested
compositor; enable it on that page when you want it for a launch. MangoHud and
Gamescope are normally supplied as Flatpak Vulkan layer extensions, so they do
not need host packages:

```bash
flatpak install flathub org.freedesktop.Platform.VulkanLayer.MangoHud
flatpak install flathub org.freedesktop.Platform.VulkanLayer.gamescope
sudo mkdir -p /usr/lib/extensions/vulkan/gamescope  # extension mount point
```

Linux GameMode needs the daemon and its auto client; on Mint/Ubuntu:

```bash
sudo apt install gamemode
systemctl --user enable --now gamemoded
```

Each switch is independently detected and disabled when its tool is absent.
The real-machine integration check is opt-in and never launches Sober:

```bash
WXB_RUN_GAMEMODE_INTEGRATION=1 python3 test_gamemode_integration.py
```

It checks the installed Flatpak app and layers, runs the real Gamescope binary
with `--version`, and starts a throwaway GameMode client to prove `gamemoded`
registers and releases it. On a machine without one of those tools that check
is skipped with the reason; without the opt-in environment variable all six
checks are skipped. The integration test intentionally uses the real HOME so
Flatpak can see user extensions, but only writes a temporary Studio config and
temporary Lution home.

Settings live at `~/.config/wxb/studio.json`, written atomically with mode
`0600`. Loading is defensive: missing, empty, truncated or non-object files
start from defaults; malformed sections fall back; out-of-range values are
clamped; foreign settings files with unknown keys are ignored rather than imported.

Macro files live at `~/.config/autoclicker/macros/<name>.wxbmacro.json`, in a
`0700` directory, with atomic `0600` writes and no-follow opens. They are never
included in the studio config, clicker profiles or any network request.

### Linux Mint / Ubuntu quick start

```bash
sudo apt update
sudo apt install python3 python3-pip python3-gi python3-cairo \
  gir1.2-gtk-4.0 gir1.2-adw-1
sudo apt install ffmpeg pulseaudio-utils       # screen recorder and audio
sudo apt install python3-pil python3-evdev xdotool wmctrl  # optional features
python3 -m pip install --user -r requirements.txt

# Run unprivileged; the clicker never needs sudo.
WXB_NO_ELEVATE=1 ./WXB.sh
# or directly:
python3 wxb.py --page automation
```

Mint 22+/Ubuntu 24.04+ provide the required GTK 4.14/libadwaita 1.5 through
those packages. Mint 21/Ubuntu 22.04 ship older GTK/libadwaita releases and
are not sufficient for the current UI. If `pip --user` is blocked by an externally managed Python, use a
virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 wxb.py
```

The GTK packages stay system-wide; only the Python extras are installed in the
environment. Do not run the app with `sudo`: root cannot inject into the
desktop user's session. `WXB.sh` can re-launch the whole app with elevation to
grant the optional SOCD cleaner device access; the clicker and hotkeys do not
require it. For clicker-only work, keep elevation off with
`WXB_NO_ELEVATE=1` (or `WXB.sh --no-elevate`).

### Build the AppImage

```bash
./packaging/appimage/build.sh
# dist/WXB-<version>-x86_64.AppImage
```

The script pins the pure-Python dependencies, builds an AppDir and runs
`appimagetool` (downloaded on first use). CI builds the same bundle on every
push and attaches it to a GitHub Release for tags and published releases.

### X11 and Wayland

WXB uses `pynput` on X11. If `pynput` cannot open a pointer backend, the
service tries `ydotool` first and `xdotool` second, then shows the exact error
if neither can click. To force the external path (useful when a Wayland
compositor accepts the `pynput` import but rejects injection):

```bash
sudo apt install ydotool        # package install needs admin
ydotoold                        # run as your desktop user, never with sudo
WXB_CLICK_BACKEND=command WXB_NO_ELEVATE=1 ./WXB.sh
```

`xdotool` is X11/XWayland-only. `ydotool` needs a running `ydotoold` and
`/dev/uinput` access for your user (a udev rule or the `uinput` group, not the
clicker running as root). Command-backed clicks are delivered on release, so
hold-time precision and very high CPS are best on X11 with `pynput`. On native
Wayland, fixed-point, jitter and corner-failsafe checks may be unavailable
because the compositor does not expose the global pointer position; WXB
reports that instead of pretending the guard ran.

The screen recorder is stricter: it records only on X11 `x11grab` in this
build. On native Wayland it may detect the ScreenCast portal and PipeWire, but
it does not advertise a capture start path that has not been exercised; the
Recorder page disables Start and says why. XWayland's `DISPLAY` is not treated
as permission to record the native desktop. See **The Recorder tab** for the
capability matrix and installation commands.

Pause-on-lock reads systemd-logind's `LockedHint` through `loginctl`. On a
minimal session without logind, lock state cannot be observed and the service
treats the session as unlocked; the independent sleep/clock-gap guard still
stops the clicker after a long suspend.

The macro recorder/player does not use the clicker's ydotool/xdotool fallback.
It requires `pynput` and an X11 display for global capture and injection; on
Wayland the Macros page reports recording as unavailable and limits playback to
XWayland. The page disables those controls rather than failing silently. No
macro operation asks for or uses `sudo`.

### Safe stop

Use any of these, in order of preference:

1. `Esc` in the WXB window, or the global emergency stop (default `Esc`).
2. `F9` (stop all engines), or click **Stop clicking** / **Stop macro**.
3. While the clicker or a macro is live, `F8` is a panic stop. `F8` while idle
   remains SOCD; a live screen recording is also stopped by the F8 panic path
   if no clicker/macro claims it first.
4. Move the pointer to the configured failsafe corner; clicking stops before
   the next press.
5. Close WXB. With **Stop engines on close** enabled (Settings → Behavior),
   the clicker, rotation, SOCD and Cordial clients are stopped and joined. A
   live screen recording is always finalized on close, even when that switch is
   off, because it is a file being written.

`Stop everything` releases the mouse buttons and held keys, including the
keys/buttons a macro injected. If the pointer backend itself has failed, WXB
still attempts the release and shows the backend error rather than leaving a
silent stuck button. A lock, sleep/clock gap, listener exit or input error stops
the macro as well; the page states the reason.

---

## Keyboard

| Keys | Action |
| --- | --- |
| `Ctrl K` | Open the command palette |
| `Ctrl 1–7` | Jump to Overview / Automation / Cordial / Settings / Macros / Recorder / GameMode |
| `Esc` | Close the palette, or stop every engine and release inputs |
| `Ctrl Q` | Quit |
| `F6` | Toggle the clicker (rebindable) |
| `F7` | Toggle the rotation macro, when its master switch is on (rebindable) |
| `F8` | Stop a live clicker or macro; otherwise toggle the SOCD cleaner (rebindable) |
| `F9` | Stop all engines and release inputs (rebindable) |
| `Ctrl+Alt+R` | Record / stop recording (rebindable) |
| `Ctrl+Alt+P` | Play / stop playback (rebindable) |
| `Ctrl+Alt+S` | Stop the macro only (rebindable) |
| `Ctrl+Alt+X` | Stop and save the screen recording (rebindable) |

---

## Architecture

| File | Responsibility |
| --- | --- |
| `wxb.py` | Entrypoint. |
| `wxb_studio.py` | Window, pages, flow layout, breakpoints, onboarding, palette, art. |
| `wxb_studio.css` | Visual system, kept out of Python. |
| `wxb_studio_service.py` | Settings normalization, engine lifecycle, hotkeys, Sober, alts. |
| `wxb_engine.py` | Click burst, rotation, SOCD state machine/pipeline, input sink. |
| `wxb_input_lab.py` | Toolkit-free Input Lab settings, remap rules/conflicts, read-only controller monitor and opt-in visualizer capture. |
| `wxb_overlay.py` | Transparent click-through keystroke/click-ring presentation window. |
| `wxb_macros.py` | Versioned macro format/store, X11 recorder, timed player with stop/release guards. |
| `wxb_recorder.py` | X11 capture capabilities/devices, FFmpeg argv, private output and bounded recorder lifecycle. |
| `roblox_runtime.py` | Cordial/Nightcap discovery, profiles, launches and quotas. |
| `lution_core/` | Vendored Lution launcher and Sober helpers. |
| `autoswitcher.py` | Standalone rotation switcher with its own desktop entry. |

The engine layer runs without a window. The GTK layer reads snapshots and calls
service methods, so an input-controller failure cannot freeze the shell.

---

## Verification

```bash
python3 test_studio.py
python3 test_input_lab.py
python3 test_clicker_hardening.py
python3 test_macros.py
python3 test_recorder.py
env GDK_DEBUG=gl-disable GSK_RENDERER=cairo xvfb-run -a python3 test_macros_x11.py
env GDK_DEBUG=gl-disable GSK_RENDERER=cairo xvfb-run -a python3 test_recorder_x11.py
env GDK_DEBUG=gl-disable GSK_RENDERER=cairo xvfb-run -a python3 test_studio_ui.py
timeout --kill-after=10s 120s env GDK_DEBUG=gl-disable GSK_RENDERER=cairo xvfb-run -a python3 test_global_hotkeys_x11.py
env PYTHONPATH=. xvfb-run -a python3 test_pointer_driver.py
env GDK_DEBUG=gl-disable GSK_RENDERER=cairo xvfb-run -a python3 test_new_features.py
python3 test_autoswitcher.py
python3 test_roblox_runtime.py
python3 test_lution_core.py
WXB_RUN_GAMEMODE_INTEGRATION=1 python3 test_gamemode_integration.py
python3 -m compileall -q .
python3 -m pyflakes *.py
```

Current results (full command block re-run end to end on 2026-11-21):

- `test_gamemode_integration.py` — **6 passed** on 2026-11-21 with
  `WXB_RUN_GAMEMODE_INTEGRATION=1`. It checks the installed Flatpak app and
  MangoHud layer, builds the real Sober argv with the MangoHud flag, verifies
  the real Gamescope extension runs `--version` without opening a window, and
  proves a throwaway GameMode client is registered and released by `gamemoded`.
  Without the opt-in variable the six checks are skipped; each component and
  the service check skip independently when its real tool is absent.
- `test_input_lab.py` — **17 passed** without a display. Covers bounded
  settings, profile validation and hotkey conflicts, capability explanations
  for every missing layer, hold/toggle remap state across multiple keyboards,
  direction-output translation, source-loss release, controller buttons/sticks/
  triggers/dead zone, controller disconnect/reconnect state, listener creation
  only for enabled switches, key formatting without a text sequence, and the
  direction-only timeline plus non-direction remap reset/unplug releases.
- `test_new_features.py` — **12 passed** under Xvfb. Covers portable-first-launch
  migration and visible warnings, app-filter whitelist/blacklist decisions,
  delay-cap/middle-button behavior, a missing foreground detector
  as an explicit start error, degraded capability hints, clicker control
  reload/reopen persistence, every page rendering, and X11 window geometry and
  always-on-top without external tools.
- `test_studio.py` — **82 passed**. Includes corrupt/empty/truncated settings,
  out-of-range clamps, old-deck-shaped files, malformed nested values, NaN,
  repeated start/stop, idle stop, repeated F6/F7/F8/F9 dispatch, shutdown with a
  running clicker, thread join, the macro master switch, ESC being reserved and
  routed as a stop, hold-mode press/release behavior, font/cursor/FPS wiring,
  flag allowlist/running refusals, flag profiles, environment validation,
  runtime/deployment state, playtime, damaged named-config refusal, partial
  Cordial-option rejection, overlapping-launch guard, asset/desktop checks, retired-file
  checks, the Flatpak `/tmp` X11 exposure, the elevated-to-owner game launch
  drop, the elevated Cordial session-bus handoff, profile add/delete and
  running-profile refusal, tiling-cycle hotkey dispatch through all styles,
  Input Lab profile round-trip/privacy/symlink refusal, controller-test
  capability refusal and start/stop, visualizer switch/error handling, Safe
  Reset stopping inputs and clearing remaps, independent per-axis SOCD mode
  selection, a Neutral fallback for configs that never mention SOCD, an engine
  sequence proving `Last win` on one axis and `Neutral` on the other, and a
  real vendored-Lution integration test under a throwaway HOME.
  That test drives the actual font, cursor, FPS, flag, environment and
  deployment modules; only the external Flatpak/bubblewrap commands are
  replaced with local fakes. A second elevated harness imports the modules
  while HOME points at a root-like throwaway directory, switches to the
  desktop home with `WXB_HOME`, writes and checks the concrete artifacts and
  their uid/gid, then restores the font/cursor files. Two isolation-guard
  tests run child processes:
  one with the redirect active must pass, and one with
  `WXB_TEST_NO_REDIRECT=1` must be rejected with the real-state paths.
- `test_clicker_hardening.py` — **29 passed**. Adds characterization of the
  existing CPS/duty/toggle contract; hotkey and toggle spam (24 repeats each);
  invalid CPS/duty/button/duration/pointer/jitter/corner input; empty,
  corrupt and foreign profile data; permission loss; display disconnect;
  corner failsafe; non-blocking UI stop; duration/fixed/jitter movement;
  lock pause/resume; F8 panic versus idle SOCD; clean shutdown/relaunch; a
  cross-instance clicker lock; an actual killed-process lock-release/relaunch;
  a simulated sleep/clock-jump stop; profile-directory `0700` and profile/lock
  file `0600` modes; refusal of symlinked profiles; release-failure
  propagation from the engine, rotation tap, stop path and hotkey-release path; and a
  **10,000-click** stress run at 1000 CPS (all clicks completed, button
  released, 15-second bound).
- `test_macros.py` — **21 passed**. This is the deterministic suite: recorder
  callbacks and the player sink are faked, so it runs without an X server.
  Covers the versioned format and event
  order, recorder callback order/control-chord filtering, player order, speed,
  loops, stop and lock interruption, release-on-error, sleep/clock-gap stop,
  a 10000-step stress pass, private/atomic files, symlink refusal, corrupt and
  unsupported imports, traversal-name refusal, orphan control-key releases
  when a hotkey stops recording, service record/play/stop hotkeys, tab-independent
  playback, clicker/macro exclusion, the X11/Wayland/no-display capability
  matrix and session-lock recording shutdown.
- `test_macros_x11.py` — **3 passed** under Xvfb's real X server. It drives
  actual Xlib **XTEST** pointer, button, scroll and keyboard input through the
  shipped `StudioService`/`MacroRecorder`/`MacroPlayer`/`InputSink` paths and
  observes playback with independent `pynput` listeners. It asserts capture
  order, coordinates, buttons, scroll, key press/release and relative timing;
  a two-loop replay at 2x finishes in about one macro duration instead of two;
  the live configured `Ctrl+Alt+M`/`N`/`B` record/play/stop chords each fire as
  real X key events; the stop chord interrupts both recording and playback;
  and a duplicate binding is refused by both `set_hotkeys` and `start_hotkeys`.
  This suite uses no fake sink and does not call the hotkey handler directly.
- `test_recorder.py` — **25 passed** without spawning FFmpeg. Covers
  normalization (including hex window IDs), shell-free argv for screen/window/
  region, positive regions and off-screen refusal, odd window dimensions,
  audio maps/mixes, missing monitor refusal, quality presets, CPU/hardware
  encoder selection and failure, symlink and non-directory refusal, timestamp
  collision naming, fake-process start/pause/resume/stop, SIGINT finalization,
  `0600` output, missing-output errors, disk guard, session-lock stop,
  shutdown, service settings/hotkey wiring and `stop_all`.
- `test_recorder_x11.py` — **6 passed** under Xvfb's real X server with a real
  FFmpeg child. Captures 320×240 region video, stops with SIGINT, checks
  `0600`, ffprobe dimensions/duration and an actual `ffmpeg -f null` decode;
  repeats start/stop three times with distinct files; pauses/resumes; muxes a
  real desktop monitor AAC stream; captures an `xmessage` window rectangle;
  and asserts the capability object does not advertise native Wayland.
- `test_studio_ui.py` — **46 passed** under Xvfb. Includes pointer routing,
  `Ctrl Q`, first-allocation 900px layout, privacy masking and restoration,
  macro gating, ESC stop order, captured keybindings, per-binding reset, slot
  hints (10 → `0`), rapid page/palette/onboarding toggling, empty/single/252-
  profile alt lists, long Unicode names, missing/malformed CSS, clicker
  start/stop, width reflow, second-instance raising, closing with
  stop-on-close enabled without stopping Sober, the pre-allocation responsive
  watchdog, the Settings behavior switch,
  config-driven navigation/Cordial option synchronization, every restored
  Lution control's service wiring, and real `compute_bounds` geometry for the
  privacy, macro, hold, hotkey, stop-on-close, low-graphics, quota, visuals,
  flags, environment and deployment rows at both the default width and
  900×700. The clicker advanced fold is expanded and checked at 900px,
  including duration, pointer, jitter, corner, failsafe, pause-on-lock,
  fixed-point capture and clicker profile save/load/delete.
  The compact Cordial page also checks the add/delete wiring and the live
  Launch button callback, plus dropdown synchronization when the global
  tiling hotkey changes the service style.
  The Macros page is built and all controls are present; a live fake playback
  keeps running across a page switch and releases its sink; the real X11
  `pynput` recorder starts and stops its listeners when the session supports it.
  The Recorder page exposes source/resolution/FPS/quality/encoder/folder/audio
  controls and its stop-hotkey hint; a fake recorder keeps running across a
  page switch, pauses/resumes and stops without spawning FFmpeg.
  The Input Lab check confirms there is still exactly one SOCD page, the lab is
  one collapsed expander inside that page, its live rows are mapped when
  opened, profile save/load/delete and Safe Reset work, the two visualizer
  switches remain independent through a fake overlay, and the real overlay
  class bounds colour/size/opacity without creating a text buffer.
  The SOCD preset check sets horizontal to `Last win` with vertical `Neutral`,
  then reverses the two axes, verifies both service states, and restores the
  shared test window to `Neutral`/`Neutral`.
  The runtime Monochrome test toggles the switch on and off, checks that the
  stylesheet provider reloads, the setting is saved to disk, the appearance
  controls are gated while mono is active, and the colour accent is restored
  when it is switched off.
  The geometry assertions reject zero-size allocations, clipping past the
  horizontal page edge, and overlapping sibling controls.
- `test_pointer_driver.py` — **ALL_POINTER_CHECKS_PASSED** under Xvfb; this
  suite reports pass/fail pointer checks rather than a numeric unit-test
  count. It covers every nav row, palette open/close, filtered result, page
  actions, keyboard and the 900px collapse; app-side logs are written to
  `/tmp/wxb-pointer-proof.log`.
- `test_autoswitcher.py` — **81 passed**.
- `test_roblox_runtime.py` — **134 passed**. Includes exact gapless tiling
  geometry for all five layouts, X11 frame-extent compensation for vertical
  cells, the `wmctrl` command path, PID-less Cordial window fallback,
  per-launch persistent window mapping, and the hard-cap/flag write before a
  profile can spawn. Profile-directory create/find/delete is covered against
  both Cordial roots, including duplicate, missing and symlink refusals.
- `test_lution_core.py` — **101 passed**, including applying a cached online
  cursor ZIP, licence/source persistence, pointer validity, unknown-cursor
  refusal, and keeping the downloaded pack after restore.
- `test_global_hotkeys_x11.py` — **4 passed** under Xvfb's real X server on
  2026-11-21. It uses `xdotool` to send real X11 key events to the shipped
  `start_hotkeys` listener and checks toggle/hold clicker control, rotation,
  SOCD dispatch, stop-all and the reserved global ESC stop. It also asserts
  that `start_hotkeys` does not return before XRecord is ready. Its clicker
  sink is a fake (no real clicks are injected), while the hotkey listener and
  key delivery are real. The live tiling harness
  `artifacts/cordial-live/hotkey_tiling_verify.py` additionally launches real
  Cordial clients and cycles every layout with the actual listener. These live
  suites are separate from the primary twelve above. The global-hotkey,
  macros-X11 and recorder-X11 suites were all rerun against the changed tree on
  2026-11-21 and exited 0; their counts are 4, 3 and 6 respectively.

**557 checks across the twelve primary suites**
(`82 + 17 + 29 + 21 + 3 + 25 + 6 + 46 + 12 + 81 + 134 + 101`), including the
macro-X11 (3) and recorder-X11 (6) acceptance checks. The command block has
**15 test entry points**: those twelve primary suites, plus the separate live
global-hotkey suite (**4**), the opt-in GameMode integration (**6** when its
environment variable is set) and the pointer driver (a pass/fail harness).
None of those three is included in the 557-check total.
Every test entry point except the opt-in GameMode integration — which
intentionally uses the real HOME so Flatpak can see user extensions — loads
`wxb_test_isolation` before its project imports, then invokes
`assert_isolated(...)` at startup before any test runs. The guard resolves 48 state paths,
checks HOME/XDG, `expanduser`, the inactive `passwd`/owner lookup, settings
and flag-profile paths, font, cursor, FPS, environment, playtime, deployment
and runtime paths, and a fresh subprocess that re-imports the Lution modules
with the XDG fallbacks removed. The two Studio guard tests prove the guard
passes with the redirect and fails when the redirect is bypassed. A full
headless run of
the GTK suite exits **0** with **empty stderr** when using
`GDK_DEBUG=gl-disable GSK_RENDERER=cairo` (the Xvfb software-renderer setup).
The current layouts were measured at 1180px and at 900×700 after a single
resize; the added rows have non-zero width and height, stay inside the page's
horizontal edge, and do not overlap their sibling buttons. Idle cost measured
**0.0% CPU over five seconds** and **88 MB RSS** under the software-rendered
Xvfb session.

What needs your real session: launching Sober or a Cordial client, enabling SOCD
or remaps against physical keyboards (`/dev/uinput` access was not assumed),
pressing F6–F9 from a physical keyboard, reading a physical controller through
`evdev`, and viewing the full-screen overlay on the target compositor. F6/F9 and
ESC were exercised synthetically in the live X11 session; the overlay's empty
input region was verified on the real X11 display, while full-screen positioning
on a multi-monitor desktop remains a manual check.

---

## Safety

- `Stop all` stops every worker, joins it, releases mouse buttons and keys, and
  stops the global hotkey listener on shutdown.
- A clicker press/release exception is reported in the Auto clicker card; the
  worker exits and the button is released. A display disconnect or permission
  loss never becomes a silent no-op or an endless spin.
- The GTK stop path signals the worker and returns without joining; the worker
  releases the button in its own `finally`. Shutdown still joins with a bound.
- The optional corner failsafe stops before the next press when the pointer is
  within 2 px of the selected corner. Locked sessions pause the loop and resume
  when the desktop reports unlocked.
- A wall-clock gap over the larger of 10 seconds or four click periods is
  treated as a sleep/clock jump: the clicker stops and asks you to press Start
  instead of clicking into a resumed desktop. A locked session pauses; a long
  suspend without a lock hint stops safely.
- An invalid or missing stylesheet falls back to GTK’s default theme instead of
  preventing startup.
- SOCD releases every virtual key on stop, error, unplug or permission loss.
- Input Lab remap profiles are validated before they can be saved, never contain
  raw events, and are written atomically at `0600` in a `0700` directory with
  symlink refusal. Safe Reset releases remapped/direction keys, stops the
  listeners and clears the active profile. Visualizer input is never persisted
  or uploaded; only the bounded appearance settings and key mappings are saved.
  The overlay refuses to stay open if the compositor will not grant its empty
  input region, so it cannot silently capture desktop clicks.
- Macro playback releases all keys and left/right/middle buttons on completion,
  stop, input error, lock, sleep/clock gap or listener loss. Macro files are
  validated, size/event capped, written atomically at `0600` under a `0700`
  directory, and symlinked files/directories are refused. Macro content is
  local-only; the app has no upload path.
- Screen capture runs as a monitored FFmpeg child, never on the GTK thread.
  Output is timestamped, reserved `0600` with `O_EXCL` (no overwrite), and
  Matroska keeps partial files recoverable. Stop/exit sends
  `SIGINT`, then bounded `SIGTERM`/kill; lock, disk-space, process-exit and
  audio/encoder failures are reported instead of leaving a silent empty file.
  Recordings are local-only and never uploaded.
- A failed rotation key press stops the macro and is shown on the Rotation
  card; it does not silently idle. The command backend reports that it cannot
  tap keys instead of pretending the rotation ran.
- Alt launches use `LaunchManager`; WXB signals only children it started.
- Settings contain preferences only; no cookies or tokens are read or stored.
- Clicker profiles contain only clicker preferences, are written atomically at
  mode `0600`, and a damaged profile is refused without replacing live values.
- Clicker profiles live in a mode `0700` directory; profile and lock files are
  created with mode `0600` using no-follow opens. Symlinked profile files or
  directories are refused. The ydotool/xdotool and loginctl calls use fixed
  argument lists, no shell, and bounded timeouts; clicker settings are never
  interpolated into a command line.
- Intended for accessibility, UI testing and repetitive tasks. Respect the
  terms of service of any game or app you use it with; WXB does not bypass
  anti-cheat or permission boundaries.
