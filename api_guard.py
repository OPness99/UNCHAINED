"""API Guard — enforces the API blacklist to prevent banned API usage.

Blocks calls to dangerous APIs that could result in account bans.
See API_REFERENCE.md for the complete classification.
"""

import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger("unchained.api_guard")

API_BLACKLIST_PATTERNS = [
    (re.compile(r"robot", re.I), "RobotAPI2.js — explicitly admits to botting"),
    (re.compile(r"intercept", re.I), "Network interception — modifies game code"),
    (re.compile(r"monkey.?patch", re.I), "Code modification — violates ToS"),
    (re.compile(r"direct.?http", re.I), "Direct HTTP — bypasses game client"),
    (re.compile(r"bypass", re.I), "Bypass — circumvents game systems"),
    (re.compile(r"exploit", re.I), "Exploit — unauthorized usage"),
]

API_BLACKLIST_METHODS = [
    "robot_start",
    "robot_stop",
    "robot_status",
    "robot_config",
    "robot_execute",
    "robot_automate",
    "robot_farm",
    "robot_harvest",
    "robot_plant",
]

DANGEROUS_JS_PATTERNS = [
    (re.compile(r"window\.fetch\s*="), "Monkey-patching window.fetch"),
    (re.compile(r"XMLHttpRequest\.prototype"), "Monkey-patching XMLHttpRequest"),
    (re.compile(r"window\.__unchained"), "Injecting global state"),
]

RISKY_API_METHODS = [
    "craft_item",
    "apply_fertilizer",
    "use_booster",
    "activate_device",
]


class APIGuard:
    """Enforces API blacklist and monitors risky API usage."""

    def __init__(self):
        self._blocked_calls: list = []
        self._risky_calls: list = []
        self._enabled = True

    def check_method(self, method_name: str) -> Tuple[bool, str]:
        """Check if an API method is allowed.

        Returns:
            (is_allowed, reason)
        """
        if not self._enabled:
            return True, ""

        method_lower = method_name.lower()

        if method_lower in [m.lower() for m in API_BLACKLIST_METHODS]:
            reason = f"Blacklisted method: {method_name}"
            self._blocked_calls.append((method_name, reason))
            logger.warning(f"BLOCKED API call: {method_name} — {reason}")
            return False, reason

        for pattern, reason in API_BLACKLIST_PATTERNS:
            if pattern.search(method_name):
                full_reason = f"{reason} (matched: {method_name})"
                self._blocked_calls.append((method_name, full_reason))
                logger.warning(f"BLOCKED API call: {method_name} — {full_reason}")
                return False, full_reason

        return True, ""

    def check_js_code(self, js_code: str) -> Tuple[bool, str]:
        """Check if JavaScript code contains dangerous patterns.

        Returns:
            (is_allowed, reason)
        """
        if not self._enabled:
            return True, ""

        for pattern, reason in DANGEROUS_JS_PATTERNS:
            if pattern.search(js_code):
                full_reason = f"Dangerous JS pattern: {reason}"
                self._blocked_calls.append(("js_eval", full_reason))
                logger.warning(f"BLOCKED JS eval: {full_reason}")
                return False, full_reason

        return True, ""

    def check_risky_method(self, method_name: str) -> Tuple[bool, str]:
        """Check if an API method is risky (allowed but logged).

        Returns:
            (is_risky, warning)
        """
        if method_name.lower() in [m.lower() for m in RISKY_API_METHODS]:
            warning = f"Risky API: {method_name} — use with caution"
            self._risky_calls.append((method_name, warning))
            logger.info(f"RISKY API call: {method_name}")
            return True, warning

        return False, ""

    def get_blocked_count(self) -> int:
        return len(self._blocked_calls)

    def get_risky_count(self) -> int:
        return len(self._risky_calls)

    def get_blocked_log(self) -> list:
        return list(self._blocked_calls)

    def get_risky_log(self) -> list:
        return list(self._risky_calls)

    def clear_logs(self):
        self._blocked_calls.clear()
        self._risky_calls.clear()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled

    def summary(self) -> str:
        return (f"API Guard: {self.get_blocked_count()} blocked, "
                f"{self.get_risky_count()} risky calls")


_api_guard_instance: Optional[APIGuard] = None


def get_api_guard() -> APIGuard:
    """Get the global API guard instance."""
    global _api_guard_instance
    if _api_guard_instance is None:
        _api_guard_instance = APIGuard()
    return _api_guard_instance


def is_api_allowed(method_name: str) -> bool:
    """Quick check if an API method is allowed."""
    allowed, _ = get_api_guard().check_method(method_name)
    return allowed


def is_js_safe(js_code: str) -> bool:
    """Quick check if JavaScript code is safe to evaluate."""
    allowed, _ = get_api_guard().check_js_code(js_code)
    return allowed
