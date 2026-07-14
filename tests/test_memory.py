import os
import time
from datetime import datetime
from unittest.mock import patch

import pytest
import yaml

from memory import (
    BotMemory,
    FRONTMATTER_RE,
    _tod_from_hour,
    find_notes,
    now_iso,
    read_note,
    write_note,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_vault(tmp_path):
    for sub in ("sessions", "seeds", "gardens", "detection", "config", "profiles"):
        tmp_path.joinpath(sub).mkdir()
    return tmp_path


def _write_session_note(vault, filename, fm):
    write_note(vault / "sessions" / filename, fm)


def _read_frontmatter(path):
    with open(path, encoding="utf-8") as f:
        content = f.read()
    m = FRONTMATTER_RE.match(content)
    if not m:
        return {}
    return yaml.safe_load(m.group(1)) or {}


# ---------------------------------------------------------------------------
# 1. _tod_from_hour
# ---------------------------------------------------------------------------

class TestTodFromHour:
    def test_morning(self):
        for h in (5, 8, 11):
            assert _tod_from_hour(h) == "morning"

    def test_afternoon(self):
        for h in (12, 14, 16):
            assert _tod_from_hour(h) == "afternoon"

    def test_evening(self):
        for h in (17, 19, 21):
            assert _tod_from_hour(h) == "evening"

    def test_night(self):
        for h in (22, 0, 2, 4):
            assert _tod_from_hour(h) == "night"

    def test_boundaries(self):
        assert _tod_from_hour(4) == "night"
        assert _tod_from_hour(5) == "morning"
        assert _tod_from_hour(11) == "morning"
        assert _tod_from_hour(12) == "afternoon"
        assert _tod_from_hour(16) == "afternoon"
        assert _tod_from_hour(17) == "evening"
        assert _tod_from_hour(21) == "evening"
        assert _tod_from_hour(22) == "night"


# ---------------------------------------------------------------------------
# 2. read_note
# ---------------------------------------------------------------------------

class TestReadNote:
    def test_reads_frontmatter_and_body(self, tmp_path):
        path = tmp_path / "note.md"
        content = "---\ntype: session\ntimestamp: '2025-01-01T00:00:00'\n---\n\nHello body\n"
        path.write_text(content, encoding="utf-8")
        fm, body = read_note(str(path))
        assert fm["type"] == "session"
        assert fm["timestamp"] == "2025-01-01T00:00:00"
        assert body == "Hello body"

    def test_missing_file(self, tmp_path):
        fm, body = read_note(str(tmp_path / "nonexistent.md"))
        assert fm is None
        assert body is None

    def test_corrupt_yaml(self, tmp_path):
        path = tmp_path / "bad.md"
        path.write_text("---\n: : : invalid\n---\nBody", encoding="utf-8")
        fm, body = read_note(str(path))
        assert fm == {}
        assert body == "Body"

    def test_no_frontmatter(self, tmp_path):
        path = tmp_path / "plain.md"
        path.write_text("Just plain text\n", encoding="utf-8")
        fm, body = read_note(str(path))
        assert fm == {}
        assert body == "Just plain text\n"

    def test_empty_frontmatter(self, tmp_path):
        path = tmp_path / "empty.md"
        path.write_text("---\n---\n\nBody here\n", encoding="utf-8")
        fm, body = read_note(str(path))
        assert fm == {}
        assert body == "---\n---\n\nBody here\n"


# ---------------------------------------------------------------------------
# 3. write_note
# ---------------------------------------------------------------------------

class TestWriteNote:
    def test_creates_dirs_and_writes(self, tmp_path):
        path = str(tmp_path / "a" / "b" / "note.md")
        fm = {"type": "test", "val": 42}
        write_note(path, fm, "Body content")
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert content.startswith("---\n")
        assert "type: test" in content
        assert "val: 42" in content
        assert "Body content" in content

    def test_frontmatter_roundtrip(self, tmp_path):
        path = str(tmp_path / "note.md")
        fm = {"key": "value", "nested": {"a": 1}}
        write_note(path, fm, "body")
        loaded, _ = read_note(path)
        assert loaded["key"] == "value"
        assert loaded["nested"]["a"] == 1

    def test_overwrites_existing(self, tmp_path):
        path = str(tmp_path / "note.md")
        write_note(path, {"x": 1}, "old")
        write_note(path, {"x": 2}, "new")
        fm, body = read_note(path)
        assert fm["x"] == 2
        assert body == "new"


# ---------------------------------------------------------------------------
# 4. find_notes
# ---------------------------------------------------------------------------

class TestFindNotes:
    def test_finds_md_files(self, tmp_path):
        d = tmp_path / "notes"
        d.mkdir()
        (d / "a.md").write_text("---\ntype: x\n---\n", encoding="utf-8")
        (d / "b.md").write_text("---\ntype: y\n---\n", encoding="utf-8")
        notes = find_notes(str(d))
        assert len(notes) == 2

    def test_filters_by_type(self, tmp_path):
        d = tmp_path / "notes"
        d.mkdir()
        (d / "a.md").write_text("---\ntype: session\n---\n", encoding="utf-8")
        (d / "b.md").write_text("---\ntype: seed\n---\n", encoding="utf-8")
        notes = find_notes(str(d), note_type="session")
        assert len(notes) == 1
        assert notes[0][0] == "a.md"

    def test_skips_template(self, tmp_path):
        d = tmp_path / "notes"
        d.mkdir()
        (d / "_template.md").write_text("---\ntype: x\n---\n", encoding="utf-8")
        (d / "real.md").write_text("---\ntype: x\n---\n", encoding="utf-8")
        notes = find_notes(str(d))
        assert len(notes) == 1
        assert notes[0][0] == "real.md"

    def test_missing_dir(self, tmp_path):
        notes = find_notes(str(tmp_path / "nonexistent"))
        assert notes == []

    def test_ignores_non_md(self, tmp_path):
        d = tmp_path / "notes"
        d.mkdir()
        (d / "file.txt").write_text("hello", encoding="utf-8")
        (d / "a.md").write_text("---\ntype: x\n---\n", encoding="utf-8")
        notes = find_notes(str(d))
        assert len(notes) == 1


# ---------------------------------------------------------------------------
# 5. now_iso
# ---------------------------------------------------------------------------

class TestNowIso:
    def test_returns_iso_format(self):
        result = now_iso()
        dt = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S")
        assert dt.year == datetime.now().year

    def test_returns_string(self):
        assert isinstance(now_iso(), str)


# ---------------------------------------------------------------------------
# 6. BotMemory.__init__
# ---------------------------------------------------------------------------

class TestBotMemoryInit:
    def test_default_vault(self):
        mem = BotMemory()
        assert os.path.isdir(mem.vault_path) or not os.path.exists(mem.vault_path)
        assert mem._session_start is None
        assert mem._garden is None
        assert mem._buffer["cycles"] == 0

    def test_custom_vault(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        assert mem.vault_path == str(vault)

    def test_fresh_buffer(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        buf = mem._buffer
        assert buf["cycles"] == 0
        assert buf["harvested"] == 0
        assert buf["planted"] == 0
        assert buf["errors"] == 0
        assert buf["harvest_details"] == []
        assert buf["plant_details"] == []
        assert buf["error_details"] == []


# ---------------------------------------------------------------------------
# 7. BotMemory.start_session
# ---------------------------------------------------------------------------

class TestStartSession:
    def test_sets_session_start_and_garden(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        config = {"garden": "roses"}
        mem.start_session(config)
        assert mem._session_start is not None
        assert mem._garden == "roses"

    def test_explicit_garden_overrides_config(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        config = {"garden": "roses"}
        mem.start_session(config, garden="tulips")
        assert mem._garden == "tulips"

    def test_resets_buffer(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem._buffer["cycles"] = 99
        mem.start_session({"garden": "x"})
        assert mem._buffer["cycles"] == 0

    def test_fallback_garden(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.start_session({})
        assert mem._garden == "unknown"


# ---------------------------------------------------------------------------
# 8. BotMemory.log_cycle
# ---------------------------------------------------------------------------

class TestLogCycle:
    def test_increments_counters(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.start_session({"garden": "x"})
        results = {
            "harvested": ["bed1", "bed2"],
            "planted": ["bed3"],
            "errors": ["err1"],
        }
        mem.log_cycle(results)
        assert mem._buffer["cycles"] == 1
        assert mem._buffer["harvested"] == 2
        assert mem._buffer["planted"] == 1
        assert mem._buffer["errors"] == 1

    def test_multiple_cycles(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.start_session({"garden": "x"})
        mem.log_cycle({"harvested": ["a"], "planted": [], "errors": []})
        mem.log_cycle({"harvested": [], "planted": ["b", "c"], "errors": ["e1", "e2"]})
        assert mem._buffer["cycles"] == 2
        assert mem._buffer["harvested"] == 1
        assert mem._buffer["planted"] == 2
        assert mem._buffer["errors"] == 2

    def test_stores_details(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.start_session({"garden": "x"})
        mem.log_cycle({"harvested": ["bed1"], "planted": ["bed2"], "errors": ["bad"]})
        assert len(mem._buffer["harvest_details"]) == 1
        assert mem._buffer["harvest_details"][0]["bed"] == "bed1"
        assert len(mem._buffer["plant_details"]) == 1
        assert mem._buffer["error_details"][0]["error"] == "bad"

    def test_noop_without_session(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.log_cycle({"harvested": ["a"]})
        assert mem._buffer["cycles"] == 0

    def test_handles_empty_results(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.start_session({"garden": "x"})
        mem.log_cycle({})
        assert mem._buffer["cycles"] == 1
        assert mem._buffer["harvested"] == 0


# ---------------------------------------------------------------------------
# 9. BotMemory.log_plant / log_harvest
# ---------------------------------------------------------------------------

class TestLogPlantHarvest:
    def test_log_plant_creates_seed_note(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.log_plant("SEED01", "bed_A")
        path = vault / "seeds" / "SEED01.md"
        assert path.exists()
        fm = _read_frontmatter(path)
        assert fm["type"] == "seed"
        assert fm["total_planted"] == 1
        assert fm["best_beds"] == ["bed_A"]

    def test_log_plant_accumulates(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.log_plant("SEED01", "bed_A")
        mem.log_plant("SEED01", "bed_B")
        fm = _read_frontmatter(vault / "seeds" / "SEED01.md")
        assert fm["total_planted"] == 2
        assert fm["best_beds"] == ["bed_A", "bed_B"]

    def test_log_harvest_updates_seed(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.log_plant("SEED01", "bed_A")
        mem.log_harvest("SEED01")
        fm = _read_frontmatter(vault / "seeds" / "SEED01.md")
        assert fm["total_harvested"] == 1
        assert fm["success_rate"] == 1.0

    def test_success_rate_calculation(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.log_plant("S1", "b1")
        mem.log_plant("S1", "b2")
        mem.log_plant("S1", "b3")
        mem.log_harvest("S1")
        fm = _read_frontmatter(vault / "seeds" / "S1.md")
        assert fm["success_rate"] == round(1 / 3, 2)

    def test_deduplicates_best_beds(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.log_plant("S1", "bed_A")
        mem.log_plant("S1", "bed_A")
        fm = _read_frontmatter(vault / "seeds" / "S1.md")
        assert fm["best_beds"] == ["bed_A"]
        assert fm["total_planted"] == 2


# ---------------------------------------------------------------------------
# 10. BotMemory.log_detection
# ---------------------------------------------------------------------------

class TestLogDetection:
    def test_writes_detection_note(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.start_session({"garden": "x"})
        config = {"cycle_delay_min": 60}
        adjusted = mem.log_detection("shadowban", "slow growth", config)
        files = list((vault / "detection").glob("*.md"))
        assert len(files) == 1
        fm = _read_frontmatter(files[0])
        assert fm["type"] == "detection"
        assert fm["severity"] == "shadowban"
        assert fm["symptom"] == "slow growth"

    def test_shadowban_adjusts_config(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.start_session({"garden": "x"})
        config = {
            "cycle_delay_min": 60,
            "cycle_delay_max": 120,
            "action_delay_min": 30,
            "action_delay_max": 60,
            "sandbagging_avoid_best_chance": 0.4,
            "offline_duration_hours": 24,
        }
        adjusted = mem.log_detection("shadowban", "sym", config)
        assert config["cycle_delay_min"] == 120
        assert config["cycle_delay_max"] == 180
        assert config["action_delay_min"] == 45
        assert config["action_delay_max"] == 90
        assert config["sandbagging_avoid_best_chance"] == pytest.approx(0.6)
        assert config["offline_duration_hours"] == 24
        assert "cycle_delay_min" in adjusted

    def test_flag_adjusts_config(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.start_session({"garden": "x"})
        config = {
            "cycle_delay_min": 60,
            "cycle_delay_max": 120,
            "action_delay_min": 30,
            "action_delay_max": 60,
            "sandbagging_avoid_best_chance": 0.4,
            "offline_duration_hours": 24,
        }
        mem.log_detection("flag", "sym", config)
        assert config["cycle_delay_min"] == 120

    def test_non_shadowban_no_adjustment(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        config = {"cycle_delay_min": 60}
        adjusted = mem.log_detection("info", "nothing", config)
        assert adjusted == {}
        assert config["cycle_delay_min"] == 60

    def test_sandbagging_capped_at_1(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        config = {
            "cycle_delay_min": 60,
            "cycle_delay_max": 120,
            "action_delay_min": 30,
            "action_delay_max": 60,
            "sandbagging_avoid_best_chance": 0.9,
            "offline_duration_hours": 24,
        }
        mem.log_detection("shadowban", "sym", config)
        assert config["sandbagging_avoid_best_chance"] == 1.0

    def test_offline_duration_minimum(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        config = {
            "cycle_delay_min": 60,
            "cycle_delay_max": 120,
            "action_delay_min": 30,
            "action_delay_max": 60,
            "sandbagging_avoid_best_chance": 0.4,
            "offline_duration_hours": 2,
        }
        mem.log_detection("shadowban", "sym", config)
        assert config["offline_duration_hours"] == 6

    def test_details_dict_included(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        config = {}
        mem.log_detection("info", "note", config, details={"foo": "bar"})
        files = list((vault / "detection").glob("*.md"))
        fm = _read_frontmatter(files[0])
        assert fm["details"]["foo"] == "bar"

    def test_details_string_included(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        config = {}
        mem.log_detection("info", "note", config, details="text info")
        files = list((vault / "detection").glob("*.md"))
        fm = _read_frontmatter(files[0])
        assert fm["details"] == "text info"


# ---------------------------------------------------------------------------
# 11. BotMemory.finalize_session
# ---------------------------------------------------------------------------

class TestFinalizeSession:
    def test_writes_session_note(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.start_session({"garden": "roses", "cycle_delay_min": 60, "cycle_delay_max": 120})
        mem.log_cycle({"harvested": ["a"], "planted": ["b"], "errors": []})
        mem.finalize_session({"cycle_delay_min": 60, "cycle_delay_max": 120, "garden": "roses"})
        files = list((vault / "sessions").glob("*.md"))
        assert len(files) == 1
        fm = _read_frontmatter(files[0])
        assert fm["type"] == "session"
        assert fm["garden"] == "roses"
        assert fm["cycles"] == 1
        assert fm["harvested"] == 1
        assert fm["planted"] == 1

    def test_resets_session_state(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.start_session({"garden": "x"})
        mem.finalize_session({"cycle_delay_min": 60, "cycle_delay_max": 120})
        assert mem._session_start is None
        assert mem._garden is None

    def test_noop_without_session(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.finalize_session({})
        files = list((vault / "sessions").glob("*.md"))
        assert len(files) == 0

    def test_updates_garden_note(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.start_session({"garden": "roses"})
        mem.log_cycle({"harvested": ["a"], "planted": ["b"], "errors": []})
        mem.finalize_session({"cycle_delay_min": 60, "cycle_delay_max": 120, "garden": "roses"})
        garden_path = vault / "gardens" / "roses.md"
        assert garden_path.exists()
        fm = _read_frontmatter(garden_path)
        assert fm["type"] == "garden"
        assert fm["total_sessions"] == 1

    def test_detection_risk_in_note(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.start_session({"garden": "x"})
        mem.log_cycle({"harvested": [], "planted": [], "errors": ["e1", "e2", "e3"]})
        mem.finalize_session({"cycle_delay_min": 60, "cycle_delay_max": 120})
        files = list((vault / "sessions").glob("*.md"))
        fm = _read_frontmatter(files[0])
        assert fm["detection_risk"] == "high"


# ---------------------------------------------------------------------------
# 12. BotMemory.generate_profiles
# ---------------------------------------------------------------------------

class TestGenerateProfiles:
    def _seed_sessions(self, vault, count, garden="roses", tod_hour=8, prefix="s"):
        ts = datetime(2025, 1, 1, tod_hour, 0, 0).strftime("%Y-%m-%dT%H:%M:%S")
        for i in range(count):
            fm = {
                "type": "session",
                "timestamp": ts,
                "duration_h": 4.0,
                "harvested": 10 + i,
                "planted": 5,
                "errors": 1,
                "cycles": 20,
                "garden": garden,
                "config": {"cycle_min": 90, "cycle_max": 180},
            }
            _write_session_note(vault, f"{prefix}{i}.md", fm)

    def test_needs_two_sessions(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        _write_session_note(vault, "s1.md", {
            "type": "session",
            "timestamp": "2025-01-01T08:00:00",
            "duration_h": 4,
            "harvested": 5,
            "garden": "roses",
            "config": {"cycle_min": 90, "cycle_max": 180},
        })
        assert mem.generate_profiles() is None

    def test_generates_profile(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        self._seed_sessions(vault, 3)
        count = mem.generate_profiles()
        assert count >= 1
        profiles = list((vault / "profiles").glob("*.md"))
        assert len(profiles) >= 1
        fm = _read_frontmatter(profiles[0])
        assert fm["type"] == "profile"
        assert fm["garden"] == "roses"

    def test_confidence_levels(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        self._seed_sessions(vault, 5)
        mem.generate_profiles()
        profiles = list((vault / "profiles").glob("*.md"))
        fm = _read_frontmatter(profiles[0])
        assert fm["confidence"] == "medium"

    def test_groups_by_garden_and_tod(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        self._seed_sessions(vault, 3, garden="roses", tod_hour=8, prefix="a")
        self._seed_sessions(vault, 3, garden="tulips", tod_hour=19, prefix="b")
        count = mem.generate_profiles()
        assert count == 2
        profiles = list((vault / "profiles").glob("*.md"))
        names = {p.stem for p in profiles}
        assert "roses-morning" in names
        assert "tulips-evening" in names


# ---------------------------------------------------------------------------
# 13. BotMemory.optimize_config
# ---------------------------------------------------------------------------

class TestOptimizeConfig:
    def _make_profile(self, vault, garden, tod, **kwargs):
        fm = {
            "type": "profile",
            "garden": garden,
            "time_of_day": tod,
            "cycle_min": 90,
            "cycle_max": 180,
            "sandbagging_chance": 0.4,
            "action_min": 5,
            "action_max": 12,
            "confidence": "medium",
            "samples": 5,
        }
        fm.update(kwargs)
        write_note(vault / "profiles" / f"{garden}-{tod}.md", fm)

    def test_applies_matching_profile(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        hour = datetime.now().hour
        tod = _tod_from_hour(hour)
        self._make_profile(vault, "roses", tod, cycle_min=120, cycle_max=240)
        config = {"garden": "roses", "cycle_delay_min": 60, "cycle_delay_max": 120}
        result = mem.optimize_config(config)
        assert result["cycle_delay_min"] == 120
        assert result["cycle_delay_max"] == 240

    def test_no_match_returns_unchanged(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        config = {"garden": "unknown", "cycle_delay_min": 60}
        result = mem.optimize_config(config)
        assert result["cycle_delay_min"] == 60

    def test_no_profiles_returns_unchanged(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        config = {"garden": "roses", "cycle_delay_min": 60}
        result = mem.optimize_config(config)
        assert result["cycle_delay_min"] == 60

    def test_key_mapping(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        hour = datetime.now().hour
        tod = _tod_from_hour(hour)
        self._make_profile(vault, "roses", tod, cycle_min=200)
        config = {
            "garden": "roses",
            "cycle_delay_min": 60,
            "cycle_delay_max": 120,
            "action_delay_min": 30,
            "action_delay_max": 60,
        }
        mem.optimize_config(config)
        assert config["cycle_delay_min"] == 200


# ---------------------------------------------------------------------------
# 14. BotMemory.summary
# ---------------------------------------------------------------------------

class TestSummary:
    def test_empty_vault(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        s = mem.summary()
        assert s["total_sessions"] == 0
        assert s["total_seeds"] == 0
        assert s["total_gardens"] == 0
        assert s["total_detections"] == 0
        assert s["total_profiles"] == 0
        assert s["last_session"] == "N/A"

    def test_populated_vault(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        write_note(vault / "sessions" / "s1.md", {"type": "session", "timestamp": "2025-01-01T08:00:00"})
        write_note(vault / "sessions" / "s2.md", {"type": "session", "timestamp": "2025-01-02T08:00:00"})
        write_note(vault / "seeds" / "seed1.md", {"type": "seed", "code": "seed1"})
        write_note(vault / "gardens" / "roses.md", {"type": "garden", "code": "roses"})
        write_note(vault / "detection" / "d1.md", {"type": "detection", "severity": "info"})
        write_note(vault / "profiles" / "p1.md", {"type": "profile", "garden": "roses"})
        s = mem.summary()
        assert s["total_sessions"] == 2
        assert s["total_seeds"] == 1
        assert s["total_gardens"] == 1
        assert s["total_detections"] == 1
        assert s["total_profiles"] == 1
        assert s["last_session"] == "2025-01-02T08:00:00"


# ---------------------------------------------------------------------------
# 15. BotMemory._detection_risk
# ---------------------------------------------------------------------------

class TestDetectionRisk:
    def test_low_when_no_cycles(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        assert mem._detection_risk() == "low"

    def test_low_error_rate(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.start_session({"garden": "x"})
        for _ in range(10):
            mem.log_cycle({"errors": []})
        assert mem._detection_risk() == "low"

    def test_medium_error_rate(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.start_session({"garden": "x"})
        mem.log_cycle({"errors": []})
        mem.log_cycle({"errors": ["e1"]})
        assert mem._detection_risk() == "medium"

    def test_high_error_rate(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.start_session({"garden": "x"})
        mem.log_cycle({"errors": ["e1"]})
        assert mem._detection_risk() == "high"

    def test_boundary_medium(self, tmp_path):
        vault = _make_vault(tmp_path)
        mem = BotMemory(vault_path=str(vault))
        mem.start_session({"garden": "x"})
        for _ in range(10):
            mem.log_cycle({"errors": ["e1"]})
        assert mem._detection_risk() == "high"
