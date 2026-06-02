# astrowidget — Project Context for Claude

## What This Is
A KDE Plasma 6 plasmoid that shows astrophotography-specific weather forecasts
for 1–3 configured imaging sites at a glance. Sourced from Open-Meteo (free,
multi-model cloud + precip + gusts) and 7Timer (free, seeing + transparency) —
no API key or subscription needed. Verdicts per site: Broadband viable /
Narrowband only / Neither.
Notification on transitions including during astro dark (alert is the
imaging-start reminder).

## Tech Stack
- **Fetcher:** Python 3.12+ (Fedora 43 stock), single external dep: `requests`.
- **Scoring:** Dart native binary built (via `dart build cli`) from the in-repo,
  self-contained `scoring/` package — a vendored, pure-Dart copy of the astroplan
  scoring engine (see `scoring/VENDORED.md`). Invoked as a subprocess by the
  Python fetcher. No build- or run-time dependency on astroplan.
- **Plasmoid:** QML, Plasma 6 APIs. Compact representation (panel) + full
  representation (popup).
- **Scheduling:** systemd user timer, 4×/day (Linux); Task Scheduler (Windows).

## Key Files
- `fetcher/astrowidget_fetch.py` — fetcher entry point.
- `plasmoid/space.dustin.astrowidget/` — QML plasmoid package.
- `scoring/` — self-contained vendored Dart scoring engine (tracked source;
  `dart build cli` produces the binary). See `scoring/VENDORED.md`.
- `bin/astrowidget-score` — compiled Dart scoring binary (gitignored;
  built from `scoring/`).

## Key Commands
- `python3 fetcher/astrowidget_fetch.py` — run fetcher once
- `pytest tests/` — run test suite
- `cd scoring && dart pub get && dart build cli -t bin/score_location.dart -o build && cp build/bundle/bin/score_location ../bin/astrowidget-score` — build scoring binary
- `kpackagetool6 --type Plasma/Applet --upgrade plasmoid/space.dustin.astrowidget` — install/upgrade plasmoid
- `systemctl --user enable --now astrowidget-fetch.timer` — enable scheduled fetches

## Conventions
- Python: 4-space indent, comprehensive docstrings, type hints.
- QML: 4-space indent (QML's convention), comments explain non-obvious bindings.
- Comment thoroughly per workspace-level rules — every function explains what,
  why, where data comes from. New language features get inline teaching comments.
- All code must pass `pre-commit` checks before commit (TBD: ruff for Python,
  high-entropy / lat-lon guard).

## Privacy
- The repo is intended for eventual public sharing; nothing personal ships.
- `config.toml` and `state.json` are gitignored. The shipped artifacts are
  `config.example.toml`, code, README, and LICENSE.
- Pre-commit hook (TBD) refuses commits containing API keys or lat/lon-shaped
  float pairs in non-template files.

## Self-Contained Scoring (vendored, 2026-05-31)
astrowidget is a SEPARATE, INDEPENDENT app. The Dart scoring engine under
`scoring/` is a VENDORED (frozen) copy of astroplan's engine — astrowidget
reuses the ideas/methods but must NEVER modify the astroplan project, and has no
build/run dependency on it. The vendored set is the transitive import closure of
`scoring/bin/score_location.dart`; re-vendoring instructions and the two
deliberate deltas from upstream (a Flutter-free `app_logger`, and astrowidget's
own `score_location.dart` scoring logic) are in `scoring/VENDORED.md`. The copy
may diverge from astroplan over time — that independence is intentional.
