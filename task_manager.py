"""Task manager for UNCHAINED — reads the in-game mission wall,
assesses feasibility against inventory/garden state, and auto-selects
the best tasks to pursue.

The missions popup lives on the parent chainers.io page (Next.js),
not inside the static.chainers.io game iframe.  Two strategies:

  1. DOM scraping via Playwright page object
  2. API interception (listen for mission JSON payloads)
"""

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Optional, Dict, Any

logger = logging.getLogger("unchained.tasks")


class TaskType(Enum):
    FARMING = "farming"
    CRAFTING = "crafting"
    COLLECTION = "collection"
    FEEDING = "feeding"
    EXTERNAL = "external"
    META = "meta"
    UNKNOWN = "unknown"


class TaskStatus(Enum):
    AVAILABLE = "available"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CLAIMED = "claimed"


TASK_TITLE_PATTERNS = [
    (re.compile(r"plant\s+(\d+)\s+(\w[\w\s]*?)\s+seeds?", re.I), TaskType.FARMING),
    (re.compile(r"harvest\s+(\d+)", re.I), TaskType.FARMING),
    (re.compile(r"feed\s+(\d+)\s+fuzzy\s+animal", re.I), TaskType.FEEDING),
    (re.compile(r"feed\s+(\d+)", re.I), TaskType.FEEDING),
    (re.compile(r"put\s+(\d+)\s+product\s+into\s+.*reward\s*pool", re.I), TaskType.COLLECTION),
    (re.compile(r"earn\s+\d+.*(?:CFB|token).*cpx", re.I), TaskType.EXTERNAL),
    (re.compile(r"complete\s+daily\s+missions?", re.I), TaskType.META),
    (re.compile(r"craft\s+(\d+)", re.I), TaskType.CRAFTING),
    (re.compile(r"sell\s+(\d+)", re.I), TaskType.COLLECTION),
]


@dataclass
class Mission:
    task_id: str = ""
    title: str = ""
    task_type: TaskType = TaskType.UNKNOWN
    progress_current: int = 0
    progress_required: int = 0
    reward_name: str = ""
    reward_count: int = 0
    reward_icon_url: str = ""
    status: TaskStatus = TaskStatus.AVAILABLE
    can_claim: bool = False
    is_completed: bool = False
    claim_button_text: str = ""
    action_url: str = ""
    tab: str = "daily"
    raw_title: str = ""
    feasibility_score: float = 0.0
    feasibility_reasons: List[str] = field(default_factory=list)

    @property
    def progress_pct(self):
        if self.progress_required <= 0:
            return 0.0
        return min(1.0, self.progress_current / self.progress_required)

    @property
    def remaining(self):
        return max(0, self.progress_required - self.progress_current)

    @property
    def is_done(self):
        return self.progress_required > 0 and self.progress_current >= self.progress_required

    def to_dict(self):
        d = asdict(self)
        d["task_type"] = self.task_type.value
        d["status"] = self.status.value
        d["progress_pct"] = round(self.progress_pct, 3)
        d["remaining"] = self.remaining
        d["is_done"] = self.is_done
        return d


def classify_task(title: str) -> TaskType:
    title_lower = title.lower()
    for pattern, ttype in TASK_TITLE_PATTERNS:
        m = pattern.search(title_lower)
        if m:
            return ttype
    if "seed" in title_lower or "plant" in title_lower or "harvest" in title_lower:
        return TaskType.FARMING
    if "craft" in title_lower:
        return TaskType.CRAFTING
    if "feed" in title_lower:
        return TaskType.FEEDING
    if "reward pool" in title_lower or "product" in title_lower:
        return TaskType.COLLECTION
    if "cpx" in title_lower or "external" in title_lower or "offer" in title_lower:
        return TaskType.EXTERNAL
    return TaskType.UNKNOWN


def parse_task_id_from_attr(attr_str: str) -> str:
    m = re.search(r"task-([a-f0-9]+)-\d+", attr_str)
    return m.group(1) if m else ""


