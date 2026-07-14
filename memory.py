"""
Memory module â€” Obsidian vault as persistent brain for UNCHAINED.
Reads/writes structured notes, optimizes config from learned patterns.
"""

import os
import re
import sys
import time
import logging
from datetime import datetime

import yaml
import numpy as np

logger = logging.getLogger("unchained.memory")

FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n\s*---\s*\n?(.*)", re.DOTALL | re.MULTILINE
)


def _data_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.getcwd()


VAULT_PATH = os.path.join(_data_dir(), "UNCHAINED")


def _tod_from_hour(h):
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 22:
        return "evening"
    return "night"


def read_note(path):
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return None, None
    try:
        m = FRONTMATTER_RE.match(content)
        if not m:
            return {}, content
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            fm = {}
        return fm, (m.group(2) or "").strip()
    except Exception:
        return None, None


def write_note(path, frontmatter, body=""):
    path = str(path)
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    fm_str = yaml.dump(
        frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False
    ).strip()
    content = f"---\n{fm_str}\n---\n\n{body.strip()}\n"
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def find_notes(directory, note_type=None):
    notes = []
    if not os.path.isdir(directory):
        return notes
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".md") or fname == "_template.md":
            continue
        path = os.path.join(directory, fname)
        fm, _ = read_note(path)
        if fm is None:
            continue
        if note_type is not None and fm.get("type") != note_type:
            continue
        notes.append((fname, fm, path))
    return notes


