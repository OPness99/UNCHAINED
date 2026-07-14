"""
Brain Viewer â€” interactive Obsidian vault graph for UNCHAINED.
Renders notes as clickable, force-directed nodes with color-coded types.
Supports ML-enhanced coloring when models are available.
"""

import math
import os
import random
from collections import defaultdict

import numpy as np

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, Signal
from PySide6.QtGui import QFont, QColor, QPen, QBrush, QPainter, QFontMetrics, QLinearGradient
from PySide6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsItem, QGraphicsSimpleTextItem,
    QDialog, QVBoxLayout, QTextEdit, QLabel, QHBoxLayout, QPushButton,
    QWidget, QSizePolicy, QCheckBox,
)

from memory import VAULT_PATH, read_note, find_notes


NODE_COLORS = {
    "session": QColor(60, 130, 230),
    "seed": QColor(70, 190, 70),
    "garden": QColor(230, 180, 30),
    "detection": QColor(220, 50, 50),
    "config_snapshot": QColor(160, 70, 210),
    "profile": QColor(50, 200, 220),
}

SIZE_BY_TYPE = {
    "session": 12,
    "seed": 10,
    "garden": 14,
    "detection": 12,
    "config_snapshot": 9,
    "profile": 11,
}


class GraphNode:
    __slots__ = ('nid', 'label', 'subtitle', 'ntype', 'path',
                 'x', 'y', 'vx', 'vy', 'radius', 'color', 'item', 'fm')

    def __init__(self, nid, label, ntype, path, subtitle='', fm=None):
        self.nid = nid
        self.label = label
        self.subtitle = subtitle
        self.ntype = ntype
        self.path = path
        self.fm = fm or {}
        self.x = random.uniform(-250, 250)
        self.y = random.uniform(-250, 250)
        self.vx = 0.0
        self.vy = 0.0
        self.radius = SIZE_BY_TYPE.get(ntype, 28)
        self.color = NODE_COLORS.get(ntype, QColor(100, 100, 130))
        self.item = None


class EdgeItem(QGraphicsItem):
    def __init__(self, a, b):
        super().__init__()
        self.a = a
        self.b = b

    def boundingRect(self):
        a_pos = self.a.item.pos() if self.a.item else QPointF()
        b_pos = self.b.item.pos() if self.b.item else QPointF()
        return QRectF(a_pos, b_pos).normalized().adjusted(-2, -2, 2, 2)

    def paint(self, painter, option, widget=None):
        a_pos = self.a.item.pos() if self.a.item else QPointF()
        b_pos = self.b.item.pos() if self.b.item else QPointF()
        painter.setPen(QPen(QColor(80, 80, 130, 40), 0.8))
        painter.drawLine(a_pos, b_pos)


class NodeItem(QGraphicsItem):
    def __init__(self, node):
        super().__init__()
        self.node = node
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self._hovered = False

        self._label = QGraphicsSimpleTextItem(self)
        self._label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self._update_label_text()
        f = self._label.font()
        f.setPointSize(8)
        self._label.setFont(f)
        self._label.setBrush(QColor(170, 180, 210))
        self._update_label_pos()

        tip = self.node.label
        if self.node.subtitle:
            tip += f"\n{self.node.subtitle}"
        self.setToolTip(tip)

    def _update_label_text(self):
        txt = self.node.label[:18] + '..' if len(self.node.label) > 20 else self.node.label[:20]
        self._label.setText(txt)

    def _update_label_pos(self):
        br = self._label.boundingRect()
        self._label.setPos(-br.width() / 2, self.node.radius + 5)

    def boundingRect(self):
        r = self.node.radius
        return QRectF(-r - 2, -r - 2, (r + 2) * 2, (r + 2) * 2)

    def paint(self, painter, option, widget=None):
        n = self.node
        r = n.radius
        painter.setRenderHint(QPainter.Antialiasing)

        if self._hovered or self.isSelected():
            glow = QColor(n.color.red(), n.color.green(), n.color.blue(), 50)
            painter.setPen(QPen(glow, r * 0.6))
            painter.setBrush(QBrush(QColor(n.color.red(), n.color.green(), n.color.blue(), 20)))
            painter.drawEllipse(QPointF(0, 0), r * 1.8, r * 1.8)

        painter.setBrush(QBrush(n.color))
        painter.setPen(QPen(QColor(255, 255, 255, 40), 0.8))
        painter.drawEllipse(QPointF(0, 0), r, r)

    def hoverEnterEvent(self, event):
        self._hovered = True
        self._label.setVisible(True)
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hovered = False
        self._label.setVisible(False)
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        self.setSelected(True)
        super().mousePressEvent(event)


