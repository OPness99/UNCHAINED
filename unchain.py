"""UNCHAINED â€” Chainers.io farming bot with Playwright + PySide6 GUI."""

import sys
import json
import logging
import random
import re
import time
import os
import threading

from PySide6.QtCore import Qt, QTimer, Signal, Slot, QObject, QThread

from PySide6.QtGui import QFont, QTextCursor, QBrush, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QLineEdit, QSplitter, QFrame,
    QGroupBox, QFormLayout, QDoubleSpinBox, QSpinBox, QCheckBox,
    QComboBox, QDialog, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QAbstractItemView, QDialogButtonBox, QSizePolicy, QScrollArea,
    QTabWidget, QListWidget, QListWidgetItem,
)

from config import load_config, save_config, is_first_run, setup_first_run
from bot_engine import init_game, run_bot_cycle, PlotTracker, fetch_seed_config_data, execute_task, fetch_inventory
from bot_advanced import (
    get_mistake_engine, get_roi_tracker, simulate_human_interaction,
    detect_active_events, get_event_strategy,
)
from plot_config import SeedConfig
from memory import BotMemory
from vpn_panel import VPNPanel
from ml.inference import MLEngine
from llm_engine import LLMEngine
from discord_integration import DiscordWebhook, DiscordBot, BotBridge
from task_manager import TaskManager, TaskType, TaskStatus
from ai_task_planner import AITaskPlanner

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
from logging.handlers import RotatingFileHandler
_logger_handler = RotatingFileHandler("unchained.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
_logger_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().addHandler(_logger_handler)
logger = logging.getLogger("unchained")


class LogBridge(QObject):
    message = Signal(str)


log_bridge = LogBridge()


class GuiLogHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        log_bridge.message.emit(msg)


bot_logger = logging.getLogger("unchained")
bot_logger.handlers.clear()
bot_logger.addHandler(GuiLogHandler())
bot_logger.setLevel(logging.INFO)


class BotWorker(QObject):
    log_msg = Signal(str)
    status_msg = Signal(str)
    state_changed = Signal(str)
    seed_config_data_ready = Signal(object)
    memory_updated = Signal()
    cycle_completed = Signal(int)
    task_wall_updated = Signal(object)
    task_completed = Signal(object)
    llm_thought = Signal(str)
    recipes_fetched = Signal(object)

    def __init__(self, config, bridge=None, ml_engine=None, llm_engine=None):
        super().__init__()
        self.config = config
        self.memory = BotMemory()
        if ml_engine is not None:
            self.memory.set_ml_engine(ml_engine)
        self._ml_engine = ml_engine
        self._llm_engine = llm_engine
        self._ai_planner = AITaskPlanner(llm_engine, self.memory) if llm_engine else None
        self._bridge = bridge
        self._stop = threading.Event()
        self._want_start_farming = threading.Event()
        self._want_seed_config = threading.Event()
        self._want_one_cycle = threading.Event()
        self._want_task_refresh = threading.Event()
        self._want_recipes = threading.Event()
        self._farming_active = False
        self._farming_active = False
        self._task_manager = TaskManager(llm_engine=llm_engine, memory=self.memory)
        self._task_manager.set_config(config)
        self._ctx = None
        self._page = None
        self._ife = None
        self._run_js = None

    def _llm_call(self, label, func, *args, **kwargs):
        """Call an LLM function and emit thought logs for the AI Console."""
        self.llm_thought.emit(f"[{label}] Thinking...")
        try:
            result = func(*args, **kwargs)
            if result:
                self.llm_thought.emit(f"[{label}] {result}")
            else:
                self.llm_thought.emit(f"[{label}] (no response)")
            return result
        except Exception as e:
            self.llm_thought.emit(f"[{label}] Error: {e}")
            return None

    def _simulate_mouse(self, selector, action="click"):
        """Human-like mouse interaction with a page element.

        Uses curved Bezier path, micro-jitter, and random pauses.
        Only runs occasionally (30% chance) to mimic real behavior.
        """
        if not self._page or random.random() > 0.3:
            return False
        try:
            return simulate_human_interaction(self._page, selector, action)
        except Exception:
            return False

    def request_one_cycle(self):
        self._want_one_cycle.set()

    def request_seed_config_data(self):
        self._want_seed_config.set()

    def request_task_refresh(self):
        self._want_task_refresh.set()

    def request_recipe_fetch(self):
        self._want_recipes.set()

    def request_start_farming(self):
        self._want_start_farming.set()

    def request_stop_farming(self):
        self._farming_active = False

    def _make_sleep_fn(self, check_fn=None):
        def interruptible_sleep(seconds):
            """sleep_fn replacement that checks stop signals every 0.3s."""
            waited = 0.0
            while waited < seconds:
                if self._stop.is_set() or (check_fn and not check_fn()):
                    return
                chunk = min(0.3, seconds - waited)
                self._stop.wait(chunk)
                waited += chunk
        return interruptible_sleep

    def _handle_seed_config_request(self):
        if not self._ife:
            self.seed_config_data_ready.emit(None)
            self.log_msg.emit("ERROR: Game not initialized yet")
            return
        try:
            sc = SeedConfig()
            data = fetch_seed_config_data(self._ife, seed_config=sc)
            self.seed_config_data_ready.emit(data)
        except Exception as e:
            self.log_msg.emit(f"ERROR fetching seed config: {e}")
            self.seed_config_data_ready.emit(None)

    def _handle_vpn_request(self, action, **kwargs):
        if not self._bridge or not self._bridge._vpn_manager:
            return False, "VPN manager not available"
        mgr = self._bridge._vpn_manager
        try:
            if action == 'connect':
                server = kwargs.get('server', {})
                ok, msg = mgr.connect(server)
                if ok:
                    self.log_msg.emit(f"VPN connected to {server.get('name', '?')}")
                    self._bridge.update_status(vpn=server.get('name', 'connected'))
                else:
                    self.log_msg.emit(f"VPN connect failed: {msg}")
                return ok, msg
            elif action == 'disconnect':
                ok, msg = mgr.disconnect()
                if ok:
                    self.log_msg.emit("VPN disconnected")
                    self._bridge.update_status(vpn="disconnected")
                else:
                    self.log_msg.emit(f"VPN disconnect failed: {msg}")
                return ok, msg
            else:
                return False, f"Unknown VPN action: {action}"
        except Exception as e:
            self.log_msg.emit(f"VPN error: {e}")
            return False, str(e)

    def _find_game_iframe(self, wait_ok=True):
        page = self._page
        if not page:
            return None
        if wait_ok:
            try:
                page.wait_for_selector("iframe#playCanvasGameWindow", timeout=30000)
            except Exception:
                pass
        for f in page.frames:
            if "static.chainers.io" in f.url:
                self.log_msg.emit(f"Game frame: {f.url[:80]}")
                return f
        if not wait_ok:
            return None
        for _ in range(30):
            for f in page.frames:
                if "static.chainers.io" in f.url:
                    self.log_msg.emit(f"Game frame: {f.url[:80]}")
                    return f
            if self._stop.is_set():
                return None
            time.sleep(1)
        return None

    def _reconnect_ife(self, init_retries=8):
        page = self._page
        nav_attempted = False
        for attempt in range(init_retries):
            new_frame = self._find_game_iframe(wait_ok=False)
            if not new_frame:
                self.log_msg.emit("Waiting for game frame to appear...")
                for _ in range(60):
                    new_frame = self._find_game_iframe(wait_ok=False)
                    if new_frame:
                        break
                    if self._stop.is_set():
                        raise RuntimeError("Stop requested")
                    time.sleep(1)
            if not new_frame:
                if not nav_attempted:
                    nav_attempted = True
                    self.log_msg.emit("Navigating back to farm page...")
                    try:
                        page.goto("https://chainers.io/game/farm", timeout=60000)
                    except Exception as nav_e:
                        self.log_msg.emit(f"Navigation failed: {nav_e}")
                        raise RuntimeError("Game frame not found after retries")
                    continue
                else:
                    raise RuntimeError("Game frame not found after retries")
            self.log_msg.emit("Waiting for game to initialize in frame...")
            def _run_in_frame(script):
                wrapped = "(async function() { try { " + script + " } catch(e) { return {_error: String(e)}; } })()"
                return new_frame.evaluate(wrapped)
            try:
                self._ife = init_game(_run_in_frame, wait_for_ready=True)
                self.log_msg.emit("Game scripts re-initialized")
                return
            except Exception as e:
                if self._stop.is_set():
                    raise RuntimeError("Stop requested")
                self.log_msg.emit(f"Re-init attempt {attempt + 1}/{init_retries} failed: {e}")
                time.sleep(3)
        raise RuntimeError(f"Failed to re-init after {init_retries} attempts")

    def _check_browser_alive(self):
        """Check if the browser context and page are still responsive."""
        if not self._ctx or not self._page:
            return False
        try:
            self._page.evaluate("1 + 1", timeout=5000)
            return True
        except Exception:
            return False

    def _kill_orphan_chrome(self):
        """Kill orphaned Chrome/Chromium processes that may lock the profile."""
        import subprocess
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", "chrome.exe"],
                capture_output=True, timeout=10
            )
            if result.returncode == 0:
                self.log_msg.emit("Killed orphaned Chrome processes")
                time.sleep(2)
        except Exception:
            pass

    def _relaunch_browser(self, pw):
        """Close dead browser context and relaunch. Returns new page or None."""
        self.log_msg.emit("Attempting browser relaunch...")
        try:
            if self._ctx:
                try:
                    self._ctx.close()
                except Exception:
                    pass
                self._ctx = None
            self._ife = None
            self._page = None

            profile_dir = self.config.get("user_data_dir", "")
            if not profile_dir or "_MEI" in profile_dir:
                base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.getcwd()
                profile_dir = os.path.join(base, "profile")
                self.config["user_data_dir"] = profile_dir

            headless = self.config.get("headless", False)
            self._ctx = pw.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=headless,
                args=["--window-size=1280,900", "--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
                no_viewport=True,
            )
            self._ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)
            self._page = self._ctx.pages[0]
            self._page.goto("https://chainers.io/game/farm", timeout=60000)
            self.log_msg.emit("Browser relaunched — waiting for page load...")
            time.sleep(3)

            if self._ensure_game_ready():
                self.log_msg.emit("Browser relaunch successful — game re-initialized")
                return self._page
            else:
                self.log_msg.emit("Browser relaunched but game init failed")
                return self._page
        except Exception as e:
            self.log_msg.emit(f"Browser relaunch failed: {e}")
            return None

    def _sleep_with_health_check(self, seconds, pw):
        """Sleep that checks browser health every 30s and relaunches if dead."""
        elapsed = 0.0
        interval = 30
        while elapsed < seconds:
            if self._stop.is_set():
                return True
            chunk = min(interval, seconds - elapsed)
            self._stop.wait(chunk)
            elapsed += chunk
            if not self._stop.is_set() and not self._check_browser_alive():
                self.log_msg.emit("Browser died during sleep — relaunching...")
                new_page = self._relaunch_browser(pw)
                if new_page:
                    self.log_msg.emit("Recovered from browser crash")
                else:
                    self.log_msg.emit("FATAL: Could not recover browser")
                    self._farming_active = False
                    return True
        return False

    def _js_evaluate(self, wrapped):
        while True:
            if self._stop.is_set():
                return None
            try:
                return self._ife(wrapped)
            except Exception as e:
                if self._stop.is_set():
                    raise RuntimeError("Stop requested")
                self.log_msg.emit(f"JS evaluate error: {e}")
                time.sleep(3)

    def _ensure_game_ready(self):
        if self._ife:
            return True
        if not self._page:
            self.log_msg.emit("ERROR: Browser not launched")
            return False
        self.log_msg.emit("Initializing game scripts...")
        iframe = self._find_game_iframe()
        if not iframe:
            self.log_msg.emit("ERROR: Game iframe not found")
            return False
        try:
            def _run_in_frame(script):
                wrapped = "(async function() { try { " + script + " } catch(e) { return {_error: String(e)}; } })()"
                return iframe.evaluate(wrapped)
            self._ife = init_game(_run_in_frame, wait_for_ready=True)
            self.log_msg.emit("Game scripts loaded")
            return True
        except Exception as e:
            self.log_msg.emit(f"ERROR: Game init failed: {e}")
            return False

    def _run_one_cycle(self):
        if not self._ensure_game_ready():
            return
        self.log_msg.emit("--- One-shot cycle ---")
        tracker = PlotTracker()
        try:
            results = run_bot_cycle(self._ife, tracker, self.config, sleep_fn=self._make_sleep_fn(lambda: self._farming_active))
            h = len(results["harvested"])
            p = len(results["planted"])
            e = len(results["errors"])
            self.log_msg.emit(f"One-shot: harvested={h} planted={p} errors={e}")
        except Exception as ex:
            self.log_msg.emit(f"One-shot cycle error: {ex}")

    def _handle_task_refresh(self):
        if not self._page:
            self.log_msg.emit("ERROR: Browser not launched for task wall")
            self.task_wall_updated.emit(None)
            return
        try:
            missions = self._task_manager.refresh(page=self._page, ife=self._ife)
            inv = fetch_inventory(self._ife) if self._ife else []
            best = self._task_manager.get_best_tasks(inventory_items=inv)
            self.log_msg.emit(f"Task wall: {len(missions)} total, {len(best)} actionable")
            self.task_wall_updated.emit(missions)
        except Exception as e:
            self.log_msg.emit(f"ERROR refreshing task wall: {e}")
            self.task_wall_updated.emit(None)

    def _handle_recipe_fetch(self):
        if not self._page:
            self.recipes_fetched.emit(None)
            self.log_msg.emit("ERROR: Browser not launched for recipe fetch")
            return
        if not self._ife:
            self.log_msg.emit("Game scripts not loaded yet — initializing...")
            try:
                game_frame = self._find_game_iframe(wait_ok=True)
                if not game_frame:
                    self.recipes_fetched.emit(None)
                    self.log_msg.emit("ERROR: Game iframe not found")
                    return
                def _run_in_frame(script):
                    wrapped = "(async function() { try { " + script + " } catch(e) { return {_error: String(e)}; } })()"
                    return game_frame.evaluate(wrapped)
                self._ife = init_game(_run_in_frame, wait_for_ready=True)
                self.log_msg.emit("Game scripts initialized for recipe fetch")
            except Exception as e:
                self.recipes_fetched.emit(None)
                self.log_msg.emit(f"ERROR initializing game for recipe fetch: {e}")
                return
        js_code = '''
            try {
                let api = new API();
                let recipes = [];
                if (typeof api.get_recipes === 'function') {
                    let r = await api.get_recipes();
                    recipes = JSON.parse(JSON.stringify(r._data || r));
                }
                return {recipes: Array.isArray(recipes) ? recipes : []};
            } catch(e) { return {_error: e.message}; }
        '''
        try:
            data = self._ife(js_code)
            if data and not data.get('_error'):
                recipes = data.get('recipes', [])
                recipe_list = []
                for r in recipes:
                    rid = r.get('id', r.get('recipeID', ''))
                    name = r.get('name', r.get('recipeName', ''))
                    if name:
                        recipe_list.append({'id': str(rid), 'name': name})
                self.recipes_fetched.emit(recipe_list)
                self.log_msg.emit(f"Fetched {len(recipe_list)} recipes from game")
            else:
                self.recipes_fetched.emit(None)
                self.log_msg.emit(f"Recipe fetch failed: {data.get('_error', 'unknown') if data else 'no response'}")
        except Exception as e:
            self.recipes_fetched.emit(None)
            self.log_msg.emit(f"ERROR fetching recipes: {e}")

    def _interruptible_sleep(self, seconds):
        """Sleep that checks for stop signals every 0.5s."""
        waited = 0.0
        while waited < seconds:
            if self._stop.is_set() or not self._farming_active:
                return True
            chunk = min(0.5, seconds - waited)
            self._stop.wait(chunk)
            waited += chunk
        return False

    def _run_task_cycle(self, ife, tracker):
        """Run a single task cycle with multi-cycle task support.

        Priority order:
        1. Continue active multi-cycle tasks (harvest, plant, repeat)
        2. Claim completed rewards
        3. Start new tasks
        """
        if not self.config.get("task_wall_enabled", True):
            return
        try:
            # Periodic claim API re-discovery (every 10 cycles)
            if not hasattr(self, '_claim_refresh_counter'):
                self._claim_refresh_counter = 0
            self._claim_refresh_counter += 1
            if self._claim_refresh_counter >= 10:
                self._claim_refresh_counter = 0
                self._task_manager._claim_api_pattern = None

            missions = self._task_manager.refresh(page=self._page, ife=ife)
            if not missions:
                self.log_msg.emit("No missions found on task wall")
                return

            # Phase 1: Claim any completed rewards
            if self.config.get("task_wall_claim_rewards", True) and self._page:
                claimable = [m for m in missions if m.is_done and m.can_claim]
                if claimable:
                    self.log_msg.emit(f"Claiming {len(claimable)} completed reward(s)...")
                    claimed = self._task_manager.claim_all_completed(self._page)
                    if claimed > 0:
                        self.log_msg.emit(f"  Claimed {claimed} reward(s)")
                        missions = self._task_manager.refresh(page=self._page, ife=ife, force=True)

            inv = fetch_inventory(ife)
            best = self._task_manager.get_best_tasks(inventory_items=inv)
            if not best:
                self.log_msg.emit("No actionable tasks after assessment")
                return

            # Phase 2: Handle active multi-cycle tasks FIRST
            active_continued = 0
            if self._ai_planner:
                active_tasks = self._ai_planner.get_active_tasks()
                if active_tasks:
                    self.log_msg.emit(f"=== Continuing {len(active_tasks)} active task(s) ===")

                    for at in active_tasks:
                        if self._stop.is_set() or not self._farming_active:
                            break

                        # Find matching mission from wall
                        mission = None
                        for m in missions:
                            if (m.task_id == at.task_id or m.raw_title == at.title):
                                mission = m
                                break

                        if not mission:
                            self.log_msg.emit(f"  Active task not on wall: {at.title} — refreshing")
                            # Task might have rotated off, keep trying
                            at.attempt_count += 1
                            if at.attempt_count > 10:
                                self.log_msg.emit(f"  Task gone too long, removing: {at.title}")
                                del self._ai_planner._active_tasks[at.task_id]
                            continue

                        # Sync progress from game state
                        if mission.progress_current > at.completed_amount:
                            delta = mission.progress_current - at.completed_amount
                            self._ai_planner.update_progress(mission, delta)
                            at.completed_amount = mission.progress_current

                        if at.is_done:
                            self.log_msg.emit(f"  Task complete: {at.title}")
                            self._ai_planner.mark_task_completed(at.task_id, at.title)
                            continue

                        # Check resources
                        resource_check = self._ai_planner.check_resources(mission, inv)
                        self.log_msg.emit(f"  Active: {at.title} ({at.completed_amount}/{at.required_amount})")

                        # Get AI advice on what to do next
                        next_action = self._ai_planner.decide_next_action(mission, inv)
                        if next_action:
                            self.llm_thought.emit(f"[CONTINUE] {next_action}")

                        # Execute based on task type and resources
                        if mission.task_type == TaskType.FARMING:
                            success = self._continue_farming_task(ife, tracker, mission, at, inv, resource_check)
                        elif mission.task_type == TaskType.FEEDING:
                            success = self._continue_generic_task(ife, mission, at, resource_check)
                        elif mission.task_type == TaskType.COLLECTION:
                            success = self._continue_generic_task(ife, mission, at, resource_check)
                        else:
                            success = self._continue_generic_task(ife, mission, at, resource_check)

                        if success:
                            active_continued += 1
                            self._ai_planner.record_step(mission, f"Continued at {time.strftime('%H:%M')}")

                        if self._stop.is_set() or not self._farming_active:
                            break
                        self._interruptible_sleep(random.uniform(2, 4))

            # Phase 3: Process new tasks
            actionable = []
            for m in best:
                if m.is_done:
                    continue
                if m.can_claim:
                    continue
                if m.task_type == TaskType.EXTERNAL and self.config.get("task_wall_skip_external", True):
                    continue
                if m.feasibility_score < 1.0:
                    continue
                # Skip if already active
                if self._ai_planner:
                    task_key = m.task_id or m.raw_title
                    if task_key in self._ai_planner._active_tasks:
                        continue
                actionable.append(m)

            # AI Task Planning for new tasks
            if self._ai_planner and self._llm_engine and self._llm_engine.available:
                session_state = self._get_session_state()
                ai_plans = self._ai_planner.analyze_missions(actionable, inv, session_state)
                if ai_plans:
                    self.log_msg.emit(f"AI analyzed {len(ai_plans)} new tasks:")
                    for plan in ai_plans[:3]:
                        multi_tag = " [MULTI-CYCLE]" if plan.needs_multi_cycle else ""
                        self.log_msg.emit(f"  [P{plan.priority}] {plan.title} "
                                          f"({plan.estimated_time_min:.1f}min, "
                                          f"{plan.success_probability:.0%} success){multi_tag}")
                        self.llm_thought.emit(f"[PLAN] {plan.reasoning}")

                    strategy = self._ai_planner.recommend_strategy(actionable, session_state)
                    if strategy:
                        self.llm_thought.emit(f"[STRATEGY] {strategy}")

            self.log_msg.emit(f"Assessed {len(actionable)} new actionable tasks:")
            for m in actionable[:5]:
                reasons = ", ".join(m.feasibility_reasons[:2]) if m.feasibility_reasons else "N/A"
                self.log_msg.emit(f"  [{m.task_type.value}] {m.title} "
                                  f"({m.progress_current}/{m.progress_required}) "
                                  f"score={m.feasibility_score:.1f} — {reasons}")

            if not actionable and active_continued == 0:
                self.log_msg.emit("Nothing worth doing on the task wall right now")
                return

            max_simult = self.config.get("task_wall_max_simultaneous", 3)
            executed = 0
            for mission in actionable:
                if executed >= max_simult:
                    break
                if self._stop.is_set() or not self._farming_active:
                    break

                # Check if AI recommends skipping
                if self._ai_planner:
                    should_skip, skip_reason = self._ai_planner.should_skip_task(mission)
                    if should_skip:
                        self.log_msg.emit(f"  AI skip: {mission.title} — {skip_reason}")
                        continue

                # Check resources before starting
                if self._ai_planner and inv:
                    resource_check = self._ai_planner.check_resources(mission, inv)
                    if not resource_check["enough"]:
                        self.log_msg.emit(f"  Starting multi-cycle: {mission.title} — {resource_check['suggestion']}")
                        # Register as active task so we continue next cycle
                        at = self._ai_planner.register_active_task(mission, inv)
                        self.llm_thought.emit(f"[MULTI-CYCLE] Registered: {mission.title} "
                                              f"({at.completed_amount}/{at.required_amount})")
                        # Still try to do what we can this cycle
                        if mission.task_type == TaskType.FARMING:
                            self._continue_farming_task(ife, tracker, mission, at, inv, resource_check)
                        continue

                self.log_msg.emit(f"Executing: {mission.title}")
                if self._ai_planner:
                    self._ai_planner.mark_task_attempted(mission.task_id, mission.title)

                try:
                    result = execute_task(ife, tracker, self.config, mission, sleep_fn=self._make_sleep_fn(lambda: self._farming_active))
                    if isinstance(result, dict):
                        errors = result.get('errors', [])
                        if errors:
                            self.log_msg.emit(f"  Failed: {errors[0]}")
                            if self._ai_planner:
                                self._ai_planner.mark_task_failed(mission.task_id, mission.title, errors[0])
                        else:
                            executed += 1
                            self.log_msg.emit(f"  Done!")
                            self.task_completed.emit(mission.to_dict())
                            if self._ai_planner:
                                self._ai_planner.mark_task_completed(mission.task_id, mission.title)
                            if self._bridge:
                                self._bridge.update_status(
                                    last_task=f"done: {mission.title[:40]}"
                                )

                            if self.config.get("task_wall_claim_rewards", True) and self._page:
                                self._interruptible_sleep(random.uniform(1, 3))
                                refreshed = self._task_manager.refresh(page=self._page, ife=ife, force=True)
                                just_done = [m for m in refreshed if m.title == mission.title and m.can_claim]
                                if just_done:
                                    self.log_msg.emit(f"  Claiming reward for: {mission.title}")
                                    claimed = self._task_manager.claim_via_api(self._page, just_done[0])
                                    if claimed:
                                        self.log_msg.emit(f"  Reward claimed!")
                                    else:
                                        self.log_msg.emit(f"  Claim failed — will retry next cycle")
                except Exception as e:
                    self.log_msg.emit(f"  Error: {e}")

                if self._stop.is_set() or not self._farming_active:
                    break
                self._interruptible_sleep(random.uniform(3, 6))

            total_done = executed + active_continued
            if total_done > 0:
                self.log_msg.emit(f"Task cycle complete: {executed} new, {active_continued} continued")
                if self._ai_planner:
                    self.log_msg.emit(f"AI Context: {self._ai_planner.get_execution_summary()}")
                self._handle_task_refresh()

        except Exception as e:
            self.log_msg.emit(f"Task cycle error: {e}")

    def _continue_farming_task(self, ife, tracker, mission, active_task, inventory, resource_check):
        """Continue a farming task: plant available seeds, harvest ready crops."""
        from bot_engine import execute_plant_task, execute_harvest_task
        title_lower = mission.raw_title.lower()

        try:
            # If we have seeds, plant them
            if resource_check["have"] > 0:
                seed_match = re.search(r"plant\s+\d+\s+([\w\s]+?)\s+seeds?", title_lower)
                if seed_match:
                    needed_seed = seed_match.group(1).strip()
                    # Find the actual item code
                    seed_code = None
                    for item in inventory:
                        code = item.get("itemCode", "").lower()
                        if needed_seed.lower() in code and item.get("itemType") == "farmSeeds":
                            seed_code = item["itemCode"]
                            break

                    if seed_code:
                        # Plant as many as we can
                        to_plant = min(resource_check["have"], active_task.remaining)
                        self.log_msg.emit(f"  Planting {to_plant} {needed_seed} seeds...")
                        result = execute_plant_task(ife, tracker, self.config, seed_code, to_plant,
                                                    sleep_fn=self._make_sleep_fn(lambda: self._farming_active))
                        if isinstance(result, dict) and not result.get('errors'):
                            planted = len(result.get('planted', []))
                            if planted > 0:
                                self._ai_planner.update_progress(mission, planted)
                                self.log_msg.emit(f"  Planted {planted} — progress: "
                                                  f"{active_task.completed_amount}/{active_task.required_amount}")
                                return True

            # If no seeds but crops might be ready, try harvesting
            elif active_task.completed_amount < active_task.required_amount:
                self.log_msg.emit(f"  No seeds available — checking for harvestable crops...")
                harvest_result = execute_harvest_task(ife, tracker, self.config, count=None,
                                                      sleep_fn=self._make_sleep_fn(lambda: self._farming_active))
                if isinstance(harvest_result, dict):
                    harvested = harvest_result.get('harvested', 0)
                    if harvested > 0:
                        self.log_msg.emit(f"  Harvested {harvested} crops — will replant next cycle")
                        self._ai_planner.record_step(mission, f"Harvested {harvested} to get seeds")

            return False

        except Exception as e:
            self.log_msg.emit(f"  Farming continuation error: {e}")
            return False

    def _continue_generic_task(self, ife, mission, active_task, resource_check):
        """Continue a non-farming task (feeding, collection, etc.)."""
        try:
            if resource_check["enough"]:
                # Resources available, try executing
                result = execute_task(ife, None, self.config, mission,
                                      sleep_fn=self._make_sleep_fn(lambda: self._farming_active))
                if isinstance(result, dict) and not result.get('errors'):
                    self._ai_planner.update_progress(mission, 1)
                    self.log_msg.emit(f"  Progress: {active_task.completed_amount}/{active_task.required_amount}")
                    return True
            else:
                self.log_msg.emit(f"  Waiting for resources: {resource_check['suggestion']}")
                return False

        except Exception as e:
            self.log_msg.emit(f"  Task continuation error: {e}")
            return False

    def _get_session_state(self):
        """Get current session state for AI analysis."""
        return {
            "consecutive_errors": getattr(self, '_consecutive_errors', 0),
            "detection_risk": getattr(self, '_detection_risk', 'low'),
            "cycles_in_session": getattr(self, '_cycles_in_session', 0),
            "session_elapsed_h": getattr(self, '_session_start', 0),
        }

    def request_stop(self):
        self._stop.set()
        self._farming_active = False


    @Slot()
    def run(self):
        self._stop.clear()
        self._want_start_farming.clear()
        self._want_one_cycle.clear()
        self._want_seed_config.clear()
        self._want_task_refresh.clear()
        self._farming_active = False
        self._farming_active = False
        self.status_msg.emit("Launching browser...")
        try:
            if getattr(sys, "frozen", False):
                exe_dir = os.path.dirname(sys.executable)
                bundled_pw = os.path.join(exe_dir, "ms-playwright")
                if os.path.isdir(bundled_pw):
                    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = bundled_pw
                else:
                    ms_dir = os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local", "ms-playwright")
                    if os.path.isdir(ms_dir):
                        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = ms_dir
            else:
                ms_dir = os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local", "ms-playwright")
                if os.path.isdir(ms_dir):
                    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = ms_dir
            from playwright.sync_api import sync_playwright
            self._kill_orphan_chrome()
            with sync_playwright() as pw:
                self._pw = pw
                profile_dir = self.config.get("user_data_dir", "")
                if not profile_dir or "_MEI" in profile_dir:
                    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.getcwd()
                    profile_dir = os.path.join(base, "profile")
                    self.config["user_data_dir"] = profile_dir
                self.log_msg.emit(f"Using profile: {profile_dir}")
                headless = self.config.get("headless", False)
                self._ctx = pw.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=headless,
                    args=["--window-size=1280,900", "--disable-blink-features=AutomationControlled"],
                    ignore_default_args=["--enable-automation"],
                    no_viewport=True,
                )
                self._ctx.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                """)
                page = self._ctx.pages[0]
                self._page = page
                page.goto("https://chainers.io/game/farm")
                self.log_msg.emit("Browser ready â€” navigate to the farm game if not already there")
                self.status_msg.emit("Browser launched â€” log in if needed, then click Start Bot")
                self.state_changed.emit("launched")
                if self._bridge:
                    self._bridge.update_status(state="launched", vpn=self.config.get("vpn_state", "disconnected"))
                while not self._stop.is_set():
                    if self._want_start_farming.is_set():
                        self._want_start_farming.clear()
                        self._farming_active = True
                        self._run_bot_cycles(page)
                    if self._want_seed_config.is_set():
                        self._want_seed_config.clear()
                        self._handle_seed_config_request()
                    if self._want_one_cycle.is_set():
                        self._want_one_cycle.clear()
                        self._run_one_cycle()
                    if self._want_task_refresh.is_set():
                        self._want_task_refresh.clear()
                        self._handle_task_refresh()
                    if self._want_recipes.is_set():
                        self._want_recipes.clear()
                        self._handle_recipe_fetch()
                    if self._bridge:
                        vpn_req = self._bridge.check_vpn_request()
                        if vpn_req:
                            action, kwargs = vpn_req
                            result = self._handle_vpn_request(action, **kwargs)
                            self._bridge.complete_vpn_request(result)
                    self._stop.wait(0.3)
                self.log_msg.emit("Browser closed")
        except Exception as e:
            self.log_msg.emit(f"ERROR: {e}")
            import traceback
            for line in traceback.format_exc().split("\n"):
                if line.strip():
                    self.log_msg.emit(f"  {line}")
            self.status_msg.emit(f"Error: {e}")
            self.state_changed.emit("error")
        finally:
            if self._ctx:
                try:
                    self._ctx.close()
                except Exception:
                    pass
                self._ctx = None
            self._ife = None
            self.state_changed.emit("idle")
            if self._bridge:
                self._bridge.update_status(state="idle")

    def _run_bot_cycles(self, page):
        self.log_msg.emit("=== Starting bot ===")
        self.state_changed.emit("bot_running")
        if self._bridge:
            self._bridge.update_status(state="bot_running", cycle=0, harvested=0, planted=0, errors=0)
            self._bridge.reset_uptime()

        if not self._ensure_game_ready():
            self.status_msg.emit("Error: game init failed")
            self.state_changed.emit("launched")
            if self._bridge:
                self._bridge.update_status(state="launched", last_cycle_result="game init failed")
            return

        self.memory.optimize_config(self.config)

        def ife(script):
            if self._stop.is_set() or not self._farming_active:
                raise RuntimeError("Stop requested")
            if not self._ife:
                try:
                    self._reconnect_ife()
                except Exception as e:
                    self.log_msg.emit(f"Auto-reconnect failed: {e}")
                    raise
            try:
                return self._ife(script)
            except Exception as e:
                self.log_msg.emit(f"Connection lost ({e}) - reconnecting...")
                self._ife = None
                try:
                    self._reconnect_ife()
                except Exception as re:
                    raise RuntimeError(f"Reconnect after loss failed: {re}")
                return self._ife(script)

        summary = None
        for _init_attempt in range(3):
            try:
                self.log_msg.emit(f"Fetching garden data (attempt {_init_attempt + 1})...")
                summary = ife('''
                    let api = new API();
                    let g = (await api.get_user_gardens())._data;
                    return g.map(garden => ({
                        code: garden.code,
                        bed_count: (garden.placedBeds || []).length
                    }));
                ''')
                break
            except Exception as e:
                self.log_msg.emit(f"Garden data fetch failed: {e}")
                self._ife = None
                if _init_attempt < 2:
                    self.log_msg.emit("Retrying in 5s...")
                    self._stop.wait(5)
                else:
                    self.log_msg.emit("FATAL: Could not fetch garden data after 3 attempts")
                    self._farming_active = False
                    self.state_changed.emit("launched")
                    return
        for s in (summary if isinstance(summary, list) else []):
            self.log_msg.emit(f"  Garden '{s.get('code')}': {s.get('bed_count')} beds")

        try:
            self._handle_seed_config_request()
        except Exception as e:
            self.log_msg.emit(f"Seed config fetch failed (non-fatal): {e}")

        time.sleep(2)

        tracker = PlotTracker()
        cycle_count = 0

        session_start = None
        session_duration = 0

        session_state = {
            "session_elapsed_h": 0.0,
            "consecutive_errors": 0,
            "detection_risk": "low",
            "hours_since_last_detection": 999.0,
            "harvest_rate_trend": 0.0,
            "cycles_in_session": 0,
        }
        _last_detection_time = 0.0
        _session_cycle_harvests = []
        offline_until = None
        last_offline_check = time.time()
        consecutive_failures = 0
        max_failures = self.config.get("max_consecutive_failures", 10)
        self.log_msg.emit(f"Entering farming loop (max_failures={max_failures})")

        while self._farming_active and not self._stop.is_set():
            now = time.time()

            if not self._check_browser_alive():
                self.log_msg.emit("Browser not responding — attempting relaunch...")
                new_page = self._relaunch_browser(pw)
                if not new_page:
                    self.log_msg.emit("FATAL: Could not recover browser — stopping")
                    self._farming_active = False
                    break
                page = new_page

            check_interval = self.config.get("offline_check_interval_hours", 24) * 3600
            if now - last_offline_check >= check_interval:
                last_offline_check = now
                base_prob = self.config.get("offline_base_probability", 0.5)
                jitter = self.config.get("offline_probability_jitter", 0.2)
                actual_prob = random.uniform(max(0.0, base_prob - jitter), min(1.0, base_prob + jitter))
                self.log_msg.emit(f"Offline roll: probability {actual_prob:.1%}")
                if random.random() < actual_prob:
                    offline_hrs = self.config.get("offline_duration_hours", 24)
                    offline_until = now + offline_hrs * 3600
                    self.log_msg.emit(f"Going offline for {offline_hrs}h (random trigger)")

            if offline_until is not None:
                if now < offline_until:
                    remaining = offline_until - now
                    self.log_msg.emit(f"Offline â€” {remaining / 3600:.1f}h remaining")
                    self.status_msg.emit(f"Offline ({remaining / 3600:.1f}h)")
                    self._stop.wait(min(remaining, 60))
                    continue
                else:
                    offline_until = None
                    self.log_msg.emit("Offline period over, resuming")

            if session_start is None:
                choices = self.config.get("session_duration_choices", [2, 4, 6, 10])
                session_duration = random.choice(choices) * 3600
                session_start = now
                self.memory.start_session(self.config)
                self.log_msg.emit(f"New session started: {session_duration / 3600:.0f}h")

                # Event detection: check for active boost events
                detected = detect_active_events(self._page)
                if detected:
                    strategy = get_event_strategy(detected)
                    if strategy:
                        self.log_msg.emit(f"EVENTS: {strategy}")
                        self.llm_thought.emit(f"[EVENT] {strategy}")
                    for ev in detected:
                        self.log_msg.emit(f"  Event: {ev.get('name', '?')} ({ev.get('type', '?')})")

            elapsed = now - session_start
            if elapsed >= session_duration:
                break_min = self.config.get("session_break_min_mins", 30)
                break_max = self.config.get("session_break_max_mins", 360)
                break_sec = random.randint(break_min, break_max) * 60
                self.memory.finalize_session(self.config)
                self.log_msg.emit(f"Session ended ({session_duration / 3600:.0f}h). Break {break_sec / 60:.0f}m")
                self.status_msg.emit(f"Session break ({break_sec / 60:.0f}m)")
                self.memory_updated.emit()
                if self._sleep_with_health_check(break_sec, pw):
                    if self._stop.is_set():
                        self.log_msg.emit("Stop signal received during session break")
                    break
                session_start = None
                self.memory.generate_profiles()
                self.memory_updated.emit()
                continue

            session_state["session_elapsed_h"] = elapsed / 3600
            session_state["consecutive_errors"] = consecutive_failures
            session_state["cycles_in_session"] = cycle_count
            if _last_detection_time:
                session_state["hours_since_last_detection"] = (now - _last_detection_time) / 3600
            if _session_cycle_harvests:
                session_state["harvest_rate_trend"] = sum(_session_cycle_harvests[-5:]) / max(len(_session_cycle_harvests[-5:]), 1)

            ml_max_actions_override = None
            if self._ml_engine is not None and self._ml_engine.available:
                try:
                    ml_actions = self._ml_engine.predict_max_actions(self.config, session_state)
                    if ml_actions is not None and ml_actions > 0:
                        ml_max_actions_override = ml_actions
                except Exception:
                    pass

            if self._ml_engine is not None and hasattr(self._ml_engine, "_anomaly") and self._ml_engine._anomaly is not None:
                try:
                    from ml.anomaly import AnomalyDetector
                    is_anom, severity = self._ml_engine._anomaly.analyze(self.config, session_state)
                    if is_anom:
                        self.log_msg.emit(f"⚠ Anomaly detected: {severity} risk")
                        if self._llm_engine is not None and self._llm_engine.available:
                            self._llm_call("ANOMALY", self._llm_engine.explain_anomaly, session_state, severity, self.config)
                except Exception:
                    pass

            cycle_count += 1
            self.log_msg.emit(f"--- Cycle {cycle_count} ---")
            self.status_msg.emit(f"Cycle {cycle_count} (session {elapsed / 3600:.1f}/{session_duration / 3600:.0f}h)")

            try:
                results = run_bot_cycle(ife, tracker, self.config, sleep_fn=self._make_sleep_fn(lambda: self._farming_active), ml_max_actions=ml_max_actions_override)
                h = len(results["harvested"])
                p = len(results["planted"])
                e = len(results["errors"])
                self.log_msg.emit(f"Cycle {cycle_count}: harvested={h} planted={p} errors={e}")
                self.cycle_completed.emit(cycle_count)
                self.memory.log_cycle(results)

                if self.config.get("task_wall_enabled", True) and self._page and cycle_count % self.config.get("task_wall_check_every_n_cycles", 10) == 0:
                    try:
                        self._run_task_cycle(ife, tracker)
                    except Exception as te:
                        self.log_msg.emit(f"Task wall error: {te}")
                if self._bridge:
                    self._bridge.update_status(
                        cycle=cycle_count, harvested=h, planted=p, errors=e,
                        session_elapsed=f"{elapsed / 3600:.1f}h",
                        last_cycle_result=f"h={h} p={p} e={e}",
                    )
                harvest_events = [
                    hv["seed"] for hv in results.get("harvested", [])
                    if isinstance(hv, dict) and hv.get("seed")
                ]
                if harvest_events:
                    self.memory.log_harvest_batch(harvest_events)
                    roi = get_roi_tracker()
                    for hv in results.get("harvested", []):
                        if isinstance(hv, dict) and hv.get("seed"):
                            roi.record_harvest(hv.get("seed"), count=1)

                plant_events = [
                    (pl["seed"], pl.get("bed", "?"))
                    for pl in results.get("planted", [])
                    if isinstance(pl, dict) and pl.get("seed")
                ]
                if plant_events:
                    self.memory.log_plant_batch(plant_events)
                    roi = get_roi_tracker()
                    for pl in results.get("planted", []):
                        if isinstance(pl, dict) and pl.get("seed"):
                            roi.record_plant(pl.get("seed"))

                # Intentional mistakes: occasionally simulate human errors
                mistake_engine = get_mistake_engine()
                if mistake_engine.should_mistake("farming"):
                    mistake = mistake_engine.pick_mistake("farming")
                    self.log_msg.emit(f"  Human-like mistake: {mistake}")
                    if mistake == "missed_harvest" and harvest_events:
                        skipped = harvest_events.pop(0) if harvest_events else None
                        if skipped:
                            self.log_msg.emit(f"  Forgot to harvest: {skipped}")
                    elif mistake == "wrong_crop" and plant_events:
                        # Plant a suboptimal seed deliberately
                        self.log_msg.emit(f"  Planted a less optimal seed on purpose")
                    elif mistake == "pause_within":
                        pause = random.uniform(5, 30)
                        self.log_msg.emit(f"  Got distracted — pausing {pause:.0f}s")
                        self._interruptible_sleep(pause)
                    elif mistake == "double_click":
                        # Harmless — just log it
                        self.log_msg.emit(f"  Accidentally clicked twice")

                # ROI summary every 10 cycles
                if cycle_count % 10 == 0:
                    roi = get_roi_tracker()
                    summary = roi.get_roi_summary()
                    best = summary.get("top_seeds", [])
                    if best:
                        top = best[0]
                        self.log_msg.emit(f"ROI: Best seed = {top['key']} ({top['efficiency']:.1%} eff, "
                                          f"{top['harvested']} harvested/{top['planted']} planted)")
                        self.llm_thought.emit(f"[ROI] Top seed: {top['key']} with {top['efficiency']:.0%} efficiency")
                    pool_window = summary.get("pool_window", 600)
                    self.log_msg.emit(f"ROI: Optimal pool window = {pool_window}s after reset")
                consecutive_failures = 0
                _session_cycle_harvests.append(h)
                risk = "high" if e > 0 else "low"
                session_state["detection_risk"] = risk
            except Exception as ex:
                consecutive_failures += 1
                err_msg = f"Cycle error ({consecutive_failures}/{max_failures}): {ex}"
                self.log_msg.emit(err_msg)
                if "Target closed" in str(ex) or "Browser" in str(ex) or "Connection" in str(ex):
                    self.log_msg.emit("Browser crash detected — attempting relaunch...")
                    new_page = self._relaunch_browser(pw)
                    if new_page:
                        page = new_page
                        consecutive_failures = 0
                        self.log_msg.emit("Recovered from browser crash — continuing")
                    else:
                        self.log_msg.emit("Could not recover browser — stopping")
                        break
                if self._bridge:
                    self._bridge.update_status(errors=consecutive_failures, last_cycle_result=f"error: {ex}")
                if consecutive_failures >= max_failures:
                    self.memory.log_detection("flag", f"Too many consecutive failures: {consecutive_failures}", self.config)
                    _last_detection_time = time.time()
                    self.log_msg.emit(f"Too many consecutive failures ({consecutive_failures}/{max_failures}) â€” stopping bot")
                    self.status_msg.emit("Error: too many failures")
                    break

            if self._stop.is_set():
                self.log_msg.emit(f"Stop signal received after cycle {cycle_count}")
                break

            if self._want_seed_config.is_set():
                self._want_seed_config.clear()
                self._handle_seed_config_request()
            if self._want_one_cycle.is_set():
                self._want_one_cycle.clear()
                self._run_one_cycle()
            if self._want_task_refresh.is_set():
                self._want_task_refresh.clear()
                self._handle_task_refresh()

            tracker.cleanup(max_age_hours=48)

            if self._llm_engine is not None and self._llm_engine.available:
                self._llm_call("STRATEGY", self._llm_engine.get_strategy_advice, session_state, self.config)

            delay = None
            if self._ml_engine is not None and self._ml_engine.available:
                try:
                    ml_delay = self._ml_engine.predict_delay(self.config, session_state)
                    if ml_delay is not None:
                        delay = ml_delay
                except Exception:
                    pass
            if delay is None:
                delay = random.uniform(
                    self.config.get("cycle_delay_min", 90),
                    self.config.get("cycle_delay_max", 180),
                )
            self.log_msg.emit(f"Next cycle in {delay:.0f}s")
            self.status_msg.emit(f"Next cycle in {delay:.0f}s")
            if self._sleep_with_health_check(delay, pw):
                if self._stop.is_set():
                    self.log_msg.emit(f"Stop signal received during inter-cycle delay after cycle {cycle_count}")
                break
            else:
                self.log_msg.emit("Delay over, resuming")

        if session_start is not None:
            self.memory.finalize_session(self.config)
        self.memory.generate_profiles()

        old_cfg = dict(self.config)
        self.memory.optimize_config(self.config)
        changed = {k: f"{old_cfg[k]} -> {self.config[k]}" for k in self.config if k in old_cfg and old_cfg[k] != self.config[k]}
        if changed:
            save_config(self.config)
            self.log_msg.emit(f"Self-teach: optimized {len(changed)} setting(s)")
            for k, v in changed.items():
                self.log_msg.emit(f"  {k}: {v}")
        else:
            self.log_msg.emit("Self-teach: settings already optimal")
        self.memory_updated.emit()
        self.log_msg.emit(f"Bot farming loop ended after {cycle_count} cycle(s), stop_set={self._stop.is_set()}")
        self.status_msg.emit("Bot idle")
        self._farming_active = False
        self.state_changed.emit("launched")
        if self._bridge:
            self._bridge.update_status(state="launched", cycle=cycle_count)


class SeedConfigDialog(QDialog):
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Seed Configuration")
        self.setMinimumSize(500, 400)
        self.resize(620, 520)
        self._data = data
        self._seed_config = SeedConfig()
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        settings_row = QHBoxLayout()
        self._use_limited_cb = QCheckBox("Use limited quantity seeds")
        self._use_limited_cb.setToolTip("When unchecked, the bot will skip seeds with low stock counts")
        self._use_limited_cb.setChecked(self._seed_config.is_use_limited_seeds())
        settings_row.addWidget(self._use_limited_cb)

        self._threshold_label = QLabel("Skip seeds with count â‰¤")
        self._threshold_label.setStyleSheet("color: #888; font-size: 11px;")
        settings_row.addWidget(self._threshold_label)
        self._threshold_spin = QSpinBox()
        self._threshold_spin.setRange(1, 99)
        self._threshold_spin.setValue(self._seed_config.get_limited_threshold())
        self._threshold_spin.setFixedWidth(50)
        settings_row.addWidget(self._threshold_spin)
        self._threshold_spin.setEnabled(self._use_limited_cb.isChecked())
        self._threshold_label.setEnabled(self._use_limited_cb.isChecked())
        self._use_limited_cb.toggled.connect(self._threshold_spin.setEnabled)
        self._use_limited_cb.toggled.connect(self._threshold_label.setEnabled)
        settings_row.addStretch()
        layout.addLayout(settings_row)

        header = QLabel("Check seeds the bot may use per bed type.  Nothing checked = block this type.  Double-click a bed type to toggle all.")
        header.setWordWrap(True)
        header.setStyleSheet("color: #888; font-size: 11px; padding: 2px 0 6px 0;")
        layout.addWidget(header)

        if not self._data:
            placeholder = QLabel("Launch the browser first to see available seeds.\nSaved seed config preferences are shown below if any exist.")
            placeholder.setWordWrap(True)
            placeholder.setStyleSheet("color: #aaa; font-size: 12px; padding: 20px; border: 1px dashed #555; border-radius: 6px;")
            layout.addWidget(placeholder)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Item", "Count"])
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.setIndentation(20)
        self.tree.setAnimated(True)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)

        saved_config = self._seed_config.get_all()

        for garden in (self._data or []):
            gc = garden.get("code", "?")
            garden_item = QTreeWidgetItem([gc, ""])
            garden_item.setFlags(garden_item.flags() & ~Qt.ItemIsSelectable)
            font = garden_item.font(0)
            font.setBold(True)
            garden_item.setFont(0, font)

            for bt in garden.get("bed_types", []):
                bc = bt.get("itemCode", "?")
                cnt = str(bt.get("count", 0))
                type_item = QTreeWidgetItem([bc, cnt])
                type_item.setFlags(type_item.flags() & ~Qt.ItemIsSelectable)
                type_item.setData(0, Qt.UserRole, {"garden": gc, "bed_type": bc})

                configured = bt.get("configured_seeds", [])
                seeds_sorted = sorted(
                    bt.get("compatible_seeds", []),
                    key=lambda s: (-s.get("owned", 0), -s.get("rarity", 0)),
                )

                for seed in seeds_sorted:
                    sc = seed.get("code", "?")
                    rarity = seed.get("rarity", 0)
                    owned = seed.get("owned", 0)
                    label = f"{sc}  (rarity {rarity})"
                    if owned:
                        label += f"  â€” owned: {owned}"
                    else:
                        label += "  â€” none owned"
                    seed_item = QTreeWidgetItem([label, ""])
                    seed_item.setFlags(seed_item.flags() | Qt.ItemIsUserCheckable)
                    seed_item.setCheckState(0, Qt.Checked if sc in configured else Qt.Unchecked)
                    seed_item.setData(0, Qt.UserRole, {"garden": gc, "bed_type": bc, "seed_code": sc, "owned": owned})
                    if not owned:
                        seed_item.setForeground(0, QBrush(Qt.gray))
                    type_item.addChild(seed_item)

                garden_item.addChild(type_item)
            self.tree.addTopLevelItem(garden_item)

        if not self._data and saved_config:
            for gc, bed_types in saved_config.items():
                if gc == "_settings" or not isinstance(bed_types, dict):
                    continue
                garden_item = QTreeWidgetItem([gc, ""])
                garden_item.setFlags(garden_item.flags() & ~Qt.ItemIsSelectable)
                font = garden_item.font(0)
                font.setBold(True)
                garden_item.setFont(0, font)
                for bc, seeds in bed_types.items():
                    if not isinstance(seeds, list):
                        continue
                    type_item = QTreeWidgetItem([bc, "(saved)"])
                    type_item.setFlags(type_item.flags() & ~Qt.ItemIsSelectable)
                    type_item.setData(0, Qt.UserRole, {"garden": gc, "bed_type": bc, "saved": True})
                    for sc in seeds:
                        seed_item = QTreeWidgetItem([sc, ""])
                        seed_item.setFlags(seed_item.flags() | Qt.ItemIsUserCheckable)
                        seed_item.setCheckState(0, Qt.Checked)
                        seed_item.setData(0, Qt.UserRole, {"garden": gc, "bed_type": bc, "seed_code": sc, "owned": 0, "saved": True})
                        seed_item.setForeground(0, QBrush(Qt.gray))
                        type_item.addChild(seed_item)
                    garden_item.addChild(type_item)
                self.tree.addTopLevelItem(garden_item)

        self.tree.expandAll()
        layout.addWidget(self.tree)

        btn_row = QHBoxLayout()
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.clicked.connect(self._select_all)
        self.select_all_btn.setEnabled(bool(self._data))
        btn_row.addWidget(self.select_all_btn)
        self.select_owned_btn = QPushButton("Select Owned")
        self.select_owned_btn.clicked.connect(self._select_owned)
        self.select_owned_btn.setEnabled(bool(self._data))
        btn_row.addWidget(self.select_owned_btn)
        if not self._data:
            clear_btn = QPushButton("Clear Saved Config")
            clear_btn.clicked.connect(self._clear_saved)
            btn_row.addWidget(clear_btn)
        btn_row.addStretch()

        ok_cancel = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_cancel.accepted.connect(self._on_save)
        ok_cancel.rejected.connect(self.reject)
        btn_row.addWidget(ok_cancel)
        layout.addLayout(btn_row)

    def _on_item_double_clicked(self, item):
        if item.childCount() == 0:
            return
        checked_count = sum(1 for i in range(item.childCount()) if item.child(i).checkState(0) == Qt.Checked)
        new_state = Qt.Unchecked if checked_count == item.childCount() else Qt.Checked
        for i in range(item.childCount()):
            item.child(i).setCheckState(0, new_state)

    def _select_all(self):
        root_count = self.tree.topLevelItemCount()
        for gi in range(root_count):
            garden_item = self.tree.topLevelItem(gi)
            for ti in range(garden_item.childCount()):
                type_item = garden_item.child(ti)
                for si in range(type_item.childCount()):
                    seed_item = type_item.child(si)
                    seed_item.setCheckState(0, Qt.Checked)

    def _select_owned(self):
        root_count = self.tree.topLevelItemCount()
        for gi in range(root_count):
            garden_item = self.tree.topLevelItem(gi)
            for ti in range(garden_item.childCount()):
                type_item = garden_item.child(ti)
                for si in range(type_item.childCount()):
                    seed_item = type_item.child(si)
                    data = seed_item.data(0, Qt.UserRole)
                    owned = data.get("owned", 0) if data else 0
                    seed_item.setCheckState(0, Qt.Checked if owned > 0 else Qt.Unchecked)

    def _clear_saved(self):
        self._seed_config._data = {}
        self._seed_config.save()
        self.tree.clear()
        self.accept()

    def _on_save(self):
        self._seed_config.set_use_limited_seeds(self._use_limited_cb.isChecked())
        self._seed_config.set_limited_threshold(self._threshold_spin.value())

        config_map = {}
        root_count = self.tree.topLevelItemCount()
        for gi in range(root_count):
            garden_item = self.tree.topLevelItem(gi)
            gc = garden_item.text(0)
            for ti in range(garden_item.childCount()):
                type_item = garden_item.child(ti)
                bc = type_item.text(0)
                checked = []
                for si in range(type_item.childCount()):
                    seed_item = type_item.child(si)
                    if seed_item.checkState(0) == Qt.Checked:
                        data = seed_item.data(0, Qt.UserRole)
                        if not data:
                            continue
                        if "seed_code" not in data:
                            continue
                        checked.append(data["seed_code"])
                config_map.setdefault(gc, {})[bc] = checked

        self._seed_config.set_allowed_batch(config_map)
        self.accept()


class TimePicker(QWidget):
    """Spinbox with a minutes/hours combo.  value_in_hours is always in hours."""
    valueChanged = Signal()

    def __init__(self, min_val=0, max_val=9999, default=0, step=1, decimals=0, allow_sec=False, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        if decimals:
            self._spin = QDoubleSpinBox()
            self._spin.setDecimals(decimals)
            self._spin.setSingleStep(step)
        else:
            self._spin = QSpinBox()
            self._spin.setSingleStep(int(step))
        self._spin.setRange(min_val, max_val)
        self._spin.setValue(default)
        self._spin.setStyleSheet("font-size: 10pt; padding: 3px 6px; background: #0e1220; border: 1px solid #2a2f4a; border-radius: 4px; color: #ccc;")

        self._combo = QComboBox()
        self._combo.setStyleSheet("font-size: 10pt; padding: 2px 4px; background: #0e1220; border: 1px solid #2a2f4a; border-radius: 4px; color: #ccc;")
        self._combo.addItem("min")
        self._combo.addItem("h")
        if allow_sec:
            self._combo.insertItem(0, "s")

        layout.addWidget(self._spin)
        layout.addWidget(self._combo)

        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self._spin.valueChanged.connect(self.valueChanged)
        self._combo.currentIndexChanged.connect(self.valueChanged)

    def set_value_in_hours(self, hours):
        if hours >= 1:
            self._combo.setCurrentIndex(self._combo.findText("h"))
            self._spin.setValue(hours)
        else:
            self._combo.setCurrentIndex(self._combo.findText("min"))
            self._spin.setValue(hours * 60)

    def value_in_hours(self):
        unit = self._combo.currentText()
        v = self._spin.value()
        if unit == "s":
            return v / 3600
        if unit == "min":
            return v / 60
        return v

    def setEnabled(self, enabled):
        self._spin.setEnabled(enabled)
        self._combo.setEnabled(enabled)

    def setToolTip(self, tip):
        self._spin.setToolTip(tip)
        self._combo.setToolTip(tip)


class TasksPanel(QWidget):
    """Embeddable task wall panel showing missions and their status."""

    log_msg = Signal(str)
    refresh_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._missions = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        header_row = QHBoxLayout()
        self._title = QLabel("Task Wall")
        self._title.setStyleSheet("font-size: 11pt; font-weight: 700; color: #c8d0e8; padding: 2px 0;")
        header_row.addWidget(self._title)
        header_row.addStretch()
        self._count_label = QLabel("")
        self._count_label.setStyleSheet("color: #7a8aaa; font-size: 9pt;")
        header_row.addWidget(self._count_label)
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.setStyleSheet(
            "QPushButton { background: #1a2a4a; border: 1px solid #3a4a7a; border-radius: 3px; "
            "color: #8aa; font-size: 9pt; padding: 2px 8px; }"
            "QPushButton:hover { background: #2a3a6a; }"
        )
        self._refresh_btn.clicked.connect(self.refresh_requested.emit)
        header_row.addWidget(self._refresh_btn)
        layout.addLayout(header_row)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Task", "Progress", "Type", "Score", "Reward"])
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setExpandsOnDoubleClick(True)
        self._tree.header().setStretchLastSection(False)
        self._tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._tree.header().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._tree.header().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self._tree.setMinimumHeight(120)
        layout.addWidget(self._tree, 1)

        self._auto_check = QCheckBox("Auto-execute best tasks")
        self._auto_check.setChecked(True)
        self._auto_check.setStyleSheet(
            "QCheckBox { color: #7a8aaa; font-size: 9pt; }"
            "QCheckBox::indicator { width: 12px; height: 12px; }"
        )
        layout.addWidget(self._auto_check)

        status_row = QHBoxLayout()
        self._status_icon = QLabel("")
        self._status_icon.setStyleSheet("font-size: 14px; color: #888;")
        self._status_label = QLabel("No data yet â€” click Refresh or Start Bot")
        self._status_label.setStyleSheet("color: #6a7a9a; font-size: 9pt;")
        status_row.addWidget(self._status_icon)
        status_row.addWidget(self._status_label)
        status_row.addStretch()
        layout.addLayout(status_row)

    def update_missions(self, missions):
        if missions is None:
            self._status_label.setText("Failed to load task wall")
            self._status_icon.setStyleSheet("font-size: 14px; color: #f44336;")
            return

        self._missions = missions
        self._tree.clear()

        type_colors = {
            "farming": "#4caf50",
            "crafting": "#ff9800",
            "collection": "#2196f3",
            "feeding": "#e91e63",
            "external": "#777",
            "meta": "#9e9e9e",
            "unknown": "#666",
        }

        done_count = 0
        for m in missions:
            mtype = m.task_type.value if hasattr(m.task_type, 'value') else str(m.task_type)
            is_done = m.is_done
            if is_done:
                done_count += 1

            progress_str = f"{m.progress_current} / {m.progress_required}"
            score_str = f"{m.feasibility_score:.1f}" if m.feasibility_score else "-"
            reward_str = f"{m.reward_count}x" if m.reward_count else "-"

            item = QTreeWidgetItem([
                m.title or "(unknown)",
                progress_str,
                mtype,
                score_str,
                reward_str,
            ])

            color = type_colors.get(mtype, "#666")
            if is_done:
                item.setForeground(0, QBrush(QColor("#555")))
                item.setForeground(1, QBrush(QColor("#4caf50")))
            else:
                score = m.feasibility_score
                if score >= 5:
                    item.setForeground(3, QBrush(QColor("#4caf50")))
                elif score >= 2:
                    item.setForeground(3, QBrush(QColor("#ff9800")))
                else:
                    item.setForeground(3, QBrush(QColor("#f44336")))

            item.setForeground(2, QBrush(QColor(color)))
            item.setData(0, Qt.UserRole, m)
            self._tree.addTopLevelItem(item)

        total = len(missions)
        self._count_label.setText(f"{done_count}/{total} done")
        if total > 0:
            self._status_icon.setStyleSheet("font-size: 14px; color: #4caf50;")
            self._status_label.setText(f"{total} tasks loaded")
        else:
            self._status_icon.setStyleSheet("font-size: 14px; color: #888;")
            self._status_label.setText("No tasks found")


class UnchainedWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("UNCHAINED")
        self.setMinimumSize(900, 600)
        self.resize(1100, 720)

        self.config = load_config()
        self._build_ui()
        self._connect_signals()

        self._bot_thread = None
        self._bot_worker = None
        self._seed_config_cache = None

        self._discord_bridge = BotBridge()
        self._discord_bridge.set_vpn_manager(self.vpn_panel.get_manager())
        self._discord_bridge.set_llm_engine(self._llm_engine)
        self._webhook = DiscordWebhook(self.config.get("discord_webhook_url", ""))
        self._discord_bot = DiscordBot(
            self.config.get("discord_token", ""),
            self.config.get("discord_channel_id", 0),
            self._discord_bridge,
        )
        if self._discord_bot.configured:
            self._discord_bot.start()

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        central.setStyleSheet("""
            QWidget#central {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #0a0c14, stop:0.5 #111827, stop:1 #0f0a18);
            }
        """)
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(8, 4, 8, 6)
        outer.setSpacing(4)

        btn_flat = """
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #2a3a6a, stop:1 #1a2a4a);
                border: 1px solid #3a4a7a;
                border-radius: 4px;
                color: #c8d0e8;
                font-size: 10pt;
                font-weight: 600;
                padding: 6px 14px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #3a4a8a, stop:1 #2a3a6a);
                border: 1px solid #5a6a9a;
            }
            QPushButton:disabled {
                background: #1a1e2e;
                color: #555;
                border: 1px solid #2a2e3e;
            }
        """

        # --- toolbar row 1: launch + settings ---
        topbar = QWidget()
        topbar.setStyleSheet("background: transparent;")
        tb1 = QHBoxLayout(topbar)
        tb1.setContentsMargins(0, 0, 0, 0)
        tb1.setSpacing(6)

        title = QLabel("UNCHAINED")
        title.setStyleSheet("font-size: 14pt; font-weight: 800; color: #c8d0e8; letter-spacing: 2px; padding: 0 4px;")
        tb1.addWidget(title)

        self.launch_btn = QPushButton("Launch Browser")
        self.launch_btn.setStyleSheet(btn_flat)
        tb1.addWidget(self.launch_btn)

        tb1.addStretch()

        self.vpn_btn = QPushButton("VPN")
        self.discord_btn = QPushButton("Discord")
        self.discord_btn.setEnabled(True)
        self.ml_check = QCheckBox("ML")
        self.ml_check.setChecked(self.config.get("ml_enabled", True))
        self.ml_check.setStyleSheet("""
            QCheckBox { color: #7a8aaa; font-size: 9pt; font-weight: 600; padding: 2px 4px; }
            QCheckBox::indicator { width: 14px; height: 14px; }
        """)
        self.settings_btn = QPushButton("Settings")

        for b in (self.vpn_btn, self.discord_btn, self.settings_btn):
            b.setStyleSheet(btn_flat)
            tb1.addWidget(b)
        tb1.addWidget(self.ml_check)

        if not self._is_discord_configured():
            self.discord_btn.setStyleSheet(btn_flat.replace("#c8d0e8", "#6a6a8a"))

        outer.addWidget(topbar)

        # --- toolbar row 2: farming vs tasks ---
        modebar = QWidget()
        modebar.setStyleSheet("background: transparent;")
        tb2 = QHBoxLayout(modebar)
        tb2.setContentsMargins(0, 0, 0, 0)
        tb2.setSpacing(6)

        farm_label = QLabel("FARMING")
        farm_label.setStyleSheet("font-size: 9pt; font-weight: 700; color: #5a8a5a; padding: 2px 4px;")
        tb2.addWidget(farm_label)

        self.start_btn = QPushButton("Start Bot")
        self.start_btn.setEnabled(False)
        self.stop_btn = QPushButton("Stop Bot")
        self.stop_btn.setEnabled(False)
        self.one_cycle_btn = QPushButton("1 Cycle")
        self.one_cycle_btn.setEnabled(False)
        self.seed_config_btn = QPushButton("Seed Config")

        for b in (self.start_btn, self.stop_btn, self.one_cycle_btn, self.seed_config_btn):
            b.setStyleSheet(btn_flat)
            tb2.addWidget(b)

        tb2.addStretch()

        vault_label = QLabel("vault")
        vault_label.setStyleSheet("font-size: 8pt; color: #6a7a9a; padding: 0 2px;")
        tb2.addWidget(vault_label)

        outer.addWidget(modebar)

        # --- central splitter: brain (top) | terminal + ai console (bot) ---
        splitter = QSplitter(Qt.Vertical)

        from brain_viewer import BrainViewer
        self.brain_viewer = BrainViewer()
        self.brain_viewer.setMinimumHeight(100)
        self.brain_viewer.setMaximumHeight(250)
        splitter.addWidget(self.brain_viewer)

        bottom_splitter = QSplitter(Qt.Horizontal)

        log_card = QFrame()
        log_card.setStyleSheet("""
            QFrame {
                background: rgba(18, 22, 36, 200);
                border: 1px solid #2a2f4a;
                border-radius: 4px;
                padding: 2px;
            }
        """)
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(4, 2, 4, 2)
        log_layout.setSpacing(1)

        log_header = QLabel("TERMINAL")
        log_header.setStyleSheet("font-size: 8pt; font-weight: 700; color: #5a6a8a; letter-spacing: 1px;")
        log_layout.addWidget(log_header)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 9))
        self.log_view.setStyleSheet("""
            QTextEdit {
                background: rgba(0,0,0,80);
                border: none;
                color: #9aaac0;
                padding: 2px;
            }
        """)
        log_layout.addWidget(self.log_view)
        bottom_splitter.addWidget(log_card)

        ai_card = QFrame()
        ai_card.setStyleSheet("""
            QFrame {
                background: rgba(14, 20, 36, 200);
                border: 1px solid #2a3a5a;
                border-radius: 4px;
                padding: 2px;
            }
        """)
        ai_layout = QVBoxLayout(ai_card)
        ai_layout.setContentsMargins(4, 2, 4, 2)
        ai_layout.setSpacing(1)

        ai_header = QLabel("AI CONSOLE")
        ai_header.setStyleSheet("font-size: 8pt; font-weight: 700; color: #4a9a6a; letter-spacing: 1px;")
        ai_layout.addWidget(ai_header)

        self.ai_view = QTextEdit()
        self.ai_view.setReadOnly(True)
        self.ai_view.setFont(QFont("Consolas", 9))
        self.ai_view.setStyleSheet("""
            QTextEdit {
                background: rgba(0,0,0,80);
                border: none;
                color: #7ac09a;
                padding: 2px;
            }
        """)
        ai_layout.addWidget(self.ai_view)
        bottom_splitter.addWidget(ai_card)

        bottom_splitter.setSizes([400, 400])
        splitter.addWidget(bottom_splitter)
        splitter.setSizes([200, 320])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setMaximumHeight(550)
        outer.addWidget(splitter)

        # --- panels ---
        self.tasks_panel = TasksPanel()
        self.tasks_panel.setVisible(False)
        self.tasks_panel.setMaximumHeight(250)
        outer.addWidget(self.tasks_panel)

        self.vpn_panel = VPNPanel()
        self.vpn_panel.setVisible(False)
        self.vpn_panel.setMaximumHeight(250)
        outer.addWidget(self.vpn_panel)

        self.status_label = QLabel("Status: Idle")
        self.status_label.setStyleSheet("""
            font-size: 9pt; color: #7a8aaa;
            background: rgba(18,22,36,160);
            border: 1px solid #2a2f4a;
            border-radius: 4px;
            padding: 3px 8px;
        """)
        outer.addWidget(self.status_label)

    def _connect_signals(self):
        self.launch_btn.clicked.connect(self._launch_browser)
        self.start_btn.clicked.connect(self._start_farming)
        self.stop_btn.clicked.connect(self._stop_farming)
        self.seed_config_btn.clicked.connect(self._open_seed_config)
        self.one_cycle_btn.clicked.connect(self._run_one_cycle)
        self.vpn_btn.clicked.connect(self._toggle_vpn_panel)
        self.discord_btn.clicked.connect(self._toggle_discord)
        self.settings_btn.clicked.connect(self._open_settings)
        self.ml_check.stateChanged.connect(self._on_ml_toggle)
        log_bridge.message.connect(self._append_log)

        self._ml_engine = MLEngine()
        if self.config.get("ml_enabled", True):
            self._ml_engine.ensure_trained(self.config)
        self.brain_viewer.set_ml_engine(self._ml_engine)
        self.brain_viewer.set_ml_mode(self.ml_check.isChecked())

        self._llm_engine = None
        if self.config.get("llm_enabled", True):
            try:
                self._llm_engine = LLMEngine(model=self.config.get("llm_model", "phi3:mini"))
                if self._llm_engine.available:
                    log_bridge.message.emit("LLM engine: connected")
                else:
                    log_bridge.message.emit("LLM engine: Ollama not available (will retry)")
            except Exception as e:
                log_bridge.message.emit(f"LLM engine init failed: {e}")

    def _is_discord_configured(self):
        return bool(self.config.get("discord_webhook_url")) or (
            self.config.get("discord_token") and self.config.get("discord_channel_id")
        )

    def _on_ml_toggle(self, state):
        enabled = bool(state)
        self.brain_viewer.set_ml_mode(enabled)
        self.config["ml_enabled"] = enabled

    def _toggle_vpn_panel(self):
        visible = not self.vpn_panel.isVisible()
        self.vpn_panel.setVisible(visible)
        if visible:
            self.vpn_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #3a5a8a, stop:1 #2a3a5a);
                border: 1px solid #5a7aaa;
                border-radius: 4px;
                color: #c8d0e8;
                font-size: 10pt;
                font-weight: 600;
                padding: 5px 12px;
            }
        """)
        else:
            self.vpn_btn.setStyleSheet("")

    def _toggle_discord(self):
        if self._discord_bot._running:
            self._discord_bot.stop()
            self._append_log("Discord bot stopped")
            self.discord_btn.setText("Discord")
            return

        if not self._is_discord_configured():
            self._show_discord_setup_dialog()
            return

        self._discord_bot.configure(
            self.config.get("discord_token", ""),
            self.config.get("discord_channel_id", 0),
        )
        self._webhook.configure(self.config.get("discord_webhook_url", ""))
        self._discord_bot.start()
        self._append_log("Discord bot started")
        self.discord_btn.setText("Discord ON")

    def _show_discord_setup_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Discord Setup")
        dlg.setMinimumWidth(420)
        dlg.setStyleSheet("""
            QDialog { background: #111827; }
            QLabel { color: #9aabca; font-size: 10pt; }
            QLineEdit { font-size: 10pt; padding: 4px 6px; background: #0e1220;
                        border: 1px solid #2a2f4a; border-radius: 4px; color: #ccc; }
        """)

        layout = QVBoxLayout(dlg)
        layout.setSpacing(10)

        heading = QLabel("Enter your Discord credentials to enable remote control:")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        form = QFormLayout()
        form.setSpacing(8)

        token_edit = QLineEdit()
        token_edit.setPlaceholderText("Bot token from https://discord.com/developers/applications")
        token_edit.setEchoMode(QLineEdit.Password)
        token_edit.setStyleSheet("font-size: 10pt; padding: 4px 6px; background: #0e1220; border: 1px solid #2a2f4a; border-radius: 4px; color: #ccc;")
        form.addRow("Bot token:", token_edit)

        channel_edit = QLineEdit()
        channel_edit.setPlaceholderText("Channel ID (right-click channel â†’ Copy ID)")
        channel_edit.setStyleSheet("font-size: 10pt; padding: 4px 6px; background: #0e1220; border: 1px solid #2a2f4a; border-radius: 4px; color: #ccc;")
        form.addRow("Channel ID:", channel_edit)

        webhook_edit = QLineEdit()
        webhook_edit.setPlaceholderText("Webhook URL (optional, for notifications)")
        webhook_edit.setStyleSheet("font-size: 10pt; padding: 4px 6px; background: #0e1220; border: 1px solid #2a2f4a; border-radius: 4px; color: #ccc;")
        form.addRow("Webhook URL:", webhook_edit)

        layout.addLayout(form)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet("""
            QPushButton { background: #2a2a3a; border: 1px solid #3a3a5a; border-radius: 4px;
                          color: #aaa; padding: 6px 16px; font-size: 10pt; }
            QPushButton:hover { background: #3a3a4a; }
        """)
        save_btn = QPushButton("Save & Start")
        save_btn.setStyleSheet("""
            QPushButton { background: #2a4a3a; border: 1px solid #3a6a4a; border-radius: 4px;
                          color: #afa; padding: 6px 16px; font-size: 10pt; font-weight: 600; }
            QPushButton:hover { background: #3a5a4a; }
        """)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        def _save():
            token = token_edit.text().strip()
            ch = channel_edit.text().strip()
            wh = webhook_edit.text().strip()
            if not token:
                token_edit.setStyleSheet("font-size: 10pt; padding: 4px 6px; background: #0e1220; border: 1px solid #8a3a3a; border-radius: 4px; color: #ccc;")
                return
            self.config["discord_token"] = token
            try:
                self.config["discord_channel_id"] = int(ch) if ch else 0
            except ValueError:
                self.config["discord_channel_id"] = 0
            self.config["discord_webhook_url"] = wh
            save_config(self.config)
            self._discord_bot.configure(token, self.config["discord_channel_id"])
            self._webhook.configure(wh)
            self._discord_bot.start()
            self._append_log("Discord bot started")
            self.discord_btn.setText("Discord ON")
            self.discord_btn.setStyleSheet("")
            dlg.accept()

        save_btn.clicked.connect(_save)
        cancel_btn.clicked.connect(dlg.reject)
        dlg.exec()

    def _open_settings(self):
        dlg = SettingsDialog(self.config, self, bot_worker=self._bot_worker)
        if dlg.exec():
            self._append_log("Settings saved")
            for k, v in dlg.result_config.items():
                self.config[k] = v
            self._discord_bot.configure(
                self.config.get("discord_token", ""),
                self.config.get("discord_channel_id", 0),
            )
            self._webhook.configure(self.config.get("discord_webhook_url", ""))
            if self._discord_bot.configured and not self._discord_bot._running:
                self._discord_bot.start()
                self.discord_btn.setText("Discord ON")
                self._append_log("Discord bot started")
            elif not self._discord_bot.configured and self._discord_bot._running:
                self._discord_bot.stop()
                self.discord_btn.setText("Discord")
                self._append_log("Discord bot stopped")

    def _append_log(self, msg):
        timestamp = time.strftime("%H:%M:%S")
        self.log_view.append(f"[{timestamp}] {msg}")
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.log_view.setTextCursor(cursor)

    def _append_ai(self, msg):
        timestamp = time.strftime("%H:%M:%S")
        self.ai_view.append(f"[{timestamp}] {msg}")
        cursor = self.ai_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.ai_view.setTextCursor(cursor)

    def _launch_browser(self):
        if self._bot_thread and self._bot_thread.isRunning():
            self._append_log("Already running")
            return

        ml_engine = getattr(self, "_ml_engine", None)
        llm_engine = getattr(self, "_llm_engine", None)
        self._bot_worker = BotWorker(self.config, ml_engine=ml_engine, llm_engine=llm_engine, bridge=self._discord_bridge)
        self._discord_bridge.set_events(
            one_cycle_event=self._bot_worker._want_one_cycle,
            stop_event=self._bot_worker._stop,
        )

        self._bot_worker.log_msg.connect(self._append_log)
        self._bot_worker.status_msg.connect(lambda s: self.status_label.setText(f"Status: {s}"))
        self._bot_worker.state_changed.connect(self._on_state_changed)
        self._bot_worker.seed_config_data_ready.connect(self._on_seed_config_data)
        self._bot_worker.memory_updated.connect(lambda: self.brain_viewer.rebuild())
        self._bot_worker.cycle_completed.connect(self._on_cycle_completed)
        self._bot_worker.task_wall_updated.connect(self.tasks_panel.update_missions)
        self._bot_worker.task_completed.connect(self._on_task_completed)
        self._bot_worker.llm_thought.connect(self._append_ai)
        self.tasks_panel.refresh_requested.connect(self._bot_worker.request_task_refresh)

        self._bot_thread = QThread()
        self._bot_worker.moveToThread(self._bot_thread)
        self._bot_thread.started.connect(self._bot_worker.run)
        self._bot_thread.start()

        self.launch_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._append_log("Launching browser...")

    def _start_farming(self):
        if self._bot_worker:
            self._bot_worker.request_start_farming()
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self._append_log("Starting bot (farming + missions)...")

    def _stop_farming(self):
        if self._bot_worker:
            self._bot_worker.request_stop_farming()
            self._append_log("Stopping bot...")

    def _open_seed_config(self):
        if self._bot_worker:
            self.status_label.setText("Status: Loading seed data...")
            self.seed_config_btn.setEnabled(False)
            self.seed_config_btn.setText("Loading...")
            self._bot_worker.request_seed_config_data()
        else:
            dlg = SeedConfigDialog([], self)
            dlg.exec()

    def _run_one_cycle(self):
        if self._bot_worker:
            self._bot_worker.request_one_cycle()

    def _on_cycle_completed(self, cycle_count):
        try:
            if self.vpn_panel.auto_rotate_enabled() and cycle_count > 0 and cycle_count % self.vpn_panel.auto_rotate_interval() == 0:
                ok, msg = self.vpn_panel.rotate_now()
                if ok:
                    self._append_log(f"VPN rotated: {msg}")
                else:
                    self._append_log(f"VPN rotate failed: {msg}")
                self._discord_bridge.update_status(vpn="connected" if ok else "error")

            if self._discord_bot.configured or self._webhook.configured:
                s = self._discord_bridge.get_status()
                elapsed = s.get("session_elapsed", "?")
                errors = s.get("errors", 0)
                if self.config.get("discord_notify_cycles", True):
                    self._webhook.notify_cycle(cycle_count, s.get("harvested", 0), s.get("planted", 0), errors, elapsed)
                    self._discord_bot.notify_cycle(cycle_count, s.get("harvested", 0), s.get("planted", 0), errors, elapsed)
        except Exception as e:
            self._append_log(f"Warning: cycle_completed handler error: {e}")

    def _on_task_completed(self, task_dict):
        try:
            title = task_dict.get("title", "unknown")
            self._append_log(f"Task completed: {title}")
            if self._discord_bot.configured or self._webhook.configured:
                self._webhook.send_embed(
                    title="Task Completed",
                    description=title,
                    color=4890367,
                )
        except Exception as e:
            self._append_log(f"Warning: task_completed handler error: {e}")

    def _on_seed_config_data(self, data):
        self.seed_config_btn.setText("Seed Config")
        self.seed_config_btn.setEnabled(True)
        if data is None:
            self.status_label.setText("Status: Failed to load seed data")
            return
        self._seed_config_cache = data
        self._show_seed_config_dialog(data)

    def _show_seed_config_dialog(self, data):
        dlg = SeedConfigDialog(data, self)
        if dlg.exec():
            self.status_label.setText("Status: Seed config saved")
        else:
            self.status_label.setText("Status: Seed config cancelled")

    def _on_state_changed(self, state):
        if state == "idle":
            self.launch_btn.setEnabled(True)
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
            self.one_cycle_btn.setEnabled(False)
            if self._bot_thread:
                self._bot_thread.quit()
                self._bot_thread.wait(3000)
                self._bot_thread = None
                self._bot_worker = None
            self.brain_viewer.rebuild()
        elif state == "launched":
            self.launch_btn.setEnabled(False)
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.one_cycle_btn.setEnabled(True)
        elif state == "bot_running":
            self.launch_btn.setEnabled(False)
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.one_cycle_btn.setEnabled(True)
        elif state == "error":
            self.launch_btn.setEnabled(True)
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
            self.one_cycle_btn.setEnabled(False)
            self.brain_viewer.rebuild()

    def closeEvent(self, event):
        if self._bot_worker:
            self._bot_worker.request_stop()
        if self._bot_thread and self._bot_thread.isRunning():
            self._bot_thread.quit()
            if not self._bot_thread.wait(3000):
                self._bot_thread.terminate()
                self._bot_thread.wait(2000)
        self._bot_thread = None
        self._bot_worker = None
        if hasattr(self, '_discord_bot'):
            self._discord_bot.stop()
        if hasattr(self, '_webhook'):
            self._webhook.close()
        event.accept()