def now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class BotMemory:
    """Manages the Obsidian vault â€” persists sessions, seeds, gardens,
    detection events, and config snapshots. Optimizes config from profiles."""

    def __init__(self, vault_path=None):
        self.vault_path = vault_path or VAULT_PATH
        self._session_start = None
        self._garden = None
        self._buffer = self._fresh_buffer()
        self._ml_engine = None

    def set_ml_engine(self, engine):
        self._ml_engine = engine

    def optimize_config(self, config):
        """Read vault, find best profile for now, tweak config in-place."""
        tod = _tod_from_hour(datetime.now().hour)
        garden = config.get("garden")
        profiles = find_notes(os.path.join(self.vault_path, "profiles"), "profile")

        best = None
        best_score = 0.0
        for _, fm, _ in profiles:
            score = 0.0
            if fm.get("garden") == garden:
                score += 2.0
            if fm.get("time_of_day") == tod:
                score += 1.0
            score += {"low": 0, "medium": 0.5, "high": 1.0}.get(
                fm.get("confidence"), 0
            )
            score += min(fm.get("samples", 0) or 0, 10.0) / 10.0 + 1.0
            if score > best_score:
                best_score = score
                best = fm

        if best and best_score >= 1.5:
            key_map = {
                "cycle_min": "cycle_delay_min",
                "cycle_max": "cycle_delay_max",
                "sandbagging_chance": "sandbagging_avoid_best_chance",
                "action_min": "action_delay_min",
                "action_max": "action_delay_max",
            }
            changes = {}
            for profile_key, config_key in key_map.items():
                val = best.get(profile_key)
                if val is None:
                    continue
                if config.get(config_key) == val:
                    continue
                old = config.get(config_key)
                config[config_key] = val
                changes[config_key] = f"{old} -> {val}"
            if changes:
                logger.info(
                    f"Memory: optimized from profile '{best.get('garden')}/{best.get('time_of_day')}'"
                )
                for k, v in changes.items():
                    logger.info(f"  {k}: {v}")
                self._write_snapshot("profile_optimization", changes)

            return config
        else:
            logger.info(
                f"Memory: no suitable profile found (score={best_score:.1f}), using defaults"
            )
            return config

    def start_session(self, config, garden=None):
        self._session_start = time.time()
        self._garden = garden or config.get("garden", "unknown")
        self._buffer = self._fresh_buffer()
        logger.info(f"Memory: session started â€” garden={self._garden}")

    def log_cycle(self, results):
        if self._session_start is None:
            return
        self._buffer["cycles"] += 1
        self._buffer["harvested"] += len(results.get("harvested", []))
        self._buffer["planted"] += len(results.get("planted", []))
        self._buffer["errors"] += len(results.get("errors", []))
        for bid in results.get("harvested", []):
            self._buffer["harvest_details"].append({"bed": bid, "time": time.time()})
        for bid in results.get("planted", []):
            self._buffer["plant_details"].append({"bed": bid, "time": time.time()})
        for err in results.get("errors", []):
            self._buffer["error_details"].append({"error": err, "time": time.time()})

    def log_plant(self, seed_code, bed_code, garden_code=None):
        self._update_seed(seed_code, {"planted": True, "bed": bed_code})

    def log_harvest(self, seed_code):
        self._update_seed(seed_code, {"harvested": True})

    def log_plant_batch(self, plant_events):
        self._batch_update_seeds([
            (s, {"planted": True, "bed": b}) for s, b in plant_events
        ])

    def log_harvest_batch(self, harvest_events):
        self._batch_update_seeds([
            (s, {"harvested": True}) for s in harvest_events
        ])

    def _batch_update_seeds(self, events):
        merged = {}
        for seed_code, event in events:
            if seed_code not in merged:
                merged[seed_code] = {}
            merged[seed_code].update(event)
        for seed_code, combined_event in merged.items():
            self._update_seed(seed_code, combined_event)

    def log_detection(self, severity, symptom, config, details=None):
        now = now_iso()
        fname = f"{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.md"
        path = os.path.join(self.vault_path, "detection", fname)

        adjusted = {}
        if severity in ("shadowban", "flag"):
            for param, factor in (
                ("cycle_delay_min", 2.0),
                ("cycle_delay_max", 1.5),
                ("action_delay_min", 1.5),
                ("action_delay_max", 1.5),
            ):
                old = config.get(param, 60)
                new_val = int(old * factor)
                config[param] = new_val
                adjusted[param] = f"{old} -> {new_val}"
            old_sb = config.get("sandbagging_avoid_best_chance", 0.4)
            config["sandbagging_avoid_best_chance"] = min(old_sb * 1.5, 1.0)
            adjusted["sandbagging_avoid_best_chance"] = f"{old_sb:.2f} -> {config['sandbagging_avoid_best_chance']:.2f}"
            config["offline_duration_hours"] = max(
                config.get("offline_duration_hours", 24), 6
            )

        fm = {
            "type": "detection",
            "timestamp": now,
            "severity": severity,
            "symptom": symptom,
            "response": "Auto-adjusted: slowed delays" if adjusted else "Logged only",
            "config_adjusted": adjusted,
            "buffer_snapshot": {k: self._buffer[k] for k in ("cycles", "harvested", "planted", "errors")},
        }
        if details:
            if isinstance(details, dict):
                fm["details"] = dict(details)
            else:
                fm["details"] = str(details)
        write_note(path, fm)
        logger.warning(f"Memory: detection â€” {severity}: {str(symptom)[:100]}")

        if adjusted:
            self._write_snapshot(f"detection_{severity}", adjusted)
            from config import save_config
            save_config(config)
        return adjusted

    def finalize_session(self, config):
        if self._session_start is None:
            return
        elapsed = time.time() - self._session_start
        fname = f"{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.md"
        path = os.path.join(self.vault_path, "sessions", fname)

        fm = {
            "type": "session",
            "timestamp": now_iso(),
            "duration_h": round(elapsed / 3600, 2),
            "cycles": self._buffer["cycles"],
            "harvested": self._buffer["harvested"],
            "planted": self._buffer["planted"],
            "errors": self._buffer["errors"],
            "yield_gold": None,
            "yield_items": None,
            "garden": self._garden,
            "offline": False,
            "detection_risk": self._detection_risk(),
            "config": {
                "cycle_min": config.get("cycle_delay_min"),
                "cycle_max": config.get("cycle_delay_max"),
                "cooldown": config.get("cooldown_hours"),
                "max_actions": config.get("max_actions_per_cycle", 0),
                "sandbagging": config.get("sandbagging_enabled"),
                "sandbagging_chance": config.get("sandbagging_avoid_best_chance"),
                "offline_prob": config.get("offline_base_probability"),
            },
        }
        write_note(path, fm)
        self._update_garden(config)
        logger.info(
            f"Memory: session saved â€” {fm['cycles']}c {fm['harvested']}h {fm['planted']}p {fm['errors']}e"
        )
        self._session_start = None
        self._garden = None

    def generate_profiles(self):
        sessions = find_notes(os.path.join(self.vault_path, "sessions"), "session")
        if len(sessions) < 2:
            logger.info("Memory: need >=2 sessions to generate profiles")
            return None

        groups = {}
        for _, fm, _ in sessions:
            ts_str = fm.get("timestamp", "")
            try:
                ts = datetime.strptime(
                    ts_str.split(".")[0].split("+")[0], "%Y-%m-%dT%H:%M:%S"
                )
                tod = _tod_from_hour(ts.hour)
            except Exception:
                continue
            garden = fm.get("garden", "unknown")
            groups.setdefault(f"{garden}|{tod}", []).append(fm)

        profiles_dir = os.path.join(self.vault_path, "profiles")
        count = 0
        for key, sess_list in groups.items():
            if len(sess_list) < 2:
                continue
            garden, tod = key.split("|", 1)
            cycle_mins = [
                s.get("config", {}).get("cycle_min")
                for s in sess_list
                if s.get("config")
            ]
            cycle_mins = [c for c in cycle_mins if c]
            cycle_maxs = [
                s.get("config", {}).get("cycle_max")
                for s in sess_list
                if s.get("config")
            ]
            cycle_maxs = [c for c in cycle_maxs if c]
            harvest_rates = [
                s.get("harvested", 0) / max(s.get("duration_h", 1), 0.1)
                for s in sess_list
            ]

            if not cycle_mins:
                continue

            avg_min = sum(cycle_mins) / len(cycle_mins)
            avg_max = (
                sum(cycle_maxs) / len(cycle_maxs) if cycle_maxs else avg_min * 2
            )
            avg_rate = sum(harvest_rates) / len(harvest_rates)
            if len(sess_list) >= 10:
                conf = "high"
            elif len(sess_list) >= 4:
                conf = "medium"
            else:
                conf = "low"

            profile = {
                "type": "profile",
                "garden": garden,
                "time_of_day": tod,
                "cycle_min": int(avg_min),
                "cycle_max": int(avg_max),
                "sandbagging_chance": 0.4,
                "session_len_h": 4,
                "action_min": 4,
                "action_max": 10,
                "harvest_rate_h": round(avg_rate, 2),
                "confidence": conf,
                "samples": len(sess_list),
            }
            write_note(
                os.path.join(profiles_dir, f"{garden}-{tod}.md"), profile
            )
            count += 1

        logger.info(f"Memory: generated {count} profile(s)")

        if self._ml_engine is not None:
            try:
                self._ml_engine.ensure_trained({})
            except Exception as exc:
                logger.warning(f"Memory: ML training failed: {exc}")

        return count

    def summary(self):
        sessions = find_notes(os.path.join(self.vault_path, "sessions"), "session")
        seeds = find_notes(os.path.join(self.vault_path, "seeds"), "seed")
        gardens = find_notes(os.path.join(self.vault_path, "gardens"), "garden")
        detections = find_notes(
            os.path.join(self.vault_path, "detection"), "detection"
        )
        profiles = find_notes(os.path.join(self.vault_path, "profiles"), "profile")
        return {
            "total_sessions": len(sessions),
            "total_seeds": len(seeds),
            "total_gardens": len(gardens),
            "total_detections": len(detections),
            "total_profiles": len(profiles),
            "last_session": sessions[-1][1].get("timestamp", "N/A")
            if sessions
            else "N/A",
        }

    @staticmethod
    def _fresh_buffer():
        return {
            "cycles": 0,
            "harvested": 0,
            "planted": 0,
            "errors": 0,
            "harvest_details": [],
            "plant_details": [],
            "error_details": [],
        }

    def _detection_risk(self):
        b = self._buffer
        if b["cycles"] == 0:
            return "low"
        er = b["errors"] / b["cycles"]
        if er > 0.5:
            return "high"
        if er > 0.2:
            return "medium"
        return "low"

    def _write_snapshot(self, trigger, changes):
        fname = f"{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.md"
        path = os.path.join(self.vault_path, "config", fname)
        write_note(
            path,
            {
                "type": "config_snapshot",
                "timestamp": now_iso(),
                "trigger": trigger,
                "changes": changes,
            },
        )

    def _update_seed(self, seed_code, event):
        path = os.path.join(self.vault_path, "seeds", f"{seed_code}.md")
        fm, body = read_note(path)
        if fm is None or fm.get("type") != "seed":
            fm = {
                "type": "seed",
                "code": seed_code,
                "group": "",
                "best_beds": [],
                "total_planted": 0,
                "total_harvested": 0,
                "success_rate": 0,
            }
        if event.get("planted"):
            fm["total_planted"] = (fm.get("total_planted") or 0) + 1
            fm["last_planted"] = now_iso()
            bed = event.get("bed")
            if bed and bed not in (fm.get("best_beds") or []):
                fm.setdefault("best_beds", []).append(bed)
        if event.get("harvested"):
            fm["total_harvested"] = (fm.get("total_harvested") or 0) + 1
        planted = fm.get("total_planted", 0)
        harvested = fm.get("total_harvested", 0)
        fm["success_rate"] = round(harvested / max(planted, 1), 2)
        write_note(path, fm, body or "")

    def _update_garden(self, config):
        if not self._garden:
            return
        path = os.path.join(self.vault_path, "gardens", f"{self._garden}.md")
        fm, body = read_note(path)
        if fm is None or fm.get("type") != "garden":
            fm = {
                "type": "garden",
                "code": self._garden,
                "bed_counts": {},
                "peak_hours": [],
            }
        fm["total_sessions"] = (fm.get("total_sessions") or 0) + 1
        fm["last_session"] = now_iso()
        fm["total_harvested"] = (
            fm.get("total_harvested") or 0
        ) + self._buffer["harvested"]
        fm["total_planted"] = (
            fm.get("total_planted") or 0
        ) + self._buffer["planted"]
        fm["total_cycles"] = (
            fm.get("total_cycles") or 0
        ) + self._buffer["cycles"]
        session_h = (
            (time.time() - self._session_start) / 3600
            if self._session_start
            else 1
        )
        new_hph = self._buffer["harvested"] / session_h if session_h > 0 else 0
        old_hph = fm.get("harvests_per_hour")
        total_s = fm["total_sessions"]
        if total_s > 1:
            fm["harvests_per_hour"] = round(
                ((old_hph or 0) * (total_s - 1) + new_hph) / total_s, 2
            )
        else:
            fm["harvests_per_hour"] = round(new_hph, 2)
        write_note(path, fm, body or "")
