# Auto Brightness

Ambient-light auto-brightness for [DankMaterialShell](https://github.com/AvengeMedia/DankMaterialShell).

The daemon is derived from [Kumar-Jy/nabu-pkgs](https://github.com/Kumar-Jy/nabu-pkgs)
(MIT, © Kumar-Jy) — the perceptual curves, EMA filtering, ramp smoothing, bias learning and
suspend/lock hooks all come from there. This fork makes the backlight backend portable
(DMS IPC / KDE PowerDevil / sysfs instead of a hardcoded Xiaomi Pad 5 path) and packages
everything as a DMS plugin.

## Features

- Reads ambient light (lux) from `iio-sensor-proxy` — works on any device that exposes an
  ambient light sensor, not just Xiaomi hardware
- **Four perceptual curves**: standard / bright / dim / battery
- **EMA filtering** so a hand passing over the sensor doesn't swing the brightness
- **Smooth ramps** (10 Hz slew) so changes are not jarring
- **Learns your preference**: drag the brightness slider and the daemon records the offset
  (`user_bias`, ±35 %) and keeps applying it
- **Screen-lock and suspend hooks** — stops adjusting while the screen is off
- **Backend auto-detection:**
  1. DMS IPC — `dms ipc call brightness set <pct> [device]`
  2. KDE PowerDevil — DBus `setBrightnessSilent`
  3. sysfs — `/sys/class/backlight/*`, first writable device
- Unix-socket IPC so `status` / `toggle` / `--profile` work from a terminal

## Requirements

- Python 3 with PyGObject (`python-gobject`) — already required by iio-sensor-proxy tooling
- An ambient light sensor exposed via `iio-sensor-proxy`
- One of: DMS, KDE Plasma (PowerDevil), or a writable `/sys/class/backlight/*`

## Install

```sh
git clone https://github.com/LilFishyChan/dms-auto-brightness \
  ~/.config/dms/plugins/nabuAutoBrightness
dms ipc call plugins reload nabuAutoBrightness
```

Then toggle it from the control center.

> The directory name must stay `nabuAutoBrightness` — it has to match `id` in `plugin.json`.

## Settings

| Setting | Purpose |
| --- | --- |
| Curve | Which lux → brightness mapping to use |

`standard` is a balanced indoor/outdoor curve, `bright` boosts visibility in well-lit rooms,
`dim` lowers glare for night reading, `battery` caps brightness at 68 %.

## CLI

The daemon shipped in `scripts/` can also be used standalone:

```sh
python3 scripts/nabu-autobrightness.py status
python3 scripts/nabu-autobrightness.py --enable | --disable | --toggle
python3 scripts/nabu-autobrightness.py --profile bright
python3 scripts/nabu-autobrightness.py --bias +10     # nudge the whole curve
python3 scripts/nabu-autobrightness.py --test         # live lux monitor, no brightness change
```

## How it works

```
iio-sensor-proxy LightLevel (lux)
        ↓
EMA filter + linear-ramp follower
        ↓
log-space anchor interpolation (per profile) + user_bias
        ↓
DMS IPC  →  PowerDevil  →  sysfs
```

Configuration lives in DMS's `plugin_settings.json` (`nabuAutoBrightness` node) and is
hot-reloaded every 2 s, including switching profiles. When run outside DMS it falls back to
`~/.config/nabu-autobrightness/config.json`.

## Layout

```
plugin.json                      manifest
AutoBrightness.qml               control-center toggle + Process supervision
AutoBrightnessSettings.qml       settings page
scripts/nabu-autobrightness.py   the daemon (CLI + daemon mode)
```

## OSD note

Under DMS the daemon goes through `dms ipc call brightness set`, and DMS shows its brightness
OSD on every change. If the automatic adjustments' OSD bothers you, turn off
**Settings → OSD → Brightness** in DMS; volume and workspace OSDs are unaffected.

## License

MIT — see `LICENSE`. Original work © Kumar-Jy; see the upstream project.