class NoteDialog(QDialog):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(os.path.basename(path).replace('.md', ''))
        self.setMinimumSize(520, 400)
        self.resize(620, 500)
        self.setStyleSheet("""
            QDialog { background: #111827; }
            QTextEdit { background: #0a0c14; color: #b0b8d0;
                        border: 1px solid #2a2f4a; border-radius: 4px;
                        font-family: Consolas; font-size: 10pt; }
        """)

        layout = QVBoxLayout(self)

        try:
            fm, body = read_note(path)
            raw = open(path, encoding='utf-8').read()
        except Exception:
            raw = f"Error reading: {path}"

        edit = QTextEdit()
        edit.setReadOnly(True)
        edit.setPlainText(raw)
        layout.addWidget(edit)

        btn = QPushButton('Close')
        btn.clicked.connect(self.accept)
        btn.setStyleSheet("""
            QPushButton { background: #2a3a6a; border: 1px solid #3a4a7a;
                          border-radius: 4px; color: #ddd; padding: 6px 20px;
                          font-size: 10pt; }
            QPushButton:hover { background: #3a4a8a; }
        """)

        bl = QHBoxLayout()
        bl.addStretch()
        bl.addWidget(btn)
        layout.addLayout(bl)


class BrainViewer(QGraphicsView):
    nodeClicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setBackgroundBrush(QColor(14, 18, 34))
        self.setStyleSheet("QGraphicsView { border: 1px solid #1e2a4a; border-radius: 6px; }")
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.MinimalViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._nodes = []
        self._edges = []
        self._empty_label = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.setInterval(50)
        self._settled_count = 0
        self._ml_mode = False
        self._ml_engine = None

        self.rebuild()
        self._timer.start()

    def set_ml_engine(self, engine):
        self._ml_engine = engine

    def set_ml_mode(self, enabled):
        self._ml_mode = enabled
        self.rebuild()

    def rebuild(self):
        nodes = []

        for folder in ('sessions', 'seeds', 'gardens', 'detection', 'config', 'profiles'):
            dir_path = os.path.join(VAULT_PATH, folder)
            if not os.path.isdir(dir_path):
                continue

            try:
                entries = sorted(os.listdir(dir_path))
            except Exception:
                continue

            for fname in entries:
                if not fname.endswith('.md') or fname == '_template.md':
                    continue
                path = os.path.join(dir_path, fname)
                try:
                    fm, _ = read_note(path)
                    if not fm:
                        continue
                    ntype = fm.get('type', 'folder')
                    label = fname.replace('.md', '')
                    subtitle = self._subtitle(fm, ntype)
                    node = GraphNode(fname, label, ntype, path, subtitle, fm)
                    nodes.append(node)
                except Exception:
                    continue

        if not nodes and not self._nodes:
            self._scene.clear()
            self._nodes.clear()
            self._edges.clear()
            self._empty_label = self._scene.addText(
                "No vault data yet.\nRun the bot to start populating notes.",
                QFont('Consolas', 12),
            )
            self._empty_label.setDefaultTextColor(QColor(80, 90, 120))
            return

        if not nodes:
            return

        self._scene.clear()
        self._nodes.clear()
        self._edges.clear()
        self._empty_label = None

        node_map = {n.label: n for n in nodes}
        edges = []
        for n in nodes:
            if n.ntype != 'session':
                continue
            if not n.fm.get('garden'):
                continue
            tgt = node_map.get(n.fm['garden'])
            if not tgt:
                continue
            edges.append((n, tgt))

        for n in nodes:
            item = NodeItem(n)
            n.item = item
            self._scene.addItem(item)

        for a, b in edges:
            ei = EdgeItem(a, b)
            self._scene.addItem(ei)
            self._edges.append((a, b, ei))

        for n in nodes:
            n.x = random.uniform(-350, 350)
            n.y = random.uniform(-350, 350)

        self._nodes = nodes

        if self._ml_mode and self._ml_engine is not None and self._ml_engine.available:
            self._apply_ml_colors()

        self._settled_count = 0
        self._scene.setSceneRect(-800, -800, 1600, 1600)

    def _tick(self):
        if not self._nodes:
            return
        moved = 0

        for i, a in enumerate(self._nodes):
            for b in self._nodes[i + 1:]:
                dx = b.x - a.x
                dy = b.y - a.y
                dist = math.sqrt(dx * dx + dy * dy) + 1
                force = 3500.0 / (dist * dist)
                fx = force * dx / dist
                fy = force * dy / dist
                a.vx -= fx
                a.vy -= fy
                b.vx += fx
                b.vy += fy

        for a, b, _ in self._edges:
            dx = b.x - a.x
            dy = b.y - a.y
            dist = math.sqrt(dx * dx + dy * dy) + 1
            force = (dist - 120) / 40.0
            fx = force * dx / dist
            fy = force * dy / dist
            a.vx += fx
            a.vy += fy
            b.vx -= fx
            b.vy -= fy

        for n in self._nodes:
            n.vx += -n.x * 0.001
            n.vy += -n.y * 0.001
            n.vx *= 0.82
            n.vy *= 0.82
            n.x += n.vx
            n.y += n.vy
            if n.item:
                n.item.setPos(n.x, n.y)
            if abs(n.vx) > 0.2 or abs(n.vy) > 0.2:
                moved += 1

        if moved == 0:
            self._settled_count += 1
        else:
            self._settled_count = 0

    def mouseDoubleClickEvent(self, event):
        item = self.itemAt(event.pos())
        if isinstance(item, NodeItem):
            NoteDialog(item.node.path, self).exec()
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        factor = 1.15
        if event.angleDelta().y() > 0:
            self.scale(factor, factor)
        else:
            self.scale(1 / factor, 1 / factor)

    def _apply_ml_colors(self):
        if not self._ml_engine:
            return

        sessions = [n for n in self._nodes if n.ntype == 'session']

        if sessions:
            harvest_counts = [n.fm.get('harvested', 0) for n in sessions]
            error_counts = [n.fm.get('errors', 0) for n in sessions]
            max_h = max(harvest_counts) if harvest_counts else 1
            max_e = max(error_counts) if error_counts else 1

            for n in sessions:
                h = n.fm.get('harvested', 0)
                e = n.fm.get('errors', 0)

                ratio = (h / max_h - e / max_e) if (max_h and max_e) else 0
                ratio = max(-1.0, min(1.0, ratio))

                if ratio > 0:
                    g = int(140 + 115 * ratio)
                    b = int(80 - 50 * ratio)
                    n.color = QColor(60, max(60, min(255, g)), max(30, min(200, b)))
                else:
                    r = int(140 - 80 * ratio)
                    n.color = QColor(max(60, min(255, r)), 100, 50)

        detections = [n for n in self._nodes if n.ntype == 'detection']
        for n in detections:
            sev = n.fm.get('severity', 'low')
            if sev == 'high':
                n.color = QColor(255, 40, 40)
            elif sev == 'medium':
                n.color = QColor(240, 120, 40)
            else:
                n.color = QColor(180, 90, 90)

    @staticmethod
    def _subtitle(fm, ntype):
        if ntype == 'session':
            h = fm.get('harvested', 0)
            p = fm.get('planted', 0)
            return f"{h}h {p}p"
        elif ntype == 'seed':
            return f"p:{fm.get('total_planted', 0)} h:{fm.get('total_harvested', 0)}"
        elif ntype == 'garden':
            return f"{fm.get('total_sessions', 0)} sessions"
        elif ntype == 'detection':
            return fm.get('severity', '')
        elif ntype == 'profile':
            return f"{fm.get('confidence', '?')} ({fm.get('samples', 0)})"
        return ''
