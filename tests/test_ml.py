"""Comprehensive tests for the ml/ package: features, models, anomaly, inference."""

import os
import math
import datetime

import numpy as np
import pytest
import yaml

from ml.features import (
    extract_cycle_features,
    build_session_dataset,
    FEATURE_NAMES,
    _garden_encode,
    _tod_sin,
    _detection_risk_encode,
    _GARDEN_MAP,
)
from ml.models import (
    train_action_forecaster,
    train_delay_optimizer,
    load_action_forecaster,
    load_delay_optimizer,
)
from ml.anomaly import AnomalyDetector
from ml.inference import MLEngine


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_session_note(sessions_dir, idx, *, garden="garden_a", hour=10,
                        cycles=5, harvested=10, errors=0, duration_h=2.0,
                        detection_risk="low", cfg=None):
    """Create a fake session .md file with YAML frontmatter."""
    ts = datetime.datetime(2025, 6, 1, hour, 0, 0)
    fm = {
        "type": "session",
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S"),
        "garden": garden,
        "duration_h": duration_h,
        "cycles": cycles,
        "harvested": harvested,
        "planted": cycles,
        "errors": errors,
        "detection_risk": detection_risk,
        "config": cfg or {},
    }
    path = os.path.join(sessions_dir, f"session-{idx:04d}.md")
    fm_str = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False).strip()
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{fm_str}\n---\n\nSession {idx}\n")
    return path


def _write_detection_note(det_dir, idx, *, hour=8):
    ts = datetime.datetime(2025, 6, 1, hour, 30, 0)
    fm = {
        "type": "detection",
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S"),
        "severity": "shadowban",
        "symptom": "harvest drop",
    }
    path = os.path.join(det_dir, f"detection-{idx:04d}.md")
    fm_str = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False).strip()
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{fm_str}\n---\n\nDetection {idx}\n")
    return path


def _make_vault(tmp_path, n_sessions=15, n_detections=2):
    """Build a minimal vault directory tree and populate with notes."""
    sessions_dir = os.path.join(str(tmp_path), "sessions")
    det_dir = os.path.join(str(tmp_path), "detection")
    os.makedirs(sessions_dir, exist_ok=True)
    os.makedirs(det_dir, exist_ok=True)
    for i in range(n_sessions):
        _write_session_note(sessions_dir, i, hour=(6 + i) % 24,
                            cycles=max(1, 5 + i % 3),
                            harvested=max(1, 8 + i % 5))
    for i in range(n_detections):
        _write_detection_note(det_dir, i, hour=(8 + i * 5) % 24)
    return str(tmp_path)


def _synthetic_data(n=20, n_features=None):
    n_features = n_features or len(FEATURE_NAMES)
    X = np.random.randn(n, n_features).astype(np.float32)
    y_action = np.random.randint(1, 10, size=n).astype(np.int32)
    y_delay = np.random.uniform(30, 3600, size=n).astype(np.float32)
    return X, y_action, y_delay


# ---------------------------------------------------------------------------
# features.py tests
# ---------------------------------------------------------------------------

