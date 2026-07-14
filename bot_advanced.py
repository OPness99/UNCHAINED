"""Advanced features for UNCHAINED — crafting, mouse simulation, mistakes, and ROI.

Adds human-like behavior, economic intelligence, and full crafting automation.
"""

import json
import logging
import math
import os
import random
import re
import time
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger("unchained.advanced")

# ── Mouse Simulation ────────────────────────────────────────────────────────

def human_mouse_path(start_x, start_y, end_x, end_y, steps=60):
    """Generate a human-like mouse movement path using Bezier curves.

    Human mouse movement isn't linear — it curves, overshoots, and has micro-jitter.
    This generates waypoints that simulate realistic cursor movement.

    Returns list of (x, y) tuples representing the path.
    """
    cp1_x = start_x + (end_x - start_x) * random.uniform(0.1, 0.4) + random.randint(-60, 60)
    cp1_y = start_y + (end_y - start_y) * random.uniform(0.0, 0.3) + random.randint(-40, 40)
    cp2_x = start_x + (end_x - start_x) * random.uniform(0.6, 0.9) + random.randint(-40, 40)
    cp2_y = start_y + (end_y - start_y) * random.uniform(0.7, 1.0) + random.randint(-30, 30)

    path = []
    for i in range(steps):
        t = i / (steps - 1)
        # Cubic Bezier
        x = (1-t)**3 * start_x + 3*(1-t)**2*t * cp1_x + 3*(1-t)*t**2 * cp2_x + t**3 * end_x
        y = (1-t)**3 * start_y + 3*(1-t)**2*t * cp1_y + 3*(1-t)*t**2 * cp2_y + t**3 * end_y
        # Add micro-jitter
        if i > 5 and i < steps - 5:
            x += random.gauss(0, 1.5)
            y += random.gauss(0, 1.5)
        path.append((round(x, 1), round(y, 1)))

    # Small overshoot at end (common in humans)
    overshoot = random.randint(3, 12)
    path.append((end_x + overshoot * random.choice([-1, 1]), end_y + random.randint(-3, 3)))
    path.append((end_x, end_y))
    return path


def simulate_human_interaction(page, target_selector, action="click"):
    """Simulate a human-like interaction with a page element.

    Moves the mouse in a curved path, pauses briefly, then clicks.
    Falls back gracefully if the element isn't found.
    """
    try:
        element = page.query_selector(target_selector)
        if not element:
            return False

        # Get element position
        box = element.bounding_box()
        if not box:
            return False

        end_x = box['x'] + box['width'] * random.uniform(0.3, 0.7)
        end_y = box['y'] + box['height'] * random.uniform(0.3, 0.7)

        # Start from a random position on screen
        viewport = page.viewport_size or {"width": 1920, "height": 1080}
        start_x = random.randint(100, viewport['width'] - 100)
        start_y = random.randint(100, viewport['height'] - 100)

        # Generate and follow path
        path = human_mouse_path(start_x, start_y, end_x, end_y,
                                steps=random.randint(40, 80))
        for x, y in path[::2]:  # Skip every other point for speed
            page.mouse.move(x, y)
            time.sleep(random.uniform(0.001, 0.005))

        # Pause like a human reading/deciding
        time.sleep(random.uniform(0.2, 0.8))

        if action == "click":
            page.mouse.click(end_x, end_y)
        elif action == "hover":
            time.sleep(random.uniform(0.5, 2.0))
        elif action == "dblclick":
            page.mouse.dblclick(end_x, end_y)

        return True

    except Exception as e:
        logger.debug(f"Mouse simulation failed: {e}")
        return False


# ── Intentional Mistakes ─────────────────────────────────────────────────────

