# Chainers.io API Reference

**Last Updated:** 2025-01-13  
**Game Version:** 1.15.28  
**Status:** Complete API inventory for UNCHAINED bot

---

## Table of Contents

1. [Safety Classification](#safety-classification)
2. [Game API Methods](#game-api-methods)
3. [Game Script Files](#game-script-files)
4. [HTTP Endpoints](#http-endpoints)
5. [Network Interception](#network-interception)
6. [Blacklisted APIs](#blacklisted-apis)
7. [Terms of Service Compliance](#terms-of-service-compliance)

---

## Safety Classification

### Legend

| Status | Meaning |
|--------|---------|
| ✅ SAFE | Mirrors normal human gameplay, low detection risk |
| ⚠️ RISKY | Could be detected as botting, use with caution |
| ❌ DANGEROUS | Explicitly violates ToS, high ban risk |

---

## Game API Methods

All methods are called via JavaScript `new API()` in the game client.

### Farming APIs

| Method | Status | Purpose | Notes |
|--------|--------|---------|-------|
| `api.get_user_gardens()` | ✅ SAFE | Fetch user's gardens + beds | Read-only, normal page load |
| `api.get_user_inventory()` | ✅ SAFE | Fetch inventory items | Read-only, normal page load |
| `api.get_beds_data()` | ✅ SAFE | Fetch bed type definitions | Read-only, normal page load |
| `api.get_seeds_data()` | ✅ SAFE | Fetch seed metadata | Read-only, normal page load |
| `api.collect_harvest({userFarmingID})` | ✅ SAFE | Collect a harvest | Normal harvest action |
| `api.plant_seed({userGardensID, userBedsID, seedID})` | ✅ SAFE | Plant a seed | Normal planting action |

### Animal APIs

| Method | Status | Purpose | Notes |
|--------|--------|---------|-------|
| `api.get_user_animals()` | ✅ SAFE | Fetch user's animals | Read-only, normal page load |
| `api.get_animals()` | ✅ SAFE | Fallback animal fetch | Read-only, normal page load |
| `api.feed_animal({userAnimalID})` | ✅ SAFE | Feed an animal | Normal feeding action |

### Mission APIs

| Method | Status | Purpose | Notes |
|--------|--------|---------|-------|
| `api.get_missions()` | ✅ SAFE | Fetch missions | Normal mission wall view |
| `api.getUserMissions()` | ✅ SAFE | Fallback mission fetch | Normal mission wall view |

### Reward Pool APIs

| Method | Status | Purpose | Notes |
|--------|--------|---------|-------|
| `api.reward_pool_add({itemID, count})` | ✅ SAFE | Submit products to reward pool | Normal reward submission |

### Crafting APIs (from CraftingAPI.js)

| Method | Status | Purpose | Notes |
|--------|--------|---------|-------|
| `api.craft_item({recipeID, count})` | ⚠️ RISKY | Craft items in workshop | Not yet implemented in bot |
| `api.get_recipes()` | ✅ SAFE | Fetch crafting recipes | Read-only |
| `api.get_workshop_items()` | ✅ SAFE | Fetch workshop inventory | Read-only |

### Fertilizer APIs (from FertilizersAPI.js)

| Method | Status | Purpose | Notes |
|--------|--------|---------|-------|
| `api.apply_fertilizer({userFarmingID, fertilizerID})` | ⚠️ RISKY | Apply fertilizer to crop | Not yet implemented in bot |
| `api.get_fertilizers()` | ✅ SAFE | Fetch fertilizer inventory | Read-only |

### Robot APIs (from RobotAPI2.js)

| Method | Status | Purpose | Notes |
|--------|--------|---------|-------|
| `api.robot_*()` | ❌ DANGEROUS | Robot automation | **BLACKLISTED** - Using "robot" APIs is explicitly admitting to botting |

---

## Game Script Files

Loaded from `static.chainers.io/webgames/farm/1.15.28/`

| File | Status | Purpose |
|------|--------|---------|
| `utils.js` | ✅ SAFE | Utility functions |
| `events.js` | ✅ SAFE | Event system |
| `WSocket.js` | ✅ SAFE | WebSocket communication |
| `MemoryManager.js` | ✅ SAFE | Memory management |
| `Data.js` | ✅ SAFE | Data structures |
| `Network.js` | ✅ SAFE | Network layer |
| `db.js` | ✅ SAFE | Database abstraction |
| `api.js` | ✅ SAFE | Main API class definition |
| `SceneManager.js` | ✅ SAFE | Scene management |
| `CraftingGroup.js` | ✅ SAFE | Crafting group logic |
| `CraftingOffer.js` | ✅ SAFE | Crafting offer logic |
| `UserPendingOffer.js` | ✅ SAFE | Pending offer management |
| `CraftingAPI.js` | ⚠️ RISKY | Crafting automation APIs |
| `FertilizersAPI.js` | ⚠️ RISKY | Fertilizer automation APIs |
| `RewardPoolAPI.js` | ✅ SAFE | Reward pool APIs |
| `RobotAPI2.js` | ❌ DANGEROUS | Robot automation - **BLACKLISTED** |
| `timer.js` | ✅ SAFE | Timer utilities |

---

## HTTP Endpoints

### Game Endpoints

| Endpoint | Method | Status | Purpose |
|----------|--------|--------|---------|
| `https://chainers.io/game/farm` | GET | ✅ SAFE | Navigate to farm game |
| `https://chainers.io/game` | GET | ✅ SAFE | Game home page |
| `https://chainers.io{action_url}` | GET | ✅ SAFE | Mission action URLs |
| `https://static.chainers.io/webgames/farm/1.15.28/*` | GET | ✅ SAFE | Game script sources |

### Local Endpoints

| Endpoint | Method | Status | Purpose |
|----------|--------|--------|---------|
| `http://127.0.0.1:11434/api/tags` | GET | ✅ SAFE | Ollama health + model list |
| `http://127.0.0.1:11434/api/generate` | POST | ✅ SAFE | Ollama LLM inference |
| `https://ollama.com/download/OllamaSetup.exe` | GET | ✅ SAFE | Ollama installer download |

### External Endpoints

| Endpoint | Method | Status | Purpose |
|----------|--------|--------|---------|
| Discord webhook URL | POST | ✅ SAFE | Discord notifications |
| Dynamic claim URL | POST | ⚠️ RISKY | Mission reward claim (discovered via interceptor) |

---

## Network Interception

### Current Implementation

The bot installs a network interceptor that monkey-patches:
- `window.fetch` - captures all fetch requests + responses
- `XMLHttpRequest.prototype.open` / `.send` / `.setRequestHeader` - captures XHR traffic

### Status: ❌ DANGEROUS

**Why it's dangerous:**
1. Modifies game code at runtime
2. Could be detected by anti-cheat systems
3. Violates ToS section on "reverse engineering"
4. Used to discover claim API endpoints dynamically

**Recommendation:** Remove network interception, use known API endpoints directly.

---

## Blacklisted APIs

The following APIs are **explicitly blacklisted** and will not be used by UNCHAINED:

### 1. RobotAPI2.js (Entire Module)

**Reason:** The word "robot" in the API name is a direct admission of botting. Using these APIs could result in immediate ban.

**Blacklisted Methods:**
- All methods in `RobotAPI2.js`

### 2. Network Interception

**Reason:** Monkey-patching `fetch` and `XMLHttpRequest` modifies game code, violates ToS.

**Blacklisted Operations:**
- `window.fetch` monkey-patching
- `XMLHttpRequest.prototype.*` monkey-patching
- Dynamic API endpoint discovery via interception

### 3. Direct HTTP Manipulation

**Reason:** Bypassing the game client to make direct HTTP requests is detectable and violates ToS.

**Blacklisted Operations:**
- Direct `fetch()` calls to game APIs without going through Playwright
- Direct `XMLHttpRequest` calls to game APIs
- Any HTTP request that doesn't originate from the game client

---

## Terms of Service Compliance

### Relevant ToS Sections

From [chainers.io/terms](https://chainers.io/terms):

> **Section 3.G - Your Obligations:**
> - "use any robot, spider, site search/retrieval application, or other devices to retrieve or index any portion of the App"
> - "modify, adapt, translate, or reverse engineer any portion of the App"
> - "exploit the App for any unauthorized commercial purpose"

> **Section 3.G - Account Restrictions:**
> - "Users may only have one active account"

### Task Wall Warning

From [Task Wall Documentation](https://docs.chainers.io/chainers-docs/chainers/chainers-features/task-wall.md):

> ⚠️ **VPN Usage Warning:**
> - Using VPNs while accessing Task Wall may **violate task providers' policies**
> - Could result in restricted access or being banned from tasks ❌
> - Task providers may not credit rewards if VPN usage is detected

### Compliance Strategy

UNCHAINED operates in a **gray area**:

1. **What we do:**
   - Use Playwright to run a real browser (like a human)
   - Call official game APIs through the game client
   - Add human-like delays and randomization
   - Avoid blacklisted APIs

2. **What we don't do:**
   - Use "robot" APIs
   - Modify game code
   - Make direct HTTP requests
   - Use VPN with Task Wall
   - Run multiple accounts

3. **Risk mitigation:**
   - Sandbagging (deliberately suboptimal play)
   - Random delays between actions
   - Session breaks
   - Detection monitoring

---

## Implementation Notes

### API Call Wrapper

All API calls should go through a wrapper that:
1. Checks against the blacklist
2. Adds human-like delays
3. Logs the call for debugging
4. Handles errors gracefully

### Blacklist Enforcement

The blacklist is enforced in `bot_engine.py` via the `API_BLACKLIST` constant:

```python
API_BLACKLIST = [
    "robot_",           # All robot APIs
    "intercept_",       # Network interception
    "monkey_patch_",    # Code modification
]
```

Any API method matching these patterns will be blocked.

---

## Changelog

| Date | Change |
|------|--------|
| 2025-01-13 | Initial API inventory and blacklist created |

---

## References

- [Chainers Terms of Service](https://chainers.io/terms)
- [Chainers Documentation](https://docs.chainers.io/chainers-docs)
- [Chainers Farm Guide](https://docs.chainers.io/chainers-docs/chainers/chainers-farm.md)
- [Task Wall Documentation](https://docs.chainers.io/chainers-docs/chainers/chainers-features/task-wall.md)
