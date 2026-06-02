# astrowidget

A KDE Plasma 6 widget for astrophotographers. At-a-glance go/no-go conditions
for up to three imaging sites tonight (and the next two nights), with verdicts
for broadband and narrowband imaging modes.

Forecast data comes entirely from **free, no-key** sources:
[Open-Meteo](https://open-meteo.com/) (multi-model cloud cover, precipitation,
wind gusts; data under CC BY 4.0) and [7Timer!](https://www.7timer.info/)
(seeing and transparency). No account, no API key, no subscription.

## Status

Pre-release. Runs end-to-end on Fedora 43 KDE Plasma 6 and should work on other
Plasma 6 distributions without modification. A cross-platform desktop window
(Windows / macOS / Linux) ships alongside the plasmoid — see below.

## Features

- **Per-site verdict at a glance** — colored dots in your Plasma panel, one
  click for the full forecast.
- **Broadband / Narrowband / Neither** recommendation per site, per night. Same
  scoring engine the author's astroplan mobile app uses.
- **Astro-specific factors** — transparency, seeing, astronomical dark window,
  moon geometry, dew spread — alongside standard weather variables.
- **Multi-model cloud cover** — cloud forecasts from GFS, ECMWF, and ICON
  simultaneously, with the model convergence shown so you can judge agreement.
- **Astro-dark notification** — fires when astro dark begins at a site with a
  GO verdict. Serves as the imaging-start reminder; disable it in config.
- **Three nights ahead** — tonight + next two via tabs. Useful for scheduling at
  remote sites.
- **Night-vision mode** — red-only palette preserves dark adaptation near a scope.
- **No background process** — fetcher runs 4×/day on a timer and exits; the
  plasmoid lives in `plasmashell` with zero network code.
- **No API key** — both data sources are free and keyless.

## Requirements

- KDE Plasma 6 (Wayland or X11) — for the plasmoid. (The cross-platform desktop
  window needs only Qt 6 / PySide6; see WINDOWS.md.)
- Python 3.11 or later
- Dart SDK 3.11 or later (to build the scoring binary)
- `requests` (`pip install requests`)
- `notify-send` (provided by libnotify on most distributions)

## Installation

```bash
# Build the Dart scoring binary from the self-contained scoring/ package.
# (dart build cli, NOT dart compile exe — geoengine ships native-asset build hooks.)
cd scoring && dart pub get && dart build cli -t bin/score_location.dart -o build
cp build/bundle/bin/score_location ../bin/astrowidget-score && cd ..

# Install the plasmoid package.
kpackagetool6 --type Plasma/Applet --install plasmoid/space.dustin.astrowidget

# Install systemd units.
cp systemd/astrowidget-fetch.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now astrowidget-fetch.timer

# Create your config from the template.
mkdir -p ~/.config/astrowidget
cp config.example.toml ~/.config/astrowidget/config.toml
chmod 600 ~/.config/astrowidget/config.toml
# Edit the file — add your imaging sites (lat/lon/timezone). No API key needed.

# Run the fetcher once to populate state.json.
./fetcher/astrowidget_fetch.py

# Add the plasmoid to your panel: right-click panel → "Add or Manage Widgets" → search "astrowidget".
```

A combined `install.sh` runs all of the above.

## Windows & macOS

The KDE plasmoid is Linux-only, but the fetcher, the scoring engine, and a
cross-platform **Qt 6 desktop window** run on Windows and macOS too. See
**[WINDOWS.md](WINDOWS.md)** for the Windows setup (Task Scheduler scheduling,
toast notifications, and the desktop app), and `desktop/README.md` for the
window itself.

## Configuration

See `config.example.toml` for the full template. Minimum to get running:

1. Add one or more `[[sites]]` blocks with real lat/lon and a timezone.
2. (Optional) Tune per-site thresholds in `[thresholds.<site_id>]` blocks.
3. (Optional) Adjust `[notifications]` to your taste.

The config holds only your site coordinates (no API key). The fetcher keeps it
`chmod 600` on Linux/macOS so other local users can't read your locations.

## Notification rules

| Trigger | Default |
|---|---|
| Tonight's verdict improves (Neither → NB, NB → BB+NB) | ON |
| Tonight's verdict degrades, day-of | ON |
| Astro dark begins at a site with a GO verdict | ON |
| Suppress all notifications during astro dark | OFF |

The astro-dark-begins notification is the imaging-start reminder. It is not
suppressed during dark hours by default — that's the point of it.

## Project structure

```
astrowidget/
├── fetcher/                              # Python fetcher (Open-Meteo + 7Timer)
├── scoring/                              # Vendored, self-contained Dart scoring engine
├── bin/                                  # Compiled Dart scoring binary (gitignored)
├── plasmoid/space.dustin.astrowidget/    # QML plasmoid package (Linux / Plasma)
├── desktop/                              # Cross-platform Qt 6 desktop app (Win/macOS/Linux)
├── systemd/                              # User-level systemd unit files (Linux)
├── windows/                              # Windows installer + Task Scheduler script
├── tests/                                # pytest suite
├── docs/design/                          # Public design documentation
├── config.example.toml                   # Configuration template
├── install.sh                            # Linux all-in-one installer
├── WINDOWS.md                            # Windows / macOS setup guide
├── LICENSE                               # GPL-3.0-or-later
└── README.md
```

## How it works

```
timer (4×/day)            Linux: systemd · Windows: Task Scheduler
    │
    ▼
Python fetcher
    │  GET from Open-Meteo (free)  ──► cloud (GFS/ECMWF/ICON), precip, wind, dew
    │  GET from 7Timer (free)      ──► seeing, transparency
    │
    ▼
Dart scoring binary (subprocess)
    │  scoreLocation(site, hourly, mode=broadband)
    │  scoreLocation(site, hourly, mode=narrowband)
    │  Computes moon geometry + astro dark window locally
    │
    ▼
state.json (atomic write)
    │
    ▼
QML reads state.json (no network, no secrets)
```

## License

`astrowidget` is released under the **GNU General Public License v3.0 or later**.
See `LICENSE` for the full text.

The Dart scoring engine under `scoring/` is **vendored** (a frozen copy) from
the author's separate `astroplan` project — astrowidget reuses its ideas and
methods but is a fully independent application with no build- or run-time
dependency on astroplan. See `scoring/VENDORED.md`.

## Acknowledgments

- Cloud / precipitation / wind data from [Open-Meteo](https://open-meteo.com/),
  under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- Seeing / transparency from [7Timer!](https://www.7timer.info/) (free, based on
  the NOAA GFS model).
- Scoring engine vendored from the author's astroplan project (same author,
  GPL-compatible).
- KDE Plasma 6 — the platform the plasmoid targets.

## Contributing

A personal project shared publicly. If you find a bug or want to suggest a
feature, open an issue.

## Privacy

This widget does not send your data anywhere. The fetcher talks only to
Open-Meteo and 7Timer (both anonymous, no key). All state is local under your
user cache directory (`~/.cache/astrowidget/` on Linux). No telemetry, no
analytics, no third-party trackers.
