"""Comprehensive tests for bot_engine.py."""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from bot_engine import (
    JS_RESERVED,
    extract_names,
    PlotTracker,
    is_harvest_ready,
    micro_pause,
    human_delay,
    init_game,
    run_bot_cycle,
)


# ---------------------------------------------------------------------------
# 1. extract_names()
# ---------------------------------------------------------------------------

class TestExtractNames:
    def test_class_names(self):
        src = "class Foo {\n  constructor() {}\n}\nclass Bar {}"
        assert extract_names(src) == ['Bar', 'Foo']

    def test_function_names(self):
        src = "function alpha() {}\nfunction beta() {}"
        assert extract_names(src) == ['alpha', 'beta']

    def test_async_function_names(self):
        src = "async function loadData() {}\nasync function saveData() {}"
        assert extract_names(src) == ['loadData', 'saveData']

    def test_export_default_class(self):
        src = "export default class MainController {}"
        assert extract_names(src) == ['MainController']

    def test_filters_reserved_words(self):
        src = "class function {}\nfunction return() {}\nclass const {}"
        assert extract_names(src) == []

    def test_mixed_declarations(self):
        src = (
            "class GameEngine {\n}\n"
            "function initGame() {\n}\n"
            "async function fetchState() {\n}\n"
            "export default class MainApp {\n}\n"
            "function _privateHelper() {\n}\n"
        )
        result = extract_names(src)
        assert 'GameEngine' in result
        assert 'initGame' in result
        assert 'fetchState' in result
        assert 'MainApp' in result
        assert '_privateHelper' in result

    def test_empty_source(self):
        assert extract_names("") == []

    def test_no_declarations(self):
        assert extract_names("let x = 1;\nconst y = 2;") == []

    def test_returns_sorted_list(self):
        src = "function zebra() {}\nfunction alpha() {}\nclass Delta {}"
        result = extract_names(src)
        assert result == sorted(result)

    def test_indented_declarations(self):
        src = "    class MyClass {\n    }\n  function myFunc() {}"
        result = extract_names(src)
        assert 'MyClass' in result
        assert 'myFunc' in result

    def test_duplicates_collapsed(self):
        src = "class Foo {}\nclass Foo {}"
        assert extract_names(src) == ['Foo']

    def test_all_reserved_filtered(self):
        for word in sorted(JS_RESERVED):
            src = f"class {word} {{}}"
            assert extract_names(src) == [], f"Reserved word '{word}' should be filtered"

    def test_non_reserved_underscore_names(self):
        src = "function _internal() {}"
        assert extract_names(src) == ['_internal']


# ---------------------------------------------------------------------------
# 2. PlotTracker.__init__ / can_interact / mark_interacted
# ---------------------------------------------------------------------------

