import json
import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000


def _data_dir():
    """Return the directory where config/state files should live."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.getcwd()


CONFIG_FILE = os.path.join(_data_dir(), 'config.json')
PROFILE_DIR = os.path.join(_data_dir(), 'profile')

DEFAULT_CONFIG = {
    'game_url': 'https://chainers.io/game',
    'user_data_dir': PROFILE_DIR,
    'headless': False,
    'action_delay_min': 4,
    'action_delay_max': 10,
    'cycle_delay_min': 90,
    'cycle_delay_max': 180,
    'cooldown_hours': 24,
    'max_consecutive_failures': 10,
    'session_duration_choices': list((2, 4, 6, 10)),
    'session_break_min_mins': 30,
    'session_break_max_mins': 360,
    'seed_bed_rotation_hours': 6,
    'sandbagging_enabled': True,
    'sandbagging_avoid_best_chance': 0.4,
    'offline_base_probability': 0.5,
    'offline_probability_jitter': 0.2,
    'offline_duration_hours': 24,
    'offline_check_interval_hours': 24,
    'ml_enabled': True,
    'ml_min_training_samples': 10,
    'ml_auto_retrain': True,
    'ml_anomaly_detection': True,
    'discord_webhook_url': '',
    'discord_token': '',
    'discord_channel_id': 1525245987461791765,
    'discord_notify_cycles': True,
    'discord_notify_errors': True,
    'discord_notify_daily': False,
    'task_wall_enabled': True,
    'task_wall_refresh_seconds': 60,
    'task_wall_auto_select': True,
    'task_wall_max_simultaneous': 3,
    'task_wall_prefer_farming': True,
    'task_wall_skip_external': True,
    'task_wall_claim_rewards': True,
    'auto_craft_enabled': True,
    'auto_craft_max_per_cycle': 3,
    'auto_craft_preferred_recipes': '',
    'auto_craft_reserve_ingredients': False,
    'auto_craft_min_ingredient_reserve': 5,
    'auto_craft_max_output_storage': 0,
    'auto_craft_enabled_recipes': list(),
    'auto_craft_known_recipes': list(),
    'llm_enabled': True,
    'llm_model': 'phi3:mini',
}


def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as f:
            saved = json.load(f)
        config.update(saved)
    except FileNotFoundError:
        pass
    except json.JSONDecodeError as e:
        logger.warning(f"Config file is corrupt, using defaults: {e}")
    except Exception as e:
        logger.error(f"Cannot read config file: {e}")
    return config


def save_config(config):
    import tempfile
    dir_ = os.path.dirname(CONFIG_FILE)
    fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(config, f, indent=2)
        os.replace(tmp_path, CONFIG_FILE)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _script_path():
    """Return the absolute directory containing this script."""
    return os.path.dirname(os.path.abspath(__file__))


def detect_chrome_exe():
    """Find Chrome/Chromium executable on the system."""
    candidates = [
        os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'),
                     'Google', 'Chrome', 'Application', 'chrome.exe'),
        os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                     'Google', 'Chrome', 'Application', 'chrome.exe'),
        os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'),
                     'Chromium', 'Application', 'chrome.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''),
                     'Google', 'Chrome', 'Application', 'chrome.exe'),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def detect_chrome_profile():
    """Find the user's default Chrome profile directory."""
    default = os.path.join(os.environ.get('LOCALAPPDATA', ''),
                           'Google', 'Chrome', 'User Data')
    if os.path.isdir(default):
        return default
    return None


def _run_playwright_install(log_func=print):
    """Run 'playwright install chromium' with live output via log_func."""
    from playwright._impl._driver import compute_driver_executable, get_driver_env
    driver_exe, driver_cli = compute_driver_executable()
    env = get_driver_env()
    proc = subprocess.Popen(
        [driver_exe, driver_cli, 'install', 'chromium'],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        creationflags=_CREATE_NO_WINDOW,
    )
    for line in proc.stdout:
        log_func(line.rstrip())
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f'playwright install exited with code {proc.returncode}')


