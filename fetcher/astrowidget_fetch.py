#!/usr/bin/env python3
"""
astrowidget-fetch — pulls forecast data from Open-Meteo (free, no key) and
7Timer (free, no key), merges per-site, invokes the Dart scoring binary
to produce per-site verdicts, and writes state.json for the QML plasmoid to
read. Designed to run from a systemd user timer 4×/day.

Architecture overview: docs/design/architecture.md

Exit codes:
    0  — success (state.json updated; notifications emitted if needed)
    2  — configuration error (missing/malformed config, wrong perms, etc.)
    3  — API error (persistent network failure across all sites)
    4  — scoring binary error (subprocess failure or timeout)
    5  — API returned a malformed/unexpected response (shape mismatch)

Why this is a single script rather than a package:
    The whole thing fits well under 800 lines and runs once per fetch then
    exits. A multi-file package would scatter the linear pipeline across
    modules with no functional benefit. If this grows beyond ~1000 lines or
    needs additional entry points, refactor then.
"""

# Standard library only, except `requests` which is the conventional choice
# over urllib for HTTPS POST with JSON. Single external dep keeps the
# installation surface small and avoids a virtualenv requirement.
from __future__ import annotations

import base64
import json
import math
import os
import re
import stat
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

# Matches a trailing ISO-8601 timezone offset like "+00:00", "-0800", "+05:30".
# Used to detect whether a timestamp already carries an offset before we
# (wrongly) append one. A bare `"+" not in s` check misses NEGATIVE offsets.
_TZ_OFFSET_RE = re.compile(r"[+-]\d{2}:?\d{2}$")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

def _generic_cache_dir() -> Path:
	"""
	Resolve the OS cache directory matching Qt's
	QStandardPaths::GenericCacheLocation, so the cross-platform desktop app
	(desktop/, which reads state.json through that Qt API) and this fetcher
	agree on where state.json lives — on every platform, not just Linux.

	Qt's GenericCacheLocation, from the Qt 6 docs table (verified 2026-05-31):
	    Windows  ->  %LOCALAPPDATA%\\cache  (C:\\Users\\<u>\\AppData\\Local\\cache)
	    macOS    ->  ~/Library/Caches
	    Linux    ->  $XDG_CACHE_HOME, else ~/.cache

	sys.platform is "win32" on all Windows, "darwin" on macOS, "linux" on Linux.
	Returns the GENERIC cache root; the caller appends the "astrowidget" subdir.
	"""
	if sys.platform == "win32":
		# %LOCALAPPDATA% is set in any normal user session; fall back to its
		# documented default layout only if the variable is somehow absent.
		local = os.environ.get("LOCALAPPDATA")
		base = Path(local) if local else Path.home() / "AppData" / "Local"
		return base / "cache"
	if sys.platform == "darwin":
		return Path.home() / "Library" / "Caches"
	# Linux / other XDG platforms. Qt honors XDG_CACHE_HOME when set, so we
	# match it — otherwise a user who relocated their cache would desync the
	# two apps (fetcher writing one place, desktop app reading another). The XDG
	# spec says a relative XDG_CACHE_HOME must be ignored; require an absolute
	# path so a stray relative value can't desync the two under different CWDs.
	xdg = os.environ.get("XDG_CACHE_HOME")
	return Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".cache"


# Canonical paths. CONFIG is an XDG-style dotfolder on every OS (the fetcher is
# its SOLE reader, so it needs no cross-process / per-OS mapping). CACHE tracks
# Qt's GenericCacheLocation per-OS so the desktop app finds the same state.json.
CONFIG_PATH = Path.home() / ".config" / "astrowidget" / "config.toml"
CACHE_DIR = _generic_cache_dir() / "astrowidget"
STATE_PATH = CACHE_DIR / "state.json"
PREV_STATE_PATH = CACHE_DIR / "state.prev.json"

# The Dart binary lives next to this script's parent (bin/ peer of fetcher/).
# Resolved relative to this file so the install location is movable.
SCRIPT_DIR = Path(__file__).resolve().parent
# Windows executables need the .exe suffix for subprocess to locate them
# (CreateProcess does not auto-append it for an absolute path). `dart build cli`
# on Windows produces score_location.exe, which install.ps1 copies to
# bin/astrowidget-score.exe.
def _score_exe_name(platform: str) -> str:
	"""The scoring binary's filename for a given sys.platform value — .exe on
	Windows (CreateProcess needs the suffix for an absolute path), bare on
	Linux/macOS. A pure function of the platform string so both branches test."""
	return "astrowidget-score.exe" if platform == "win32" else "astrowidget-score"


SCORING_BINARY = SCRIPT_DIR.parent / "bin" / _score_exe_name(sys.platform)

# Open-Meteo free, no key. Hourly forecast endpoint.
# https://open-meteo.com/en/docs
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Hourly variables we request from Open-Meteo. Names match the
# astroplan HourlyWeather.fromJson() snake_case schema directly, so the
# Dart scoring binary parses them with zero translation.
OPEN_METEO_HOURLY_VARS = ",".join([
	"cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
	"relative_humidity_2m", "temperature_2m", "dewpoint_2m",
	"wind_speed_10m", "wind_gusts_10m",
	"precipitation_probability", "precipitation",
	"visibility",
	# 250 hPa jet-stream wind — the Phase-1 seeing input (spec §5). Some Open-Meteo
	# models omit pressure-level winds; merge_hourly reads it None-preserving so a
	# missing hour is "no jet data" (skipped), never a calm-jet 0.
	"wind_speed_250hPa",
])

# Days of forecast to request from Open-Meteo. We score 3 nights (tonight +2),
# but a western-US astronomical dark window for "+2 nights" ends ~78 hours out,
# which exceeds a 72-hour (3-day) forecast — the tail would be scored on
# fabricated defaults. Requesting 4 days guarantees the +2 window is fully
# covered. (Fix for the horizon-truncation finding, 2026-05-28 review.)
OPEN_METEO_FORECAST_DAYS = 4

# Open-Meteo Air-Quality API base URL (separate host from the weather API). Source
# of AOD-550nm for the Phase-1 transparency factor. Free, no key. (spec §5/§7)
OPEN_METEO_AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# Open-Meteo models used in the cloud ensemble + convergence display. Three
# DISTINCT models (ECMWF, NOAA GFS, DWD ICON). We deliberately do NOT include
# `best_match` here — it is itself an internal blend of these, so averaging it
# alongside its own constituents would double-count (scope-audit catch
# 2026-05-29). Open-Meteo returns each model's cloud_cover under a suffixed key
# (cloud_cover_gfs_seamless, etc.) when `models=` lists more than one.
OPEN_METEO_CONVERGENCE_MODELS = ["gfs_seamless", "ecmwf_ifs04", "icon_seamless"]

# ─────────────────────────────────────────────────────────────────────────────
# Cloud ENSEMBLE
#
# For every site we fuse cloud from Open-Meteo's three DISTINCT physical models
# into a consensus (equal-weight mean) used for the scoring cloud factor, and
# report the spread as a confidence signal. Distinct documented models only, no
# best_match (a blend). The three scoring models:
#   - Open-Meteo ECMWF, GFS, ICON
# Equal weight = zero tunable parameters (no magic numbers). Ensemble consensus
# reliably beats any single model — that's the accuracy gain, and it formalizes
# the user's trust in multi-model "convergence".

# 7Timer! ASTRO — free, global, no key. Used for seeing/transparency at every
# site. Verified live 2026-05-29.
# https://www.7timer.info/  (3-hourly, 72h, product=astro)
SEVENTIMER_URL = "https://www.7timer.info/bin/api.pl"

# 7Timer scales are 1-8 with 1 = best. Documented at 7timer.info. We map each to
# a human label vocabulary for consistent display.
#   seeing 1-8: 1=<0.5" (excellent) … 8=>2.5" (terrible)
#   transparency 1-8: 1=<0.3 mag (excellent) … 8=>1.0 mag (poor)
_SEVENTIMER_SEEING_LABELS = {
	1: "Excellent", 2: "Above Average", 3: "Average", 4: "Average",
	5: "Below Average", 6: "Below Average", 7: "Poor", 8: "Cloudy",
}
_SEVENTIMER_TRANSPARENCY_LABELS = {
	1: "Excellent", 2: "Above Average", 3: "Average", 4: "Average",
	5: "Below Average", 6: "Below Average", 7: "Poor", 8: "Cloudy",
}


def seventimer_seeing_label(raw: float | None) -> str:
	"""Maps a 7Timer seeing index (1-8, LOWER=better) to a shared label."""
	if raw is None:
		return "—"
	return _SEVENTIMER_SEEING_LABELS.get(int(round(max(1, min(8, raw)))), "—")


def seventimer_transparency_label(raw: float | None) -> str:
	"""Maps a 7Timer transparency index (1-8, LOWER=better) to a shared label."""
	if raw is None:
		return "—"
	return _SEVENTIMER_TRANSPARENCY_LABELS.get(int(round(max(1, min(8, raw)))), "—")


# Request timeout (seconds) per API call.
HTTP_TIMEOUT = 30

