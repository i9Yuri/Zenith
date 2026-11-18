# Copyright (c) 2026 Yuri Barbosa (@i9Yuri). All Rights Reserved.
# Proprietary source-available software (personal, non-commercial use only).
# Official repository: https://github.com/i9Yuri/Zenith
# Unauthorized commercial use, redistribution, or rebranding is prohibited. See LICENSE.
"""
AtmosHunter — Interface Gráfica Principal (PySide6)
v2.2:
  - Aba 1: Buscar Faixas (Dolby Atmos 3D, Surround 5.1/7.1, Ultra HD Estéreo) com fila sequencial
  - Aba 2: Minha Biblioteca (~/Music/DolbyAtmos) com tags reais via ffprobe + Tocar na TV/JBL
  - Indicadores de saúde: Rede Atmos (Soulseek) + Servidor DLNA (Rygel)
  - Zero crashes de lifecycle QThread (protegido com shiboken6.isValid)
"""
from __future__ import annotations

import html as html_lib
import logging
import shutil
import subprocess
import sys
import threading
import time
from typing import Optional

from shiboken6 import isValid as qt_is_valid
from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QRect,
    QRectF,
    Qt,
    QThread,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPainterPath,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.dlna_bridge import (
    DlnaRenderer,
    LibraryItem,
    PlaylistSummary,
    check_rygel_status,
    discover_renderers,
    play_on_renderer,
    restart_rygel,
    scan_local_library,
    shutdown_cast_server,
    stop_renderers,
    summarize_playlists,
)
from core.downloader import DOWNLOAD_DIR, DownloadJob, DownloadManager, DownloadStatus
from core.playlist_importer import PlaylistImportReport
from core.query_resolver import ResolvedQuery
from core.ranker import AudioTag, TrackResult, format_duration, format_size, rank_results
from core.search import SearchEngine
from core.slsk_service import get_slsk_service
from core.tv_control import get_tv_controller
from ui.playlist_view import PlaylistImportTab, PlaylistReportDialog

logger = logging.getLogger("AtmosHunter.ui")


# ══════════════════════════════════════════════════════════════
# TOKENS DE DESIGN (SEMÂNTICOS — HALLMARK / IMPECCABLE)
# ══════════════════════════════════════════════════════════════

class T:
    BG_CANVAS      = "#090d16"
    BG_SURFACE     = "#111827"
    BG_ELEVATED    = "#1e293b"
    BG_ROW_ALT     = "#0d1320"
    BG_TOP_PICK    = "#0c2238"

    BORDER_SUBTLE  = "#1f2937"
    BORDER_ACTIVE  = "#38bdf8"

    ACCENT         = "#38bdf8"
    ACCENT_HOVER   = "#7dd3fc"
    ACCENT_PRESS   = "#0284c7"

    SUCCESS        = "#34d399"
    WARNING        = "#fbbf24"
    ERROR          = "#f87171"

    TEXT_PRIMARY   = "#f8fafc"
    TEXT_SECONDARY = "#94a3b8"
    TEXT_MUTED     = "#64748b"

    FONT_UI        = "Inter, Segoe UI, Noto Sans, system-ui, sans-serif"
    FONT_MONO      = "JetBrains Mono, Fira Code, monospace"


# Estilo Amazon Music: cápsulas flat frosted-glass sem borda neon, cantos 5.5px, tipografia sans-serif geométrica
# Formato: kind -> (bg_rgba, text_hex, secondary_text_hex)
TAG_STYLES: dict[str, tuple[tuple[int, int, int, int], str, str]] = {
    "atmos":      ((255, 255, 255, 34), "#dce7ec", "#dce7ec"),  # Réplica exata da tag [DD] Dolby Atmos do Amazon Music
    "lossless":   ((255, 255, 255, 32), "#dce7ec", "#dce7ec"),  # Réplica exata da tag ULTRA HD / LOSSLESS do Amazon Music
    "hd":         ((255, 255, 255, 23), "#b9c9d4", "#b9c9d4"),  # HD / DD+ JOC / DD 5.1 / AAC 5.1 harmônico
    "surround":   ((251, 191, 36, 28),  "#e5cf8e", "#e5cf8e"),  # SURROUND discreto em vidro fosco âmbar suave
    "channels":   ((255, 255, 255, 19), "#c8d6e0", "#c8d6e0"),  # 7.1.4 / 7.1 / 5.1 / 2.0
    "format":     ((255, 255, 255, 14), "#94a6b5", "#94a6b5"),  # MP4 · 768k / FLAC · 24-bit
    "stereo":     ((255, 255, 255, 14), "#8c9eae", "#8c9eae"),  # ESTÉREO discreto
    "verified":   ((52, 211, 153, 30),  "#5eead4", "#5eead4"),  # Codec real confirmado via ffprobe
    "unverified": ((251, 191, 36, 20),  "#cbb984", "#cbb984"),  # Anunciado pelo peer (será verificado ao baixar)
}

GLOBAL_QSS = f"""
QMainWindow, QWidget {{
    background-color: {T.BG_CANVAS};
    color: {T.TEXT_PRIMARY};
    font-family: {T.FONT_UI};
    font-size: 13px;
}}

QFrame#searchFrame {{
    background-color: {T.BG_SURFACE};
    border: 1px solid {T.BORDER_SUBTLE};
    border-radius: 12px;
}}

QLineEdit#searchBar {{
    background-color: transparent;
    border: none;
    padding: 10px 14px;
    font-size: 15px;
    color: {T.TEXT_PRIMARY};
    selection-background-color: {T.ACCENT_PRESS};
}}
QLineEdit#searchBar::placeholder {{
    color: {T.TEXT_MUTED};
}}

QPushButton#btnSearch {{
    background-color: {T.ACCENT};
    color: #04111d;
    border: none;
    border-radius: 9px;
    padding: 9px 22px;
    font-size: 13px;
    font-weight: 700;
}}
QPushButton#btnSearch:hover {{ background-color: {T.ACCENT_HOVER}; }}
QPushButton#btnSearch:pressed {{ background-color: {T.ACCENT_PRESS}; color: #f8fafc; }}
QPushButton#btnSearch:disabled {{
    background-color: {T.BG_ELEVATED};
    color: {T.TEXT_MUTED};
}}

QPushButton.nav-tab {{
    background-color: transparent;
    color: {T.TEXT_SECONDARY};
    border: none;
    border-bottom: 2px solid transparent;
    padding: 7px 14px;
    font-size: 13px;
    font-weight: 600;
}}
QPushButton.nav-tab:hover {{
    color: {T.TEXT_PRIMARY};
}}
QPushButton.nav-tab:checked {{
    color: {T.ACCENT};
    border-bottom: 2px solid {T.ACCENT};
}}

QPushButton.filter-pill {{
    background-color: {T.BG_SURFACE};
    color: {T.TEXT_SECONDARY};
    border: 1px solid {T.BORDER_SUBTLE};
    border-radius: 14px;
    padding: 5px 14px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton.filter-pill:hover {{
    border-color: {T.TEXT_MUTED};
    color: {T.TEXT_PRIMARY};
}}
QPushButton.filter-pill:checked {{
    background-color: rgba(56, 189, 248, 0.14);
    color: {T.ACCENT};
    border-color: rgba(56, 189, 248, 0.45);
}}

QPushButton.suggestion-pill {{
    background-color: {T.BG_SURFACE};
    color: {T.TEXT_SECONDARY};
    border: 1px solid {T.BORDER_SUBTLE};
    border-radius: 16px;
    padding: 7px 15px;
    font-size: 12px;
    font-weight: 500;
}}
QPushButton.suggestion-pill:hover {{
    background-color: {T.BG_ELEVATED};
    border-color: rgba(56, 189, 248, 0.45);
    color: {T.TEXT_PRIMARY};
}}

QPushButton.subtle-btn {{
    background-color: {T.BG_SURFACE};
    color: {T.TEXT_SECONDARY};
    border: 1px solid {T.BORDER_SUBTLE};
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 500;
}}
QPushButton.subtle-btn:hover {{
    background-color: {T.BG_ELEVATED};
    color: {T.TEXT_PRIMARY};
    border-color: {T.TEXT_MUTED};
}}

QTableView {{
    background-color: {T.BG_SURFACE};
    border: 1px solid {T.BORDER_SUBTLE};
    border-radius: 12px;
    gridline-color: transparent;
    selection-background-color: {T.BG_ELEVATED};
    selection-color: {T.TEXT_PRIMARY};
    alternate-background-color: {T.BG_ROW_ALT};
    outline: none;
}}
QTableView::item {{
    padding: 4px 8px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
}}
QTableView::item:selected {{
    background-color: {T.BG_ELEVATED};
}}

QHeaderView::section {{
    background-color: {T.BG_SURFACE};
    color: {T.TEXT_MUTED};
    border: none;
    border-bottom: 1px solid {T.BORDER_SUBTLE};
    padding: 10px 10px;
    font-size: 12px;
    font-weight: 600;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 8px;
}}
QScrollBar::handle:vertical {{
    background: {T.BG_ELEVATED};
    border-radius: 4px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: {T.TEXT_MUTED}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

QProgressBar {{
    background-color: {T.BG_CANVAS};
    border: none;
    border-radius: 4px;
    height: 7px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background-color: {T.ACCENT};
    border-radius: 4px;
}}

QFrame#downloadPanel {{
    background-color: {T.BG_SURFACE};
    border: 1px solid {T.BORDER_SUBTLE};
    border-radius: 12px;
}}

QStatusBar {{
    background-color: {T.BG_SURFACE};
    color: {T.TEXT_SECONDARY};
    border-top: 1px solid {T.BORDER_SUBTLE};
    font-size: 12px;
}}

QToolTip {{
    background-color: {T.BG_ELEVATED};
    color: {T.TEXT_PRIMARY};
    border: 1px solid rgba(56, 189, 248, 0.35);
    border-radius: 8px;
    padding: 8px 10px;
    font-size: 12px;
}}
"""


# ══════════════════════════════════════════════════════════════
# DELEGATES COMPARTILHADOS (BUSCA + BIBLIOTECA)
# ══════════════════════════════════════════════════════════════

ROLE_TITLE_PAIR = Qt.UserRole + 11   # (title, subtitle)
ROLE_TAGS_LIST  = Qt.UserRole + 12   # list[AudioTag]


class TwoLineTitleDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option, index: QModelIndex):
        pair = index.data(ROLE_TITLE_PAIR)
        if not pair:
            super().paint(painter, option, index)
            return
        title, subtitle = pair

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)

        if option.state & QStyle.State_Selected:
            bg = QColor(T.BG_ELEVATED)
        else:
            bg_brush = index.data(Qt.BackgroundRole)
            bg = bg_brush.color() if isinstance(bg_brush, QBrush) else QColor(T.BG_SURFACE)
        painter.fillRect(option.rect, bg)

        painter.setPen(QColor(255, 255, 255, 10))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())

        rect = option.rect.adjusted(10, 7, -10, -7)

        f_title = QFont(T.FONT_UI)
        f_title.setPointSize(10)
        f_title.setWeight(QFont.DemiBold if index.row() == 0 else QFont.Medium)
        painter.setFont(f_title)
        painter.setPen(QColor(T.TEXT_PRIMARY))
        fm_t = QFontMetrics(f_title)
        r_top = QRect(rect.left(), rect.top(), rect.width(), fm_t.height())
        painter.drawText(r_top, Qt.AlignLeft | Qt.AlignVCenter, fm_t.elidedText(title, Qt.ElideRight, rect.width()))

        f_sub = QFont(T.FONT_UI)
        f_sub.setPointSize(8)
        painter.setFont(f_sub)
        painter.setPen(QColor(T.TEXT_MUTED))
        fm_s = QFontMetrics(f_sub)
        r_bot = QRect(rect.left(), r_top.bottom() + 2, rect.width(), fm_s.height())
        painter.drawText(r_bot, Qt.AlignLeft | Qt.AlignVCenter, fm_s.elidedText(subtitle, Qt.ElideRight, rect.width()))

        painter.restore()


