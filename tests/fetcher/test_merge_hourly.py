"""
Tests for merge_hourly — the per-hour forecast merger.

Open-Meteo is the base weather source for every site. On top of it, the merger
takes an optional ensemble cloud override (which replaces Open-Meteo's
cloud_cover for scoring) and an optional pre-built 7Timer seeing/transparency
lookup keyed by UTC hour. Output is the canonical hourly array the Dart scoring
binary expects.

Signature: merge_hourly(open_meteo, *, cloud_by_hour=None, st_by_hour=None) —
open_meteo is the only positional arg; the two overrides are keyword-only.
"""

import astrowidget_fetch as fx


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _open_meteo_response(hours: int = 3) -> dict:
	"""Builds a minimal but well-formed Open-Meteo /forecast response."""
	times = [f"2026-05-29T0{i}:00:00" for i in range(hours)]
	def fill(default):
		return [default] * hours
	return {
		"hourly": {
			"time": times,
			"cloud_cover": fill(15.0),
			"cloud_cover_low": fill(0.0),
			"cloud_cover_mid": fill(5.0),
			"cloud_cover_high": fill(10.0),
			"relative_humidity_2m": fill(65.0),
			"temperature_2m": fill(11.0),
			"dewpoint_2m": fill(8.0),
			"wind_speed_10m": fill(8.0),
			"wind_gusts_10m": fill(13.0),
			"precipitation_probability": fill(5.0),
			"precipitation": fill(0.0),
			"visibility": fill(24000.0),
		},
	}


# ─────────────────────────────────────────────────────────────────────────────
# Base Open-Meteo merge
# ─────────────────────────────────────────────────────────────────────────────


def test_merge_produces_one_row_per_hour():
	"""For 3 hours of input, output has 3 rows."""
	merged = fx.merge_hourly(_open_meteo_response(3))
	assert len(merged) == 3
	assert all("time" in row for row in merged)


def test_merge_preserves_open_meteo_field_names():
	"""Open-Meteo's snake_case keys must roundtrip unchanged into Dart."""
	merged = fx.merge_hourly(_open_meteo_response(2))
	row = merged[0]
	# These are the exact keys astroplan's HourlyWeather.fromJson() reads.
	for key in (
		"cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
		"relative_humidity_2m", "temperature_2m", "dewpoint_2m",
		"wind_speed_10m", "wind_gusts_10m",
		"precipitation_probability", "precipitation", "visibility",
	):
		assert key in row, f"missing key {key}"


def test_merge_empty_open_meteo_yields_empty_list():
	"""No timestamps from Open-Meteo means no rows produced."""
	om = {"hourly": {"time": []}}
	merged = fx.merge_hourly(om)
	assert merged == []


def test_merge_truncates_when_required_field_missing():
	"""
	Missing entire variable array in Open-Meteo truncates the output to
	prevent fabricated data flowing into scoring. Spec §13 explicit rule.
	"""
	om = _open_meteo_response(2)
	# Strip wind_gusts_10m to simulate API omitting it entirely.
	del om["hourly"]["wind_gusts_10m"]
	merged = fx.merge_hourly(om)
	# Truncated to zero complete hours — no fabricated wind values reach scoring.
	assert merged == []


def test_merge_handles_none_values_uses_distinct_sentinel():
	"""Null Open-Meteo entries become the default; non-null real values pass through.
	Uses 73.0 (not the 50.0 default) so the test actually distinguishes
	'fell back to default' from 'kept the original'."""
	om = _open_meteo_response(2)
	om["hourly"]["cloud_cover"] = [None, 73.0]
	merged = fx.merge_hourly(om)
	assert merged[0]["cloud_cover"] == 50.0  # default when None
	assert merged[1]["cloud_cover"] == 73.0  # passes through unchanged


def test_merge_rejects_nonfinite_cloud():
	"""A non-finite Open-Meteo value (inf/NaN) falls back to the default rather
	than poisoning the scoring cloud. The meteogram clamps for display, but the
	scoring path reads cloud_cover raw, so the merge must sanitize it here."""
	om = _open_meteo_response(2)
	om["hourly"]["cloud_cover"] = [float("inf"), 30.0]
	merged = fx.merge_hourly(om)
	assert merged[0]["cloud_cover"] == 50.0   # inf → default
	assert merged[1]["cloud_cover"] == 30.0   # finite value passes through


def test_merge_no_seeing_source_is_none():
	"""With no st_by_hour lookup at all, seeing/transparency are None (shown as
	"—"), never a fabricated good-seeing default."""
	merged = fx.merge_hourly(_open_meteo_response(2))
	assert merged[0]["_seeing_raw"] is None
	assert merged[0]["_transparency_raw"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Ensemble cloud override + 7Timer seeing/transparency
# ─────────────────────────────────────────────────────────────────────────────


def test_merge_cloud_by_hour_overrides_open_meteo_cloud():
	"""The ensemble consensus REPLACES Open-Meteo's cloud_cover for the hours it
	covers; hours the ensemble missed fall back to Open-Meteo so a partial
	ensemble never blanks the meteogram."""
	om = _open_meteo_response(2)  # cloud_cover = 15.0 each hour
	# Consensus covers only hour 0 (00:00 UTC) with 60%.
	consensus = {fx._parse_utc_hour("2026-05-29T00:00:00"): 60.0}
	merged = fx.merge_hourly(om, cloud_by_hour=consensus)
	assert merged[0]["cloud_cover"] == 60.0   # ensemble override
	assert merged[1]["cloud_cover"] == 15.0   # fell back to Open-Meteo


def test_merge_seventimer_seeing_transparency_by_hour():
	"""Seeing/transparency come from the passed-in 7Timer st_by_hour lookup,
	keyed and aligned by UTC hour."""
	om = _open_meteo_response(2)
	st = {
		fx._parse_utc_hour("2026-05-29T00:00:00"): (2, 3),  # (seeing, transparency)
		fx._parse_utc_hour("2026-05-29T01:00:00"): (4, 5),
	}
	merged = fx.merge_hourly(om, st_by_hour=st)
	assert merged[0]["_seeing_raw"] == 2
	assert merged[0]["_transparency_raw"] == 3
	assert merged[1]["_seeing_raw"] == 4
	# No consensus override → cloud_cover stays Open-Meteo's.
	assert merged[0]["cloud_cover"] == 15.0


def test_merge_missing_7timer_hour_is_none_not_default():
	"""When no 7Timer hour matches an Open-Meteo hour, the field is None (honest
	"no data"), NOT a fabricated good-seeing default. Fabricating astro defaults
	is anti-coverage (adversarial-review finding, carried over from the
	Astrospheric merge)."""
	om = _open_meteo_response(3)
	# 7Timer covers only the first hour.
	st = {fx._parse_utc_hour("2026-05-29T00:00:00"): (4, 3)}
	merged = fx.merge_hourly(om, st_by_hour=st)
	assert merged[0]["_seeing_raw"] == 4     # matched
	assert merged[2]["_seeing_raw"] is None  # no matching 7Timer hour


def test_merge_empty_7timer_lookup_is_none():
	"""An empty st_by_hour (e.g. a 7Timer fetch that failed and returned {}) →
	seeing/transparency are None, shown as "—"."""
	merged = fx.merge_hourly(_open_meteo_response(2), st_by_hour={})
	assert merged[0]["_seeing_raw"] is None
	assert merged[0]["_transparency_raw"] is None