class TestPlotTrackerInteraction:
    def test_init_creates_empty_state(self, tmp_path):
        tracker = PlotTracker(state_file=str(tmp_path / 'state.json'))
        assert tracker._state == {}

    def test_init_loads_existing_state(self, tmp_path):
        path = tmp_path / 'state.json'
        path.write_text(json.dumps({"1": 1234567890.0}))
        tracker = PlotTracker(state_file=str(path))
        assert tracker._state == {"1": 1234567890.0}

    def test_init_corrupt_file(self, tmp_path):
        path = tmp_path / 'state.json'
        path.write_text("NOT JSON {{{")
        tracker = PlotTracker(state_file=str(path))
        assert tracker._state == {}

    def test_can_interact_new_bed(self, tmp_path):
        tracker = PlotTracker(state_file=str(tmp_path / 's.json'))
        assert tracker.can_interact('bed_1') is True

    def test_can_interact_on_cooldown(self, tmp_path):
        tracker = PlotTracker(state_file=str(tmp_path / 's.json'))
        tracker._state['bed_1'] = time.time()
        assert tracker.can_interact('bed_1', cooldown_hours=24) is False

    def test_can_interact_cooldown_expired(self, tmp_path):
        tracker = PlotTracker(state_file=str(tmp_path / 's.json'))
        tracker._state['bed_1'] = time.time() - 25 * 3600
        assert tracker.can_interact('bed_1', cooldown_hours=24) is True

    def test_can_interact_custom_cooldown(self, tmp_path):
        tracker = PlotTracker(state_file=str(tmp_path / 's.json'))
        tracker._state['b1'] = time.time() - 5 * 3600
        assert tracker.can_interact('b1', cooldown_hours=6) is False
        assert tracker.can_interact('b1', cooldown_hours=4) is True

    def test_mark_interacted_persists(self, tmp_path):
        path = tmp_path / 's.json'
        tracker = PlotTracker(state_file=str(path))
        tracker.mark_interacted('bed_X')
        assert 'bed_X' in tracker._state
        assert isinstance(tracker._state['bed_X'], float)
        tracker.flush_if_dirty()
        assert path.exists()
        reloaded = json.loads(path.read_text())
        assert 'bed_X' in reloaded

    def test_mark_interacted_overwrites(self, tmp_path):
        tracker = PlotTracker(state_file=str(tmp_path / 's.json'))
        tracker.mark_interacted('b1')
        t1 = tracker._state['b1']
        time.sleep(0.05)
        tracker.mark_interacted('b1')
        t2 = tracker._state['b1']
        assert t2 >= t1

    def test_multiple_beds(self, tmp_path):
        tracker = PlotTracker(state_file=str(tmp_path / 's.json'))
        tracker.mark_interacted('A')
        tracker.mark_interacted('B')
        assert tracker.can_interact('A') is False
        assert tracker.can_interact('B') is False
        assert tracker.can_interact('C') is True


# ---------------------------------------------------------------------------
# 3. PlotTracker seed rotation
# ---------------------------------------------------------------------------

class TestPlotTrackerSeedRotation:
    def test_can_plant_new_seed(self, tmp_path):
        tracker = PlotTracker(state_file=str(tmp_path / 's.json'))
        assert tracker.can_plant_seed_in_bed('bed1', 'seedA') is True

    def test_cannot_plant_same_seed_within_rotation(self, tmp_path):
        tracker = PlotTracker(state_file=str(tmp_path / 's.json'))
        tracker.mark_seed_planted('bed1', 'seedA')
        assert tracker.can_plant_seed_in_bed('bed1', 'seedA', rotation_hours=6) is False

    def test_can_plant_different_seed_same_bed(self, tmp_path):
        tracker = PlotTracker(state_file=str(tmp_path / 's.json'))
        tracker.mark_seed_planted('bed1', 'seedA')
        assert tracker.can_plant_seed_in_bed('bed1', 'seedB') is True

    def test_can_plant_after_rotation(self, tmp_path):
        tracker = PlotTracker(state_file=str(tmp_path / 's.json'))
        key = 'sb:bed1:seedA'
        tracker._state[key] = time.time() - 7 * 3600
        assert tracker.can_plant_seed_in_bed('bed1', 'seedA', rotation_hours=6) is True

    def test_mark_seed_planted_persists(self, tmp_path):
        path = tmp_path / 's.json'
        tracker = PlotTracker(state_file=str(path))
        tracker.mark_seed_planted('b1', 's1')
        tracker.flush_if_dirty()
        reloaded = json.loads(path.read_text())
        assert 'sb:b1:s1' in reloaded

    def test_key_format(self, tmp_path):
        tracker = PlotTracker(state_file=str(tmp_path / 's.json'))
        tracker.mark_seed_planted('garden_42', 'Tulip_red')
        assert 'sb:garden_42:Tulip_red' in tracker._state


# ---------------------------------------------------------------------------
# 4. PlotTracker.cleanup()
# ---------------------------------------------------------------------------

