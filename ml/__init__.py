"""ML module for UNCHAINED — feature extraction, models, inference, anomaly detection."""


def __getattr__(name):
    if name == 'extract_cycle_features':
        from ml.features import extract_cycle_features
        return extract_cycle_features
    if name == 'build_session_dataset':
        from ml.features import build_session_dataset
        return build_session_dataset
    if name == 'FEATURE_NAMES':
        from ml.features import FEATURE_NAMES
        return FEATURE_NAMES
    if name == 'train_action_forecaster':
        from ml.models import train_action_forecaster
        return train_action_forecaster
    if name == 'train_delay_optimizer':
        from ml.models import train_delay_optimizer
        return train_delay_optimizer
    if name == 'MLEngine':
        from ml.inference import MLEngine
        return MLEngine
    if name == 'AnomalyDetector':
        from ml.anomaly import AnomalyDetector
        return AnomalyDetector
    raise AttributeError(f"module 'ml' has no attribute {name!r}")


__all__ = ['extract_cycle_features', 'build_session_dataset', 'FEATURE_NAMES', 'train_action_forecaster', 'train_delay_optimizer', 'MLEngine', 'AnomalyDetector']