class MistakeEngine:
    """Generates human-like mistakes to reduce bot detection signatures."""

    MISTAKE_TYPES = [
        "wrong_crop",       # Plant a suboptimal seed on purpose
        "early_harvest",    # Harvest a crop that's not quite ready
        "missed_harvest",   # Skip a ready crop (forgetfulness)
        "wrong_animal",     # Feed the wrong animal
        "double_click",     # Click twice on the same thing
        "overwater",        # Water a plant unnecessarily
        "wrong_bed",        # Plant in a less optimal bed
        "pause_within",     # Long pause mid-action (got distracted)
    ]

    def __init__(self, mistake_rate=0.05):
        self.mistake_rate = mistake_rate
        self._mistake_log = []
        self._last_mistake_time = 0
        self._cooldown = 60  # No mistakes within 60s of each other

    def should_mistake(self, action_type: str = "any") -> bool:
        """Decide if we should make a mistake right now."""
        if time.time() - self._last_mistake_time < self._cooldown:
            return False
        return random.random() < self.mistake_rate

    def pick_mistake(self, action_type: str = "any") -> str:
        """Pick an appropriate mistake type for the current action."""
        suitable = {
            "planting": ["wrong_crop", "wrong_bed", "double_click", "pause_within"],
            "harvesting": ["early_harvest", "missed_harvest", "double_click", "pause_within"],
            "feeding": ["wrong_animal", "double_click", "pause_within"],
            "crafting": ["double_click", "pause_within", "overwater"],
        }
        pool = suitable.get(action_type, self.MISTAKE_TYPES)
        mistake = random.choice(pool)
        self._mistake_log.append({
            "mistake": mistake,
            "action": action_type,
            "time": time.strftime("%H:%M:%S")
        })
        self._last_mistake_time = time.time()
        if len(self._mistake_log) > 100:
            self._mistake_log = self._mistake_log[-50:]
        return mistake

    def get_mistake_count(self) -> int:
        return len(self._mistake_log)

    def get_mistake_summary(self) -> str:
        counts = {}
        for m in self._mistake_log:
            counts[m["mistake"]] = counts.get(m["mistake"], 0) + 1
        return ", ".join(f"{k}: {v}" for k, v in counts.items())


_mistake_engine: Optional[MistakeEngine] = None


def get_mistake_engine() -> MistakeEngine:
    global _mistake_engine
    if _mistake_engine is None:
        _mistake_engine = MistakeEngine()
    return _mistake_engine


# ── Economic ROI Tracking ────────────────────────────────────────────────────