class SettingsDialog(QDialog):
    def __init__(self, config, parent=None, bot_worker=None):
        super().__init__(parent)
        self.setWindowTitle("Bot Settings")
        self.setMinimumSize(580, 480)
        self.resize(620, 720)
        self._bot_worker = bot_worker
        self.setStyleSheet("""
            QDialog { background: #111827; }
            QLabel { font-size: 10pt; color: #9aabca; }
            QGroupBox { font-size: 10pt; font-weight: 700; color: #7a8aaa;
                         border: 1px solid #2a2f4a; border-radius: 6px;
                         margin-top: 12px; padding: 18px 10px 8px 10px; }
            QGroupBox::title { color: #c8d0e8; padding: 0 4px; }
            QScrollArea { border: none; }
            QTabWidget::pane { border: 1px solid #2a2f4a; border-radius: 4px; background: #111827; }
            QTabBar::tab { background: #1a1f3a; color: #7a8aaa; padding: 8px 16px;
                           border: 1px solid #2a2f4a; border-bottom: none; border-radius: 4px 4px 0 0;
                           margin-right: 2px; font-size: 10pt; }
            QTabBar::tab:selected { background: #111827; color: #c8d0e8; font-weight: 700; }
            QTabBar::tab:hover { background: #2a2f5a; color: #9aabca; }
            QListWidget { background: #0e1220; border: 1px solid #2a2f4a; border-radius: 4px;
                          color: #c8d0e8; font-size: 10pt; }
            QListWidget::item { padding: 4px 8px; }
            QListWidget::item:selected { background: #2a3a6a; }
        """)
        self._config = dict(config)
        self.result_config = {}
        self._recipe_list_items = []
        self._build_ui()

    def _build_ui(self):
        tabs = QTabWidget()
        tabs.setStyleSheet("QTabWidget::pane { padding: 4px; }")

        # ── General tab ────────────────────────────────────────────────
        gen_scroll = QScrollArea()
        gen_scroll.setWidgetResizable(True)
        gen_scroll.setFrameShape(QFrame.NoFrame)
        gen_inner = QWidget()
        gen_layout = QVBoxLayout(gen_inner)
        gen_layout.setContentsMargins(12, 6, 12, 6)
        gen_layout.setSpacing(8)

        delays = QGroupBox("Cycle & Action Delays")
        dg = QFormLayout(delays)
        dg.setSpacing(6)
        dg.setContentsMargins(8, 4, 8, 4)
        dg.setLabelAlignment(Qt.AlignRight)

        self.action_min_spin = QDoubleSpinBox()
        self.action_min_spin.setRange(1, 60)
        self.action_min_spin.setValue(self._config.get("action_delay_min", 6))
        self.action_min_spin.setSuffix(" s")
        self.action_min_spin.setStyleSheet(_spin_style())
        dg.addRow("Action min:", self.action_min_spin)

        self.action_max_spin = QDoubleSpinBox()
        self.action_max_spin.setRange(1, 60)
        self.action_max_spin.setValue(self._config.get("action_delay_max", 14))
        self.action_max_spin.setSuffix(" s")
        self.action_max_spin.setStyleSheet(_spin_style())
        dg.addRow("Action max:", self.action_max_spin)

        self.max_actions_spin = QSpinBox()
        self.max_actions_spin.setRange(0, 20)
        self.max_actions_spin.setValue(self._config.get("max_actions_per_cycle", 0))
        self.max_actions_spin.setToolTip("0 = use default random distribution (1-4)")
        self.max_actions_spin.setStyleSheet(_spin_style())
        dg.addRow("Max actions/cycle:", self.max_actions_spin)

        self.cycle_min_spin = QDoubleSpinBox()
        self.cycle_min_spin.setRange(5, 3600)
        self.cycle_min_spin.setValue(self._config.get("cycle_delay_min", 90))
        self.cycle_min_spin.setSuffix(" s")
        self.cycle_min_spin.setSingleStep(10)
        self.cycle_min_spin.setStyleSheet(_spin_style())
        dg.addRow("Cycle min:", self.cycle_min_spin)

        self.cycle_max_spin = QDoubleSpinBox()
        self.cycle_max_spin.setRange(5, 3600)
        self.cycle_max_spin.setValue(self._config.get("cycle_delay_max", 180))
        self.cycle_max_spin.setSuffix(" s")
        self.cycle_max_spin.setSingleStep(10)
        self.cycle_max_spin.setStyleSheet(_spin_style())
        dg.addRow("Cycle max:", self.cycle_max_spin)

        gen_layout.addWidget(delays)

        cd = QGroupBox("Cooldowns & Rotation")
        cg = QFormLayout(cd)
        cg.setSpacing(6)
        cg.setContentsMargins(8, 4, 8, 4)
        cg.setLabelAlignment(Qt.AlignRight)

        self.cooldown_picker = TimePicker(min_val=1, max_val=168, default=24)
        self.cooldown_picker.set_value_in_hours(self._config.get("cooldown_hours", 24))
        self.cooldown_picker.setToolTip("How long before the same bed can be interacted with again")
        cg.addRow("Bed cooldown:", self.cooldown_picker)

        self.seed_rotation_picker = TimePicker(min_val=1, max_val=48, default=6)
        self.seed_rotation_picker.set_value_in_hours(self._config.get("seed_bed_rotation_hours", 6))
        self.seed_rotation_picker.setToolTip("How long before the same seed can be planted in the same bed again")
        cg.addRow("Seed re-plant delay:", self.seed_rotation_picker)

        gen_layout.addWidget(cd)

        sb = QGroupBox("Sandbagging")
        sg = QFormLayout(sb)
        sg.setSpacing(6)
        sg.setContentsMargins(8, 4, 8, 4)
        sg.setLabelAlignment(Qt.AlignRight)

        self.sandbagging_check = QCheckBox("Avoid best combos")
        self.sandbagging_check.setChecked(self._config.get("sandbagging_enabled", True))
        sg.addRow("", self.sandbagging_check)

        self.sandbagging_chance_spin = QDoubleSpinBox()
        self.sandbagging_chance_spin.setRange(0.0, 1.0)
        self.sandbagging_chance_spin.setSingleStep(0.05)
        self.sandbagging_chance_spin.setValue(self._config.get("sandbagging_avoid_best_chance", 0.4))
        self.sandbagging_chance_spin.setStyleSheet(_spin_style())
        sg.addRow("Chance:", self.sandbagging_chance_spin)

        gen_layout.addWidget(sb)

        so = QGroupBox("Sessions & Offline")
        sog = QFormLayout(so)
        sog.setSpacing(6)
        sog.setContentsMargins(8, 4, 8, 4)
        sog.setLabelAlignment(Qt.AlignRight)

        self.session_durations_edit = QLineEdit()
        cur = ", ".join(str(x) for x in self._config.get("session_duration_choices", [2, 4, 6, 10]))
        self.session_durations_edit.setText(cur)
        self.session_durations_edit.setStyleSheet(_line_style())
        sog.addRow("Session hrs (csv):", self.session_durations_edit)

        self.break_min_picker = TimePicker(min_val=5, max_val=1440, default=30)
        self.break_min_picker.set_value_in_hours(self._config.get("session_break_min_mins", 30) / 60)
        sog.addRow("Break min:", self.break_min_picker)

        self.break_max_picker = TimePicker(min_val=10, max_val=4320, default=360)
        self.break_max_picker.set_value_in_hours(self._config.get("session_break_max_mins", 360) / 60)
        sog.addRow("Break max:", self.break_max_picker)

        self.offline_prob_spin = QDoubleSpinBox()
        self.offline_prob_spin.setRange(0.0, 1.0)
        self.offline_prob_spin.setSingleStep(0.05)
        self.offline_prob_spin.setValue(self._config.get("offline_base_probability", 0.5))
        self.offline_prob_spin.setStyleSheet(_spin_style())
        sog.addRow("Offline prob:", self.offline_prob_spin)

        self.offline_jitter_spin = QDoubleSpinBox()
        self.offline_jitter_spin.setRange(0.0, 0.5)
        self.offline_jitter_spin.setSingleStep(0.05)
        self.offline_jitter_spin.setValue(self._config.get("offline_probability_jitter", 0.2))
        self.offline_jitter_spin.setStyleSheet(_spin_style())
        sog.addRow("Jitter:", self.offline_jitter_spin)

        self.offline_duration_picker = TimePicker(min_val=1, max_val=168, default=24)
        self.offline_duration_picker.set_value_in_hours(self._config.get("offline_duration_hours", 24))
        sog.addRow("Offline duration:", self.offline_duration_picker)

        self.offline_interval_picker = TimePicker(min_val=1, max_val=168, default=24)
        self.offline_interval_picker.set_value_in_hours(self._config.get("offline_check_interval_hours", 24))
        sog.addRow("Check every:", self.offline_interval_picker)

        self.max_failures_spin = QSpinBox()
        self.max_failures_spin.setRange(1, 100)
        self.max_failures_spin.setValue(self._config.get("max_consecutive_failures", 10))
        self.max_failures_spin.setStyleSheet(_spin_style())
        sog.addRow("Max failures:", self.max_failures_spin)

        gen_layout.addWidget(so)

        ml_group = QGroupBox("Machine Learning")
        ml_form = QFormLayout(ml_group)
        ml_form.setSpacing(6)
        ml_form.setContentsMargins(8, 4, 8, 4)
        ml_form.setLabelAlignment(Qt.AlignRight)

        self.ml_enabled_check = QCheckBox("Enable ML predictions")
        self.ml_enabled_check.setChecked(self._config.get("ml_enabled", True))
        ml_form.addRow("", self.ml_enabled_check)

        self.ml_auto_retrain_check = QCheckBox("Auto-retrain after sessions")
        self.ml_auto_retrain_check.setChecked(self._config.get("ml_auto_retrain", True))
        ml_form.addRow("", self.ml_auto_retrain_check)

        self.ml_anomaly_check = QCheckBox("Anomaly detection (early warnings)")
        self.ml_anomaly_check.setChecked(self._config.get("ml_anomaly_detection", True))
        ml_form.addRow("", self.ml_anomaly_check)

        self.ml_min_samples_spin = QSpinBox()
        self.ml_min_samples_spin.setRange(5, 100)
        self.ml_min_samples_spin.setValue(self._config.get("ml_min_training_samples", 10))
        self.ml_min_samples_spin.setStyleSheet(_spin_style())
        ml_form.addRow("Min training samples:", self.ml_min_samples_spin)

        gen_layout.addWidget(ml_group)

        misc = QGroupBox("Other")
        mg = QFormLayout(misc)
        mg.setSpacing(6)
        mg.setContentsMargins(8, 4, 8, 4)
        mg.setLabelAlignment(Qt.AlignRight)

        self.headless_check = QCheckBox("Headless mode (no visible browser)")
        self.headless_check.setChecked(self._config.get("headless", False))
        mg.addRow("", self.headless_check)

        gen_layout.addWidget(misc)

        tw_group = QGroupBox("Task Wall")
        tw_form = QFormLayout(tw_group)
        tw_form.setSpacing(6)
        tw_form.setContentsMargins(8, 4, 8, 4)
        tw_form.setLabelAlignment(Qt.AlignRight)

        self.task_wall_enabled_check = QCheckBox("Enable task wall automation")
        self.task_wall_enabled_check.setChecked(self._config.get("task_wall_enabled", True))
        tw_form.addRow("", self.task_wall_enabled_check)

        self.task_wall_skip_ext_check = QCheckBox("Skip external/CPX tasks")
        self.task_wall_skip_ext_check.setChecked(self._config.get("task_wall_skip_external", True))
        tw_form.addRow("", self.task_wall_skip_ext_check)

        self.task_wall_claim_check = QCheckBox("Auto-claim completed rewards")
        self.task_wall_claim_check.setChecked(self._config.get("task_wall_claim_rewards", True))
        tw_form.addRow("", self.task_wall_claim_check)

        self.task_wall_refresh_spin = QSpinBox()
        self.task_wall_refresh_spin.setRange(10, 600)
        self.task_wall_refresh_spin.setValue(self._config.get("task_wall_refresh_seconds", 60))
        self.task_wall_refresh_spin.setSuffix(" s")
        self.task_wall_refresh_spin.setStyleSheet(_spin_style())
        tw_form.addRow("Refresh interval:", self.task_wall_refresh_spin)

        self.task_wall_max_spin = QSpinBox()
        self.task_wall_max_spin.setRange(1, 10)
        self.task_wall_max_spin.setValue(self._config.get("task_wall_max_simultaneous", 3))
        self.task_wall_max_spin.setSuffix(" tasks")
        self.task_wall_max_spin.setStyleSheet(_spin_style())
        tw_form.addRow("Max per cycle:", self.task_wall_max_spin)

        self.task_wall_cycle_interval_spin = QSpinBox()
        self.task_wall_cycle_interval_spin.setRange(1, 50)
        self.task_wall_cycle_interval_spin.setValue(self._config.get("task_wall_check_every_n_cycles", 10))
        self.task_wall_cycle_interval_spin.setSuffix(" cycles")
        self.task_wall_cycle_interval_spin.setStyleSheet(_spin_style())
        tw_form.addRow("Check every:", self.task_wall_cycle_interval_spin)

        gen_layout.addWidget(tw_group)

        dc_group = QGroupBox("Discord Integration (restart bot after changes)")
        dc_form = QFormLayout(dc_group)
        dc_form.setSpacing(6)
        dc_form.setContentsMargins(8, 4, 8, 4)
        dc_form.setLabelAlignment(Qt.AlignRight)

        self.discord_webhook_edit = QLineEdit()
        self.discord_webhook_edit.setText(self._config.get("discord_webhook_url", ""))
        self.discord_webhook_edit.setPlaceholderText("https://discord.com/api/webhooks/...")
        self.discord_webhook_edit.setStyleSheet(_line_style())
        dc_form.addRow("Webhook URL:", self.discord_webhook_edit)

        self.discord_token_edit = QLineEdit()
        self.discord_token_edit.setText(self._config.get("discord_token", ""))
        self.discord_token_edit.setPlaceholderText("Bot token (leave empty to disable)")
        self.discord_token_edit.setEchoMode(QLineEdit.Password)
        self.discord_token_edit.setStyleSheet(_line_style())
        dc_form.addRow("Bot token:", self.discord_token_edit)

        self.discord_channel_edit = QLineEdit()
        raw_id = self._config.get("discord_channel_id", 0)
        self.discord_channel_edit.setText(str(raw_id) if raw_id else "")
        self.discord_channel_edit.setPlaceholderText("Numeric channel ID (right-click -> Copy ID in Discord)")
        self.discord_channel_edit.setStyleSheet(_line_style())
        dc_form.addRow("Channel ID:", self.discord_channel_edit)

        self.discord_notify_cycles_check = QCheckBox("Notify on cycle complete")
        self.discord_notify_cycles_check.setChecked(self._config.get("discord_notify_cycles", True))
        dc_form.addRow("", self.discord_notify_cycles_check)

        self.discord_notify_errors_check = QCheckBox("Notify on errors")
        self.discord_notify_errors_check.setChecked(self._config.get("discord_notify_errors", True))
        dc_form.addRow("", self.discord_notify_errors_check)

        self.discord_notify_daily_check = QCheckBox("Daily summary")
        self.discord_notify_daily_check.setChecked(self._config.get("discord_notify_daily", False))
        dc_form.addRow("", self.discord_notify_daily_check)

        gen_layout.addWidget(dc_group)
        gen_layout.addStretch()
        gen_scroll.setWidget(gen_inner)
        tabs.addTab(gen_scroll, "General")

        # ── Auto-Crafting tab ──────────────────────────────────────────
        craft_scroll = QScrollArea()
        craft_scroll.setWidgetResizable(True)
        craft_scroll.setFrameShape(QFrame.NoFrame)
        craft_inner = QWidget()
        craft_layout = QVBoxLayout(craft_inner)
        craft_layout.setContentsMargins(12, 6, 12, 6)
        craft_layout.setSpacing(8)

        ac_group = QGroupBox("Crafting Settings")
        ac_form = QFormLayout(ac_group)
        ac_form.setSpacing(6)
        ac_form.setContentsMargins(8, 4, 8, 4)
        ac_form.setLabelAlignment(Qt.AlignRight)

        self.auto_craft_enabled_check = QCheckBox("Enable auto-crafting")
        self.auto_craft_enabled_check.setChecked(self._config.get("auto_craft_enabled", True))
        ac_form.addRow("", self.auto_craft_enabled_check)

        self.auto_craft_max_spin = QSpinBox()
        self.auto_craft_max_spin.setRange(1, 20)
        self.auto_craft_max_spin.setValue(self._config.get("auto_craft_max_per_cycle", 3))
        self.auto_craft_max_spin.setSuffix(" items")
        self.auto_craft_max_spin.setStyleSheet(_spin_style())
        ac_form.addRow("Max per cycle:", self.auto_craft_max_spin)

        self.auto_craft_preferred_edit = QLineEdit()
        self.auto_craft_preferred_edit.setText(self._config.get("auto_craft_preferred_recipes", ""))
        self.auto_craft_preferred_edit.setPlaceholderText("e.g. Wood Plank, Stone Brick (blank = all)")
        self.auto_craft_preferred_edit.setStyleSheet(_line_style())
        ac_form.addRow("Preferred recipes:", self.auto_craft_preferred_edit)

        self.auto_craft_reserve_check = QCheckBox("Reserve ingredients")
        self.auto_craft_reserve_check.setChecked(self._config.get("auto_craft_reserve_ingredients", False))
        self.auto_craft_reserve_check.setToolTip("Don't use up all ingredients; keep a minimum amount in stock")
        ac_form.addRow("", self.auto_craft_reserve_check)

        self.auto_craft_reserve_spin = QSpinBox()
        self.auto_craft_reserve_spin.setRange(1, 100)
        self.auto_craft_reserve_spin.setValue(self._config.get("auto_craft_min_ingredient_reserve", 5))
        self.auto_craft_reserve_spin.setSuffix(" min each")
        self.auto_craft_reserve_spin.setStyleSheet(_spin_style())
        ac_form.addRow("Min reserve:", self.auto_craft_reserve_spin)

        self.auto_craft_max_storage_check = QCheckBox("Skip if output already in storage")
        self.auto_craft_max_storage_check.setChecked(self._config.get("auto_craft_max_output_storage", 0) > 0)
        self.auto_craft_max_storage_check.setToolTip("Don't craft if you already have enough of the output item")
        ac_form.addRow("", self.auto_craft_max_storage_check)

        self.auto_craft_max_storage_spin = QSpinBox()
        self.auto_craft_max_storage_spin.setRange(1, 9999)
        self.auto_craft_max_storage_spin.setValue(self._config.get("auto_craft_max_output_storage", 0) or 50)
        self.auto_craft_max_storage_spin.setSuffix(" each")
        self.auto_craft_max_storage_spin.setStyleSheet(_spin_style())
        ac_form.addRow("Max in storage:", self.auto_craft_max_storage_spin)

        craft_layout.addWidget(ac_group)

        # ── Recipe checklist ───────────────────────────────────────────
        recipe_group = QGroupBox("Enabled Recipes (checked = will craft)")
        rg_layout = QVBoxLayout(recipe_group)
        rg_layout.setContentsMargins(8, 4, 8, 4)
        rg_layout.setSpacing(6)

        recipe_btn_row = QHBoxLayout()
        self._fetch_recipes_btn = QPushButton("Fetch from Game")
        self._fetch_recipes_btn.setStyleSheet(_btn_style())
        self._fetch_recipes_btn.setToolTip("Fetch available recipes from the running game")
        self._fetch_recipes_btn.clicked.connect(self._fetch_recipes)
        recipe_btn_row.addWidget(self._fetch_recipes_btn)

        self._recipe_select_all_btn = QPushButton("Select All")
        self._recipe_select_all_btn.setStyleSheet(_btn_style())
        self._recipe_select_all_btn.clicked.connect(lambda: self._set_all_recipes(True))
        recipe_btn_row.addWidget(self._recipe_select_all_btn)

        self._recipe_deselect_all_btn = QPushButton("Deselect All")
        self._recipe_deselect_all_btn.setStyleSheet(_btn_style())
        self._recipe_deselect_all_btn.clicked.connect(lambda: self._set_all_recipes(False))
        recipe_btn_row.addWidget(self._recipe_deselect_all_btn)
        recipe_btn_row.addStretch()
        rg_layout.addLayout(recipe_btn_row)

        self.recipe_list_widget = QListWidget()
        self.recipe_list_widget.setMinimumHeight(180)
        self.recipe_list_widget.setMaximumHeight(300)
        rg_layout.addWidget(self.recipe_list_widget)

        self._recipe_status_label = QLabel("")
        self._recipe_status_label.setStyleSheet("color: #6a7a9a; font-size: 9pt;")
        rg_layout.addWidget(self._recipe_status_label)

        craft_layout.addWidget(recipe_group)
        craft_layout.addStretch()
        craft_scroll.setWidget(craft_inner)
        tabs.addTab(craft_scroll, "Auto-Crafting")

        # ── Dialog buttons ─────────────────────────────────────────────
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        outer.addWidget(tabs)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(12, 0, 12, 8)
        btn_row.addStretch()
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self._on_ok)
        ok_btn.setStyleSheet(_btn_style())
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        cancel_btn.setStyleSheet(_btn_style())
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        outer.addLayout(btn_row)

        # Populate recipe checklist from config
        self._populate_recipe_list()

    def _populate_recipe_list(self):
        self.recipe_list_widget.clear()
        self._recipe_list_items = []
        enabled = set(self._config.get("auto_craft_enabled_recipes", []))
        known = self._config.get("auto_craft_known_recipes", [])
        for entry in known:
            if isinstance(entry, dict):
                name = entry.get("name", "")
                rid = entry.get("id", "")
            else:
                name = str(entry)
                rid = ""
            if not name:
                continue
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, {"id": rid, "name": name})
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if (not enabled or name in enabled) else Qt.Unchecked)
            self.recipe_list_widget.addItem(item)
            self._recipe_list_items.append(item)
        if known:
            self._recipe_status_label.setText(f"{len(known)} recipes loaded")
        else:
            self._recipe_status_label.setText("No recipes loaded yet — click Fetch from Game")

    def _fetch_recipes(self):
        if self._bot_worker and (self._bot_worker._ife or self._bot_worker._page):
            self._fetch_recipes_btn.setEnabled(False)
            self._fetch_recipes_btn.setText("Fetching...")
            self._recipe_status_label.setText("Fetching recipes from game...")
            self._bot_worker.recipes_fetched.connect(self._on_recipes_fetched)
            self._bot_worker.request_recipe_fetch()
        else:
            self._recipe_status_label.setText("Game not running — launch browser first")

    def _on_recipes_fetched(self, recipes):
        try:
            self._bot_worker.recipes_fetched.disconnect(self._on_recipes_fetched)
        except RuntimeError:
            pass
        self._fetch_recipes_btn.setEnabled(True)
        self._fetch_recipes_btn.setText("Fetch from Game")
        if recipes is None:
            self._recipe_status_label.setText("Failed to fetch recipes")
            return
        enabled = set(self._config.get("auto_craft_enabled_recipes", []))
        prev_checked = {}
        for i in range(self.recipe_list_widget.count()):
            item = self.recipe_list_widget.item(i)
            data = item.data(Qt.UserRole)
            if data:
                prev_checked[data["name"]] = item.checkState() == Qt.Checked

        self.recipe_list_widget.clear()
        self._recipe_list_items = []
        for entry in recipes:
            name = entry.get("name", "")
            rid = entry.get("id", "")
            if not name:
                continue
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, {"id": rid, "name": name})
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            if name in prev_checked:
                checked = prev_checked[name]
            elif enabled:
                checked = name in enabled
            else:
                checked = True
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            self.recipe_list_widget.addItem(item)
            self._recipe_list_items.append(item)
        self._config["auto_craft_known_recipes"] = recipes
        self._recipe_status_label.setText(f"{len(recipes)} recipes loaded from game")

    def _set_all_recipes(self, checked):
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self.recipe_list_widget.count()):
            self.recipe_list_widget.item(i).setCheckState(state)

    def _on_ok(self):
        self._config["action_delay_min"] = self.action_min_spin.value()
        self._config["action_delay_max"] = self.action_max_spin.value()
        self._config["max_actions_per_cycle"] = self.max_actions_spin.value()
        self._config["cycle_delay_min"] = self.cycle_min_spin.value()
        self._config["cycle_delay_max"] = self.cycle_max_spin.value()
        self._config["cooldown_hours"] = self.cooldown_picker.value_in_hours()
        self._config["seed_bed_rotation_hours"] = self.seed_rotation_picker.value_in_hours()
        self._config["sandbagging_enabled"] = self.sandbagging_check.isChecked()
        self._config["sandbagging_avoid_best_chance"] = self.sandbagging_chance_spin.value()
        self._config["headless"] = self.headless_check.isChecked()
        self._config["max_consecutive_failures"] = self.max_failures_spin.value()
        self._config["task_wall_enabled"] = self.task_wall_enabled_check.isChecked()
        self._config["task_wall_skip_external"] = self.task_wall_skip_ext_check.isChecked()
        self._config["task_wall_claim_rewards"] = self.task_wall_claim_check.isChecked()
        self._config["task_wall_refresh_seconds"] = self.task_wall_refresh_spin.value()
        self._config["task_wall_max_simultaneous"] = self.task_wall_max_spin.value()
        self._config["task_wall_check_every_n_cycles"] = self.task_wall_cycle_interval_spin.value()
        self._config["session_break_min_mins"] = int(self.break_min_picker.value_in_hours() * 60)
        self._config["session_break_max_mins"] = int(self.break_max_picker.value_in_hours() * 60)
        self._config["ml_enabled"] = self.ml_enabled_check.isChecked()
        self._config["ml_auto_retrain"] = self.ml_auto_retrain_check.isChecked()
        self._config["ml_anomaly_detection"] = self.ml_anomaly_check.isChecked()
        self._config["ml_min_training_samples"] = self.ml_min_samples_spin.value()
        self._config["discord_webhook_url"] = self.discord_webhook_edit.text().strip()
        self._config["discord_token"] = self.discord_token_edit.text().strip()
        try:
            self._config["discord_channel_id"] = int(self.discord_channel_edit.text().strip() or "0")
        except ValueError:
            self._config["discord_channel_id"] = 0

        self._config["discord_notify_cycles"] = self.discord_notify_cycles_check.isChecked()
        self._config["discord_notify_errors"] = self.discord_notify_errors_check.isChecked()
        self._config["discord_notify_daily"] = self.discord_notify_daily_check.isChecked()
        self._config["offline_base_probability"] = self.offline_prob_spin.value()
        self._config["offline_probability_jitter"] = self.offline_jitter_spin.value()
        self._config["offline_duration_hours"] = self.offline_duration_picker.value_in_hours()
        self._config["offline_check_interval_hours"] = self.offline_interval_picker.value_in_hours()
        self._config["auto_craft_enabled"] = self.auto_craft_enabled_check.isChecked()
        self._config["auto_craft_max_per_cycle"] = self.auto_craft_max_spin.value()
        self._config["auto_craft_preferred_recipes"] = self.auto_craft_preferred_edit.text().strip()
        self._config["auto_craft_reserve_ingredients"] = self.auto_craft_reserve_check.isChecked()
        self._config["auto_craft_min_ingredient_reserve"] = self.auto_craft_reserve_spin.value()
        self._config["auto_craft_max_output_storage"] = self.auto_craft_max_storage_spin.value() if self.auto_craft_max_storage_check.isChecked() else 0

        enabled_recipes = []
        for i in range(self.recipe_list_widget.count()):
            item = self.recipe_list_widget.item(i)
            if item.checkState() == Qt.Checked:
                data = item.data(Qt.UserRole)
                if data:
                    enabled_recipes.append(data["name"])
        self._config["auto_craft_enabled_recipes"] = enabled_recipes

        try:
            vals = [int(x.strip()) for x in self.session_durations_edit.text().split(",") if x.strip()]
            if vals:
                self._config["session_duration_choices"] = vals
        except ValueError:
            pass

        save_config(self._config)
        self.result_config = dict(self._config)
        self.accept()