class TestPlotTrackerCleanup:
    def test_removes_old_entries(self, tmp_path):
        tracker = PlotTracker(state_file=str(tmp_path / 's.json'))
        tracker._state['old_bed'] = time.time() - 50 * 3600
        tracker._state['new_bed'] = time.time()
        tracker.cleanup(max_age_hours=48)
        assert 'old_bed' not in tracker._state
        assert 'new_bed' in tracker._state

    def test_keeps_entries_at_boundary(self, tmp_path):
        tracker = PlotTracker(state_file=str(tmp_path / 's.json'))
        tracker._state['exact'] = time.time() - 48 * 3600
        tracker.cleanup(max_age_hours=48)
        assert 'exact' in tracker._state

    def test_keeps_non_numeric_values(self, tmp_path):
        tracker = PlotTracker(state_file=str(tmp_path / 's.json'))
        tracker._state['incompat:x'] = ['id1', 'id2']
        tracker._state['old'] = time.time() - 100 * 3600
        tracker.cleanup(max_age_hours=48)
        assert tracker._state['incompat:x'] == ['id1', 'id2']

    def test_persists_after_cleanup(self, tmp_path):
        path = tmp_path / 's.json'
        tracker = PlotTracker(state_file=str(path))
        tracker._state['gone'] = time.time() - 100 * 3600
        tracker._state['stays'] = time.time()
        tracker.cleanup()
        reloaded = json.loads(path.read_text())
        assert 'gone' not in reloaded
        assert 'stays' in reloaded

    def test_empty_state(self, tmp_path):
        tracker = PlotTracker(state_file=str(tmp_path / 's.json'))
        tracker.cleanup()
        assert tracker._state == {}


# ---------------------------------------------------------------------------
# 5. is_harvest_ready()
# ---------------------------------------------------------------------------

class TestIsHarvestReady:
    def test_ready_past_timestamp_string_digit(self):
        ts_ms = str(int((time.time() - 100) * 1000))
        assert is_harvest_ready({'dateGrowth': ts_ms}) is True

    def test_not_ready_future_timestamp_string_digit(self):
        ts_ms = str(int((time.time() + 100) * 1000))
        assert is_harvest_ready({'dateGrowth': ts_ms}) is False

    def test_ready_int_timestamp_large(self):
        ts_ms = int((time.time() - 100) * 1000)
        assert is_harvest_ready({'dateGrowth': ts_ms}) is True

    def test_not_ready_int_timestamp_large_future(self):
        ts_ms = int((time.time() + 100) * 1000)
        assert is_harvest_ready({'dateGrowth': ts_ms}) is False

    def test_ready_int_seconds_small(self):
        ts_s = int(time.time()) - 100
        assert is_harvest_ready({'dateGrowth': ts_s}) is True

    def test_not_ready_int_seconds_small_future(self):
        ts_s = int(time.time()) + 100
        assert is_harvest_ready({'dateGrowth': ts_s}) is False

    def test_ready_iso_datetime(self):
        dt = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert is_harvest_ready({'dateGrowth': dt}) is True

    def test_not_ready_iso_datetime_future(self):
        dt = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        assert is_harvest_ready({'dateGrowth': dt}) is False

    def test_ready_iso_z_suffix(self):
        dt = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        assert is_harvest_ready({'dateGrowth': dt}) is True

    def test_missing_date_growth(self):
        assert is_harvest_ready({}) is False

    def test_none_date_growth(self):
        assert is_harvest_ready({'dateGrowth': None}) is False

    def test_empty_string_date_growth(self):
        assert is_harvest_ready({'dateGrowth': ''}) is False

    def test_invalid_string(self):
        assert is_harvest_ready({'dateGrowth': 'not-a-date'}) is False

    def test_zero_int(self):
        assert is_harvest_ready({'dateGrowth': 0}) is False

    def test_float_large(self):
        ts_ms = (time.time() - 100) * 1000
        assert is_harvest_ready({'dateGrowth': float(ts_ms)}) is True


# ---------------------------------------------------------------------------
# 6. micro_pause()
# ---------------------------------------------------------------------------

