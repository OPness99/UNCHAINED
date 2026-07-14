"""Inference engine — cached model predictors with auto-refit."""

import os
import logging

import numpy as np

from memory import VAULT_PATH
from ml.features import extract_cycle_features, build_session_dataset
from ml.models import (
    train_action_forecaster, train_delay_optimizer,
    load_action_forecaster, load_delay_optimizer,
)
from ml.anomaly import AnomalyDetector

logger = logging.getLogger('unchained.ml.inference')


class MLEngine:
    """Singleton-like engine holding loaded models and providing predictions.

    Usage:
        engine = MLEngine(vault_path)
        engine.ensure_trained(config)          # auto-train if stale
        max_actions = engine.predict_actions(config, session_state)
        delay = engine.predict_delay(config, session_state)
    """

    def __init__(self, vault_path=None):
        self.vault_path = vault_path or VAULT_PATH
        self._action_model = None
        self._action_scaler = None
        self._delay_model = None
        self._delay_scaler = None
        self._delay_yscaler = None
        self._trained_samples = 0
        self._retrain_requested = False
        self._anomaly = AnomalyDetector(self.vault_path)

    @property
    def available(self):
        return self._action_model is not None

    def ensure_trained(self, config):
        """Load models from disk, train if missing or stale."""
        # Try loading action model
        if self._action_model is None:
            model, scaler = load_action_forecaster(self.vault_path)
            if model is not None:
                self._action_model = model
                self._action_scaler = scaler
                logger.info('MLEngine: loaded existing action model')

        # Try loading delay model
        if self._delay_model is None:
            model, scaler, yscaler = load_delay_optimizer(self.vault_path)
            if model is not None:
                self._delay_model = model
                self._delay_scaler = scaler
                self._delay_yscaler = yscaler
                logger.info('MLEngine: loaded existing delay model')

        # Train anomaly detector if enabled and not yet available
        if config.get('ml_anomaly_detection', True) and not self._anomaly.available:
            X, _, _ = build_session_dataset(self.vault_path)
            self._anomaly.ensure_trained(X)

        # Retrain if no model or retrain requested
        if self._action_model is None or self._retrain_requested:
            X, y_action, y_delay = build_session_dataset(self.vault_path)
            if X is not None:
                n = X.shape[0]
                if n >= config.get('ml_min_training_samples', 10) and n > self._trained_samples:
                    self._train(X, y_action, y_delay)
                    self._retrain_requested = False
                    if config.get('ml_anomaly_detection', True):
                        self._anomaly.ensure_trained(X, force=True)

    def _train(self, X, y_action, y_delay):
        """Train both models and reload them."""
        train_action_forecaster(X, y_action, self.vault_path)
        train_delay_optimizer(X, y_delay, self.vault_path)

        am, asc = load_action_forecaster(self.vault_path)
        if am is not None:
            self._action_model = am
            self._action_scaler = asc

        dm, dsc, dysc = load_delay_optimizer(self.vault_path)
        if dm is not None:
            self._delay_model = dm
            self._delay_scaler = dsc
            self._delay_yscaler = dysc

        self._trained_samples = X.shape[0]
        logger.info(f'MLEngine: trained on {self._trained_samples} samples')

    def request_retrain(self):
        self._retrain_requested = True

    def predict_max_actions(self, config, session_state):
        """Predict the optimal max_actions for the current cycle.

        Returns:
            int in [0, 10] or None if model unavailable
        """
        if self._action_model is None:
            return None
        try:
            feats = extract_cycle_features(config, session_state)
            feats_scaled = self._action_scaler.transform(feats.reshape(1, -1))
            pred = self._action_model.predict(feats_scaled)[0]
            return int(np.clip(pred, 0, 10))
        except Exception as e:
            logger.warning(f'Action prediction failed: {e}')
            return None

    def predict_delay(self, config, session_state):
        """Predict the optimal cycle delay in seconds.

        Returns:
            float in [30, 3600] or None if model unavailable
        """
        if self._delay_model is None:
            return None
        try:
            feats = extract_cycle_features(config, session_state)
            feats_scaled = self._delay_scaler.transform(feats.reshape(1, -1))
            pred_scaled = self._delay_model.predict(feats_scaled)[0]

            if self._delay_yscaler is not None:
                pred = self._delay_yscaler.inverse_transform([[pred_scaled]])[0][0]
            else:
                pred = pred_scaled

            return float(np.clip(pred, 30, 3600))
        except Exception as e:
            logger.warning(f'Delay prediction failed: {e}')
            return None