# Subprocess timeout (seconds) for the Dart scoring binary.
SCORING_TIMEOUT = 10


# ─────────────────────────────────────────────────────────────────────────────
# Configuration loading
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict[str, Any]:
	"""
	Reads ~/.config/astrowidget/config.toml after verifying that file
	permissions are 0600 (refuses world-readable configs to keep the
	site's lat/lon coordinates from leaking to other local users).

	Returns: parsed config dict.
	Exits: code 2 on any config failure (missing file, bad perms, malformed
	    TOML, missing required fields, duplicate site IDs, Null Island
	    placeholders). Always emits a user-facing notify-send on failure so
	    silent misconfiguration is impossible.
	"""
	if not CONFIG_PATH.exists():
		_fail_config(
			"Configuration missing",
			f"Create {CONFIG_PATH} from config.example.toml and add at least "
			f"one site. See README for details.",
		)

	# Permission check — refuse world-readable configs (the file carries the
	# site's lat/lon, privacy-sensitive even with no API key present).
	# Unix only: st_mode permission bits are a POSIX concept. On Windows os.stat
	# reports synthetic bits (usually 0o666) that don't reflect the real ACL, so
	# the 0o077 test would reject every config. Windows file privacy comes from
	# NTFS ACLs + the file living under the user profile, so we skip the bitmask
	# check there rather than enforce one that can never pass.
	if sys.platform != "win32":
		st = CONFIG_PATH.stat()
		mode = stat.S_IMODE(st.st_mode)
		if mode & 0o077:  # any group/other bits set
			_fail_config(
				"Configuration permissions too open",
				f"{CONFIG_PATH} must be chmod 600. Current mode: {oct(mode)}.",
			)

	try:
		with CONFIG_PATH.open("rb") as f:
			cfg = tomllib.load(f)
	except (tomllib.TOMLDecodeError, OSError) as e:
		_fail_config("Configuration parse error", str(e))

	# Validate required fields. Fail loud with actionable messages.
	sites = cfg.get("sites", [])
	if not sites:
		_fail_config(
			"No sites configured",
			f"Add at least one [[sites]] block to {CONFIG_PATH}.",
		)

	# Duplicate site-id check — the state.json shape uses id as the key for
	# the scoring-output merge, so duplicates silently collide otherwise.
	seen_ids: set[str] = set()
	for site in sites:
		sid = site.get("id")
		if not sid:
			_fail_config("Site missing 'id'", f"Add an id field to each [[sites]] block in {CONFIG_PATH}.")
		if sid in seen_ids:
			_fail_config(
				"Duplicate site id",
				f"Site id '{sid}' is used more than once in {CONFIG_PATH}. "
				f"Each [[sites]] block must have a unique id.",
			)
		seen_ids.add(sid)

		# Null Island sentinel — placeholder values from the example file.
		if site.get("lat", 0) == 0.0 and site.get("lon", 0) == 0.0:
			_fail_config(
				"Site at Null Island",
				f"Site '{sid}' has placeholder lat/lon (0.0, 0.0). "
				f"Edit {CONFIG_PATH} with real coordinates.",
			)

		# Visibility flag.
		#   primary: shown as a full always-on column (true) vs a collapsed,
		#     click-to-expand verdict chip (false).
		# Defaults true so a minimal site needs no flag. A non-bool value is
		# rejected loudly: the string "false" is truthy in Python, so a typo
		# would silently flip a site to the wrong layout on every run.
		if "primary" in site and not isinstance(site["primary"], bool):
			_fail_config(
				f"Invalid 'primary' for site '{sid}'",
				f"primary must be the TOML boolean true or false, "
				f"got {site['primary']!r}.",
			)
		site["primary"] = bool(site.get("primary", True))

		# The `managed` flag (spec §4) selects the scoring MODE for this site:
		#   false (default) = HOME mode — a clear-starved site where the user images
		#     cloudy, low-precip nights gambling for sucker holes, so the precip
		#     equipment veto fires (an uncovered scope) and the output is the best
		#     clear WINDOW plus an honest per-filter verdict.
		#   true  = REMOTE/managed mode — a hosted, weatherproof dome that self-gates
		#     to clear sky, so the precip equipment veto is skipped (the dome
		#     self-protects) and the widget gives a clean go/no-go.
		# Same loud-rejection contract as `primary` (the string "false" is truthy in
		# Python, so a typo would otherwise silently flip the mode). Documented for
		# other users in config.example.toml.
		if "managed" in site and not isinstance(site["managed"], bool):
			_fail_config(
				f"Invalid 'managed' for site '{sid}'",
				f"managed must be the TOML boolean true or false, "
				f"got {site['managed']!r}.",
			)
		site["managed"] = bool(site.get("managed", False))

		# Optional per-site `bortle` light-pollution class (spec §5a/§8): sets the
		# sky-brightness baseline. Absent → None, and the Dart scorer falls back to a
		# documented default baseline so the geometry-aware moon penalty still applies
		# EVERYWHERE (the Phase-1 fix — see locationSkyBrightnessScore). When present
		# it must be an int 1–9; anything else is rejected loudly rather than silently
		# clamped, since a typo'd Bortle would quietly mis-score every night. A future
		# release may auto-derive this from lat/lon; the override always wins.
		if "bortle" in site:
			bval = site["bortle"]
			# bool is an int subclass — exclude it so a stray `true` can't read as
			# Bortle 1.
			if (isinstance(bval, bool) or not isinstance(bval, int)
					or not (1 <= bval <= 9)):
				_fail_config(
					f"Invalid 'bortle' for site '{sid}'",
					f"bortle must be an integer 1–9, got {site['bortle']!r}.",
				)
			site["bortle"] = bval
		else:
			site["bortle"] = None

	# Per-site veto threshold validation. Adversarial review flagged that
	# wind_max_kmh = -5 (etc.) silently produces "Neither" for every hour —
	# valid TOML, nonsensical scoring outcome. Reject negative or absurd
	# values up front with a clear error rather than letting them flow
	# through the Dart binary as silent vetoes.
	thresholds_section = cfg.get("thresholds", {})
	if isinstance(thresholds_section, dict):
		for sid, t in thresholds_section.items():
			if not isinstance(t, dict):
				continue
			for key, lo, hi in (
				("wind_max_kmh", 0.0, 200.0),
				("precip_max_pct", 0.0, 100.0),
				("dew_spread_min_c", 0.0, 50.0),
			):
				if key in t:
					try:
						v = float(t[key])
					except (TypeError, ValueError):
						_fail_config(
							f"Invalid threshold for site '{sid}'",
							f"{key} must be a number, got {t[key]!r}.",
						)
					if not (lo <= v <= hi):
						_fail_config(
							f"Out-of-range threshold for site '{sid}'",
							f"{key}={v} is outside the allowed range [{lo}, {hi}].",
						)

	return cfg


def _fail_config(title: str, body: str) -> None:
	"""
	Emits a notify-send with critical urgency, prints to stderr, exits code 2.
	The notification makes config errors impossible to silently miss.
	"""
	sys.stderr.write(f"astrowidget: CONFIG ERROR: {title} — {body}\n")
	_notify(title, body, urgency="critical")
	sys.exit(2)


# ─────────────────────────────────────────────────────────────────────────────
# API fetchers
# ─────────────────────────────────────────────────────────────────────────────

def fetch_open_meteo(lat: float, lon: float) -> dict[str, Any]:
	"""
	GETs Open-Meteo's forecast API and returns the parsed JSON.

	Always uses Open-Meteo's default best-match blend — multi-model
	convergence (gfs_seamless + ecmwf_ifs04 + icon_seamless, etc.) is a
	v2 feature that requires per-model column-name handling (Open-Meteo
	suffixes variables like `cloud_cover_gfs_seamless` when multiple
	models are queried). Deferring keeps v1 simple and lets scoring use
	the standard `cloud_cover`/`wind_speed_10m`/etc. keys directly.

	Receives: lat, lon — decimal degrees.
	Returns: dict with `hourly` key containing time + variable arrays.
	"""
	params: dict[str, Any] = {
		"latitude": lat,
		"longitude": lon,
		"hourly": OPEN_METEO_HOURLY_VARS,
		# 4 days, not 3 — the +2-night dark window can end ~78h out for
		# western-US sites, beyond a 72h horizon. See OPEN_METEO_FORECAST_DAYS.
		"forecast_days": OPEN_METEO_FORECAST_DAYS,
		"timezone": "UTC",
		"wind_speed_unit": "kmh",
		"temperature_unit": "celsius",
		"precipitation_unit": "mm",
	}
	r = requests.get(OPEN_METEO_URL, params=params, timeout=HTTP_TIMEOUT)
	r.raise_for_status()
	parsed = r.json()
	if not isinstance(parsed, dict) or "hourly" not in parsed:
		raise RuntimeError("Open-Meteo response missing 'hourly' block")
	return parsed


