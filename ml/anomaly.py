"""Anomaly detection — flag unusual cycle results using IsolationForest."""

import os
import logging
import joblib

import numpy as np
from sklearn.ensemble import IsolationForest

from memory import VAULT_PATH
from ml.features import extract_cycle_features, FEATURE_NAMES

logger = logging.getLogger('unchained.ml.anomaly')

MODEL_NAME = 'anomaly'


def _model_path(vault_path):
    return os.path.join(vault_path, 'ml', f'{MODEL_NAME}.joblib')


class AnomalyDetector:
    """Detects anomalous bot cycles — potential early warning for shadowbans.

    Stores an IsolationForest model that learns normal cycle feature
    distributions and flags outliers.

    Usage:
        ad = AnomalyDetector(vault_path)
        ad.ensure_trained(X)          # fit on historical data
        score = ad.score(config, session_state)  # -1 = anomaly, 1 = normal
        is_anomaly, severity = ad.analyze(config, session_state)
    """

    def __init__(self, vault_path=None):
        self.vault_path = vault_path or VAULT_PATH
        self._model = None
        self._trained = False
        self._mean_feat = None
        self._std_feat = None

    @property
    def available(self):
        return self._trained

    def ensure_trained(self, X=None, force=False):
        """Load or train the anomaly model.

        Args:
            X: feature matrix for training (optional if model file exists)
            force: retrain even if model exists
        """
        if self._trained and not force:
            return

        path = _model_path(self.vault_path)
        if os.path.exists(path) and not force:
            try:
                data = joblib.load(path)
                if isinstance(data, dict):
                    self._model = data.get('model')
                    self._mean_feat = data.get('mean')
                    self._std_feat = data.get('std')
                else:
                    self._model = data
                self._trained = True
                logger.info('AnomalyDetector: loaded existing model')
                return
            except Exception as e:
                logger.warning(f'AnomalyDetector: failed to load: {e}')

        if X is None or X.shape[0] < 10:
            return

        self._mean_feat = X.mean(axis=0)
        self._std_feat = X.std(axis=0) + 1e-08
        X_norm = (X - self._mean_feat) / self._std_feat

        self._model = IsolationForest(
            n_estimators=100,
            max_samples=min(256, X.shape[0]),
            contamination=0.1,
            random_state=42,
            n_jobs=1,
        )

        self._model.fit(X_norm)
        self._trained = True

        os.makedirs(os.path.join(self.vault_path, 'ml'), exist_ok=True)
        joblib.dump({
            'model': self._model,
            'mean': self._mean_feat,
            'std': self._std_feat,
        }, path)

        logger.info(f'AnomalyDetector: trained on {X.shape[0]} samples, saved to {path}')

    def score(self, config, session_state):
        """Return anomaly score: -1 = anomaly, 1 = normal, or None if unavailable."""
        if not self._trained or self._model is None:
            return None
        try:
            self._last_feats = extract_cycle_features(config, session_state)
            feats = self._last_feats
            if self._mean_feat is not None and self._std_feat is not None:
                feats = (feats - self._mean_feat) / self._std_feat
                self._last_feats_norm = feats
            else:
                self._last_feats_norm = feats
            score = self._model.predict(feats.reshape(1, -1))[0]
            return int(score)
        except Exception as e:
            logger.warning(f'Anomaly score failed: {e}')
            return None

    def analyze(self, config, session_state):
        """Return (is_anomaly: bool, severity: str).

        severity is one of 'normal', 'low', 'medium', 'high', 'unknown'.
        """
        score = self.score(config, session_state)
        if score is None:
            return False, 'unknown'

        if score == -1:
            try:
                feats_norm = getattr(self, '_last_feats_norm', None)
                if feats_norm is None:
                    return True, 'medium'
                decision = self._model.decision_function(feats_norm.reshape(1, -1))[0]

                if decision < -0.3:
                    return True, 'high'
                if decision < -0.1:
                    return True, 'medium'
                return True, 'low'
            except Exception:
                return True, 'medium'

        return False, 'normal'
