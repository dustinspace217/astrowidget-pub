# astrowidget — Architecture Overview

A high-level summary of how astrowidget fits together: a Python fetcher, a Dart
scoring binary, and a QML / Qt front-end, joined by a single `state.json` file.

## Three-tier split

```
timer, 4×/day              systemd (Linux) · Task Scheduler (Windows)
    │
    ▼
Python fetcher (no persistent process)
    │  GET Open-Meteo (free, no key)  ──► cloud (GFS/ECMWF/ICON), precip,
    │                                     gusts, visibility, temp, dewpoint
    │  GET 7Timer    (free, no key)   ──► seeing, transparency
    │
    ▼
Dart scoring binary (subprocess, from the vendored scoring/ package)
    │  scoreLocation() × 2 per site (broadband, narrowband)
    │  moon geometry + astro dark window computed locally
    │
    ▼
<cache>/astrowidget/state.json (atomic write) + desktop notification on
                                                verdict transitions
    │
    ▼
KDE plasmoid (QML) — or the cross-platform Qt 6 desktop window
    compact: per-site colored dots · full: per-site columns + meteogram
    reads state.json; no network code, no secrets
```

## Why this split

- **Decoupling.** A flaky API never freezes your panel; a UI reload never
  re-fetches; the fetcher exits between runs and consumes zero steady-state
  resources.
- **No secrets at all.** Both data sources are free and keyless, so there is
  nothing to leak. Your config holds only per-site coordinates, kept `0600` on
  Linux/macOS and read only by the fetcher.
- **Scoring reuse.** The Dart binary is built from the vendored `scoring/`
  package — a frozen, self-contained copy of the `scoreLocation()` engine the
  author's `astroplan` app also uses (see `scoring/VENDORED.md`). astrowidget
  has no build- or run-time dependency on astroplan; the binary is invoked as a
  subprocess, and the fetcher is otherwise scoring-agnostic.

## Recommendation algorithm

For each site, for each of the next three nights:

1. Run `scoreLocation()` with `ImagingMode.broadband`.
2. Run `scoreLocation()` with `ImagingMode.narrowband`.
3. Each call returns a verdict (Excellent / Good / Marginal / Poor / Don't Bother)
   and any safety vetoes that fired (cloud >95%, precip, wind, dew).
4. If both pass (verdict ≥ Marginal, no vetoes) → **BB+NB**.
5. Else if only narrowband passes → **NB only**.
6. Else → **Neither**.

The scoring engine handles the broadband/narrowband distinction internally: its
moon and darkness weights drop to near zero under narrowband, reflecting that
narrowband filters reject ~99% of moonlight.

## Notification model

The fetcher diffs the new `state.json` against the previous run's and notifies
when:

- Tonight's verdict transitions upward (improvement)
- Tonight's verdict degrades day-of (you're about to image, conditions tanked)
- Astronomical dark begins at a site with a GO verdict (imaging-start reminder)

All three are user-configurable.

## Update cadence

The GFS model (which 7Timer and Open-Meteo's GFS both derive from) refreshes
every 6 hours, so the fetcher runs four times per day at 00:10 / 06:10 / 12:10 /
18:10 (UTC on Linux, local time on Windows). Every call is free and keyless —
no credits, no quota to track: roughly four Open-Meteo requests and one 7Timer
request per site per run, well under both services' free limits.

## Storage

- `<config>/astrowidget/config.toml` — your configuration (per-site
  coordinates; no API key). Kept `0600` on Linux/macOS; never committed.
- `<cache>/astrowidget/state.json` — current forecast state, atomically
  rewritten by every fetcher run.
- `<cache>/astrowidget/state.prev.json` — prior state, for diff-based
  notifications.
- `bin/astrowidget-score` — the compiled Dart binary (gitignored; built from
  the vendored `scoring/` package).

`<config>` and `<cache>` are the OS-standard locations (`~/.config` and
`~/.cache` on Linux; the matching `%USERPROFILE%` / `%LOCALAPPDATA%` paths on
Windows). No telemetry, no analytics, no remote logging.