def fetch_open_meteo_convergence(
	lat: float, lon: float, models: list[str]
) -> dict[str, list[Any]]:
	"""
	Fetches per-model cloud-cover forecasts from Open-Meteo for convergence
	analysis (the user's "each model and the convergences" request).

	A single Open-Meteo call with `models=a,b,c` returns each model's
	cloud_cover under a suffixed key — `cloud_cover_gfs_seamless`, etc. We
	request ONLY cloud_cover here (cheap) and keep the primary forecast call
	on best_match. Best-effort: on any failure this returns {} and the
	caller simply omits the convergence panel — it never aborts the run.

	Receives: lat, lon — decimal degrees; models — list of OM model ids.
	Returns: dict with key "time" (ISO list) plus one "cloud_cover_<model>"
	    list per model. Empty dict on failure or if fewer than 2 models given.
	"""
	if len(models) < 2:
		return {}
	params: dict[str, Any] = {
		"latitude": lat,
		"longitude": lon,
		"hourly": "cloud_cover",
		"models": ",".join(models),
		"forecast_days": OPEN_METEO_FORECAST_DAYS,
		"timezone": "UTC",
	}
	try:
		r = requests.get(OPEN_METEO_URL, params=params, timeout=HTTP_TIMEOUT)
		r.raise_for_status()
		parsed = r.json()
		hourly = parsed.get("hourly", {})
		if not isinstance(hourly, dict) or "time" not in hourly:
			return {}
		return hourly
	except (requests.RequestException, ValueError):
		# Convergence is a nice-to-have; never let it fail the forecast.
		sys.stderr.write("astrowidget: convergence fetch failed (non-fatal)\n")
		return {}


def fetch_open_meteo_air_quality(lat: float, lon: float) -> dict[str, list[Any]]:
	"""
	Fetches aerosol optical depth (AOD-550nm) from the Open-Meteo Air-Quality API
	for the Phase-1 transparency factor (spec §5: thin haze/smoke is the
	under-forecast killer that ruins faint-signal frames). Separate endpoint,
	free, no key.

	Best-effort, exactly like fetch_open_meteo_convergence: on ANY failure this
	returns {} and the transparency factor is simply omitted downstream — AOD must
	never abort the run (spec §7: the score stays fully functional on the free
	baseline).

	Receives: lat, lon — decimal degrees.
	Returns: the "hourly" dict ({"time": [...], "aerosol_optical_depth": [...]}),
	    or {} on failure.
	"""
	params: dict[str, Any] = {
		"latitude": lat,
		"longitude": lon,
		"hourly": "aerosol_optical_depth",
		"forecast_days": OPEN_METEO_FORECAST_DAYS,
		"timezone": "UTC",
	}
	try:
		r = requests.get(OPEN_METEO_AIR_QUALITY_URL, params=params, timeout=HTTP_TIMEOUT)
		r.raise_for_status()
		parsed = r.json()
		hourly = parsed.get("hourly", {})
		if not isinstance(hourly, dict) or "time" not in hourly:
			return {}
		return hourly
	except (requests.RequestException, ValueError):
		# Transparency is a baseline-optional enhancement; never fail the run.
		sys.stderr.write("astrowidget: air-quality (AOD) fetch failed (non-fatal)\n")
		return {}


def build_air_quality_rows(aq_hourly: dict[str, list[Any]]) -> list[dict[str, Any]]:
	"""
	Transforms the Open-Meteo air-quality "hourly" dict into the row list the Dart
	`AirQuality.fromJson` expects — one {"time", "aerosol_optical_depth"} per hour.

	Receives: aq_hourly — the dict returned by fetch_open_meteo_air_quality
	    ({"time": [...], "aerosol_optical_depth": [...]}), or {} on fetch failure.
	Returns: list of per-hour dicts. EMPTY list when AOD is unavailable, which the
	    Dart side reads as "no transparency data" and OMITS the factor entirely — it
	    is NOT scored as zero (absence ≠ worst transparency; that inversion is the
	    Phase-1 null-polarity rule, mirrored from the 250 hPa handling above).

	Only "time" + "aerosol_optical_depth" are emitted: AirQuality.fromJson defaults
	every other field (pm2_5, us_aqi, …) to 0 and the scorer reads only AOD, so an
	AOD-only row is a complete, valid AirQuality. We pair element-wise and stop at
	the shorter array so a truncated AOD response never fabricates tail hours (the
	same length-mismatch defense merge_hourly uses).
	"""
	if not aq_hourly:
		return []
	times = aq_hourly.get("time")
	aod = aq_hourly.get("aerosol_optical_depth")
	# Defensive: the air-quality API can return a key PRESENT but null (e.g.
	# {"time": [...], "aerosol_optical_depth": null}), or rarely a non-list. A
	# `.get("k", [])` default only fires when the key is ABSENT, so a present-null
	# would yield None and crash len(None) — aborting the whole run and breaking the
	# "AOD must never abort" guarantee (spec §7). Treat anything that is not a
	# non-empty list as "no AOD" and omit transparency downstream.
	if not isinstance(times, list) or not isinstance(aod, list) or not times:
		return []
	n = min(len(times), len(aod))
	rows: list[dict[str, Any]] = []
	for i in range(n):
		v = aod[i]
		# Coerce a non-finite AOD to None: AirQuality.fromJson reads a null
		# aerosol_optical_depth as "station doesn't report it", which the engine
		# treats as no-smoke-data — preferable to a confident NaN-derived reading.
		if isinstance(v, float) and not math.isfinite(v):
			v = None
		rows.append({"time": times[i], "aerosol_optical_depth": v})
	return rows


def fetch_7timer(lat: float, lon: float) -> dict[datetime, tuple[Any, Any]]:
	"""
	Fetches the 7Timer! ASTRO forecast (free, global, no key) for every site,
	and returns seeing/transparency keyed by UTC hour.

	7Timer returns `init` (model-run "YYYYMMDDHH" UTC) and a `dataseries` of
	3-hourly entries with `timepoint` (hours from init), `seeing` (1-8) and
	`transparency` (1-8). Each entry's UTC time = init + timepoint hours. We
	expand each 3-hourly point to cover its 3-hour block (so an hourly lookup
	finds the nearest 7Timer value).

	Best-effort: returns {} on any failure (the site then shows "—" for
	seeing/transparency but still gets a full Open-Meteo verdict).

	Returns: { utc_hour(datetime) -> (seeing_raw|None, transparency_raw|None) }.
	"""
	params = {"lon": lon, "lat": lat, "product": "astro", "output": "json"}
	try:
		r = requests.get(SEVENTIMER_URL, params=params, timeout=HTTP_TIMEOUT)
		r.raise_for_status()
		parsed = r.json()
	except (requests.RequestException, ValueError):
		sys.stderr.write("astrowidget: 7Timer fetch failed (non-fatal)\n")
		return {}
	return build_7timer_by_hour(parsed)


def build_7timer_by_hour(parsed: dict[str, Any]) -> dict[datetime, tuple[Any, Any]]:
	"""
	Transforms a parsed 7Timer ASTRO response into {utc_hour -> (seeing, transparency)}.
	Each 3-hourly point is expanded across its 3-hour block so an hourly lookup
	hits the nearest forecast value. Returns {} if the response is unusable.
	"""
	init_raw = parsed.get("init")
	series = parsed.get("dataseries")
	if not isinstance(init_raw, str) or not isinstance(series, list):
		# Don't return {} silently: a 200 response with a missing/garbled
		# init or dataseries (a real 7Timer hiccup shape) would otherwise be
		# invisible even in the journal. fetch_7timer only logs network/JSON
		# failures, not a successful-but-unusable body.
		sys.stderr.write(
			"astrowidget: 7Timer response missing usable init/dataseries; "
			"seeing/transparency unavailable this run.\n"
		)
		return {}
	# init is "YYYYMMDDHH" in UTC.
	try:
		init = datetime(
			int(init_raw[0:4]), int(init_raw[4:6]), int(init_raw[6:8]),
			int(init_raw[8:10]), tzinfo=timezone.utc,
		)
	except (ValueError, IndexError):
		sys.stderr.write(
			f"astrowidget: 7Timer init not a YYYYMMDDHH date ({init_raw!r}); "
			"seeing/transparency unavailable this run.\n"
		)
		return {}
	out: dict[datetime, tuple[Any, Any]] = {}
	for entry in series:
		if not isinstance(entry, dict):
			continue
		tp = entry.get("timepoint")
		# bool is a subclass of int in Python, so isinstance(True, int) is True.
		# Guard it everywhere a 7Timer field is coerced: a stray JSON true/false
		# would otherwise become 1.0/0.0 and render as a CONFIDENT but wrong
		# label ("Excellent"/"Cloudy") — 7Timer is an unauthenticated free
		# service with loose shape guarantees.
		if not isinstance(tp, (int, float)) or isinstance(tp, bool):
			continue
		base = init + timedelta(hours=int(tp))
		seeing = entry.get("seeing")
		transp = entry.get("transparency")
		seeing = float(seeing) if isinstance(seeing, (int, float)) and not isinstance(seeing, bool) else None
		transp = float(transp) if isinstance(transp, (int, float)) and not isinstance(transp, bool) else None
		# Expand the 3-hourly point across its block (this hour + next 2).
		for dh in range(3):
			out[base + timedelta(hours=dh)] = (seeing, transp)
	return out