class TestMicroPause:
    def test_calls_sleep_randomly(self):
        mock_sleep = MagicMock()
        calls = 0
        for _ in range(200):
            micro_pause(sleep_fn=mock_sleep)
            calls += mock_sleep.call_count
        assert calls > 0

    def test_sleep_called_with_in_range(self):
        mock_sleep = MagicMock()
        for _ in range(500):
            mock_sleep.reset_mock()
            micro_pause(sleep_fn=mock_sleep)
            if mock_sleep.call_count:
                dur = mock_sleep.call_args[0][0]
                assert 0.1 <= dur <= 0.6

    def test_no_call_majority_of_time(self):
        mock_sleep = MagicMock()
        sleep_count = 0
        for _ in range(1000):
            mock_sleep.reset_mock()
            micro_pause(sleep_fn=mock_sleep)
            sleep_count += mock_sleep.call_count
        assert sleep_count < 600

    def test_never_exceeds_06(self):
        mock_sleep = MagicMock()
        for _ in range(1000):
            mock_sleep.reset_mock()
            micro_pause(sleep_fn=mock_sleep)
            if mock_sleep.call_count:
                assert mock_sleep.call_args[0][0] <= 0.6


# ---------------------------------------------------------------------------
# 7. human_delay()
# ---------------------------------------------------------------------------

class TestHumanDelay:
    def test_returns_within_bounds(self):
        config = {'action_delay_min': 4, 'action_delay_max': 10}
        for _ in range(200):
            val = human_delay(config)
            assert val >= 2.0
            assert val <= 25.0

    def test_respects_custom_config(self):
        config = {'action_delay_min': 10, 'action_delay_max': 20}
        vals = [human_delay(config) for _ in range(300)]
        assert min(vals) >= 5.0
        assert max(vals) <= 50.0

    def test_custom_action_key(self):
        config = {'harvest_delay_min': 2, 'harvest_delay_max': 5}
        vals = [human_delay(config, action_key='harvest') for _ in range(200)]
        assert min(vals) >= 1.0
        assert max(vals) <= 12.5

    def test_defaults_when_no_config(self):
        vals = [human_delay({}) for _ in range(200)]
        assert min(vals) >= 2.0

    def test_minimum_clamp(self):
        config = {'action_delay_min': 100, 'action_delay_max': 100}
        for _ in range(50):
            val = human_delay(config)
            assert val >= 50.0

    def test_maximum_clamp(self):
        config = {'action_delay_min': 1, 'action_delay_max': 1}
        for _ in range(50):
            val = human_delay(config)
            assert val <= 2.5


# ---------------------------------------------------------------------------
# 8. init_game()
# ---------------------------------------------------------------------------

class TestInitGame:
    def test_calls_ready_check(self):
        run_js = MagicMock(side_effect=[True, {'k': 'v'}, {}, {}])
        ife = init_game(run_js, wait_for_ready=True)
        assert run_js.call_count >= 1
        first_call = run_js.call_args_list[0][0][0]
        assert 'Application' in first_call

    def test_skips_ready_check(self):
        run_js = MagicMock(return_value={})
        ife = init_game(run_js, wait_for_ready=False)
        first_call = run_js.call_args_list[0][0][0]
        assert 'assets' in first_call

    def test_raises_on_game_not_ready(self):
        run_js = MagicMock(return_value=False)
        with pytest.raises(RuntimeError, match='did not initialize'):
            init_game(run_js, wait_for_ready=True)

    def test_raises_on_bridge_error(self):
        run_js = MagicMock(return_value={'_error': 'something broke'})
        with pytest.raises(RuntimeError, match='Bridge error'):
            init_game(run_js, wait_for_ready=True)

    def test_returns_ife_callable(self):
        run_js = MagicMock(side_effect=[True, {'k': 'v'}, {}, {}])
        ife = init_game(run_js, wait_for_ready=True)
        assert callable(ife)

    def test_script_loading_order(self):
        responses = [True, {'utils.js': 'url1'}, {'utils.js': 'src1'}, {}]
        run_js = MagicMock(side_effect=responses)
        init_game(run_js, wait_for_ready=True)
        eval_call = run_js.call_args_list[3][0][0]
        assert 'order' in eval_call
        assert 'utils.js' in eval_call

    def test_raises_on_non_dict_urls(self):
        run_js = MagicMock(side_effect=[True, "not a dict"])
        with pytest.raises(RuntimeError, match='Expected dict'):
            init_game(run_js, wait_for_ready=True)

    def test_ife_wraps_bridge_error(self):
        responses = [True, {}, {}, {}]
        run_js = MagicMock(side_effect=responses)
        ife = init_game(run_js, wait_for_ready=True)
        run_js.side_effect = [{'_error': 'test err'}]
        with pytest.raises(RuntimeError, match='test err'):
            ife('test js')


