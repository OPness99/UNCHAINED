import json
import os
import sys
from unittest import mock

import pytest

import config


# ---------------------------------------------------------------------------
# _data_dir()
# ---------------------------------------------------------------------------

class TestDataDir:
    def test_not_frozen_returns_cwd(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            assert config._data_dir() == os.getcwd()

    def test_frozen_returns_exe_dir(self, tmp_path):
        fake_exe = tmp_path / "myapp.exe"
        fake_exe.touch()
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", str(fake_exe)):
            assert config._data_dir() == str(tmp_path)


# ---------------------------------------------------------------------------
# DEFAULT_CONFIG
# ---------------------------------------------------------------------------

class TestDefaultConfig:
    EXPECTED_KEYS = {
        "game_url",
        "user_data_dir",
        "headless",
        "action_delay_min",
        "action_delay_max",
        "cycle_delay_min",
        "cycle_delay_max",
        "cooldown_hours",
        "max_consecutive_failures",
        "session_duration_choices",
        "session_break_min_mins",
        "session_break_max_mins",
        "seed_bed_rotation_hours",
        "sandbagging_enabled",
        "sandbagging_avoid_best_chance",
        "offline_base_probability",
        "offline_probability_jitter",
        "offline_duration_hours",
        "offline_check_interval_hours",
        "ml_enabled",
        "ml_min_training_samples",
        "ml_auto_retrain",
        "ml_anomaly_detection",
        "discord_webhook_url",
        "discord_token",
        "discord_channel_id",
        "discord_notify_cycles",
        "discord_notify_errors",
        "discord_notify_daily",
        "task_wall_enabled",
        "task_wall_refresh_seconds",
        "task_wall_auto_select",
        "task_wall_max_simultaneous",
        "task_wall_prefer_farming",
        "task_wall_skip_external",
        "task_wall_claim_rewards",
    }

    def test_all_expected_keys_exist(self):
        assert self.EXPECTED_KEYS.issubset(set(config.DEFAULT_CONFIG.keys()))

    def test_no_unexpected_keys(self):
        assert set(config.DEFAULT_CONFIG.keys()) == self.EXPECTED_KEYS

    def test_game_url(self):
        assert config.DEFAULT_CONFIG["game_url"] == "https://chainers.io/game"

    def test_headless_default_false(self):
        assert config.DEFAULT_CONFIG["headless"] is False

    def test_action_delays(self):
        assert config.DEFAULT_CONFIG["action_delay_min"] == 4
        assert config.DEFAULT_CONFIG["action_delay_max"] == 10

    def test_cycle_delays(self):
        assert config.DEFAULT_CONFIG["cycle_delay_min"] == 90
        assert config.DEFAULT_CONFIG["cycle_delay_max"] == 180

    def test_cooldown_hours(self):
        assert config.DEFAULT_CONFIG["cooldown_hours"] == 24

    def test_max_consecutive_failures(self):
        assert config.DEFAULT_CONFIG["max_consecutive_failures"] == 10

    def test_session_duration_choices(self):
        assert config.DEFAULT_CONFIG["session_duration_choices"] == [2, 4, 6, 10]

    def test_session_break_mins(self):
        assert config.DEFAULT_CONFIG["session_break_min_mins"] == 30
        assert config.DEFAULT_CONFIG["session_break_max_mins"] == 360

    def test_seed_bed_rotation_hours(self):
        assert config.DEFAULT_CONFIG["seed_bed_rotation_hours"] == 6

    def test_sandbagging_defaults(self):
        assert config.DEFAULT_CONFIG["sandbagging_enabled"] is True
        assert config.DEFAULT_CONFIG["sandbagging_avoid_best_chance"] == 0.4

    def test_offline_defaults(self):
        assert config.DEFAULT_CONFIG["offline_base_probability"] == 0.5
        assert config.DEFAULT_CONFIG["offline_probability_jitter"] == 0.2
        assert config.DEFAULT_CONFIG["offline_duration_hours"] == 24
        assert config.DEFAULT_CONFIG["offline_check_interval_hours"] == 24

    def test_ml_defaults(self):
        assert config.DEFAULT_CONFIG["ml_enabled"] is True
        assert config.DEFAULT_CONFIG["ml_min_training_samples"] == 10
        assert config.DEFAULT_CONFIG["ml_auto_retrain"] is True
        assert config.DEFAULT_CONFIG["ml_anomaly_detection"] is True

    def test_discord_defaults(self):
        assert config.DEFAULT_CONFIG["discord_webhook_url"] == ""
        assert config.DEFAULT_CONFIG["discord_token"] == ""
        assert config.DEFAULT_CONFIG["discord_channel_id"] == 1525245987461791765
        assert config.DEFAULT_CONFIG["discord_notify_cycles"] is True
        assert config.DEFAULT_CONFIG["discord_notify_errors"] is True
        assert config.DEFAULT_CONFIG["discord_notify_daily"] is False

    def test_default_config_is_dict(self):
        assert isinstance(config.DEFAULT_CONFIG, dict)

    def test_default_config_not_shared(self):
        copy = dict(config.DEFAULT_CONFIG)
        copy["game_url"] = "mutated"
        assert config.DEFAULT_CONFIG["game_url"] == "https://chainers.io/game"


# ---------------------------------------------------------------------------
# load_config() / save_config()
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_returns_defaults_when_no_file(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        with mock.patch.object(config, "CONFIG_FILE", str(cfg_path)):
            result = config.load_config()
        assert result["game_url"] == "https://chainers.io/game"
        assert result["headless"] is False

    def test_loads_saved_values(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        saved = {"game_url": "http://custom", "headless": True}
        cfg_path.write_text(json.dumps(saved))
        with mock.patch.object(config, "CONFIG_FILE", str(cfg_path)):
            result = config.load_config()
        assert result["game_url"] == "http://custom"
        assert result["headless"] is True

    def test_saved_merges_over_defaults(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        saved = {"action_delay_min": 99}
        cfg_path.write_text(json.dumps(saved))
        with mock.patch.object(config, "CONFIG_FILE", str(cfg_path)):
            result = config.load_config()
        assert result["action_delay_min"] == 99
        assert result["action_delay_max"] == 10

    def test_corrupt_json_returns_defaults(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{invalid json!!!")
        with mock.patch.object(config, "CONFIG_FILE", str(cfg_path)):
            result = config.load_config()
        assert result == dict(config.DEFAULT_CONFIG)

    def test_empty_file_returns_defaults(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("")
        with mock.patch.object(config, "CONFIG_FILE", str(cfg_path)):
            result = config.load_config()
        assert result == dict(config.DEFAULT_CONFIG)

    def test_non_dict_json_returns_defaults(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps([1, 2, 3]))
        with mock.patch.object(config, "CONFIG_FILE", str(cfg_path)):
            result = config.load_config()
        assert result == dict(config.DEFAULT_CONFIG)

    def test_loads_setup_complete_flag(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        saved = {"setup_complete": True, "setup_version": 1}
        cfg_path.write_text(json.dumps(saved))
        with mock.patch.object(config, "CONFIG_FILE", str(cfg_path)):
            result = config.load_config()
        assert result["setup_complete"] is True
        assert result["setup_version"] == 1


class TestSaveConfig:
    def test_creates_file(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        with mock.patch.object(config, "CONFIG_FILE", str(cfg_path)):
            config.save_config({"key": "value"})
        assert cfg_path.exists()

    def test_writes_correct_content(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        data = {"game_url": "http://test", "headless": True}
        with mock.patch.object(config, "CONFIG_FILE", str(cfg_path)):
            config.save_config(data)
        with open(cfg_path) as f:
            loaded = json.load(f)
        assert loaded == data

    def test_overwrites_existing(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"old": 1}))
        with mock.patch.object(config, "CONFIG_FILE", str(cfg_path)):
            config.save_config({"new": 2})
        with open(cfg_path) as f:
            loaded = json.load(f)
        assert loaded == {"new": 2}
        assert "old" not in loaded

    def test_roundtrip_save_load(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        original = dict(config.DEFAULT_CONFIG)
        original["game_url"] = "http://roundtrip"
        with mock.patch.object(config, "CONFIG_FILE", str(cfg_path)):
            config.save_config(original)
            loaded = config.load_config()
        assert loaded == original


# ---------------------------------------------------------------------------
# is_first_run()
# ---------------------------------------------------------------------------

class TestIsFirstRun:
    def test_true_when_no_config(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        with mock.patch.object(config, "CONFIG_FILE", str(cfg_path)):
            assert config.is_first_run() is True

    def test_false_when_setup_complete(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"setup_complete": True}))
        with mock.patch.object(config, "CONFIG_FILE", str(cfg_path)):
            assert config.is_first_run() is False

    def test_false_when_setup_complete_false_but_present(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"setup_complete": False}))
        with mock.patch.object(config, "CONFIG_FILE", str(cfg_path)):
            result = config.is_first_run()
        # When setup_complete is falsy, is_first_run writes True and returns False
        assert result is False

    def test_returns_false_and_writes_setup_complete(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"some_key": "some_val"}))
        with mock.patch.object(config, "CONFIG_FILE", str(cfg_path)):
            result = config.is_first_run()
        assert result is False
        with open(cfg_path) as f:
            written = json.load(f)
        assert written["setup_complete"] is True
        assert written["setup_version"] == 1
        assert written["some_key"] == "some_val"

    def test_corrupt_file_returns_true(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("not json!!")
        with mock.patch.object(config, "CONFIG_FILE", str(cfg_path)):
            assert config.is_first_run() is True

    def test_empty_file_returns_true(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("")
        with mock.patch.object(config, "CONFIG_FILE", str(cfg_path)):
            assert config.is_first_run() is True


# ---------------------------------------------------------------------------
# detect_chrome_exe()
# ---------------------------------------------------------------------------

class TestDetectChromeExe:
    def test_returns_none_when_no_candidate_exists(self, tmp_path):
        env = {
            "ProgramFiles": str(tmp_path / "pf"),
            "ProgramFiles(x86)": str(tmp_path / "pf86"),
            "LOCALAPPDATA": str(tmp_path / "local"),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            assert config.detect_chrome_exe() is None

    def test_returns_program_files_path(self, tmp_path):
        chrome_dir = tmp_path / "pf" / "Google" / "Chrome" / "Application"
        chrome_dir.mkdir(parents=True)
        (chrome_dir / "chrome.exe").touch()
        env = {
            "ProgramFiles": str(tmp_path / "pf"),
            "ProgramFiles(x86)": str(tmp_path / "pf86"),
            "LOCALAPPDATA": str(tmp_path / "local"),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            result = config.detect_chrome_exe()
        assert result == str(chrome_dir / "chrome.exe")

    def test_returns_localappdata_path(self, tmp_path):
        chrome_dir = tmp_path / "local" / "Google" / "Chrome" / "Application"
        chrome_dir.mkdir(parents=True)
        (chrome_dir / "chrome.exe").touch()
        env = {
            "ProgramFiles": str(tmp_path / "pf"),
            "ProgramFiles(x86)": str(tmp_path / "pf86"),
            "LOCALAPPDATA": str(tmp_path / "local"),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            result = config.detect_chrome_exe()
        assert result == str(chrome_dir / "chrome.exe")

    def test_returns_chromium_path(self, tmp_path):
        chromium_dir = tmp_path / "pf" / "Chromium" / "Application"
        chromium_dir.mkdir(parents=True)
        (chromium_dir / "chrome.exe").touch()
        env = {
            "ProgramFiles": str(tmp_path / "pf"),
            "ProgramFiles(x86)": str(tmp_path / "pf86"),
            "LOCALAPPDATA": str(tmp_path / "local"),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            result = config.detect_chrome_exe()
        assert result == str(chromium_dir / "chrome.exe")

    def test_returns_first_found_when_multiple_exist(self, tmp_path):
        pf = tmp_path / "pf"
        (pf / "Google" / "Chrome" / "Application").mkdir(parents=True)
        (pf / "Google" / "Chrome" / "Application" / "chrome.exe").touch()
        (pf / "Chromium" / "Application").mkdir(parents=True)
        (pf / "Chromium" / "Application" / "chrome.exe").touch()
        env = {
            "ProgramFiles": str(pf),
            "ProgramFiles(x86)": str(tmp_path / "pf86"),
            "LOCALAPPDATA": str(tmp_path / "local"),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            result = config.detect_chrome_exe()
        assert result == str(pf / "Google" / "Chrome" / "Application" / "chrome.exe")

    def test_returns_none_when_directory_exists_but_no_exe(self, tmp_path):
        chrome_dir = tmp_path / "pf" / "Google" / "Chrome" / "Application"
        chrome_dir.mkdir(parents=True)
        env = {
            "ProgramFiles": str(tmp_path / "pf"),
            "ProgramFiles(x86)": str(tmp_path / "pf86"),
            "LOCALAPPDATA": str(tmp_path / "local"),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            assert config.detect_chrome_exe() is None


# ---------------------------------------------------------------------------
# detect_chrome_profile()
# ---------------------------------------------------------------------------

class TestDetectChromeProfile:
    def test_returns_none_when_no_profile_dir(self, tmp_path):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(tmp_path)}, clear=True):
            assert config.detect_chrome_profile() is None

    def test_returns_path_when_profile_dir_exists(self, tmp_path):
        profile_dir = tmp_path / "Google" / "Chrome" / "User Data"
        profile_dir.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(tmp_path)}, clear=True):
            result = config.detect_chrome_profile()
        assert result == str(profile_dir)

    def test_returns_none_when_localappdata_missing(self, tmp_path):
        with mock.patch.dict(os.environ, {}, clear=True):
            result = config.detect_chrome_profile()
        assert result is None


# ---------------------------------------------------------------------------
# ensure_state_files()
# ---------------------------------------------------------------------------

class TestEnsureStateFiles:
    def test_creates_seed_config_and_plot_state(self, tmp_path):
        with mock.patch.object(config, "_data_dir", return_value=str(tmp_path)):
            config.ensure_state_files()
        assert (tmp_path / "seed_config.json").exists()
        assert (tmp_path / "plot_state.json").exists()

    def test_created_files_are_valid_json(self, tmp_path):
        with mock.patch.object(config, "_data_dir", return_value=str(tmp_path)):
            config.ensure_state_files()
        for name in ("seed_config.json", "plot_state.json"):
            with open(tmp_path / name) as f:
                data = json.load(f)
            assert data == {}

    def test_does_not_overwrite_existing_files(self, tmp_path):
        (tmp_path / "seed_config.json").write_text(json.dumps({"existing": True}))
        (tmp_path / "plot_state.json").write_text(json.dumps({"keep": True}))
        with mock.patch.object(config, "_data_dir", return_value=str(tmp_path)):
            config.ensure_state_files()
        with open(tmp_path / "seed_config.json") as f:
            assert json.load(f) == {"existing": True}
        with open(tmp_path / "plot_state.json") as f:
            assert json.load(f) == {"keep": True}

    def test_calls_log_func_for_created_files(self, tmp_path):
        log_calls = []
        with mock.patch.object(config, "_data_dir", return_value=str(tmp_path)):
            config.ensure_state_files(log_func=log_calls.append)
        assert len(log_calls) == 2
        assert any("seed_config.json" in m for m in log_calls)
        assert any("plot_state.json" in m for m in log_calls)

    def test_no_log_when_files_already_exist(self, tmp_path):
        (tmp_path / "seed_config.json").write_text("{}")
        (tmp_path / "plot_state.json").write_text("{}")
        log_calls = []
        with mock.patch.object(config, "_data_dir", return_value=str(tmp_path)):
            config.ensure_state_files(log_func=log_calls.append)
        assert log_calls == []

    def test_creates_files_with_empty_json_structure(self, tmp_path):
        with mock.patch.object(config, "_data_dir", return_value=str(tmp_path)):
            config.ensure_state_files()
        for name in ("seed_config.json", "plot_state.json"):
            with open(tmp_path / name) as f:
                content = f.read()
            assert content.strip() == "{}"
