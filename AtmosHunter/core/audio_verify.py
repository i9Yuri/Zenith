# Copyright (c) 2026 Yuri Barbosa (@i9Yuri). All Rights Reserved.
# Proprietary source-available software (personal, non-commercial use only).
# Official repository: https://github.com/i9Yuri/Zenith
# Unauthorized commercial use, redistribution, or rebranding is prohibited. See LICENSE.
"""
AtmosHunter — Fonte Única de Verdade para Verificação Real de Áudio (ffprobe + Cabeçalho MP4 + Cache)
Garante consistência absoluta entre:
  - Busca (tags anunciadas vs. verificadas + cache persistente de peers já auditados)
  - Download (sondagem rápida do cabeçalho MP4/M4A durante a transferência + ffprobe completo antes de salvar)
  - Importação de Playlists (classificação honesta sem falso Atmos)
  - Minha Biblioteca (tags idênticas ao codec real dentro do arquivo)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.ranker import AudioTag

logger = logging.getLogger("AtmosHunter.verify")

VERIFIED_CACHE_FILE = Path.home() / ".cache" / "atmoshunter" / "verified_tracks.json"
_CACHE_LOCK = threading.Lock()
_CACHE_MEM: Optional[dict] = None
_MAX_CACHE_ENTRIES = 2000


@dataclass
class VerifiedAudio:
    codec_name: str = ""
    profile: str = ""
    channels: int = 0
    channel_layout: str = ""
    sample_rate: int = 0
    bit_rate: int = 0
    bits: int = 0

    codec_tier: int = 5          # 1=TrueHD Atmos, 2=E-AC-3 JOC Atmos, 3=Surround, 4=Lossless Stereo, 5=Lossy Stereo
    is_atmos: bool = False
    is_surround: bool = False
    is_lossless: bool = False

    spatial_label: str = "ESTÉREO"
    quality_label: str = "SD"
    channels_label: str = "2.0"
    format_label: str = ""
    codec_label: str = ""
    summary: str = ""
    filename_tag: str = ""       # " [Dolby Atmos TrueHD]" | " [Dolby Atmos]" | " [Surround 5.1]" | " [Surround 7.1]" | ""
    jbl_explanation: str = ""
    tags: list[AudioTag] = field(default_factory=list)


def probe_audio_full(path: Path, timeout: float = 8.0) -> dict:
    """Executa ffprobe completo retornando codec real, profile (JOC/Atmos), canais, sample_rate e bitrate."""
    if not shutil.which("ffprobe") or not path.exists():
        return {}
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries",
                "stream=codec_type,codec_name,profile,channels,channel_layout,sample_rate,bit_rate,bits_per_raw_sample"
                ":format=duration,format_name:format_tags=title,artist",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
        if not audio:
            return {}
        has_video = any(s.get("codec_type") == "video" and s.get("codec_name") != "mjpeg" for s in streams)
        fmt = data.get("format") or {}
        tags = fmt.get("tags") or {}
        return {
            "codec_name": str(audio.get("codec_name") or "").lower(),
            "profile": str(audio.get("profile") or ""),
            "channels": int(audio.get("channels") or 0),
            "channel_layout": str(audio.get("channel_layout") or ""),
            "sample_rate": int(audio.get("sample_rate") or 0),
            "bit_rate": int(audio.get("bit_rate") or 0),
            "bits": int(audio.get("bits_per_raw_sample") or 0),
            "duration": int(float(fmt.get("duration") or 0)),
            "has_video": has_video,
            "tag_title": str(tags.get("title") or tags.get("TITLE") or ""),
            "tag_artist": str(tags.get("artist") or tags.get("ARTIST") or ""),
        }
    except Exception as e:
        logger.debug(f"ffprobe erro em {path.name}: {e}")
        return {}


def inspect_mp4_prefix(path: Path, max_read_bytes: int = 4 * 1024 * 1024) -> dict:
    """
    Sondagem ultra-rápida (<1ms) dos boxes MP4/M4A iniciais (ftyp/moov/stsd) sem depender de arquivo completo.
    Se o box `moov` estiver completo no prefixo já baixado, identifica imediatamente os FourCCs de áudio
    (`ec-3`, `mlpa`, `ac-3`, `mp4a`, `alac`, `fLaC`, `Opus`) para abortar falsos Atmos logo nos primeiros %!
    """
    try:
        if not path.exists():
            return {"moov_complete": False}
        with open(path, "rb") as f:
            data = f.read(max_read_bytes)
    except OSError:
        return {"moov_complete": False}

    n = len(data)
    if n < 16:
        return {"moov_complete": False}

    pos = 0
    moov_bytes: Optional[bytes] = None
    while pos + 8 <= n:
        size, btype = struct.unpack(">I4s", data[pos:pos + 8])
        hdr = 8
        if size == 1 and pos + 16 <= n:
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            hdr = 16
        elif size == 0:
            # Box vai até EOF — em arquivo parcial não temos certeza se terminou
            break
        if size < hdr:
            break
        if btype == b"moov":
            if pos + size <= n:
                moov_bytes = data[pos + hdr:pos + size]
            break
        pos += size

    if not moov_bytes:
        return {"moov_complete": False}

    codecs: list[str] = []
    for tag in (b"ec-3", b"mlpa", b"ac-3", b"mp4a", b"alac", b"fLaC", b"Opus"):
        if tag in moov_bytes:
            codecs.append(tag.decode("ascii"))

    # Atmos só existe em MP4/M4A com stream E-AC-3 (`ec-3`) ou TrueHD (`mlpa`)
    has_atmos_capable = ("ec-3" in codecs) or ("mlpa" in codecs)
    return {
        "moov_complete": True,
        "codecs": codecs,
        "has_dec3": b"dec3" in moov_bytes,
        "has_atmos_capable": has_atmos_capable,
    }


def _channels_label_from_count(ch: int, layout: str = "") -> str:
    low = (layout or "").lower()
    if "7.1" in low or ch >= 8:
        return "7.1"
    if "5.1" in low or ch == 6:
        return "5.1"
    if ch == 5:
        return "5.0"
    if ch == 4:
        return "4.0"
    if ch == 1:
        return "1.0"
    return "2.0"


def classify_probed_audio(
    info: dict,
    ext: str = ".m4a",
    verified_pill: bool = False,
) -> VerifiedAudio:
    """
    Classificação 100% honesta baseada exclusivamente em dados reais do ffprobe.
    Usada de forma idêntica na Busca (cache), Download, Playlists e Biblioteca!
    """
    codec = str(info.get("codec_name") or "").lower()
    profile = str(info.get("profile") or "")
    prof_low = profile.lower()
    ch = int(info.get("channels") or 0)
    layout = str(info.get("channel_layout") or "")
    sr = int(info.get("sample_rate") or 0)
    br = int(info.get("bit_rate") or 0)
    bits = int(info.get("bits") or 0)
    ext_u = ext.lstrip(".").upper() or "AUDIO"
    br_k = br // 1000 if br else 0
    br_s = f" · {br_k}k" if br_k else ""
    sr_s = f"{sr / 1000:.1f} kHz" if sr else ""
    ch_lbl = _channels_label_from_count(ch, layout)

    res = VerifiedAudio(
        codec_name=codec,
        profile=profile,
        channels=ch,
        channel_layout=layout,
        sample_rate=sr,
        bit_rate=br,
        bits=bits,
    )

    if codec == "truehd":
        # FFmpeg reporta "Dolby TrueHD + Dolby Atmos" quando há objetos Atmos; TrueHD puro 5.1/7.1 não é Atmos
        is_atmos = ("atmos" in prof_low) or (not prof_low and ch >= 8)
        if is_atmos:
            res.codec_tier = 1
            res.is_atmos = True
            res.is_lossless = True
            res.spatial_label = "Dolby Atmos"
            res.quality_label = "LOSSLESS"
            res.channels_label = "7.1.4"
            res.format_label = f"{ext_u} · TrueHD"
            res.codec_label = "Dolby Atmos TrueHD (Lossless)"
            res.filename_tag = " [Dolby Atmos TrueHD]"
            res.summary = f"Dolby Atmos TrueHD · {sr_s}" if sr_s else "Dolby Atmos TrueHD"
            res.jbl_explanation = (
                "Verificado via ffprobe: Dolby Atmos TrueHD sem perdas (objetos 3D 7.1.4 na JBL Bar 1000MK2)."
            )
            res.tags = [
                AudioTag("Dolby Atmos", "atmos"),
                AudioTag("LOSSLESS", "lossless"),
                AudioTag("7.1.4", "channels"),
                AudioTag(res.format_label, "format"),
            ]
        else:
            res.codec_tier = 3
            res.is_surround = True
            res.is_lossless = True
            res.spatial_label = "SURROUND"
            res.quality_label = "TRUEHD"
            res.channels_label = ch_lbl if ch >= 4 else "5.1"
            res.format_label = f"{ext_u} · TrueHD"
            res.codec_label = f"Dolby TrueHD Surround {res.channels_label} (sem Atmos)"
            res.filename_tag = f" [Surround {res.channels_label}]"
            res.summary = f"Dolby TrueHD {res.channels_label} (sem Atmos) · {sr_s}" if sr_s else f"Dolby TrueHD {res.channels_label}"
            res.jbl_explanation = (
                f"Verificado via ffprobe: Dolby TrueHD {res.channels_label} sem metadados de objetos Dolby Atmos."
            )
            res.tags = [
                AudioTag("SURROUND", "surround"),
                AudioTag("TRUEHD", "lossless"),
                AudioTag(res.channels_label, "channels"),
                AudioTag(res.format_label, "format"),
            ]

    elif codec == "eac3":
        # Regra estrita: E-AC-3 só é Dolby Atmos quando o ffprobe confirma JOC / Atmos no profile!
        is_atmos = ("atmos" in prof_low) or ("joc" in prof_low)
        if is_atmos:
            res.codec_tier = 2
            res.is_atmos = True
            res.spatial_label = "Dolby Atmos"
            res.quality_label = "DD+ JOC"
            res.channels_label = "7.1.4"
            res.format_label = f"{ext_u}{br_s}"
            res.codec_label = "Dolby Atmos (E-AC-3 JOC)"
            res.filename_tag = " [Dolby Atmos]"
            parts = ["Dolby Atmos (E-AC-3 JOC)", layout or f"{ch}ch"]
            if sr_s:
                parts.append(sr_s)
            if br_k:
                parts.append(f"{br_k} kbps")
            res.summary = " · ".join(p for p in parts if p)
            res.jbl_explanation = (
                "Verificado via ffprobe: Dolby Atmos real (E-AC-3 JOC com objetos espaciais 3D 7.1.4 na JBL Bar 1000MK2)."
            )
            res.tags = [
                AudioTag("Dolby Atmos", "atmos"),
                AudioTag("DD+ JOC", "hd"),
                AudioTag("7.1.4", "channels"),
                AudioTag(res.format_label, "format"),
            ]
        elif ch >= 4:
            res.codec_tier = 3
            res.is_surround = True
            res.spatial_label = "SURROUND"
            res.quality_label = f"DD+ {ch_lbl}"
            res.channels_label = ch_lbl
            res.format_label = f"{ext_u}{br_s}"
            res.codec_label = f"Dolby Digital Plus {ch_lbl} (sem Atmos)"
            res.filename_tag = f" [Surround {ch_lbl}]"
            parts = [f"Dolby Digital Plus {ch_lbl} (sem Atmos)", sr_s, f"{br_k} kbps" if br_k else ""]
            res.summary = " · ".join(p for p in parts if p)
            res.jbl_explanation = (
                f"Verificado via ffprobe: Dolby Digital Plus {ch_lbl} comum (E-AC-3 sem objetos Dolby Atmos)."
            )
            res.tags = [
                AudioTag("SURROUND", "surround"),
                AudioTag(res.quality_label, "hd"),
                AudioTag(ch_lbl, "channels"),
                AudioTag(res.format_label, "format"),
            ]
        else:
            res.codec_tier = 5
            res.spatial_label = "ESTÉREO"
            res.quality_label = "DD+"
            res.channels_label = ch_lbl
            res.format_label = f"{ext_u}{br_s}"
            res.codec_label = "Dolby Digital Plus Estéreo"
            res.summary = f"Dolby Digital Plus 2.0 · {br_k} kbps" if br_k else "Dolby Digital Plus 2.0"
            res.jbl_explanation = "Verificado via ffprobe: Dolby Digital Plus estéreo 2.0 (não é Atmos)."
            res.tags = [
                AudioTag("ESTÉREO", "stereo"),
                AudioTag("DD+", "hd"),
                AudioTag(ch_lbl, "channels"),
                AudioTag(res.format_label, "format"),
            ]

    elif codec == "ac3":
        if ch >= 4:
            res.codec_tier = 3
            res.is_surround = True
            res.spatial_label = "SURROUND"
            res.quality_label = f"DD {ch_lbl}"
            res.channels_label = ch_lbl
            res.format_label = f"{ext_u}{br_s}"
            res.codec_label = f"Dolby Digital AC-3 {ch_lbl}"
            res.filename_tag = f" [Surround {ch_lbl}]"
            parts = [f"Dolby Digital AC-3 {ch_lbl}", sr_s, f"{br_k} kbps" if br_k else ""]
            res.summary = " · ".join(p for p in parts if p)
            res.jbl_explanation = (
                f"Verificado via ffprobe: Dolby Digital AC-3 {ch_lbl} discreto (sem objetos de teto Dolby Atmos)."
            )
            res.tags = [
                AudioTag("SURROUND", "surround"),
                AudioTag(res.quality_label, "hd"),
                AudioTag(ch_lbl, "channels"),
                AudioTag(res.format_label, "format"),
            ]
        else:
            res.codec_tier = 5
            res.spatial_label = "ESTÉREO"
            res.quality_label = "DD 2.0"
            res.channels_label = ch_lbl
            res.format_label = f"{ext_u}{br_s}"
            res.codec_label = "Dolby Digital AC-3 Estéreo"
            res.summary = f"Dolby Digital AC-3 2.0 · {br_k} kbps" if br_k else "Dolby Digital AC-3 2.0"
            res.jbl_explanation = "Verificado via ffprobe: AC-3 estéreo 2.0 (não é Atmos)."
            res.tags = [
                AudioTag("ESTÉREO", "stereo"),
                AudioTag("DD 2.0", "hd"),
                AudioTag(ch_lbl, "channels"),
                AudioTag(res.format_label, "format"),
            ]

    elif codec == "aac":
        if ch >= 4:
            res.codec_tier = 3
            res.is_surround = True
            res.spatial_label = "SURROUND"
            res.quality_label = f"AAC {ch_lbl}"
            res.channels_label = ch_lbl
            res.format_label = f"{ext_u}{br_s}"
            res.codec_label = f"AAC Multicanal {ch_lbl}"
            res.filename_tag = f" [Surround {ch_lbl}]"
            parts = [f"AAC {ch_lbl} (não é Dolby Atmos)", sr_s, f"{br_k} kbps" if br_k else ""]
            res.summary = " · ".join(p for p in parts if p)
            res.jbl_explanation = (
                f"Verificado via ffprobe: AAC {ch_lbl} comprimido (não é Dolby Atmos 7.1.4)."
            )
            res.tags = [
                AudioTag("SURROUND", "surround"),
                AudioTag(res.quality_label, "hd"),
                AudioTag(ch_lbl, "channels"),
                AudioTag(res.format_label, "format"),
            ]
        else:
            res.codec_tier = 5
            res.spatial_label = "ESTÉREO"
            res.quality_label = "AAC"
            res.channels_label = ch_lbl
            res.format_label = f"{ext_u}{br_s}"
            res.codec_label = "AAC Estéreo (Lossy)"
            parts = ["AAC Estéreo 2.0", sr_s, f"{br_k} kbps" if br_k else ""]
            res.summary = " · ".join(p for p in parts if p)
            res.jbl_explanation = "Verificado via ffprobe: AAC estéreo comprimido (não é Dolby Atmos nem Lossless)."
            res.tags = [
                AudioTag("ESTÉREO", "stereo"),
                AudioTag("AAC", "hd"),
                AudioTag(ch_lbl, "channels"),
                AudioTag(res.format_label, "format"),
            ]

    elif codec in ("flac", "alac", "pcm_s16le", "pcm_s24le", "pcm_s24be", "pcm_s16be", "wavpack"):
        res.is_lossless = True
        is_ultra = (bits >= 24) or (sr > 48000)
        q_lbl = "ULTRA HD" if is_ultra else "HD"
        cname = "ALAC" if codec == "alac" else ("FLAC" if codec == "flac" else "PCM")
        bd_s = f" · {bits}-bit" if bits else ""
        res.format_label = f"{cname}{bd_s}"
        if ch >= 4:
            res.codec_tier = 3
            res.is_surround = True
            res.spatial_label = "SURROUND"
            res.quality_label = q_lbl
            res.channels_label = ch_lbl
            res.codec_label = f"{cname} Multicanal {ch_lbl}"
            res.filename_tag = f" [Surround {ch_lbl}]"
            parts = [f"{cname} {bits or 16}-bit {ch_lbl}", sr_s]
            res.summary = " · ".join(p for p in parts if p)
            res.jbl_explanation = f"Verificado via ffprobe: {cname} multicanal discreto {ch_lbl} sem perdas."
            res.tags = [
                AudioTag("SURROUND", "surround"),
                AudioTag(q_lbl, "lossless" if is_ultra else "hd"),
                AudioTag(ch_lbl, "channels"),
                AudioTag(res.format_label, "format"),
            ]
        else:
            res.codec_tier = 4
            res.spatial_label = "ESTÉREO"
            res.quality_label = q_lbl
            res.channels_label = ch_lbl
            res.codec_label = f"{cname} {q_lbl} Estéreo"
            parts = [f"{cname} {bits or 16}-bit", sr_s]
            res.summary = " · ".join(p for p in parts if p)
            res.jbl_explanation = f"Verificado via ffprobe: áudio sem perdas {cname} ({q_lbl} 2.0)."
            res.tags = [
                AudioTag("ESTÉREO", "stereo"),
                AudioTag(q_lbl, "lossless" if is_ultra else "hd"),
                AudioTag(ch_lbl, "channels"),
                AudioTag(res.format_label, "format"),
            ]

    elif codec == "dts":
        res.codec_tier = 3
        res.is_surround = True
        res.spatial_label = "SURROUND"
        res.quality_label = "DTS-HD" if ("ma" in prof_low or "hd" in prof_low) else "DTS"
        res.channels_label = ch_lbl if ch >= 4 else "5.1"
        res.format_label = f"{ext_u}{br_s}"
        res.codec_label = f"{res.quality_label} Surround {res.channels_label}"
        res.filename_tag = f" [Surround {res.channels_label}]"
        res.summary = f"{res.quality_label} {res.channels_label}" + (f" · {sr_s}" if sr_s else "")
        res.jbl_explanation = f"Verificado via ffprobe: {res.quality_label} multicanal {res.channels_label}."
        res.tags = [
            AudioTag("SURROUND", "surround"),
            AudioTag(res.quality_label, "hd"),
            AudioTag(res.channels_label, "channels"),
            AudioTag(res.format_label, "format"),
        ]

    else:
        is_mc = ch >= 4
        res.codec_tier = 3 if is_mc else 5
        res.is_surround = is_mc
        res.spatial_label = "SURROUND" if is_mc else "ESTÉREO"
        res.quality_label = (codec.upper() or ext_u)[:10]
        res.channels_label = ch_lbl
        res.format_label = f"{ext_u}{br_s}"
        res.codec_label = f"{codec.upper() or ext_u} ({ch_lbl})"
        res.filename_tag = f" [Surround {ch_lbl}]" if is_mc else ""
        res.summary = f"{codec.upper() or ext_u} · {ch_lbl}" + (f" · {br_k} kbps" if br_k else "")
        res.jbl_explanation = f"Verificado via ffprobe: {res.summary}."
        res.tags = [
            AudioTag(res.spatial_label, "surround" if is_mc else "stereo"),
            AudioTag(res.quality_label, "hd"),
            AudioTag(ch_lbl, "channels"),
            AudioTag(res.format_label, "format"),
        ]

    if verified_pill:
        res.tags.append(AudioTag("Verificado", "verified"))
    return res


# ──────────────────────────────────────────────────────────────
# CACHE PERSISTENTE DE VERIFICAÇÕES REAIS (EXATAMENTE O QUE CADA PEER ENVIOU)
# ──────────────────────────────────────────────────────────────

def _ensure_cache_loaded(cache_file: Optional[Path] = None) -> dict:
    global _CACHE_MEM
    cf = cache_file or VERIFIED_CACHE_FILE
    if _CACHE_MEM is not None and cf == VERIFIED_CACHE_FILE:
        return _CACHE_MEM
    data: dict = {"by_peer": {}, "by_sig": {}}
    if cf.exists():
        try:
            raw = json.loads(cf.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data["by_peer"] = dict(raw.get("by_peer") or {})
                data["by_sig"] = dict(raw.get("by_sig") or {})
        except Exception as e:
            logger.debug(f"Cache de verificação ignorado: {e}")
    if cf == VERIFIED_CACHE_FILE:
        _CACHE_MEM = data
    return data


def _save_cache(data: dict, cache_file: Optional[Path] = None) -> None:
    cf = cache_file or VERIFIED_CACHE_FILE
    try:
        # Limitar crescimento do cache mantendo os mais recentes
        for bucket_name in ("by_peer", "by_sig"):
            bucket = data.get(bucket_name) or {}
            if len(bucket) > _MAX_CACHE_ENTRIES:
                ordered = sorted(bucket.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:_MAX_CACHE_ENTRIES]
                data[bucket_name] = dict(ordered)
        cf.parent.mkdir(parents=True, exist_ok=True)
        tmp = cf.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, cf)
    except Exception as e:
        logger.debug(f"Não foi possível salvar verified_tracks.json: {e}")


def _peer_key(remote_user: str, remote_path: str) -> str:
    return f"{(remote_user or '').strip().lower()}|{(remote_path or '').strip().lower()}"


def _sig_key(filename: str, size_bytes: int) -> str:
    base = os.path.basename(filename or "").strip().lower()
    if not base or size_bytes < 2 * 1024 * 1024:
        return ""
    return f"{base}|{int(size_bytes)}"


def record_verified_track(
    remote_user: str,
    remote_path: str,
    filename: str,
    size_bytes: int,
    info: dict,
    cache_file: Optional[Path] = None,
) -> None:
    """Grava o resultado real do ffprobe para um arquivo de peer (nunca mais engana em buscas futuras)."""
    if not info or not info.get("codec_name"):
        return
    cf = cache_file or VERIFIED_CACHE_FILE
    entry = {
        "codec_name": str(info.get("codec_name") or "").lower(),
        "profile": str(info.get("profile") or ""),
        "channels": int(info.get("channels") or 0),
        "channel_layout": str(info.get("channel_layout") or ""),
        "sample_rate": int(info.get("sample_rate") or 0),
        "bit_rate": int(info.get("bit_rate") or 0),
        "bits": int(info.get("bits") or 0),
        "ts": int(time.time()),
    }
    with _CACHE_LOCK:
        data = _ensure_cache_loaded(cf)
        pk = _peer_key(remote_user, remote_path)
        if remote_user and remote_path:
            data["by_peer"][pk] = entry
        sk = _sig_key(filename or remote_path, size_bytes)
        if sk:
            data["by_sig"][sk] = entry
        _save_cache(data, cf)


def lookup_verified_track(
    remote_user: str,
    remote_path: str,
    filename: str = "",
    size_bytes: int = 0,
    cache_file: Optional[Path] = None,
) -> Optional[dict]:
    """Retorna metadados reais do ffprobe se este arquivo/peer já foi auditado anteriormente."""
    cf = cache_file or VERIFIED_CACHE_FILE
    with _CACHE_LOCK:
        data = _ensure_cache_loaded(cf)
        if remote_user and remote_path:
            hit = data["by_peer"].get(_peer_key(remote_user, remote_path))
            if hit:
                return dict(hit)
        sk = _sig_key(filename or remote_path, size_bytes)
        if sk:
            hit = data["by_sig"].get(sk)
            if hit:
                return dict(hit)
    return None


def reset_cache_memory() -> None:
    """Usado apenas em testes unitários para isolar estado em memória."""
    global _CACHE_MEM
    with _CACHE_LOCK:
        _CACHE_MEM = None
