"""VPN backend â€” manages Proton VPN WireGuard service + config switching."""
import os
import re
import sys
import time
import base64
import logging
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger("unchained.vpn")

_CREATE_NO_WINDOW = 0x08000000

PROTON_VPN_CONF = Path(r"C:\Program Files\Proton\VPN\v4.4.1\ServiceData\WireGuard\ProtonVPN.conf")
SERVICE_NAME = "ProtonVPN WireGuard"


def _run_sc(action, service_name):
    try:
        r = subprocess.run(
            ["sc.exe", action, service_name],
            capture_output=True, text=True, timeout=30,
            creationflags=_CREATE_NO_WINDOW,
        )
        out = (r.stdout + r.stderr).strip()
        logger.info("sc %s %s: %s â€” %s", action, service_name, r.returncode, out[:120])
        if r.returncode == 0:
            return True, out
        if r.returncode == 5:
            return _run_sc_elevated(action, service_name)
        return False, out
    except Exception as e:
        logger.error("sc %s failed: %s", action, e)
        return False, str(e)


def _run_sc_elevated(action, service_name):
    ps_cmd = (
        "Start-Process sc.exe -Verb RunAs -WindowStyle Hidden -Wait "
        f"-ArgumentList '{action} \"{service_name}\"'"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=60,
            creationflags=_CREATE_NO_WINDOW,
        )
        out = (r.stdout + r.stderr).strip()
        logger.info("elevated sc %s %s: %s â€” %s", action, service_name, r.returncode, out[:120])
        return r.returncode == 0, out or "Elevated sc completed"
    except Exception as e:
        logger.error("elevated sc %s failed: %s", action, e)
        return False, str(e)


def gen_wg_keypair():
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    priv = X25519PrivateKey.generate()
    return (
        base64.b64encode(priv.private_bytes_raw()).decode(),
        base64.b64encode(priv.public_key().public_bytes_raw()).decode(),
    )


def make_wg_config(privkey_b64, server_pk, endpoint, address="10.2.0.2/32", dns="10.2.0.1"):
    return (
        f"[Interface]\nPrivateKey = {privkey_b64}\n"
        f"Address = {address}\nDNS = {dns}\n\n"
        f"[Peer]\nPublicKey = {server_pk}\n"
        f"AllowedIPs = 0.0.0.0/0\nEndpoint = {endpoint}\n"
    )


def parse_conf_file(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return None
    peer_name = ""
    pubkey = ""
    endpoint = ""
    m = re.search(r"# (.+)", text)
    if m:
        peer_name = m.group(1).strip()
    m = re.search(r"PrivateKey = (\S+)", text)
    if not m:
        return None
    privkey = m.group(1)
    m = re.search(r"PublicKey = (\S+)", text)
    if m:
        pubkey = m.group(1)
    m = re.search(r"Endpoint = ([\d.]+:\d+)", text)
    if m:
        endpoint = m.group(1)
    if not pubkey or not endpoint:
        return None
    entry_ip = endpoint.rsplit(":", 1)[0]
    port = int(endpoint.rsplit(":", 1)[1])
    return {
        "name": peer_name or Path(path).stem,
        "country": Path(path).stem[:2].upper() if Path(path).stem else "??",
        "city": "",
        "tier": 1,
        "entry_ip": entry_ip,
        "exit_ip": entry_ip,
        "x25519_pk": pubkey,
        "port": port,
        "_conf_path": str(path),
        "_privkey": privkey,
    }


class VPNManager:
    """Controls the Proton VPN WireGuard service and manages configs."""

    def __init__(self, config_dir=None):
        self._lock = threading.Lock()
        self._connected = False
        self._current_name = None
        if config_dir:
            self._config_dir = Path(config_dir)
        else:
            base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.getcwd()
            self._config_dir = Path(base) / "vpn_configs"
        self._config_dir.mkdir(exist_ok=True)

    def config_dir(self):
        return str(self._config_dir)

    def get_servers(self):
        servers = []
        for f in sorted(self._config_dir.glob("*.conf")):
            info = parse_conf_file(f)
            if not info:
                continue
            servers.append(info)
        return servers

    def get_state(self):
        try:
            r = subprocess.run(
                ["sc.exe", "query", SERVICE_NAME],
                capture_output=True, text=True, timeout=15,
                creationflags=_CREATE_NO_WINDOW,
            )
            running = "RUNNING" in r.stdout
            info = self._read_current_conf()
            if running and info:
                return running, info.get("name")
            return running, None
        except Exception:
            return False, None

    def _read_current_conf(self):
        if not PROTON_VPN_CONF.exists():
            return None
        return parse_conf_file(PROTON_VPN_CONF)

    def _write_config_elevated(self, text):
        try:
            PROTON_VPN_CONF.parent.mkdir(parents=True, exist_ok=True)
            PROTON_VPN_CONF.write_text(text, encoding="utf-8")
            return True, ""
        except PermissionError:
            logger.info("Direct write failed â€” trying elevated PowerShell...")
        except Exception as e:
            return False, str(e)
        escaped = text.replace("'", "''")
        ps_cmd = (
            "Start-Process powershell -Verb RunAs -WindowStyle Hidden -Wait -ArgumentList "
            "'-NoProfile -Command \""
            f"New-Item -ItemType Directory -Force -Path '{PROTON_VPN_CONF.parent}' | Out-Null; "
            f"Set-Content -LiteralPath '{PROTON_VPN_CONF}' -Value '{escaped}' -Force"
            "\"'"
        )
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=120,
                creationflags=_CREATE_NO_WINDOW,
            )
            if r.returncode != 0:
                return False, r.stderr.strip() or r.stdout.strip() or "Elevated write failed"
            if PROTON_VPN_CONF.exists() and PROTON_VPN_CONF.stat().st_size > 0:
                return True, ""
            return False, "File not written (UAC cancelled?)"
        except Exception as e:
            return False, str(e)

    def connect(self, server_info):
        with self._lock:
            if server_info.get("_conf_path"):
                src = Path(server_info["_conf_path"])
                if not src.exists():
                    return False, "Config file not found"
                ok, err = self._write_config_elevated(src.read_text(encoding="utf-8"))
                if not ok:
                    return False, f"Write config failed: {err}"
            else:
                priv, _ = gen_wg_keypair()
                ep = f"{server_info['entry_ip']}:{server_info.get('port', 51820)}"
                config = make_wg_config(priv, server_info["x25519_pk"], ep)
                ok, err = self._write_config_elevated(config)
                if not ok:
                    return False, f"Write config failed: {err}"
            logger.info("Config written for %s", server_info.get("name", "?"))
            ok, out = _run_sc("stop", SERVICE_NAME)
            if not ok:
                logger.warning("Stop failed: %s", out)
            time.sleep(1.5)
            ok, out = _run_sc("start", SERVICE_NAME)
            if ok:
                self._connected = True
                self._current_name = server_info.get("name", "?")
                return True, f"Connected to {self._current_name}"
            return False, f"Service start failed: {out}"

    def disconnect(self):
        with self._lock:
            ok, out = _run_sc("stop", SERVICE_NAME)
            if ok:
                self._connected = False
                self._current_name = None
                return True, "VPN disconnected"
            return False, f"Stop failed: {out}"

    def is_connected(self):
        return self._connected

    def current_server(self):
        return self._current_name