JS_SCRAPE_MISSIONS = """
(async () => {
    const results = [];
    const tabNames = ['daily', 'event', 'weekly', 'monthly', 'taskwall'];
    const tabButtons = document.querySelectorAll('.missions-nav-panel-wrapper .tabs-button');

    for (let ti = 0; ti < tabButtons.length && ti < tabNames.length; ti++) {
        const tabName = tabNames[ti];
        if (tabName === 'taskwall') continue;

        tabButtons[ti].click();
        await new Promise(r => setTimeout(r, 300));

        const items = document.querySelectorAll('.missions-tab-content-wrapper .missions-tab-content-wrapper-item');
        for (const item of items) {
            const titleEl = item.querySelector('.mission-line-title');
            const progressEl = item.querySelector('.mission-line-progress-bar-text');
            const fillEl = item.querySelector('.mission-line-progress-bar-fill');
            const rewardCountEl = item.querySelector('.reward-preview-count');
            const rewardImgEl = item.querySelector('.reward-preview-container img[alt="reward"]');
            const claimBtn = item.querySelector('.mission-line-button-claim');
            const bonusEl = item.querySelector('.mission-line-bonus');
            const containerEl = item.querySelector('.mission-line-container');

            const title = titleEl ? titleEl.textContent.trim() : '';
            const progressText = progressEl ? progressEl.textContent.trim() : '0 / 0';
            const fillStyle = fillEl ? fillEl.getAttribute('style') : '';
            const rewardCount = rewardCountEl ? rewardCountEl.textContent.trim() : '0';
            const rewardImg = rewardImgEl ? rewardImgEl.getAttribute('src') : '';
            const claimHref = claimBtn ? claimBtn.getAttribute('href') : '';
            const claimText = claimBtn ? claimBtn.textContent.trim().toLowerCase() : '';
            const claimVisible = !!claimBtn && claimText.includes('claim');
            const hasCompleted = containerEl ? containerEl.classList.contains('red') : false;

            const progressMatch = progressText.match(/(\\d+)\\s*\\/\\s*(\\d+)/);
            const current = progressMatch ? parseInt(progressMatch[1]) : 0;
            const required = progressMatch ? parseInt(progressMatch[2]) : 0;

            const fillMatch = fillStyle ? fillStyle.match(/width:\\s*([\\d.]+)%/) : null;
            const fillPct = fillMatch ? parseFloat(fillMatch[1]) : 0;

            let taskId = '';
            const imgs = item.querySelectorAll('img[id]');
            for (const img of imgs) {
                const id = img.getAttribute('id') || '';
                if (id.startsWith('task-')) {
                    const m = id.match(/task-([a-f0-9]+)-/);
                    if (m) { taskId = m[1]; break; }
                }
            }

            results.push({
                tab: tabName,
                task_id: taskId,
                title: title,
                progress_current: current,
                progress_required: required,
                progress_pct: fillPct,
                reward_count: parseInt(rewardCount) || 0,
                reward_icon: rewardImg,
                action_url: claimHref,
                can_claim: claimVisible,
                claim_button_text: claimBtn ? claimBtn.textContent.trim() : '',
                is_completed_style: hasCompleted
            });
        }
    }
    return results;
})()
"""

JS_OPEN_MISSIONS_POPUP = """
(() => {
    const missionBtns = document.querySelectorAll('[class*="mission"], [class*="Mission"]');
    for (const btn of missionBtns) {
        if (btn.textContent.includes('Mission') || btn.textContent.includes('mission') ||
            btn.getAttribute('class')?.includes('mission')) {
            btn.click();
            return true;
        }
    }
    const allBtns = document.querySelectorAll('button, a, div[role="button"]');
    for (const btn of allBtns) {
        const text = btn.textContent.toLowerCase();
        if (text.includes('mission') || text.includes('task') || text.includes('daily')) {
            btn.click();
            return true;
        }
    }
    return false;
})()
"""

