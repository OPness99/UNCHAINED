"""Feature extraction — converts vault sessions + live state into ML feature vectors."""

import os
import logging
from datetime import datetime

import numpy as np

from memory import find_notes, read_note, _tod_from_hour, VAULT_PATH

logger = logging.getLogger('unchained.ml.features')


FEATURE_NAMES = [
    'garden_encoded', 'tod_sin', 'tod_cos', 'session_elapsed_h',
    'consecutive_errors', 'detection_risk_encoded',
    'hours_since_last_detection', 'harvest_rate_trend',
    'cycle_delay_min', 'cycle_delay_max',
    'action_delay_min', 'action_delay_max',
    'sandbagging_chance', 'cycles_in_session',
]

# Lazy-initialized garden encoder
_GARDEN_SET = set()
_GARDEN_MAP = {}


def _garden_encode(garden):
    if garden not in _GARDEN_MAP:
        _GARDEN_MAP[garden] = len(_GARDEN_MAP)
    return _GARDEN_MAP[garden]


def _tod_sin(tod):
    m = {'morning': 0, 'afternoon': 1, 'evening': 2, 'night': 3}
    idx = m.get(tod, 0)
    return np.sin(2 * np.pi * idx / 4), np.cos(2 * np.pi * idx / 4)


def _detection_risk_encode(risk):
    return {'low': 0.0, 'medium': 0.5, 'high': 1.0}.get(risk, 0.0)


def extract_cycle_features(config, session_state):
    """Extract a fixed-length feature vector from the current config + session state.

    Returns:
        np.ndarray of shape (len(FEATURE_NAMES),) with dtype float32
    """
    tod = _tod_from_hour(datetime.now().hour)
    tod_s, tod_c = _tod_sin(tod)

    garden = config.get('garden', 'unknown')
    garden_enc = _garden_encode(garden)

    return np.array([
        float(garden_enc),
        tod_s,
        tod_c,
        float(session_state.get('session_elapsed_h', 0)),
        float(session_state.get('consecutive_errors', 0)),
        _detection_risk_encode(session_state.get('detection_risk', 'low')),
        float(session_state.get('hours_since_last_detection', 999)),
        float(session_state.get('harvest_rate_trend', 0)),
        float(config.get('cycle_delay_min', 90)),
        float(config.get('cycle_delay_max', 180)),
        float(config.get('action_delay_min', 6)),
        float(config.get('action_delay_max', 14)),
        float(config.get('sandbagging_avoid_best_chance', 0.4)),
        float(session_state.get('cycles_in_session', 0)),
    ], dtype=np.float32)


def build_session_dataset(vault_path=None, min_samples=10):
    """Build feature matrix + targets from session history in the vault.

    Args:
        vault_path: path to Obsidian vault (default: VAULT_PATH)
        min_samples: minimum sessions required

    Returns:
        (X, y_action, y_delay) as numpy arrays, or (None, None, None) if insufficient data
    """
    vault = vault_path or VAULT_PATH
    sessions_dir = os.path.join(vault, 'sessions')
    sessions = find_notes(sessions_dir, 'session')

    if len(sessions) < min_samples:
        logger.debug(f'Feature build: need {min_samples} sessions, have {len(sessions)}')
        return None, None, None

    detection_dir = os.path.join(vault, 'detection')
    all_detections = find_notes(detection_dir, 'detection')
    detection_times = []
    for _, dfm, _ in all_detections:
        dt_str = dfm.get('timestamp', '')
        try:
            detection_times.append(datetime.strptime(dt_str.split('.')[0].split('+')[0], '%Y-%m-%dT%H:%M:%S'))
        except Exception:
            continue

    X_rows = []
    y_action = []
    y_delay = []

    for _, fm, _ in sessions:
        ts_str = fm.get('timestamp', '')
        try:
            ts = datetime.strptime(ts_str.split('.')[0].split('+')[0], '%Y-%m-%dT%H:%M:%S')
            tod = _tod_from_hour(ts.hour)
        except Exception:
            continue

        garden = fm.get('garden', 'unknown')
        cfg = fm.get('config', {})

        tod_s, tod_c = _tod_sin(tod)
        garden_enc = _garden_encode(garden)
        detection_risk = fm.get('detection_risk', 'low')

        duration_h = fm.get('duration_h', 1)
        cycles = fm.get('cycles', 1)
        harvested = fm.get('harvested', 0)
        errors = fm.get('errors', 0)

        # Best action label — avoid feedback loop where low max_actions → low harvests → label=1
        # Use session's max_actions as floor so the model doesn't learn to always predict 1
        session_max = cfg.get('max_actions', 0)
        h_per_cycle = harvested / max(cycles, 1)
        if session_max > 0 and h_per_cycle >= session_max * 0.7:
            # Bot used most of its capacity — suggest trying more
            best_action = min(session_max + 1, 10)
        else:
            best_action = min(int(round(h_per_cycle + 1)), 10)
        # Never label below 1
        best_action = max(best_action, 1)

        # Average delay in seconds
        avg_delay = (duration_h * 3600) / max(cycles, 1)

        # Find hours since last detection
        hours_since_detection = 999.0
        for dt in detection_times:
            diff_h = abs((ts - dt).total_seconds()) / 3600
            if diff_h < hours_since_detection:
                hours_since_detection = diff_h

        # Harvest rate (harvests per hour)
        harvest_rate = harvested / max(duration_h, 0.1)

        row = [
            float(garden_enc),
            tod_s,
            tod_c,
            float(duration_h),
            float(errors),
            _detection_risk_encode(detection_risk),
            min(hours_since_detection, 999.0),
            min(harvest_rate, 50.0),
            float(cfg.get('cycle_delay_min', cfg.get('cycle_min', 90))),
            float(cfg.get('cycle_delay_max', cfg.get('cycle_max', 180))),
            float(cfg.get('action_delay_min', cfg.get('action_min', 6))),
            float(cfg.get('action_delay_max', cfg.get('action_max', 14))),
            float(cfg.get('sandbagging_avoid_best_chance', cfg.get('sandbagging_chance', 0.4))),
            float(cycles),
        ]

        X_rows.append(row)
        y_action.append(best_action)
        y_delay.append(avg_delay)

    if len(X_rows) < min_samples:
        return None, None, None

    X = np.array(X_rows, dtype=np.float32)
    y_action = np.array(y_action, dtype=np.int32)
    y_delay = np.array(y_delay, dtype=np.float32)

    logger.info(f'Feature build: {len(X_rows)} samples, {len(FEATURE_NAMES)} features')
    return X, y_action, y_delay
