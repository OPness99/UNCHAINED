"""Ollama Manager — auto-download, install, start, and manage Ollama + models."""

import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

logger = logging.getLogger("unchained.ollama")

OLLAMA_INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"
OLLAMA_API = "http://127.0.0.1:11434"
DEFAULT_MODEL = "phi3:mini"
_CREATE_NO_WINDOW = 0x08000000


def _data_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.getcwd()


def _installer_path():
    return os.path.join(_data_dir(), "OllamaSetup.exe")


def _ollama_exe_path():
    local = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Programs", "Ollama", "ollama.exe",
    )
    if os.path.isfile(local):
        return local
    for p in [
        r"C:\Program Files\Ollama\ollama.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Ollama", "ollama.exe"),
    ]:
        if os.path.isfile(p):
            return p
    return "ollama"


def is_ollama_installed():
    exe = _ollama_exe_path()
    if exe == "ollama":
        try:
            r = subprocess.run(
                ["ollama", "--version"],
                capture_output=True, text=True, timeout=10,
                creationflags=_CREATE_NO_WINDOW,
            )
            return r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    return os.path.isfile(exe)


def is_ollama_running():
    try:
        req = urllib.request.Request(f"{OLLAMA_API}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_ollama_server(log_func=logger.info):
    if is_ollama_running():
        log_func("Ollama server already running.")
        return True

    exe = _ollama_exe_path()
    log_func("Starting Ollama server...")
    try:
        subprocess.Popen(
            [exe, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception as e:
        log_func(f"Failed to start Ollama: {e}")
        return False

    for i in range(30):
        time.sleep(1)
        if is_ollama_running():
            log_func("Ollama server ready.")
            return True
    log_func("Ollama server failed to start within 30s.")
    return False


def download_installer(log_func=logger.info):
    path = _installer_path()
    if os.path.isfile(path):
        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb > 50:
            log_func(f"Installer already downloaded ({size_mb:.0f} MB).")
            return path

    log_func("Downloading Ollama installer (~150 MB)...")
    log_func("This may take a few minutes on first run...")

    try:
        urllib.request.urlretrieve(OLLAMA_INSTALLER_URL, path)
    except urllib.error.URLError as e:
        log_func(f"Download failed: {e}")
        if os.path.exists(path):
            os.remove(path)
        return None

    size_mb = os.path.getsize(path) / (1024 * 1024)
    log_func(f"Downloaded: {size_mb:.0f} MB")
    return path


def install_ollama(log_func=logger.info):
    installer = download_installer(log_func)
    if not installer:
        return False

    log_func("Installing Ollama (silent, ~2 min)...")
    try:
        proc = subprocess.run(
            [installer, "/SILENT", "/NORESTART"],
            timeout=300,
            creationflags=_CREATE_NO_WINDOW,
        )
        if proc.returncode != 0:
            log_func(f"Installer exited with code {proc.returncode}")
            return False
    except subprocess.TimeoutExpired:
        log_func("Installer timed out after 5 minutes.")
        return False
    except Exception as e:
        log_func(f"Install failed: {e}")
        return False

    log_func("Ollama installed successfully.")
    try:
        os.remove(installer)
        log_func("Cleaned up installer.")
    except OSError:
        pass
    return True


def get_installed_models():
    try:
        req = urllib.request.Request(f"{OLLAMA_API}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def is_model_available(model_name):
    models = get_installed_models()
    for m in models:
        if m == model_name or m.startswith(model_name + ":"):
            return True
    return False


def pull_model(model_name=DEFAULT_MODEL, force=False, log_func=logger.info):
    if not force and is_model_available(model_name):
        log_func(f"Model '{model_name}' already available.")
        return True

    log_func(f"Pulling model '{model_name}' (~2 GB)...")
    log_func("This is a one-time download...")
    try:
        proc = subprocess.Popen(
            [_ollama_exe_path(), "pull", model_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                clean = line.encode("ascii", errors="replace").decode("ascii")
                log_func(f"  {clean}")
        proc.wait()
        if proc.returncode == 0:
            log_func(f"Model '{model_name}' ready.")
            return True
        log_func(f"Pull failed with code {proc.returncode}")
        return False
    except Exception as e:
        log_func(f"Pull failed: {e}")
        return False


def ensure_ollama(model_name=DEFAULT_MODEL, log_func=logger.info):
    """Full bootstrap: install if needed, start server, always pull model."""
    log_func("")
    log_func("=" * 50)
    log_func("  Ollama Setup")
    log_func("=" * 50)
    log_func("")

    if not is_ollama_installed():
        log_func("Ollama not found — installing...")
        if not install_ollama(log_func):
            log_func("WARNING: Ollama install failed.")
            log_func("LLM features will be unavailable.")
            return False
    else:
        log_func("Ollama: installed")

    if not start_ollama_server(log_func):
        log_func("WARNING: Could not start Ollama server.")
        log_func("LLM features will be unavailable.")
        return False

    log_func("Ollama: running")

    if not pull_model(model_name, force=True, log_func=log_func):
        log_func(f"WARNING: Could not pull model '{model_name}'.")
        log_func("LLM features will be unavailable.")
        return False

    log_func("")
    log_func("Ollama setup complete!")
    log_func("=" * 50)
    log_func("")
    return True


def send_prompt(model_name, prompt, system=None, temperature=0.7,
                max_tokens=512, log_func=logger.info):
    """Send a prompt to Ollama and return the response text.

    Handles both regular models (response field) and thinking/reasoning
    models like qwen3/deepseek (thinking field).
    """
    if not is_ollama_running():
        log_func("Ollama server not running.")
        return None

    payload = {
        "model": model_name,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    if system:
        payload["system"] = system

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_API}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
            text = result.get("response", "")
            if not text:
                text = result.get("thinking", "")
            return text
    except Exception as e:
        log_func(f"Ollama request failed: {e}")
        return None
