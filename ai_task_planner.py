"""AI Task Planner — intelligent task analysis and execution planning.

Analyzes available missions, builds prioritized execution plans,
tracks completed tasks across cycles, and self-automates decisions.
Supports multi-cycle tasks that can't be completed in one pass.
"""

import json
import logging
import os
import re
import time
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta

from task_manager import Mission, TaskType, TaskStatus
from memory import BotMemory, write_note, read_note, find_notes, now_iso

logger = logging.getLogger("unchained.ai_planner")


@dataclass
class TaskPlan:
    """AI-generated plan for task execution."""
    mission_id: str
    title: str
    task_type: str
    priority: int  # 1=highest
    steps: List[str]
    estimated_time_min: float
    success_probability: float
    dependencies: List[str]  # task_ids this depends on
    reasoning: str
    needs_multi_cycle: bool = False
    created_at: str = ""


@dataclass
class ActiveTask:
    """Tracks a task that spans multiple cycles."""
    task_id: str
    title: str
    task_type: str
    required_amount: int
    completed_amount: int
    current_step: str  # what we're doing right now
    step_history: List[str]  # what we've already done
    started_at: str
    last_attempt: str
    attempt_count: int = 0
    blocked_reason: str = ""  # why we can't proceed right now

    @property
    def remaining(self) -> int:
        return max(0, self.required_amount - self.completed_amount)

    @property
    def progress_pct(self) -> float:
        if self.required_amount <= 0:
            return 1.0
        return min(1.0, self.completed_amount / self.required_amount)

    @property
    def is_done(self) -> bool:
        return self.completed_amount >= self.required_amount

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "task_type": self.task_type,
            "required_amount": self.required_amount,
            "completed_amount": self.completed_amount,
            "current_step": self.current_step,
            "step_history": self.step_history,
            "started_at": self.started_at,
            "last_attempt": self.last_attempt,
            "attempt_count": self.attempt_count,
            "blocked_reason": self.blocked_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ActiveTask":
        return cls(
            task_id=d["task_id"],
            title=d["title"],
            task_type=d.get("task_type", ""),
            required_amount=d.get("required_amount", 0),
            completed_amount=d.get("completed_amount", 0),
            current_step=d.get("current_step", ""),
            step_history=d.get("step_history", []),
            started_at=d.get("started_at", ""),
            last_attempt=d.get("last_attempt", ""),
            attempt_count=d.get("attempt_count", 0),
            blocked_reason=d.get("blocked_reason", ""),
        )


@dataclass
class CycleContext:
    """State tracked across farming cycles."""
    cycle_number: int
    tasks_attempted: List[str]
    tasks_completed: List[str]
    tasks_failed: List[str]
    last_cycle_time: str
    session_stats: Dict[str, Any]


