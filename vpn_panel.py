"""PySide6 VPN panel widget for the Unchained bot."""
import os
import logging
import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot, QTimer
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QFrame, QGroupBox,
    QCheckBox, QSpinBox, QFormLayout, QMessageBox, QFileDialog,
    QAbstractItemView,
)

from vpn_manager import VPNManager, parse_conf_file

logger = logging.getLogger("unchained.vpn_panel")


class VPNPanel(QWidget):
    """Embeddable VPN control panel."""

    log_msg = Signal(str)
    status_msg = Signal(str)
    vpn_state_changed = Signal(bool)
    _poll_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._manager = VPNManager()
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_state)
        self._poll_requested.connect(self._poll_state, Qt.QueuedConnection)
        self._connected = False
        self._build_ui()
        self._refresh_servers()
        self._poll_timer.start(5000)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        status_row = QHBoxLayout()
        self._status_icon = QLabel("●")
        self._status_icon.setStyleSheet("color: #888; font-size: 18px;")
        self._status_label = QLabel("Disconnected")
        self._server_label = QLabel("")
        self._server_label.setStyleSheet("color: #aaa;")
        status_row.addWidget(self._status_icon)
        status_row.addWidget(self._status_label)
        status_row.addWidget(self._server_label)
        status_row.addStretch()
        layout.addLayout(status_row)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Country", "Server", "Tier", ""])
        self._tree.setColumnWidth(0, 70)
        self._tree.setColumnWidth(1, 200)
        self._tree.setColumnWidth(2, 50)
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        layout.addWidget(self._tree, 1)

        btn_row = QHBoxLayout()
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.clicked.connect(self._do_connect)
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.clicked.connect(self._do_disconnect)
        self._disconnect_btn.setEnabled(False)
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self._refresh_servers)
        self._open_dir_btn = QPushButton("Config Folder")
        self._open_dir_btn.clicked.connect(self._open_config_dir)
        btn_row.addWidget(self._connect_btn)
        btn_row.addWidget(self._disconnect_btn)
        btn_row.addWidget(self._refresh_btn)
        btn_row.addWidget(self._open_dir_btn)
        layout.addLayout(btn_row)

        rot_group = QGroupBox("Auto-Rotate")
        rot_layout = QFormLayout(rot_group)
        self._auto_rotate_cb = QCheckBox("Rotate IP between bot cycles")
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(1, 999)
        self._interval_spin.setValue(3)
        self._interval_spin.setSuffix(" cycles")
        rot_layout.addRow(self._auto_rotate_cb)
        rot_layout.addRow("Every:", self._interval_spin)
        layout.addWidget(rot_group)

        help_label = QLabel(
            "Place WireGuard .conf files in the Config Folder. Download them from:\n"
            "  https://account.protonvpn.com/downloads  →  WireGuard configuration"
        )
        help_label.setStyleSheet("color: #888; font-size: 11px; padding: 4px;")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)

    def get_manager(self):
        return self._manager

    def auto_rotate_enabled(self):
        return self._auto_rotate_cb.isChecked()

    def auto_rotate_interval(self):
        return self._interval_spin.value()

    def rotate_now(self):
        servers = self._manager.get_servers()
        current = self._manager.current_server()
        candidates = [s for s in servers if s.get("name") != current]
        if not candidates:
            candidates = servers
        if not candidates:
            return False, "No configs available"
        import random
        target = random.choice(candidates)
        return self._manager.connect(target)

    def _refresh_servers(self):
        servers = self._manager.get_servers()
        self._tree.clear()
        for s in servers:
            item = QTreeWidgetItem()
            item.setText(0, s.get("country", "??"))
            item.setText(1, s.get("name", "?"))
            item.setText(2, "Free" if s.get("tier") == 0 else "Paid")
            item.setText(3, s.get("_conf_path", ""))
            item.setData(0, Qt.UserRole, s)
            if s.get("tier") == 0:
                item.setForeground(0, QBrush(QColor("#4caf50")))
            self._tree.addTopLevelItem(item)
        count = len(servers)
        self._connect_btn.setEnabled(count > 0)
        if count == 0:
            self._status_label.setText("No configs — add .conf files")
            self._status_icon.setStyleSheet("color: #ff9800; font-size: 18px;")

    def _do_connect(self):
        item = self._tree.currentItem()
        if not item:
            self.log_msg.emit("Select a server first")
            return
        server = item.data(0, Qt.UserRole)
        if not server:
            return
        self._connect_btn.setEnabled(False)
        self._status_label.setText("Connecting...")
        threading.Thread(target=self._connect_thread, args=(server,), daemon=True).start()

    def _connect_thread(self, server):
        ok, msg = self._manager.connect(server)
        self._poll_requested.emit()

    def _do_disconnect(self):
        self._disconnect_btn.setEnabled(False)
        threading.Thread(target=self._disconnect_thread, daemon=True).start()

    def _disconnect_thread(self):
        ok, msg = self._manager.disconnect()
        self._poll_requested.emit()

    def _poll_state(self):
        running, name = self._manager.get_state()
        self._connected = running
        if running:
            self._status_icon.setStyleSheet("color: #4caf50; font-size: 18px;")
            self._status_label.setText("Connected")
            self._server_label.setText(name or "")
            self._connect_btn.setEnabled(False)
            self._disconnect_btn.setEnabled(True)
        else:
            self._status_icon.setStyleSheet("color: #f44336; font-size: 18px;")
            self._status_label.setText("Disconnected")
            self._server_label.setText("")
            self._connect_btn.setEnabled(len(self._manager.get_servers()) > 0)
            self._disconnect_btn.setEnabled(False)
        self.vpn_state_changed.emit(running)

    def _open_config_dir(self):
        d = self._manager.config_dir()
        try:
            os.startfile(d)
        except Exception:
            self.log_msg.emit(f"Config folder: {d}")