class TestExtractCycleFeatures:
    def test_returns_correct_shape(self):
        feats = extract_cycle_features({}, {})
        assert feats.shape == (len(FEATURE_NAMES),)
        assert feats.shape == (14,)

    def test_returns_float32(self):
        feats = extract_cycle_features({}, {})
        assert feats.dtype == np.float32

    def test_defaults_applied(self):
        feats = extract_cycle_features({}, {})
        assert feats[3] == pytest.approx(0.0)   # session_elapsed_h
        assert feats[4] == pytest.approx(0.0)   # consecutive_errors
        assert feats[5] == pytest.approx(0.0)   # detection_risk low -> 0.0
        assert feats[6] == pytest.approx(999.0) # hours_since_last_detection
        assert feats[8] == pytest.approx(90.0)  # cycle_delay_min
        assert feats[9] == pytest.approx(180.0) # cycle_delay_max
        assert feats[10] == pytest.approx(6.0)  # action_delay_min
        assert feats[11] == pytest.approx(14.0) # action_delay_max
        assert feats[12] == pytest.approx(0.4)  # sandbagging

    def test_config_overrides(self):
        cfg = {
            "garden": "sakura",
            "cycle_delay_min": 120,
            "cycle_delay_max": 240,
            "action_delay_min": 10,
            "action_delay_max": 20,
            "sandbagging_avoid_best_chance": 0.7,
        }
        state = {
            "session_elapsed_h": 3.5,
            "consecutive_errors": 2,
            "detection_risk": "high",
            "hours_since_last_detection": 12.0,
            "harvest_rate_trend": 0.8,
            "cycles_in_session": 14,
        }
        feats = extract_cycle_features(cfg, state)
        assert feats[3] == pytest.approx(3.5)
        assert feats[4] == pytest.approx(2.0)
        assert feats[5] == pytest.approx(1.0)
        assert feats[6] == pytest.approx(12.0)
        assert feats[7] == pytest.approx(0.8)
        assert feats[8] == pytest.approx(120.0)
        assert feats[9] == pytest.approx(240.0)
        assert feats[10] == pytest.approx(10.0)
        assert feats[11] == pytest.approx(20.0)
        assert feats[12] == pytest.approx(0.7)
        assert feats[13] == pytest.approx(14.0)


class TestGardenEncode:
    def setup_method(self):
        _GARDEN_MAP.clear()

    def test_first_garden_encodes_to_zero(self):
        assert _garden_encode("sakura") == 0

    def test_second_garden_encodes_to_one(self):
        _garden_encode("sakura")
        assert _garden_encode("tulip") == 1

    def test_consistent_encoding(self):
        a = _garden_encode("garden_x")
        b = _garden_encode("garden_x")
        assert a == b

    def test_new_garden_gets_next_id(self):
        _GARDEN_MAP["a"] = 0
        _GARDEN_MAP["b"] = 1
        assert _garden_encode("c") == 2


class TestTodSinCos:
    def test_morning(self):
        s, c = _tod_sin("morning")
        # morning idx=0 -> sin(0)=0, cos(0)=1
        assert s == pytest.approx(0.0, abs=1e-6)
        assert c == pytest.approx(1.0, abs=1e-6)

    def test_afternoon(self):
        s, c = _tod_sin("afternoon")
        # idx=1 -> sin(pi/2)=1, cos(pi/2)=0
        assert s == pytest.approx(1.0, abs=1e-6)
        assert c == pytest.approx(0.0, abs=1e-6)

    def test_evening(self):
        s, c = _tod_sin("evening")
        # idx=2 -> sin(pi)=0, cos(pi)=-1
        assert s == pytest.approx(0.0, abs=1e-6)
        assert c == pytest.approx(-1.0, abs=1e-6)

    def test_night(self):
        s, c = _tod_sin("night")
        # idx=3 -> sin(3pi/2)=-1, cos(3pi/2)=0
        assert s == pytest.approx(-1.0, abs=1e-6)
        assert c == pytest.approx(0.0, abs=1e-6)

    def test_unknown_tod_defaults_to_morning(self):
        s, c = _tod_sin("unknown")
        s_m, c_m = _tod_sin("morning")
        assert s == pytest.approx(s_m)
        assert c == pytest.approx(c_m)

    def test_tod_cos_matches_tod_sin(self):
        """Verify sin²+cos²=1 for all valid tod values."""
        for tod in ["morning", "afternoon", "evening", "night"]:
            s, c = _tod_sin(tod)
            assert s ** 2 + c ** 2 == pytest.approx(1.0)


class TestDetectionRiskEncode:
    def test_low(self):
        assert _detection_risk_encode("low") == 0.0

    def test_medium(self):
        assert _detection_risk_encode("medium") == 0.5

    def test_high(self):
        assert _detection_risk_encode("high") == 1.0

    def test_unknown_defaults_to_zero(self):
        assert _detection_risk_encode("unknown") == 0.0

    def test_none_defaults_to_zero(self):
        assert _detection_risk_encode(None) == 0.0


