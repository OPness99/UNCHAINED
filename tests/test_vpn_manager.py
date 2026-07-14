"""Tests for vpn_manager.py"""

import base64
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vpn_manager import (
    SERVICE_NAME,
    VPNManager,
    _run_sc,
    _run_sc_elevated,
    gen_wg_keypair,
    make_wg_config,
    parse_conf_file,
)


# ---------------------------------------------------------------------------
# gen_wg_keypair
# ---------------------------------------------------------------------------

try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey as _X25519Check
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False


@pytest.mark.skipif(not HAS_CRYPTOGRAPHY, reason="cryptography module not installed")
class TestGenWgKeypair:
    def test_returns_two_strings(self):
        priv, pub = gen_wg_keypair()
        assert isinstance(priv, str)
        assert isinstance(pub, str)

    def test_base64_length(self):
        priv, pub = gen_wg_keypair()
        priv_bytes = base64.b64decode(priv)
        pub_bytes = base64.b64decode(pub)
        assert len(priv_bytes) == 32
        assert len(pub_bytes) == 32

    def test_valid_base64(self):
        priv, pub = gen_wg_keypair()
        assert base64.b64decode(priv)
        assert base64.b64decode(pub)

    def test_unique_each_call(self):
        p1, u1 = gen_wg_keypair()
        p2, u2 = gen_wg_keypair()
        assert p1 != p2 or u1 != u2


# ---------------------------------------------------------------------------
# make_wg_config
# ---------------------------------------------------------------------------

class TestMakeWgConfig:
    def test_basic_config(self):
        cfg = make_wg_config("PRIVKEY", "SERVERPK", "1.2.3.4:51820")
        assert "[Interface]" in cfg
        assert "[Peer]" in cfg
        assert "PrivateKey = PRIVKEY" in cfg
        assert "PublicKey = SERVERPK" in cfg
        assert "Endpoint = 1.2.3.4:51820" in cfg
        assert "AllowedIPs = 0.0.0.0/0" in cfg

    def test_defaults(self):
        cfg = make_wg_config("K", "P", "10.0.0.1:51820")
        assert "Address = 10.2.0.2/32" in cfg
        assert "DNS = 10.2.0.1" in cfg

    def test_custom_address_dns(self):
        cfg = make_wg_config("K", "P", "1.1.1.1:5", address="10.0.0.5/24", dns="8.8.8.8")
        assert "Address = 10.0.0.5/24" in cfg
        assert "DNS = 8.8.8.8" in cfg


# ---------------------------------------------------------------------------
# parse_conf_file
# ---------------------------------------------------------------------------

class TestParseConfFile:
    def _write_conf(self, tmp_path, name, content):
        p = tmp_path / name
        p.write_text(textwrap.dedent(content), encoding="utf-8")
        return p

    def test_valid_conf(self, tmp_path):
        p = self._write_conf(
            tmp_path,
            "CH.conf",
            """\
            # CH VPN
            [Interface]
            PrivateKey = abc123
            Address = 10.2.0.2/32

            [Peer]
            PublicKey = pub999
            AllowedIPs = 0.0.0.0/0
            Endpoint = 185.159.156.1:51820
            """,
        )
        result = parse_conf_file(p)
        assert result is not None
        assert result["name"] == "CH VPN"
        assert result["_privkey"] == "abc123"
        assert result["x25519_pk"] == "pub999"
        assert result["entry_ip"] == "185.159.156.1"
        assert result["port"] == 51820
        assert result["country"] == "CH"
        assert result["tier"] == 1
        assert "_conf_path" in result

    def test_no_comment_uses_stem(self, tmp_path):
        p = self._write_conf(
            tmp_path,
            "US.conf",
            """\
            [Interface]
            PrivateKey = key
            [Peer]
            PublicKey = pk
            Endpoint = 1.2.3.4:443
            """,
        )
        result = parse_conf_file(p)
        assert result is not None
        assert result["name"] == "US"

    def test_missing_file_returns_none(self):
        assert parse_conf_file("C:\\nonexistent\\file.conf") is None

    def test_no_privatekey_returns_none(self, tmp_path):
        p = self._write_conf(
            tmp_path,
            "bad.conf",
            """\
            [Peer]
            PublicKey = pk
            Endpoint = 1.2.3.4:51820
            """,
        )
        assert parse_conf_file(p) is None

    def test_no_pubkey_returns_none(self, tmp_path):
        p = self._write_conf(
            tmp_path,
            "bad.conf",
            """\
            [Interface]
            PrivateKey = key
            [Peer]
            AllowedIPs = 0.0.0.0/0
            Endpoint = 1.2.3.4:51820
            """,
        )
        assert parse_conf_file(p) is None

    def test_no_endpoint_returns_none(self, tmp_path):
        p = self._write_conf(
            tmp_path,
            "bad.conf",
            """\
            [Interface]
            PrivateKey = key
            [Peer]
            PublicKey = pk
            AllowedIPs = 0.0.0.0/0
            """,
        )
        assert parse_conf_file(p) is None

    def test_empty_file_returns_none(self, tmp_path):
        p = tmp_path / "empty.conf"
        p.write_text("", encoding="utf-8")
        assert parse_conf_file(p) is None

    def test_country_from_stem(self, tmp_path):
        p = self._write_conf(
            tmp_path,
            "DE.conf",
            """\
            [Interface]
            PrivateKey = k
            [Peer]
            PublicKey = p
            Endpoint = 2.3.4.5:51820
            """,
        )
        result = parse_conf_file(p)
        assert result["country"] == "DE"