class AITaskPlanner:
    """AI-driven task analysis and execution planning.

    Features:
    - Analyzes tasks and builds step-by-step execution plans
    - Tracks completed tasks to avoid repeats
    - Maintains state context across cycles
    - Self-automates decision-making based on history
    - Multi-cycle task support: tracks in-progress tasks across cycles
    - Resource-aware planning: detects insufficient resources and plans around it
    """

    def __init__(self, llm_engine, memory: BotMemory = None):
        self._llm = llm_engine
        self._memory = memory
        self._context = CycleContext(
            cycle_number=0,
            tasks_attempted=[],
            tasks_completed=[],
            tasks_failed=[],
            last_cycle_time="",
            session_stats={}
        )
        self._active_tasks: Dict[str, ActiveTask] = {}
        self._plan_cache: Dict[str, TaskPlan] = {}
        self._completed_history: List[str] = []
        self._load_persistent_state()

    def _load_persistent_state(self):
        """Load previously completed tasks and active tasks from vault."""
        if not self._memory:
            return
        try:
            task_dir = os.path.join(self._memory.vault_path, "tasks")
            state_file = os.path.join(task_dir, "_planner_state.json")
            if os.path.exists(state_file):
                with open(state_file, encoding="utf-8") as f:
                    state = json.load(f)
                    self._completed_history = state.get("completed_history", [])
                    self._context.cycle_number = state.get("last_cycle", 0)
                    for ad in state.get("active_tasks", []):
                        at = ActiveTask.from_dict(ad)
                        self._active_tasks[at.task_id] = at
                    logger.info(f"Loaded planner state: {len(self._completed_history)} completed, "
                                f"{len(self._active_tasks)} active tasks")
        except Exception as e:
            logger.debug(f"No previous planner state: {e}")

    def _save_persistent_state(self):
        """Save planner state to vault."""
        if not self._memory:
            return
        try:
            task_dir = os.path.join(self._memory.vault_path, "tasks")
            os.makedirs(task_dir, exist_ok=True)
            state_file = os.path.join(task_dir, "_planner_state.json")
            state = {
                "completed_history": self._completed_history[-500:],
                "active_tasks": [at.to_dict() for at in self._active_tasks.values()],
                "last_cycle": self._context.cycle_number,
                "last_updated": now_iso()
            }
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save planner state: {e}")

    def get_active_tasks(self) -> List[ActiveTask]:
        """Return all tasks that are in-progress and need continuation."""
        return [at for at in self._active_tasks.values() if not at.is_done]

    def get_active_task_for_mission(self, mission: Mission) -> Optional[ActiveTask]:
        """Check if a mission already has an active tracking entry."""
        task_key = mission.task_id or mission.raw_title
        return self._active_tasks.get(task_key)

    def register_active_task(self, mission: Mission, inventory: List[Dict] = None) -> ActiveTask:
        """Register a new multi-cycle task for tracking."""
        task_key = mission.task_id or mission.raw_title
        if task_key in self._active_tasks:
            return self._active_tasks[task_key]

        # Parse required amount from title
        required = self._parse_required_amount(mission)
        completed = mission.progress_current

        at = ActiveTask(
            task_id=task_key,
            title=mission.raw_title,
            task_type=mission.task_type.value,
            required_amount=required,
            completed_amount=completed,
            current_step="starting",
            step_history=[],
            started_at=now_iso(),
            last_attempt=now_iso(),
        )
        self._active_tasks[task_key] = at
        self._save_persistent_state()
        return at

    def _parse_required_amount(self, mission: Mission) -> int:
        """Extract the required amount from task title or progress."""
        if mission.progress_required > 0:
            return mission.progress_required
        m = re.search(r"(\d+)", mission.raw_title)
        return int(m.group(1)) if m else 1

    def check_resources(self, mission: Mission, inventory: List[Dict]) -> Dict[str, Any]:
        """Check if we have enough resources to complete a task.

        Returns dict with:
            - enough: bool
            - have: int (what we have)
            - need: int (what we need)
            - item: str (what item)
            - suggestion: str (what to do about it)
        """
        title_lower = mission.raw_title.lower()
        result = {"enough": True, "have": 0, "need": 0, "item": "", "suggestion": ""}

        if mission.task_type == TaskType.FARMING:
            seed_match = re.search(r"plant\s+\d+\s+([\w\s]+?)\s+seeds?", title_lower)
            if seed_match:
                needed_seed = seed_match.group(1).strip()
                needed_count = self._parse_required_amount(mission)
                have_count = 0
                for item in inventory:
                    code = item.get("itemCode", "").lower()
                    if needed_seed.lower() in code and item.get("itemType") == "farmSeeds":
                        have_count = item.get("count", 0)
                        break

                result["have"] = have_count
                result["need"] = needed_count
                result["item"] = needed_seed

                if have_count < needed_count:
                    result["enough"] = False
                    if have_count == 0:
                        result["suggestion"] = f"No {needed_seed} seeds — need to obtain them"
                    else:
                        result["suggestion"] = (f"Only {have_count} {needed_seed} seeds, "
                                                f"need {needed_count} — plant what we have, "
                                                "harvest when ready, then continue")
                return result

        elif mission.task_type == TaskType.FEEDING:
            feed_match = re.search(r"feed\s+(\d+)", title_lower)
            if feed_match:
                needed = int(feed_match.group(1))
                have = 0
                for item in inventory:
                    if item.get("itemType") == "food" or "feed" in item.get("itemCode", "").lower():
                        have += item.get("count", 0)
                result["have"] = have
                result["need"] = needed
                result["item"] = "feed"
                if have < needed:
                    result["enough"] = False
                    result["suggestion"] = f"Only {have} feed items, need {needed}"
                return result

        elif mission.task_type == TaskType.COLLECTION:
            put_match = re.search(r"put\s+(\d+)", title_lower)
            if put_match:
                needed = int(put_match.group(1))
                have = 0
                for item in inventory:
                    if item.get("itemType") in ("farmProducts", "crafted"):
                        have += item.get("count", 0)
                result["have"] = have
                result["need"] = needed
                result["item"] = "products"
                if have < needed:
                    result["enough"] = False
                    result["suggestion"] = f"Only {have} products, need {needed} — farm more first"
                return result

        return result

    def plan_multi_cycle(self, mission: Mission, inventory: List[Dict],
                         session_state: Dict = None) -> Optional[TaskPlan]:
        """Create a multi-cycle plan for tasks that can't be done in one pass.

        This is the key intelligence: when resources are insufficient,
        the AI figures out what to do first (harvest, obtain seeds, etc.)
        and builds a plan that spans multiple cycles.
        """
        if not self._llm or not self._llm.available:
            return None

        resource_check = self.check_resources(mission, inventory)
        active = self.get_active_task_for_mission(mission)
        progress = active.completed_amount if active else mission.progress_current
        remaining = resource_check["need"] - progress

        ctx_parts = [
            f"Task: {mission.raw_title}",
            f"Type: {mission.task_type.value}",
            f"Progress: {progress}/{resource_check['need']} ({progress/resource_check['need']:.0%})" if resource_check['need'] > 0 else "Progress: unknown",
            f"Remaining to complete: {remaining}",
            f"Resource check: have {resource_check['have']} {resource_check['item']}, need {resource_check['need']}",
        ]

        if not resource_check["enough"]:
            ctx_parts.append(f"PROBLEM: {resource_check['suggestion']}")

        if active:
            ctx_parts.append(f"Already attempted {active.attempt_count} times")
            ctx_parts.append(f"History: {'; '.join(active.step_history[-3:])}")

        if inventory:
            seed_items = [i for i in inventory if i.get("itemType") == "farmSeeds"]
            ctx_parts.append(f"Seeds in inventory:")
            for s in seed_items[:8]:
                ctx_parts.append(f"  - {s.get('itemCode')}: {s.get('count', 0)}")
            product_items = [i for i in inventory if i.get("itemType") in ("farmProducts", "")]
            harvestable = [i for i in inventory if i.get("itemType") == "farmSeeds" and i.get("growing")]
            if harvestable:
                ctx_parts.append(f"Currently growing: {len(harvestable)} beds")

        if session_state:
            ctx_parts.append(f"Session errors: {session_state.get('consecutive_errors', 0)}")
            ctx_parts.append(f"Detection risk: {session_state.get('detection_risk', 'low')}")

        context = "\n".join(ctx_parts)

        if not resource_check["enough"]:
            prompt = f"""This task cannot be completed in one cycle due to insufficient resources.

{context}

Create a multi-cycle plan. The bot should:
1. Do what it CAN right now (plant available seeds, harvest ready crops, etc.)
2. Track progress after each action
3. Continue in the next cycle until the task is complete

Return JSON:
- priority: 1-5 (1=highest)
- steps: list of actions for THIS cycle only (be specific)
- next_cycle_steps: what to do next cycle
- time_minutes: estimated time for this cycle
- success_chance: 0.0-1.0
- reasoning: explain the multi-cycle approach"""
        else:
            prompt = f"""Analyze this farming task and create an execution plan.

{context}

Return JSON:
- priority: 1-5 (1=highest)
- steps: list of 2-4 short action steps
- time_minutes: estimated time in minutes
- success_chance: 0.0-1.0
- reasoning: 1-2 sentence explanation

Focus on: feasibility, resource requirements, time efficiency, and risk."""

        response = self._llm._ask(prompt, temperature=0.3, max_tokens=400)
        if not response:
            return None

        plan_data = self._parse_json_response(response)
        if not plan_data:
            return None

        return TaskPlan(
            mission_id=mission.task_id or mission.raw_title[:20],
            title=mission.raw_title,
            task_type=mission.task_type.value,
            priority=plan_data.get("priority", 3),
            steps=plan_data.get("steps", ["Execute task"]),
            estimated_time_min=plan_data.get("time_minutes", 2.0),
            success_probability=plan_data.get("success_chance", 0.5),
            dependencies=plan_data.get("dependencies", []),
            reasoning=plan_data.get("reasoning", "AI analysis"),
            needs_multi_cycle=not resource_check["enough"],
            created_at=now_iso()
        )

    def update_progress(self, mission: Mission, amount_completed: int = 1):
        """Update the progress of an active task after an action."""
        task_key = mission.task_id or mission.raw_title
        at = self._active_tasks.get(task_key)
        if at:
            at.completed_amount += amount_completed
            at.last_attempt = now_iso()
            at.attempt_count += 1
            at.step_history.append(f"Completed {amount_completed} more at {now_iso()}")
            if at.is_done:
                self.mark_task_completed(at.task_id, at.title)
                del self._active_tasks[task_key]
            self._save_persistent_state()

    def record_step(self, mission: Mission, step: str):
        """Record what step we're currently on for an active task."""
        task_key = mission.task_id or mission.raw_title
        at = self._active_tasks.get(task_key)
        if at:
            at.current_step = step
            at.last_attempt = now_iso()
            self._save_persistent_state()

    def block_task(self, mission: Mission, reason: str):
        """Mark a task as blocked with a reason."""
        task_key = mission.task_id or mission.raw_title
        at = self._active_tasks.get(task_key)
        if at:
            at.blocked_reason = reason
            self._save_persistent_state()

    def unblock_task(self, mission: Mission):
        """Remove block from a task."""
        task_key = mission.task_id or mission.raw_title
        at = self._active_tasks.get(task_key)
        if at:
            at.blocked_reason = ""
            self._save_persistent_state()

    def decide_next_action(self, mission: Mission, inventory: List[Dict]) -> Optional[str]:
        """AI decides what to do next for an active multi-cycle task."""
        if not self._llm or not self._llm.available:
            return None

        task_key = mission.task_id or mission.raw_title
        at = self._active_tasks.get(task_key)
        if not at:
            return None

        resource_check = self.check_resources(mission, inventory)

        ctx = (
            f"Active task: {at.title}\n"
            f"Progress: {at.completed_amount}/{at.required_amount}\n"
            f"Current step: {at.current_step}\n"
            f"Attempts: {at.attempt_count}\n"
            f"Resources: have {resource_check['have']} {resource_check['item']}, "
            f"need {resource_check['need']}\n"
            f"Last steps: {'; '.join(at.step_history[-3:])}\n"
        )
        if at.blocked_reason:
            ctx += f"Blocked: {at.blocked_reason}\n"

        prompt = (
            f"{ctx}\n"
            "What should the bot do RIGHT NOW for this task? "
            "Be specific and actionable. One short sentence."
        )
        return self._llm._ask(prompt, temperature=0.3, max_tokens=80)

    def analyze_missions(self, missions: List[Mission], inventory: List[Dict] = None,
                         session_state: Dict = None) -> List[TaskPlan]:
        """Analyze available missions and generate prioritized execution plans.

        Active (in-progress) tasks get priority over new tasks.
        """
        if not missions or not self._llm or not self._llm.available:
            return []

        available = [m for m in missions if not m.is_done and not m.can_claim]
        if not available:
            return []

        plans = []

        # Active tasks get first priority
        for mission in available:
            if mission.task_id in self._active_tasks or mission.raw_title in self._active_tasks:
                plan = self.plan_multi_cycle(mission, inventory, session_state)
                if plan:
                    plan.priority = 1  # Active tasks are always highest priority
                    plans.append(plan)

        # Then analyze new tasks
        fresh_tasks = self._filter_recently_completed(available)
        for mission in fresh_tasks[:6]:
            task_key = mission.task_id or mission.raw_title
            if task_key in self._active_tasks:
                continue  # Already handled above
            resource_check = self.check_resources(mission, inventory) if inventory else {"enough": True}
            plan = self.plan_multi_cycle(mission, inventory, session_state)
            if plan:
                if not resource_check.get("enough", True):
                    plan.needs_multi_cycle = True
                plans.append(plan)

        plans.sort(key=lambda p: (p.priority, -p.success_probability))

        self._context.cycle_number += 1
        self._context.last_cycle_time = now_iso()

        return plans

    def _filter_recently_completed(self, missions: List[Mission]) -> List[Mission]:
        """Remove tasks completed recently from consideration."""
        fresh = []
        for m in missions:
            task_key = f"{m.task_id}_{m.raw_title}"
            if task_key not in self._completed_history:
                fresh.append(m)
            else:
                logger.debug(f"Skipping recently completed: {m.title}")
        return fresh

    def _parse_json_response(self, response: str) -> Optional[Dict]:
        """Extract JSON from LLM response."""
        json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', response, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass

        brace_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        return None

    def mark_task_attempted(self, task_id: str, title: str):
        """Record that a task was attempted."""
        key = f"{task_id}_{title}"
        if key not in self._context.tasks_attempted:
            self._context.tasks_attempted.append(key)

    def mark_task_completed(self, task_id: str, title: str):
        """Record task completion and persist to history."""
        key = f"{task_id}_{title}"
        if key not in self._context.tasks_completed:
            self._context.tasks_completed.append(key)
        if key not in self._completed_history:
            self._completed_history.append(key)
        # Remove from active tasks if present
        if task_id in self._active_tasks:
            del self._active_tasks[task_id]
        self._save_persistent_state()
        self._save_task_note(task_id, title, "completed")

    def mark_task_failed(self, task_id: str, title: str, reason: str = ""):
        """Record task failure. Does NOT remove from active — we retry next cycle."""
        key = f"{task_id}_{title}"
        if key not in self._context.tasks_failed:
            self._context.tasks_failed.append(key)
        # Update active task attempt count
        at = self._active_tasks.get(task_id)
        if at:
            at.attempt_count += 1
            at.blocked_reason = reason
            at.last_attempt = now_iso()
            self._save_persistent_state()
        self._save_task_note(task_id, title, "failed", reason)

    def should_skip_task(self, mission: Mission) -> Tuple[bool, str]:
        """Determine if a task should be skipped.

        Active (in-progress) tasks are NEVER skipped — we keep trying.
        Only skip truly completed or permanently failed tasks.
        """
        task_key = mission.task_id or mission.raw_title

        # NEVER skip an active multi-cycle task
        if task_key in self._active_tasks:
            return False, ""

        # Skip if completed recently
        full_key = f"{mission.task_id}_{mission.raw_title}"
        if full_key in self._completed_history:
            return True, "Recently completed"

        # Skip if failed 5+ times this session (permanent failure)
        fail_count = self._context.tasks_failed.count(full_key)
        if fail_count >= 5:
            return True, f"Failed {fail_count} times — giving up"

        return False, ""

    def _save_task_note(self, task_id: str, title: str, status: str, reason: str = ""):
        """Save a task execution note to the vault."""
        if not self._memory:
            return
        try:
            task_dir = os.path.join(self._memory.vault_path, "tasks")
            os.makedirs(task_dir, exist_ok=True)
            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in title[:30])
            note_file = os.path.join(task_dir, f"{safe_name}_{status}.md")
            fm = {
                "type": "task_execution",
                "task_id": task_id,
                "title": title,
                "status": status,
                "reason": reason,
                "timestamp": now_iso(),
                "cycle": self._context.cycle_number
            }
            body = f"Task {status}: {title}"
            if reason:
                body += f"\n\nReason: {reason}"
            write_note(note_file, fm, body)
        except Exception as e:
            logger.debug(f"Failed to save task note: {e}")

    def get_execution_summary(self) -> str:
        """Get a summary of current planning context for display."""
        active = self.get_active_tasks()
        parts = [
            f"Cycle #{self._context.cycle_number}",
            f"Active: {len(active)}",
            f"Completed: {len(self._context.tasks_completed)}",
            f"Failed: {len(self._context.tasks_failed)}",
        ]
        if active:
            for at in active[:3]:
                parts.append(f"  -> {at.title} ({at.completed_amount}/{at.required_amount})")
        return " | ".join(parts)

    def recommend_strategy(self, available_tasks: List[Mission],
                           session_state: Dict) -> Optional[str]:
        """Get AI recommendation on overall task strategy."""
        if not self._llm or not self._llm.available or not available_tasks:
            return None

        active = self.get_active_tasks()
        task_summary = []
        if active:
            task_summary.append("IN-PROGRESS TASKS:")
            for at in active:
                task_summary.append(
                    f"  - {at.title} ({at.completed_amount}/{at.required_amount}, "
                    f"attempt {at.attempt_count})"
                )

        task_summary.append("NEW TASKS:")
        for t in available_tasks[:8]:
            task_summary.append(
                f"  - {t.raw_title} ({t.task_type.value}, {t.progress_pct:.0%} done, "
                f"reward={t.reward_count}x)"
            )

        ctx = "\n".join(task_summary)
        if session_state:
            ctx += f"\n\nSession: errors={session_state.get('consecutive_errors', 0)}, "
            ctx += f"risk={session_state.get('detection_risk', 'low')}"

        prompt = f"""{ctx}

What's the best strategy? Prioritize completing in-progress tasks first.
Be concise: 2-3 sentences max."""

        return self._llm._ask(prompt, temperature=0.4, max_tokens=150)

    def reset_session(self):
        """Reset session-specific state (call on new session)."""
        self._context.tasks_attempted.clear()
        self._context.tasks_completed.clear()
        self._context.tasks_failed.clear()
        self._context.cycle_number = 0
        self._plan_cache.clear()
        # Note: do NOT clear active_tasks — those persist across sessions
