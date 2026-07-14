"""Model definitions — lightweight sklearn models for action/delay prediction."""

import os
import logging
import joblib

import numpy as np
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger('unchained.ml.models')

MODEL_DIR = 'ml'


def _model_path(vault_path, name):
    return os.path.join(vault_path, MODEL_DIR, f'{name}.joblib')


def _scaler_path(vault_path, name):
    return os.path.join(vault_path, MODEL_DIR, f'{name}_scaler.joblib')


def train_action_forecaster(X, y, vault_path=None):
    """Train a RandomForestClassifier to predict optimal max_actions.

    Args:
        X: feature matrix (n_samples, n_features)
        y: target labels (n_samples,) — best max_actions per cycle
        vault_path: if set, persist model + scaler to vault

    Returns:
        (model, scaler) or (None, None) if insufficient data
    """
    if X.shape[0] < 10:
        logger.warning(f'Action forecaster: need >=10 samples, got {X.shape[0]}')
        return None, None

    classes = np.unique(y)
    n_classes = len(classes)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    model = RandomForestClassifier(
        n_estimators=max(50, min(200, X.shape[0] * 5)),
        max_depth=max(3, min(10, X.shape[0] // 5)),
        min_samples_leaf=max(1, X.shape[0] // 20),
        random_state=42,
        class_weight='balanced' if n_classes > 1 else None,
        n_jobs=1,
    )

    model.fit(X_scaled, y)

    score = model.score(X_scaled, y)
    logger.info(f'Action forecaster trained: {X.shape[0]} samples, {n_classes} classes, R²={score:.3f}')

    if vault_path:
        os.makedirs(os.path.join(vault_path, MODEL_DIR), exist_ok=True)
        joblib.dump(model, _model_path(vault_path, 'action'))
        joblib.dump(scaler, _scaler_path(vault_path, 'action'))
        logger.info(f'Action model saved to {_model_path(vault_path, "action")}')

    return model, scaler


def train_delay_optimizer(X, y, vault_path=None):
    """Train a RandomForestRegressor to predict optimal cycle delay.

    Args:
        X: feature matrix (n_samples, n_features)
        y: target delays in seconds (n_samples,)
        vault_path: if set, persist model + scaler to vault

    Returns:
        (model, scaler) or (None, None) if insufficient data
    """
    if X.shape[0] < 10:
        logger.warning(f'Delay optimizer: need >=10 samples, got {X.shape[0]}')
        return None, None

    y_clipped = np.clip(y, 10, 3600)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    y_scaler = StandardScaler()
    y_scaled = y_scaler.fit_transform(y_clipped.reshape(-1, 1)).ravel()

    model = RandomForestRegressor(
        n_estimators=max(50, min(200, X.shape[0] * 5)),
        max_depth=max(3, min(10, X.shape[0] // 5)),
        min_samples_leaf=max(1, X.shape[0] // 20),
        random_state=42,
        n_jobs=1,
    )

    model.fit(X_scaled, y_scaled)

    # Compute R² on training set
    y_pred_scaled = model.predict(X_scaled)
    y_pred = y_scaler.inverse_transform(y_pred_scaled.reshape(-1, 1)).ravel()
    ss_res = np.sum((y_clipped - y_pred) ** 2)
    ss_tot = np.sum((y_clipped - np.mean(y_clipped)) ** 2)
    r2 = 1 - ss_res / max(ss_tot, 1e-10)
    logger.info(f'Delay optimizer trained: {X.shape[0]} samples, R²={r2:.3f}')

    if vault_path:
        os.makedirs(os.path.join(vault_path, MODEL_DIR), exist_ok=True)
        joblib.dump(model, _model_path(vault_path, 'delay'))
        joblib.dump(scaler, _scaler_path(vault_path, 'delay'))
        joblib.dump(y_scaler, _model_path(vault_path, 'delay_yscaler'))
        logger.info(f'Delay model saved to {_model_path(vault_path, "delay")}')

    return model, scaler


def load_action_forecaster(vault_path):
    """Load persisted action model + scaler from vault."""
    model_path = _model_path(vault_path, 'action')
    scaler_path = _scaler_path(vault_path, 'action')
    if not os.path.exists(model_path) or not os.path.exists(scaler_path):
        return None, None
    try:
        model = joblib.load(model_path)
        scaler = joblib.load(scaler_path)
        return model, scaler
    except Exception as e:
        logger.warning(f'Failed to load action model: {e}')
        return None, None


def load_delay_optimizer(vault_path):
    """Load persisted delay model + scaler (+ optional y_scaler) from vault."""
    model_path = _model_path(vault_path, 'delay')
    scaler_path = _scaler_path(vault_path, 'delay')
    yscaler_path = _model_path(vault_path, 'delay_yscaler')
    if not os.path.exists(model_path) or not os.path.exists(scaler_path):
        return None, None, None
    try:
        model = joblib.load(model_path)
        scaler = joblib.load(scaler_path)
        y_scaler = joblib.load(yscaler_path) if os.path.exists(yscaler_path) else None
        return model, scaler, y_scaler
    except Exception as e:
        logger.warning(f'Failed to load delay model: {e}')
        return None, None, None