# ─────────────────────────────────────────────────────────────────────────────
# Cloud ensemble
# ─────────────────────────────────────────────────────────────────────────────

def ensemble_cloud_by_hour(
	convergence_hourly: dict[str, list[Any]],
) -> tuple[dict[datetime, float], dict[datetime, dict[str, float]]]:
	"""
	Builds the per-hour cloud ENSEMBLE from Open-Meteo's distinct models.

	Scoring models (equal weight): Open-Meteo ECMWF / GFS / ICON — three
	independent physical models, NOT Open-Meteo's best_match blend (which is
	itself a mix of them and would corrupt the convergence spread).

	Receives:
	- convergence_hourly: Open-Meteo per-model response (cloud_cover_<model>).

	Returns:
	- consensus: { utc_hour -> mean cloud % across the available models }
	- per_model: { utc_hour -> {model_label: cloud %} } for the display/spread.
	"""
	# Open-Meteo per-model cloud, keyed by hour.
	om_by_hour: dict[datetime, dict[str, float]] = {}
	# .get(key) or [] (not .get(key, [])): a key present with a JSON null value
	# returns None from .get with a default, which then fails iteration. The live
	# Open-Meteo responses DO send present-but-null arrays.
	times = (convergence_hourly.get("time") or []) if convergence_hourly else []
	for i, t in enumerate(times):
		hour = _parse_utc_hour(t)
		if hour is None:
			continue
		entry: dict[str, float] = {}
		for m in OPEN_METEO_CONVERGENCE_MODELS:
			arr = convergence_hourly.get(f"cloud_cover_{m}") or []
			if i < len(arr) and arr[i] is not None:
				try:
					entry[m.split("_")[0]] = float(arr[i])  # 'gfs'/'ecmwf'/'icon'
				except (TypeError, ValueError):
					pass
		if entry:
			om_by_hour[hour] = entry

	consensus: dict[datetime, float] = {}
	per_model: dict[datetime, dict[str, float]] = {}
	for hour, models in om_by_hour.items():
		per_model[hour] = dict(models)
		if models:
			consensus[hour] = sum(models.values()) / len(models)
	return consensus, per_model


# ─────────────────────────────────────────────────────────────────────────────
# Response merging
# ─────────────────────────────────────────────────────────────────────────────

def merge_hourly(
	open_meteo: dict[str, Any],
	*,
	cloud_by_hour: dict[datetime, float] | None = None,
	st_by_hour: dict[datetime, tuple[Any, Any]] | None = None,
) -> list[dict[str, Any]]:
	"""
	Combines per-hour forecast data into the unified shape the Dart scoring
	binary expects — one row per Open-Meteo hour.

	Open-Meteo is the base weather source for every site (cloud layers, wind,
	dewpoint, precip, visibility); its schema is preserved verbatim so no
	field-name translation happens here. On top of that:

	- cloud_by_hour (optional): the multi-model ENSEMBLE consensus cloud %,
	  keyed by UTC hour. When given it REPLACES Open-Meteo's single cloud_cover
	  for scoring — this is how the Open-Meteo 3-model (ECMWF/GFS/ICON) ensemble
	  drives the verdict. Hours the ensemble didn't cover fall back to
	  Open-Meteo's own cloud_cover so a partial ensemble never blanks the
	  meteogram.
	- st_by_hour (optional): a {hour: (seeing, transparency)} lookup from the
	  free, global 7Timer service. Stored per row under _seeing_raw /
	  _transparency_raw; None (not a fabricated default) for hours 7Timer didn't
	  cover, so the display shows "—" rather than inventing a value.

	Length-mismatch defense: Open-Meteo's `time` array is canonical; if any
	variable array is shorter (truncated/partial response) only the COMPLETE
	hours are emitted — we never fabricate tail data.

	Receives: parsed Open-Meteo JSON (required); optional ensemble consensus and
	    pre-built 7Timer seeing/transparency lookup.
	Returns: list of merged hourly dicts, one per complete Open-Meteo hour.
	"""
	om_hourly = open_meteo.get("hourly", {})
	times = om_hourly.get("time", [])
	if not times:
		return []

	def col(key: str) -> list[Any]:
		return om_hourly.get(key, [])

	# ── Seeing/transparency: a source-agnostic {utc_hour: (seeing, transparency)}
	# lookup supplied by the caller from the free, global 7Timer service. ──
	st_lookup: dict[datetime, tuple[Any, Any]] = st_by_hour or {}

	# Open-Meteo's variable arrays MUST match the time array length. If
	# they don't, the safe behavior is to truncate to the minimum complete
	# length rather than fabricate data.
	required_om_cols = [
		"cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
		"relative_humidity_2m", "temperature_2m", "dewpoint_2m",
		"wind_speed_10m", "wind_gusts_10m",
		"precipitation_probability", "precipitation", "visibility",
	]
	min_complete = len(times)
	for key in required_om_cols:
		arr = col(key)
		if len(arr) < min_complete:
			min_complete = len(arr)
	if min_complete < len(times):
		sys.stderr.write(
			f"astrowidget: WARNING: Open-Meteo returned {len(times)} timestamps "
			f"but at least one variable array is shorter "
			f"({min_complete}). Truncating to {min_complete} complete hours.\n"
		)

	cloud = col("cloud_cover")
	cloud_low = col("cloud_cover_low")
	cloud_mid = col("cloud_cover_mid")
	cloud_high = col("cloud_cover_high")
	humidity = col("relative_humidity_2m")
	temp = col("temperature_2m")
	dewpoint = col("dewpoint_2m")
	wind = col("wind_speed_10m")
	gusts = col("wind_gusts_10m")
	precip_prob = col("precipitation_probability")
	precip = col("precipitation")
	visibility = col("visibility")
	# 250 hPa jet-stream wind — the Phase-1 seeing input (spec §5: upper-level
	# wind is the right ALTITUDE for seeing; surface stability was the wrong
	# proxy). Read None-preserving (NOT _safe with a 0.0 default): a missing hour
	# means "no jet data → skip it in the blend", whereas 0.0 would mean a real
	# dead-calm jet (a perfect-seeing signal). The two must never collapse.
	jet250 = col("wind_speed_250hPa")

	out: list[dict[str, Any]] = []
	for i in range(min_complete):
		om_hour = _parse_utc_hour(times[i])
		# Seeing/transparency for THIS hour from the 7Timer lookup built above.
		# None when no matching hour.
		seeing_v, transparency_v = (
			st_lookup.get(om_hour, (None, None)) if om_hour is not None
			else (None, None)
		)
		# Ensemble consensus overrides Open-Meteo's own cloud_cover for scoring;
		# fall back to the Open-Meteo value for any hour the ensemble missed so a
		# partial ensemble never blanks the meteogram.
		cloud_v = cloud_by_hour.get(om_hour) if (cloud_by_hour and om_hour is not None) else None
		if cloud_v is None:
			cloud_v = _safe(cloud, i, 50.0)

		row: dict[str, Any] = {
			"time": times[i],
			"cloud_cover": cloud_v,
			"cloud_cover_low": _safe(cloud_low, i, 0.0),
			"cloud_cover_mid": _safe(cloud_mid, i, 0.0),
			"cloud_cover_high": _safe(cloud_high, i, 0.0),
			"relative_humidity_2m": _safe(humidity, i, 50.0),
			"temperature_2m": _safe(temp, i, 15.0),
			"dewpoint_2m": _safe(dewpoint, i, 5.0),
			"wind_speed_10m": _safe(wind, i, 0.0),
			"wind_gusts_10m": _safe(gusts, i, 0.0),
			"precipitation_probability": _safe(precip_prob, i, 50.0),
			"precipitation": _safe(precip, i, 0.0),
			"visibility": _safe(visibility, i, 10000.0),
			# 250 hPa jet wind — None when absent (NOT 0.0). The Dart HourlyWeather
			# reads this nullable and the seeing blend SKIPS null hours; a 0.0 here
			# would be scored as an ideal calm jet. See jet250/_safe_optional above.
			"wind_speed_250hPa": _safe_optional(jet250, i),
			# Seeing/transparency from 7Timer. None (not a fabricated default)
			# when no matching hour exists — display shows "—" and the
			# enrichment averaging skips it, rather than inventing a value.
			"_transparency_raw": transparency_v,
			"_seeing_raw": seeing_v,
		}
		out.append(row)
	return out


def _safe(arr: list[Any], i: int, default: Any) -> Any:
	"""Index into a list, falling back to `default` if out of bounds or None.
	Also coerces non-finite floats (NaN, ±inf) to default — Open-Meteo can emit
	a wild value at a model boundary, and the SCORING path reads this raw (only
	the meteogram clamps cloud to [0,100] for display), so a non-finite here
	would poison the cloud/wind sub-scores."""
	if i >= len(arr):
		return default
	v = arr[i]
	if v is None:
		return default
	if isinstance(v, float) and not math.isfinite(v):
		return default
	return v