def ensure_playwright_browsers(log_func=print):
    """Install Playwright's Chromium browser if missing."""
    try:
        import playwright
    except ImportError:
        if getattr(sys, 'frozen', False):
            log_func('Playwright not bundled â€” browsers must be pre-installed')
            return
        log_func('Installing Playwright package...')
        subprocess.check_call(
            [sys.executable, '-m', 'pip', 'install', 'playwright'],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
        log_func('Playwright installed.')

    ms_dir = os.path.join(os.environ.get('USERPROFILE', ''),
                          'AppData', 'Local', 'ms-playwright')
    chromium_ok = False
    if os.path.isdir(ms_dir):
        for entry in os.listdir(ms_dir):
            if entry.startswith('chromium'):
                chromium_ok = True
                break

    if not chromium_ok:
        log_func('Downloading Chromium browser for Playwright (â‰ˆ150MB)...')
        log_func('This may take a few minutes...')
        try:
            _run_playwright_install(log_func)
        except Exception as e:
            raise RuntimeError(f'Playwright install failed: {e}')
        log_func('Chromium ready.')


def ensure_ollama_setup(model_name='phi3:mini', log_func=print):
    """Install Ollama + pull model if not already present. Non-blocking on failure."""
    try:
        from ollama_manager import ensure_ollama
        return ensure_ollama(model_name=model_name, log_func=log_func)
    except Exception as e:
        log_func(f"Ollama setup skipped: {e}")
        return False


def ensure_state_files(log_func=print):
    """Create seed_config.json and plot_state.json if missing (next to exe)."""
    d = _data_dir()
    for name, default in [('seed_config.json', {}), ('plot_state.json', {})]:
        path = os.path.join(d, name)
        if os.path.exists(path):
            continue
        with open(path, 'w') as f:
            json.dump(default, f, indent=2)
        log_func(f'Created: {name}')


def setup_first_run(log_func=print):
    """Auto-configure everything for a first-time launch."""
    log_func('')
    log_func('====================================================')
    log_func('  UNCHAINED â€” First-Time Setup')
    log_func('====================================================')
    log_func('')

    chrome_exe = detect_chrome_exe()
    if chrome_exe:
        log_func(f'  Chrome:     {chrome_exe}')
    else:
        log_func('  Chrome:     not detected (Playwright will use its own Chromium)')

    chrome_profile = detect_chrome_profile()
    if chrome_profile:
        log_func(f'  Profile:    {chrome_profile}')
        log_func('              (playwright will create a dedicated profile for the bot)')

    log_func('')
    log_func('  Checking Playwright...')
    try:
        ensure_playwright_browsers(log_func)
        log_func('  Playwright: ready')
    except Exception as e:
        log_func(f'  WARNING: Playwright setup failed: {e}')
        log_func('  You can manually run: pip install playwright && playwright install chromium')

    os.makedirs(PROFILE_DIR, exist_ok=True)
    log_func(f'  Profile:    {PROFILE_DIR}')

    ensure_state_files(log_func)

    log_func('')
    log_func('  Checking Ollama (local AI)...')
    model_name = 'phi3:mini'
    try:
        ensure_ollama_setup(model_name, log_func)
    except Exception as e:
        log_func(f'  WARNING: Ollama setup failed: {e}')
        log_func('  Bot will run without AI features.')

    config = dict(DEFAULT_CONFIG)
    config['setup_complete'] = True
    config['setup_version'] = 1
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)
    log_func(f'  Config:     {CONFIG_FILE}')

    log_func('')
    log_func('  Setup complete! Launching UNCHAINED...')
    log_func('====================================================')


def is_first_run():
    """Return True if first-time setup hasn't been completed."""
    if not os.path.exists(CONFIG_FILE):
        return True
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
    except Exception:
        return True
    if config.get('setup_complete'):
        return False
    config['setup_complete'] = True
    config['setup_version'] = 1
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)
    return False
