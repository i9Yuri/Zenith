# Copyright (c) 2026 Yuri Barbosa (@i9Yuri). All Rights Reserved.
# Proprietary source-available software (personal, non-commercial use only).
# Official repository: https://github.com/i9Yuri/Zenith
# Unauthorized commercial use, redistribution, or rebranding is prohibited. See LICENSE.
"""
AtmosHunter — Aba de Importação de Playlists e Diálogo de Relatório (PySide6)
Segue estritamente os tokens Hallmark / Impeccable da janela principal.
"""
from __future__ import annotations

import html as html_lib
import logging
import subprocess
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.downloader import DOWNLOAD_DIR
from core.playlist_importer import (
    STATUS_CANCELLED,
    STATUS_DOWNGRADED_STEREO,
    STATUS_DOWNGRADED_SURROUND,
    STATUS_DOWNLOADING,
    STATUS_FAILED,
    STATUS_OPTIMAL_ATMOS,
    STATUS_PENDING,
    STATUS_SEARCHING,
    STATUS_SKIPPED,
    ParsedPlaylist,
    PlaylistImportReport,
    PlaylistImportRunner,
    PlaylistTrack,
    TrackImportReport,
    parse_playlist_input,
)
from core.ranker import format_duration
from core.slsk_service import SoulseekService

logger = logging.getLogger("AtmosHunter.playlist_ui")


# ══════════════════════════════════════════════════════════════
# DIÁLOGO DE RELATÓRIO DE INCONSISTÊNCIAS E QUALIDADE
# ══════════════════════════════════════════════════════════════

