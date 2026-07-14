"""Bot engine for UNCHAINED â€” farm automation via game-internal API calls.
Adapted from main.py to work with QWebEnginePage (or any run_js callable).
"""

import json as _json
import logging
import os
import re as re_mod
import random
import sys
import time

from config import load_config
from plot_config import SeedConfig
from api_guard import get_api_guard, is_api_allowed, is_js_safe
from bot_advanced import (
    execute_craft_task, execute_smart_reward_pool, detect_active_events,
    get_event_strategy, get_mistake_engine, get_roi_tracker,
    simulate_human_interaction, _human_delay as advanced_delay,
)

logger = logging.getLogger('unchained')

JS_RESERVED = {
    'abstract', 'arguments', 'await', 'boolean', 'break', 'byte', 'case', 'catch',
    'char', 'class', 'const', 'continue', 'debugger', 'default', 'delete', 'do',
    'double', 'else', 'enum', 'export', 'extends', 'false', 'final', 'finally',
    'float', 'for', 'function', 'goto', 'if', 'implements', 'import', 'in',
    'instanceof', 'int', 'interface', 'let', 'long', 'native', 'new', 'null',
    'package', 'private', 'protected', 'public', 'return', 'short', 'static',
    'super', 'switch', 'synchronized', 'this', 'throw', 'throws', 'transient',
    'true', 'try', 'typeof', 'var', 'void', 'volatile', 'while', 'with', 'yield',
}


def extract_names(src_str):
    class_names = re_mod.findall(r'^\s*class\s+(\w+)', src_str, re_mod.MULTILINE)
    func_names = re_mod.findall(r'^\s*function\s+(\w+)', src_str, re_mod.MULTILINE)
    async_func = re_mod.findall(r'^\s*async\s+function\s+(\w+)', src_str, re_mod.MULTILINE)
    export_class = re_mod.findall(r'^\s*export\s+default\s+class\s+(\w+)', src_str, re_mod.MULTILINE)
    all_names = set(class_names + func_names + async_func + export_class)
    return sorted(n for n in all_names if n not in JS_RESERVED)


def _plot_state_path():
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), 'plot_state.json')
    return os.path.join(os.getcwd(), 'plot_state.json')