JS_INSTALL_INTERCEPTOR = """
(() => {
    if (window.__unchained_interceptor_installed) return true;

    window.__unchained_captured_requests = [];
    const origFetch = window.fetch;
    window.fetch = function(...args) {
        const req = { type: 'fetch', url: '', method: 'GET', body: null, headers: {}, response: null, timestamp: Date.now() };
        try {
            if (args[0] instanceof Request) {
                req.url = args[0].url;
                req.method = args[0].method || 'GET';
                req.headers = Object.fromEntries(args[0].headers?.entries?.() || []);
            } else {
                req.url = String(args[0]);
                req.method = (args[1]?.method || 'GET').toUpperCase();
                req.headers = args[1]?.headers || {};
            }
            if (args[1]?.body) {
                req.body = typeof args[1].body === 'string' ? args[1].body : JSON.stringify(args[1].body);
            }
        } catch(e) {}

        return origFetch.apply(this, args).then(resp => {
            const clone = resp.clone();
            clone.text().then(text => {
                req.response = text.substring(0, 2000);
                req.status = resp.status;
                window.__unchained_captured_requests.push(req);
                if (window.__unchained_captured_requests.length > 50) {
                    window.__unchained_captured_requests.shift();
                }
            }).catch(() => {});
            return resp;
        });
    };

    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url, ...rest) {
        this.__captured = { type: 'xhr', url: String(url), method: (method || 'GET').toUpperCase(), body: null, headers: {}, response: null, timestamp: Date.now() };
        return origOpen.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.send = function(body) {
        if (this.__captured) {
            this.__captured.body = body ? String(body).substring(0, 2000) : null;
            this.addEventListener('load', () => {
                try {
                    this.__captured.response = (this.responseText || '').substring(0, 2000);
                    this.__captured.status = this.status;
                    window.__unchained_captured_requests.push(this.__captured);
                    if (window.__unchained_captured_requests.length > 50) {
                        window.__unchained_captured_requests.shift();
                    }
                } catch(e) {}
            });
        }
        return origSend.call(this, body);
    };

    const origSetHeader = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
        if (this.__captured) {
            this.__captured.headers[name] = value;
        }
        return origSetHeader.call(this, name, value);
    };

    window.__unchained_interceptor_installed = true;
    return true;
})()
"""

JS_GET_CAPTURED = """
(() => {
    const reqs = window.__unchained_captured_requests || [];
    window.__unchained_captured_requests = [];
    return reqs;
})()
"""