class TestBuildSessionDataset:
    def test_returns_none_when_fewer_than_min_samples(self, tmp_path):
        vault = _make_vault(tmp_path, n_sessions=3)
        X, y_a, y_d = build_session_dataset(vault, min_samples=10)
        assert X is None
        assert y_a is None
        assert y_d is None

    def test_returns_none_when_no_sessions_dir(self, tmp_path):
        X, y_a, y_d = build_session_dataset(str(tmp_path), min_samples=1)
        assert X is None
        assert y_a is None
        assert y_d is None

    def test_returns_arrays_when_enough_sessions(self, tmp_path):
        vault = _make_vault(tmp_path, n_sessions=15)
        X, y_a, y_d = build_session_dataset(vault, min_samples=10)
        assert X is not None
        assert y_a is not None
        assert y_d is not None
        assert X.shape[1] == len(FEATURE_NAMES)
        assert X.shape[0] >= 10
        assert X.dtype == np.float32
        assert y_a.dtype == np.int32
        assert y_d.dtype == np.float32

    def test_action_labels_clamped_1_to_10(self, tmp_path):
        vault = _make_vault(tmp_path, n_sessions=15)
        X, y_a, y_d = build_session_dataset(vault, min_samples=10)
        assert np.all(y_a >= 1)
        assert np.all(y_a <= 10)

    def test_delay_positive(self, tmp_path):
        vault = _make_vault(tmp_path, n_sessions=15)
        X, y_a, y_d = build_session_dataset(vault, min_samples=10)
        assert np.all(y_d > 0)


# ---------------------------------------------------------------------------
# models.py tests
# ---------------------------------------------------------------------------

class TestTrainActionForecaster:
    def test_returns_none_when_too_few_samples(self):
        X = np.random.randn(5, len(FEATURE_NAMES)).astype(np.float32)
        y = np.array([1, 2, 3, 4, 5], dtype=np.int32)
        model, scaler = train_action_forecaster(X, y)
        assert model is None
        assert scaler is None

    def test_returns_model_and_scaler_with_enough_data(self):
        X, y_a, _ = _synthetic_data(n=20)
        model, scaler = train_action_forecaster(X, y_a)
        assert model is not None
        assert scaler is not None
        pred = model.predict(scaler.transform(X[:3]))
        assert pred.shape == (3,)

    def test_persists_when_vault_path_set(self, tmp_path):
        X, y_a, _ = _synthetic_data(n=20)
        vault = str(tmp_path)
        train_action_forecaster(X, y_a, vault_path=vault)
        assert os.path.exists(os.path.join(vault, "ml", "action.joblib"))
        assert os.path.exists(os.path.join(vault, "ml", "action_scaler.joblib"))


class TestTrainDelayOptimizer:
    def test_returns_none_when_too_few_samples(self):
        X = np.random.randn(5, len(FEATURE_NAMES)).astype(np.float32)
        y = np.random.uniform(30, 3600, size=5).astype(np.float32)
        result = train_delay_optimizer(X, y)
        assert result == (None, None)

    def test_returns_model_and_scaler_with_enough_data(self):
        X, _, y_d = _synthetic_data(n=20)
        model, scaler = train_delay_optimizer(X, y_d)
        assert model is not None
        assert scaler is not None

    def test_persists_when_vault_path_set(self, tmp_path):
        X, _, y_d = _synthetic_data(n=20)
        vault = str(tmp_path)
        train_delay_optimizer(X, y_d, vault_path=vault)
        ml_dir = os.path.join(vault, "ml")
        assert os.path.exists(os.path.join(ml_dir, "delay.joblib"))
        assert os.path.exists(os.path.join(ml_dir, "delay_scaler.joblib"))
        assert os.path.exists(os.path.join(ml_dir, "delay_yscaler.joblib"))


class TestLoadActionForecaster:
    def test_returns_none_when_files_missing(self, tmp_path):
        m, s = load_action_forecaster(str(tmp_path))
        assert m is None
        assert s is None

    def test_loads_after_training(self, tmp_path):
        X, y_a, _ = _synthetic_data(n=20)
        vault = str(tmp_path)
        train_action_forecaster(X, y_a, vault_path=vault)
        m, s = load_action_forecaster(vault)
        assert m is not None
        assert s is not None
        preds = m.predict(s.transform(X[:5]))
        assert preds.shape == (5,)


