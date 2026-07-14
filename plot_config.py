"""Seed configuration per garden bed type."""

import json
import os
import sys


def _data_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.getcwd()


CONFIG_FILE = os.path.join(_data_dir(), 'seed_config.json')


class SeedConfig:
    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self._data = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}

    def save(self):
        with open(self.path, 'w') as f:
            json.dump(self._data, f, indent=2)

    def get_allowed(self, garden_code, bed_code):
        return self._data.get(garden_code, {}).get(bed_code)

    def set_allowed(self, garden_code, bed_code, seed_codes):
        self._data.setdefault(garden_code, {})[bed_code] = list(seed_codes)
        self.save()

    def get_all(self):
        return self._data

    def set_allowed_batch(self, mapping):
        for gc, bed_types in mapping.items():
            for bc, seeds in bed_types.items():
                self._data.setdefault(gc, {})[bc] = list(seeds)
        self.save()

    def is_use_limited_seeds(self):
        return self._data.get('_settings', {}).get('use_limited_seeds', True)

    def set_use_limited_seeds(self, enabled):
        self._data.setdefault('_settings', {})['use_limited_seeds'] = enabled
        self.save()

    def get_limited_threshold(self):
        return self._data.get('_settings', {}).get('limited_threshold', 5)

    def set_limited_threshold(self, threshold):
        self._data.setdefault('_settings', {})['limited_threshold'] = max(1, int(threshold))
        self.save()