class TaskManager:
    """Reads, assesses, and prioritizes missions from the game's mission wall."""

    def __init__(self):
        self._missions: List[Mission] = []
        self._last_fetch: float = 0
        self._fetch_interval: float = 60
        self._config: Dict[str, Any] = {}
        self._claim_api_pattern: Optional[Dict[str, Any]] = None
        self._interceptor_installed: bool = False

    @property
    def missions(self):
        return list(self._missions)

    def set_config(self, config):
        self._config = config
        self._fetch_interval = config.get("task_wall_refresh_seconds", 60)

    def get_available_tasks(self):
        return [m for m in self._missions if not m.is_done]

    def get_completed_tasks(self):
        return [m for m in self._missions if m.is_done]

    def get_best_tasks(self, inventory_items=None, garden_capacity=None):
        available = self.get_available_tasks()
        if not available:
            return []

        for mission in available:
            self._assess_feasibility(mission, inventory_items, garden_capacity)

        scored = sorted(available, key=lambda m: (-m.feasibility_score, -m.progress_pct))
        return scored

    def _assess_feasibility(self, mission: Mission, inventory_items=None, garden_capacity=None):
        score = 0.0
        reasons = []

        mission.progress_pct

        if mission.progress_pct >= 0.5:
            score += 3.0
            reasons.append(f"Already {mission.progress_pct:.0%} done")
        elif mission.progress_pct > 0:
            score += 1.5
            reasons.append(f"Started ({mission.progress_pct:.0%})")

        if mission.task_type == TaskType.FARMING:
            score += 2.0
            reasons.append("Farming task — bot can handle")
        elif mission.task_type == TaskType.FEEDING:
            score += 1.0
            reasons.append("Feeding — needs animal API")
        elif mission.task_type == TaskType.CRAFTING:
            score += 0.5
            reasons.append("Crafting — may need specific recipes")
        elif mission.task_type == TaskType.COLLECTION:
            score += 0.5
            reasons.append("Collection — depends on inventory")
        elif mission.task_type == TaskType.EXTERNAL:
            score -= 5.0
            reasons.append("External/CPX — cannot automate")
        elif mission.task_type == TaskType.META:
            score -= 3.0
            reasons.append("Meta task — depends on other tasks")

        if mission.task_type == TaskType.FARMING:
            title_lower = mission.raw_title.lower()
            if "garlic" in title_lower:
                score += 1.0
                reasons.append("Specific seed: garlic")
            if "plant" in title_lower and inventory_items:
                seed_match = re.search(r"plant\s+\d+\s+(\w[\w\s]*?)\s+seeds?", title_lower)
                if seed_match:
                    needed_seed = seed_match.group(1).strip()
                    for item in inventory_items:
                        code = item.get("itemCode", "").lower()
                        if needed_seed.lower() in code and item.get("count", 0) > 0:
                            score += 2.0
                            reasons.append(f"Have {needed_seed} seeds in inventory")
                            break
                    else:
                        score -= 1.0
                        reasons.append(f"Need {needed_seed} seeds — not found in inventory")

        if mission.remaining <= 3 and mission.remaining > 0:
            score += 2.0
            reasons.append(f"Only {mission.remaining} more to go")
        elif mission.remaining <= 10 and mission.remaining > 0:
            score += 1.0
            reasons.append(f"{mission.remaining} remaining — manageable")

        if mission.reward_count >= 5:
            score += 1.5
            reasons.append(f"Good reward: {mission.reward_count}x")
        elif mission.reward_count >= 2:
            score += 0.5
            reasons.append(f"Reward: {mission.reward_count}x")

        mission.feasibility_score = score
        mission.feasibility_reasons = reasons

    def fetch_missions_from_page(self, page):
        """Scrape missions from the parent page DOM using Playwright.

        Args:
            page: Playwright Page object (the main chainers.io page)
        """
        try:
            opened = page.evaluate(JS_OPEN_MISSIONS_POPUP)
            if opened:
                time.sleep(0.5)

            raw = page.evaluate(JS_SCRAPE_MISSIONS)
            if not raw or not isinstance(raw, list):
                logger.warning("Task wall scrape returned no data")
                return []

            missions = []
            for item in raw:
                m = Mission(
                    task_id=item.get("task_id", ""),
                    title=item.get("title", ""),
                    task_type=classify_task(item.get("title", "")),
                    progress_current=int(item.get("progress_current", 0) or 0),
                    progress_required=int(item.get("progress_required", 0) or 0),
                    reward_count=int(item.get("reward_count", 0) or 0),
                    reward_icon_url=item.get("reward_icon", ""),
                    can_claim=bool(item.get("can_claim", False)),
                    is_completed=bool(item.get("is_completed_style", False)),
                    claim_button_text=item.get("claim_button_text", ""),
                    action_url=item.get("action_url", ""),
                    tab=item.get("tab", "daily"),
                    raw_title=item.get("title", ""),
                )
                missions.append(m)

            self._missions = missions
            self._last_fetch = time.time()
            logger.info(f"Task wall: scraped {len(missions)} missions across tabs")
            return missions

        except Exception as e:
            logger.error(f"Failed to scrape mission wall: {e}")
            return []

    def fetch_missions_via_game_api(self, ife):
        """Try fetching missions through the game's internal API if available.

        Args:
            ife: The game's JS evaluate callable
        """
        try:
            raw = ife('''
                try {
                    let api = new API();
                    if (typeof api.get_missions === 'function') {
                        let r = await api.get_missions();
                        return JSON.parse(JSON.stringify(r._data || r));
                    }
                    if (typeof api.getUserMissions === 'function') {
                        let r = await api.getUserMissions();
                        return JSON.parse(JSON.stringify(r._data || r));
                    }
                    return {_error: 'no_missions_api'};
                } catch(e) { return {_error: e.message}; }
            ''')

            if isinstance(raw, dict) and "_error" in raw:
                logger.debug(f"Game API missions not available: {raw['_error']}")
                return []

            if isinstance(raw, list):
                missions = []
                for item in raw:
                    m = Mission(
                        task_id=str(item.get("id", item.get("missionID", ""))),
                        title=item.get("title", item.get("name", "")),
                        task_type=classify_task(item.get("title", item.get("name", ""))),
                        progress_current=item.get("current", item.get("progress", 0)),
                        progress_required=item.get("required", item.get("target", 0)),
                        reward_count=item.get("rewardCount", item.get("reward", 0)),
                        reward_name=item.get("rewardName", item.get("rewardItem", "")),
                        raw_title=item.get("title", item.get("name", "")),
                    )
                    missions.append(m)

                self._missions = missions
                self._last_fetch = time.time()
                logger.info(f"Task wall (API): fetched {len(missions)} missions")
                return missions

        except Exception as e:
            logger.debug(f"Game API mission fetch failed: {e}")

        return []

    def refresh(self, page=None, ife=None, force=False):
        """Fetch missions from the best available source."""
        if not force and time.time() - self._last_fetch < self._fetch_interval:
            return self._missions

        if ife:
            result = self.fetch_missions_via_game_api(ife)
            if result:
                return result

        if page:
            return self.fetch_missions_from_page(page)

        return self._missions

    def mission_action(self, page, mission: Mission):
        """Navigate to a mission's action URL or trigger the claim via API."""
        if not page:
            return False

        try:
            if mission.can_claim and mission.task_id:
                return self.claim_via_api(page, mission)

            if mission.action_url:
                if mission.action_url.startswith("/game/"):
                    page.goto(f"https://chainers.io{mission.action_url}")
                elif mission.action_url.startswith("http"):
                    page.goto(mission.action_url)
                else:
                    page.goto(f"https://chainers.io/{mission.action_url}")
                mission.status = TaskStatus.IN_PROGRESS
                return True

        except Exception as e:
            logger.error(f"Failed to execute mission action: {e}")

        return False

    def install_interceptor(self, page):
        """Install the network interceptor on the parent page."""
        if self._interceptor_installed:
            return True
        try:
            result = page.evaluate(JS_INSTALL_INTERCEPTOR)
            self._interceptor_installed = bool(result)
            if self._interceptor_installed:
                logger.info("Network interceptor installed on parent page")
            return self._interceptor_installed
        except Exception as e:
            logger.warning(f"Failed to install interceptor: {e}")
            return False

    def discover_claim_api(self, page):
        """Discover the claim API by intercepting network traffic.

        Installs the interceptor, clicks the first claim button,
        captures the resulting API call, and stores the endpoint pattern.
        """
        if self._claim_api_pattern:
            return self._claim_api_pattern

        if not self.install_interceptor(page):
            return None

        try:
            result = page.evaluate(JS_OPEN_MISSIONS_POPUP)
            if result:
                time.sleep(0.5)
        except Exception:
            pass

        try:
            clicked = page.evaluate("""
                (() => {
                    const btns = document.querySelectorAll('.mission-line-button-claim');
                    for (const btn of btns) {
                        const text = btn.textContent.toLowerCase().trim();
                        if (text.includes('claim') && !text.includes('claimed')) {
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                })()
            """)

            if not clicked:
                logger.debug("No claimable button found for API discovery")
                return None

            time.sleep(1.5)

            captured = page.evaluate(JS_GET_CAPTURED)
            if not captured or not isinstance(captured, list):
                return None

            claim_reqs = [
                r for r in captured
                if r.get('url') and 'claim' in r.get('url', '').lower()
                or 'mission' in r.get('url', '').lower()
                or 'reward' in r.get('url', '').lower()
            ]

            if not claim_reqs:
                claim_reqs = [
                    r for r in captured
                    if r.get('method') in ('POST', 'PUT', 'PATCH')
                    and r.get('status', 0) in (200, 201)
                ]

            if not claim_reqs:
                logger.debug("No claim API call captured")
                return None

            req = claim_reqs[-1]
            self._claim_api_pattern = {
                'url': req['url'],
                'method': req.get('method', 'POST'),
                'headers': req.get('headers', {}),
                'body_template': req.get('body'),
                'response_example': req.get('response'),
            }
            logger.info(f"Discovered claim API: {req.get('method')} {req['url'][:120]}")
            return self._claim_api_pattern

        except Exception as e:
            logger.warning(f"Claim API discovery failed: {e}")
            return None

    def claim_via_api(self, page, mission: Mission):
        """Claim a mission reward via API call instead of DOM clicking.

        Uses the discovered API endpoint pattern, or falls back to
        finding the matching claim button and using its href.
        """
        if not self.install_interceptor(page):
            return self._claim_reward_fallback(page, mission)

        pattern = self._claim_api_pattern
        if not pattern:
            pattern = self.discover_claim_api(page)

        if pattern:
            return self._claim_with_pattern(page, mission, pattern)

        return self._claim_reward_fallback(page, mission)

    def _claim_with_pattern(self, page, mission: Mission, pattern: Dict):
        """Execute a claim using the discovered API pattern."""
        try:
            url = pattern['url']
            method = pattern.get('method', 'POST')
            headers = pattern.get('headers', {})
            body = pattern.get('body_template')

            if mission.task_id and body:
                import re as _re
                body = _re.sub(r'"[a-f0-9]{24}"', f'"{mission.task_id}"', body)
                if mission.task_id not in body:
                    if '{task_id}' in body:
                        body = body.replace('{task_id}', mission.task_id)
                    elif '{id}' in body:
                        body = body.replace('{id}', mission.task_id)

            if mission.task_id and '{task_id}' in url:
                url = url.replace('{task_id}', mission.task_id)

            headers_json = json.dumps(headers)
            body_json = json.dumps(body) if body else 'null'

            result = page.evaluate(f"""
                (async () => {{
                    try {{
                        const resp = await fetch("{url}", {{
                            method: "{method}",
                            headers: {headers_json},
                            body: {body_json},
                            credentials: 'include'
                        }});
                        const text = await resp.text();
                        return {{ ok: resp.ok, status: resp.status, body: text.substring(0, 2000) }};
                    }} catch(e) {{
                        return {{ ok: false, error: e.message }};
                    }}
                }})()
            """)

            if result and result.get('ok'):
                mission.status = TaskStatus.CLAIMED
                mission.can_claim = False
                logger.info(f"API claim succeeded for: {mission.title} (task_id={mission.task_id})")
                return True
            else:
                err = result.get('error', result.get('body', '?')) if result else 'No response'
                logger.warning(f"API claim failed for {mission.title}: {err}")
                return self._claim_reward_fallback(page, mission)

        except Exception as e:
            logger.warning(f"API claim error: {e}")
            return self._claim_reward_fallback(page, mission)

    def _claim_reward_fallback(self, page, mission: Mission):
        """Fallback: find and click the specific claim button matching this mission."""
        try:
            escaped_title = mission.title.replace("'", "\\'").replace('"', '\\"')
            result = page.evaluate(f"""
                (() => {{
                    const items = document.querySelectorAll('.missions-tab-content-wrapper-item');
                    for (const item of items) {{
                        const titleEl = item.querySelector('.mission-line-title');
                        if (!titleEl) continue;
                        const titleText = titleEl.textContent.trim();
                        if (titleText !== '{escaped_title}') continue;
                        const btn = item.querySelector('.mission-line-button-claim');
                        if (!btn) continue;
                        const text = btn.textContent.toLowerCase().trim();
                        if (text.includes('claim') && !text.includes('claimed')) {{
                            btn.click();
                            return true;
                        }}
                    }}
                    return false;
                }})()
            """)

            if result:
                mission.status = TaskStatus.CLAIMED
                mission.can_claim = False
                logger.info(f"Fallback claim clicked for: {mission.title}")
                return True

            logger.warning(f"No claim button found for mission: {mission.title}")
            return False

        except Exception as e:
            logger.error(f"Fallback claim failed: {e}")
            return False

    def claim_all_completed(self, page):
        """Scan for and claim all completed missions with claimable buttons."""
        if not self.install_interceptor(page):
            return 0

        try:
            opened = page.evaluate(JS_OPEN_MISSIONS_POPUP)
            if opened:
                time.sleep(0.5)
        except Exception:
            pass

        claimed = 0
        for _ in range(10):
            result = page.evaluate("""
                (() => {
                    const items = document.querySelectorAll('.missions-tab-content-wrapper-item');
                    for (const item of items) {
                        const btn = item.querySelector('.mission-line-button-claim');
                        if (!btn) continue;
                        const text = btn.textContent.toLowerCase().trim();
                        if (text.includes('claim') && !text.includes('claimed')) {
                            const titleEl = item.querySelector('.mission-line-title');
                            const title = titleEl ? titleEl.textContent.trim() : '';
                            btn.click();
                            return { clicked: true, title: title };
                        }
                    }
                    return { clicked: false };
                })()
            """)

            if not result or not result.get('clicked'):
                break

            title = result.get('title', '?')
            logger.info(f"Claimed reward: {title}")
            claimed += 1
            time.sleep(1.0)

            for m in self._missions:
                if m.title == title and m.can_claim:
                    m.status = TaskStatus.CLAIMED
                    m.can_claim = False
                    break

        if claimed > 0:
            logger.info(f"Claimed {claimed} reward(s) from completed missions")

        return claimed

    def summary(self):
        available = self.get_available_tasks()
        completed = self.get_completed_tasks()
        return {
            "total": len(self._missions),
            "available": len(available),
            "completed": len(completed),
            "by_type": {
                t.value: len([m for m in self._missions if m.task_type == t])
                for t in TaskType
            },
        }
