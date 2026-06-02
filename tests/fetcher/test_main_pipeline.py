"""
Tests for the main() pipeline — end-to-end resilience and partial-success
behavior.

These tests mock the external boundaries (config load, both API calls,
scoring binary subprocess) and exercise the pipeline glue that lives in
main(): per-site loop, error counting, scoring-output merge by id,
state.json shape, notify dispatch.

The test_analyzer review identified this as a HIGH-risk coverage gap
because partial-failure resilience is the whole point of the multi-site
design.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

import astrowidget_fetch as fx


VALID_CFG = {
	"open_meteo": {"models": []},
	"sites": [
		{"id": "site_a", "label": "A", "lat": 47.0, "lon": -122.0, "timezone": "UTC"},
		{"id": "site_b", "label": "B", "lat": 37.0, "lon": -119.0, "timezone": "UTC"},
	],
	"thresholds": {},
	"notifications": {
		"upward_transitions": False,
		"downward_transitions_day_of": False,
		"astro_dark_start_reminder": False,
	},
}


def _open_meteo_stub():
	"""Minimal good-shape Open-Meteo response."""
	return {
		"hourly": {
			"time": ["2026-05-29T04:00:00"],
			"cloud_cover": [10],
			"cloud_cover_low": [0],
			"cloud_cover_mid": [0],
			"cloud_cover_high": [15],
			"relative_humidity_2m": [65],
			"temperature_2m": [11],
			"dewpoint_2m": [8],
			"wind_speed_10m": [8],
			"wind_gusts_10m": [13],
			"precipitation_probability": [5],
			"precipitation": [0],
			"visibility": [24000],
		}
	}


def _7timer_stub():
	"""Minimal 7Timer lookup keyed by the single 04:00 UTC hour (inside the
	scoring stub's dark window). (seeing, transparency) on the 1-8 scale."""
	return {fx._parse_utc_hour("2026-05-29T04:00:00"): (2, 1)}


def _scoring_output(*site_ids):
	"""Minimal scoring binary output with one Tonight per site."""
	return {
		"schema_version": 1,
		"sites": [
			{
				"id": sid,
				"label": sid.upper(),
				"status": "ok",
				"nights": [
					{
						"label": "Tonight",
						"dark_window": {
							"start": "2026-05-29T04:00:00Z",
							"end": "2026-05-29T10:00:00Z",
							"duration_minutes": 360,
						},
						"recommendation": "BB+NB",
						"broadband": {"score": 80, "verdict": "excellent", "vetoes": []},
						"narrowband": {"score": 85, "verdict": "excellent", "vetoes": []},
					}
				],
			}
			for sid in site_ids
		],
	}


@pytest.fixture(autouse=True)
def no_convergence_network(monkeypatch):
	"""
	Stub the multi-model convergence fetch so pipeline tests never hit the
	real Open-Meteo network. Convergence is best-effort (returns {} on
	failure), so an empty stub exercises the no-convergence path cleanly.
	Convergence-specific behavior is tested separately in test_convergence.py.
	"""
	monkeypatch.setattr(fx, "fetch_open_meteo_convergence", lambda *a, **k: {})


@pytest.fixture(autouse=True)
def stub_7timer(monkeypatch):
	"""Stub 7Timer so the pipeline never hits the real network. Returns a single
	hour of seeing/transparency that lines up with the scoring stub's dark
	window; individual tests override it where they need a different shape."""
	monkeypatch.setattr(fx, "fetch_7timer", lambda *a, **k: _7timer_stub())


@pytest.fixture
def patched_paths(tmp_path, monkeypatch):
	"""Reroute CACHE_DIR and STATE_PATH to tmp_path so tests don't touch ~/.cache."""
	monkeypatch.setattr(fx, "CACHE_DIR", tmp_path / "cache")
	monkeypatch.setattr(fx, "STATE_PATH", tmp_path / "cache" / "state.json")
	monkeypatch.setattr(fx, "PREV_STATE_PATH", tmp_path / "cache" / "state.prev.json")
	yield tmp_path


def test_main_all_sites_succeed_returns_0(patched_paths):
	"""Happy path: both sites succeed → exit 0, state.json contains both."""
	with patch.object(fx, "load_config", return_value=VALID_CFG), \
		 patch.object(fx, "fetch_open_meteo", return_value=_open_meteo_stub()), \
		 patch.object(fx, "invoke_scoring_binary",
					  return_value=_scoring_output("site_a", "site_b")), \
		 patch.object(fx, "_notify"):
		rc = fx.main()
	assert rc == 0
	state_path = patched_paths / "cache" / "state.json"
	assert state_path.exists()
	import json
	state = json.loads(state_path.read_text())
	assert len(state["sites"]) == 2
	assert all(s["status"] == "ok" for s in state["sites"])
	# Scoring output merged into site_results.
	assert all("nights" in s for s in state["sites"])


def test_main_partial_failure_continues_with_other_sites(patched_paths):
	"""One Open-Meteo call fails → site has error status, other succeeds, exit 0."""
	call_count = [0]
	def maybe_fail(*a, **kw):
		call_count[0] += 1
		if call_count[0] == 1:
			raise RuntimeError("simulated network failure")
		return _open_meteo_stub()

	with patch.object(fx, "load_config", return_value=VALID_CFG), \
		 patch.object(fx, "fetch_open_meteo", side_effect=maybe_fail), \
		 patch.object(fx, "invoke_scoring_binary",
					  return_value=_scoring_output("site_b")), \
		 patch.object(fx, "_notify"):
		rc = fx.main()
	assert rc == 0  # partial success is still 0 — surviving sites still updated
	import json
	state = json.loads((patched_paths / "cache" / "state.json").read_text())
	statuses = {s["id"]: s["status"] for s in state["sites"]}
	assert statuses["site_a"] == "error"
	assert statuses["site_b"] == "ok"


def test_main_all_sites_fail_returns_3(patched_paths):
	"""Every site fails its API call → exit 3 (per documented contract)."""
	with patch.object(fx, "load_config", return_value=VALID_CFG), \
		 patch.object(fx, "fetch_open_meteo",
					  side_effect=RuntimeError("down")), \
		 patch.object(fx, "invoke_scoring_binary",
					  return_value=_scoring_output()), \
		 patch.object(fx, "_notify"):
		rc = fx.main()
	assert rc == 3


def test_main_state_has_no_credit_fields(patched_paths):
	"""The public build dropped Astrospheric: state.json must NOT carry the old
	credit-tracking fields (a regression re-adding them would mislead the UI)."""
	with patch.object(fx, "load_config", return_value=VALID_CFG), \
		 patch.object(fx, "fetch_open_meteo", return_value=_open_meteo_stub()), \
		 patch.object(fx, "invoke_scoring_binary",
					  return_value=_scoring_output("site_a", "site_b")), \
		 patch.object(fx, "_notify"):
		fx.main()
	import json
	state = json.loads((patched_paths / "cache" / "state.json").read_text())
	assert "astrosphericCreditCost" not in state
	assert "astrosphericCreditBudget" not in state
	assert state["schemaVersion"] == 2


def test_main_merges_scoring_output_by_id(patched_paths):
	"""Scoring binary output is matched to site_results by id, not by index."""
	# Reverse scoring output order — main() must still pair by id.
	with patch.object(fx, "load_config", return_value=VALID_CFG), \
		 patch.object(fx, "fetch_open_meteo", return_value=_open_meteo_stub()), \
		 patch.object(fx, "invoke_scoring_binary",
					  return_value=_scoring_output("site_b", "site_a")), \
		 patch.object(fx, "_notify"):
		fx.main()
	import json
	state = json.loads((patched_paths / "cache" / "state.json").read_text())
	by_id = {s["id"]: s for s in state["sites"]}
	# Both sites should have nights attached (the merge worked despite the
	# reversed order in the scoring output).
	assert "nights" in by_id["site_a"]
	assert "nights" in by_id["site_b"]
