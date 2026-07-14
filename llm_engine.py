"""LLM Engine — local intelligence layer via Ollama for UNCHAINED.

Provides natural-language strategy advice, session analysis, anomaly
explanations, and brain summaries.  Falls back gracefully when Ollama
is unavailable so the bot always works without it.
"""

import json
import logging
import time

from ollama_manager import (
    is_ollama_running,
    is_model_available,
    send_prompt,
    DEFAULT_MODEL,
)

logger = logging.getLogger("unchained.llm")

_SYSTEM_PROMPT = (
    "You are UNCHAINED, an autonomous farming assistant for the game chainers.io. "
    "You analyze session data, seed performance, detection risks, and garden patterns "
    "to provide concise strategic advice. Be brief — 1-3 sentences max per answer. "
    "Use plain language. Never mention being an AI or language model."
)


class LLMEngine:
    """Thin wrapper around local Ollama for contextual intelligence.

    Usage:
        llm = LLMEngine()
        if llm.available:
            advice = llm.get_strategy_advice(session_state, config, memory_summary)
    """

    def __init__(self, model=None):
        self.model = model or DEFAULT_MODEL
        self._available = False
        self._last_check = 0
        self._check_interval = 30

    @property
    def available(self):
        now = time.time()
        if now - self._last_check < self._check_interval:
            return self._available
        self._last_check = now
        self._available = is_ollama_running() and is_model_available(self.model)
        return self._available

    def _ask(self, prompt, temperature=0.5, max_tokens=256):
        if not self.available:
            return None
        return send_prompt(
            self.model, prompt,
            system=_SYSTEM_PROMPT,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def get_strategy_advice(self, session_state, config, memory_summary=None):
        """Analyze current state and return a short strategy recommendation."""
        ctx = self._build_context(session_state, config, memory_summary)
        prompt = (
            f"Current state:\n{ctx}\n\n"
            "Should I continue farming, take a break, or adjust strategy? "
            "Consider detection risk, errors, harvest rate, and session length."
        )
        return self._ask(prompt, temperature=0.4, max_tokens=150)

    def explain_anomaly(self, session_state, severity, config):
        """Explain why an anomaly was flagged and what to do about it."""
        ctx = self._build_context(session_state, config)
        prompt = (
            f"Anomaly detected (severity: {severity}):\n{ctx}\n\n"
            "Why might this be happening and what should I do?"
        )
        return self._ask(prompt, temperature=0.3, max_tokens=150)

    def summarize_brain(self, memory_summary, recent_sessions=None, detections=None):
        """Natural-language brain summary for the Discord /brain command."""
        parts = []
        if memory_summary:
            parts.append("Memory stats:")
            for k, v in memory_summary.items():
                parts.append(f"  {k}: {v}")
        if recent_sessions:
            parts.append("Recent sessions (last 5):")
            for s in recent_sessions[-5:]:
                ts = s.get("timestamp", "?")
                h = s.get("harvested", 0)
                e = s.get("errors", 0)
                parts.append(f"  {ts}: harvested={h} errors={e}")
        if detections:
            parts.append(f"Total detections: {len(detections)}")
            for d in detections[-3:]:
                parts.append(f"  {d.get('type', '?')}: {d.get('detail', '?')}")

        if not parts:
            return "No data available yet."

        ctx = "\n".join(parts)
        prompt = (
            f"Here is the bot's knowledge vault:\n{ctx}\n\n"
            "Summarize the current state in 2-4 lines. "
            "Highlight trends, risks, and anything unusual."
        )
        return self._ask(prompt, temperature=0.4, max_tokens=200)

    def analyze_seed_performance(self, seed_data):
        """Analyze which seeds perform best and suggest rotations."""
        if not seed_data:
            return None
        prompt = (
            f"Seed performance data:\n{json.dumps(seed_data, indent=1)}\n\n"
            "Which seeds are performing well? Suggest a rotation strategy."
        )
        return self._ask(prompt, temperature=0.4, max_tokens=150)

    def analyze_task(self, mission_data, inventory=None, session_state=None):
        """Analyze a single task and return structured plan."""
        ctx_parts = [f"Task: {mission_data.get('title', '?')}",
                     f"Type: {mission_data.get('type', '?')}",
                     f"Progress: {mission_data.get('current', 0)}/{mission_data.get('required', 0)}",
                     f"Remaining: {mission_data.get('remaining', 0)}",
                     f"Reward: {mission_data.get('reward', 0)}x"]

        if inventory:
            seeds = [i for i in inventory if i.get("itemType") == "farmSeeds"]
            ctx_parts.append(f"Seeds available: {len(seeds)} types")

        if session_state:
            ctx_parts.append(f"Errors: {session_state.get('consecutive_errors', 0)}")
            ctx_parts.append(f"Risk: {session_state.get('detection_risk', 'low')}")

        context = "\n".join(ctx_parts)
        prompt = (
            f"Analyze this task:\n{context}\n\n"
            "Return JSON: {\"priority\": 1-5, \"steps\": [list], "
            "\"time_min\": float, \"success\": 0.0-1.0, \"reason\": \"1 sentence\"}"
        )
        return self._ask(prompt, temperature=0.3, max_tokens=200)

    def plan_task_sequence(self, tasks, session_state=None):
        """Get AI recommendation on task execution order."""
        task_list = []
        for t in tasks[:8]:
            task_list.append(
                f"- {t.get('title', '?')} ({t.get('type', '?')}, "
                f"{t.get('progress', 0):.0%} done, reward={t.get('reward', 0)}x)"
            )
        ctx = "Tasks:\n" + "\n".join(task_list)
        if session_state:
            ctx += f"\n\nSession: errors={session_state.get('consecutive_errors', 0)}, risk={session_state.get('detection_risk', 'low')}"

        prompt = (
            f"{ctx}\n\n"
            "What order should I execute these tasks? Consider time, rewards, and risk. "
            "Reply with comma-separated task numbers in priority order."
        )
        return self._ask(prompt, temperature=0.4, max_tokens=100)

    def should_skip_task(self, task_data, history=None):
        """Decide if a task should be skipped."""
        ctx = f"Task: {task_data.get('title', '?')}\nType: {task_data.get('type', '?')}"
        if history:
            ctx += f"\nHistory: {history}"
        prompt = (
            f"{ctx}\n\n"
            "Should this task be skipped? Reply YES or NO with brief reason."
        )
        return self._ask(prompt, temperature=0.3, max_tokens=50)

    def chat(self, user_message, context=None):
        """Free-form chat for user queries about the bot."""
        prompt = user_message
        if context:
            prompt = f"Context:\n{context}\n\nQuestion: {user_message}"
        return self._ask(prompt, temperature=0.6, max_tokens=300)

    @staticmethod
    def _build_context(session_state, config, memory_summary=None):
        lines = []
        lines.append(f"Session elapsed: {session_state.get('session_elapsed_h', 0):.1f}h")
        lines.append(f"Cycles in session: {session_state.get('cycles_in_session', 0)}")
        lines.append(f"Consecutive errors: {session_state.get('consecutive_errors', 0)}")
        lines.append(f"Detection risk: {session_state.get('detection_risk', 'low')}")
        lines.append(f"Hours since detection: {session_state.get('hours_since_last_detection', 'N/A')}")
        lines.append(f"Harvest rate trend: {session_state.get('harvest_rate_trend', 0):.1f}")
        lines.append(f"Delays: action {config.get('action_delay_min', 4)}-{config.get('action_delay_max', 10)}s, "
                      f"cycle {config.get('cycle_delay_min', 90)}-{config.get('cycle_delay_max', 180)}s")
        lines.append(f"Sandbagging: {config.get('sandbagging_enabled', True)}")
        if memory_summary:
            lines.append(f"Vault: {memory_summary.get('total_sessions', 0)} sessions, "
                         f"{memory_summary.get('total_detections', 0)} detections")
        return "\n".join(lines)
