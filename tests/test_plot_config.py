import json
import pytest
from plot_config import SeedConfig


@pytest.fixture
def cfg_file(tmp_path):
    return tmp_path / "seed_config.json"


# --- __init__ ---

class TestInit:
    def test_creates_empty_when_missing(self, cfg_file):
        assert not cfg_file.exists()
        sc = SeedConfig(path=str(cfg_file))
        assert sc._data == {}
        assert not cfg_file.exists()

    def test_loads_existing_file(self, cfg_file):
        seed_data = {"garden1": {"bed_a": ["tomato", "carrot"]}}
        cfg_file.write_text(json.dumps(seed_data))
        sc = SeedConfig(path=str(cfg_file))
        assert sc._data == seed_data

    def test_handles_corrupt_json(self, cfg_file):
        cfg_file.write_text("NOT VALID JSON {{{")
        sc = SeedConfig(path=str(cfg_file))
        assert sc._data == {}


# --- save ---

class TestSave:
    def test_writes_json(self, cfg_file):
        sc = SeedConfig(path=str(cfg_file))
        sc._data = {"g1": {"b1": ["seed1"]}}
        sc.save()
        with open(cfg_file) as f:
            assert json.load(f) == {"g1": {"b1": ["seed1"]}}

    def test_save_roundtrip(self, cfg_file):
        sc = SeedConfig(path=str(cfg_file))
        sc._data = {"garden_a": {"bed_1": ["a"], "bed_2": ["b", "c"]}}
        sc.save()
        reloaded = SeedConfig(path=str(cfg_file))
        assert reloaded._data == sc._data


# --- get_allowed ---

class TestGetAllowed:
    def test_returns_seeds(self, cfg_file):
        cfg_file.write_text(json.dumps({"g": {"b": ["s1", "s2"]}}))
        sc = SeedConfig(path=str(cfg_file))
        assert sc.get_allowed("g", "b") == ["s1", "s2"]

    def test_returns_none_when_not_set(self, cfg_file):
        sc = SeedConfig(path=str(cfg_file))
        assert sc.get_allowed("no_garden", "no_bed") is None

    def test_returns_none_for_unknown_bed(self, cfg_file):
        cfg_file.write_text(json.dumps({"g": {"existing": []}}))
        sc = SeedConfig(path=str(cfg_file))
        assert sc.get_allowed("g", "missing_bed") is None


# --- set_allowed ---

class TestSetAllowed:
    def test_sets_and_persists(self, cfg_file):
        sc = SeedConfig(path=str(cfg_file))
        sc.set_allowed("g1", "b1", ["seed_a", "seed_b"])
        assert sc.get_allowed("g1", "b1") == ["seed_a", "seed_b"]

        reloaded = SeedConfig(path=str(cfg_file))
        assert reloaded.get_allowed("g1", "b1") == ["seed_a", "seed_b"]

    def test_overwrites_previous(self, cfg_file):
        sc = SeedConfig(path=str(cfg_file))
        sc.set_allowed("g", "b", ["old"])
        sc.set_allowed("g", "b", ["new"])
        assert sc.get_allowed("g", "b") == ["new"]

    def test_multiple_gardens_beds(self, cfg_file):
        sc = SeedConfig(path=str(cfg_file))
        sc.set_allowed("g1", "b1", ["s1"])
        sc.set_allowed("g1", "b2", ["s2"])
        sc.set_allowed("g2", "b1", ["s3"])
        assert sc.get_allowed("g1", "b1") == ["s1"]
        assert sc.get_allowed("g1", "b2") == ["s2"]
        assert sc.get_allowed("g2", "b1") == ["s3"]

    def test_converts_iterable_to_list(self, cfg_file):
        sc = SeedConfig(path=str(cfg_file))
        sc.set_allowed("g", "b", {"x", "y"})
        assert isinstance(sc.get_allowed("g", "b"), list)


# --- get_all ---

class TestGetAll:
    def test_returns_full_dict(self, cfg_file):
        data = {"g1": {"b1": ["s1"]}, "g2": {"b2": ["s2"]}}
        cfg_file.write_text(json.dumps(data))
        sc = SeedConfig(path=str(cfg_file))
        assert sc.get_all() == data

    def test_returns_empty_dict_when_nothing_set(self, cfg_file):
        sc = SeedConfig(path=str(cfg_file))
        assert sc.get_all() == {}