class TestLoadDelayOptimizer:
    def test_returns_none_when_files_missing(self, tmp_path):
        result = load_delay_optimizer(str(tmp_path))
        assert result == (None, None, None)

    def test_loads_after_training(self, tmp_path):
        X, _, y_d = _synthetic_data(n=20)
        vault = str(tmp_path)
        train_delay_optimizer(X, y_d, vault_path=vault)
        m, s, ys = load_delay_optimizer(vault)
        assert m is not None
        assert s is not None
        assert ys is not None

    def test_y_scaler_none_when_missing(self, tmp_path):
        X, _, y_d = _synthetic_data(n=20)
        vault = str(tmp_path)
        train_delay_optimizer(X, y_d, vault_path=vault)
        os.remove(os.path.join(vault, "ml", "delay_yscaler.joblib"))
        m, s, ys = load_delay_optimizer(vault)
        assert m is not None
        assert ys is None


# ---------------------------------------------------------------------------
# anomaly.py tests
# ---------------------------------------------------------------------------

class TestAnomalyDetector:
    def _train_detector(self, vault_path, n=30):
        det = AnomalyDetector(vault_path)
        X, _, _ = _synthetic_data(n=n)
        det.ensure_trained(X)
        return det

    def test_ensure_trained_loads_model(self, tmp_path):
        det = self._train_detector(str(tmp_path))
        assert det._trained is True
        assert det._model is not None

    def test_ensure_trained_insufficient_data(self, tmp_path):
        det = AnomalyDetector(str(tmp_path))
        X = np.random.randn(5, len(FEATURE_NAMES)).astype(np.float32)
        det.ensure_trained(X)
        assert det._trained is False

    def test_ensure_trained_force_retrain(self, tmp_path):
        vault = str(tmp_path)
        det = self._train_detector(vault)
        old_model = det._model
        det2 = AnomalyDetector(vault)
        det2.ensure_trained()
        assert det2._trained is True
        X_new, _, _ = _synthetic_data(n=30)
        det2.ensure_trained(X_new, force=True)
        assert det2._model is not None

    def test_ensure_trained_loads_from_disk(self, tmp_path):
        vault = str(tmp_path)
        self._train_detector(vault)
        det2 = AnomalyDetector(vault)
        det2.ensure_trained()
        assert det2._trained is True
        assert det2._model is not None

    def test_score_returns_minus1_or_1(self, tmp_path):
        det = self._train_detector(str(tmp_path))
        cfg = {"garden": "test"}
        state = {"session_elapsed_h": 1.0, "cycles_in_session": 5}
        s = det.score(cfg, state)
        assert s in (-1, 1)

    def test_score_returns_none_when_untrained(self, tmp_path):
        det = AnomalyDetector(str(tmp_path))
        assert det.score({}, {}) is None

    def test_analyze_returns_bool_and_severity(self, tmp_path):
        det = self._train_detector(str(tmp_path))
        cfg = {"garden": "test"}
        state = {"session_elapsed_h": 1.0, "cycles_in_session": 5}
        is_anomaly, severity = det.analyze(cfg, state)
        assert isinstance(is_anomaly, bool)
        assert severity in ("normal", "low", "medium", "high", "unknown")

    def test_analyze_untrained_returns_false_unknown(self, tmp_path):
        det = AnomalyDetector(str(tmp_path))
        is_anomaly, severity = det.analyze({}, {})
        assert is_anomaly is False
        assert severity == "unknown"

    def test_available_property(self, tmp_path):
        det = AnomalyDetector(str(tmp_path))
        assert det.available is False
        self._train_detector(str(tmp_path))
        det2 = AnomalyDetector(str(tmp_path))
        det2.ensure_trained()
        assert det2.available is True


# ---------------------------------------------------------------------------
# inference.py tests
# ---------------------------------------------------------------------------