# ---------------------------------------------------------------------------
# _run_sc
# ---------------------------------------------------------------------------

class TestRunSc:
    @patch("vpn_manager.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        ok, out = _run_sc("start", "svc")
        assert ok is True
        assert out == "ok"
        mock_run.assert_called_once()

    @patch("vpn_manager.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error msg")
        ok, out = _run_sc("stop", "svc")
        assert ok is False
        assert "error msg" in out

    @patch("vpn_manager._run_sc_elevated")
    @patch("vpn_manager.subprocess.run")
    def test_access_denied_triggers_elevated(self, mock_run, mock_elevated):
        mock_run.return_value = MagicMock(returncode=5, stdout="", stderr="access denied")
        mock_elevated.return_value = True, "elevated ok"
        ok, out = _run_sc("start", "svc")
        assert ok is True
        mock_elevated.assert_called_once_with("start", "svc")

    @patch("vpn_manager.subprocess.run", side_effect=OSError("boom"))
    def test_exception(self, mock_run):
        ok, out = _run_sc("start", "svc")
        assert ok is False
        assert "boom" in out


# ---------------------------------------------------------------------------
# _run_sc_elevated
# ---------------------------------------------------------------------------

class TestRunScElevated:
    @patch("vpn_manager.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ok, out = _run_sc_elevated("start", "svc")
        assert ok is True
        assert "Elevated sc completed" in out

    @patch("vpn_manager.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="err")
        ok, out = _run_sc_elevated("stop", "svc")
        assert ok is False

    @patch("vpn_manager.subprocess.run", side_effect=OSError("denied"))
    def test_exception(self, mock_run):
        ok, out = _run_sc_elevated("start", "svc")
        assert ok is False
        assert "denied" in out

    @patch("vpn_manager.subprocess.run")
    def test_powershell_command_format(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _run_sc_elevated("start", "MySvc")
        args = mock_run.call_args[0][0]
        assert args[0] == "powershell"
        assert "Start-Process sc.exe" in args[3]
        assert 'start "MySvc"' in args[3]


# ---------------------------------------------------------------------------
# VPNManager.__init__
# ---------------------------------------------------------------------------

class TestVPNManagerInit:
    def test_creates_config_dir(self, tmp_path):
        cfg_dir = tmp_path / "configs"
        mgr = VPNManager(config_dir=cfg_dir)
        assert cfg_dir.exists()
        assert mgr.config_dir() == str(cfg_dir)

    def test_default_config_dir(self, tmp_path):
        mgr = VPNManager(config_dir=tmp_path / "sub")
        assert Path(mgr.config_dir()).exists()

    def test_initial_state(self, tmp_path):
        mgr = VPNManager(config_dir=tmp_path)
        assert mgr.is_connected() is False
        assert mgr.current_server() is None


# ---------------------------------------------------------------------------
# VPNManager.get_servers
# ---------------------------------------------------------------------------

class TestGetServers:
    def _write_conf(self, cfg_dir, name, privkey="key", pk="pk", ep="1.2.3.4:51820"):
        p = cfg_dir / name
        p.write_text(
            textwrap.dedent(
                f"""\
                [Interface]
                PrivateKey = {privkey}
                [Peer]
                PublicKey = {pk}
                Endpoint = {ep}
                """
            ),
            encoding="utf-8",
        )

    def test_reads_conf_files(self, tmp_path):
        self._write_conf(tmp_path, "FR.conf", pk="pub1", ep="10.0.0.1:51820")
        self._write_conf(tmp_path, "JP.conf", pk="pub2", ep="10.0.0.2:51820")
        mgr = VPNManager(config_dir=tmp_path)
        servers = mgr.get_servers()
        assert len(servers) == 2
        names = {s["name"] for s in servers}
        assert names == {"FR", "JP"}

    def test_skips_invalid_files(self, tmp_path):
        self._write_conf(tmp_path, "good.conf")
        (tmp_path / "bad.conf").write_text("nonsense", encoding="utf-8")
        mgr = VPNManager(config_dir=tmp_path)
        servers = mgr.get_servers()
        assert len(servers) == 1

    def test_empty_dir(self, tmp_path):
        mgr = VPNManager(config_dir=tmp_path)
        assert mgr.get_servers() == []


# ---------------------------------------------------------------------------
# VPNManager.get_state
# ---------------------------------------------------------------------------

class TestGetState:
    @patch("vpn_manager.subprocess.run")
    def test_running(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="STATE: 4 RUNNING", stderr=""
        )
        with patch.object(VPNManager, "_read_current_conf", return_value=None):
            mgr = VPNManager(config_dir=Path("."))
            running, name = mgr.get_state()
        assert running is True
        assert name is None

    @patch("vpn_manager.subprocess.run")
    def test_not_running(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="STATE: 1 STOPPED", stderr=""
        )
        mgr = VPNManager(config_dir=Path("."))
        running, name = mgr.get_state()
        assert running is False
        assert name is None

    @patch("vpn_manager.subprocess.run", side_effect=OSError("fail"))
    def test_exception(self, mock_run):
        mgr = VPNManager(config_dir=Path("."))
        running, name = mgr.get_state()
        assert running is False
        assert name is None


# ---------------------------------------------------------------------------
# VPNManager.connect
# ---------------------------------------------------------------------------

class TestConnect:
    @patch("vpn_manager.time.sleep")
    @patch("vpn_manager._run_sc")
    def test_connect_from_conf_path(self, mock_sc, mock_sleep, tmp_path):
        conf = tmp_path / "DE.conf"
        conf.write_text(
            textwrap.dedent(
                """\
                [Interface]
                PrivateKey = mykey
                [Peer]
                PublicKey = mypk
                Endpoint = 5.6.7.8:51820
                """
            ),
            encoding="utf-8",
        )
        mock_sc.return_value = (True, "ok")
        mgr = VPNManager(config_dir=tmp_path)
        ok, msg = mgr.connect({"_conf_path": str(conf), "name": "DE"})
        assert ok is True
        assert "Connected to DE" in msg
        assert mgr.is_connected() is True
        assert mgr.current_server() == "DE"

    @patch("vpn_manager.time.sleep")
    @patch("vpn_manager._run_sc")
    @patch("vpn_manager.make_wg_config", return_value="generated config")
    @patch("vpn_manager.gen_wg_keypair", return_value=("fake_privkey", "fake_pubkey"))
    def test_connect_without_conf_path(
        self, mock_wgkey, mock_wg, mock_sc, mock_sleep, tmp_path
    ):
        mock_sc.return_value = (True, "ok")
        mgr = VPNManager(config_dir=tmp_path)
        with patch.object(mgr, "_write_config_elevated", return_value=(True, "")):
            info = {"name": "JP", "entry_ip": "1.2.3.4", "port": 51820, "x25519_pk": "pubk"}
            ok, msg = mgr.connect(info)
        assert ok is True
        mock_wgkey.assert_called_once()
        assert mgr.current_server() == "JP"

    def test_connect_missing_config_file(self, tmp_path):
        mgr = VPNManager(config_dir=tmp_path)
        ok, msg = mgr.connect({"_conf_path": str(tmp_path / "missing.conf")})
        assert ok is False
        assert "not found" in msg

    @patch("vpn_manager.time.sleep")
    @patch("vpn_manager._run_sc")
    @patch("vpn_manager.gen_wg_keypair", return_value=("fake_privkey", "fake_pubkey"))
    def test_connect_write_fails(self, mock_wgkey, mock_sc, mock_sleep, tmp_path):
        mgr = VPNManager(config_dir=tmp_path)
        with patch.object(mgr, "_write_config_elevated", return_value=(False, "perm denied")):
            ok, msg = mgr.connect({"name": "X", "entry_ip": "1.1.1.1", "x25519_pk": "pk"})
        assert ok is False
        assert "perm denied" in msg

    @patch("vpn_manager.time.sleep")
    @patch("vpn_manager._run_sc")
    @patch("vpn_manager.gen_wg_keypair", return_value=("fake_privkey", "fake_pubkey"))
    def test_connect_service_start_fails(self, mock_wgkey, mock_sc, mock_sleep, tmp_path):
        def sc_side_effect(action, name):
            if action == "stop":
                return True, "stopped"
            return False, "start failed"

        mock_sc.side_effect = sc_side_effect
        mgr = VPNManager(config_dir=tmp_path)
        with patch.object(mgr, "_write_config_elevated", return_value=(True, "")):
            ok, msg = mgr.connect({"name": "X", "entry_ip": "1.1.1.1", "x25519_pk": "pk"})
        assert ok is False
        assert "start failed" in msg


# ---------------------------------------------------------------------------
# VPNManager.disconnect
# ---------------------------------------------------------------------------

class TestDisconnect:
    @patch("vpn_manager._run_sc", return_value=(True, "stopped"))
    def test_disconnect_success(self, mock_sc, tmp_path):
        mgr = VPNManager(config_dir=tmp_path)
        mgr._connected = True
        mgr._current_name = "FR"
        ok, msg = mgr.disconnect()
        assert ok is True
        assert mgr.is_connected() is False
        assert mgr.current_server() is None
        mock_sc.assert_called_once_with("stop", SERVICE_NAME)

    @patch("vpn_manager._run_sc", return_value=(False, "access denied"))
    def test_disconnect_failure(self, mock_sc, tmp_path):
        mgr = VPNManager(config_dir=tmp_path)
        mgr._connected = True
        ok, msg = mgr.disconnect()
        assert ok is False
        assert "access denied" in msg
