"""
Tests for the cloud-ensemble + 7Timer layer:

  - ensemble_cloud_by_hour: the Open-Meteo (ECMWF/GFS/ICON) consensus cloud,
    plus the per-model display dict.
  - build_7timer_by_hour + the 7Timer label mappers: seeing/transparency on the
    1-8 scale (1 = best).
  - main() per-site behavior: every site uses Open-Meteo + 7Timer; the 7Timer
    seeing reaches displayFactors and per-site state never cross-wires.
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import astrowidget_fetch as fx


# ─────────────────────────────────────────────────────────────────────────────
# ensemble_cloud_by_hour
#
# Signature: ensemble_cloud_by_hour(convergence_hourly) — takes only the
# Open-Meteo per-model convergence response (cloud_cover_<model> arrays).
# ─────────────────────────────────────────────────────────────────────────────


def test_ensemble_is_mean_of_three_open_meteo_models():
	"""The scoring consensus is the equal-weight mean of the three Open-Meteo
	models (GFS/ECMWF/ICON), and per_model carries each one for the display."""
	conv = {
		"time": ["2026-05-29T00:00:00", "2026-05-29T01:00:00"],
		"cloud_cover_gfs_seamless": [10, 20],
		"cloud_cover_ecmwf_ifs04": [30, 40],
		"cloud_cover_icon_seamless": [20, 30],
	}
	consensus, per_model = fx.ensemble_cloud_by_hour(conv)
	h0 = fx._parse_utc_hour("2026-05-29T00:00:00")
	assert consensus[h0] == 20.0   # (10 + 30 + 20) / 3
	assert set(per_model[h0].keys()) == {"gfs", "ecmwf", "icon"}
	assert per_model[h0]["gfs"] == 10
	assert per_model[h0]["ecmwf"] == 30
	assert per_model[h0]["icon"] == 20


def test_ensemble_empty_input_yields_empty():
	"""No convergence → empty consensus (caller falls back to Open-Meteo's own
	cloud_cover in merge_hourly)."""
	consensus, per_model = fx.ensemble_cloud_by_hour({})
	assert consensus == {}
	assert per_model == {}


def test_ensemble_tolerates_present_but_null_convergence_array():
	"""null-vs-missing trap: a model column present with a null value (a model out
	of coverage for these coords) must not crash the consensus build. `.get(key)
	or []` coerces the present-but-null array to empty before iterating."""
	conv = {
		"time": ["2026-05-29T00:00:00"],
		"cloud_cover_gfs_seamless": [20],
		"cloud_cover_ecmwf_ifs04": None,   # present but null
	}
	consensus, per_model = fx.ensemble_cloud_by_hour(conv)
	h0 = fx._parse_utc_hour("2026-05-29T00:00:00")
	assert consensus[h0] == 20.0           # GFS alone, no crash
	assert "ecmwf" not in per_model[h0]


# ─────────────────────────────────────────────────────────────────────────────
# build_7timer_by_hour + label mappers
# ─────────────────────────────────────────────────────────────────────────────


def test_build_7timer_expands_3hourly_to_hourly():
	"""Each 3-hourly 7Timer point is expanded across its 3-hour block and keyed
	by UTC hour (init 'YYYYMMDDHH' + timepoint)."""
	parsed = {
		"init": "2026052900",   # 2026-05-29T00:00 UTC
		"dataseries": [
			{"timepoint": 3, "seeing": 2, "transparency": 1},
			{"timepoint": 6, "seeing": 5, "transparency": 4},
		],
	}
	out = fx.build_7timer_by_hour(parsed)
	# timepoint 3 -> 03:00 UTC, expanded across 03/04/05.
	for h in (3, 4, 5):
		assert out[datetime(2026, 5, 29, h, tzinfo=timezone.utc)] == (2.0, 1.0)
	# timepoint 6 -> 06:00 UTC.
	assert out[datetime(2026, 5, 29, 6, tzinfo=timezone.utc)] == (5.0, 4.0)


def test_build_7timer_bad_input_returns_empty():
	"""Unusable responses degrade to {} (caller shows '—', site still scores)."""
	assert fx.build_7timer_by_hour({}) == {}
	assert fx.build_7timer_by_hour({"init": "not-a-date", "dataseries": []}) == {}
	assert fx.build_7timer_by_hour({"init": "2026052900", "dataseries": "nope"}) == {}


def test_seventimer_labels_invert_scale():
	"""7Timer uses 1 = best, 8 = worst. Getting this backwards would render the
	sky inverted."""
	assert fx.seventimer_seeing_label(1) == "Excellent"
	assert fx.seventimer_seeing_label(8) == "Cloudy"
	assert fx.seventimer_transparency_label(1) == "Excellent"
	assert fx.seventimer_transparency_label(8) == "Cloudy"
	# None -> dash; out-of-range clamps to [1, 8].
	assert fx.seventimer_seeing_label(None) == "—"
	assert fx.seventimer_seeing_label(0) == "Excellent"   # clamps up to 1
	assert fx.seventimer_seeing_label(99) == "Cloudy"     # clamps down to 8


def test_build_7timer_rejects_bool_seeing():
	"""bool is an int subclass: a stray JSON true/false in a 7Timer numeric field
	must become None ('—'), NOT coerce to 1.0/0.0 and render a confident-but-
	wrong 'Excellent'/'Cloudy' label."""
	parsed = {
		"init": "2026052900",
		"dataseries": [{"timepoint": 0, "seeing": True, "transparency": 3}],
	}
	out = fx.build_7timer_by_hour(parsed)
	h0 = datetime(2026, 5, 29, 0, tzinfo=timezone.utc)
	assert out[h0] == (None, 3.0)   # bool seeing → None; real transparency kept


# ─────────────────────────────────────────────────────────────────────────────
# main() per-site behavior
# ─────────────────────────────────────────────────────────────────────────────


def _om_one_hour():
	"""Minimal Open-Meteo response with the single 04:00 UTC hour (inside the
	scoring stub's dark window)."""
	return {
		"hourly": {
			"time": ["2026-05-29T04:00:00"],
			"cloud_cover": [10], "cloud_cover_low": [0], "cloud_cover_mid": [0],
			"cloud_cover_high": [10], "relative_humidity_2m": [60],
			"temperature_2m": [12], "dewpoint_2m": [7], "wind_speed_10m": [5],
			"wind_gusts_10m": [8], "precipitation_probability": [5],
			"precipitation": [0], "visibility": [24000],
		}
	}


def _scoring_stub(sid):
	"""Minimal scoring output: one Tonight with a dark window over 04:00-10:00."""
	return {
		"schema_version": 1,
		"sites": [{
			"id": sid, "label": sid, "status": "ok",
			"nights": [{
				"label": "Tonight",
				"dark_window": {"start": "2026-05-29T04:00:00Z",
								"end": "2026-05-29T10:00:00Z", "duration_minutes": 360},
				"recommendation": "BB+NB",
				"broadband": {"score": 80, "verdict": "excellent", "vetoes": []},
				"narrowband": {"score": 85, "verdict": "excellent", "vetoes": []},
			}],
		}],
	}


def test_main_site_uses_7timer_and_openmeteo(tmp_path):
	"""A site tags meta.source as the 7Timer+Open-Meteo stack, and its 7Timer
	seeing reaches displayFactors with the 1-8 (1=best) label scale. The seeing
	assertion also guards the fetch_7timer double-build regression (a re-wrapped
	lookup would come back empty → '—')."""
	cfg = {
		"open_meteo": {"models": []},
		"sites": [{
			"id": "chile", "label": "Deep Sky Chile", "lat": -33.0, "lon": -70.0,
			"timezone": "America/Santiago", "primary": True,
		}],
		"thresholds": {},
		"notifications": {"upward_transitions": False,
			"downward_transitions_day_of": False, "astro_dark_start_reminder": False},
	}
	seventimer = {fx._parse_utc_hour("2026-05-29T04:00:00"): (2, 1)}  # (seeing, transparency)

	with patch.object(fx, "load_config", return_value=cfg), \
		 patch.object(fx, "fetch_open_meteo", return_value=_om_one_hour()), \
		 patch.object(fx, "fetch_open_meteo_convergence", lambda *a, **k: {}), \
		 patch.object(fx, "fetch_7timer", return_value=seventimer), \
		 patch.object(fx, "invoke_scoring_binary", return_value=_scoring_stub("chile")), \
		 patch.object(fx, "CACHE_DIR", tmp_path), \
		 patch.object(fx, "STATE_PATH", tmp_path / "state.json"), \
		 patch.object(fx, "PREV_STATE_PATH", tmp_path / "state.prev.json"), \
		 patch.object(fx, "_notify"):
		rc = fx.main()

	assert rc == 0
	state = json.loads((tmp_path / "state.json").read_text())
	site = state["sites"][0]
	assert site["status"] == "ok"
	assert site["meta"]["source"] == "7timer+openmeteo"
	# 7Timer seeing=2 on the 1-8 (1=best) scale -> "Above Average".
	df = site["nights"][0]["displayFactors"]
	assert df["seeing"]["label"] == "Above Average"
	assert df["transparency"]["label"] == "Excellent"   # 7Timer transparency 1 = best


def test_main_two_sites_do_not_cross_wire(tmp_path):
	"""Two sites in the SAME main() run must keep their per-site seeing/transparency
	independent. Each gets a DIFFERENT 7Timer seeing value; the merged state must
	preserve the distinction (guards against cross-wiring the per-site hourly /
	convergence dicts)."""
	cfg = {
		"open_meteo": {"models": []},
		"sites": [
			{"id": "north", "label": "North", "lat": 45.0, "lon": -120.0,
			 "timezone": "UTC"},
			{"id": "south", "label": "South", "lat": -33.0, "lon": -70.0,
			 "timezone": "UTC"},
		],
		"thresholds": {},
		"notifications": {"upward_transitions": False,
			"downward_transitions_day_of": False, "astro_dark_start_reminder": False},
	}
	hour = fx._parse_utc_hour("2026-05-29T04:00:00")
	# Different seeing per site: north=2 ("Above Average"), south=6 ("Below Average").
	per_site_7timer = {
		(45.0, -120.0): {hour: (2, 1)},
		(-33.0, -70.0): {hour: (6, 7)},
	}
	def seventimer_for(lat, lon):
		return per_site_7timer[(lat, lon)]
	scoring = {"schema_version": 1, "sites": [
		_scoring_stub("north")["sites"][0], _scoring_stub("south")["sites"][0]]}

	with patch.object(fx, "load_config", return_value=cfg), \
		 patch.object(fx, "fetch_open_meteo", return_value=_om_one_hour()), \
		 patch.object(fx, "fetch_open_meteo_convergence", lambda *a, **k: {}), \
		 patch.object(fx, "fetch_7timer", side_effect=seventimer_for), \
		 patch.object(fx, "invoke_scoring_binary", return_value=scoring), \
		 patch.object(fx, "CACHE_DIR", tmp_path), \
		 patch.object(fx, "STATE_PATH", tmp_path / "state.json"), \
		 patch.object(fx, "PREV_STATE_PATH", tmp_path / "state.prev.json"), \
		 patch.object(fx, "_notify"):
		rc = fx.main()

	assert rc == 0
	state = json.loads((tmp_path / "state.json").read_text())
	by_id = {s["id"]: s for s in state["sites"]}
	assert by_id["north"]["meta"]["source"] == "7timer+openmeteo"
	assert by_id["south"]["meta"]["source"] == "7timer+openmeteo"
	# Distinct seeing values preserved per site (no cross-wiring).
	assert by_id["north"]["nights"][0]["displayFactors"]["seeing"]["label"] == "Above Average"
	assert by_id["south"]["nights"][0]["displayFactors"]["seeing"]["label"] == "Below Average"


@pytest.fixture
def intl_cfg():
	"""Single-site config (Deep Sky Chile). A fixture because it's shared test
	CONTEXT two tests run inside — distinct from the pure value builders above
	(_om_one_hour / _scoring_stub), which stay plain functions. Function-scoped,
	so each test gets a fresh dict (no leak)."""
	return {
		"open_meteo": {"models": []},
		"sites": [{"id": "chile", "label": "Deep Sky Chile", "lat": -33.0, "lon": -70.0,
				   "timezone": "America/Santiago", "primary": True}],
		"thresholds": {},
		"notifications": {"upward_transitions": False,
			"downward_transitions_day_of": False, "astro_dark_start_reminder": False},
	}


def test_main_7timer_down_sets_degraded(intl_cfg, tmp_path):
	"""When fetch_7timer returns {} (its failure mode), the site's meta carries
	degraded=['7timer'] so the widget shows the "7Timer unavailable" badge
	instead of a silent "—". The site still scores on Open-Meteo cloud, so its
	status stays ok — the degradation is partial, not a site failure."""
	with patch.object(fx, "load_config", return_value=intl_cfg), \
		 patch.object(fx, "fetch_open_meteo", return_value=_om_one_hour()), \
		 patch.object(fx, "fetch_open_meteo_convergence", lambda *a, **k: {}), \
		 patch.object(fx, "fetch_7timer", return_value={}), \
		 patch.object(fx, "invoke_scoring_binary", return_value=_scoring_stub("chile")), \
		 patch.object(fx, "CACHE_DIR", tmp_path), \
		 patch.object(fx, "STATE_PATH", tmp_path / "state.json"), \
		 patch.object(fx, "PREV_STATE_PATH", tmp_path / "state.prev.json"), \
		 patch.object(fx, "_notify"):
		assert fx.main() == 0
	site = json.loads((tmp_path / "state.json").read_text())["sites"][0]
	assert site["status"] == "ok"
	assert site["meta"]["degraded"] == ["7timer"]


def test_main_7timer_up_no_degraded(intl_cfg, tmp_path):
	"""When 7Timer returns data, meta carries no 'degraded' key (no badge)."""
	seventimer = {fx._parse_utc_hour("2026-05-29T04:00:00"): (2, 1)}
	with patch.object(fx, "load_config", return_value=intl_cfg), \
		 patch.object(fx, "fetch_open_meteo", return_value=_om_one_hour()), \
		 patch.object(fx, "fetch_open_meteo_convergence", lambda *a, **k: {}), \
		 patch.object(fx, "fetch_7timer", return_value=seventimer), \
		 patch.object(fx, "invoke_scoring_binary", return_value=_scoring_stub("chile")), \
		 patch.object(fx, "CACHE_DIR", tmp_path), \
		 patch.object(fx, "STATE_PATH", tmp_path / "state.json"), \
		 patch.object(fx, "PREV_STATE_PATH", tmp_path / "state.prev.json"), \
		 patch.object(fx, "_notify"):
		assert fx.main() == 0
	assert "degraded" not in json.loads((tmp_path / "state.json").read_text())["sites"][0]["meta"]
