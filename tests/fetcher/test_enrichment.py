"""
Tests for enrich_night_factors — the function that surfaces the previously-
dropped 7Timer seeing/transparency (plus the full weather readout) into each
night's displayFactors block.

Includes the GOLDEN ACCEPTANCE TEST: the end-to-end "spec-to-screen" check
that would have caught the original dead-data bug. It asserts that the astro
data is not merely fetched but actually reaches the state a QML row binds to.

Seeing/transparency use 7Timer's 1-8 scale (1 = best) via the
seventimer_seeing_label / seventimer_transparency_label mappers:
    1 Excellent | 2 Above Average | 3-4 Average | 5-6 Below Average |
    7 Poor | 8 Cloudy.
"""

import astrowidget_fetch as fx


def _slice(seeing=4, transparency=3, cloud=10, wind=8, temp=12.0, dew=8.0,
		   precip=5, vis=24000, n=4):
	"""Builds a dark-window hourly slice with the given (constant) values."""
	return [
		{
			"time": f"2026-05-29T0{4 + i}:00:00",
			"cloud_cover": cloud,
			"cloud_cover_low": 0,
			"cloud_cover_mid": 0,
			"cloud_cover_high": cloud,
			"temperature_2m": temp,
			"dewpoint_2m": dew,
			"wind_speed_10m": wind,
			"wind_gusts_10m": wind + 5,
			"precipitation_probability": precip,
			"visibility": vis,
			"_seeing_raw": seeing,
			"_transparency_raw": transparency,
		}
		for i in range(n)
	]


# ── THE GOLDEN ACCEPTANCE TEST ────────────────────────────────────────────────

def test_golden_astro_data_reaches_displayfactors():
	"""
	Given 7Timer seeing=2 / transparency=1 in the dark-window slice, the
	enriched night MUST expose those as labeled displayFactors a QML row binds
	to. This is the test whose absence let the dead-data bug ship: it pins the
	requirement to the user-visible outcome, not to mid-pipeline plumbing.
	"""
	night = {"dark_window": {"start": "2026-05-29T04:00:00Z", "end": "2026-05-29T08:00:00Z"}}
	fx.enrich_night_factors(night, _slice(seeing=2, transparency=1), {})
	df = night["displayFactors"]
	assert df is not None, "displayFactors must be populated"
	# 7Timer (1=best): seeing 2 -> 'Above Average'; transparency 1 -> 'Excellent'.
	assert df["seeing"]["label"] == "Above Average"
	assert df["seeing"]["raw"] == 2
	assert df["transparency"]["label"] == "Excellent"
	assert df["transparency"]["raw"] == 1


def test_enrich_computes_weather_rows():
	"""All the weather rows the user asked for are present and averaged."""
	night = {"dark_window": {"start": "2026-05-29T04:00:00Z", "end": "2026-05-29T08:00:00Z"}}
	fx.enrich_night_factors(night, _slice(cloud=20, wind=12, temp=10.0, dew=6.0,
										   precip=15, vis=20000), {})
	df = night["displayFactors"]
	assert df["cloudPct"] == 20
	assert df["windKmh"] == 12
	assert df["gustsKmh"] == 17
	assert df["dewSpreadC"] == 4.0          # 10 - 6
	assert df["precipPct"] == 15
	assert df["visibilityKm"] == 20.0       # 20000 m -> 20 km


def test_enrich_empty_slice_yields_none():
	"""No dark-window hours (polar summer, etc.) → displayFactors None, no crash."""
	night = {"dark_window": None}
	fx.enrich_night_factors(night, [], {})
	assert night["displayFactors"] is None


def test_enrich_skips_none_astro_values():
	"""
	When some hours have None seeing (no matching 7Timer hour), the average is
	over the present values only — not contaminated by None.
	"""
	slc = _slice(seeing=2, n=2)
	slc.append({**slc[0], "time": "2026-05-29T06:00:00", "_seeing_raw": None})
	night = {"dark_window": {"start": "2026-05-29T04:00:00Z", "end": "2026-05-29T08:00:00Z"}}
	fx.enrich_night_factors(night, slc, {})
	# Mean of [2, 2] (the None is skipped) = 2.
	assert night["displayFactors"]["seeing"]["raw"] == 2


def test_enrich_all_none_astro_is_dash():
	"""Every hour missing seeing → label '—', raw None (honest no-data)."""
	slc = _slice(n=2)
	for h in slc:
		h["_seeing_raw"] = None
		h["_transparency_raw"] = None
	night = {"dark_window": {"start": "2026-05-29T04:00:00Z", "end": "2026-05-29T08:00:00Z"}}
	fx.enrich_night_factors(night, slc, {})
	assert night["displayFactors"]["seeing"]["label"] == "—"
	assert night["displayFactors"]["seeing"]["raw"] is None


def test_enrich_uses_seventimer_label_scale():
	"""enrich maps seeing/transparency on 7Timer's INVERTED 1-8 scale (1 = best).
	A LOW raw seeing must read as GOOD — a regression to a higher-is-better scale
	would render the sky exactly backwards."""
	night = {"dark_window": {"start": "2026-05-29T04:00:00Z", "end": "2026-05-29T08:00:00Z"}}
	# 7Timer seeing 2 -> "Above Average"; seeing 7 -> "Poor".
	fx.enrich_night_factors(night, _slice(seeing=2), {})
	assert night["displayFactors"]["seeing"]["label"] == "Above Average"
	night2 = {"dark_window": {"start": "2026-05-29T04:00:00Z", "end": "2026-05-29T08:00:00Z"}}
	fx.enrich_night_factors(night2, _slice(seeing=7), {})
	assert night2["displayFactors"]["seeing"]["label"] == "Poor"


def test_enrich_precip_uses_dart_peak_when_present():
	"""displayFactors.precipPct comes from the Dart binary's precip_peak_pct
	(peak over the sunset→sunrise exposure window) when present, so the display
	matches the equipment-protection veto — NOT the dark-window slice average."""
	night = {"dark_window": {"start": "2026-05-29T04:00:00Z", "end": "2026-05-29T08:00:00Z"},
			 "precip_peak_pct": 40}
	# Slice average precip is 5, but the Dart peak (40) must win.
	fx.enrich_night_factors(night, _slice(precip=5), {})
	assert night["displayFactors"]["precipPct"] == 40


def test_enrich_precip_falls_back_to_average_without_dart_peak():
	"""An older binary with no precip_peak_pct → fall back to the dark-window
	average so the precip readout is never blank."""
	night = {"dark_window": {"start": "2026-05-29T04:00:00Z", "end": "2026-05-29T08:00:00Z"}}
	fx.enrich_night_factors(night, _slice(precip=12), {})
	assert night["displayFactors"]["precipPct"] == 12