def _safe_optional(arr: list[Any], i: int) -> Any:
	"""Like _safe but PRESERVES None instead of substituting a default.

	For OPTIONAL columns where absence is semantically distinct from any real
	value — currently wind_speed_250hPa: None means "no jet-stream data for this
	hour" (the Dart seeing blend skips it), whereas 0.0 would mean a genuinely
	dead-calm jet (a perfect-seeing signal). Never collapse the two — that is the
	polarity trap the Phase-1 redesign explicitly guards against.

	Receives: arr — a (possibly short) value list; i — the hour index.
	Returns: the finite numeric value at i, or None if out of bounds / None /
	    non-finite. Unlike _safe there is NO default substitution.
	"""
	if i >= len(arr):
		return None
	v = arr[i]
	if v is None:
		return None
	if isinstance(v, float) and not math.isfinite(v):
		return None
	return v


def _parse_utc_hour(s: Any) -> datetime | None:
	"""
	Parses an ISO-8601 timestamp string to a UTC datetime truncated to the
	hour. Used to align the Open-Meteo and 7Timer hourly series, whose arrays
	start at different times.

	Handles Open-Meteo's no-suffix UTC timestamps ("2026-05-29T04:00") as well
	as "Z"-suffixed or offset forms. Returns None on anything unparseable so
	callers can fall back gracefully.
	"""
	if not isinstance(s, str) or not s:
		return None
	t = s.replace("Z", "+00:00")
	# Append a UTC offset ONLY if the string carries no offset at all. The
	# trailing-offset regex correctly recognizes NEGATIVE offsets (e.g.
	# "...-08:00") that a bare `"+" not in t` check would miss — appending
	# "+00:00" to those produced an unparseable string and silently dropped
	# the hour. Open-Meteo with timezone=UTC omits the offset entirely.
	if "T" in t and not _TZ_OFFSET_RE.search(t):
		t = t + "+00:00"
	try:
		dt = datetime.fromisoformat(t)
	except ValueError:
		return None
	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=timezone.utc)
	dt = dt.astimezone(timezone.utc)
	return dt.replace(minute=0, second=0, microsecond=0)