def _draw_dolby_double_d(painter: QPainter, x: float, y: float, h: float, color: QColor) -> float:
    """
    Desenha o logotipo oficial Dolby Double-D vetorialmente (dois D espelhados sólidos,
    arestas retas nas extremidades externas e semicírculos voltados para dentro),
    replicando pixel por pixel o ícone do badge do Amazon Music.
    Retorna a largura total ocupada.
    """
    d_w = h * 0.54
    gap = max(1.4, h * 0.14)
    total_w = d_w * 2.0 + gap
    flat_w = d_w * 0.16
    r = h * 0.5

    # D esquerdo: aresta reta na esquerda, arco semicircular voltado para a direita
    left_d = QPainterPath()
    left_d.moveTo(x, y)
    left_d.lineTo(x + flat_w, y)
    left_d.arcTo(QRectF(x + flat_w - r, y, h, h), 90.0, -180.0)
    left_d.lineTo(x, y + h)
    left_d.closeSubpath()

    # D direito: arco semicircular voltado para a esquerda, aresta reta na direita
    rx = x + d_w + gap
    right_d = QPainterPath()
    right_d.moveTo(rx + d_w, y)
    right_d.lineTo(rx + d_w - flat_w, y)
    right_d.arcTo(QRectF(rx + d_w - flat_w - r, y, h, h), 90.0, 180.0)
    right_d.lineTo(rx + d_w, y + h)
    right_d.closeSubpath()

    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(color))
    painter.drawPath(left_d)
    painter.drawPath(right_d)
    return total_w


class AudioTagsDelegate(QStyledItemDelegate):
    """
    Renderiza badges no estilo exato flat frosted-glass do Amazon Music:
      - Dolby Atmos: ícone vetorial Double-D + 'Dolby' (Medium) + 'Atmos' (Light)
      - ULTRA HD / LOSSLESS / SURROUND / HD / Canais / Formato: mesma cápsula flat 5.5px
    """

    def paint(self, painter: QPainter, option, index: QModelIndex):
        tags: Optional[list[AudioTag]] = index.data(ROLE_TAGS_LIST)
        if not tags:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        if option.state & QStyle.State_Selected:
            bg = QColor(T.BG_ELEVATED)
        else:
            bg_brush = index.data(Qt.BackgroundRole)
            bg = bg_brush.color() if isinstance(bg_brush, QBrush) else QColor(T.BG_SURFACE)
        painter.fillRect(option.rect, bg)

        painter.setPen(QColor(255, 255, 255, 10))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())

        # Tipografias harmonizadas com o Amazon Music (sans-serif limpa)
        f_dolby = QFont(T.FONT_UI)
        f_dolby.setPointSizeF(9.0)
        f_dolby.setWeight(QFont.Medium)

        f_atmos = QFont(T.FONT_UI)
        f_atmos.setPointSizeF(9.0)
        f_atmos.setWeight(QFont.Light)

        f_caps = QFont(T.FONT_UI)
        f_caps.setPointSizeF(8.3)
        f_caps.setWeight(QFont.DemiBold)
        f_caps.setLetterSpacing(QFont.PercentageSpacing, 104.0)

        f_meta = QFont(T.FONT_UI)
        f_meta.setPointSizeF(8.1)
        f_meta.setWeight(QFont.Medium)

        fm_dolby = QFontMetrics(f_dolby)
        fm_atmos = QFontMetrics(f_atmos)
        fm_caps = QFontMetrics(f_caps)
        fm_meta = QFontMetrics(f_meta)

        pill_h = 24.0
        radius = 5.5
        pad_x = 9.0
        gap_x = 6.5
        x = float(option.rect.left() + 8)
        y = float(option.rect.top() + (option.rect.height() - pill_h) / 2.0)
        max_x = float(option.rect.right() - 8)

        for tag in tags:
            bg_rgba, text_hex, sec_hex = TAG_STYLES.get(tag.kind, TAG_STYLES["format"])

            if tag.kind == "atmos":
                # Réplica pixel por pixel da tag [DD] Dolby Atmos do Amazon Music
                icon_h = 9.5
                icon_w = icon_h * 0.54 * 2.0 + max(1.4, icon_h * 0.14)
                icon_gap = 4.5
                word_gap = 3.2
                w_dolby = float(fm_dolby.horizontalAdvance("Dolby"))
                w_atmos = float(fm_atmos.horizontalAdvance("Atmos"))
                pill_w = pad_x + icon_w + icon_gap + w_dolby + word_gap + w_atmos + pad_x

                if x + pill_w > max_x:
                    break

                pill_rect = QRectF(x, y, pill_w, pill_h)
                path = QPainterPath()
                path.addRoundedRect(pill_rect, radius, radius)
                painter.fillPath(path, QColor(*bg_rgba))

                # Ícone Double-D alinhado opticamente à altura das maiúsculas de 'Dolby'
                ix = x + pad_x
                iy = y + (pill_h - icon_h) / 2.0 + 0.4
                _draw_dolby_double_d(painter, ix, iy, icon_h, QColor(text_hex))

                # "Dolby" em peso Medium
                tx = ix + icon_w + icon_gap
                painter.setFont(f_dolby)
                painter.setPen(QColor(text_hex))
                painter.drawText(QRectF(tx, y, w_dolby + 2.0, pill_h), Qt.AlignLeft | Qt.AlignVCenter, "Dolby")

                # "Atmos" em peso Light
                tx2 = tx + w_dolby + word_gap
                painter.setFont(f_atmos)
                painter.setPen(QColor(sec_hex))
                painter.drawText(QRectF(tx2, y, w_atmos + 2.0, pill_h), Qt.AlignLeft | Qt.AlignVCenter, "Atmos")

                x += pill_w + gap_x
                continue

            # Demais tags harmonizadas no estilo flat frosted-glass do Amazon Music
            if tag.kind in ("lossless", "surround", "hd", "stereo"):
                font = f_caps
                fm = fm_caps
                p_x = 8.5 if tag.kind == "lossless" else 7.5
            else:
                font = f_meta
                fm = fm_meta
                p_x = 7.5

            pill_w = float(fm.horizontalAdvance(tag.text)) + p_x * 2.0
            if x + pill_w > max_x:
                break

            pill_rect = QRectF(x, y, pill_w, pill_h)
            path = QPainterPath()
            path.addRoundedRect(pill_rect, radius, radius)
            painter.fillPath(path, QColor(*bg_rgba))

            painter.setFont(font)
            color_hex = "#e2ecf2" if (tag.kind == "channels" and tag.text == "7.1.4") else text_hex
            painter.setPen(QColor(color_hex))
            painter.drawText(pill_rect, Qt.AlignCenter, tag.text)
            x += pill_w + gap_x

        painter.restore()


# ══════════════════════════════════════════════════════════════
# MODELS: RESULTADOS DE BUSCA E BIBLIOTECA LOCAL
# ══════════════════════════════════════════════════════════════

COLUMNS = ["#", "Faixa", "Qualidade e Espacialidade", "Tamanho", "Disponibilidade", "Ação"]
COL_RANK   = 0
COL_TRACK  = 1
COL_TAGS   = 2
COL_SIZE   = 3
COL_AVAIL  = 4
COL_ACTION = 5


class ResultsModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_results: list[TrackResult] = []
        self._seen_keys: set[str] = set()
        self._visible: list[TrackResult] = []
        self._filter_mode: str = "all"
        self._current_query: str = ""
        self._canonical_query: str = ""
        self._canonical_artist: str = ""
        self._canonical_title: str = ""

    def clear(self, query: str = ""):
        self.beginResetModel()
        self._all_results = []
        self._seen_keys = set()
        self._visible = []
        self._current_query = query
        self._canonical_query = ""
        self._canonical_artist = ""
        self._canonical_title = ""
        self.endResetModel()

    def set_resolved_query(self, rq: ResolvedQuery):
        self._canonical_query = rq.canonical_full or rq.corrected
        self._canonical_artist = rq.canonical_artist
        self._canonical_title = rq.canonical_title
        if self._all_results:
            self._rebuild_visible()

    def set_filter(self, mode: str):
        self._filter_mode = mode
        self._rebuild_visible()

    def add_batch(self, batch: list[TrackResult]):
        added = False
        for item in batch:
            dedup_key = f"{item.provider}|{(item.remote_user or '').lower()}|{(item.remote_path or item.download_url or item.filename).lower()}"
            if dedup_key in self._seen_keys:
                continue
            self._seen_keys.add(dedup_key)
            self._all_results.append(item)
            added = True
        if added:
            self._rebuild_visible()

    def refresh_after_verification(self):
        """Re-avalia e reordena os resultados da busca usando os codecs reais recém-confirmados pelo ffprobe."""
        if self._all_results:
            self._rebuild_visible()

    def visible_results(self) -> list[TrackResult]:
        return list(self._visible)

    def _rebuild_visible(self):
        ranked = rank_results(
            self._all_results,
            query=self._current_query,
            drop_junk=True,
            canonical_query=self._canonical_query,
            canonical_artist=self._canonical_artist,
            canonical_title=self._canonical_title,
        )
        if self._filter_mode == "atmos":
            filtered = [r for r in ranked if r.codec_tier in (1, 2)]
        elif self._filter_mode == "surround":
            filtered = [r for r in ranked if r.codec_tier == 3]
        elif self._filter_mode == "hires":
            filtered = [r for r in ranked if r.codec_tier == 4]
        else:
            filtered = ranked

        self.beginResetModel()
        self._visible = filtered
        self.endResetModel()

    def counts(self) -> tuple[int, int, int, int]:
        ranked = rank_results(
            self._all_results,
            query=self._current_query,
            drop_junk=True,
            canonical_query=self._canonical_query,
            canonical_artist=self._canonical_artist,
            canonical_title=self._canonical_title,
        )
        atmos = sum(1 for r in ranked if r.codec_tier in (1, 2))
        surround = sum(1 for r in ranked if r.codec_tier == 3)
        hires = sum(1 for r in ranked if r.codec_tier == 4)
        return len(ranked), atmos, surround, hires

    def result_at(self, row: int) -> Optional[TrackResult]:
        return self._visible[row] if 0 <= row < len(self._visible) else None

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._visible)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        r = self._visible[row]

        if role == ROLE_TITLE_PAIR and col == COL_TRACK:
            parts = []
            dur = format_duration(r.duration_sec)
            if dur:
                parts.append(dur)
            if r.album and r.album.lower() != r.title.lower():
                parts.append(r.album)
            parts.append(r.source)
            return (r.display_name, "  ·  ".join(parts))

        if role == ROLE_TAGS_LIST and col == COL_TAGS:
            return r.tags

        if role == Qt.DisplayRole:
            if col == COL_RANK:
                return f"#{row + 1}"
            if col == COL_TRACK:
                return r.display_name
            if col == COL_TAGS:
                return " · ".join(t.text for t in r.tags)
            if col == COL_SIZE:
                return r.size_label or format_size(r.size_bytes)
            if col == COL_AVAIL:
                return r.availability_label
            if col == COL_ACTION:
                return ""

        elif role == Qt.TextAlignmentRole:
            if col in (COL_RANK, COL_SIZE):
                return Qt.AlignCenter
            return Qt.AlignVCenter | Qt.AlignLeft

        elif role == Qt.ForegroundRole:
            if col == COL_RANK:
                return QBrush(QColor(T.SUCCESS if row == 0 else T.TEXT_MUTED))
            if col == COL_AVAIL:
                return QBrush(QColor(T.SUCCESS if (r.provider == "http" or r.has_free_slot) else T.WARNING))
            return QBrush(QColor(T.TEXT_SECONDARY))

        elif role == Qt.BackgroundRole:
            if row == 0:
                return QBrush(QColor(T.BG_TOP_PICK))
            return QBrush(QColor(T.BG_SURFACE if row % 2 == 0 else T.BG_ROW_ALT))

        elif role == Qt.FontRole:
            if col in (COL_RANK, COL_SIZE):
                font = QFont(T.FONT_MONO)
                font.setPointSize(10)
                if col == COL_RANK and row == 0:
                    font.setBold(True)
                return font
            return QFont(T.FONT_UI)

        elif role == Qt.ToolTipRole:
            tags_str = html_lib.escape("  |  ".join(t.text for t in r.tags))
            lines = [f"<b>{html_lib.escape(r.display_name)}</b>", f"<br><span style='color:{T.ACCENT}'>{tags_str}</span>"]
            if r.jbl_explanation:
                lines.append(f"<br>{html_lib.escape(r.jbl_explanation)}")
            lines.append(
                f"<br><span style='color:{T.TEXT_MUTED}'>Arquivo: {html_lib.escape(r.filename)} "
                f"({html_lib.escape(r.size_label)}) · Score {r.score:.0f}/100</span>"
            )
            return "".join(lines)

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable


LIB_COLUMNS = ["Faixa Salva", "Áudio Verificado (ffprobe)", "Tamanho", "Ações"]
LIB_COL_TITLE  = 0
LIB_COL_TAGS   = 1
LIB_COL_SIZE   = 2
LIB_COL_ACTION = 3


class LibraryModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[LibraryItem] = []

    def set_items(self, items: list[LibraryItem]):
        self.beginResetModel()
        self._items = items
        self.endResetModel()

    def item_at(self, row: int) -> Optional[LibraryItem]:
        return self._items[row] if 0 <= row < len(self._items) else None

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._items)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(LIB_COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return LIB_COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        it = self._items[row]

        if role == ROLE_TITLE_PAIR and col == LIB_COL_TITLE:
            dur = format_duration(it.duration_sec)
            title_txt = f"{it.track_order:02d}. {it.title}" if it.track_order > 0 else it.title
            sub_parts = []
            if it.playlist_name:
                sub_parts.append(f"Playlist: {it.playlist_name}")
            if dur:
                sub_parts.append(dur)
            sub_parts.append(it.filename)
            return (title_txt, "  ·  ".join(sub_parts))

        if role == ROLE_TAGS_LIST and col == LIB_COL_TAGS:
            return it.tags

        if role == Qt.DisplayRole:
            if col == LIB_COL_TITLE:
                return it.title
            if col == LIB_COL_TAGS:
                return it.codec_summary
            if col == LIB_COL_SIZE:
                return it.size_label
            return ""

        elif role == Qt.TextAlignmentRole:
            if col == LIB_COL_SIZE:
                return Qt.AlignCenter
            return Qt.AlignVCenter | Qt.AlignLeft

        elif role == Qt.ForegroundRole:
            return QBrush(QColor(T.TEXT_SECONDARY))

        elif role == Qt.BackgroundRole:
            return QBrush(QColor(T.BG_SURFACE if row % 2 == 0 else T.BG_ROW_ALT))

        elif role == Qt.FontRole:
            if col == LIB_COL_SIZE:
                f = QFont(T.FONT_MONO)
                f.setPointSize(10)
                return f
            return QFont(T.FONT_UI)

        elif role == Qt.ToolTipRole:
            return (
                f"<b>{html_lib.escape(it.title)}</b><br>{html_lib.escape(it.codec_summary)}"
                f"<br><span style='color:{T.TEXT_MUTED}'>{html_lib.escape(str(it.path))}</span>"
            )

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable


# ══════════════════════════════════════════════════════════════
# WORKER DE BUSCA ASSÍNCRONA
# ══════════════════════════════════════════════════════════════

class SearchWorker(QObject):
    resolved_ready = Signal(object)
    batch_ready = Signal(str, list)
    finished = Signal()
    error = Signal(str)

    def __init__(self, query: str, engine: SearchEngine):
        super().__init__()
        self.query = query
        self.engine = engine

    def run(self):
        try:
            self.engine.resolved_cb = lambda rq: self.resolved_ready.emit(rq)
            self.engine.progress_cb = lambda src, items: self.batch_ready.emit(src, items)
            self.engine.search(self.query)
            self.finished.emit()
        except Exception as e:
            logger.exception("Erro na thread de busca")
            self.error.emit(str(e))


# ══════════════════════════════════════════════════════════════
# PAINEL DE DOWNLOAD INFERIOR
# ══════════════════════════════════════════════════════════════

class DownloadPanel(QFrame):
    cancel_requested = Signal()
    open_folder_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("downloadPanel")
        self.hide()
        self._mode = "cancel"   # "cancel" | "folder" | "dismiss"

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(14)

        left = QVBoxLayout()
        left.setSpacing(5)

        top_row = QHBoxLayout()
        self.lbl_title = QLabel("—")
        self.lbl_title.setStyleSheet(f"color: {T.TEXT_PRIMARY}; font-weight: 600; font-size: 13px;")
        self.lbl_detail = QLabel("")
        self.lbl_detail.setStyleSheet(
            f"color: {T.TEXT_SECONDARY}; font-family: {T.FONT_MONO}; font-size: 11px;"
        )
        top_row.addWidget(self.lbl_title, stretch=1)
        top_row.addWidget(self.lbl_detail)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(7)

        left.addLayout(top_row)
        left.addWidget(self.progress)
        layout.addLayout(left, stretch=1)

        self.btn_action = QPushButton("Cancelar")
        self.btn_action.setFixedWidth(108)
        self.btn_action.setCursor(Qt.PointingHandCursor)
        self.btn_action.setStyleSheet(
            f"QPushButton {{ background: {T.BG_ELEVATED}; color: {T.TEXT_SECONDARY}; "
            f"border: 1px solid {T.BORDER_SUBTLE}; border-radius: 7px; padding: 6px 12px; font-weight: 600; }}"
            f"QPushButton:hover {{ color: {T.TEXT_PRIMARY}; border-color: {T.TEXT_MUTED}; }}"
        )
        self.btn_action.clicked.connect(self._on_action_btn)
        layout.addWidget(self.btn_action)

    def _on_action_btn(self):
        if self._mode == "folder":
            self.open_folder_requested.emit()
        elif self._mode == "dismiss":
            self.hide()
        else:
            self.cancel_requested.emit()

    def update_job(self, job: DownloadJob):
        self.show()
        q_suffix = f"  (+{job.queue_remaining} na fila)" if job.queue_remaining > 0 else ""
        short = job.title[:72] + ("…" if len(job.title) > 72 else "")
        self.lbl_title.setText(f"{short}{q_suffix}")

        if job.status in (DownloadStatus.STARTING, DownloadStatus.QUEUED):
            self._mode = "cancel"
            self.progress.setRange(0, 0)
            self.lbl_detail.setText(job.status_detail or "Preparando...")
            self.lbl_detail.setStyleSheet(f"color: {T.WARNING}; font-family: {T.FONT_MONO}; font-size: 11px;")
            self.btn_action.setText("Cancelar")
            self.btn_action.setEnabled(True)

        elif job.status == DownloadStatus.DOWNLOADING:
            self._mode = "cancel"
            self.progress.setRange(0, 100)
            pct = int(job.progress * 100)
            self.progress.setValue(pct)
            self.progress.setStyleSheet(f"QProgressBar::chunk {{ background-color: {T.ACCENT}; border-radius: 4px; }}")
            spd = f" · {job.speed_label}" if job.speed_label else ""
            self.lbl_detail.setText(f"{pct}%{spd} · {job.status_detail}")
            self.lbl_detail.setStyleSheet(f"color: {T.ACCENT}; font-family: {T.FONT_MONO}; font-size: 11px;")
            self.btn_action.setText("Cancelar")
            self.btn_action.setEnabled(True)

        elif job.status == DownloadStatus.VERIFYING:
            self._mode = "cancel"
            self.progress.setRange(0, 100)
            self.progress.setValue(99)
            self.lbl_detail.setText(job.status_detail or "Empacotando Dolby Atmos...")
            self.lbl_detail.setStyleSheet(f"color: {T.ACCENT}; font-family: {T.FONT_MONO}; font-size: 11px;")
            self.btn_action.setEnabled(False)

        elif job.status == DownloadStatus.DONE:
            self._mode = "folder"
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            if job.verification_warning:
                self.progress.setStyleSheet(f"QProgressBar::chunk {{ background-color: {T.WARNING}; border-radius: 4px; }}")
                self.lbl_detail.setText(job.status_detail or job.verification_warning)
                self.lbl_detail.setStyleSheet(f"color: {T.WARNING}; font-weight: 600; font-size: 12px;")
            else:
                self.progress.setStyleSheet(f"QProgressBar::chunk {{ background-color: {T.SUCCESS}; border-radius: 4px; }}")
                self.lbl_detail.setText(job.status_detail or "Pronto! Disponível na TV/Soundbar")
                self.lbl_detail.setStyleSheet(f"color: {T.SUCCESS}; font-weight: 600; font-size: 12px;")
            self.btn_action.setText("Abrir pasta")
            self.btn_action.setEnabled(True)

        elif job.status == DownloadStatus.ERROR:
            self._mode = "dismiss"
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.progress.setStyleSheet(f"QProgressBar::chunk {{ background-color: {T.ERROR}; border-radius: 4px; }}")
            self.lbl_detail.setText(job.error_msg or "Erro no download.")
            self.lbl_detail.setStyleSheet(f"color: {T.ERROR}; font-size: 12px;")
            self.btn_action.setText("Fechar")
            self.btn_action.setEnabled(True)

        elif job.status == DownloadStatus.CANCELLED:
            self._mode = "dismiss"
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.lbl_detail.setText("Download cancelado.")
            self.lbl_detail.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 12px;")
            self.btn_action.setText("Fechar")
            self.btn_action.setEnabled(True)


# ══════════════════════════════════════════════════════════════
# JANELA PRINCIPAL
# ══════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    _sig_dl_update = Signal(object)
    _sig_slsk_status = Signal(bool, str)
    _sig_dlna_status = Signal(bool, str)
    _sig_library_loaded = Signal(list)
    _sig_cast_result = Signal(bool, str)
    _sig_peer_verified = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("AtmosHunter — Dolby Atmos Music")
        self.setMinimumSize(1080, 680)
        self.resize(1240, 760)

        self._slsk = get_slsk_service()
        self._tv = get_tv_controller()
        self._model = ResultsModel(self)
        self._lib_model = LibraryModel(self)
        self._engine: Optional[SearchEngine] = None
        self._dl_manager = DownloadManager(self._slsk)
        self._search_thread: Optional[QThread] = None
        self._search_worker: Optional[SearchWorker] = None

        self._active_track_key: Optional[str] = None
        self._queued_keys: set[str] = set()
        self._saved_keys: set[str] = set()
        self._row_buttons: dict[str, QPushButton] = {}
        self._renderers: list[DlnaRenderer] = []
        self._resolved_hint: str = ""
        self._all_library_items: list[LibraryItem] = []
        self._playlist_summaries: list[PlaylistSummary] = []
        self._active_lib_scope: str = "__ALL__"   # "__ALL__" | "__LOOSE__" | <playlist_name>
        self._lib_search_text: str = ""
        self._lib_only_atmos: bool = False
        self._lib_scope_group: Optional[QButtonGroup] = None

        self._sig_dl_update.connect(self._on_dl_update)
        self._sig_slsk_status.connect(self._on_slsk_status)
        self._sig_dlna_status.connect(self._on_dlna_status)
        self._sig_library_loaded.connect(self._on_library_loaded)
        self._sig_cast_result.connect(self._on_cast_result)
        self._sig_peer_verified.connect(self._on_peer_verified_ui)

        self._build_ui()
        self.setStyleSheet(GLOBAL_QSS)

        self._slsk.add_status_listener(lambda ok, msg: self._sig_slsk_status.emit(ok, msg))
        if self._slsk.is_connected:
            self._on_slsk_status(True, "Rede Dolby Atmos pronta")
        self._slsk.start()

        # Verificar Rygel + carregar biblioteca local em background
        self._refresh_ecosystem_async()

    def closeEvent(self, event):
        self._cancel_search()
        if hasattr(self, "playlist_tab"):
            self.playlist_tab.cancel_active()
        self._dl_manager.cancel_all()
        shutdown_cast_server()
        self._slsk.stop()
        super().closeEvent(event)

    # ── CONSTRUÇÃO DA INTERFACE ──────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 14, 20, 12)
        root.setSpacing(10)

        # ── Header ──
        header = QHBoxLayout()
        header.setSpacing(14)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        lbl_title = QLabel("AtmosHunter")
        lbl_title.setStyleSheet(f"font-size: 20px; font-weight: 700; color: {T.TEXT_PRIMARY};")
        lbl_sub = QLabel("Faixas individuais em Dolby Atmos 7.1.4 e Ultra HD · JBL Bar 1000MK2")
        lbl_sub.setStyleSheet(f"font-size: 12px; color: {T.TEXT_SECONDARY};")
        title_box.addWidget(lbl_title)
        title_box.addWidget(lbl_sub)
        header.addLayout(title_box)
        header.addStretch()

        # Status Rede Atmos (Soulseek) clicável para reconectar caso oscile
        self.btn_net = QPushButton("● Conectando rede Atmos...")
        self.btn_net.setProperty("class", "subtle-btn")
        self.btn_net.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.btn_net.setCursor(Qt.PointingHandCursor)
        self.btn_net.setToolTip("Status da rede Dolby Atmos (Soulseek). Clique para reconectar se necessário.")
        self.btn_net.clicked.connect(self._on_reconnect_slsk)
        header.addWidget(self.btn_net)

        # Status Servidor DLNA (Rygel) clicável para reiniciar se necessário
        self.btn_dlna = QPushButton("● DLNA ativo")
        self.btn_dlna.setProperty("class", "subtle-btn")
        self.btn_dlna.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.btn_dlna.setCursor(Qt.PointingHandCursor)
        self.btn_dlna.setToolTip("Status do servidor DLNA (Rygel). Clique para recarregar.")
        self.btn_dlna.clicked.connect(self._on_restart_rygel)
        header.addWidget(self.btn_dlna)

        btn_folder = QPushButton("Pasta DolbyAtmos")
        btn_folder.setProperty("class", "subtle-btn")
        btn_folder.setCursor(Qt.PointingHandCursor)
        btn_folder.clicked.connect(self._open_dest_folder)
        header.addWidget(btn_folder)

        root.addLayout(header)

        # ── Abas de Navegação (Buscar Faixas | Importar Playlist | Minha Biblioteca) ──
        nav_row = QHBoxLayout()
        nav_row.setSpacing(4)
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)

        self.tab_search = QPushButton("Buscar Faixas")
        self.tab_search.setCheckable(True)
        self.tab_search.setChecked(True)
        self.tab_search.setProperty("class", "nav-tab")
        self.tab_search.setCursor(Qt.PointingHandCursor)
        self.tab_search.clicked.connect(lambda: self._switch_page(0))

        self.tab_playlist = QPushButton("Importar Playlist")
        self.tab_playlist.setCheckable(True)
        self.tab_playlist.setProperty("class", "nav-tab")
        self.tab_playlist.setCursor(Qt.PointingHandCursor)
        self.tab_playlist.clicked.connect(lambda: self._switch_page(1))

        self.tab_library = QPushButton("Minha Biblioteca (0)")
        self.tab_library.setCheckable(True)
        self.tab_library.setProperty("class", "nav-tab")
        self.tab_library.setCursor(Qt.PointingHandCursor)
        self.tab_library.clicked.connect(lambda: self._switch_page(2))

        self._nav_group.addButton(self.tab_search)
        self._nav_group.addButton(self.tab_playlist)
        self._nav_group.addButton(self.tab_library)
        nav_row.addWidget(self.tab_search)
        nav_row.addWidget(self.tab_playlist)
        nav_row.addWidget(self.tab_library)
        nav_row.addStretch()
        root.addLayout(nav_row)

        # ── Container Principal de Páginas ──
        self.playlist_tab = PlaylistImportTab(
            slsk_service=self._slsk,
            tokens=T,
            tag_styles=TAG_STYLES,
            title_delegate_cls=TwoLineTitleDelegate,
            tags_delegate_cls=AudioTagsDelegate,
            role_title_pair=ROLE_TITLE_PAIR,
            role_tags_list=ROLE_TAGS_LIST,
            parent=self,
        )
        self.playlist_tab.status_message.connect(lambda m: self.status_bar.showMessage(m))
        self.playlist_tab.library_changed.connect(self._refresh_ecosystem_async)
        self.playlist_tab.open_playlist_in_library.connect(self._open_specific_playlist_in_library)

        self.main_pages = QStackedWidget()
        self.main_pages.addWidget(self._build_search_page())
        self.main_pages.addWidget(self.playlist_tab)
        self.main_pages.addWidget(self._build_library_page())
        root.addWidget(self.main_pages, stretch=1)

        # ── Painel de Download Compartilhado ──
        self.dl_panel = DownloadPanel()
        self.dl_panel.cancel_requested.connect(self._on_cancel_download)
        self.dl_panel.open_folder_requested.connect(self._open_dest_folder)
        root.addWidget(self.dl_panel)

        # ── Guia Compacto no Rodapé ──
        footer = QHBoxLayout()
        footer.setSpacing(14)
        for color, badge, desc in [
            ("#dce7ec", "Dolby Atmos · 7.1.4", "Objetos 3D + teto (JBL Bar 1000MK2)"),
            ("#dce7ec", "ULTRA HD / LOSSLESS", "Sem perdas / 24-bit"),
            ("#e5cf8e", "SURROUND 5.1/7.1", "Traseiras sem teto"),
        ]:
            lbl = QLabel(f"<b style='color:{color}'>{badge}</b> <span style='color:{T.TEXT_MUTED}'>{desc}</span>")
            lbl.setStyleSheet("font-size: 11px;")
            footer.addWidget(lbl)
        footer.addStretch()
        lbl_dest = QLabel(f"Destino: {DOWNLOAD_DIR}")
        lbl_dest.setStyleSheet(f"color: {T.TEXT_MUTED}; font-family: {T.FONT_MONO}; font-size: 11px;")
        footer.addWidget(lbl_dest)
        root.addLayout(footer)

        self.status_bar = self.statusBar()
        self.status_bar.showMessage("Dica: você pode clicar em Baixar em várias músicas seguidas para enfileirar.")

        QShortcut(QKeySequence("Ctrl+L"), self, activated=self._focus_search)
        QShortcut(QKeySequence("Escape"), self, activated=self._cancel_search)

    def _build_search_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(10)

        # Barra de Busca
        search_frame = QFrame()
        search_frame.setObjectName("searchFrame")
        sf_layout = QHBoxLayout(search_frame)
        sf_layout.setContentsMargins(6, 5, 6, 5)
        sf_layout.setSpacing(8)

        self.search_input = QLineEdit()
        self.search_input.setObjectName("searchBar")
        self.search_input.setPlaceholderText(
            "Digite o nome da música ou artista (ex: Last Christmas, Blinding Lights, Pink Floyd Time)"
        )
        self.search_input.returnPressed.connect(self._on_search)

        self.btn_search = QPushButton("Buscar")
        self.btn_search.setObjectName("btnSearch")
        self.btn_search.setFixedWidth(112)
        self.btn_search.setCursor(Qt.PointingHandCursor)
        self.btn_search.clicked.connect(self._on_search)

        sf_layout.addWidget(self.search_input, stretch=1)
        sf_layout.addWidget(self.btn_search)
        lay.addWidget(search_frame)

        self.search_progress = QProgressBar()
        self.search_progress.setRange(0, 0)
        self.search_progress.setFixedHeight(3)
        self.search_progress.hide()
        lay.addWidget(self.search_progress)

        # Filtros + Contagem
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        self._filter_btns: dict[str, QPushButton] = {}
        for key, label in [
            ("all", "Todas"),
            ("atmos", "Dolby Atmos"),
            ("surround", "Surround 5.1 / 7.1"),
            ("hires", "Ultra HD Estéreo"),
        ]:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setProperty("class", "filter-pill")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _, m=key: self._on_filter_change(m))
            self._filter_group.addButton(b)
            self._filter_btns[key] = b
            toolbar.addWidget(b)
        self._filter_btns["all"].setChecked(True)
        toolbar.addStretch()

        self.lbl_count = QLabel("Pronto para buscar faixas individuais.")
        self.lbl_count.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 12px;")
        toolbar.addWidget(self.lbl_count)
        lay.addLayout(toolbar)

        # Stack Empty / Tabela
        self.stack = QStackedWidget()

        empty_page = QFrame()
        empty_page.setStyleSheet(
            f"QFrame {{ background-color: {T.BG_SURFACE}; border: 1px solid {T.BORDER_SUBTLE}; border-radius: 12px; }}"
        )
        ep_layout = QVBoxLayout(empty_page)
        ep_layout.setAlignment(Qt.AlignCenter)
        ep_layout.setSpacing(12)

        self.lbl_empty_title = QLabel("Busque uma música para baixar em arquivo único")
        self.lbl_empty_title.setAlignment(Qt.AlignCenter)
        self.lbl_empty_title.setStyleSheet(
            f"font-size: 16px; font-weight: 600; color: {T.TEXT_PRIMARY}; border: none; background: transparent;"
        )
        self.lbl_empty_desc = QLabel(
            "Faixas Dolby Atmos são empacotadas automaticamente sem perda de áudio,\n"
            "prontas para tocar na sua TV Samsung + JBL Bar 1000MK2 pelo BubbleUPnP."
        )
        self.lbl_empty_desc.setAlignment(Qt.AlignCenter)
        self.lbl_empty_desc.setStyleSheet(
            f"font-size: 13px; color: {T.TEXT_SECONDARY}; border: none; background: transparent;"
        )

        sugg_row = QHBoxLayout()
        sugg_row.setSpacing(8)
        sugg_row.setAlignment(Qt.AlignCenter)
        for sample in ["Last Christmas", "The Weeknd Blinding Lights", "Pink Floyd Time", "Bohemian Rhapsody"]:
            sb = QPushButton(sample)
            sb.setProperty("class", "suggestion-pill")
            sb.setCursor(Qt.PointingHandCursor)
            sb.clicked.connect(lambda _, q=sample: self._quick_search(q))
            sugg_row.addWidget(sb)

        ep_layout.addWidget(self.lbl_empty_title)
        ep_layout.addWidget(self.lbl_empty_desc)
        ep_layout.addSpacing(8)
        ep_layout.addLayout(sugg_row)

        self.table = QTableView()
        self.table.setModel(self._model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.doubleClicked.connect(self._on_row_double_click)

        self.table.setItemDelegateForColumn(COL_TRACK, TwoLineTitleDelegate(self.table))
        self.table.setItemDelegateForColumn(COL_TAGS, AudioTagsDelegate(self.table))

        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(COL_RANK,   QHeaderView.Fixed)
        hdr.setSectionResizeMode(COL_TRACK,  QHeaderView.Stretch)
        hdr.setSectionResizeMode(COL_TAGS,   QHeaderView.Fixed)
        hdr.setSectionResizeMode(COL_SIZE,   QHeaderView.Fixed)
        hdr.setSectionResizeMode(COL_AVAIL,  QHeaderView.Fixed)
        hdr.setSectionResizeMode(COL_ACTION, QHeaderView.Fixed)
        self.table.setColumnWidth(COL_RANK,   52)
        self.table.setColumnWidth(COL_TAGS,   465)
        self.table.setColumnWidth(COL_SIZE,   92)
        self.table.setColumnWidth(COL_AVAIL,  132)
        self.table.setColumnWidth(COL_ACTION, 102)
        self.table.verticalHeader().setDefaultSectionSize(54)

        self.stack.addWidget(empty_page)
        self.stack.addWidget(self.table)
        self.stack.setCurrentIndex(0)
        lay.addWidget(self.stack, stretch=1)
        return page

    def _build_library_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(10)

        # Barra dedicada de seleção de Playlists com rolagem horizontal (suporta dezenas de playlists sem espremer pills)
        self.lib_scope_scroll = QScrollArea()
        self.lib_scope_scroll.setWidgetResizable(False)
        self.lib_scope_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.lib_scope_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lib_scope_scroll.setFixedHeight(44)
        self.lib_scope_scroll.setFrameShape(QFrame.NoFrame)
        self.lib_scope_scroll.setStyleSheet(
            f"QScrollArea {{ background: transparent; border: none; }}"
            f"QScrollBar:horizontal {{ height: 6px; background: transparent; margin: 2px 0 0 0; }}"
            f"QScrollBar::handle:horizontal {{ background: {T.BG_ELEVATED}; border-radius: 3px; min-width: 32px; }}"
            f"QScrollBar::handle:horizontal:hover {{ background: {T.TEXT_MUTED}; }}"
            f"QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}"
        )
        self._scope_container = QWidget()
        self._scope_container.setStyleSheet("background: transparent;")
        self.lib_scope_bar = QHBoxLayout(self._scope_container)
        self.lib_scope_bar.setSizeConstraint(QLayout.SetFixedSize)
        self.lib_scope_bar.setContentsMargins(0, 2, 8, 2)
        self.lib_scope_bar.setSpacing(8)
        self.lib_scope_scroll.setWidget(self._scope_container)
        lay.addWidget(self.lib_scope_scroll)

        top = QHBoxLayout()
        top.setSpacing(8)

        self.lib_filter_input = QLineEdit()
        self.lib_filter_input.setPlaceholderText("Filtrar músicas...")
        self.lib_filter_input.setFixedWidth(190)
        self.lib_filter_input.setClearButtonEnabled(True)
        self.lib_filter_input.setStyleSheet(
            f"QLineEdit {{ background: {T.BG_SURFACE}; color: {T.TEXT_PRIMARY}; border: 1px solid {T.BORDER_SUBTLE}; "
            f"border-radius: 8px; padding: 5px 10px; font-size: 12px; }}"
            f"QLineEdit:focus {{ border-color: rgba(56, 189, 248, 0.55); }}"
        )
        self.lib_filter_input.textChanged.connect(self._on_lib_filter_changed)
        top.addWidget(self.lib_filter_input)

        self.btn_lib_only_atmos = QPushButton("Só Atmos")
        self.btn_lib_only_atmos.setCheckable(True)
        self.btn_lib_only_atmos.setProperty("class", "filter-pill")
        self.btn_lib_only_atmos.setCursor(Qt.PointingHandCursor)
        self.btn_lib_only_atmos.toggled.connect(self._on_lib_only_atmos_toggled)
        top.addWidget(self.btn_lib_only_atmos)

        self.lbl_lib_info = QLabel("Carregando sua biblioteca Dolby Atmos...")
        self.lbl_lib_info.setMinimumWidth(0)
        self.lbl_lib_info.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.lbl_lib_info.setStyleSheet(f"color: {T.TEXT_SECONDARY}; font-size: 12px; padding-left: 4px;")
        top.addWidget(self.lbl_lib_info, stretch=1)

        btn_stop_cast = QPushButton("Parar TV/JBL")
        btn_stop_cast.setProperty("class", "subtle-btn")
        btn_stop_cast.setCursor(Qt.PointingHandCursor)
        btn_stop_cast.setToolTip("Interrompe a reprodução DLNA atual na TV Samsung e na JBL")
        btn_stop_cast.clicked.connect(self._on_stop_renderers)
        top.addWidget(btn_stop_cast)

        self.btn_tv_auto = QPushButton("TV Samsung ▾")
        self.btn_tv_auto.setProperty("class", "subtle-btn")
        self.btn_tv_auto.setCursor(Qt.PointingHandCursor)
        self.btn_tv_auto.setToolTip("Automação da TV Samsung (Wake-on-LAN, Apagar Tela e Pareamento único)")
        self.btn_tv_auto.clicked.connect(self._on_open_tv_menu)
        top.addWidget(self.btn_tv_auto)

        btn_refresh = QPushButton("Atualizar")
        btn_refresh.setProperty("class", "subtle-btn")
        btn_refresh.setCursor(Qt.PointingHandCursor)
        btn_refresh.clicked.connect(self._refresh_ecosystem_async)
        top.addWidget(btn_refresh)
        lay.addLayout(top)

        # Barra contextual de ações da playlist (exibida apenas ao selecionar uma playlist específica)
        self.playlist_actions_bar = QFrame()
        self.playlist_actions_bar.setStyleSheet(
            f"QFrame {{ background: {T.BG_SURFACE}; border: 1px solid {T.BORDER_SUBTLE}; border-radius: 8px; }}"
        )
        pl_bar = QHBoxLayout(self.playlist_actions_bar)
        pl_bar.setContentsMargins(12, 6, 10, 6)
        pl_bar.setSpacing(8)

        self.lbl_pl_context = QLabel("")
        self.lbl_pl_context.setStyleSheet(f"color: {T.TEXT_PRIMARY}; font-size: 12px; font-weight: 600; border: none; background: transparent;")
        pl_bar.addWidget(self.lbl_pl_context)
        pl_bar.addStretch()

        self.btn_lib_report = QPushButton("Ver Relatório")
        self.btn_lib_report.setProperty("class", "subtle-btn")
        self.btn_lib_report.setCursor(Qt.PointingHandCursor)
        self.btn_lib_report.clicked.connect(self._on_open_playlist_report)
        pl_bar.addWidget(self.btn_lib_report)

        self.btn_lib_pl_folder = QPushButton("Abrir Pasta da Playlist")
        self.btn_lib_pl_folder.setProperty("class", "subtle-btn")
        self.btn_lib_pl_folder.setCursor(Qt.PointingHandCursor)
        self.btn_lib_pl_folder.clicked.connect(self._on_open_active_playlist_folder)
        pl_bar.addWidget(self.btn_lib_pl_folder)

        self.btn_lib_del_pl = QPushButton("Excluir Playlist")
        self.btn_lib_del_pl.setProperty("class", "subtle-btn")
        self.btn_lib_del_pl.setCursor(Qt.PointingHandCursor)
        self.btn_lib_del_pl.clicked.connect(self._on_delete_active_playlist)
        pl_bar.addWidget(self.btn_lib_del_pl)

        self.playlist_actions_bar.hide()
        lay.addWidget(self.playlist_actions_bar)

        self.lib_table = QTableView()
        self.lib_table.setModel(self._lib_model)
        self.lib_table.setAlternatingRowColors(True)
        self.lib_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.lib_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.lib_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.lib_table.verticalHeader().hide()
        self.lib_table.setShowGrid(False)
        self.lib_table.setWordWrap(False)
        self.lib_table.doubleClicked.connect(self._on_lib_row_double_click)

        self.lib_table.setItemDelegateForColumn(LIB_COL_TITLE, TwoLineTitleDelegate(self.lib_table))
        self.lib_table.setItemDelegateForColumn(LIB_COL_TAGS, AudioTagsDelegate(self.lib_table))

        hdr = self.lib_table.horizontalHeader()
        hdr.setSectionResizeMode(LIB_COL_TITLE,  QHeaderView.Stretch)
        hdr.setSectionResizeMode(LIB_COL_TAGS,   QHeaderView.Fixed)
        hdr.setSectionResizeMode(LIB_COL_SIZE,   QHeaderView.Fixed)
        hdr.setSectionResizeMode(LIB_COL_ACTION, QHeaderView.Fixed)
        self.lib_table.setColumnWidth(LIB_COL_TAGS,   425)
        self.lib_table.setColumnWidth(LIB_COL_SIZE,   94)
        self.lib_table.setColumnWidth(LIB_COL_ACTION, 200)
        self.lib_table.verticalHeader().setDefaultSectionSize(54)

        lay.addWidget(self.lib_table, stretch=1)
        return page

    # ── NAVEGAÇÃO E ECOSSISTEMA ──────────────────────────────

    def _focus_search(self):
        self.tab_search.setChecked(True)
        self._switch_page(0)
        self.search_input.setFocus()

    def _switch_page(self, idx: int):
        self.main_pages.setCurrentIndex(idx)
        self.tab_search.setChecked(idx == 0)
        self.tab_playlist.setChecked(idx == 1)
        self.tab_library.setChecked(idx == 2)
        if idx == 2:
            self._refresh_ecosystem_async()

    def _open_specific_playlist_in_library(self, playlist_name: str):
        self._active_lib_scope = playlist_name
        self._switch_page(2)

    def _refresh_ecosystem_async(self):
        def _work():
            ok, msg = check_rygel_status()
            self._sig_dlna_status.emit(ok, msg)
            items = scan_local_library(DOWNLOAD_DIR)
            self._renderers = discover_renderers()
            self._sig_library_loaded.emit(items)

        threading.Thread(target=_work, daemon=True, name="eco-refresh").start()

    def _on_slsk_status(self, connected: bool, message: str):
        color = T.SUCCESS if connected else T.WARNING
        self.btn_net.setText(f"● {message}")
        self.btn_net.setStyleSheet(
            f"QPushButton {{ background: {T.BG_SURFACE}; color: {color}; "
            f"border: 1px solid {T.BORDER_SUBTLE}; border-radius: 8px; padding: 6px 12px; font-size: 12px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: {T.BG_ELEVATED}; border-color: {T.TEXT_MUTED}; }}"
        )
        self.btn_net.adjustSize()
        self.btn_net.updateGeometry()

    def _on_reconnect_slsk(self):
        self.status_bar.showMessage("Reconectando à rede Dolby Atmos (Soulseek)...")
        self._slsk.reconnect(force_new_account=True)

    def _on_dlna_status(self, active: bool, message: str):
        color = T.SUCCESS if active else T.ERROR
        self.btn_dlna.setText(f"● {message}")
        self.btn_dlna.setStyleSheet(
            f"QPushButton {{ background: {T.BG_SURFACE}; color: {color}; "
            f"border: 1px solid {T.BORDER_SUBTLE}; border-radius: 8px; padding: 6px 12px; font-size: 12px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: {T.BG_ELEVATED}; border-color: {T.TEXT_MUTED}; }}"
        )
        self.btn_dlna.adjustSize()
        self.btn_dlna.updateGeometry()

    def _on_restart_rygel(self):
        self.status_bar.showMessage("Reconstruindo índice e reiniciando servidor DLNA (Rygel)...")
        def _work():
            ok, msg = restart_rygel(rebuild_db=True)
            self._sig_dlna_status.emit(ok, msg)
        threading.Thread(target=_work, daemon=True).start()

    def _on_library_loaded(self, items: list[LibraryItem]):
        self._all_library_items = items
        self._playlist_summaries = summarize_playlists(items, DOWNLOAD_DIR)
        self.tab_library.setText(f"Minha Biblioteca ({len(items)})")

        # Se o escopo ativo era uma playlist que deixou de existir, voltar para Todas
        valid_pl_names = {ps.name for ps in self._playlist_summaries}
        if self._active_lib_scope not in ("__ALL__", "__LOOSE__") and self._active_lib_scope not in valid_pl_names:
            self._active_lib_scope = "__ALL__"

        self._rebuild_library_scope_pills()
        self._apply_library_scope()

    def _rebuild_library_scope_pills(self):
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        bar = QHBoxLayout(container)
        bar.setSizeConstraint(QLayout.SetFixedSize)
        bar.setContentsMargins(0, 2, 8, 2)
        bar.setSpacing(8)

        self._lib_scope_group = QButtonGroup(self)
        self._lib_scope_group.setExclusive(True)

        loose_count = sum(1 for it in self._all_library_items if not it.playlist_name)
        pills: list[tuple[str, str]] = [
            ("__ALL__", f"Todas ({len(self._all_library_items)})"),
            ("__LOOSE__", f"Avulsas ({loose_count})"),
        ]
        for ps in self._playlist_summaries:
            alert_s = f" · {ps.issue_count}!" if ps.issue_count else ""
            short_n = ps.name[:26] + "…" if len(ps.name) > 26 else ps.name
            pills.append((ps.name, f"{short_n} ({ps.track_count}){alert_s}"))

        for scope_key, label in pills:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setProperty("class", "filter-pill")
            btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setChecked(scope_key == self._active_lib_scope)
            if scope_key not in ("__ALL__", "__LOOSE__"):
                btn.setToolTip(f"Playlist: {scope_key}")
            btn.clicked.connect(lambda _, sk=scope_key: self._set_library_scope(sk))
            self._lib_scope_group.addButton(btn)
            bar.addWidget(btn)

        self._scope_container = container
        self.lib_scope_bar = bar
        self.lib_scope_scroll.setWidget(container)

    def _set_library_scope(self, scope_key: str):
        self._active_lib_scope = scope_key
        self._apply_library_scope()

    def _on_lib_filter_changed(self, text: str):
        self._lib_search_text = text.strip().lower()
        self._apply_library_scope()

    def _on_lib_only_atmos_toggled(self, checked: bool):
        self._lib_only_atmos = checked
        self._apply_library_scope()

    def _on_stop_renderers(self):
        self.status_bar.showMessage("Enviando comando Parar para TV Samsung / JBL...")
        def _work():
            ok, msg = stop_renderers(self._renderers)
            self._sig_cast_result.emit(ok, msg)
        threading.Thread(target=_work, daemon=True).start()

    def _on_open_tv_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background: {T.BG_ELEVATED}; color: {T.TEXT_PRIMARY}; "
            f"border: 1px solid {T.BORDER_SUBTLE}; border-radius: 8px; padding: 4px; }}"
            f"QMenu::item {{ padding: 7px 16px; border-radius: 5px; }}"
            f"QMenu::item:selected {{ background: rgba(56, 189, 248, 0.16); color: {T.ACCENT}; }}"
            f"QMenu::separator {{ height: 1px; background: {T.BORDER_SUBTLE}; margin: 4px 8px; }}"
        )
        pair_label = "Parear controle da TV (pareado)" if self._tv.is_paired else "Parear controle da TV (1ª vez)"
        act_pair = menu.addAction(pair_label)
        act_wake = menu.addAction("Acordar TV agora (Wake-on-LAN)")
        act_screen_off = menu.addAction("Apagar tela da TV (Picture Off)")
        act_screen_on = menu.addAction("Acender tela da TV")
        act_stop_standby = menu.addAction("Parar música e desligar TV (Standby)")
        act_standby = menu.addAction("Colocar TV em standby")
        menu.addSeparator()

        act_auto_wake = menu.addAction("Acordar TV automaticamente ao tocar")
        act_auto_wake.setCheckable(True)
        act_auto_wake.setChecked(self._tv.config.auto_wake)

        act_auto_off = menu.addAction("Apagar tela automaticamente ao tocar Atmos")
        act_auto_off.setCheckable(True)
        act_auto_off.setChecked(self._tv.config.auto_screen_off)

        chosen = menu.exec(self.btn_tv_auto.mapToGlobal(self.btn_tv_auto.rect().bottomLeft()))
        if chosen == act_pair:
            self.status_bar.showMessage("Conectando à TV Samsung — clique em 'Permitir' no aviso que aparecer na tela da TV...")
            threading.Thread(target=lambda: self._sig_cast_result.emit(*self._tv.pair()), daemon=True).start()
        elif chosen == act_wake:
            self.status_bar.showMessage("Enviando Wake-on-LAN para a TV Samsung...")
            threading.Thread(
                target=lambda: self._sig_cast_result.emit(
                    *self._tv.wake_and_wait(status_cb=lambda m: self._sig_cast_result.emit(True, m))
                ),
                daemon=True,
            ).start()
        elif chosen == act_screen_off:
            self.status_bar.showMessage("Enviando comando para apagar apenas a tela da TV...")
            threading.Thread(target=lambda: self._sig_cast_result.emit(*self._tv.turn_screen_off()), daemon=True).start()
        elif chosen == act_screen_on:
            self.status_bar.showMessage("Acendendo a tela da TV Samsung...")
            threading.Thread(target=lambda: self._sig_cast_result.emit(*self._tv.turn_screen_on()), daemon=True).start()
        elif chosen == act_stop_standby:
            self.status_bar.showMessage("Parando reprodução e colocando TV Samsung em standby...")
            def _do_stop_standby():
                stop_renderers(self._renderers)
                time.sleep(0.3)
                ok, msg = self._tv.standby_tv()
                if ok:
                    self._renderers = [r for r in self._renderers if not r.is_tv]
                self._sig_cast_result.emit(ok, f"Música parada · {msg}" if ok else msg)
            threading.Thread(target=_do_stop_standby, daemon=True).start()
        elif chosen == act_standby:
            self.status_bar.showMessage("Colocando TV Samsung em standby...")
            def _do_standby():
                ok, msg = self._tv.standby_tv()
                if ok:
                    self._renderers = [r for r in self._renderers if not r.is_tv]
                self._sig_cast_result.emit(ok, msg)
            threading.Thread(target=_do_standby, daemon=True).start()
        elif chosen == act_auto_wake:
            self._tv.config.auto_wake = act_auto_wake.isChecked()
            self._tv.save()
            self.status_bar.showMessage(
                "Wake-on-LAN automático ativado ao clicar em Tocar na TV."
                if self._tv.config.auto_wake
                else "Wake-on-LAN automático desativado."
            )
        elif chosen == act_auto_off:
            self._tv.config.auto_screen_off = act_auto_off.isChecked()
            self._tv.save()
            self.status_bar.showMessage(
                "Tela da TV será apagada automaticamente após iniciar Dolby Atmos."
                if self._tv.config.auto_screen_off
                else "Apagar tela automaticamente desativado."
            )

    def _apply_library_scope(self):
        scope = self._active_lib_scope
        if scope == "__ALL__":
            filtered = list(self._all_library_items)
            is_playlist_view = False
        elif scope == "__LOOSE__":
            filtered = [it for it in self._all_library_items if not it.playlist_name]
            is_playlist_view = False
        else:
            filtered = [it for it in self._all_library_items if it.playlist_name == scope]
            filtered.sort(key=lambda it: (it.track_order if it.track_order else 9999, it.filename.lower()))
            is_playlist_view = True

        if self._lib_only_atmos:
            filtered = [it for it in filtered if it.is_atmos]
        if self._lib_search_text:
            q = self._lib_search_text
            filtered = [
                it for it in filtered
                if q in it.title.lower() or q in it.filename.lower() or q in it.codec_summary.lower() or q in it.playlist_name.lower()
            ]

        self._lib_model.set_items(filtered)

        total_mb = sum(i.size_bytes for i in filtered) / (1024 ** 2)
        atmos_n = sum(1 for i in filtered if i.is_atmos)
        faixa_lbl = "faixa" if len(filtered) == 1 else "faixas"
        rnd_names = ", ".join(r.name for r in self._renderers) or "nenhum reprodutor detectado agora"

        if is_playlist_view:
            ps = next((p for p in self._playlist_summaries if p.name == scope), None)
            self.lbl_pl_context.setText(f"Playlist: {scope}")
            self.playlist_actions_bar.show()
            if ps and ps.has_report:
                alert_lbl = f" ({ps.issue_count} alerta{'s' if ps.issue_count != 1 else ''})" if ps.issue_count else " (OK)"
                self.btn_lib_report.setText(f"Ver Relatório{alert_lbl}")
                self.btn_lib_report.show()
            else:
                self.btn_lib_report.hide()
            self.lbl_lib_info.setText(
                f"{len(filtered)} {faixa_lbl} ({atmos_n} Atmos · {total_mb:.1f} MB)  ·  {rnd_names}"
            )
        else:
            self.playlist_actions_bar.hide()
            pl_total = len(self._playlist_summaries)
            pl_str = f" · {pl_total} playlist{'s' if pl_total != 1 else ''}" if pl_total else ""
            self.lbl_lib_info.setText(
                f"{len(filtered)} {faixa_lbl} ({atmos_n} Atmos · {total_mb:.1f} MB{pl_str})  ·  {rnd_names}"
            )

        self._sync_library_buttons()

    def _on_open_playlist_report(self):
        scope = self._active_lib_scope
        if scope in ("__ALL__", "__LOOSE__"):
            return
        folder = DOWNLOAD_DIR / scope
        report = PlaylistImportReport.load(folder)
        if not report:
            self.status_bar.showMessage("Relatório desta playlist não foi encontrado.")
            return
        dlg = PlaylistReportDialog(
            report=report,
            tokens=T,
            tag_styles=TAG_STYLES,
            allow_retry=not self.playlist_tab.is_busy,
            parent=self,
        )
        dlg.retry_requested.connect(lambda rep=report: self._retry_playlist_from_library(rep))
        dlg.exec()

    def _retry_playlist_from_library(self, report: PlaylistImportReport):
        self._switch_page(1)
        self.playlist_tab.load_report_for_retry(report)

    def _on_open_active_playlist_folder(self):
        scope = self._active_lib_scope
        if scope in ("__ALL__", "__LOOSE__"):
            self._open_dest_folder()
            return
        folder = DOWNLOAD_DIR / scope
        folder.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(folder)], start_new_session=True)

    def _on_delete_active_playlist(self):
        scope = self._active_lib_scope
        if scope in ("__ALL__", "__LOOSE__"):
            return
        folder = DOWNLOAD_DIR / scope
        resp = QMessageBox.question(
            self,
            "Excluir playlist",
            f"Remover a pasta completa da playlist '{scope}' e todas as faixas dentro dela?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resp == QMessageBox.Yes:
            try:
                shutil.rmtree(folder, ignore_errors=False)
                self._active_lib_scope = "__ALL__"
                self.status_bar.showMessage(f"Playlist '{scope}' excluída.")
                self._refresh_ecosystem_async()
            except Exception as e:
                self.status_bar.showMessage(f"Erro ao excluir playlist: {e}")

    def _sync_library_buttons(self):
        for row in range(self._lib_model.rowCount()):
            it = self._lib_model.item_at(row)
            if not it:
                continue

            wrap = QWidget()
            wrap.setStyleSheet("background: transparent;")
            hl = QHBoxLayout(wrap)
            hl.setContentsMargins(6, 9, 6, 9)
            hl.setSpacing(6)

            needs_tv = it.is_video_wrapped or it.is_atmos or it.is_surround or it.path.suffix.lower() in (".mp4", ".mkv")
            btn_cast = QPushButton("Tocar na TV" if needs_tv else "Tocar na JBL")
            btn_cast.setFixedHeight(32)
            btn_cast.setCursor(Qt.PointingHandCursor)
            btn_cast.setToolTip(
                "Envia para a TV Samsung (a3) com áudio Dolby Atmos / Multicanal via HDMI eARC na JBL Bar 1000MK2"
                if needs_tv
                else "Envia áudio estéreo direto para a JBL Bar 1000MK2 (ou TV Samsung)"
            )
            btn_cast.setStyleSheet(
                f"QPushButton {{ background: rgba(56, 189, 248, 0.14); color: {T.ACCENT}; "
                f"border: 1px solid rgba(56, 189, 248, 0.4); border-radius: 7px; padding: 0 10px; font-size: 12px; font-weight: 600; }}"
                f"QPushButton:hover {{ background: {T.ACCENT}; color: #04111d; }}"
            )
            btn_cast.clicked.connect(lambda _, item=it: self._on_cast_library_item(item))

            btn_del = QPushButton("Excluir")
            btn_del.setFixedHeight(32)
            btn_del.setCursor(Qt.PointingHandCursor)
            btn_del.setStyleSheet(
                f"QPushButton {{ background: {T.BG_ELEVATED}; color: {T.TEXT_SECONDARY}; "
                f"border: 1px solid {T.BORDER_SUBTLE}; border-radius: 7px; padding: 0 10px; font-size: 12px; }}"
                f"QPushButton:hover {{ color: {T.ERROR}; border-color: rgba(248, 113, 113, 0.5); }}"
            )
            btn_del.clicked.connect(lambda _, item=it: self._on_delete_library_item(item))

            hl.addWidget(btn_cast)
            hl.addWidget(btn_del)
            idx = self._lib_model.index(row, LIB_COL_ACTION)
            old_w = self.lib_table.indexWidget(idx)
            if old_w is not None:
                old_w.hide()
                old_w.setParent(None)
                old_w.deleteLater()
            self.lib_table.setIndexWidget(idx, wrap)

    def _on_lib_row_double_click(self, index: QModelIndex):
        item = self._lib_model.item_at(index.row())
        if item:
            self._on_cast_library_item(item)

    def _on_cast_library_item(self, item: LibraryItem):
        needs_tv = item.is_video_wrapped or item.is_atmos or item.is_surround or item.path.suffix.lower() in (".mp4", ".mkv")
        self.status_bar.showMessage(f"Preparando {'TV Samsung' if needs_tv else 'JBL Bar 1000MK2'} para '{item.title}'...")

        def _send():
            renderers = self._renderers
            target: Optional[DlnaRenderer] = None
            if needs_tv:
                # Sempre validar estado REAL da TV antes de confiar no cache self._renderers:
                # reconhece 'on' E 'pictureoff' como acordada; limpa cache se estiver em 'standby'.
                woke_up = False
                tv_state = self._tv.get_power_state(timeout=2.0)
                dlna_ready = self._tv.is_awake_state(tv_state) and self._tv.is_dlna_ready(timeout=1.5)

                if dlna_ready:
                    target = next((r for r in renderers if r.is_tv), None)
                    if not target:
                        renderers = discover_renderers()
                        self._renderers = renderers
                        target = next((r for r in renderers if r.is_tv), None)
                else:
                    # Remover entrada obsoleta da TV em standby do cache
                    self._renderers = [r for r in self._renderers if not r.is_tv]
                    target = None

                    if self._tv.config.auto_wake:
                        ok_w, msg_w = self._tv.wake_and_wait(
                            status_cb=lambda m: self._sig_cast_result.emit(True, m)
                        )
                        if not ok_w:
                            self._sig_cast_result.emit(False, msg_w)
                            return
                        woke_up = True
                        renderers = discover_renderers()
                        self._renderers = renderers
                        target = next((r for r in renderers if r.is_tv), None)
                    else:
                        self._sig_cast_result.emit(
                            False,
                            "TV Samsung (a3) está em standby. Ative 'Acordar TV automaticamente' no menu TV Samsung ou ligue a TV.",
                        )
                        return

                # Fallback direto para a URL padrão AVTransport da Samsung se o SSDP ainda estiver anunciando após acordar
                if not target and self._tv.is_dlna_ready(timeout=1.5):
                    target = DlnaRenderer(
                        name=f"TV Samsung ({self._tv.config.name})",
                        ip=self._tv.config.ip,
                        location=f"http://{self._tv.config.ip}:9197/dmr",
                        av_transport_url=f"http://{self._tv.config.ip}:9197/upnp/control/AVTransport1",
                        is_tv=True,
                    )

                if not target:
                    self._sig_cast_result.emit(
                        False,
                        "TV Samsung (a3) não respondeu ao DLNA. Aguarde alguns segundos ou clique em Atualizar.",
                    )
                    return

                # Manter IP da TV sincronizado caso mude via DHCP
                self._tv.update_endpoint(ip=target.ip)
                ok, msg = play_on_renderer(item, target)
                # Logo após acordar do standby, o player Tizen pode levar +2s para aceitar SetAVTransportURI
                if not ok and woke_up:
                    self._sig_cast_result.emit(True, "TV iniciando player interno — tentando reproduzir novamente...")
                    time.sleep(2.5)
                    ok, msg = play_on_renderer(item, target)

                if ok and self._tv.config.auto_screen_off and self._tv.is_paired:
                    self._sig_cast_result.emit(True, f"{msg} · apagando tela da TV...")
                    time.sleep(2.5)
                    ok_off, _ = self._tv.turn_screen_off()
                    if ok_off:
                        self._sig_cast_result.emit(True, f"{msg} (tela apagada · Dolby Atmos ativo na JBL)")
                        return
                self._sig_cast_result.emit(ok, msg)
                return

            # FLAC estéreo -> JBL direto (ou TV se JBL ausente)
            target = next((r for r in renderers if r.is_jbl), None) or next((r for r in renderers if r.is_tv), None)
            if not target:
                renderers = discover_renderers()
                self._renderers = renderers
                target = next((r for r in renderers if r.is_jbl), None) or (renderers[0] if renderers else None)
            if not target:
                self._sig_cast_result.emit(
                    False,
                    "Nenhum reprodutor DLNA (TV Samsung ou JBL) encontrado agora. Verifique o Wi-Fi e clique em Atualizar.",
                )
                return

            ok, msg = play_on_renderer(item, target)
            self._sig_cast_result.emit(ok, msg)

        threading.Thread(target=_send, daemon=True).start()

    def _on_cast_result(self, ok: bool, msg: str):
        self.status_bar.showMessage(msg)

    def _on_delete_library_item(self, item: LibraryItem):
        resp = QMessageBox.question(
            self,
            "Excluir faixa",
            f"Remover '{item.filename}' da pasta DolbyAtmos?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resp == QMessageBox.Yes:
            try:
                item.path.unlink(missing_ok=True)
                self._saved_keys.clear()
                self._sync_action_buttons()
                self.status_bar.showMessage(f"Removido: {item.filename}")
                self._refresh_ecosystem_async()
            except Exception as e:
                self.status_bar.showMessage(f"Erro ao excluir: {e}")

    # ── BUSCA ────────────────────────────────────────────────

    def _quick_search(self, query: str):
        self.search_input.setText(query)
        self._on_search()

    def _on_filter_change(self, mode: str):
        self._model.set_filter(mode)
        self._sync_action_buttons()
        if self._model.rowCount() > 0:
            self.stack.setCurrentIndex(1)

    def _on_search(self):
        query = self.search_input.text().strip()
        if not query:
            self.search_input.setFocus()
            return

        self._cancel_search()
        self._model.clear(query=query)
        self._row_buttons.clear()
        self._resolved_hint = ""
        self.btn_search.setEnabled(False)
        self.btn_search.setText("Buscando...")
        self.search_progress.show()
        self.lbl_count.setText(f"Buscando faixas para '{query}'...")
        self.status_bar.showMessage(f"Buscando '{query}' em Dolby Atmos 3D, Surround e Ultra HD...")

        self._engine = SearchEngine(slsk_service=self._slsk)
        worker = SearchWorker(query, self._engine)
        thread = QThread()
        worker.moveToThread(thread)

        self._search_worker = worker
        self._search_thread = thread

        thread.started.connect(worker.run)
        worker.resolved_ready.connect(self._on_resolved_ready)
        worker.batch_ready.connect(self._on_batch_ready)
        worker.finished.connect(self._on_search_finished)
        worker.error.connect(self._on_search_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(lambda t=thread: self._on_thread_cleaned(t))
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_resolved_ready(self, rq: ResolvedQuery):
        self._model.set_resolved_query(rq)
        hint = rq.display_hint
        if hint:
            self._resolved_hint = hint
            self.status_bar.showMessage(f"Buscando por '{hint}' em Dolby Atmos 3D, Hi-Res Direct e Internet Archive...")

    def _on_thread_cleaned(self, thread: QThread):
        if self._search_thread is thread:
            self._search_thread = None
            self._search_worker = None

    def _cancel_search(self):
        if self._engine:
            self._engine.stop()
        thread = self._search_thread
        if thread is not None and qt_is_valid(thread):
            if thread.isRunning():
                thread.quit()
                thread.wait(1500)
        self._search_thread = None
        self._search_worker = None
        self.search_progress.hide()
        self.btn_search.setEnabled(True)
        self.btn_search.setText("Buscar")

    def _update_counts_label(self) -> int:
        total, atmos, surround, hires = self._model.counts()
        hint = f"  ·  Música: {self._resolved_hint}" if self._resolved_hint else ""
        self.lbl_count.setText(
            f"{total} faixas · {atmos} Atmos 3D · {surround} Surround · {hires} Ultra HD{hint}"
        )
        return total

    def _on_batch_ready(self, source: str, batch: list):
        self._model.add_batch(batch)
        self._sync_action_buttons()
        total = self._update_counts_label()
        if total > 0:
            self.stack.setCurrentIndex(1)
            self.status_bar.showMessage(f"Recebendo faixas ({source})...")

    def _on_search_finished(self):
        self.search_progress.hide()
        self.btn_search.setEnabled(True)
        self.btn_search.setText("Buscar")
        self._sync_action_buttons()

        total = self._update_counts_label()
        if total == 0:
            self.stack.setCurrentIndex(0)
            self.lbl_empty_title.setText("Nenhuma faixa individual encontrada")
            self.lbl_empty_desc.setText(
                "Tente digitar apenas o artista e o nome principal da música (sem parênteses ou ano)."
            )
            self.lbl_count.setText("0 faixas encontradas")
            self.status_bar.showMessage("Busca concluída sem resultados.")
        else:
            self.stack.setCurrentIndex(1)
            self.status_bar.showMessage(
                f"Busca concluída: {total} faixas prontas. Passe o mouse sobre as tags para detalhes técnicos."
            )

    def _on_search_error(self, msg: str):
        self.search_progress.hide()
        self.btn_search.setEnabled(True)
        self.btn_search.setText("Buscar")
        self.lbl_count.setText("Erro durante a busca.")
        self.status_bar.showMessage(f"Erro na busca: {msg}")

    # ── BOTÕES DE AÇÃO NA TABELA ─────────────────────────────

    def _track_key(self, r: TrackResult) -> str:
        return f"{r.provider}|{r.remote_user}|{r.remote_path or r.download_url}"

    def _sync_action_buttons(self):
        self._row_buttons.clear()
        for row in range(self._model.rowCount()):
            track = self._model.result_at(row)
            if not track:
                continue
            key = self._track_key(track)
            btn = QPushButton()
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(32)

            if key in self._saved_keys:
                btn.setText("Salvo")
                btn.setEnabled(False)
                btn.setStyleSheet(
                    f"QPushButton {{ background: rgba(52, 211, 153, 0.14); color: {T.SUCCESS}; "
                    f"border: 1px solid rgba(52, 211, 153, 0.4); border-radius: 7px; "
                    f"padding: 0px 10px; font-size: 12px; font-weight: 600; }}"
                )
            elif key == self._active_track_key:
                btn.setText("Baixando")
                btn.setEnabled(False)
                btn.setStyleSheet(
                    f"QPushButton {{ background: rgba(56, 189, 248, 0.14); color: {T.ACCENT}; "
                    f"border: 1px solid rgba(56, 189, 248, 0.4); border-radius: 7px; "
                    f"padding: 0px 10px; font-size: 12px; font-weight: 600; }}"
                )
            elif key in self._queued_keys:
                btn.setText("Na fila")
                btn.setEnabled(False)
                btn.setStyleSheet(
                    f"QPushButton {{ background: rgba(251, 191, 36, 0.14); color: {T.WARNING}; "
                    f"border: 1px solid rgba(251, 191, 36, 0.4); border-radius: 7px; "
                    f"padding: 0px 10px; font-size: 12px; font-weight: 600; }}"
                )
            else:
                btn.setText("Baixar")
                if row == 0:
                    btn.setStyleSheet(
                        f"QPushButton {{ background: {T.ACCENT}; color: #04111d; border: none; "
                        f"border-radius: 7px; padding: 0px 12px; font-size: 12px; font-weight: 700; }}"
                        f"QPushButton:hover {{ background: {T.ACCENT_HOVER}; }}"
                    )
                else:
                    btn.setStyleSheet(
                        f"QPushButton {{ background: {T.BG_ELEVATED}; color: {T.ACCENT}; "
                        f"border: 1px solid rgba(56, 189, 248, 0.35); border-radius: 7px; "
                        f"padding: 0px 12px; font-size: 12px; font-weight: 600; }}"
                        f"QPushButton:hover {{ background: {T.ACCENT}; color: #04111d; border-color: {T.ACCENT}; }}"
                    )
                btn.clicked.connect(lambda _, t=track: self._start_download(t))

            self._row_buttons[key] = btn
            wrap = QWidget()
            wrap.setStyleSheet("background: transparent;")
            wl = QHBoxLayout(wrap)
            wl.setContentsMargins(6, 9, 6, 9)
            wl.addWidget(btn)
            idx = self._model.index(row, COL_ACTION)
            old_w = self.table.indexWidget(idx)
            if old_w is not None:
                old_w.hide()
                old_w.setParent(None)
                old_w.deleteLater()
            self.table.setIndexWidget(idx, wrap)

    # ── DOWNLOAD (FILA SEQUENCIAL) ───────────────────────────

    def _on_peer_verified_ui(self):
        self._model.refresh_after_verification()
        self._sync_action_buttons()
        self._update_counts_label()

    def _collect_atmos_fallbacks(self, primary: TrackResult, max_fallbacks: int = 3) -> list[TrackResult]:
        """Seleciona até 3 candidatos Atmos alternativos da mesma música para failover automático se o peer #1 mentir."""
        if primary.provider != "soulseek" or primary.codec_tier not in (1, 2):
            return []
        primary_key = self._track_key(primary)
        seen_users = {primary.remote_user.lower()}
        fallbacks: list[TrackResult] = []
        for cand in self._model.visible_results():
            if self._track_key(cand) == primary_key:
                continue
            if cand.provider != "soulseek" or cand.codec_tier not in (1, 2):
                continue
            if int(cand.extra.get("relevance_tier", 3)) < 2:
                continue
            if cand.remote_user.lower() in seen_users:
                continue
            seen_users.add(cand.remote_user.lower())
            fallbacks.append(cand)
            if len(fallbacks) >= max_fallbacks:
                break
        return fallbacks

    def _start_download(self, track: TrackResult):
        key = self._track_key(track)
        if key == self._active_track_key or key in self._queued_keys:
            return

        fallbacks = self._collect_atmos_fallbacks(track, max_fallbacks=3)
        self._queued_keys.add(key)
        self._sync_action_buttons()
        self.status_bar.showMessage(f"Adicionado à fila: {track.display_name}")
        self._dl_manager.start_download(
            track,
            lambda job: self._sig_dl_update.emit(job),
            fallback_tracks=fallbacks,
            on_peer_verified=lambda _cand, _va: self._sig_peer_verified.emit(),
        )

    def _on_cancel_download(self):
        self._dl_manager.cancel_current()

    def _on_dl_update(self, job: DownloadJob):
        key = self._track_key(job.track)

        if job.status in (DownloadStatus.STARTING, DownloadStatus.DOWNLOADING, DownloadStatus.VERIFYING):
            self._queued_keys.discard(key)
            if self._active_track_key != key:
                self._active_track_key = key
                self._sync_action_buttons()

        self.dl_panel.update_job(job)

        btn = self._row_buttons.get(key)
        if job.status == DownloadStatus.DOWNLOADING and btn is not None and qt_is_valid(btn):
            btn.setText(f"{int(job.progress * 100)}%")

        elif job.status == DownloadStatus.DONE:
            self._queued_keys.discard(key)
            if self._active_track_key == key:
                self._active_track_key = None
            self._saved_keys.add(key)
            if job.actual_track:
                self._saved_keys.add(self._track_key(job.actual_track))
            self._sync_action_buttons()
            fname = job.saved_path.name if job.saved_path else job.title
            if job.verification_warning:
                self.status_bar.showMessage(f"{job.verification_warning} · {fname}")
            else:
                self.status_bar.showMessage(
                    f"Pronto! Verificado ({job.verified_audio}) · {fname}"
                )
            # Atualizar a aba Minha Biblioteca silenciosamente
            self._refresh_ecosystem_async()

        elif job.status in (DownloadStatus.ERROR, DownloadStatus.CANCELLED):
            self._queued_keys.discard(key)
            if self._active_track_key == key:
                self._active_track_key = None
            self._sync_action_buttons()
            if job.status == DownloadStatus.ERROR:
                self.status_bar.showMessage(f"Falha no download: {job.error_msg}")
            else:
                self.status_bar.showMessage("Download cancelado.")

    # ── MENU DE CONTEXTO & DETALHES ──────────────────────────

    def _open_dest_folder(self):
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(DOWNLOAD_DIR)], start_new_session=True)

    def _on_context_menu(self, pos):
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        track = self._model.result_at(index.row())
        if not track:
            return

        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background: {T.BG_ELEVATED}; color: {T.TEXT_PRIMARY}; "
            f"border: 1px solid {T.BORDER_SUBTLE}; border-radius: 8px; padding: 4px; }}"
            f"QMenu::item {{ padding: 7px 18px; border-radius: 5px; }}"
            f"QMenu::item:selected {{ background: rgba(56, 189, 248, 0.16); color: {T.ACCENT}; }}"
        )
        act_dl = menu.addAction("Baixar / adicionar à fila")
        act_copy = menu.addAction("Copiar nome da faixa")
        act_details = menu.addAction("Ver ficha técnica completa")

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == act_dl:
            self._start_download(track)
        elif chosen == act_copy:
            QApplication.clipboard().setText(track.display_name)
            self.status_bar.showMessage("Nome copiado.")
        elif chosen == act_details:
            self._show_details(track)

    def _on_row_double_click(self, index: QModelIndex):
        track = self._model.result_at(index.row())
        if track:
            self._show_details(track)

    def _show_details(self, r: TrackResult):
        dlg = QDialog(self)
        dlg.setWindowTitle("Ficha Técnica da Faixa")
        dlg.resize(580, 380)
        dlg.setStyleSheet(f"QDialog {{ background: {T.BG_SURFACE}; color: {T.TEXT_PRIMARY}; }}")
        layout = QVBoxLayout(dlg)

        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setStyleSheet(
            f"background: {T.BG_CANVAS}; color: {T.TEXT_PRIMARY}; "
            f"border: 1px solid {T.BORDER_SUBTLE}; border-radius: 8px; "
            f"font-family: {T.FONT_UI}; font-size: 13px; padding: 12px;"
        )
        tags_html = " ".join(
            f"<span style='background:rgba(255,255,255,0.12); color:{TAG_STYLES.get(t.kind, TAG_STYLES['format'])[1]}; "
            f"padding:3px 9px; border-radius:5px; font-weight:600; font-size:12px;'>{html_lib.escape(t.text)}</span>"
            for t in r.tags
        )
        txt.setHtml(
            f"<h3 style='color:{T.TEXT_PRIMARY}; margin:0 0 8px 0;'>{html_lib.escape(r.display_name)}</h3>"
            f"<p style='margin:6px 0 12px 0;'>{tags_html}</p>"
            f"<p style='color:{T.TEXT_SECONDARY}; line-height:1.45;'>{html_lib.escape(r.jbl_explanation)}</p>"
            f"<hr style='border-color:{T.BORDER_SUBTLE};'>"
            f"<p><b style='color:{T.TEXT_MUTED}'>Espacialidade:</b> {html_lib.escape(r.spatial_label)} ({html_lib.escape(r.channels_label)})</p>"
            f"<p><b style='color:{T.TEXT_MUTED}'>Qualidade:</b> {html_lib.escape(r.quality_label)} · {html_lib.escape(r.format_label)}</p>"
            f"<p><b style='color:{T.TEXT_MUTED}'>Tamanho:</b> {html_lib.escape(r.size_label)}</p>"
            f"<p><b style='color:{T.TEXT_MUTED}'>Arquivo:</b> <code>{html_lib.escape(r.filename)}</code></p>"
            f"<p><b style='color:{T.TEXT_MUTED}'>Álbum / Pasta:</b> {html_lib.escape(r.album or '—')}</p>"
            f"<p><b style='color:{T.TEXT_MUTED}'>Disponibilidade:</b> {html_lib.escape(r.availability_label)} ({html_lib.escape(r.source)})</p>"
        )
        layout.addWidget(txt)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(dlg.accept)
        layout.addWidget(btns)
        dlg.exec()


# ══════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════

def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def run():
    _setup_logging()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("AtmosHunter")
    app.setOrganizationName("Yuri")

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