def _spin_style():
    return "font-size: 10pt; padding: 3px 6px; background: #0e1220; border: 1px solid #2a2f4a; border-radius: 4px; color: #ccc;"

def _line_style():
    return "font-size: 10pt; padding: 2px 4px; background: #0e1220; border: 1px solid #2a2f4a; border-radius: 4px; color: #ccc;"

def _btn_style():
    return """
        QPushButton {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #2a3a6a, stop:1 #1a2a4a);
            border: 1px solid #3a4a7a;
            border-radius: 4px;
            color: #c8d0e8;
            font-size: 10pt;
            font-weight: 600;
            padding: 6px 20px;
        }
        QPushButton:hover {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #3a4a8a, stop:1 #2a3a6a);
            border: 1px solid #5a6a9a;
        }
    """


class SetupDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("UNCHAINED â€” First-Time Setup")
        self.setFixedSize(520, 360)
        self.setStyleSheet("""
            QDialog { background: #111827; }
            QLabel { color: #c8d0e8; font-size: 12pt; }
            QTextEdit { background: #0a0c14; color: #8a9abb;
                        border: 1px solid #2a2f4a; border-radius: 4px;
                        font-family: Consolas; font-size: 10pt; }
        """)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("Setting up UNCHAINED")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 16pt; font-weight: 700; color: #e0e4f0;")
        layout.addWidget(title)

        self.progress = QTextEdit()
        self.progress.setReadOnly(True)
        layout.addWidget(self.progress)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setStyleSheet("""
            QPushButton { background: #3a2a2a; border: 1px solid #5a3a3a;
                          border-radius: 4px; color: #ccc; padding: 6px 20px;
                          font-size: 11pt; }
            QPushButton:hover { background: #4a3a3a; }
        """)
        self.cancel_btn.clicked.connect(self._cancel)
        self.cancel_btn.setEnabled(False)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(self.cancel_btn)
        layout.addLayout(btn_row)

        self._cancelled = False

    def log(self, msg):
        self.progress.append(msg)
        cursor = self.progress.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.progress.setTextCursor(cursor)
        QApplication.processEvents()

    def _cancel(self):
        self._cancelled = True
        self.log("Cancelling...")

    def is_cancelled(self):
        return self._cancelled


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("UNCHAINED")
    app.setOrganizationName("ChainBot")

    if is_first_run():
        dlg = SetupDialog()
        dlg.show()
        QApplication.processEvents()
        try:
            setup_first_run(log_func=dlg.log)
            dlg.log("Setup complete!")
            dlg.cancel_btn.setText("Continue")
            dlg.cancel_btn.setEnabled(True)
            dlg.cancel_btn.clicked.disconnect()
            dlg.cancel_btn.clicked.connect(dlg.accept)
            dlg.exec()
        except Exception as e:
            dlg.log(f"Setup failed: {e}")
            dlg.cancel_btn.setText("Exit")
            dlg.cancel_btn.setEnabled(True)
            dlg.cancel_btn.clicked.disconnect()
            dlg.cancel_btn.clicked.connect(sys.exit)
            dlg.exec()
        finally:
            dlg.deleteLater()

    window = UnchainedWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