class ROITracker:
    """Tracks profit/loss per seed, per garden, per action.

    Data is persisted to UNCHAINED/roi/ in the vault.
    """

    def __init__(self, vault_path=None):
        self.vault_path = vault_path or os.path.join(os.getcwd(), "UNCHAINED", "roi")
        self._seed_stats: Dict[str, dict] = {}
        self._pool_timing: Dict[str, Any] = {}
        self._session_pnl: Dict[str, float] = {"plated": 0, "harvested": 0, "pool_submitted": 0}
        self._last_pool_reset: float = 0
        self._load()

    def _save(self):
        os.makedirs(self.vault_path, exist_ok=True)
        data = {
            "seed_stats": self._seed_stats,
            "pool_timing": self._pool_timing,
            "last_pool_reset": self._last_pool_reset,
        }
        with open(os.path.join(self.vault_path, "roi.json"), "w") as f:
            json.dump(data, f, indent=2, default=str)

    def _load(self):
        path = os.path.join(self.vault_path, "roi.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                    self._seed_stats = data.get("seed_stats", {})
                    self._pool_timing = data.get("pool_timing", {})
                    self._last_pool_reset = data.get("last_pool_reset", 0)
            except Exception:
                pass

    def record_plant(self, seed_code: str, garden: str = "default"):
        key = f"{garden}:{seed_code}"
        if key not in self._seed_stats:
            self._seed_stats[key] = {"planted": 0, "harvested": 0, "pool_value": 0.0,
                                       "growth_time_avg": 0, "last_planted": None}
        self._seed_stats[key]["planted"] += 1
        self._seed_stats[key]["last_planted"] = time.strftime("%Y-%m-%d %H:%M")
        self._session_pnl["plated"] += 1

    def record_harvest(self, seed_code: str, count: int = 1, garden: str = "default",
                       growth_time: float = 0, rarity: str = "common"):
        key = f"{garden}:{seed_code}"
        if key not in self._seed_stats:
            self._seed_stats[key] = {"planted": 0, "harvested": 0, "pool_value": 0.0,
                                       "growth_time_avg": 0, "last_harvested": None,
                                       "rarity": rarity}
        stats = self._seed_stats[key]
        stats["harvested"] += count
        stats["last_harvested"] = time.strftime("%Y-%m-%d %H:%M")
        stats["rarity"] = rarity
        if growth_time > 0:
            if stats["growth_time_avg"] == 0:
                stats["growth_time_avg"] = growth_time
            else:
                n = stats["harvested"]
                stats["growth_time_avg"] = (stats["growth_time_avg"] * (n - count) + growth_time * count) / n
        self._session_pnl["harvested"] += count
        self._save()

    def record_pool_submit(self, item_code: str, count: int, value_estimate: float = 0):
        self._session_pnl["pool_submitted"] += count
        key = f"pool:{item_code}"
        if key not in self._seed_stats:
            self._seed_stats[key] = {"submitted": 0, "total_value": 0.0}
        self._seed_stats[key]["submitted"] += count
        self._seed_stats[key]["total_value"] += value_estimate
        self._save()

    def record_pool_timing(self, pool_id: str, time_since_reset: float, reward: float):
        """Track reward pool submission timing for optimization."""
        bucket = int(time_since_reset // 600)  # 10-minute buckets
        if pool_id not in self._pool_timing:
            self._pool_timing[pool_id] = {}
        bucket_str = str(bucket)
        if bucket_str not in self._pool_timing[pool_id]:
            self._pool_timing[pool_id][bucket_str] = {"count": 0, "total_reward": 0.0}
        self._pool_timing[pool_id][bucket_str]["count"] += 1
        self._pool_timing[pool_id][bucket_str]["total_reward"] += reward
        self._save()

    def get_best_pool_window(self, pool_id: str = "cfb") -> int:
        """Return the optimal time (in seconds) after pool reset to submit."""
        timing = self._pool_timing.get(pool_id, {})
        if not timing:
            return 600  # Default: 10 min after reset

        best_bucket = None
        best_avg = 0
        for bucket, data in timing.items():
            avg = data["total_reward"] / data["count"] if data["count"] > 0 else 0
            if avg > best_avg:
                best_avg = avg
                best_bucket = int(bucket)

        return best_bucket * 600 if best_bucket is not None else 600

    def get_best_seeds(self, top_n=5) -> List[dict]:
        """Return seeds ranked by harvest efficiency (harvested/planted ratio)."""
        ranked = []
        for key, stats in self._seed_stats.items():
            if stats["planted"] == 0:
                continue
            efficiency = stats["harvested"] / stats["planted"]
            ranked.append({
                "key": key,
                "efficiency": efficiency,
                "planted": stats["planted"],
                "harvested": stats["harvested"],
                "growth_time_avg": stats.get("growth_time_avg", 0),
                "rarity": stats.get("rarity", "unknown"),
            })
        ranked.sort(key=lambda s: -s["efficiency"])
        return ranked[:top_n]

    def get_roi_summary(self) -> dict:
        best = self.get_best_seeds(3)
        return {
            "session": dict(self._session_pnl),
            "top_seeds": best,
            "pool_window": self.get_best_pool_window(),
            "total_seeds_tracked": len(self._seed_stats),
        }

    def reset_session_pnl(self):
        self._session_pnl = {"plated": 0, "harvested": 0, "pool_submitted": 0}


_roi_tracker: Optional[ROITracker] = None


def get_roi_tracker(vault_path=None) -> ROITracker:
    global _roi_tracker
    if _roi_tracker is None:
        _roi_tracker = ROITracker(vault_path)
    return _roi_tracker


# ── Crafting Automation ──────────────────────────────────────────────────────

def execute_craft_task(ife, config, count=None, recipe_name=None, sleep_fn=time.sleep):
    """Craft items in the Workshop using CraftingAPI.js.

    The crafting system requires recipes. We fetch available recipes,
    find craftable ones (with ingredients in inventory), and craft them.
    """
    results = {'crafted': 0, 'errors': []}

    if not config.get("auto_craft_enabled", True):
        results['errors'].append("Auto-crafting is disabled in settings")
        return results

    if count is None:
        count = config.get("auto_craft_max_per_cycle", 3)

    preferred = config.get("auto_craft_preferred_recipes", "")
    reserve_mode = config.get("auto_craft_reserve_ingredients", False)
    min_reserve = config.get("auto_craft_min_ingredient_reserve", 5)
    max_output = config.get("auto_craft_max_output_storage", 0)
    enabled_recipes = set(config.get("auto_craft_enabled_recipes", []))
    known_recipes = config.get("auto_craft_known_recipes", [])

    # Fetch crafting recipes and available items
    craft_data = ife('''
        try {
            let api = new API();
            let recipes = [];
            let workshop_items = [];
            let inventory = [];

            if (typeof api.get_recipes === 'function') {
                let r = await api.get_recipes();
                recipes = JSON.parse(JSON.stringify(r._data || r));
            }
            if (typeof api.get_workshop_items === 'function') {
                let r = await api.get_workshop_items();
                workshop_items = JSON.parse(JSON.stringify(r._data || r));
            }
            if (typeof api.get_user_inventory === 'function') {
                let r = await api.get_user_inventory();
                if (r.ok && r._data && r._data.strData) {
                    inventory = JSON.parse(r._data.strData);
                }
            }

            return {
                recipes: Array.isArray(recipes) ? recipes : [],
                workshop_items: Array.isArray(workshop_items) ? workshop_items : [],
                inventory: Array.isArray(inventory) ? inventory : []
            };
        } catch(e) { return {_error: e.message}; }
    ''')

    if not craft_data or isinstance(craft_data, str) or '_error' in craft_data:
        results['errors'].append(f"Crafting fetch failed: {craft_data.get('_error', 'unknown') if isinstance(craft_data, dict) else 'unknown'}")
        return results

    recipes = craft_data.get('recipes', [])
    inventory = craft_data.get('inventory', [])

    if not recipes:
        results['errors'].append("No crafting recipes available")
        return results

    # Update known recipes list for settings UI
    updated_known = []
    for r in recipes:
        rid = r.get('id', r.get('recipeID', ''))
        rname = r.get('name', r.get('recipeName', ''))
        if rname:
            updated_known.append({'id': str(rid), 'name': rname})
    if updated_known:
        config["auto_craft_known_recipes"] = updated_known

    # Build inventory lookup
    inv_map = {}
    for item in inventory:
        code = item.get('itemCode', item.get('itemID', ''))
        inv_map[code] = inv_map.get(code, 0) + item.get('count', 0)

    # Parse preferred recipe filters
    preferred_list = []
    if preferred:
        preferred_list = [p.strip().lower() for p in preferred.split(",") if p.strip()]

    # Find craftable recipes
    craftable = []
    for recipe in recipes:
        name = recipe.get('name', recipe.get('recipeName', ''))
        recipe_id = recipe.get('id', recipe.get('recipeID', ''))

        if recipe_name and recipe_name.lower() not in name.lower():
            continue

        # Apply preferred filter if set
        if preferred_list:
            matched = any(p in name.lower() for p in preferred_list)
            if not matched:
                continue

        # Apply enabled recipes checklist filter
        if enabled_recipes and name not in enabled_recipes:
            continue

        ingredients = recipe.get('ingredients', recipe.get('inputs', []))
        can_craft = True
        for ing in ingredients:
            ing_code = ing.get('itemCode', ing.get('itemID', ''))
            ing_need = ing.get('count', ing.get('amount', 1))
            available = inv_map.get(ing_code, 0)
            if available < ing_need:
                can_craft = False
                break
            if reserve_mode and available - ing_need < min_reserve:
                can_craft = False
                break

        if can_craft:
            output_code = recipe.get('output', {}).get('itemCode', '?')
            output_count = recipe.get('output', {}).get('count', 1)
            if max_output > 0 and inv_map.get(output_code, 0) >= max_output:
                continue
            craftable.append({
                'id': recipe_id,
                'name': name,
                'output': output_code,
                'output_count': output_count,
            })

    if not craftable:
        results['errors'].append("No craftable recipes with available ingredients")
        return results

    # Craft items
    crafted = 0
    for recipe in craftable:
        if crafted >= count:
            break

        batches = 1
        if recipe['output_count'] > 0:
            batches = min(count - crafted, 3)  # Max 3 batches per recipe

        micro_pause(sleep_fn)
        delay = _human_delay(config, 'action')
        sleep_fn(delay)
        micro_pause(sleep_fn)

        result = ife(f'''
            try {{
                let api = new API();
                let r = await api.craft_item({{
                    recipeID: '{recipe["id"]}',
                    count: {batches}
                }});
                if (r.ok) return {{ok: true, count: {batches}}};
                else return {{ok: false, error: typeof r._data === 'string' ? r._data : JSON.stringify(r._data)}};
            }} catch(e) {{ return {{ok: false, error: e.message}}; }}
        ''')

        if result and result.get('ok'):
            count_crafted = result.get('count', batches)
            crafted += count_crafted
            logger.info(f"Crafted {count_crafted}x {recipe['name']}")
        else:
            err = result.get('error', '?') if result else 'No response'
            if 'not enough' in str(err).lower() or 'ingredient' in str(err).lower():
                continue  # Try next recipe
            results['errors'].append(f"Craft {recipe['name']}: {err}")

    results['crafted'] = crafted
    return results


# ── Smart Reward Pool Timing ─────────────────────────────────────────────────

def execute_smart_reward_pool(ife, config, amount=None, sleep_fn=time.sleep):
    """Submit products to reward pool with optimal timing.

    Tracks the 4-hour pool cycle and submits during the optimal window
    (just after reset, when there's less competition).
    """
    results = {'submitted': 0, 'errors': [], 'timing': 'unknown'}

    roi = get_roi_tracker()
    best_window = roi.get_best_pool_window()

    # Get pool status to determine time since last reset
    pool_status = ife('''
        try {
            let api = new API();
            if (typeof api.get_reward_pool_status === 'function') {
                let r = await api.get_reward_pool_status();
                if (r.ok) return JSON.parse(JSON.stringify(r._data));
            }
            if (typeof api.get_pool_info === 'function') {
                let r = await api.get_pool_info();
                return JSON.parse(JSON.stringify(r._data || r));
            }
            return {_info: 'no_pool_status_api'};
        } catch(e) { return {_error: e.message}; }
    ''')

    time_since_reset = 0
    if pool_status and isinstance(pool_status, dict):
        time_since_reset = pool_status.get('secondsSinceReset',
                            pool_status.get('timeSinceReset',
                            pool_status.get('elapsed', 0)))

    # Determine if we should submit now or wait
    if time_since_reset > 0:
        cycle_seconds = 14400  # 4 hours = 14400 seconds
        position_in_cycle = time_since_reset % cycle_seconds

        if position_in_cycle < best_window:
            wait = best_window - position_in_cycle
            if wait < 600:  # Wait if less than 10 min to optimal window
                logger.info(f"Pool timing: waiting {wait:.0f}s for optimal window (at {best_window}s)")
                results['timing'] = 'waiting_for_window'
                sleep_fn(wait)
            else:
                results['timing'] = 'early_submit'

        roi.record_pool_timing("cfb", position_in_cycle, 0)

    # Execute the actual submission
    inv_items = fetch_inventory(ife)
    product_items = [
        i for i in inv_items
        if i.get('itemType') in ('farmProducts', 'products', 'animalProducts')
        and i.get('count', 0) > 0
    ]

    if not product_items:
        results['errors'].append("No products in inventory to submit")
        return results

    submitted = 0
    max_submit = amount if amount else min(50, sum(i.get('count', 0) for i in product_items))

    for item in product_items:
        if submitted >= max_submit:
            break
        remaining = max_submit - submitted
        iid = item.get('itemID', '')
        available = item.get('count', 0)
        to_send = min(remaining, available)

        micro_pause(sleep_fn)
        delay = _human_delay(config, 'action')
        sleep_fn(delay)
        micro_pause(sleep_fn)

        result = ife(f'''
            try {{
                let api = new API();
                let r = await api.reward_pool_add({{
                    itemID: '{iid}',
                    count: {to_send}
                }});
                if (r.ok) return {{ok: true, count: {to_send}}};
                else return {{ok: false, error: typeof r._data === 'string' ? r._data : JSON.stringify(r._data)}};
            }} catch(e) {{ return {{ok: false, error: e.message}}; }}
        ''')

        if result and result.get('ok'):
            submitted += to_send
            roi.record_pool_submit(item.get('itemCode', '?'), to_send)
            logger.info(f"Smart pool: submitted {to_send}x {item.get('itemCode', '?')}")
        else:
            err = result.get('error', '?') if result else 'No response'
            results['errors'].append(f"reward pool {iid}: {err}")

    results['submitted'] = submitted
    return results


# ── Event Auto-Detection ─────────────────────────────────────────────────────

def detect_active_events(page) -> List[dict]:
    """Scrape the HUB for active event banners and boosted rewards.

    Returns list of events with their details.
    """
    if not page:
        return []

    try:
        events = page.evaluate('''
            (() => {
                const events = [];
                const containers = document.querySelectorAll(
                    '[class*="event"], [class*="Event"], [class*="banner"], [class*="promo"]'
                );

                for (const el of containers) {
                    const text = el.textContent.toLowerCase();
                    if (!text) continue;

                    let name = '';
                    let type = 'unknown';
                    let reward = '';

                    const nameEl = el.querySelector('h1, h2, h3, h4, [class*="title"]');
                    if (nameEl) name = nameEl.textContent.trim();

                    if (text.includes('boost') || text.includes('bonus')) type = 'boost';
                    if (text.includes('event') || text.includes('special')) type = 'event';
                    if (text.includes('season') || text.includes('pass')) type = 'season';

                    const rewardMatch = text.match(/(\\d+\\.?\\d*x?)\\s*(reward|bonus|boost|cfb|chu)/i);
                    if (rewardMatch) reward = rewardMatch[1];

                    const linkEl = el.querySelector('a[href]');
                    const link = linkEl ? linkEl.getAttribute('href') : '';

                    events.push({name, type, reward, link, active: true});
                }

                return events.slice(0, 10);
            })()
        ''')

        if events and isinstance(events, list):
            logger.info(f"Detected {len(events)} active event(s)")
            return events

    except Exception as e:
        logger.debug(f"Event detection failed: {e}")

    return []


def get_event_strategy(events: List[dict]) -> Optional[str]:
    """Return strategy recommendation based on active events."""
    if not events:
        return None

    boost_events = [e for e in events if e.get('type') in ('boost', 'event')]
    if not boost_events:
        return None

    strategies = []
    for e in boost_events:
        name = e.get('name', 'Unknown event')
        multiplier = e.get('reward', '')
        mult_str = f" ({multiplier})" if multiplier else ""
        strategies.append(f"Event active: {name}{mult_str}")

    strategy = "\n".join(strategies)
    strategy += "\nRecommendation: Prioritize event-boosted crops and actions."
    return strategy


# ── Helpers ──────────────────────────────────────────────────────────────────

def micro_pause(sleep_fn, max_s=0.3):
    sleep_fn(random.uniform(0.01, max_s))


def _human_delay(config, category='action'):
    """Human-like delay using triangular distribution."""
    lo = config.get('action_delay_min', 4)
    hi = config.get('action_delay_max', 10)
    base = lo + random.triangular(0, 1, 0.4) * (hi - lo)
    if random.random() < 0.04:
        base += random.uniform(15, 45)
    return max(lo * 0.5, min(hi * 2.5, base))


def fetch_inventory(ife):
    """Fetch current inventory items from the game API."""
    inv_data = ife('''
        try {
            let api = new API();
            let r = await api.get_user_inventory();
            if (!r.ok) return {_error: 'fetch_failed'};
            let items = JSON.parse(r._data.strData);
            return {items: items};
        } catch(e) { return {_error: e.message}; }
    ''')
    if inv_data is None or isinstance(inv_data, str) or '_error' in (inv_data or {}):
        return []
    items = inv_data.get('items', []) if isinstance(inv_data, dict) else []
    return [i for i in items if isinstance(i, dict) and i.get('count', 0) > 0]