class PlaylistReportDialog(QDialog):
    retry_requested = Signal()

    def __init__(
        self,
        report: PlaylistImportReport,
        tokens,
        tag_styles: dict,
        allow_retry: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._report = report
        self._T = tokens
        self._tag_styles = tag_styles

        self.setWindowTitle(f"Relatório da Playlist — {report.playlist_name}")
        self.resize(760, 560)
        self.setStyleSheet(
            f"QDialog {{ background: {tokens.BG_CANVAS}; color: {tokens.TEXT_PRIMARY}; font-family: {tokens.FONT_UI}; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # Cabeçalho
        title = QLabel(report.playlist_name)
        title.setStyleSheet(f"font-size: 18px; font-weight: 700; color: {tokens.TEXT_PRIMARY};")
        sub = QLabel(
            f"Origem: {report.source_platform}  ·  Gerado em: {report.created_at or 'agora'}  ·  "
            f"Pasta: {report.folder_path}"
        )
        sub.setStyleSheet(f"font-size: 12px; color: {tokens.TEXT_SECONDARY};")
        sub.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(sub)

        # KPIs em linha
        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(10)
        kpis = [
            ("Total", str(report.total), tokens.TEXT_PRIMARY),
            ("Dolby Atmos (7.1.4)", str(report.count_atmos), tokens.SUCCESS),
            ("Surround 5.1/7.1", str(report.count_surround), tokens.WARNING),
            ("Estéreo HD/Ultra HD", str(report.count_stereo), tokens.ACCENT),
            ("Erros / Não enc.", str(report.count_failed), tokens.ERROR if report.count_failed else tokens.TEXT_MUTED),
        ]
        for label, val, col in kpis:
            card = QFrame()
            card.setStyleSheet(
                f"QFrame {{ background: {tokens.BG_SURFACE}; border: 1px solid {tokens.BORDER_SUBTLE}; border-radius: 10px; }}"
            )
            cl = QVBoxLayout(card)
            cl.setContentsMargins(12, 8, 12, 8)
            cl.setSpacing(2)
            v_lbl = QLabel(val)
            v_lbl.setStyleSheet(f"font-size: 18px; font-weight: 700; font-family: {tokens.FONT_MONO}; color: {col}; border: none;")
            k_lbl = QLabel(label)
            k_lbl.setStyleSheet(f"font-size: 11px; color: {tokens.TEXT_SECONDARY}; border: none;")
            cl.addWidget(v_lbl)
            cl.addWidget(k_lbl)
            kpi_row.addWidget(card)
        root.addLayout(kpi_row)

        # Conteúdo HTML detalhado
        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setStyleSheet(
            f"background: {tokens.BG_SURFACE}; color: {tokens.TEXT_PRIMARY}; "
            f"border: 1px solid {tokens.BORDER_SUBTLE}; border-radius: 10px; "
            f"font-family: {tokens.FONT_UI}; font-size: 13px; padding: 12px;"
        )
        txt.setHtml(self._build_html())
        root.addWidget(txt, stretch=1)

        # Rodapé de ações
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        btn_folder = QPushButton("Abrir Pasta da Playlist")
        btn_folder.setProperty("class", "subtle-btn")
        btn_folder.setCursor(Qt.PointingHandCursor)
        btn_folder.clicked.connect(self._open_folder)
        btn_row.addWidget(btn_folder)

        has_retryable = any(
            t.status in (STATUS_FAILED, STATUS_DOWNGRADED_SURROUND, STATUS_DOWNGRADED_STEREO, STATUS_CANCELLED)
            for t in report.tracks
        )
        if allow_retry and has_retryable:
            btn_retry = QPushButton("Tentar novamente falhas / sem Atmos")
            btn_retry.setCursor(Qt.PointingHandCursor)
            btn_retry.setStyleSheet(
                f"QPushButton {{ background: rgba(56, 189, 248, 0.14); color: {tokens.ACCENT}; "
                f"border: 1px solid rgba(56, 189, 248, 0.45); border-radius: 8px; padding: 6px 14px; font-weight: 600; }}"
                f"QPushButton:hover {{ background: {tokens.ACCENT}; color: #04111d; }}"
            )
            btn_retry.clicked.connect(self._on_retry_clicked)
            btn_row.addWidget(btn_retry)

        btn_row.addStretch()

        btn_close = QPushButton("Fechar")
        btn_close.setProperty("class", "subtle-btn")
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)

        root.addLayout(btn_row)

    def _open_folder(self):
        p = Path(self._report.folder_path)
        p.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(p)], start_new_session=True)

    def _on_retry_clicked(self):
        self.retry_requested.emit()
        self.accept()

    def _build_html(self) -> str:
        T = self._T
        r = self._report
        parts: list[str] = []

        if r.cancelled:
            parts.append(
                f"<p style='color:{T.WARNING}; font-weight:600;'>Importação interrompida antes do fim. "
                f"As faixas já concluídas permanecem salvas na pasta da playlist.</p>"
            )

        if r.parse_warnings:
            parts.append(f"<h4 style='color:{T.WARNING}; margin:6px 0;'>Avisos na leitura da playlist</h4><ul>")
            for w in r.parse_warnings:
                parts.append(f"<li style='color:{T.TEXT_SECONDARY};'>{html_lib.escape(w)}</li>")
            parts.append("</ul>")

        issues = r.issues
        if issues:
            parts.append(
                f"<h4 style='color:{T.WARNING}; margin:10px 0 6px 0;'>"
                f"Problemas, rebaixamentos e inconsistências ({len(issues)})</h4>"
            )
            for t in issues:
                col = T.ERROR if t.status == STATUS_FAILED else T.WARNING
                name = html_lib.escape(f"{t.requested_artist} - {t.requested_title}" if t.requested_artist else t.requested_title)
                var_s = f" <i>[{html_lib.escape(t.requested_variation)}]</i>" if t.requested_variation else ""
                parts.append(
                    f"<div style='margin:6px 0; padding:8px 10px; background:{T.BG_CANVAS}; "
                    f"border-left:3px solid {col}; border-radius:4px;'>"
                    f"<b style='color:{T.TEXT_PRIMARY};'>#{t.index:02d} {name}</b>{var_s} "
                    f"<span style='color:{col}; font-weight:600;'>· {html_lib.escape(t.status_label)}</span><br>"
                    f"<span style='color:{T.TEXT_SECONDARY}; font-size:12px;'>{html_lib.escape(t.issue_message)}</span>"
                    f"</div>"
                )
        else:
            parts.append(
                f"<p style='color:{T.SUCCESS}; font-weight:600; margin:8px 0;'>"
                f"Nenhuma inconsistência detectada: todas as faixas foram baixadas em Dolby Atmos 7.1.4 na variação esperada.</p>"
            )

        parts.append(f"<h4 style='color:{T.TEXT_PRIMARY}; margin:14px 0 6px 0;'>Resumo completo por faixa</h4>")
        for t in r.tracks:
            name = html_lib.escape(f"{t.requested_artist} - {t.requested_title}" if t.requested_artist else t.requested_title)
            if t.status == STATUS_OPTIMAL_ATMOS:
                col = T.SUCCESS
            elif t.status in (STATUS_DOWNGRADED_SURROUND, STATUS_DOWNGRADED_STEREO):
                col = T.WARNING
            elif t.status == STATUS_FAILED:
                col = T.ERROR
            else:
                col = T.TEXT_MUTED
            detail = html_lib.escape(t.verified_audio or t.issue_message or t.status_label)
            parts.append(
                f"<p style='margin:4px 0; font-size:12px;'>"
                f"<span style='color:{T.TEXT_MUTED}; font-family:monospace;'>#{t.index:02d}</span> "
                f"<b style='color:{T.TEXT_PRIMARY};'>{name}</b> — "
                f"<span style='color:{col}; font-weight:600;'>{html_lib.escape(t.status_label)}</span> "
                f"<span style='color:{T.TEXT_MUTED};'>({detail})</span>"
                f"</p>"
            )

        return "".join(parts)


# ══════════════════════════════════════════════════════════════
# MODELO DE TABELA DA PLAYLIST
# ══════════════════════════════════════════════════════════════

PL_COLUMNS = ["#", "Faixa e Álbum", "Variação", "Qualidade Obtida", "Estado", "Ação"]
PL_COL_IDX    = 0
PL_COL_TRACK  = 1
PL_COL_VAR    = 2
PL_COL_TAGS   = 3
PL_COL_STATUS = 4
PL_COL_ACTION = 5