def _mean(values: list[float]) -> float | None:
	"""Arithmetic mean of a list, or None if empty. Skips non-finite values."""
	finite = [v for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
	if not finite:
		return None
	return sum(finite) / len(finite)


def enrich_night_factors(
	night: dict[str, Any],
	hourly_slice: list[dict[str, Any]],
	convergence_by_hour: dict[datetime, dict[str, float]],
) -> None:
	"""
	Computes the human-readable per-night weather panel ("displayFactors")
	from the dark-window hourly slice and attaches it to `night` in place.

	This is THE fix for the dead-data finding: the seeing + transparency values
	(which the Dart binary drops, because HourlyWeather only knows Open-Meteo
	keys) are read here — in the fetcher, the only place that still has them —
	averaged over the dark window, mapped to their documented labels, and
	surfaced for the QML to render.

	The Dart binary's broadband.factors (cloud/stability/skyBrightness/transparency SCORES)
	remain the scoring breakdown; displayFactors is the separate
	weather-readout the user asked for ("all the astro-specific weather info").

	Receives:
	- night: a per-night dict from the scoring output (mutated in place).
	- hourly_slice: merged hourly rows that fall within this night's dark window.
	- convergence_by_hour: UTC-hour -> {model_id: cloud_pct} from the Open-Meteo
	  cloud ensemble (may be empty if that fetch was skipped or failed).
	"""
	if not hourly_slice:
		night["displayFactors"] = None
		return

	def avg(key: str) -> float | None:
		return _mean([h[key] for h in hourly_slice if h.get(key) is not None])

	seeing_avg = avg("_seeing_raw")
	transparency_avg = avg("_transparency_raw")
	temp_avg = avg("temperature_2m")
	dew_avg = avg("dewpoint_2m")
	dew_spread = (temp_avg - dew_avg) if (temp_avg is not None and dew_avg is not None) else None
	vis_avg = avg("visibility")

	# Seeing/transparency come from 7Timer (the only source now). Its 1-8 scale
	# has 1 = best; the seventimer_* mappers render the shared label vocabulary.
	seeing_lbl, trans_lbl = seventimer_seeing_label, seventimer_transparency_label

	# Per-model cloud convergence over the dark window. For each model, average
	# its cloud_cover across the slice's hours; report the per-model means and
	# the numeric SPREAD (max - min). We deliberately do NOT bucket the spread
	# into strong/moderate/weak labels — those thresholds were arbitrary magic
	# numbers (flagged in the design review). The raw spread in cloud % lets the
	# user judge agreement directly: ~5% is tight concord, ~40% is real
	# disagreement between models.
	convergence = None
	if convergence_by_hour:
		per_model: dict[str, list[float]] = {}
		for h in hourly_slice:
			hour = _parse_utc_hour(h.get("time"))
			models_at_hour = convergence_by_hour.get(hour, {}) if hour else {}
			for model_id, cloud in models_at_hour.items():
				per_model.setdefault(model_id, []).append(cloud)
		model_means = {m: _mean(v) for m, v in per_model.items() if _mean(v) is not None}
		if len(model_means) >= 2:
			vals = list(model_means.values())
			convergence = {
				"models": {m: round(v) for m, v in model_means.items()},
				"spread": round(max(vals) - min(vals)),
			}

	night["displayFactors"] = {
		"seeing": {"raw": _round1(seeing_avg), "label": seeing_lbl(seeing_avg)},
		"transparency": {"raw": _round1(transparency_avg), "label": trans_lbl(transparency_avg)},
		"cloudPct": _round0(avg("cloud_cover")),
		"cloudLow": _round0(avg("cloud_cover_low")),
		"cloudMid": _round0(avg("cloud_cover_mid")),
		"cloudHigh": _round0(avg("cloud_cover_high")),
		"windKmh": _round0(avg("wind_speed_10m")),
		"gustsKmh": _round0(avg("wind_gusts_10m")),
		"dewSpreadC": _round1(dew_spread),
		# Peak overnight precip from the Dart binary (max over the sunset→sunrise
		# exposure window) so the DISPLAY matches the equipment-protection VETO,
		# which also uses the peak. Falls back to the dark-window average for an
		# older binary that doesn't emit precip_peak_pct.
		"precipPct": night.get("precip_peak_pct", _round0(avg("precipitation_probability"))),
		"visibilityKm": _round1(vis_avg / 1000.0) if vis_avg is not None else None,
		"cloudConvergence": convergence,
	}


def build_convergence_index(
	conv_hourly: dict[str, list[Any]], models: list[str]
) -> dict[datetime, dict[str, float]]:
	"""
	Transforms Open-Meteo's per-model convergence response into a UTC-hour
	lookup: {hour -> {model_id: cloud_pct}}. Open-Meteo returns each model's
	cloud cover under `cloud_cover_<model>`. Aligns by the response's own
	`time` array. Returns {} if the convergence fetch was empty.

	NOTE: production now derives the per-night convergence index from
	ensemble_cloud_by_hour's per_model output instead. This helper is retained as
	a tested utility that produces the same {hour: {model: pct}} shape from a raw
	Open-Meteo convergence response — handy for tests and any other consumer.
	"""
	if not conv_hourly:
		return {}
	times = conv_hourly.get("time") or []
	index: dict[datetime, dict[str, float]] = {}
	for i, t in enumerate(times):
		hour = _parse_utc_hour(t)
		if hour is None:
			continue
		entry: dict[str, float] = {}
		for m in models:
			arr = conv_hourly.get(f"cloud_cover_{m}") or []
			if i < len(arr) and arr[i] is not None:
				try:
					entry[m] = float(arr[i])
				except (TypeError, ValueError):
					pass
		if entry:
			index[hour] = entry
	return index


def _round0(v: float | None) -> int | None:
	"""Round to whole number, passing through None."""
	return round(v) if v is not None else None


def _round1(v: float | None) -> float | None:
	"""Round to 1 decimal, passing through None."""
	return round(v, 1) if v is not None else None


# ─────────────────────────────────────────────────────────────────────────────
# Scoring binary invocation
# ─────────────────────────────────────────────────────────────────────────────

def invoke_scoring_binary(payload: dict[str, Any]) -> dict[str, Any]:
	"""
	Spawns the compiled Dart scoring binary as a subprocess, pipes the
	payload as JSON on stdin, reads JSON from stdout. Times out at
	SCORING_TIMEOUT seconds.

	stderr is piped through to the journal (not captured silently) so any
	per-site warnings the Dart binary emits are diagnosable via
	`journalctl --user-unit=astrowidget-fetch.service`.

	Receives: payload dict to be serialized as the binary's stdin JSON.
	Returns: parsed stdout JSON dict.
	Exits: code 4 on subprocess failure or timeout — emits notify-send.
	"""
	if not SCORING_BINARY.exists():
		# Point at the OS-appropriate installer (the canonical rebuild path) plus
		# the manual 'dart build cli' route documented in the README. The previous
		# message hardcoded a Linux `cp` command that was wrong on Windows.
		installer = "windows\\install.ps1" if sys.platform == "win32" else "install.sh"
		sys.stderr.write(
			f"astrowidget: scoring binary not found at {SCORING_BINARY}.\n"
			f"Build it by re-running the installer ({installer}), or manually with "
			f"'dart build cli' from the scoring/ package (see README).\n"
		)
		_notify(
			"Scoring binary missing",
			"Rebuild the Dart binary — see fetcher log for the command.",
			urgency="critical",
		)
		sys.exit(4)

	try:
		# stdout is captured (we parse it). On Linux the fetcher runs under
		# systemd, so inheriting stderr (None) routes the binary's diagnostics to
		# the journal. On Windows the inherited OS stderr handle is invalid under
		# pythonw, but main() has rebound sys.stderr to a real log file — pass
		# that explicit handle so the Dart binary's diagnostics land in the log
		# instead of being discarded.
		score_stderr = sys.stderr if sys.platform == "win32" else None
		if score_stderr is not None:
			# Flush our buffered writes first: the child shares this fd, so any
			# unflushed parent text could otherwise interleave with or clobber the
			# binary's stderr in the log file.
			score_stderr.flush()
		proc = subprocess.run(
			[str(SCORING_BINARY)],
			input=json.dumps(payload).encode("utf-8"),
			stdout=subprocess.PIPE,
			stderr=score_stderr,
			timeout=SCORING_TIMEOUT,
			check=True,
		)
	except subprocess.TimeoutExpired:
		_notify(
			"Scoring binary timed out",
			f"Exceeded {SCORING_TIMEOUT}s timeout. Check the binary for issues.",
			urgency="critical",
		)
		sys.exit(4)
	except subprocess.CalledProcessError as e:
		sys.stderr.write(
			f"astrowidget: scoring binary exited {e.returncode}\n"
		)
		_notify(
			"Scoring binary failed",
			f"Exit code {e.returncode}. Run the fetcher manually to see the "
			f"binary's output.",
			urgency="critical",
		)
		sys.exit(4)

	try:
		return json.loads(proc.stdout.decode("utf-8"))
	except json.JSONDecodeError as e:
		sys.stderr.write(f"astrowidget: scoring binary output is not JSON: {e}\n")
		# Emit a user-visible notification too — previously this exit path
		# was silent (only stderr), which violated the spec §6.1 promise that
		# "every persistent error emits exactly one user-visible notification."
		_notify(
			"Scoring binary output malformed",
			"The Dart binary returned non-JSON. Run the fetcher manually to see "
			"its output.",
			urgency="critical",
		)
		sys.exit(4)


# ─────────────────────────────────────────────────────────────────────────────
# State persistence + notifications
# ─────────────────────────────────────────────────────────────────────────────

def write_state(new_state: dict[str, Any]) -> None:
	"""
	Atomically writes state.json with restricted (0600) permissions.

	The state file contains lat/lon coordinates — privacy-sensitive even
	though no API key is present. Restricting to 0600 prevents other
	local users from reading user location data.

	Atomic semantics: BOTH the new-state write AND the prev-state rotation
	use the .tmp+rename pattern. POSIX rename is atomic; a partial write
	can never be observed by the plasmoid, and the prev-state rotation
	cannot leave a half-copied file (which adversarial review flagged as
	a TOCTOU on `shutil.copy2`).
	"""
	CACHE_DIR.mkdir(parents=True, exist_ok=True)
	if STATE_PATH.exists():
		# Read-then-rename pattern for the previous-state rotation. Reading
		# the existing state.json into memory and writing it out via the
		# tmp+rename pattern guarantees that PREV_STATE_PATH is either
		# fully the old content or fully the new content at any instant,
		# never a half-copied file. The cost is one full read of state.json
		# (negligible — a few KB).
		try:
			old_bytes = STATE_PATH.read_bytes()
		except OSError:
			# state.json disappeared between exists() and read() (race with
			# another fetcher invocation, very unlikely). Skip prev rotation.
			old_bytes = None
		if old_bytes is not None:
			prev_tmp = PREV_STATE_PATH.with_suffix(".json.tmp")
			prev_tmp.write_bytes(old_bytes)
			os.chmod(prev_tmp, 0o600)
			# os.replace, NOT os.rename: it atomically overwrites an existing
			# destination on BOTH POSIX and Windows. os.rename raises
			# FileExistsError on Windows when the target already exists.
			os.replace(prev_tmp, PREV_STATE_PATH)

	tmp = STATE_PATH.with_suffix(".json.tmp")
	tmp.write_text(json.dumps(new_state, indent=2), encoding="utf-8")
	# Restrict perms BEFORE the replace so the atomic visible state is already
	# protected. os.chmod on the tmp file then replace guarantees no window
	# where the file is world-readable.
	os.chmod(tmp, 0o600)
	# os.replace, NOT os.rename: atomic overwrite on BOTH POSIX and Windows.
	# os.rename raises FileExistsError on Windows once state.json exists — i.e.
	# every run after the first, which would freeze the widget's data silently.
	os.replace(tmp, STATE_PATH)


def load_prev_state() -> dict[str, Any] | None:
	"""
	Returns the prior state.prev.json contents, or None if missing or corrupt.

	If state.prev.json is malformed (e.g., disk failure mid-write), we log
	to stderr and remove the corrupt file. The next run will treat the
	current state as a fresh start (no diff-based notifications), which is
	the right behavior — the alternative would be silently suppressing all
	upgrade/downgrade alerts indefinitely.
	"""
	if not PREV_STATE_PATH.exists():
		return None
	try:
		return json.loads(PREV_STATE_PATH.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as e:
		sys.stderr.write(
			f"astrowidget: state.prev.json corrupt or unreadable ({e}); "
			f"removing so the next run starts fresh.\n"
		)
		try:
			PREV_STATE_PATH.unlink()
		except OSError:
			pass
		return None


def emit_notifications(
	prev: dict[str, Any] | None,
	new: dict[str, Any],
	cfg: dict[str, Any],
) -> None:
	"""
	Diffs prior verdicts against new verdicts and fires notify-send per the
	configured rules. See design spec §9.

	Three trigger families:
	  1. Upward transition (Neither→NB, NB→BB+NB) — fires when conditions
	     improve relative to the prior state.
	  2. Downward transition day-of — fires when conditions degrade.
	  3. Astro dark begins with GO verdict — fires when current time is at
	     or past tonight's dark_window.start AND we haven't already fired
	     for this specific dark window's start. Acts as the imaging-run
	     reminder for users prone to forgetting once distracted.

	The astro-dark-begins rule mutates `new["astroDarkNotifiedFor"]` so the
	next run knows which dark windows have been alerted. Persisted to
	state.json transparently.
	"""
	notif_cfg = cfg.get("notifications", {})
	upward = notif_cfg.get("upward_transitions", True)
	downward_day_of = notif_cfg.get("downward_transitions_day_of", True)
	dark_start_reminder = notif_cfg.get("astro_dark_start_reminder", True)

	rank = {"Neither": 0, "NB only": 1, "BB+NB": 2}
	prev_sites = {s["id"]: s for s in (prev or {}).get("sites", [])}

	# Carry forward (or initialize) the per-site "we already notified for
	# this dark window" map. Keys are site ids; values are the ISO timestamp
	# of the dark_window.start we most recently notified for.
	dark_notified = dict((prev or {}).get("astroDarkNotifiedFor", {}))
	now_utc = datetime.now(timezone.utc)

	for site in new.get("sites", []):
		sid = site["id"]
		label = site.get("label", sid)
		if site.get("status") != "ok":
			# Error case — already notified by the fetch step.
			continue
		tonight_nights = [n for n in site.get("nights", []) if n.get("label") == "Tonight"]
		if not tonight_nights:
			continue
		tonight = tonight_nights[0]
		new_rec = tonight.get("recommendation", "Neither")

		prev_site = prev_sites.get(sid)
		prev_tonight = next(
			(n for n in (prev_site or {}).get("nights", []) if n.get("label") == "Tonight"),
			None,
		)
		prev_rec = prev_tonight.get("recommendation") if prev_tonight else None

		new_rank = rank.get(new_rec, 0)
		old_rank = rank.get(prev_rec, 0) if prev_rec is not None else None

		# Transition notifications need a prior state to compare against.
		if prev_rec is not None:
			if upward and new_rank > old_rank:
				_notify(
					f"{label}: {new_rec}",
					f"Conditions improved (was {prev_rec}). Tonight's outlook upgraded.",
				)
			elif downward_day_of and new_rank < old_rank:
				_notify(
					f"{label}: {new_rec}",
					f"Conditions degraded (was {prev_rec}). Reconsider imaging plans.",
					urgency="critical",
				)

		# Astro-dark-begins reminder — fires once per dark window per site.
		# Triggers when:
		#   - feature is enabled
		#   - tonight's verdict is NB only or BB+NB (a GO state)
		#   - the dark window has started (now >= dark_window.start)
		#   - we haven't already notified for this specific dark window
		# Independent of prev_rec so it fires on the first run too —
		# the user wants the imaging reminder even on a fresh install.
		dw = tonight.get("dark_window") or {}
		dw_start_str = dw.get("start")
		if (
			dark_start_reminder
			and new_rank >= 1
			and dw_start_str
			and dark_notified.get(sid) != dw_start_str
		):
			try:
				dw_start = datetime.fromisoformat(dw_start_str.replace("Z", "+00:00"))
			except ValueError:
				dw_start = None
			if dw_start is not None and now_utc >= dw_start:
				_notify(
					f"{label}: astro dark — {new_rec}",
					f"Imaging window is open. Verdict: {new_rec}.",
				)
				dark_notified[sid] = dw_start_str

	new["astroDarkNotifiedFor"] = dark_notified


def _sanitize_notify_text(s: str) -> str:
	"""
	Make a string safe for every notification backend: collapse newlines to
	spaces and drop other control characters (C0 + DEL). A raw newline would let
	a site label inject a second AppleScript statement via osascript on macOS,
	and XML 1.0 rejects C0 controls (even when escaped), which would break the
	Windows toast. Notifications are single-line, so this loses nothing real.
	"""
	s = s.replace("\r", " ").replace("\n", " ")
	return "".join(c for c in s if c >= " " and c != "\x7f")


def _notify(title: str, body: str, urgency: str = "normal") -> None:
	"""
	Sends a best-effort desktop notification, dispatched per OS. A missing or
	failed notifier is logged to stderr but is NEVER fatal — the fetcher
	proceeds. The stderr fallback still records the message so a missing
	notifier can't fully hide a config failure (the systemd journal on Linux /
	the console + Event Log on Windows capture stderr).

	`urgency` is "normal" or "critical"; only Linux's notify-send has a direct
	urgency concept, so it's passed there and ignored on the other platforms.

	Per-OS backends (see the _notify_* helpers below):
	  - Linux  : notify-send (libnotify) — the original mechanism, unchanged.
	  - Windows: a WinRT toast via built-in PowerShell, no third-party module.
	  - macOS  : osascript 'display notification' (keeps run.py's advertised
	             macOS support honest — the desktop app already runs there).
	"""
	# Notifications are single-line UI. Sanitize first so a control character in
	# a site label can't inject AppleScript on macOS or break the toast XML on
	# Windows — one guard covers all three backends at the dispatch boundary.
	title = _sanitize_notify_text(title)
	body = _sanitize_notify_text(body)
	try:
		if sys.platform == "win32":
			_notify_windows(title, body)
		elif sys.platform == "darwin":
			_notify_macos(title, body)
		else:
			_notify_linux(title, body, urgency)
	except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
		# A notifier problem (binary absent, timeout, spawn failure) degrades to a
		# stderr line rather than crashing the fetch — notifications are a
		# convenience, not a correctness requirement. The exception TYPE is logged
		# so a masked logic bug (e.g. an OSError-family error raised while building
		# the Windows toast) is identifiable, not silently indistinguishable from a
		# benign missing notifier.
		sys.stderr.write(
			f"astrowidget: notifier unavailable [{type(e).__name__}] "
			f"[{urgency}] {title}: {body}\n"
		)


def _notify_linux(title: str, body: str, urgency: str) -> None:
	"""Linux desktop notification via libnotify's notify-send (the original)."""
	subprocess.run(
		[
			"notify-send",
			"--app-name=astrowidget",
			"--icon=weather-clear-night",
			f"--urgency={urgency}",
			title,
			body,
		],
		check=False,
		timeout=5,
	)


def _notify_macos(title: str, body: str) -> None:
	"""
	macOS notification via osascript. AppleScript string literals are double-
	quoted, so a literal backslash or double-quote in the text must be escaped —
	backslash FIRST, then the quote, otherwise the quote-escape's own backslash
	gets doubled.
	"""
	t = title.replace("\\", "\\\\").replace('"', '\\"')
	b = body.replace("\\", "\\\\").replace('"', '\\"')
	subprocess.run(
		["osascript", "-e", f'display notification "{b}" with title "{t}"'],
		check=False,
		timeout=5,
	)


def _xml_escape(s: str) -> str:
	"""
	Escape the XML special chars that matter inside element text. `&` MUST be
	replaced first, or the `&` it introduces in `&lt;`/`&gt;` gets re-escaped.
	Quotes need no escaping here because user text only goes in <text> ELEMENTS,
	never in attributes — so & < > suffice.
	"""
	return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _windows_toast_script(title: str, body: str) -> str:
	"""
	Build the PowerShell (WinRT) script that shows exactly one toast. Kept
	separate from the subprocess call so the script-building — the only part of
	the Windows path that can be checked without a Windows host — is unit
	testable on Linux. _notify_windows base64-encodes and runs the result.

	Escaping: XML-escape the user text for the toast's <text> elements, then
	double any single quote so the XML survives the surrounding PowerShell
	single-quoted string literal passed to LoadXml().
	"""
	xml = (
		'<toast><visual><binding template="ToastGeneric">'
		f"<text>{_xml_escape(title)}</text><text>{_xml_escape(body)}</text>"
		"</binding></visual></toast>"
	).replace("'", "''")
	# AppUserModelID = Windows PowerShell's own built-in Start-menu identity,
	# present on every default Win10/11 install. Attributing the toast to an
	# already-registered app means it shows without us pre-creating a shortcut;
	# the source label reads "Windows PowerShell". A custom branded AUMID is
	# deliberately left out for now — a toast that reliably appears beats a
	# branded one that might not. Raw string: the backslashes are literal.
	app_id = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
	# WinRT type projection in Windows PowerShell 5.1 uses the three-part
	# [Type, Assembly, ContentType=WindowsRuntime] form. ::New() constructs;
	# CreateToastNotifier($AppId).Show($toast) displays it.
	return (
		"$ErrorActionPreference='Stop';"
		f"$AppId='{app_id}';"
		"$x=[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]::New();"
		f"$x.LoadXml('{xml}');"
		"$t=[Windows.UI.Notifications.ToastNotification,Windows.UI.Notifications,ContentType=WindowsRuntime]::New($x);"
		"[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]::CreateToastNotifier($AppId).Show($t);"
	)


def _notify_windows(title: str, body: str) -> None:
	"""
	Windows toast via the built-in WinRT ToastNotificationManager, driven by
	Windows PowerShell (powershell.exe / 5.1, present on every Windows 10/11).
	Uses NO third-party module, preserving the project's "Python stdlib +
	requests only" install surface — the toast machinery lives entirely on the
	PowerShell/WinRT side; Python only spawns it.

	Why WinRT and not the alternatives (researched 2026-05-31):
	  - BurntToast is more ergonomic but needs `Install-Module BurntToast`, an
	    extra dependency the user must add — against the minimal-install ethic.
	  - System.Windows.Forms NotifyIcon balloons are flaky: they silently fail
	    to render if the spawning process exits too quickly, and modern Windows
	    ignores their timeout.
	  - WinRT is the native path, present by default, and the most likely to
	    work on a first, untested run.

	IMPORTANT — untestable from this (Linux) machine; needs on-Windows
	verification. The toast only DISPLAYS when the fetcher runs in the logged-in
	user's interactive session. The scheduled task MUST use "Run only when user
	is logged on" — under Session 0 isolation the toast is created but never
	shown. install.ps1 configures the task that way and the Windows docs call it
	out. If a toast still doesn't appear, the first thing to toggle is the `-Sta`
	flag below (WinRT wants a single-threaded apartment; this is the one detail
	the research could not empirically confirm).

	Delivery: the script is passed via -EncodedCommand (base64 of UTF-16LE),
	NOT -Command. That sidesteps PowerShell's notorious command-line quoting
	rules entirely — the script can contain quotes, braces, and XML freely with
	no shell-escaping layer to get wrong. The only escaping that remains is the
	content escaping done in _windows_toast_script().
	"""
	# -EncodedCommand expects base64 of the UTF-16LE (little-endian) script.
	encoded = base64.b64encode(
		_windows_toast_script(title, body).encode("utf-16-le")
	).decode("ascii")
	subprocess.run(
		[
			"powershell",
			"-NoProfile",
			"-NonInteractive",
			"-Sta",
			"-EncodedCommand",
			encoded,
		],
		check=False,
		timeout=10,
		# CREATE_NO_WINDOW suppresses the console flash on Windows; getattr keeps
		# this safe on non-Windows where the flag doesn't exist (this helper is
		# only reached on win32, but the attribute is resolved defensively).
		creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
	)


# ─────────────────────────────────────────────────────────────────────────────
# Hourly slicing for meteogram
# ─────────────────────────────────────────────────────────────────────────────

def slice_hourly_for_night(
	hourly: list[dict[str, Any]],
	dark_window: dict[str, Any] | None,
) -> list[dict[str, Any]]:
	"""
	Filters a merged hourly list down to hours that fall within a given
	astro dark window. Used to embed per-night hourly data into state.json
	so the QML meteogram can render without needing the raw forecast.

	Receives:
	- hourly: full merged hourly list from merge_hourly.
	- dark_window: dict with "start" and "end" ISO timestamps, or None.

	Returns: filtered list of dicts. Empty if dark_window is None or no
	hours fall inside.
	"""
	if not dark_window or not dark_window.get("start") or not dark_window.get("end"):
		return []
	try:
		start = datetime.fromisoformat(dark_window["start"].replace("Z", "+00:00"))
		end = datetime.fromisoformat(dark_window["end"].replace("Z", "+00:00"))
	except (ValueError, TypeError):
		return []
	out: list[dict[str, Any]] = []
	for row in hourly:
		try:
			t_str = row["time"]
			# Open-Meteo timestamps omit timezone — treat as UTC.
			if not t_str.endswith("Z") and "+" not in t_str:
				t_str = t_str + "+00:00"
			t = datetime.fromisoformat(t_str)
		except (KeyError, ValueError):
			continue
		if start <= t <= end:
			out.append(row)
	return out


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
	"""
	Pipeline:
	1. Load config (validates + checks perms; exits 2 on failure).
	2. For each site: Open-Meteo GET + 7Timer GET, merge into hourly
	   list. Per-site failure marks status: error, continues with others.
	3. Build scoring binary input; invoke binary; parse output.
	4. Attach per-night hourly slices to the state for meteogram rendering.
	5. Atomic write of state.json (0600 perms, with state.prev.json rotation).
	6. Diff vs. prev and emit notifications.
	Returns: 0 on success.
	"""
	# Under Windows' pythonw.exe (which the scheduled task uses so it doesn't
	# flash a console window 4x/day), sys.stdout/sys.stderr can be None — the
	# Python docs note this for GUI hosts with no console attached. Several paths
	# below write to sys.stderr (error logging, the notifier fallback), and
	# writing to None would crash the run. No-op on Linux/macOS and on console
	# Python, where the standard streams are never None.
	if sys.stderr is None or sys.stdout is None:
		# Route diagnostics to a log file under the cache dir so the scheduled
		# Windows run still has a durable trail (the systemd-journal equivalent).
		# Truncate per run so the log always reflects the latest run and never
		# grows without bound. A failed open() must not abort the fetch — fall
		# back to the null device.
		try:
			CACHE_DIR.mkdir(parents=True, exist_ok=True)
			_null_log = open(CACHE_DIR / "fetch.log", "w", encoding="utf-8")
		except OSError:
			_null_log = open(os.devnull, "w", encoding="utf-8")
		if sys.stderr is None:
			sys.stderr = _null_log
		if sys.stdout is None:
			sys.stdout = _null_log

	cfg = load_config()
	sites_cfg = cfg["sites"]

	now_utc = datetime.now(timezone.utc)
	site_results: list[dict[str, Any]] = []
	scoring_sites: list[dict[str, Any]] = []
	error_count = 0
	# Keep a per-site hourly reference so we can slice it per-night after
	# the Dart binary returns dark-window timestamps.
	hourly_by_id: dict[str, list[dict[str, Any]]] = {}
	# Per-site cloud convergence index (UTC hour -> {model: cloud_pct}) for the
	# convergence display.
	convergence_by_id: dict[str, dict[datetime, dict[str, float]]] = {}

	for site_cfg in sites_cfg:
		sid = site_cfg["id"]
		label = site_cfg.get("label", sid)
		lat = float(site_cfg["lat"])
		lon = float(site_cfg["lon"])
		# Parsed by load_config (defaults true). .get() keeps main() working for
		# hand-built configs in tests that predate the flag.
		primary = site_cfg.get("primary", True)
		# Phase-1 scoring inputs, both validated/defaulted by load_config:
		#   managed → HOME (false) vs REMOTE (true) scoring mode (spec §4)
		#   bortle  → light-pollution class 1–9, or None (Dart default baseline)
		managed = site_cfg.get("managed", False)
		bortle = site_cfg.get("bortle")  # None when not configured
		# Defensive: thresholds section may be missing or non-dict.
		thresholds_section = cfg.get("thresholds")
		if not isinstance(thresholds_section, dict):
			thresholds_section = {}
		thresholds = thresholds_section.get(sid, {})

		try:
			# Open-Meteo is the base weather source for EVERY site (wind, dew,
			# precip, visibility, cloud layers). Plus the free multi-model cloud
			# convergence used by BOTH the scoring ensemble and the display.
			om = fetch_open_meteo(lat, lon)
			conv_hourly = fetch_open_meteo_convergence(
				lat, lon, OPEN_METEO_CONVERGENCE_MODELS
			)
			# AOD-550nm for the Phase-1 transparency factor (spec §5). Best-effort:
			# {} on failure → build_air_quality_rows returns [] → the Dart wrapper
			# OMITS the transparency factor (never scored as zero). Free endpoint.
			air_quality_rows = build_air_quality_rows(
				fetch_open_meteo_air_quality(lat, lon)
			)

			# The cloud ensemble is Open-Meteo's three models; seeing and
			# transparency come from the free, global 7Timer service (best-effort:
			# if 7Timer is down they show "—" and the site still scores on the
			# Open-Meteo cloud ensemble).
			consensus, per_model = ensemble_cloud_by_hour(conv_hourly)
			# fetch_7timer returns the {utc_hour: (seeing, transparency)} lookup
			# (it calls build_7timer_by_hour internally) — pass it straight to
			# merge, don't double-build it.
			seventimer = fetch_7timer(lat, lon)
			hourly = merge_hourly(
				om,
				cloud_by_hour=consensus,
				st_by_hour=seventimer,
			)
			meta = {"source": "7timer+openmeteo"}
			# Flag a 7Timer outage so the widget shows a "7Timer unavailable" badge
			# instead of a silent "—" for seeing/transparency. fetch_7timer returns
			# {} on any failure; the site still scores on Open-Meteo cloud, only its
			# astro-quality readout is missing. degraded is a list so more sources
			# can be flagged later without a schema change.
			if not seventimer:
				meta["degraded"] = ["7timer"]

			hourly_by_id[sid] = hourly
			# per_model is the convergence-display source — the Open-Meteo models
			# in the {hour: {model: pct}} shape enrich() expects.
			convergence_by_id[sid] = per_model

			scoring_sites.append({
				"id": sid,
				"label": label,
				"lat": lat,
				"lon": lon,
				"thresholds": thresholds,
				"hourly": hourly,
				# Phase-1 additions (spec §4/§5). bortle may be None (Dart falls back
				# to the default baseline); managed selects HOME vs REMOTE mode;
				# airQuality is the per-hour AOD list ([] when unavailable → the
				# transparency factor is omitted, not zeroed).
				"bortle": bortle,
				"managed": managed,
				"airQuality": air_quality_rows,
			})
			site_results.append({
				"id": sid,
				"label": label,
				"lat": lat,
				"lon": lon,
				"primary": primary,
				"status": "ok",
				"meta": meta,
			})
		except (requests.RequestException, RuntimeError) as e:
			error_count += 1
			sys.stderr.write(f"astrowidget: {sid}: API failure: {e}\n")
			site_results.append({
				"id": sid,
				"label": label,
				"lat": lat,
				"lon": lon,
				"primary": primary,
				"status": "error",
				"error": str(e),
			})
			_notify(
				f"{label}: forecast fetch failed",
				str(e),
				urgency="critical",
			)

	# If every site failed, this is a more severe condition — return 3.
	if error_count == len(sites_cfg):
		return 3

	# Invoke scoring binary on sites that succeeded.
	if scoring_sites:
		scoring_input = {
			"now_utc": now_utc.isoformat().replace("+00:00", "Z"),
			"sites": scoring_sites,
		}
		scoring_output = invoke_scoring_binary(scoring_input)

		# Merge scoring output back into site_results by id, and attach the
		# per-night hourly slice the QML meteogram needs to render.
		scored_by_id = {s["id"]: s for s in scoring_output.get("sites", [])}
		for sr in site_results:
			scored = scored_by_id.get(sr["id"])
			if not scored:
				continue
			nights = scored.get("nights", [])
			site_hourly = hourly_by_id.get(sr["id"], [])
			site_convergence = convergence_by_id.get(sr["id"], {})
			for night in nights:
				night_slice = slice_hourly_for_night(
					site_hourly, night.get("dark_window")
				)
				night["hourly"] = night_slice
				# Surface the seeing/transparency + full weather readout per
				# night, using the 7Timer label scale. This is where the
				# previously-dropped astro data finally reaches the user.
				# (2026-05-28 review fix.)
				enrich_night_factors(night, night_slice, site_convergence)
			sr["nights"] = nights

	# Compose final state.json. schemaVersion bumped 1 -> 2 (2026-05-28):
	# nights now carry displayFactors (seeing/transparency/wind/dew/precip/
	# visibility/cloud-convergence). The plasmoid's StateModel knows v2.
	state = {
		"schemaVersion": 2,
		"lastUpdated": now_utc.isoformat().replace("+00:00", "Z"),
		"sites": site_results,
	}

	prev = load_prev_state()
	write_state(state)
	emit_notifications(prev, state, cfg)

	return 0


if __name__ == "__main__":
	sys.exit(main())