class TestMLEngine:
    def test_init_sets_vault_path(self, tmp_path):
        engine = MLEngine(str(tmp_path))
        assert engine.vault_path == str(tmp_path)

    def test_available_false_initially(self, tmp_path):
        engine = MLEngine(str(tmp_path))
        assert engine.available is False

    def test_available_true_after_training(self, tmp_path):
        vault = str(tmp_path)
        X, y_a, y_d = _synthetic_data(n=20)
        train_action_forecaster(X, y_a, vault_path=vault)
        train_delay_optimizer(X, y_d, vault_path=vault)
        engine = MLEngine(vault)
        engine.ensure_trained({})
        assert engine.available is True

    def test_ensure_trained_loads_models(self, tmp_path):
        vault = str(tmp_path)
        X, y_a, y_d = _synthetic_data(n=20)
        train_action_forecaster(X, y_a, vault_path=vault)
        train_delay_optimizer(X, y_d, vault_path=vault)
        engine = MLEngine(vault)
        engine.ensure_trained({})
        assert engine._action_model is not None
        assert engine._delay_model is not None

    def test_predict_max_actions_returns_int_or_none(self, tmp_path):
        vault = str(tmp_path)
        engine = MLEngine(vault)
        # no model loaded → None
        assert engine.predict_max_actions({}, {}) is None
        # train and predict
        X, y_a, y_d = _synthetic_data(n=20)
        train_action_forecaster(X, y_a, vault_path=vault)
        train_delay_optimizer(X, y_d, vault_path=vault)
        engine2 = MLEngine(vault)
        engine2.ensure_trained({})
        pred = engine2.predict_max_actions({"garden": "g"}, {})
        assert isinstance(pred, int)
        assert 0 <= pred <= 10

    def test_predict_delay_returns_float_or_none(self, tmp_path):
        vault = str(tmp_path)
        engine = MLEngine(vault)
        assert engine.predict_delay({}, {}) is None
        X, y_a, y_d = _synthetic_data(n=20)
        train_action_forecaster(X, y_a, vault_path=vault)
        train_delay_optimizer(X, y_d, vault_path=vault)
        engine2 = MLEngine(vault)
        engine2.ensure_trained({})
        pred = engine2.predict_delay({"garden": "g"}, {})
        assert isinstance(pred, float)
        assert 30.0 <= pred <= 3600.0

    def test_ensure_trained_with_vault_sessions(self, tmp_path):
        vault = _make_vault(tmp_path, n_sessions=20)
        engine = MLEngine(vault)
        engine.ensure_trained({"ml_anomaly_detection": False})
        assert engine.available is True
        pred = engine.predict_max_actions({"garden": "garden_a"}, {})
        assert pred is None or isinstance(pred, int)

    def test_request_retrain_flag(self, tmp_path):
        engine = MLEngine(str(tmp_path))
        assert engine._retrain_requested is False
        engine.request_retrain()
        assert engine._retrain_requested is True

    def test_predict_max_actions_clamp(self, tmp_path):
        vault = str(tmp_path)
        X, y_a, y_d = _synthetic_data(n=20)
        y_a[:] = 1
        train_action_forecaster(X, y_a, vault_path=vault)
        train_delay_optimizer(X, y_d, vault_path=vault)
        engine = MLEngine(vault)
        engine.ensure_trained({})
        for _ in range(5):
            pred = engine.predict_max_actions({"garden": "g"}, {})
            assert pred is not None
            assert 0 <= pred <= 10

    def test_predict_delay_clamp(self, tmp_path):
        vault = str(tmp_path)
        X, y_a, y_d = _synthetic_data(n=20)
        y_d[:] = 50000.0
        train_action_forecaster(X, y_a, vault_path=vault)
        train_delay_optimizer(X, y_d, vault_path=vault)
        engine = MLEngine(vault)
        engine.ensure_trained({})
        pred = engine.predict_delay({"garden": "g"}, {})
        assert pred is not None
        assert pred <= 3600.0
        y_d[:] = 1.0
        train_delay_optimizer(X, y_d, vault_path=vault)
        engine2 = MLEngine(vault)
        engine2.ensure_trained({})
        pred2 = engine2.predict_delay({"garden": "g"}, {})
        assert pred2 is not None
        assert pred2 >= 30.0
