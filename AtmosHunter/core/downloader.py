# Copyright (c) 2026 Yuri Barbosa (@i9Yuri). All Rights Reserved.
# Proprietary source-available software (personal, non-commercial use only).
# Official repository: https://github.com/i9Yuri/Zenith
# Unauthorized commercial use, redistribution, or rebranding is prohibited. See LICENSE.
"""
AtmosHunter — Motor de Download Direto de Faixa Única (com Fila Sequencial)
- Baixa arquivos individuais direto para ~/Music/DolbyAtmos/
- Fila sequencial: clicar em Baixar em várias músicas enfileira sem cancelar o download atual
- Empacotamento rápido (<2s) sem perda de áudio (-c:a copy) para .mp4 (E-AC-3 JOC) / .mkv (TrueHD)
- Verificação real com ffprobe ao finalizar cada faixa
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Optional

import requests
from aioslsk.transfer.state import TransferState

from core.audio_verify import (
    VerifiedAudio,
    classify_probed_audio,
    inspect_mp4_prefix,
    probe_audio_full,
    record_verified_track,
)
from core.ranker import TrackResult, format_size, format_speed
from core.slsk_service import STAGING_DIR, SoulseekService, get_slsk_service

logger = logging.getLogger("AtmosHunter.download")

DOWNLOAD_DIR = Path.home() / "Music" / "DolbyAtmos"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}


# ──────────────────────────────────────────────────────────────
# ESTADOS E MODELO DO JOB
# ──────────────────────────────────────────────────────────────

class DownloadStatus(Enum):
    PENDING = auto()
    QUEUED = auto()
    STARTING = auto()
    DOWNLOADING = auto()
    VERIFYING = auto()
    DONE = auto()
    ERROR = auto()
    CANCELLED = auto()


@dataclass
class DownloadJob:
    track: TrackResult
    dest_dir: Path = DOWNLOAD_DIR
    filename_prefix: str = ""
    fallback_tracks: list[TrackResult] = field(default_factory=list)
    actual_track: Optional[TrackResult] = None
    status: DownloadStatus = DownloadStatus.PENDING
    progress: float = 0.0           # 0.0 -> 1.0
    bytes_done: int = 0
    bytes_total: int = 0
    speed_label: str = ""
    status_detail: str = ""
    saved_path: Optional[Path] = None
    verified_audio: str = ""
    verified_tier: int = 0
    verification_warning: str = ""
    error_msg: str = ""
    queue_remaining: int = 0
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _on_peer_verified: Optional[Callable[[TrackResult, VerifiedAudio], None]] = field(default=None, repr=False)

    @property
    def title(self) -> str:
        return self.track.display_name

    def cancel(self):
        self._cancel_event.set()


# ──────────────────────────────────────────────────────────────
# NOMES LIMPOS E ANTI-SOBRESCRITA
# ──────────────────────────────────────────────────────────────

def _sanitize_filename(name: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    return clean[:140] or "Faixa_Atmos"


def _has_stem_collision(directory: Path, stem: str, ext: str) -> bool:
    """Evita colisão tanto na extensão original quanto nas extensões de remux (.mp4 / .mkv)."""
    check_exts = {ext.lower(), ".mp4", ".mkv"} if ext.lower() in (".m4a", ".mka", ".ec3", ".eac3", ".ac3", ".mlp", ".mp4", ".mkv") else {ext.lower()}
    return any((directory / f"{stem}{e}").exists() for e in check_exts)


def _build_target_path(
    track: TrackResult,
    ext_override: str = "",
    directory: Path = DOWNLOAD_DIR,
    filename_prefix: str = "",
    tag_override: Optional[str] = None,
) -> Path:
    ext = ext_override or track.extension or ".m4a"
    if not ext.startswith("."):
        ext = "." + ext

    base = track.display_name
    if tag_override is not None:
        tag = tag_override
    else:
        tag = ""
        if track.codec_tier == 1:
            tag = " [Dolby Atmos TrueHD]"
        elif track.codec_tier == 2:
            tag = " [Dolby Atmos]"
        elif track.codec_tier == 3:
            tag = " [Surround 5.1]"

    full_name = f"{base}{tag}" if (tag and tag.strip(" []").lower() not in base.lower()) else base
    if filename_prefix:
        full_name = f"{filename_prefix}{full_name}"
    safe = _sanitize_filename(full_name)

    if not _has_stem_collision(directory, safe, ext):
        return directory / f"{safe}{ext}"

    for idx in range(2, 100):
        cand_stem = f"{safe} ({idx})"
        if not _has_stem_collision(directory, cand_stem, ext):
            return directory / f"{cand_stem}{ext}"
    return directory / f"{safe}_{int(time.time())}{ext}"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for idx in range(2, 100):
        cand = path.with_name(f"{path.stem} ({idx}){path.suffix}")
        if not cand.exists():
            return cand
    return path.with_name(f"{path.stem}_{int(time.time())}{path.suffix}")


# ──────────────────────────────────────────────────────────────
# FFPROBE E REMUX RÁPIDO SEM PERDA PARA TV SAMSUNG / JBL
# ──────────────────────────────────────────────────────────────

def _probe_stream_info(path: Path) -> dict:
    if not shutil.which("ffprobe"):
        return {}
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name,profile,channels,channel_layout,sample_rate,bit_rate,bits_per_raw_sample",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        return streams[0] if streams else {}
    except Exception as e:
        logger.debug(f"ffprobe falhou: {e}")
        return {}


def probe_audio_file(path: Path) -> str:
    info = probe_audio_full(path)
    if not info:
        return ""
    return classify_probed_audio(info, ext=path.suffix.lower()).summary


def remux_atmos_for_tv_soundbar(src_path: Path, track: TrackResult, work_dir: Optional[Path] = None) -> Path:
    """
    Empacota faixas Dolby Atmos (E-AC-3 JOC / TrueHD) sem re-encodar o áudio (-c:a copy)
    em um container de vídeo leve e rápido (1280x720 pré-escalado 1x, 1fps, yuv420p, faststart).
    - E-AC-3 JOC -> .mp4 (DLNA AVC_MP4_HP_HD_EAC3)
    - TrueHD     -> .mkv (DLNA video/x-mkv)
    FLAC permanece intocado.
    Todos os arquivos temporários (.raw_cover.jpg, .scaled_cover.png) são criados em `work_dir`
    (fora de ~/Music/DolbyAtmos) para NUNCA corromper o banco inotify do Rygel!
    """
    if not shutil.which("ffmpeg"):
        return src_path

    info = _probe_stream_info(src_path)
    codec_name = str(info.get("codec_name") or "").lower()
    if codec_name not in ("eac3", "truehd", "ac3"):
        return src_path

    out_ext = ".mkv" if codec_name == "truehd" else ".mp4"
    desired = src_path.with_suffix(out_ext)
    if desired == src_path:
        desired = src_path.with_name(src_path.stem + "_atmos" + out_ext)
    out_path = _unique_path(desired)

    tmp_dir = work_dir or STAGING_DIR
    tmp_dir.mkdir(parents=True, exist_ok=True)
    nonce = f"{os.getpid()}_{int(time.time() * 1000)}"
    raw_cover = tmp_dir / f".raw_cover_{nonce}.jpg"
    scaled_cover = tmp_dir / f".scaled_cover_{nonce}.png"
    has_scaled_cover = False

    try:
        # 1. Extrair capa original e pré-redimensionar 1 única vez para 1280x720 (acelera ffmpeg em ~8x!)
        cproc = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(src_path), "-an", "-vcodec", "copy", str(raw_cover)],
            capture_output=True,
            timeout=10,
        )
        got_raw = cproc.returncode == 0 and raw_cover.exists() and raw_cover.stat().st_size > 1024
        if not got_raw and track.artwork_url:
            try:
                img = requests.get(track.artwork_url, headers=HTTP_HEADERS, timeout=6)
                if img.status_code == 200 and len(img.content) > 1024:
                    raw_cover.write_bytes(img.content)
                    got_raw = True
            except Exception:
                pass

        if got_raw:
            sproc = subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-i", str(raw_cover),
                    "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=#090d16,setsar=1",
                    "-frames:v", "1",
                    str(scaled_cover),
                ],
                capture_output=True,
                timeout=10,
            )
            has_scaled_cover = sproc.returncode == 0 and scaled_cover.exists() and scaled_cover.stat().st_size > 1024

        # 2. Mux ultra-rápido (-c:a copy = 0% perda no áudio Atmos)
        cmd = ["ffmpeg", "-y", "-v", "error"]
        if has_scaled_cover:
            cmd += ["-loop", "1", "-framerate", "1", "-i", str(scaled_cover)]
        else:
            cmd += ["-f", "lavfi", "-i", "color=c=#090d16:s=1280x720:r=1"]

        cmd += [
            "-i", str(src_path),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "stillimage",
            "-profile:v", "main",
            "-level", "3.1",
            "-pix_fmt", "yuv420p",
            "-color_range", "tv",
            "-r", "1",
            "-g", "60",
            "-crf", "27",
            "-c:a", "copy",
            "-strict", "unofficial",
            "-shortest",
        ]
        if track.title:
            cmd += ["-metadata", f"title={track.display_name}"]
        if track.artist:
            cmd += ["-metadata", f"artist={track.artist}"]
        if track.album:
            cmd += ["-metadata", f"album={track.album}"]
        if out_ext == ".mp4":
            cmd += ["-movflags", "+faststart"]

        cmd.append(str(out_path))

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode == 0 and out_path.exists() and out_path.stat().st_size > 512 * 1024:
            check = _probe_stream_info(out_path)
            if str(check.get("codec_name") or "").lower() == codec_name:
                src_path.unlink(missing_ok=True)
                logger.info(f"Remux TV/Soundbar concluído: {out_path.name}")
                return out_path

        logger.warning(f"Remux Atmos falhou (mantendo original): {res.stderr[:200]}")
        out_path.unlink(missing_ok=True)
        return src_path
    except Exception as e:
        logger.warning(f"Erro no remux Atmos (mantendo original): {e}")
        out_path.unlink(missing_ok=True)
        return src_path
    finally:
        raw_cover.unlink(missing_ok=True)
        scaled_cover.unlink(missing_ok=True)


def _embed_flac_metadata(path: Path, track: TrackResult):
    if path.suffix.lower() != ".flac":
        return
    try:
        from mutagen.flac import FLAC, Picture

        audio = FLAC(str(path))
        if track.title and not audio.get("title"):
            audio["title"] = track.title
        if track.artist and not audio.get("artist"):
            audio["artist"] = track.artist
        if track.album and not audio.get("album"):
            audio["album"] = track.album

        if track.artwork_url and not audio.pictures:
            try:
                img = requests.get(track.artwork_url, headers=HTTP_HEADERS, timeout=6)
                if img.status_code == 200 and len(img.content) > 1000:
                    pic = Picture()
                    pic.type = 3
                    pic.mime = img.headers.get("Content-Type", "image/jpeg").split(";")[0]
                    pic.data = img.content
                    audio.add_picture(pic)
            except Exception:
                pass
        audio.save()
    except Exception as e:
        logger.debug(f"Tagging FLAC ignorado: {e}")


# ──────────────────────────────────────────────────────────────
# DOWNLOAD MANAGER COM FILA SEQUENCIAL
# ──────────────────────────────────────────────────────────────

class DownloadManager:
    """Gerencia fila sequencial de downloads diretos (Soulseek + HTTP Hi-Res)."""

    def __init__(self, slsk_service: Optional[SoulseekService] = None, dest_dir: Path = DOWNLOAD_DIR):
        self.slsk = slsk_service or get_slsk_service()
        self.dest_dir = dest_dir
        self._lock = threading.Lock()
        self._queue: deque[tuple[DownloadJob, Callable[[DownloadJob], None]]] = deque()
        self._current_job: Optional[DownloadJob] = None
        self._worker_running = False

    @property
    def active_job(self) -> Optional[DownloadJob]:
        with self._lock:
            return self._current_job

    @property
    def queued_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def cancel_current(self):
        with self._lock:
            job = self._current_job
        if job:
            job.cancel()

    def cancel_all(self):
        with self._lock:
            job = self._current_job
            pending = list(self._queue)
            self._queue.clear()
        if job:
            job.cancel()
        for j, cb in pending:
            j.status = DownloadStatus.CANCELLED
            j.status_detail = "Cancelado."
            try:
                cb(j)
            except Exception:
                pass

    def start_download(
        self,
        track: TrackResult,
        status_cb: Callable[[DownloadJob], None],
        fallback_tracks: Optional[list[TrackResult]] = None,
        on_peer_verified: Optional[Callable[[TrackResult, VerifiedAudio], None]] = None,
    ) -> DownloadJob:
        self.dest_dir.mkdir(parents=True, exist_ok=True)
        job = DownloadJob(
            track=track,
            dest_dir=self.dest_dir,
            fallback_tracks=list(fallback_tracks or []),
            bytes_total=track.size_bytes,
            status=DownloadStatus.QUEUED,
            status_detail="Na fila de downloads...",
            _on_peer_verified=on_peer_verified,
        )
        start_worker = False
        with self._lock:
            self._queue.append((job, status_cb))
            job.queue_remaining = len(self._queue) - (0 if self._worker_running else 1)
            if not self._worker_running:
                self._worker_running = True
                start_worker = True

        status_cb(job)
        if start_worker:
            threading.Thread(target=self._process_queue, daemon=True, name="dl-queue").start()
        return job

    def _process_queue(self):
        while True:
            with self._lock:
                if not self._queue:
                    self._current_job = None
                    self._worker_running = False
                    return
                job, cb = self._queue.popleft()
                self._current_job = job
                rem = len(self._queue)

            job.queue_remaining = rem
            if not job._cancel_event.is_set():
                self._run_job(job, cb)

    def _emit(self, job: DownloadJob, cb: Callable[[DownloadJob], None]):
        with self._lock:
            job.queue_remaining = len(self._queue)
        cb(job)

    def download_track_sync(
        self,
        track: TrackResult,
        dest_dir: Optional[Path] = None,
        filename_prefix: str = "",
        status_cb: Optional[Callable[[DownloadJob], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> DownloadJob:
        """Executa download síncrono reutilizando o mesmo pipeline (HTTP/Soulseek + remux + ffprobe)."""
        target_dir = dest_dir or self.dest_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        job = DownloadJob(
            track=track,
            dest_dir=target_dir,
            filename_prefix=filename_prefix,
            bytes_total=track.size_bytes,
            status=DownloadStatus.STARTING,
            status_detail="Iniciando download...",
            _cancel_event=cancel_event or threading.Event(),
        )
        with self._lock:
            self._current_job = job
        cb = status_cb or (lambda _j: None)
        try:
            self._run_job(job, cb)
        finally:
            with self._lock:
                if self._current_job is job:
                    self._current_job = None
        return job

    def _run_job(self, job: DownloadJob, status_cb: Callable[[DownloadJob], None]):
        job.status = DownloadStatus.STARTING
        job.status_detail = "Iniciando download..."
        self._emit(job, status_cb)
        try:
            if job.track.provider == "http":
                self._download_http(job, status_cb)
            elif job.track.provider == "soulseek":
                self._download_soulseek(job, status_cb)
            else:
                job.status = DownloadStatus.ERROR
                job.error_msg = "Provedor de download desconhecido."
                self._emit(job, status_cb)
        except Exception as e:
            logger.exception("Erro inesperado durante download")
            job.status = DownloadStatus.ERROR
            job.error_msg = str(e)
            self._emit(job, status_cb)

    def _download_http(self, job: DownloadJob, status_cb: Callable[[DownloadJob], None]):
        url = job.track.download_url
        if not url:
            job.status = DownloadStatus.ERROR
            job.error_msg = "URL de download indisponível."
            self._emit(job, status_cb)
            return

        target = _build_target_path(
            job.track,
            job.track.extension or ".flac",
            directory=job.dest_dir,
            filename_prefix=job.filename_prefix,
        )
        # Baixar o arquivo .part em STAGING_DIR (fora da pasta monitorada pelo Rygel!)
        # para evitar que o Rygel indexe arquivos incompletos ou temporários
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        part_path = STAGING_DIR / f"{target.name}.{int(time.time() * 1000)}.part"

        try:
            with requests.get(url, headers=HTTP_HEADERS, stream=True, timeout=20) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length") or job.track.size_bytes or 0)
                job.bytes_total = total
                job.status = DownloadStatus.DOWNLOADING
                self._emit(job, status_cb)

                done = 0
                t0 = time.monotonic()
                last_emit = 0.0

                with open(part_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=128 * 1024):
                        if job._cancel_event.is_set():
                            part_path.unlink(missing_ok=True)
                            job.status = DownloadStatus.CANCELLED
                            job.status_detail = "Download cancelado."
                            self._emit(job, status_cb)
                            return
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        now = time.monotonic()
                        if now - last_emit >= 0.15 or (total and done >= total):
                            elapsed = max(now - t0, 0.05)
                            job.bytes_done = done
                            job.progress = min(0.99, done / total) if total else 0.5
                            job.speed_label = format_speed(done / elapsed)
                            job.status_detail = f"{format_size(done)} / {format_size(total)}" if total else format_size(done)
                            self._emit(job, status_cb)
                            last_emit = now

            shutil.move(str(part_path), str(target))
            job.status = DownloadStatus.VERIFYING
            job.progress = 0.99
            job.status_detail = "Verificando áudio..."
            self._emit(job, status_cb)

            _embed_flac_metadata(target, job.track)
            job.verified_audio = probe_audio_file(target) or job.track.codec_label
            job.saved_path = target
            job.progress = 1.0
            job.status = DownloadStatus.DONE
            job.speed_label = ""
            job.status_detail = f"Pronto! Disponível na Soundbar ({job.verified_audio})"
            self._emit(job, status_cb)

        except Exception as e:
            part_path.unlink(missing_ok=True)
            job.status = DownloadStatus.ERROR
            job.error_msg = f"Falha no download direto: {e}"
            self._emit(job, status_cb)

    def _download_single_soulseek_candidate(
        self,
        cand: TrackResult,
        job: DownloadJob,
        status_cb: Callable[[DownloadJob], None],
        attempt_idx: int,
        total_attempts: int,
        allow_early_abort: bool,
    ) -> tuple[str, Optional[Path], Optional[dict], Optional[VerifiedAudio], str]:
        """
        Baixa 1 candidato Soulseek para STAGING_DIR com sondagem precoce do cabeçalho MP4/M4A.
        Retorna (outcome, staged_path, raw_info, verified_audio, error_msg), onde outcome é:
          - 'ok': baixado e auditado com ffprobe completo
          - 'early_fake': cabeçalho MP4 nos primeiros KB/MB já provou não ser Atmos (abortado cedo!)
          - 'cancelled': cancelado pelo usuário
          - 'error': falha na transferência
        """
        last_emit = [0.0]
        early_abort = threading.Event()
        early_fake_result: list[tuple[dict, VerifiedAudio]] = []
        inspected_thresholds = [False, False]  # 512 KB e 3 MB

        prefix_lbl = f"[{attempt_idx}/{total_attempts}] " if total_attempts > 1 else ""

        def _check_early_header(local_path_str: str, done_bytes: int):
            if not allow_early_abort or early_abort.is_set():
                return
            if cand.extension.lower() not in (".m4a", ".mp4"):
                return
            stage = -1
            if done_bytes >= 512 * 1024 and not inspected_thresholds[0]:
                inspected_thresholds[0] = True
                stage = 0
            elif done_bytes >= 3 * 1024 * 1024 and not inspected_thresholds[1]:
                inspected_thresholds[1] = True
                stage = 1
            if stage < 0 or not local_path_str:
                return

            p = Path(local_path_str)
            hdr = inspect_mp4_prefix(p)
            if not hdr.get("moov_complete"):
                return
            if hdr.get("has_atmos_capable"):
                return

            # O box moov completo provou que NÃO existe stream ec-3 nem mlpa neste arquivo!
            # Rodar ffprobe no prefixo já baixado (funciona porque moov está completo!) para saber o codec real exato.
            info = probe_audio_full(p, timeout=4.0)
            if not info:
                codecs = hdr.get("codecs") or []
                guess_codec = "aac" if "mp4a" in codecs else ("ac3" if "ac-3" in codecs else ("alac" if "alac" in codecs else (codecs[0] if codecs else "")))
                if guess_codec:
                    info = {"codec_name": guess_codec, "profile": "", "channels": 6 if guess_codec in ("ac3",) else 2}
            if info:
                va = classify_probed_audio(info, ext=cand.extension.lower(), verified_pill=True)
                if not va.is_atmos:
                    record_verified_track(cand.remote_user, cand.remote_path, cand.filename, cand.size_bytes, info)
                    if job._on_peer_verified:
                        try:
                            job._on_peer_verified(cand, va)
                        except Exception:
                            pass
                    early_fake_result.append((info, va))
                    early_abort.set()

        class _CombinedCancel:
            def is_set(self) -> bool:
                return job._cancel_event.is_set() or early_abort.is_set()

        def _on_update(payload: dict):
            st = payload["state"]
            now = time.monotonic()
            total = payload.get("total_bytes") or cand.size_bytes
            done = payload.get("bytes_transferred", 0)
            spd = payload.get("speed", 0.0)

            job.bytes_done = done
            if total:
                job.bytes_total = total

            if st == TransferState.State.QUEUED:
                job.status = DownloadStatus.QUEUED
                job.status_detail = f"{prefix_lbl}Na fila do peer ({cand.remote_user})..."
                self._emit(job, status_cb)
            elif st == TransferState.State.INITIALIZING:
                job.status = DownloadStatus.STARTING
                job.status_detail = f"{prefix_lbl}Conectando a {cand.remote_user}..."
                self._emit(job, status_cb)
            elif st == TransferState.State.DOWNLOADING:
                lp = payload.get("local_path") or ""
                if lp and done >= 512 * 1024:
                    _check_early_header(str(lp), done)
                if early_abort.is_set():
                    return
                if now - last_emit[0] < 0.15 and done < total:
                    return
                last_emit[0] = now
                job.status = DownloadStatus.DOWNLOADING
                job.progress = min(0.99, done / total) if total else 0.0
                job.speed_label = format_speed(spd)
                job.status_detail = (
                    f"{prefix_lbl}{format_size(done)} / {format_size(total)}" if total else f"{prefix_lbl}{format_size(done)}"
                )
                self._emit(job, status_cb)

        ok, staged_path, err_msg = self.slsk.download(
            username=cand.remote_user,
            remote_path=cand.remote_path,
            on_update=_on_update,
            cancel_event=_CombinedCancel(),  # type: ignore[arg-type]
        )

        if job._cancel_event.is_set():
            if staged_path:
                staged_path.unlink(missing_ok=True)
            return "cancelled", None, None, None, "Download cancelado."

        if early_abort.is_set() and early_fake_result:
            if staged_path:
                staged_path.unlink(missing_ok=True)
            info, va = early_fake_result[0]
            return "early_fake", None, info, va, f"Cabeçalho indicou {va.quality_label} (não é Atmos)"

        if not ok or not staged_path or not staged_path.exists():
            return "error", None, None, None, err_msg or "Falha na transferência."

        info = probe_audio_full(staged_path)
        va = classify_probed_audio(info, ext=staged_path.suffix.lower(), verified_pill=True) if info else None
        if info and va:
            record_verified_track(cand.remote_user, cand.remote_path, cand.filename, cand.size_bytes, info)
            if job._on_peer_verified:
                try:
                    job._on_peer_verified(cand, va)
                except Exception:
                    pass
        return "ok", staged_path, info, va, ""

    def _download_soulseek(self, job: DownloadJob, status_cb: Callable[[DownloadJob], None]):
        want_atmos = job.track.codec_tier in (1, 2)
        candidates: list[TrackResult] = [job.track]
        if want_atmos and job.fallback_tracks:
            candidates.extend(job.fallback_tracks)

        total_attempts = len(candidates)
        # Guardar melhor arquivo não-Atmos em STAGING caso nenhuma fonte entregue Atmos real
        best_fallback: Optional[tuple[int, Path, TrackResult, Optional[VerifiedAudio]]] = None
        last_err = ""
        fake_reports: list[str] = []

        chosen_staged: Optional[Path] = None
        chosen_track: TrackResult = job.track
        chosen_va: Optional[VerifiedAudio] = None

        for idx, cand in enumerate(candidates, start=1):
            if job._cancel_event.is_set():
                break

            has_more = idx < total_attempts
            outcome, staged_path, _info, va, err_msg = self._download_single_soulseek_candidate(
                cand=cand,
                job=job,
                status_cb=status_cb,
                attempt_idx=idx,
                total_attempts=total_attempts,
                allow_early_abort=(want_atmos and has_more),
            )

            if outcome == "cancelled":
                if best_fallback:
                    best_fallback[1].unlink(missing_ok=True)
                job.status = DownloadStatus.CANCELLED
                job.status_detail = "Download cancelado."
                self._emit(job, status_cb)
                return

            if outcome == "early_fake" and va:
                fake_reports.append(f"{cand.remote_user}: {va.quality_label} {va.channels_label}")
                job.status = DownloadStatus.STARTING
                job.progress = 0.0
                job.status_detail = (
                    f"Fonte {idx}/{total_attempts} era {va.quality_label} (não é Atmos) — tentando próxima fonte..."
                )
                self._emit(job, status_cb)
                continue

            if outcome != "ok" or not staged_path:
                last_err = err_msg or "Falha na transferência."
                if has_more:
                    job.status = DownloadStatus.STARTING
                    job.progress = 0.0
                    job.status_detail = f"Fonte {idx}/{total_attempts} falhou — tentando próxima fonte..."
                    self._emit(job, status_cb)
                continue

            # Arquivo baixado e auditado pelo ffprobe!
            if (not want_atmos) or (va and va.is_atmos) or (not has_more and best_fallback is None):
                chosen_staged = staged_path
                chosen_track = cand
                chosen_va = va
                if best_fallback and best_fallback[1] != staged_path:
                    best_fallback[1].unlink(missing_ok=True)
                    best_fallback = None
                break

            # Peer mentiu sobre Dolby Atmos (ex: mandou AAC 5.1, AC-3 5.1 ou DD+ 5.1 sem JOC) e temos mais fontes!
            rank_val = {1: 5, 2: 4, 3: 3, 4: 2, 5: 1}.get(va.codec_tier if va else 5, 1)
            if va:
                fake_reports.append(f"{cand.remote_user}: {va.quality_label} {va.channels_label}")
            if best_fallback is None or rank_val > best_fallback[0]:
                if best_fallback is not None:
                    best_fallback[1].unlink(missing_ok=True)
                best_fallback = (rank_val, staged_path, cand, va)
            else:
                staged_path.unlink(missing_ok=True)

            job.status = DownloadStatus.STARTING
            job.progress = 0.0
            actual_lbl = f"{va.quality_label} {va.channels_label}" if va else "não-Atmos"
            job.status_detail = (
                f"Fonte {idx}/{total_attempts} enviou {actual_lbl} (não é Atmos) — buscando Atmos real ({idx + 1}/{total_attempts})..."
            )
            self._emit(job, status_cb)

        # Se esgotamos as fontes e nenhuma tinha Atmos real, usar o melhor fallback salvo em staging (se houver)
        if chosen_staged is None and best_fallback is not None:
            _, chosen_staged, chosen_track, chosen_va = best_fallback

        if chosen_staged is None or not chosen_staged.exists():
            job.status = DownloadStatus.ERROR
            if fake_reports:
                job.error_msg = f"Nenhuma fonte entregou Dolby Atmos real ({'; '.join(fake_reports)})."
            else:
                job.error_msg = last_err or "Falha na transferência."
            self._emit(job, status_cb)
            return

        job.actual_track = chosen_track
        job.status = DownloadStatus.VERIFYING
        job.progress = 0.99
        if chosen_va and chosen_va.is_atmos:
            job.status_detail = "Dolby Atmos confirmado! Empacotando para TV/Soundbar..."
        else:
            job.status_detail = "Verificando e preparando arquivo para a biblioteca..."
        self._emit(job, status_cb)

        # Fazer 100% do remux DENTRO de STAGING_DIR (fora de ~/Music/DolbyAtmos)
        # para que o Rygel nunca veja .m4a / .jpg / .png temporários e nunca corrompa seu banco SQLite!
        processed_path = remux_atmos_for_tv_soundbar(chosen_staged, chosen_track, work_dir=STAGING_DIR)

        # Classificação FINAL via ffprobe no arquivo que vai efetivamente para a biblioteca
        final_info = probe_audio_full(processed_path)
        final_va = classify_probed_audio(final_info, ext=processed_path.suffix.lower()) if final_info else chosen_va

        honest_tag = final_va.filename_tag if final_va else ""
        target = _build_target_path(
            chosen_track,
            processed_path.suffix,
            directory=job.dest_dir,
            filename_prefix=job.filename_prefix,
            tag_override=honest_tag,
        )
        try:
            shutil.move(str(processed_path), str(target))
        except Exception:
            shutil.copy2(str(processed_path), str(target))
            processed_path.unlink(missing_ok=True)

        job.verified_audio = final_va.summary if final_va else (probe_audio_file(target) or chosen_track.codec_label)
        job.verified_tier = final_va.codec_tier if final_va else chosen_track.codec_tier
        job.saved_path = target
        job.progress = 1.0
        job.status = DownloadStatus.DONE
        job.speed_label = ""

        if want_atmos and final_va and not final_va.is_atmos:
            job.verification_warning = (
                f"Aviso: o arquivo da rede era {final_va.quality_label} {final_va.channels_label} "
                f"(não contém Dolby Atmos 7.1.4). Salvo honestamente como {final_va.spatial_label}."
            )
            job.status_detail = f"Salvo como {final_va.quality_label} {final_va.channels_label} — não é Dolby Atmos ({job.verified_audio})"
        elif want_atmos and fake_reports and final_va and final_va.is_atmos:
            job.status_detail = f"Dolby Atmos real encontrado na tentativa seguinte! ({job.verified_audio})"
        else:
            job.status_detail = f"Pronto! Verificado ({job.verified_audio})"
        self._emit(job, status_cb)