class PlotTracker:
    """Prevents interacting with the same plot within cooldown_hours."""

    def __init__(self, state_file=None):
        self.state_file = state_file or _plot_state_path()
        self._state = {}
        self._dirty = False
        self._load()

    def _load(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    self._state = _json.load(f)
            except Exception:
                self._state = {}

    def save(self):
        with open(self.state_file, 'w') as f:
            _json.dump(self._state, f, indent=2)
        self._dirty = False

    def flush_if_dirty(self):
        if self._dirty:
            self.save()

    def can_interact(self, bed_id, cooldown_hours=24):
        ts = self._state.get(str(bed_id))
        if ts is None:
            return True
        return time.time() - ts >= cooldown_hours * 3600

    def mark_interacted(self, bed_id):
        self._state[str(bed_id)] = time.time()
        self._dirty = True

    def can_plant_seed_in_bed(self, bed_id, seed_id, rotation_hours=6):
        key = f"sb:{bed_id}:{seed_id}"
        ts = self._state.get(key)
        if ts is None:
            return True
        return time.time() - ts >= rotation_hours * 3600

    def mark_seed_planted(self, bed_id, seed_id):
        key = f"sb:{bed_id}:{seed_id}"
        self._state[key] = time.time()
        self._dirty = True

    def cleanup(self, max_age_hours=48):
        cutoff = time.time() - max_age_hours * 3600
        keep = {}
        for k, v in self._state.items():
            if isinstance(v, (int, float)):
                if v >= cutoff:
                    keep[k] = v
            else:
                keep[k] = v
        self._state = keep
        self.save()


def is_harvest_ready(planted_seed):
    from datetime import datetime, timezone
    date_growth = planted_seed.get('dateGrowth')
    if not date_growth:
        return False
    try:
        if isinstance(date_growth, str) and date_growth.isdigit():
            ts = int(date_growth) / 1000.0
        elif isinstance(date_growth, (int, float)):
            ts = date_growth / 1000.0 if date_growth > 1000000000000.0 else date_growth
        else:
            dt = datetime.fromisoformat(str(date_growth).replace('Z', '+00:00'))
            ts = dt.timestamp()
        return datetime.now(timezone.utc).timestamp() >= ts
    except Exception:
        return False


def micro_pause(sleep_fn=time.sleep):
    if random.random() < 0.4:
        sleep_fn(random.uniform(0.1, 0.6))


def human_delay(config, action_key='action'):
    lo = config.get(f"{action_key}_delay_min", 4)
    hi = config.get(f"{action_key}_delay_max", 10)

    base = random.triangular(lo, hi, lo + (hi - lo) * 0.25)

    if random.random() < 0.15:
        base += random.uniform(3, 18)

    if random.random() < 0.05:
        base += random.uniform(15, 45)

    return max(lo * 0.5, min(hi * 2.5, base))


def init_game(run_js, wait_for_ready=True):
    def ife(js):
        result = run_js(js)
        if isinstance(result, dict) and '_error' in result:
            raise RuntimeError(f"Bridge error: {result['_error']}")
        return result

    if wait_for_ready:
        logger.info('Waiting for game to initialize...')
        ready = ife('''
            let retries = 0;
            while (retries < 60) {
                try {
                    let app = pc.Application.getApplication();
                    if (app) return true;
                } catch(e) {}
                await new Promise(r => setTimeout(r, 1000));
                retries++;
            }
            return false;
        ''')
        if not ready:
            raise RuntimeError('Game did not initialize after 60s')

    logger.info('Fetching script URLs from asset registry...')
    raw_urls = ife('''
        let app = pc.Application.getApplication();
        if (!app) throw new Error('pc.Application not found â€” game may not be fully loaded');
        let list = app.assets.list().filter(a => a.type === 'script' && a.file && a.file.url);
        let r = {};
        let base = 'https://static.chainers.io/webgames/farm/1.15.28/';
        for (let a of list) r[a.name] = base + a.file.url;
        return r;
    ''')

    if not isinstance(raw_urls, dict):
        raise RuntimeError(f"Expected dict from asset registry, got: {type(raw_urls).__name__}: {str(raw_urls)[:200]}")

    script_urls = raw_urls

    targets = ['utils.js', 'Data.js', 'Network.js', 'db.js', 'api.js', 'WSocket.js', 'MemoryManager.js', 'SceneManager.js', 'FertilizersAPI.js', 'RewardPoolAPI.js', 'CraftingAPI.js', 'CraftingGroup.js', 'CraftingOffer.js', 'UserPendingOffer.js', 'timer.js', 'events.js']

    order = ['utils.js', 'events.js', 'WSocket.js', 'MemoryManager.js', 'Data.js', 'Network.js', 'db.js', 'api.js', 'SceneManager.js', 'CraftingGroup.js', 'CraftingOffer.js', 'UserPendingOffer.js', 'CraftingAPI.js', 'FertilizersAPI.js', 'RewardPoolAPI.js', 'timer.js']

    sources = ife('\n        let urls = ' + _json.dumps(script_urls) + ';\n        let results = {};\n        let targets = ' + _json.dumps(targets) + ';\n        let fetches = targets.map(async (name) => {\n            let url = urls[name];\n            if (!url) return [name, \'NO_URL\'];\n            try {\n                let resp = await fetch(url);\n                if (!resp.ok) return [name, \'HTTP_\' + resp.status];\n                return [name, await resp.text()];\n            } catch(e) {\n                return [name, \'FETCH_ERROR: \' + e.message];\n            }\n        });\n        return Object.fromEntries(await Promise.all(fetches));\n    ')

    per_names = {n: extract_names(sources.get(n, '')) for n in order}

    ife('\n        let sources = ' + _json.dumps(sources) + ';\n        let order = ' + _json.dumps(order) + ';\n        let nameMap = ' + _json.dumps(per_names) + ';\n        for (let name of order) {\n            let src = sources[name];\n            if (!src || typeof src !== \'string\' || src.startsWith(\'NO_URL\') || src.startsWith(\'HTTP_\') || src.startsWith(\'FETCH_ERROR\')) continue;\n            let names = nameMap[name] || [];\n            let suffix = names.map(n => \'if (typeof \' + n + \' !== "undefined") window.\' + n + \' = \' + n + \';\').join(\'\');\n            (0, eval)(src + \'\\n\' + suffix);\n        }\n    ')

    return ife


def safe_ife(ife, js_code, api_method=None):
    """Execute JavaScript with API guard checks.

    Args:
        ife: The JavaScript execution function
        js_code: The JavaScript code to execute
        api_method: Optional API method name to check against blacklist

    Returns:
        Result from ife() or None if blocked
    """
    guard = get_api_guard()

    if api_method:
        allowed, reason = guard.check_method(api_method)
        if not allowed:
            logger.warning(f"API Guard blocked call to {api_method}: {reason}")
            return None

    js_allowed, js_reason = guard.check_js_code(js_code)
    if not js_allowed:
        logger.warning(f"API Guard blocked JS code: {js_reason}")
        return None

    return ife(js_code)


def run_bot_cycle(ife, tracker, config, sleep_fn=time.sleep, seed_info=None, ml_max_actions=None):
    cooldown = config.get('cooldown_hours', 24)
    results = {'harvested': [], 'planted': [], 'errors': []}

    if random.random() < 0.08:
        logger.info('Just checking... no actions this cycle')
        return results

    max_cfg = config.get('max_actions_per_cycle', 0)
    if max_cfg > 0:
        max_actions = random.choices(
            list(range(max_cfg + 1)),
            weights=[max(1, 40 - abs(i - max_cfg // 2) * 10) for i in range(max_cfg + 1)]
        )[0]
    elif ml_max_actions is not None and ml_max_actions > 0:
        max_actions = random.choices(
            list(range(ml_max_actions + 1)),
            weights=[max(1, 40 - abs(i - ml_max_actions // 2) * 10) for i in range(ml_max_actions + 1)]
        )[0]
    else:
        max_actions = random.choices([0, 1, 2, 3, 4], weights=[2, 25, 40, 25, 8])[0]

    if max_actions == 0:
        logger.info('Nothing felt worth doing this cycle')
        return results

    actions_done = 0

    micro_pause(sleep_fn)

    setup_data = ife('''
        try {
            let api = new API();
            let [gardensRes, invRes, bedsRes, sdataRes] = await Promise.all([
                api.get_user_gardens(),
                api.get_user_inventory(),
                api.get_beds_data(),
                api.get_seeds_data()
            ]);
            let gardens = JSON.parse(JSON.stringify(gardensRes._data));
            let items = invRes.ok ? JSON.parse(invRes._data.strData) : [];
            let bedToSeedGroup = {};
            bedsRes._data.forEach(b => {
                if (b.type && b.type.groupCode) {
                    let sg = b.type.groupCode === "beds" ? "plants" : b.type.groupCode;
                    bedToSeedGroup[b.code] = sg;
                }
            });
            let seedToGroup = {};
            let seedInfo = {};
            sdataRes._data.forEach(s => {
                let code = s.code || s.name || '';
                if (s.type && s.type.groupCode) seedToGroup[code] = s.type.groupCode;
                seedInfo[code] = {
                    rarity: Number(s.rarity) || Number(s.tier) || 0,
                    price: Number(s.price) || Number(s.sellPrice) || 0,
                };
            });
            return {gardens, items, bedToSeedGroup, seedToGroup, seedInfo};
        } catch(e) { return {_error: e.message}; }
    ''')

    if isinstance(setup_data, dict) and '_error' in setup_data:
        logger.error(f"Setup fetch failed: {setup_data['_error']}")
        return results
    if not isinstance(setup_data, dict):
        logger.error(f"Unexpected setup data: {type(setup_data).__name__}")
        return results

    gardens_data = setup_data.get('gardens', [])
    items = setup_data.get('items', [])
    seeds = [i for i in items if isinstance(i, dict) and i.get('count', 0) > 0 and i.get('itemType') == 'farmSeeds']
    bed_to_seed = setup_data.get('bedToSeedGroup', {})
    seed_to_group = setup_data.get('seedToGroup', {})
    seed_info = setup_data.get('seedInfo', {})

    if not isinstance(gardens_data, list):
        logger.error(f"Unexpected gardens data: {type(gardens_data).__name__}")
        return results

    logger.info(f'Got {len(gardens_data)} gardens, {len(seeds)} seed types')
    if seeds:
        logger.info(f'  e.g. {seeds[0].get("itemCode")} x{seeds[0].get("count")}')

    seed_rotation_hours = config.get('seed_bed_rotation_hours', 6)
    sandbagging = config.get('sandbagging_enabled', True)
    avoid_best_chance = config.get('sandbagging_avoid_best_chance', 0.4)

    seed_cfg_obj = SeedConfig()
    seed_cfg = seed_cfg_obj.get_all()
    use_limited_seeds = seed_cfg_obj.is_use_limited_seeds()
    limited_threshold = seed_cfg_obj.get_limited_threshold()

    micro_pause(sleep_fn)

    actions = []
    for garden in gardens_data:
        gid = garden.get('userGardensID')
        if not gid:
            continue
        code = garden.get('code', '?')
        beds = garden.get('placedBeds') or []

        if random.random() < 0.25:
            logger.info(f"  Skipping '{code}' this cycle")
            continue

        logger.info(f"  '{code}': {len(beds)} beds")

        garden_config = seed_cfg.get(code, {})

        for bed in beds:
            bid = bed.get('userBedsID')
            if not bid:
                continue
            planted = bed.get('plantedSeed')

            if planted:
                uid = planted.get('userFarmingID')
                if not uid:
                    continue
                if not tracker.can_interact(bid, cooldown):
                    logger.info(f"    bed {bid[:10]}... occupied, on cooldown")
                    continue
                if not is_harvest_ready(planted):
                    logger.info(f"    bed {bid[:10]}... occupied, not ready to harvest")
                    continue
                actions.append(('harvest', gid, bid, uid, None, planted.get('seedCode', '?')))
                logger.info(f"    bed {bid[:10]}... harvest ready!")
            else:
                if not seeds:
                    logger.info(f"    bed {bid[:10]}... empty, no seeds in inventory")
                    continue

                bed_code = bed.get('itemCode')
                req_group = bed_to_seed.get(bed_code)
                bed_compat_key = 'incompat:' + bid
                failed_on_bed = tracker._state.get(bed_compat_key, [])
                if not isinstance(failed_on_bed, list):
                    failed_on_bed = []
                compat_seeds = []
                for s in seeds:
                    sid = s.get('itemID')
                    sc = s.get('itemCode', '')
                    if sid in failed_on_bed:
                        continue
                    if req_group and seed_to_group.get(sc) not in (req_group, None, ''):
                        continue
                    if not tracker.can_plant_seed_in_bed(bid, sc, seed_rotation_hours):
                        continue
                    allowed = garden_config.get(bed_code)
                    if allowed is not None and sc not in allowed:
                        continue
                    if not use_limited_seeds:
                        count = s.get('count', 0)
                        if count <= limited_threshold:
                            continue
                    compat_seeds.append(s)

                if not compat_seeds:
                    logger.info(f"    bed {bid[:10]}... empty, no compatible seeds (group={req_group})")
                    continue

                if sandbagging and len(compat_seeds) > 1:
                    def seed_val(s):
                        info = seed_info.get(s.get('itemCode', ''), {})
                        v = info.get('rarity', info.get('price', 0))
                        return v if isinstance(v, (int, float)) else 0

                    if random.random() < avoid_best_chance:
                        best = max(compat_seeds, key=seed_val)
                        compat_seeds = [s for s in compat_seeds if s.get('itemID') != best.get('itemID')]

                actions.append(('plant', gid, bid, None, compat_seeds, None))
                logger.info(f"    bed {bid[:10]}... can plant")

    random.shuffle(actions)
    logger.info(f"  {len(actions)} eligible actions, max {max_actions} this cycle")

    for action_type, gid, bid, uid, seeds_list, label in actions:
        if actions_done >= max_actions:
            break

        if random.random() < 0.12:
            logger.info(f"    Skipping {action_type} on {bid[:10]}... (just didn't feel like it)")
            continue

        micro_pause(sleep_fn)

        if action_type == 'harvest':
            delay = human_delay(config)
            logger.info(f"    Harvesting {label} ({bid[:10]}...) â€” waiting {delay:.0f}s")
            sleep_fn(delay)

            micro_pause(sleep_fn)
            result = ife("\n                try {\n                    let api = new API();\n                    let r = await api.collect_harvest({userFarmingID: '" + uid + "'});\n                    if (r.ok) return {ok: true, data: JSON.parse(JSON.stringify(r._data))};\n                    else return {ok: false, error: typeof r._data === 'string' ? r._data : JSON.stringify(r._data)};\n                } catch(e) { return {ok: false, error: e.message}; }\n            ")

            if result and result.get('ok'):
                tracker.mark_interacted(bid)
                results['harvested'].append({'bed': bid, 'seed': label})
                logger.info('      \u2713 Collected')
            else:
                err = result.get('error', '?') if result else 'No response'
                logger.warning(f"      Harvest failed: {err}")
                results['errors'].append(f"harvest {bid}: {err}")

            actions_done += 1

        elif action_type == 'plant':
            compat_seeds = seeds_list
            if not compat_seeds:
                continue

            seed = random.choice(compat_seeds)
            sid = seed.get('itemID')
            scode = seed.get('itemCode', '?')
            delay = human_delay(config)
            logger.info(f"    Planting {scode} ({bid[:10]}...) â€” waiting {delay:.0f}s")
            sleep_fn(delay)

            micro_pause(sleep_fn)
            result = ife("\n                try {\n                    let api = new API();\n                    let r = await api.plant_seed({userGardensID: '" + gid + "', userBedsID: '" + bid + "', seedID: '" + sid + "'});\n                    if (r.ok) return {ok: true, data: JSON.parse(JSON.stringify(r._data))};\n                    else return {ok: false, error: typeof r._data === 'string' ? r._data : JSON.stringify(r._data)};\n                } catch(e) { return {ok: false, error: e.message}; }\n            ")

            if result and result.get('ok'):
                tracker.mark_interacted(bid)
                tracker.mark_seed_planted(bid, scode)
                results['planted'].append({'bed': bid, 'seed': scode})
                logger.info('      \u2713 Planted')
            else:
                err = result.get('error', '?') if result else 'No response'
                logger.warning(f"      Plant failed: {err}")
                results['errors'].append(f"plant {bid}: {err}")
                if 'incompatible' in str(err).lower():
                    key = 'incompat:' + bid
                    failed = tracker._state.get(key, [])
                    if not isinstance(failed, list):
                        failed = []
                    failed.append(sid)
                    tracker._state[key] = failed
                    tracker.save()

            actions_done += 1

    tracker.flush_if_dirty()
    return results


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


def execute_plant_task(ife, tracker, config, seed_code, count, sleep_fn=time.sleep):
    """Attempt to plant a specific seed type in available empty beds.

    Returns dict with 'planted' list and 'errors' list.
    """
    results = {'planted': [], 'errors': []}

    seeds_data = ife('''
        try {
            let api = new API();
            let beds = await api.get_beds_data();
            let sdata = await api.get_seeds_data();
            let bedToSeedGroup = {};
            beds._data.forEach(b => {
                if (b.type && b.type.groupCode) {
                    bedToSeedGroup[b.code] = b.type.groupCode === "beds" ? "plants" : b.type.groupCode;
                }
            });
            let seedToGroup = {};
            sdata._data.forEach(s => {
                let code = s.code || s.name || '';
                if (s.type && s.type.groupCode) seedToGroup[code] = s.type.groupCode;
            });
            return {bedToSeedGroup, seedToGroup};
        } catch(e) { return {_error: e.message}; }
    ''')

    if not isinstance(seeds_data, dict) or '_error' in seeds_data:
        err = seeds_data.get('_error', 'Invalid response') if isinstance(seeds_data, dict) else 'No response'
        results['errors'].append(f"seed data fetch: {err}")
        return results

    bed_to_seed = seeds_data.get('bedToSeedGroup', {})
    seed_to_group = seeds_data.get('seedToGroup', {})
    seed_group = seed_to_group.get(seed_code, '')

    inv_items = fetch_inventory(ife)
    matching_seeds = [
        i for i in inv_items
        if i.get('itemType') == 'farmSeeds' and i.get('itemCode', '') == seed_code
    ]

    if not matching_seeds:
        results['errors'].append(f"No {seed_code} seeds in inventory")
        return results

    seed_item = matching_seeds[0]
    seed_id = seed_item.get('itemID', '')
    available_count = seed_item.get('count', 0)

    seed_cfg_obj = SeedConfig()
    use_limited = seed_cfg_obj.is_use_limited_seeds()
    limited_threshold = seed_cfg_obj.get_limited_threshold()
    if not use_limited and available_count <= limited_threshold:
        results['errors'].append(f"Skipped {seed_code}: limited quantity ({available_count} â‰¤ {limited_threshold})")
        return results

    to_plant = min(count, available_count)

    gardens_data = ife('''
        try {
            let api = new API();
            let r = await api.get_user_gardens();
            return JSON.parse(JSON.stringify(r._data));
        } catch(e) { return {_error: e.message}; }
    ''')

    if not isinstance(gardens_data, list):
        results['errors'].append("Failed to get gardens")
        return results

    empty_beds = []
    for garden in gardens_data:
        gid = garden.get('userGardensID')
        for bed in garden.get('placedBeds', []):
            bid = bed.get('userBedsID')
            if bed.get('plantedSeed'):
                continue
            if not tracker.can_interact(bid, config.get('cooldown_hours', 24)):
                continue
            bed_code = bed.get('itemCode')
            req_group = bed_to_seed.get(bed_code, '')
            if seed_group and req_group and seed_group != req_group:
                continue
            empty_beds.append((gid, bid))

            if len(empty_beds) >= to_plant:
                break
        if len(empty_beds) >= to_plant:
            break

    if not empty_beds:
        results['errors'].append(f"No empty compatible beds for {seed_code}")
        return results

    planted = 0
    for gid, bid in empty_beds[:to_plant]:
        micro_pause(sleep_fn)
        delay = human_delay(config, 'action')
        sleep_fn(delay)
        micro_pause(sleep_fn)

        result = ife("\n            try {\n                let api = new API();\n                let r = await api.plant_seed({userGardensID: '" + gid + "', userBedsID: '" + bid + "', seedID: '" + seed_id + "'});\n                if (r.ok) return {ok: true, data: JSON.parse(JSON.stringify(r._data))};\n                else return {ok: false, error: typeof r._data === 'string' ? r._data : JSON.stringify(r._data)};\n            } catch(e) { return {ok: false, error: e.message}; }\n        ")

        if result and result.get('ok'):
            tracker.mark_interacted(bid)
            tracker.mark_seed_planted(bid, seed_code)
            results['planted'].append({'bed': bid, 'seed': seed_code})
            planted += 1
            logger.info(f"Task plant: {seed_code} in bed {bid[:10]}... OK")
        else:
            err = result.get('error', '?') if result else 'No response'
            results['errors'].append(f"plant {bid}: {err}")
            logger.warning(f"Task plant failed: {err}")

        if planted >= to_plant:
            break

    return results


def execute_harvest_task(ife, tracker, config, count=None, sleep_fn=time.sleep):
    """Harvest all ready beds.  Returns dict with 'harvested' and 'errors'."""
    results = {'harvested': [], 'errors': []}
    cooldown = config.get('cooldown_hours', 24)

    gardens_data = ife('''
        try {
            let api = new API();
            let r = await api.get_user_gardens();
            return JSON.parse(JSON.stringify(r._data));
        } catch(e) { return {_error: e.message}; }
    ''')

    if not isinstance(gardens_data, list):
        results['errors'].append("Failed to get gardens")
        return results

    harvestable = []
    for garden in gardens_data:
        for bed in garden.get('placedBeds', []):
            planted = bed.get('plantedSeed')
            if not planted:
                continue
            bid = bed.get('userBedsID')
            uid = planted.get('userFarmingID')
            if not tracker.can_interact(bid, cooldown):
                continue
            if not is_harvest_ready(planted):
                continue
            harvestable.append((bid, uid, planted.get('seedCode', '?')))

    harvested = 0
    for bid, uid, label in harvestable:
        if count and harvested >= count:
            break
        micro_pause(sleep_fn)
        delay = human_delay(config, 'action')
        sleep_fn(delay)
        micro_pause(sleep_fn)

        result = ife("\n            try {\n                let api = new API();\n                let r = await api.collect_harvest({userFarmingID: '" + uid + "'});\n                if (r.ok) return {ok: true, data: JSON.parse(JSON.stringify(r._data))};\n                else return {ok: false, error: typeof r._data === 'string' ? r._data : JSON.stringify(r._data)};\n            } catch(e) { return {ok: false, error: e.message}; }\n        ")

        if result and result.get('ok'):
            tracker.mark_interacted(bid)
            results['harvested'].append({'bed': bid, 'seed': label})
            harvested += 1
            logger.info(f"Task harvest: {label} ({bid[:10]}...) OK")
        else:
            err = result.get('error', '?') if result else 'No response'
            results['errors'].append(f"harvest {bid}: {err}")

        if count is not None and harvested >= count:
            break

    return results


def execute_feed_task(ife, config, count=1, sleep_fn=time.sleep):
    """Attempt to feed fuzzy animals on the farm.

    Returns dict with 'fed' count and 'errors'.
    """
    results = {'fed': 0, 'errors': []}

    animals_raw = ife('''
        try {
            let api = new API();
            if (typeof api.get_user_animals === 'function') {
                let r = await api.get_user_animals();
                return JSON.parse(JSON.stringify(r._data || r));
            }
            if (typeof api.get_animals === 'function') {
                let r = await api.get_animals();
                return JSON.parse(JSON.stringify(r._data || r));
            }
            return {_error: 'no_animal_api'};
        } catch(e) { return {_error: e.message}; }
    ''')

    if isinstance(animals_raw, dict) and '_error' in animals_raw:
        results['errors'].append(f"Animal API: {animals_raw['_error']}")
        return results

    if not isinstance(animals_raw, list):
        results['errors'].append("Unexpected animal data")
        return results

    fed = 0
    for animal in animals_raw:
        if fed >= count:
            break
        aid = animal.get('userAnimalID') or animal.get('id') or animal.get('_id')
        hungry = animal.get('isHungry', True)
        if not hungry:
            continue

        micro_pause(sleep_fn)
        delay = human_delay(config, 'action')
        sleep_fn(delay)
        micro_pause(sleep_fn)

        result = ife('''
            try {
                let api = new API();
                let r = await api.feed_animal({userAnimalID: '''' + str(aid) + ''''});
                if (r.ok) return {ok: true};
                else return {ok: false, error: typeof r._data === 'string' ? r._data : JSON.stringify(r._data)};
            } catch(e) { return {ok: false, error: e.message}; }
        ''')

        if result and result.get('ok'):
            fed += 1
            logger.info(f"Task feed: animal {str(aid)[:10]}... OK")
        else:
            err = result.get('error', '?') if result else 'No response'
            results['errors'].append(f"feed {str(aid)[:10]}: {err}")

    results['fed'] = fed
    if fed == 0 and not results['errors']:
        results['errors'].append("No hungry animals found")
    return results


def execute_reward_pool_task(ife, config, amount, sleep_fn=time.sleep):
    """Put product into the CFB reward pool with smart timing optimization.

    Returns dict with 'submitted' count and 'errors'.
    """
    return execute_smart_reward_pool(ife, config, amount=amount, sleep_fn=sleep_fn)


def execute_task(ife, tracker, config, mission, sleep_fn=time.sleep):
    """Execute a single mission based on its type.

    Args:
        ife: game JS evaluator
        tracker: PlotTracker
        config: bot config dict
        mission: Mission object from task_manager
        sleep_fn: callable to sleep

    Returns:
        dict with results
    """
    from task_manager import TaskType

    title = mission.raw_title.lower()
    logger.info(f"Executing task: {mission.title} (type={mission.task_type.value})")

    if mission.task_type == TaskType.FARMING:
        if "harvest" in title:
            count_match = re_mod.search(r"harvest\s+(\d+)", title)
            count = int(count_match.group(1)) if count_match else None
            return execute_harvest_task(ife, tracker, config, count=count, sleep_fn=sleep_fn)
        else:
            count_match = re_mod.search(r"plant\s+(\d+)", title)
            count = int(count_match.group(1)) if count_match else 1
            seed_match = re_mod.search(r"plant\s+\d+\s+([\w\s]+?)\s+seeds?", title)
            seed_code = seed_match.group(1).strip() if seed_match else None
            if not seed_code:
                return {'planted': [], 'errors': ['Could not determine seed type from task title']}
            inv_items = fetch_inventory(ife)
            for item in inv_items:
                if item.get('itemType') == 'farmSeeds' and seed_code.lower() in item.get('itemCode', '').lower():
                    seed_code = item['itemCode']
                    break
            return execute_plant_task(ife, tracker, config, seed_code, count, sleep_fn=sleep_fn)

    elif mission.task_type == TaskType.FEEDING:
        count_match = re_mod.search(r"feed\s+(\d+)", title)
        count = int(count_match.group(1)) if count_match else 1
        return execute_feed_task(ife, config, count=count, sleep_fn=sleep_fn)

    elif mission.task_type == TaskType.COLLECTION:
        amount_match = re_mod.search(r"put\s+(\d+)", title)
        amount = int(amount_match.group(1)) if amount_match else 50
        return execute_reward_pool_task(ife, config, amount=amount, sleep_fn=sleep_fn)

    elif mission.task_type == TaskType.CRAFTING:
        count_match = re_mod.search(r"craft\s+(\d+)", title)
        count = int(count_match.group(1)) if count_match else None
        return execute_craft_task(ife, config, count=count, sleep_fn=sleep_fn)

    else:
        return {'skipped': True, 'errors': [f'Cannot automate task type: {mission.task_type.value}']}


def fetch_seed_config_data(ife, seed_config=None):
    raw = ife('''
        try {
            let api = new API();
            let gardens = (await api.get_user_gardens())._data;
            let beds = await api.get_beds_data();
            let sdata = await api.get_seeds_data();
            let inv = await api.get_user_inventory();
            let invMap = {};
            if (inv && inv.ok && inv._data && inv._data.strData) {
                let items = JSON.parse(inv._data.strData);
                items.forEach(i => {
                    if (i.itemType === 'farmSeeds' || i.itemCode) {
                        invMap[i.itemCode] = (invMap[i.itemCode] || 0) + (i.count || 0);
                    }
                });
            }

            let bedToSeedGroup = {};
            beds._data.forEach(b => {
                if (b.type && b.type.groupCode) {
                    bedToSeedGroup[b.code] = b.type.groupCode === "beds" ? "plants" : b.type.groupCode;
                }
            });

            let seedInfo = {};
            sdata._data.forEach(s => {
                let code = s.code || s.name || '';
                seedInfo[code] = {
                    code: code,
                    rarity: Number(s.rarity) || Number(s.tier) || 0,
                    groupCode: s.type && s.type.groupCode || '',
                    owned: invMap[code] || 0
                };
            });

            let result = [];
            for (let garden of gardens) {
                let bedTypeMap = {};
                for (let bed of (garden.placedBeds || [])) {
                    let code = bed.itemCode;
                    if (!bedTypeMap[code]) bedTypeMap[code] = 0;
                    bedTypeMap[code]++;
                }
                let bedTypes = [];
                for (let [code, count] of Object.entries(bedTypeMap)) {
                    let reqGroup = bedToSeedGroup[code] || '';
                    let compatSeeds = Object.values(seedInfo).filter(s => s.groupCode === reqGroup);
                    bedTypes.push({
                        itemCode: code,
                        count: count,
                        compatible_seeds: compatSeeds,
                        compatible_seed_count: compatSeeds.length,
                    });
                }
                result.push({
                    code: garden.code || garden.userGardensID,
                    userGardensID: garden.userGardensID,
                    bed_types: bedTypes,
                });
            }
            return result;
        } catch(e) { return {_error: e.message}; }
    ''')

    if not raw or (isinstance(raw, dict) and '_error' in raw):
        err = raw.get('_error', 'No response') if isinstance(raw, dict) else 'No response'
        raise RuntimeError(f"Seed config fetch failed: {err}")

    if not isinstance(raw, list):
        raise RuntimeError(f"Expected list from seed config fetch, got {type(raw).__name__}")

    existing = seed_config.get_all() if seed_config else {}
    for garden in raw:
        gc = garden.get('code', garden.get('userGardensID', '?'))
        for bt in garden.get('bed_types', []):
            bt['configured_seeds'] = existing.get(gc, {}).get(bt.get('itemCode', ''), [])

    return raw