class PlaylistTableModel(QAbstractTableModel):
    def __init__(self, tokens, role_title_pair: int, role_tags_list: int, parent=None):
        super().__init__(parent)
        self._T = tokens
        self._role_pair = role_title_pair
        self._role_tags = role_tags_list
        self._tracks: list[PlaylistTrack] = []
        self._reports: list[TrackImportReport] = []

    def set_playlist(self, playlist: Optional[ParsedPlaylist]):
        self.beginResetModel()
        if not playlist:
            self._tracks = []
            self._reports = []
        else:
            self._tracks = list(playlist.tracks)
            self._reports = [
                TrackImportReport(
                    index=t.index,
                    requested_artist=t.artist,
                    requested_title=t.title,
                    requested_album=t.album,
                    requested_variation=t.variation,
                    status=STATUS_PENDING if t.selected else STATUS_SKIPPED,
                    status_label="Pronta" if t.selected else "Ignorada",
                )
                for t in self._tracks
            ]
        self.endResetModel()

    def update_track_report(self, row: int, rep: TrackImportReport):
        if 0 <= row < len(self._reports):
            self._reports[row] = rep
            top_left = self.index(row, 0)
            bot_right = self.index(row, len(PL_COLUMNS) - 1)
            self.dataChanged.emit(top_left, bot_right)

    def toggle_selected(self, row: int) -> bool:
        if 0 <= row < len(self._tracks):
            t = self._tracks[row]
            t.selected = not t.selected
            self._reports[row].status = STATUS_PENDING if t.selected else STATUS_SKIPPED
            self._reports[row].status_label = "Pronta" if t.selected else "Ignorada"
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(PL_COLUMNS) - 1))
            return t.selected
        return False

    def track_at(self, row: int) -> Optional[PlaylistTrack]:
        return self._tracks[row] if 0 <= row < len(self._tracks) else None

    def report_at(self, row: int) -> Optional[TrackImportReport]:
        return self._reports[row] if 0 <= row < len(self._reports) else None

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._tracks)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(PL_COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return PL_COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        t = self._tracks[row]
        rep = self._reports[row]
        T = self._T

        if role == self._role_pair and col == PL_COL_TRACK:
            sub_parts = []
            dur = format_duration(t.duration_sec)
            if dur:
                sub_parts.append(dur)
            if t.album:
                sub_parts.append(t.album)
            if rep.saved_filename:
                sub_parts.append(rep.saved_filename)
            return (t.display_name, "  ·  ".join(sub_parts) if sub_parts else "Álbum não informado")

        if role == self._role_tags and col == PL_COL_TAGS:
            return rep.tags

        if role == Qt.DisplayRole:
            if col == PL_COL_IDX:
                return f"{t.index:02d}"
            if col == PL_COL_TRACK:
                return t.display_name
            if col == PL_COL_VAR:
                return t.variation or "Original"
            if col == PL_COL_TAGS:
                if rep.tags:
                    return " · ".join(tg.text for tg in rep.tags)
                return "—"
            if col == PL_COL_STATUS:
                return rep.status_label
            return ""

        elif role == Qt.TextAlignmentRole:
            if col in (PL_COL_IDX, PL_COL_VAR):
                return Qt.AlignCenter
            return Qt.AlignVCenter | Qt.AlignLeft

        elif role == Qt.ForegroundRole:
            if not t.selected or rep.status == STATUS_SKIPPED:
                return QBrush(QColor(T.TEXT_MUTED))
            if col == PL_COL_VAR:
                return QBrush(QColor(T.ACCENT if t.variation else T.TEXT_MUTED))
            if col == PL_COL_STATUS:
                if rep.status == STATUS_OPTIMAL_ATMOS:
                    return QBrush(QColor(T.WARNING if rep.variation_mismatch else T.SUCCESS))
                if rep.status in (STATUS_DOWNGRADED_SURROUND, STATUS_DOWNGRADED_STEREO):
                    return QBrush(QColor(T.WARNING))
                if rep.status == STATUS_FAILED:
                    return QBrush(QColor(T.ERROR))
                if rep.status in (STATUS_SEARCHING, STATUS_DOWNLOADING):
                    return QBrush(QColor(T.ACCENT))
                return QBrush(QColor(T.TEXT_SECONDARY))
            if col == PL_COL_IDX:
                return QBrush(QColor(T.TEXT_MUTED))
            return QBrush(QColor(T.TEXT_SECONDARY))

        elif role == Qt.BackgroundRole:
            return QBrush(QColor(T.BG_SURFACE if row % 2 == 0 else T.BG_ROW_ALT))

        elif role == Qt.FontRole:
            if col == PL_COL_IDX:
                f = QFont(T.FONT_MONO)
                f.setPointSize(9)
                return f
            if col == PL_COL_STATUS:
                f = QFont(T.FONT_UI)
                f.setWeight(QFont.DemiBold)
                return f
            return QFont(T.FONT_UI)

        elif role == Qt.ToolTipRole:
            lines = [f"<b>#{t.index:02d} {html_lib.escape(t.display_name)}</b>"]
            if t.album:
                lines.append(f"<br>Álbum: {html_lib.escape(t.album)}")
            if t.variation:
                lines.append(f"<br>Variação pedida: <b>{html_lib.escape(t.variation)}</b>")
            lines.append(f"<br>Estado: {html_lib.escape(rep.status_label)}")
            if rep.verified_audio:
                lines.append(f"<br>Áudio verificado: {html_lib.escape(rep.verified_audio)}")
            if rep.issue_message:
                lines.append(f"<br><span style='color:{T.WARNING}'>{html_lib.escape(rep.issue_message)}</span>")
            return "".join(lines)

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable


# ══════════════════════════════════════════════════════════════
# WIDGET PRINCIPAL DA ABA "IMPORTAR PLAYLIST"
# ══════════════════════════════════════════════════════════════

class PlaylistImportTab(QWidget):
    open_playlist_in_library = Signal(str)
    status_message = Signal(str)
    library_changed = Signal()

    _sig_parse_done = Signal(object)
    _sig_parse_error = Signal(str)
    _sig_track_update = Signal(int, object)
    _sig_overall_progress = Signal(int, int, str)
    _sig_import_done = Signal(object)

    def __init__(
        self,
        slsk_service: SoulseekService,
        tokens,
        tag_styles: dict,
        title_delegate_cls,
        tags_delegate_cls,
        role_title_pair: int,
        role_tags_list: int,
        parent=None,
    ):
        super().__init__(parent)
        self._slsk = slsk_service
        self._T = tokens
        self._tag_styles = tag_styles
        self._title_delegate_cls = title_delegate_cls
        self._tags_delegate_cls = tags_delegate_cls

        self._playlist: Optional[ParsedPlaylist] = None
        self._report: Optional[PlaylistImportReport] = None
        self._runner: Optional[PlaylistImportRunner] = None
        self._is_importing = False
        self._is_parsing = False

        self._model = PlaylistTableModel(tokens, role_title_pair, role_tags_list, self)

        self._sig_parse_done.connect(self._on_parse_done)
        self._sig_parse_error.connect(self._on_parse_error)
        self._sig_track_update.connect(self._on_track_update)
        self._sig_overall_progress.connect(self._on_overall_progress)
        self._sig_import_done.connect(self._on_import_done)

        self._build_ui()

    @property
    def is_busy(self) -> bool:
        return self._is_importing or self._is_parsing

    def cancel_active(self):
        if self._runner:
            self._runner.cancel()

    def _build_ui(self):
        T = self._T
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(10)

        # ── Barra de entrada de link da playlist ──
        url_frame = QFrame()
        url_frame.setObjectName("searchFrame")
        uf = QHBoxLayout(url_frame)
        uf.setContentsMargins(6, 5, 6, 5)
        uf.setSpacing(8)

        self.url_input = QLineEdit()
        self.url_input.setObjectName("searchBar")
        self.url_input.setPlaceholderText(
            "Cole o link da playlist (Spotify, YouTube Music, Amazon Music, Deezer, Apple Music) ou arquivo .m3u / .csv"
        )
        self.url_input.returnPressed.connect(self._on_analyze)

        self.btn_file = QPushButton("Arquivo...")
        self.btn_file.setProperty("class", "subtle-btn")
        self.btn_file.setCursor(Qt.PointingHandCursor)
        self.btn_file.setToolTip("Abrir arquivo local .m3u, .m3u8, .csv (Exportify) ou .txt")
        self.btn_file.clicked.connect(self._on_pick_file)

        self.btn_paste_mode = QPushButton("Colar lista")
        self.btn_paste_mode.setCheckable(True)
        self.btn_paste_mode.setProperty("class", "subtle-btn")
        self.btn_paste_mode.setCursor(Qt.PointingHandCursor)
        self.btn_paste_mode.setToolTip("Alternar campo para colar uma lista de músicas (1 por linha)")
        self.btn_paste_mode.toggled.connect(self._toggle_paste_box)

        self.btn_analyze = QPushButton("Analisar Playlist")
        self.btn_analyze.setObjectName("btnSearch")
        self.btn_analyze.setFixedWidth(148)
        self.btn_analyze.setCursor(Qt.PointingHandCursor)
        self.btn_analyze.clicked.connect(self._on_analyze)

        uf.addWidget(self.url_input, stretch=1)
        uf.addWidget(self.btn_file)
        uf.addWidget(self.btn_paste_mode)
        uf.addWidget(self.btn_analyze)
        lay.addWidget(url_frame)

        # ── Caixa opcional para colar lista manual / CSV ──
        self.paste_frame = QFrame()
        self.paste_frame.setStyleSheet(
            f"QFrame {{ background: {T.BG_SURFACE}; border: 1px solid {T.BORDER_SUBTLE}; border-radius: 10px; }}"
        )
        pf = QVBoxLayout(self.paste_frame)
        pf.setContentsMargins(10, 8, 10, 8)
        pf.setSpacing(6)
        lbl_p = QLabel("Cole uma faixa por linha (ex: Wham! - Last Christmas (Pudding Mix)) ou um CSV do Exportify:")
        lbl_p.setStyleSheet(f"color: {T.TEXT_SECONDARY}; font-size: 12px; border: none;")
        self.paste_edit = QPlainTextEdit()
        self.paste_edit.setFixedHeight(96)
        self.paste_edit.setPlaceholderText(
            "Don Toliver - Lose My Mind\nWham! - Last Christmas (Pudding Mix)\nThe Weeknd - Blinding Lights"
        )
        self.paste_edit.setStyleSheet(
            f"background: {T.BG_CANVAS}; color: {T.TEXT_PRIMARY}; border: 1px solid {T.BORDER_SUBTLE}; "
            f"border-radius: 7px; padding: 6px; font-family: {T.FONT_MONO}; font-size: 12px;"
        )
        pf.addWidget(lbl_p)
        pf.addWidget(self.paste_edit)
        self.paste_frame.hide()
        lay.addWidget(self.paste_frame)

        # ── Progresso de análise / importação ──
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.hide()
        lay.addWidget(self.progress_bar)

        # ── Barra de Metadados e Controles da Playlist Analisada ──
        self.meta_bar = QFrame()
        self.meta_bar.setStyleSheet(
            f"QFrame {{ background: {T.BG_SURFACE}; border: 1px solid {T.BORDER_SUBTLE}; border-radius: 10px; }}"
        )
        mb = QHBoxLayout(self.meta_bar)
        mb.setContentsMargins(12, 8, 12, 8)
        mb.setSpacing(10)

        lbl_nome = QLabel("Playlist:")
        lbl_nome.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 12px; border: none;")
        self.name_input = QLineEdit()
        self.name_input.setFixedWidth(230)
        self.name_input.setStyleSheet(
            f"background: {T.BG_CANVAS}; color: {T.TEXT_PRIMARY}; border: 1px solid {T.BORDER_SUBTLE}; "
            f"border-radius: 6px; padding: 5px 8px; font-weight: 600; font-size: 13px;"
        )
        self.name_input.setToolTip(f"Nome da pasta onde esta playlist será organizada em {DOWNLOAD_DIR}")

        self.lbl_pl_stats = QLabel("")
        self.lbl_pl_stats.setStyleSheet(f"color: {T.TEXT_SECONDARY}; font-size: 12px; border: none;")

        self.btn_start_import = QPushButton("Baixar Playlist (Melhor Qualidade)")
        self.btn_start_import.setObjectName("btnSearch")
        self.btn_start_import.setCursor(Qt.PointingHandCursor)
        self.btn_start_import.clicked.connect(self._on_start_import)

        self.btn_cancel_import = QPushButton("Cancelar")
        self.btn_cancel_import.setProperty("class", "subtle-btn")
        self.btn_cancel_import.setCursor(Qt.PointingHandCursor)
        self.btn_cancel_import.clicked.connect(self._on_cancel_import)
        self.btn_cancel_import.hide()

        self.btn_report = QPushButton("Ver Relatório")
        self.btn_report.setProperty("class", "subtle-btn")
        self.btn_report.setCursor(Qt.PointingHandCursor)
        self.btn_report.clicked.connect(self._on_show_report)
        self.btn_report.hide()

        self.btn_open_lib = QPushButton("Abrir Playlist")
        self.btn_open_lib.setCursor(Qt.PointingHandCursor)
        self.btn_open_lib.setStyleSheet(
            f"QPushButton {{ background: rgba(52, 211, 153, 0.14); color: {T.SUCCESS}; "
            f"border: 1px solid rgba(52, 211, 153, 0.45); border-radius: 8px; padding: 6px 12px; font-weight: 600; font-size: 12px; }}"
            f"QPushButton:hover {{ background: {T.SUCCESS}; color: #04111d; }}"
        )
        self.btn_open_lib.clicked.connect(self._on_open_in_library)
        self.btn_open_lib.hide()

        mb.addWidget(lbl_nome)
        mb.addWidget(self.name_input)
        mb.addWidget(self.lbl_pl_stats, stretch=1)
        mb.addWidget(self.btn_report)
        mb.addWidget(self.btn_open_lib)
        mb.addWidget(self.btn_cancel_import)
        mb.addWidget(self.btn_start_import)
        self.meta_bar.hide()
        lay.addWidget(self.meta_bar)

        # ── Stack: Empty State / Tabela da Playlist ──
        self.stack = QStackedWidget()

        empty_card = QFrame()
        empty_card.setStyleSheet(
            f"QFrame {{ background: {T.BG_SURFACE}; border: 1px solid {T.BORDER_SUBTLE}; border-radius: 12px; }}"
        )
        ec = QVBoxLayout(empty_card)
        ec.setAlignment(Qt.AlignCenter)
        ec.setSpacing(10)

        self.lbl_empty_title = QLabel("Importe uma playlist inteira em Dolby Atmos 7.1.4 e Ultra HD")
        self.lbl_empty_title.setAlignment(Qt.AlignCenter)
        self.lbl_empty_title.setStyleSheet(
            f"font-size: 16px; font-weight: 600; color: {T.TEXT_PRIMARY}; border: none; background: transparent;"
        )
        self.lbl_empty_desc = QLabel(
            "Cole o link público do Spotify, YouTube Music, Amazon Music, Deezer ou Apple Music.\n"
            "O AtmosHunter identifica artista, álbum e variação exata (Live, Acoustic, Remix, Pudding Mix),\n"
            "baixa cada faixa na melhor espacialidade disponível e gera um relatório completo de inconsistências."
        )
        self.lbl_empty_desc.setAlignment(Qt.AlignCenter)
        self.lbl_empty_desc.setStyleSheet(
            f"font-size: 13px; color: {T.TEXT_SECONDARY}; border: none; background: transparent;"
        )
        lbl_plat = QLabel("Spotify  ·  YouTube Music  ·  Amazon Music  ·  Deezer  ·  Apple Music  ·  M3U / CSV / Texto")
        lbl_plat.setAlignment(Qt.AlignCenter)
        lbl_plat.setStyleSheet(
            f"font-size: 11px; font-family: {T.FONT_MONO}; color: {T.ACCENT}; border: none; background: transparent; margin-top: 4px;"
        )
        ec.addWidget(self.lbl_empty_title)
        ec.addWidget(self.lbl_empty_desc)
        ec.addWidget(lbl_plat)

        self.table = QTableView()
        self.table.setModel(self._model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)

        self.table.setItemDelegateForColumn(PL_COL_TRACK, self._title_delegate_cls(self.table))
        self.table.setItemDelegateForColumn(PL_COL_TAGS, self._tags_delegate_cls(self.table))

        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(PL_COL_IDX,    QHeaderView.Fixed)
        hdr.setSectionResizeMode(PL_COL_TRACK,  QHeaderView.Stretch)
        hdr.setSectionResizeMode(PL_COL_VAR,    QHeaderView.Fixed)
        hdr.setSectionResizeMode(PL_COL_TAGS,   QHeaderView.Fixed)
        hdr.setSectionResizeMode(PL_COL_STATUS, QHeaderView.Fixed)
        hdr.setSectionResizeMode(PL_COL_ACTION, QHeaderView.Fixed)
        self.table.setColumnWidth(PL_COL_IDX,    46)
        self.table.setColumnWidth(PL_COL_VAR,    132)
        self.table.setColumnWidth(PL_COL_TAGS,   385)
        self.table.setColumnWidth(PL_COL_STATUS, 172)
        self.table.setColumnWidth(PL_COL_ACTION, 88)
        self.table.verticalHeader().setDefaultSectionSize(52)

        self.stack.addWidget(empty_card)
        self.stack.addWidget(self.table)
        self.stack.setCurrentIndex(0)
        lay.addWidget(self.stack, stretch=1)

    # ── AÇÕES DE LEITURA E PARSE ─────────────────────────────

    def _toggle_paste_box(self, checked: bool):
        self.paste_frame.setVisible(checked)
        if checked:
            self.paste_edit.setFocus()

    def _on_pick_file(self):
        fp, _ = QFileDialog.getOpenFileName(
            self,
            "Selecionar arquivo de playlist",
            str(Path.home()),
            "Playlists (*.m3u *.m3u8 *.csv *.txt);;Todos os arquivos (*)",
        )
        if fp:
            self.btn_paste_mode.setChecked(False)
            self.url_input.setText(fp)
            self._on_analyze()

    def _on_analyze(self):
        if self._is_importing or self._is_parsing:
            return

        if self.btn_paste_mode.isChecked() and self.paste_edit.toPlainText().strip():
            raw = self.paste_edit.toPlainText().strip()
        else:
            raw = self.url_input.text().strip()
            if not raw and self.paste_edit.toPlainText().strip():
                raw = self.paste_edit.toPlainText().strip()

        if not raw:
            self.status_message.emit("Cole o link da playlist ou ative 'Colar lista' para inserir músicas.")
            self.url_input.setFocus()
            return

        self._is_parsing = True
        self.btn_analyze.setEnabled(False)
        self.btn_analyze.setText("Analisando...")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self.status_message.emit("Analisando playlist e extraindo variações, artistas e álbuns...")

        def _work():
            try:
                pl = parse_playlist_input(raw)
                self._sig_parse_done.emit(pl)
            except Exception as e:
                self._sig_parse_error.emit(str(e))

        threading.Thread(target=_work, daemon=True, name="pl-parse").start()

    def _on_parse_done(self, pl: ParsedPlaylist):
        self._is_parsing = False
        self.btn_analyze.setEnabled(True)
        self.btn_analyze.setText("Analisar Playlist")
        self.progress_bar.hide()

        self._playlist = pl
        self._report = None
        self._model.set_playlist(pl)
        self.name_input.setText(pl.name)
        var_count = sum(1 for t in pl.tracks if t.variation)
        warn_s = f" · {len(pl.warnings)} aviso(s)" if pl.warnings else ""
        self.lbl_pl_stats.setText(
            f"{pl.source_platform} · {len(pl.tracks)} faixas ({var_count} com variação específica){warn_s}"
        )
        self.meta_bar.show()
        self.btn_start_import.show()
        self.btn_start_import.setEnabled(True)
        self.btn_start_import.setText("Baixar Playlist (Melhor Qualidade)")
        self.btn_cancel_import.hide()
        self.btn_report.hide()
        self.btn_open_lib.hide()
        self.stack.setCurrentIndex(1)
        self._sync_row_buttons()

        msg = f"Playlist '{pl.name}' analisada ({len(pl.tracks)} faixas de {pl.source_platform})."
        if pl.warnings:
            msg += f" Aviso: {pl.warnings[0]}"
        self.status_message.emit(msg)

    def _on_parse_error(self, err: str):
        self._is_parsing = False
        self.btn_analyze.setEnabled(True)
        self.btn_analyze.setText("Analisar Playlist")
        self.progress_bar.hide()
        self.status_message.emit(f"Não foi possível ler a playlist: {err}")
        QMessageBox.warning(self, "Erro ao analisar playlist", err)

    # ── BOTÕES POR LINHA ─────────────────────────────────────

    def _sync_row_buttons(self):
        T = self._T
        for row in range(self._model.rowCount()):
            t = self._model.track_at(row)
            rep = self._model.report_at(row)
            if not t or not rep:
                continue

            btn = QPushButton()
            btn.setFixedHeight(30)
            btn.setCursor(Qt.PointingHandCursor)

            if self._is_importing:
                btn.setText("...")
                btn.setEnabled(False)
                btn.setStyleSheet(
                    f"QPushButton {{ background: {T.BG_ELEVATED}; color: {T.TEXT_MUTED}; border: none; border-radius: 6px; font-size: 11px; }}"
                )
            elif self._report is not None and rep.status not in (STATUS_PENDING, STATUS_SKIPPED):
                if rep.has_issue:
                    btn.setText("Alerta")
                    btn.setStyleSheet(
                        f"QPushButton {{ background: rgba(251, 191, 36, 0.14); color: {T.WARNING}; "
                        f"border: 1px solid rgba(251, 191, 36, 0.4); border-radius: 6px; font-size: 11px; font-weight: 600; }}"
                        f"QPushButton:hover {{ background: {T.WARNING}; color: #04111d; }}"
                    )
                    btn.clicked.connect(lambda _, r=rep: self._show_track_issue(r))
                else:
                    btn.setText("OK")
                    btn.setEnabled(False)
                    btn.setStyleSheet(
                        f"QPushButton {{ background: rgba(52, 211, 153, 0.12); color: {T.SUCCESS}; "
                        f"border: 1px solid rgba(52, 211, 153, 0.35); border-radius: 6px; font-size: 11px; font-weight: 600; }}"
                    )
            else:
                if t.selected:
                    btn.setText("Remover")
                    btn.setStyleSheet(
                        f"QPushButton {{ background: {T.BG_ELEVATED}; color: {T.TEXT_SECONDARY}; "
                        f"border: 1px solid {T.BORDER_SUBTLE}; border-radius: 6px; font-size: 11px; }}"
                        f"QPushButton:hover {{ color: {T.ERROR}; border-color: rgba(248, 113, 113, 0.45); }}"
                    )
                else:
                    btn.setText("Incluir")
                    btn.setStyleSheet(
                        f"QPushButton {{ background: rgba(56, 189, 248, 0.12); color: {T.ACCENT}; "
                        f"border: 1px solid rgba(56, 189, 248, 0.4); border-radius: 6px; font-size: 11px; font-weight: 600; }}"
                    )
                btn.clicked.connect(lambda _, r=row: self._on_toggle_track(r))

            wrap = QWidget()
            wrap.setStyleSheet("background: transparent;")
            hl = QHBoxLayout(wrap)
            hl.setContentsMargins(6, 9, 6, 9)
            hl.addWidget(btn)
            idx = self._model.index(row, PL_COL_ACTION)
            old_w = self.table.indexWidget(idx)
            if old_w is not None:
                old_w.hide()
                old_w.setParent(None)
                old_w.deleteLater()
            self.table.setIndexWidget(idx, wrap)

    def _on_toggle_track(self, row: int):
        if self._is_importing or not self._playlist:
            return
        self._model.toggle_selected(row)
        sel_n = sum(1 for t in self._playlist.tracks if t.selected)
        var_count = sum(1 for t in self._playlist.tracks if t.selected and t.variation)
        self.lbl_pl_stats.setText(
            f"{self._playlist.source_platform} · {sel_n} de {len(self._playlist.tracks)} selecionadas ({var_count} com variação)"
        )
        self.btn_start_import.setEnabled(sel_n > 0)
        self._sync_row_buttons()

    def load_report_for_retry(self, report: PlaylistImportReport):
        """Carrega uma playlist já salva (vinda da Biblioteca) e dispara retry das faixas com falha/sem Atmos."""
        if self._is_importing or self._is_parsing:
            return
        tracks = [
            PlaylistTrack(
                index=t.index,
                title=t.requested_title,
                artist=t.requested_artist,
                album=t.requested_album,
                variation=t.requested_variation,
                selected=(t.status != STATUS_SKIPPED),
            )
            for t in report.tracks
        ]
        pl = ParsedPlaylist(
            name=report.playlist_name,
            source_platform=report.source_platform,
            source_url=report.source_url,
            tracks=tracks,
            warnings=list(report.parse_warnings),
        )
        self._playlist = pl
        self._report = report
        self._model.set_playlist(pl)
        for pos, tr in enumerate(report.tracks):
            self._model.update_track_report(pos, tr)
        self.name_input.setText(pl.name)
        self.url_input.setText(pl.source_url)
        self.meta_bar.show()
        self.stack.setCurrentIndex(1)
        self._sync_row_buttons()
        self._on_retry_issues()

    def _show_track_issue(self, rep: TrackImportReport):
        name = f"{rep.requested_artist} - {rep.requested_title}" if rep.requested_artist else rep.requested_title
        QMessageBox.information(
            self,
            f"Detalhes — #{rep.index:02d} {name}",
            f"Estado: {rep.status_label}\n"
            f"Áudio verificado: {rep.verified_audio or '—'}\n\n"
            f"{rep.issue_message}",
        )

    # ── IMPORTAÇÃO EM LOTE ───────────────────────────────────

    def _on_start_import(self, only_indices: Optional[set[int]] = None):
        if not self._playlist or self._is_importing:
            return
        custom_name = self.name_input.text().strip()
        if custom_name:
            self._playlist.name = custom_name

        selected = [t for t in self._playlist.tracks if t.selected]
        if not selected:
            self.status_message.emit("Selecione pelo menos uma faixa para baixar.")
            return

        self._is_importing = True
        self.btn_analyze.setEnabled(False)
        self.name_input.setEnabled(False)
        self.btn_start_import.setEnabled(False)
        self.btn_start_import.setText("Importando...")
        self.btn_cancel_import.show()
        self.btn_report.hide()
        self.btn_open_lib.hide()
        self.progress_bar.setRange(0, len(only_indices) if only_indices else len(selected))
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self._sync_row_buttons()

        runner = PlaylistImportRunner(
            playlist=self._playlist,
            dest_root=DOWNLOAD_DIR,
            slsk_service=self._slsk,
            on_track_update=lambda pos, rep: self._sig_track_update.emit(pos, rep),
            on_overall_progress=lambda done, tot, msg: self._sig_overall_progress.emit(done, tot, msg),
            on_finished=lambda report: self._sig_import_done.emit(report),
            only_indices=only_indices,
            existing_report=self._report if only_indices else None,
        )
        self._runner = runner
        threading.Thread(target=runner.run, daemon=True, name="pl-import").start()

    def _on_cancel_import(self):
        if self._runner:
            self._runner.cancel()
            self.btn_cancel_import.setEnabled(False)
            self.status_message.emit("Cancelando importação da playlist...")

    def _on_track_update(self, pos: int, rep: TrackImportReport):
        self._model.update_track_report(pos, rep)
        self.table.scrollTo(self._model.index(pos, 0))

    def _on_overall_progress(self, done: int, total: int, msg: str):
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(min(done, total))
        self.lbl_pl_stats.setText(msg)
        self.status_message.emit(msg)

    def _on_import_done(self, report: PlaylistImportReport):
        self._is_importing = False
        self._runner = None
        self._report = report

        self.btn_analyze.setEnabled(True)
        self.name_input.setEnabled(True)
        self.btn_cancel_import.hide()
        self.btn_cancel_import.setEnabled(True)
        self.btn_start_import.setEnabled(True)
        self.btn_start_import.setText("Baixar Novamente")
        self.progress_bar.hide()

        issues_n = len(report.issues)
        self.btn_report.setText(f"Ver Relatório ({issues_n} alerta{'s' if issues_n != 1 else ''})" if issues_n else "Ver Relatório (100% OK)")
        self.btn_report.show()
        self.btn_open_lib.show()

        self.lbl_pl_stats.setText(
            f"Concluído: {report.count_downloaded}/{report.total} faixas · "
            f"{report.count_atmos} Atmos 3D · {report.count_surround} Surround · "
            f"{report.count_stereo} Estéreo · {report.count_failed} erro(s)"
        )
        self._sync_row_buttons()
        self.library_changed.emit()

        # Abrir relatório automaticamente se houver problemas/rebaixamentos para transparência imediata
        if issues_n > 0 and not report.cancelled:
            self._on_show_report()

    def _on_show_report(self):
        if not self._report:
            return
        dlg = PlaylistReportDialog(
            report=self._report,
            tokens=self._T,
            tag_styles=self._tag_styles,
            allow_retry=not self._is_importing,
            parent=self,
        )
        dlg.retry_requested.connect(self._on_retry_issues)
        dlg.exec()

    def _on_retry_issues(self):
        if not self._report or self._is_importing:
            return
        retry_ids = {
            t.index for t in self._report.tracks
            if t.status in (STATUS_FAILED, STATUS_DOWNGRADED_SURROUND, STATUS_DOWNGRADED_STEREO, STATUS_CANCELLED)
        }
        if retry_ids:
            self._on_start_import(only_indices=retry_ids)

    def _on_open_in_library(self):
        if self._report:
            folder_name = Path(self._report.folder_path).name
            self.open_playlist_in_library.emit(folder_name)