# ---------------------------------------------------------------------------
# 9. run_bot_cycle()
# ---------------------------------------------------------------------------

class TestRunBotCycle:
    def _make_tracker(self, tmp_path):
        return PlotTracker(state_file=str(tmp_path / 'state.json'))

    def _base_config(self):
        return {
            'cooldown_hours': 24,
            'max_actions_per_cycle': 0,
            'action_delay_min': 1,
            'action_delay_max': 2,
            'seed_bed_rotation_hours': 6,
            'sandbagging_enabled': False,
            'sandbagging_avoid_best_chance': 0,
        }

    @patch('bot_engine.random')
    @patch('bot_engine.SeedConfig')
    @patch('bot_engine.micro_pause')
    def test_returns_empty_on_skip(self, mock_mp, mock_sc, mock_rand, tmp_path):
        mock_rand.random.side_effect = [0.01]
        mock_sc.return_value.get_all.return_value = {}
        tracker = self._make_tracker(tmp_path)
        mock_ife = MagicMock()
        result = run_bot_cycle(mock_ife, tracker, self._base_config())
        assert result == {'harvested': [], 'planted': [], 'errors': []}
        mock_ife.assert_not_called()

    @patch('bot_engine.random')
    @patch('bot_engine.SeedConfig')
    @patch('bot_engine.micro_pause')
    @patch('bot_engine.human_delay', return_value=1)
    def test_returns_empty_when_max_actions_zero(self, mock_hd, mock_mp, mock_sc, mock_rand, tmp_path):
        mock_rand.random.side_effect = [0.5, 0]
        mock_rand.choices.return_value = [0]
        mock_sc.return_value.get_all.return_value = {}
        tracker = self._make_tracker(tmp_path)
        mock_ife = MagicMock()
        result = run_bot_cycle(mock_ife, tracker, self._base_config())
        assert result == {'harvested': [], 'planted': [], 'errors': []}

    @patch('bot_engine.random')
    @patch('bot_engine.SeedConfig')
    @patch('bot_engine.micro_pause')
    @patch('bot_engine.human_delay', return_value=1)
    def test_returns_empty_on_gardens_error(self, mock_hd, mock_mp, mock_sc, mock_rand, tmp_path):
        mock_rand.random.side_effect = [0.5, 0.5]
        mock_rand.choices.return_value = [1]
        mock_rand.shuffle = MagicMock()
        mock_sc.return_value.get_all.return_value = {}
        tracker = self._make_tracker(tmp_path)
        mock_ife = MagicMock(return_value={'_error': 'garden fail'})
        result = run_bot_cycle(mock_ife, tracker, self._base_config())
        assert result['errors'] == []

    @patch('bot_engine.random')
    @patch('bot_engine.SeedConfig')
    @patch('bot_engine.micro_pause')
    @patch('bot_engine.human_delay', return_value=1)
    def test_returns_empty_on_gardens_non_list(self, mock_hd, mock_mp, mock_sc, mock_rand, tmp_path):
        mock_rand.random.side_effect = [0.5, 0.5]
        mock_rand.choices.return_value = [1]
        mock_rand.shuffle = MagicMock()
        mock_sc.return_value.get_all.return_value = {}
        tracker = self._make_tracker(tmp_path)
        mock_ife = MagicMock(return_value="string")
        result = run_bot_cycle(mock_ife, tracker, self._base_config())
        assert result['planted'] == []

    @patch('bot_engine.random')
    @patch('bot_engine.SeedConfig')
    @patch('bot_engine.micro_pause')
    @patch('bot_engine.human_delay', return_value=1)
    def test_harvest_ready_bed(self, mock_hd, mock_mp, mock_sc, mock_rand, tmp_path):
        mock_rand.random.side_effect = [0.5, 0.5, 0.5]
        mock_rand.choices.return_value = [1]
        mock_rand.shuffle = lambda x: None
        mock_sc.return_value.get_all.return_value = {}
        tracker = self._make_tracker(tmp_path)
        past_ts = str(int((time.time() - 100) * 1000))
        gardens = [{'userGardensID': 'g1', 'code': 'Garden1', 'placedBeds': [
            {'userBedsID': 'b1', 'plantedSeed': {'userFarmingID': 'f1', 'dateGrowth': past_ts, 'seedCode': 'Tulip'}}
        ]}]
        inv = [{'itemType': 'farmSeeds', 'itemCode': 'Tulip', 'itemID': 's1', 'count': 5}]
        combined = {'gardens': gardens, 'items': inv, 'bedToSeedGroup': {}, 'seedToGroup': {}, 'seedInfo': {}}
        harvest_result = {'ok': True, 'data': {}}
        mock_ife = MagicMock(side_effect=[combined, harvest_result])
        result = run_bot_cycle(mock_ife, tracker, self._base_config())
        assert len(result['harvested']) == 1
        assert result['harvested'][0]['seed'] == 'Tulip'

    @patch('bot_engine.random')
    @patch('bot_engine.SeedConfig')
    @patch('bot_engine.micro_pause')
    @patch('bot_engine.human_delay', return_value=1)
    def test_plant_in_empty_bed(self, mock_hd, mock_mp, mock_sc, mock_rand, tmp_path):
        mock_rand.random.side_effect = [0.5, 0.5, 0.5]
        mock_rand.choices.return_value = [1]
        mock_rand.shuffle = lambda x: None
        mock_rand.choice = lambda lst: lst[0]
        mock_sc.return_value.get_all.return_value = {}
        tracker = self._make_tracker(tmp_path)
        gardens = [{'userGardensID': 'g1', 'code': 'G1', 'placedBeds': [
            {'userBedsID': 'b1', 'plantedSeed': None, 'itemCode': 'BedTypeA'}
        ]}]
        inv = [{'itemType': 'farmSeeds', 'itemCode': 'Rose', 'itemID': 's1', 'count': 3}]
        combined = {'gardens': gardens, 'items': inv, 'bedToSeedGroup': {}, 'seedToGroup': {}, 'seedInfo': {}}
        plant_result = {'ok': True, 'data': {}}
        mock_ife = MagicMock(side_effect=[combined, plant_result])
        result = run_bot_cycle(mock_ife, tracker, self._base_config())
        assert len(result['planted']) == 1
        assert result['planted'][0]['seed'] == 'Rose'

    @patch('bot_engine.random')
    @patch('bot_engine.SeedConfig')
    @patch('bot_engine.micro_pause')
    @patch('bot_engine.human_delay', return_value=1)
    def test_no_seeds_skips_planting(self, mock_hd, mock_mp, mock_sc, mock_rand, tmp_path):
        mock_rand.random.side_effect = [0.5, 0.5]
        mock_rand.choices.return_value = [1]
        mock_rand.shuffle = lambda x: None
        mock_sc.return_value.get_all.return_value = {}
        tracker = self._make_tracker(tmp_path)
        gardens = [{'userGardensID': 'g1', 'code': 'G1', 'placedBeds': [
            {'userBedsID': 'b1', 'plantedSeed': None, 'itemCode': 'BedTypeA'}
        ]}]
        combined = {'gardens': gardens, 'items': [], 'bedToSeedGroup': {}, 'seedToGroup': {}, 'seedInfo': {}}
        mock_ife = MagicMock(side_effect=[combined])
        result = run_bot_cycle(mock_ife, tracker, self._base_config())
        assert result['planted'] == []

    @patch('bot_engine.random')
    @patch('bot_engine.SeedConfig')
    @patch('bot_engine.micro_pause')
    @patch('bot_engine.human_delay', return_value=1)
    def test_cooldown_skips_harvest(self, mock_hd, mock_mp, mock_sc, mock_rand, tmp_path):
        mock_rand.random.side_effect = [0.5, 0.5]
        mock_rand.choices.return_value = [1]
        mock_rand.shuffle = lambda x: None
        mock_sc.return_value.get_all.return_value = {}
        tracker = self._make_tracker(tmp_path)
        tracker.mark_interacted('b1')
        tracker.flush_if_dirty()
        gardens = [{'userGardensID': 'g1', 'code': 'G1', 'placedBeds': [
            {'userBedsID': 'b1', 'plantedSeed': {'userFarmingID': 'f1', 'dateGrowth': '1000', 'seedCode': 'X'}}
        ]}]
        combined = {'gardens': gardens, 'items': [], 'bedToSeedGroup': {}, 'seedToGroup': {}, 'seedInfo': {}}
        mock_ife = MagicMock(side_effect=[combined])
        result = run_bot_cycle(mock_ife, tracker, self._base_config())
        assert result['harvested'] == []

    @patch('bot_engine.random')
    @patch('bot_engine.SeedConfig')
    @patch('bot_engine.micro_pause')
    @patch('bot_engine.human_delay', return_value=1)
    def test_inventory_none(self, mock_hd, mock_mp, mock_sc, mock_rand, tmp_path):
        mock_rand.random.side_effect = [0.5, 0.5]
        mock_rand.choices.return_value = [1]
        mock_rand.shuffle = lambda x: None
        mock_sc.return_value.get_all.return_value = {}
        tracker = self._make_tracker(tmp_path)
        gardens = [{'userGardensID': 'g1', 'code': 'G1', 'placedBeds': []}]
        combined = {'gardens': gardens, 'items': [], 'bedToSeedGroup': {}, 'seedToGroup': {}, 'seedInfo': {}}
        mock_ife = MagicMock(side_effect=[combined])
        result = run_bot_cycle(mock_ife, tracker, self._base_config())
        assert result['planted'] == []

    @patch('bot_engine.random')
    @patch('bot_engine.SeedConfig')
    @patch('bot_engine.micro_pause')
    @patch('bot_engine.human_delay', return_value=1)
    def test_max_actions_limits_work(self, mock_hd, mock_mp, mock_sc, mock_rand, tmp_path):
        mock_rand.random.side_effect = [0.5, 0.9, 0.5]
        mock_rand.choices.return_value = [1]
        mock_rand.shuffle = lambda x: None
        mock_rand.choice = lambda lst: lst[0]
        mock_sc.return_value.get_all.return_value = {}
        tracker = self._make_tracker(tmp_path)
        beds = [{'userBedsID': f'b{i}', 'plantedSeed': None, 'itemCode': 'BedA'} for i in range(5)]
        gardens = [{'userGardensID': 'g1', 'code': 'G1', 'placedBeds': beds}]
        seeds = [{'itemType': 'farmSeeds', 'itemCode': 'S', 'itemID': f's{i}', 'count': 1} for i in range(5)]
        combined = {'gardens': gardens, 'items': seeds, 'bedToSeedGroup': {}, 'seedToGroup': {}, 'seedInfo': {}}
        plant_result = {'ok': True, 'data': {}}
        mock_ife = MagicMock(side_effect=[combined] + [plant_result] * 5)
        cfg = self._base_config()
        cfg['max_actions_per_cycle'] = 2
        result = run_bot_cycle(mock_ife, tracker, cfg)
        assert len(result['planted']) <= 2

    @patch('bot_engine.random')
    @patch('bot_engine.SeedConfig')
    @patch('bot_engine.micro_pause')
    @patch('bot_engine.human_delay', return_value=1)
    def test_harvest_failure_recorded(self, mock_hd, mock_mp, mock_sc, mock_rand, tmp_path):
        mock_rand.random.side_effect = [0.5, 0.5, 0.5]
        mock_rand.choices.return_value = [1]
        mock_rand.shuffle = lambda x: None
        mock_sc.return_value.get_all.return_value = {}
        tracker = self._make_tracker(tmp_path)
        past_ts = str(int((time.time() - 100) * 1000))
        gardens = [{'userGardensID': 'g1', 'code': 'G1', 'placedBeds': [
            {'userBedsID': 'b1', 'plantedSeed': {'userFarmingID': 'f1', 'dateGrowth': past_ts, 'seedCode': 'X'}}
        ]}]
        combined = {'gardens': gardens, 'items': [], 'bedToSeedGroup': {}, 'seedToGroup': {}, 'seedInfo': {}}
        fail_result = {'ok': False, 'error': 'harvest error msg'}
        mock_ife = MagicMock(side_effect=[combined, fail_result])
        result = run_bot_cycle(mock_ife, tracker, self._base_config())
        assert len(result['errors']) == 1
        assert 'harvest error msg' in result['errors'][0]

    @patch('bot_engine.random')
    @patch('bot_engine.SeedConfig')
    @patch('bot_engine.micro_pause')
    @patch('bot_engine.human_delay', return_value=1)
    def test_plant_incompatible_records_state(self, mock_hd, mock_mp, mock_sc, mock_rand, tmp_path):
        mock_rand.random.side_effect = [0.5, 0.5, 0.5]
        mock_rand.choices.return_value = [1]
        mock_rand.shuffle = lambda x: None
        mock_rand.choice = lambda lst: lst[0]
        mock_sc.return_value.get_all.return_value = {}
        tracker = self._make_tracker(tmp_path)
        gardens = [{'userGardensID': 'g1', 'code': 'G1', 'placedBeds': [
            {'userBedsID': 'b1', 'plantedSeed': None, 'itemCode': 'BedA'}
        ]}]
        inv = [{'itemType': 'farmSeeds', 'itemCode': 'R', 'itemID': 's1', 'count': 1}]
        combined = {'gardens': gardens, 'items': inv, 'bedToSeedGroup': {}, 'seedToGroup': {}, 'seedInfo': {}}
        fail_result = {'ok': False, 'error': 'incompatible seed type'}
        mock_ife = MagicMock(side_effect=[combined, fail_result])
        result = run_bot_cycle(mock_ife, tracker, self._base_config())
        assert len(result['errors']) == 1
        assert 'incompat:b1' in tracker._state
        assert 's1' in tracker._state['incompat:b1']

    @patch('bot_engine.random')
    @patch('bot_engine.SeedConfig')
    @patch('bot_engine.micro_pause')
    @patch('bot_engine.human_delay', return_value=1)
    def test_garden_skip_random(self, mock_hd, mock_mp, mock_sc, mock_rand, tmp_path):
        mock_rand.random.side_effect = [0.5, 0.2]
        mock_rand.choices.return_value = [1]
        mock_rand.shuffle = lambda x: None
        mock_sc.return_value.get_all.return_value = {}
        tracker = self._make_tracker(tmp_path)
        gardens = [{'userGardensID': 'g1', 'code': 'G1', 'placedBeds': [
            {'userBedsID': 'b1', 'plantedSeed': None, 'itemCode': 'BedA'}
        ]}]
        combined = {'gardens': gardens, 'items': [], 'bedToSeedGroup': {}, 'seedToGroup': {}, 'seedInfo': {}}
        mock_ife = MagicMock(side_effect=[combined])
        result = run_bot_cycle(mock_ife, tracker, self._base_config())
        assert result['planted'] == []
        assert result['harvested'] == []
