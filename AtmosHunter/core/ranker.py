# Copyright (c) 2026 Yuri Barbosa (@i9Yuri). All Rights Reserved.
# Proprietary source-available software (personal, non-commercial use only).
# Official repository: https://github.com/i9Yuri/Zenith
# Unauthorized commercial use, redistribution, or rebranding is prohibited. See LICENSE.
"""
AtmosHunter — Motor de Classificação, Metadados, Tags e Matching Fuzzy
- Tolerante a erros de digitação (matching fuzzy por token via SequenceMatcher)
- Detecta Dolby Atmos E-AC-3 JOC mesmo quando o peer não escreve "atmos" na pasta
  (ex: pasta 'Multichannel Music' + .m4a ~768 kbps)
- Detecta ALAC Ultra HD (.m4a lossless 24-bit/96kHz) em vez de descartar como lossy
- Proteção anti-spam contra bots que inundam resultados com remixes genéricos
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher


# ──────────────────────────────────────────────────────────────
# MODELO DE DADOS
# ──────────────────────────────────────────────────────────────

@dataclass
class AudioTag:
    """Badge identificador exibido na tabela (ex: ATMOS 3D, ULTRA HD, 7.1.4, M4A)."""
    text: str
    kind: str   # "atmos" | "lossless" | "hd" | "surround" | "channels" | "format" | "stereo"


@dataclass
class TrackResult:
    title: str = ""
    artist: str = ""
    album: str = ""
    filename: str = ""
    extension: str = ""

    codec_label: str = ""
    codec_tier: int = 5            # 1 = TrueHD Atmos, 2 = Atmos E-AC-3 JOC, 3 = Multicanal, 4 = Lossless/Hi-Res, 5 = Lossy
    spatial_label: str = ""        # "ATMOS 3D" | "SURROUND" | "ESTÉREO"
    quality_label: str = ""        # "LOSSLESS" | "ULTRA HD" | "HD" | "DD+ JOC"
    channels_label: str = ""       # "7.1.4" | "7.1" | "5.1" | "2.0"
    format_label: str = ""
    jbl_explanation: str = ""
    tags: list[AudioTag] = field(default_factory=list)

    size_bytes: int = 0
    size_label: str = ""
    duration_sec: int = 0
    bitrate_kbps: int = 0
    sample_rate: int = 0
    bit_depth: int = 0

    speed_bps: int = 0
    has_free_slot: bool = True
    queue_length: int = 0
    source: str = ""               # "Soulseek" | "Hi-Res Direct" | "Internet Archive"
    provider: str = ""             # "soulseek" | "http"

    remote_user: str = ""
    remote_path: str = ""
    download_url: str = ""
    artwork_url: str = ""

    score: float = 0.0
    match_score: float = 0.0
    is_junk: bool = False
    junk_reason: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        if self.artist and self.title:
            if self.artist.lower() in self.title.lower():
                return self.title
            return f"{self.artist} - {self.title}"
        return self.title or self.filename

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024) if self.size_bytes else 0.0

    @property
    def availability_label(self) -> str:
        if self.provider == "http":
            return "Direto"
        speed_str = format_speed(self.speed_bps) if self.speed_bps > 0 else ""
        if self.has_free_slot:
            return f"Livre · {speed_str}" if speed_str else "Livre"
        q = self.queue_length
        if q > 0:
            return f"Fila ({q})"
        return "Ocupado"

    @property
    def is_atmos(self) -> bool:
        return self.codec_tier in (1, 2)


TorrentResult = TrackResult


# ──────────────────────────────────────────────────────────────
# CONSTANTES DE FILTRAGEM DE FAIXA ÚNICA
# ──────────────────────────────────────────────────────────────

MIN_TRACK_BYTES = 2 * 1024 * 1024
MAX_TRACK_BYTES = 450 * 1024 * 1024

ALLOWED_AUDIO_EXTS = {
    ".m4a", ".ec3", ".eac3", ".ac3", ".mka", ".flac", ".mlp", ".dts", ".wav", ".aiff", ".alac",
}
CONDITIONAL_CONTAINER_EXTS = {".mp4"}

FORBIDDEN_EXTS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".iso", ".img", ".bin", ".cue",
    ".mkv", ".avi", ".mov", ".wmv", ".webm", ".ts", ".m2ts", ".vob",
    ".mp3", ".ogg", ".wma", ".opus", ".aac",
    ".jpg", ".jpeg", ".png", ".pdf", ".txt", ".nfo", ".log", ".m3u", ".m3u8", ".sfv", ".lrc",
}

HARD_JUNK_PATTERNS = [
    r'[Ss]\d{2}[Ee]\d{2}',
    r'\b(2160p|1080p|720p|480p|4K\.UHD|UHD\.Blu-?ray)\b',
    r'\b(HDRip|BDRip|DVDRip|WEBRip|WEB-?DL|HDTV|BRRip|DvdScr|CAM|TS|REMUX)\b',
    r'\b(x264|x265|h\.?264|h\.?265|HEVC|AVC|XviD|DivX|DV\.HDR10|HDR10)\b',
    r'\b(Complete\.?Series|Season\s*\d+|Episode\s*\d+|Feature\.?Film|Theatrical\.?Cut)\b',
    r'\b(AMZN|DSNP|NF|HMAX)\s+(WEB|WEBDL|WEB-DL)\b',
    r'\b(Karaoke|Instrumental\s+Cover|8D\s+Audio|Slowed\s*\+?\s*Reverb|Nightcore)\b',
]


# ──────────────────────────────────────────────────────────────
# HELPERS DE FORMATAÇÃO E HIGIENIZAÇÃO
# ──────────────────────────────────────────────────────────────

def format_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "—"
    mb = size_bytes / (1024 ** 2)
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    if mb >= 10:
        return f"{mb:.1f} MB"
    return f"{mb:.2f} MB"


def format_speed(bps: float) -> str:
    if bps <= 0:
        return ""
    if bps >= 1024 ** 2:
        return f"{bps / (1024 ** 2):.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps:.0f} B/s"


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return ""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )


def _normalize_tokens(text: str) -> list[str]:
    clean = _strip_accents(text.lower())
    clean = re.sub(r"[^a-z0-9]+", " ", clean)
    stop = {"the", "a", "an", "of", "and", "in", "on", "to", "dolby", "atmos", "flac", "eac3", "m4a", "audio", "mix"}
    return [t for t in clean.split() if len(t) > 1 and t not in stop]


def _is_valid_artist_dir(name: str) -> bool:
    s = name.strip()
    if not s or len(s) > 45:
        return False
    low = s.lower()
    generic = {
        "music", "audio", "atmos", "dolby atmos", "multichannel", "multichannel music",
        "flac", "lossless", "hi-res", "hires", "downloads", "shares", "files", "samplers",
        "surround", "surround music", "shared", "users", "home", "media", "albums",
        "complete", "complete discography", "discography", "pop", "rock", "christmas",
        "dolby atmos music", "spatial", "spatial audio", "blu-ray", "bluray", "bd",
        "house", "dance", "electronic", "bass", "trance", "techno", "edm", "dubstep",
        "hip-hop", "rap", "r&b", "soul", "jazz", "classical", "apple music", "lossy",
    }
    if low in generic:
        return False
    if re.match(r"^\d+$", s) or re.match(r"^[a-z]{2,8}\d{3,}$", low):
        return False
    if re.match(r"^(?:19|20)\d{2}\b", s):
        return False
    if re.search(r"\b(sony|epic|columbia|warner|universal|remaster|anniversary|edition|box set|bluray|blu-ray|bd|atmos)\b", low):
        return False
    return True


def clean_track_metadata_from_path(remote_path: str) -> tuple[str, str, str, str]:
    parts = [p for p in re.split(r"[\\/]+", remote_path) if p and not p.startswith("@@")]
    filename = parts[-1] if parts else remote_path
    stem, _ = os.path.splitext(filename)

    cleaned = re.sub(r"^\s*(?:\d{1,2}\s*[-.]\s*\d{1,2}|\d{1,3})\s*[-._)]\s*", "", stem)
    cleaned = re.sub(r"^\s*\d{2}\s+", "", cleaned)
    cleaned = re.sub(
        r"\s*\[(?:E|M|Explicit|Clean|Atmos|Dolby Atmos|E-AC-3[^\]]*|FLAC[^\]]*|24-?bit[^\]]*|Hi-?Res[^\]]*)\]",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_")

    artist = ""
    title = cleaned
    album = ""

    if " - " in cleaned:
        left, right = cleaned.split(" - ", 1)
        if left.strip() and right.strip() and not re.match(r"^(?:19|20)\d{2}$", left.strip()):
            artist = left.strip()
            title = right.strip()

    parent_dirs = parts[:-1]
    useful_dirs = [
        d for d in parent_dirs
        if not re.match(r"^(?:cd|disc|disk)\s*\d+$", d.strip(), re.I)
        and not re.match(r"^\d+\s*-\s*bonus", d.strip(), re.I)
    ]

    if useful_dirs:
        last_dir = useful_dirs[-1]
        dir_clean = re.sub(r"\s*[\[\{][^\]\}]*[\]\}]", "", last_dir).strip()
        dir_no_year = re.sub(r"^(?:19|20)\d{2}\s*-\s*", "", dir_clean).strip()
        if " - " in dir_no_year:
            d_left, d_right = dir_no_year.split(" - ", 1)
            if not artist and _is_valid_artist_dir(d_left):
                artist = d_left.strip()
            album = re.sub(r"\s*\((?:19|20)\d{2}[^)]*\)", "", d_right).strip() or d_right.strip()
        else:
            album = re.sub(r"\s*\((?:19|20)\d{2}[^)]*\)", "", dir_no_year).strip() or dir_no_year

        if not artist:
            for prev_dir in reversed(useful_dirs[:-1]):
                cand = re.sub(r"\s*[\[\{][^\]\}]*[\]\}]", "", prev_dir).strip()
                if _is_valid_artist_dir(cand):
                    artist = cand
                    break

    return artist, title or stem, album, filename


# ──────────────────────────────────────────────────────────────
# FILTRO ANTI-LIXO (ARQUIVO ÚNICO DE MÚSICA)
# ──────────────────────────────────────────────────────────────

def is_junk(result: TrackResult) -> tuple[bool, str]:
    ext = result.extension.lower()
    if not ext and result.filename:
        ext = os.path.splitext(result.filename)[1].lower()
        result.extension = ext

    full_ctx = f"{result.remote_path} {result.filename} {result.title} {result.album}"

    if ext in FORBIDDEN_EXTS:
        return True, f"Extensão descartada ({ext})"

    if ext and ext not in ALLOWED_AUDIO_EXTS and ext not in CONDITIONAL_CONTAINER_EXTS:
        return True, f"Formato não suportado ({ext})"

    for pat in HARD_JUNK_PATTERNS:
        m = re.search(pat, full_ctx, re.IGNORECASE)
        if m:
            return True, f"Vídeo/série detectado ({m.group()})"

    if ext in CONDITIONAL_CONTAINER_EXTS:
        if not re.search(r"\b(atmos|eac3|e-ac-3|joc|spatial|multichannel)\b", full_ctx, re.I):
            return True, "MP4 sem indicação de Dolby Atmos"

    if result.size_bytes > 0:
        if result.size_bytes < MIN_TRACK_BYTES:
            return True, f"Arquivo muito pequeno ({result.size_mb:.1f} MB)"
        if result.size_bytes > MAX_TRACK_BYTES:
            return True, f"Arquivo grande demais ({result.size_mb:.0f} MB — álbum/vídeo)"

    return False, ""


# ──────────────────────────────────────────────────────────────
# DETECÇÃO DE CODEC, ESPACIALIDADE, QUALIDADE E TAGS
# ──────────────────────────────────────────────────────────────

def _estimate_kbps(result: TrackResult) -> int:
    if result.bitrate_kbps > 0:
        return result.bitrate_kbps
    if result.size_bytes > 0 and result.duration_sec > 30:
        return int((result.size_bytes * 8) / (result.duration_sec * 1000))
    return 0


def _build_tags(result: TrackResult) -> list[AudioTag]:
    tags: list[AudioTag] = []
    if result.spatial_label == "Dolby Atmos":
        tags.append(AudioTag("Dolby Atmos", "atmos"))
    elif result.spatial_label == "SURROUND":
        tags.append(AudioTag("SURROUND", "surround"))
    elif result.spatial_label == "ESTÉREO":
        tags.append(AudioTag("ESTÉREO", "stereo"))

    if result.quality_label:
        qkind = "lossless" if result.quality_label in ("LOSSLESS", "ULTRA HD") else "hd"
        tags.append(AudioTag(result.quality_label, qkind))

    if result.channels_label:
        tags.append(AudioTag(result.channels_label, "channels"))

    if result.format_label:
        tags.append(AudioTag(result.format_label, "format"))
    return tags


def detect_codec(result: TrackResult) -> tuple[int, str]:
    ext = (result.extension or os.path.splitext(result.filename)[1]).lower()
    ext_upper = ext.lstrip(".").upper() or "AUDIO"

    # ── STEP 0: Se este arquivo/peer já foi auditado via ffprobe anteriormente, usar a VERDADE REAL! ──
    if result.provider == "soulseek":
        from core.audio_verify import classify_probed_audio, lookup_verified_track

        cached = lookup_verified_track(
            remote_user=result.remote_user,
            remote_path=result.remote_path,
            filename=result.filename,
            size_bytes=result.size_bytes,
        )
        if cached:
            va = classify_probed_audio(cached, ext=ext, verified_pill=True)
            result.codec_tier = va.codec_tier
            result.spatial_label = va.spatial_label
            result.quality_label = va.quality_label
            result.channels_label = va.channels_label
            result.format_label = va.format_label
            result.codec_label = va.codec_label
            result.jbl_explanation = va.jbl_explanation
            result.tags = list(va.tags)
            result.extra["verified"] = True
            result.extra["verified_codec"] = va.codec_name
            return result.codec_tier, result.codec_label

    result.extra["verified"] = (result.provider == "http")
    full_ctx = f"{result.remote_path} {result.album} {result.title} {result.filename}".lower()
    est_kbps = _estimate_kbps(result)
    sr_str = f"{result.sample_rate / 1000:.1f}k" if result.sample_rate else ""

    has_atmos = bool(re.search(r"\b(dolby\s*atmos|atmos|joc|spatial)\b", full_ctx))
    has_eac3_tag = bool(re.search(r"\b(?:e[-\s]?ac[-\s]?3|eac3|dd\+|ddp)\b", full_ctx))
    has_multich_ctx = bool(re.search(r"\b(multichannel|multicanal|surround|5\.1|7\.1|quad)\b", full_ctx))
    has_truehd = bool(re.search(r"\b(truehd|mlp)\b", full_ctx)) or ext == ".mlp"
    has_bluray = bool(re.search(r"\b(blu-?ray|bd|pure\s*audio)\b", full_ctx))
    has_hires_ctx = bool(re.search(r"24[-\s]?bit|96\s*khz|192\s*khz|hi[-\s]?res|alac|lossless", full_ctx))
    # Se o próprio caminho/arquivo denuncia codec não-Atmos (AAC, AC-3, DD 5.1, DTS, Quad) sem E-AC-3/JOC/TrueHD,
    # nunca promover para Dolby Atmos!
    has_explicit_non_atmos = bool(
        re.search(r"\b(?:aac|ac[-\s]?3|dd\s*5\.1|quad|dts)\b", full_ctx)
    ) and not (has_eac3_tag or has_truehd or bool(re.search(r"\bjoc\b", full_ctx)))

    # ── TIER 1: Dolby Atmos Lossless (TrueHD / MLP / MKA Blu-ray grande) ──
    is_large_atmos_container = (
        ext in (".mka", ".mlp", ".m4a", ".mp4")
        and has_atmos
        and not has_explicit_non_atmos
        and (result.size_bytes >= 110 * 1024 * 1024 or est_kbps >= 2500)
    )
    if ((has_truehd and has_atmos) or (has_truehd and ext in (".mka", ".mlp", ".m4a")) or is_large_atmos_container) and not has_explicit_non_atmos:
        result.codec_tier = 1
        result.spatial_label = "Dolby Atmos"
        result.quality_label = "LOSSLESS"
        result.channels_label = "7.1.4"
        result.format_label = f"{ext_upper} · TrueHD"
        result.codec_label = "Dolby Atmos TrueHD (Lossless)"
        result.jbl_explanation = (
            "Anunciado pelo peer como Dolby Atmos TrueHD sem perdas (Blu-ray Pure Audio). "
            "Verificado automaticamente via ffprobe ao baixar (com troca de fonte se o arquivo não for Atmos real)."
        )
        result.tags = _build_tags(result)
        if result.provider == "soulseek":
            result.tags.append(AudioTag("A verificar", "unverified"))
        return result.codec_tier, result.codec_label

    # ── TIER 2: Dolby Atmos E-AC-3 JOC (.m4a / .mp4 / .ec3 / .mka COM indicação explícita de Atmos/JOC/E-AC-3) ──
    # Piso físico: E-AC-3 JOC tem bitrate >= 420 kbps (~768k padrão).
    # Pastas genéricas 'Multichannel' / 'Surround' / '5.1' SEM 'atmos'/'joc'/'eac3' NÃO são mais promovidas a Atmos:
    # vão honestamente para Tier 3 (SURROUND 5.1) para evitar falsos positivos de AAC 5.1 / AC-3 5.1!
    passes_atmos_bitrate = (est_kbps == 0 or est_kbps >= 420) and (
        result.size_bytes == 0 or result.size_bytes >= 11 * 1024 * 1024 or (0 < result.duration_sec < 130)
    )
    if (
        ext in (".m4a", ".mp4", ".ec3", ".eac3", ".mka")
        and passes_atmos_bitrate
        and (has_atmos or has_eac3_tag)
        and not has_explicit_non_atmos
    ):
        result.codec_tier = 2
        result.spatial_label = "Dolby Atmos"
        result.quality_label = "DD+ JOC"
        result.channels_label = "7.1.4"
        br_tag = f" · {est_kbps}k" if 400 <= est_kbps <= 1600 else ""
        result.format_label = f"{ext_upper}{br_tag}"
        result.codec_label = "Dolby Atmos (E-AC-3 JOC)"
        result.jbl_explanation = (
            "Anunciado pelo peer como Dolby Atmos (E-AC-3 JOC). "
            "O codec real é verificado automaticamente via ffprobe durante/após o download — "
            "se o peer enviar AAC 5.1 ou AC-3 5.1 falso, o AtmosHunter tenta outra fonte automaticamente."
        )
        result.tags = _build_tags(result)
        if result.provider == "soulseek":
            result.tags.append(AudioTag("A verificar", "unverified"))
        return result.codec_tier, result.codec_label

    # ── TIER 3: Áudio Multicanal Discreto (FLAC 5.1/7.1, M4A Multicanal/AAC 5.1, AC-3 5.1, DTS-HD) ──
    is_multich_flac = ext == ".flac" and (
        has_atmos or has_multich_ctx or result.size_bytes >= 95 * 1024 * 1024
    )
    is_multich_m4a = ext in (".m4a", ".mp4", ".mka") and (
        has_multich_ctx or has_explicit_non_atmos
    ) and (est_kbps == 0 or est_kbps >= 300) and (result.size_bytes == 0 or result.size_bytes >= 8 * 1024 * 1024)
    is_plain_ac3 = ext == ".ac3"
    is_dts = ext == ".dts" or bool(re.search(r"\bdts[-\s]?(?:hd|x)\b", full_ctx))
    if is_multich_flac or is_multich_m4a or is_dts or is_plain_ac3:
        result.codec_tier = 3
        result.spatial_label = "SURROUND"
        if is_plain_ac3 or re.search(r"\b(?:ac[-\s]?3|dd\s*5\.1)\b", full_ctx):
            result.quality_label = "DD 5.1"
        elif is_multich_m4a and re.search(r"\baac\b", full_ctx):
            result.quality_label = "AAC 5.1"
        elif is_multich_m4a:
            result.quality_label = "MULTICANAL"
        else:
            result.quality_label = "ULTRA HD" if (result.bit_depth >= 24 or result.size_bytes >= 80 * 1024 * 1024 or has_bluray) else "HD"

        if is_plain_ac3:
            ch = "5.1"
        elif re.search(r"7\.1", full_ctx) or (has_atmos and result.size_bytes >= 110 * 1024 * 1024):
            ch = "7.1"
        elif re.search(r"5\.1", full_ctx):
            ch = "5.1"
        elif is_multich_m4a:
            ch = "5.1"
        else:
            ch = "5.1 / 7.1"
        result.channels_label = ch

        if is_plain_ac3:
            br_tag = f" · {est_kbps}k" if 300 <= est_kbps <= 700 else ""
            result.format_label = f"AC3{br_tag}"
            result.codec_label = "Dolby Digital Surround 5.1"
        elif is_dts:
            result.format_label = "DTS-HD"
            result.codec_label = f"DTS-HD Surround {ch}"
        elif is_multich_m4a:
            br_tag = f" · {est_kbps}k" if 300 <= est_kbps <= 1200 else ""
            result.format_label = f"{ext_upper}{br_tag}"
            result.codec_label = f"Áudio Multicanal {ch} ({result.quality_label})"
        else:
            bd = f" {result.bit_depth}-bit" if result.bit_depth >= 24 else ""
            result.format_label = f"FLAC{bd}"
            result.codec_label = f"FLAC Multicanal {ch}"
        result.jbl_explanation = (
            f"Áudio multicanal discreto ({ch} canais, sem garantia de objetos Dolby Atmos de teto). "
            "O codec exato (AAC 5.1, AC-3 5.1 ou E-AC-3 JOC) é confirmado via ffprobe ao baixar."
        )
        result.tags = _build_tags(result)
        return result.codec_tier, result.codec_label

    # ── TIER 4: Lossless Estéreo (FLAC / WAV / ALAC em .m4a de alto bitrate!) ──
    # Um .m4a >= 18 MB (ou bitrate >= 800 kbps ou 24-bit/96kHz) é ALAC (Apple Lossless), não AAC 256k!
    is_alac_m4a = ext == ".m4a" and (
        has_hires_ctx
        or result.bit_depth >= 16
        or est_kbps >= 780
        or result.size_bytes >= 20 * 1024 * 1024
    )
    if ext in (".flac", ".wav", ".aiff", ".alac") or is_alac_m4a:
        result.codec_tier = 4
        result.spatial_label = "ESTÉREO"
        is_ultra = (
            result.bit_depth >= 24
            or result.sample_rate > 48000
            or result.provider == "http"
            or bool(re.search(r"24[-\s]?bit|96\s*khz|192\s*khz|hi[-\s]?res", full_ctx))
        )
        result.quality_label = "ULTRA HD" if is_ultra else "HD"
        result.channels_label = "2.0"

        if ext == ".flac":
            bits = "24-bit" if is_ultra else "16-bit"
            result.format_label = f"FLAC · {bits}" + (f"/{sr_str}" if sr_str else "")
            result.codec_label = f"FLAC {result.quality_label} Estéreo"
        elif is_alac_m4a:
            bits = "24-bit" if is_ultra else "ALAC"
            result.format_label = f"M4A · {bits}" + (f"/{sr_str}" if sr_str else "")
            result.codec_label = f"ALAC {result.quality_label} Estéreo"
        else:
            result.format_label = ext_upper
            result.codec_label = f"{ext_upper} Lossless Estéreo"

        result.jbl_explanation = (
            f"Áudio sem perdas ({result.quality_label} — equivalente ao Amazon Music {result.quality_label}). "
            "Estéreo puro 2.0 canais."
        )
        result.tags = _build_tags(result)
        return result.codec_tier, result.codec_label

    # ── TIER 5: Lossy comum (AAC 256k etc.) — descartado ──
    result.codec_tier = 5
    result.spatial_label = "ESTÉREO"
    result.quality_label = "SD"
    result.channels_label = "2.0"
    result.format_label = ext_upper
    result.codec_label = f"{ext_upper} Lossy"
    result.tags = _build_tags(result)
    return 5, result.codec_label


# ──────────────────────────────────────────────────────────────
# PONTUAÇÃO MULTICRITÉRIO + MATCHING DE TÍTULO/ARTISTA + ANTI-TYPO
# ──────────────────────────────────────────────────────────────

TIER_BASE_SCORE = {1: 96.0, 2: 100.0, 3: 76.0, 4: 54.0, 5: 10.0}

_TECH_NOISE_TOKENS = {
    "dolby", "atmos", "flac", "eac3", "m4a", "mp4", "mka", "audio", "truehd", "hires", "lossless",
}


def _clean_title_core(text: str) -> str:
    """Limpa apenas colchetes/parênteses de feat/versão/remaster, preservando todas as palavras do título ('the', 'no', etc.)."""
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", " ", text)
    # Remover sufixos tipo " - Single Version", " - Acoustic", " - 2011 Remaster", " - Slowed + Reverb"
    t = re.sub(
        r"\s*-\s*(?:single\s+version|radio\s+edit|acoustic|live|remaster\w*|slowed.*|instrumental|deluxe.*|explicit|clean)$",
        "",
        t,
        flags=re.I,
    )
    t = _strip_accents(t.lower())
    t = re.sub(r"[^a-z0-9]+", " ", t)
    words = [w for w in t.split() if w not in _TECH_NOISE_TOKENS]
    return " ".join(words).strip()


def _token_fuzzy_hit(q_tok: str, cand_tokens: set[str]) -> float:
    """Retorna 1.0 para match exato, 0.78..0.95 para match fuzzy (1–2 letras erradas), 0.0 caso contrário."""
    if q_tok in cand_tokens:
        return 1.0
    if len(q_tok) <= 2:
        # Palavras curtas ('the', 'no', 'my', 'in') exigem match exato para não confundir 'no' com 'on' ou 'to'!
        return 0.0
    best = 0.0
    for ct in cand_tokens:
        if abs(len(q_tok) - len(ct)) > 2:
            continue
        r = SequenceMatcher(None, q_tok, ct).ratio()
        if r >= 0.78 and r > best:
            best = r
    return best


def _title_similarity(expected_title: str, cand_title: str) -> float:
    """
    Compara especificamente o TÍTULO esperado contra o TÍTULO limpo da faixa.
    Preserva palavras essenciais ('the', 'no', 'my') e penaliza títulos com muitas palavras extras
    ('No Reason' vs 'The Reason' -> baixo; 'Epitaph Including March For No Reason' vs 'The Reason' -> muito baixo;
    'The Gallows' vs 'The Reason' -> 0.0).
    """
    exp = _clean_title_core(expected_title)
    cand = _clean_title_core(cand_title)
    if not exp or not cand:
        return 0.0
    if exp == cand:
        return 1.0

    exp_words = exp.split()
    cand_words = cand.split()
    cand_set = set(cand_words)

    # Recall (quantas palavras do título esperado aparecem no título candidato — incluindo 'the', 'no', etc.)
    hits = sum(_token_fuzzy_hit(w, cand_set) for w in exp_words)
    recall = hits / len(exp_words)

    # Precisão (penaliza fortemente títulos muito maiores com palavras não buscadas!)
    exp_set = set(exp_words)
    rev_hits = sum(_token_fuzzy_hit(w, exp_set) for w in cand_words)
    precision = rev_hits / len(cand_words)

    # Similaridade de sequência da frase inteira (captura ordem exata e typos leves)
    seq = SequenceMatcher(None, exp, cand).ratio()

    # Se o título esperado é curto (<= 3 palavras, ex: 'the reason', 'time', 'lose my mind'),
    # exigir que a maioria das palavras do título esperado esteja presente e sem excesso de ruído
    f1 = (2 * recall * precision / (recall + precision)) if (recall + precision) > 0 else 0.0
    sim = (0.55 * f1) + (0.25 * recall) + (0.20 * seq)

    # Penalidade explícita de negação/divergência em títulos curtos ('no reason' vs 'the reason')
    if len(exp_words) <= 3 and recall < 0.99 and seq < 0.80:
        sim *= 0.62

    return min(1.0, sim)


def _artist_matches(expected_artist: str, result: TrackResult) -> bool:
    if not expected_artist:
        return False
    primary = re.split(r"\s*(?:,|&|feat\.?|ft\.?|x|with)\s*", expected_artist, maxsplit=1, flags=re.I)[0].strip()
    art_words = [w for w in _clean_title_core(primary or expected_artist).split() if len(w) > 1]
    if not art_words:
        return False
    cand_ctx = _clean_title_core(f"{result.artist} {result.filename} {result.remote_path}")
    cand_set = set(cand_ctx.split())
    hits = sum(1 for w in art_words if _token_fuzzy_hit(w, cand_set) >= 0.82)
    return hits == len(art_words)


def _single_query_match(query: str, result: TrackResult) -> float:
    q_clean = _clean_title_core(query)
    q_words = q_clean.split()
    if not q_words:
        return 1.0

    track_words = set(_clean_title_core(f"{result.artist} {result.title} {result.filename}").split())
    title_words = set(_clean_title_core(result.title).split())

    track_hits = sum(_token_fuzzy_hit(w, track_words) for w in q_words)
    title_hits = sum(_token_fuzzy_hit(w, title_words) for w in q_words)

    # Exigir pelo menos 1 palavra relevante no TÍTULO da faixa (evita 'Pure Reason Revolution - The Gallows'!)
    if title_hits == 0.0:
        return 0.12

    t_sim = _title_similarity(query, result.title)
    full_sim = _title_similarity(query, f"{result.artist} {result.title}")
    coverage = track_hits / len(q_words)

    return min(1.0, max(t_sim, full_sim, (0.50 * coverage) + (0.50 * t_sim)))


def compute_query_match(
    query: str,
    result: TrackResult,
    canonical_query: str = "",
    canonical_title: str = "",
) -> float:
    """Calcula a aderência real da faixa considerando título exato, query original e correção canônica."""
    scores: list[float] = []
    if canonical_title:
        scores.append(_title_similarity(canonical_title, result.title))
    if query:
        scores.append(_single_query_match(query, result))
    if canonical_query and canonical_query.lower() != query.lower():
        scores.append(_single_query_match(canonical_query, result))
    return max(scores) if scores else 1.0


def _availability_score(result: TrackResult) -> float:
    if result.provider == "http":
        return 96.0

    base = 75.0 if result.has_free_slot else max(15.0, 45.0 - result.queue_length * 4.0)
    # Cap em 5 MB/s para evitar que bots anunciando "88.9 MB/s" falsos dominem o topo
    kbps = min(result.speed_bps / 1024.0, 5120.0)
    if kbps >= 3000:
        speed_bonus = 24.0
    elif kbps >= 1000:
        speed_bonus = 18.0
    elif kbps >= 300:
        speed_bonus = 10.0
    elif kbps > 0:
        speed_bonus = 4.0
    else:
        speed_bonus = -5.0

    return max(0.0, min(100.0, base + speed_bonus))


def _derive_canonical_title(query: str, canonical_query: str, canonical_artist: str, canonical_title: str) -> str:
    if canonical_title:
        return canonical_title.strip()
    if canonical_query and canonical_artist:
        cq = _clean_title_core(canonical_query)
        ca = _clean_title_core(canonical_artist)
        if ca and cq.startswith(ca + " "):
            return cq[len(ca):].strip()
    return query.strip()


def _compute_relevance_tier(
    result: TrackResult,
    query: str,
    canonical_query: str,
    canonical_artist: str,
    canonical_title: str,
) -> tuple[int, float]:
    """
    Classifica a relevância musical em tiers claros para que uma música ERRADA em Dolby Atmos
    NUNCA fique acima da música CERTA em Ultra HD / Surround:
      Tier 3: Música exata (título >= 0.82 E artista canônico bate — ou busca sem artista canônico e título exato)
      Tier 2: Título exato (>= 0.85), mas de outro artista (homônimo legítimo, ex: 'Baby Rose - The Reason')
      Tier 1: Match parcial próximo (ex: subtítulo extra ou versão/feat, título >= 0.58)
      Tier 0: Não corresponde à música buscada (descartado)
    """
    exp_title = _derive_canonical_title(query, canonical_query, canonical_artist, canonical_title)
    t_sim = _title_similarity(exp_title, result.title) if exp_title else 1.0
    # Também testar contra a query direta caso o usuário tenha digitado 'Artista Título' ou algo não resolvido
    q_sim = _single_query_match(query, result) if query else 1.0
    best_title_sim = max(t_sim, _title_similarity(query, result.title) if query else 0.0)

    art_hit = _artist_matches(canonical_artist, result) if canonical_artist else False

    # Se o usuário digitou o próprio artista + título ('wham last christmas'), checar também match combinado
    if art_hit and max(best_title_sim, q_sim) >= 0.78:
        # Penalizar levemente variações não pedidas ('Acoustic', 'Slowed + Reverb') frente à original
        low_t = _strip_accents(result.title.lower())
        low_q = _strip_accents(query.lower())
        unwanted_var = any(
            w in low_t and w not in low_q
            for w in ("acoustic", "acustico", "instrumental", "karaoke", "slowed", "nightcore", "8d", "remix", "cover")
        )
        return (2 if unwanted_var else 3), max(best_title_sim, q_sim)

    if not canonical_artist and best_title_sim >= 0.85:
        return 3, best_title_sim

    if best_title_sim >= 0.85:
        # Título idêntico ('The Reason'), mas de outro artista ('Baby Rose')
        return 2, best_title_sim

    if art_hit and max(best_title_sim, q_sim) >= 0.55:
        return 2, max(best_title_sim, q_sim)

    if best_title_sim >= 0.62:
        return 1, best_title_sim

    return 0, best_title_sim


def compute_score(
    result: TrackResult,
    query: str = "",
    canonical_query: str = "",
    canonical_artist: str = "",
    canonical_title: str = "",
) -> float:
    codec_pts = TIER_BASE_SCORE.get(result.codec_tier, 0.0)
    avail_pts = _availability_score(result)

    if query or canonical_query or canonical_title:
        rel_tier, rel_sim = _compute_relevance_tier(
            result,
            query=query,
            canonical_query=canonical_query,
            canonical_artist=canonical_artist,
            canonical_title=canonical_title,
        )
        result.match_score = rel_sim
        result.extra["relevance_tier"] = rel_tier
    else:
        rel_tier, rel_sim = 3, 1.0
        result.match_score = 1.0
        result.extra["relevance_tier"] = 3

    # Peso balanceado: relevância + qualidade + disponibilidade
    score = (codec_pts * 0.45) + (rel_sim * 100.0 * 0.35) + (avail_pts * 0.20)

    # Bônus para Dolby Atmos já verificado via ffprobe e para bitrate típico de E-AC-3 JOC (~768 kbps)
    if result.codec_tier in (1, 2) and result.extra.get("verified"):
        score += 6.0
    if result.codec_tier == 2:
        est = _estimate_kbps(result)
        if 720 <= est <= 860:
            score += 3.5
    elif result.codec_tier == 4 and result.quality_label == "ULTRA HD":
        # Dentro de Estéreo Lossless, ULTRA HD 24-bit vence HD 16-bit
        score += 4.5

    if rel_tier == 3:
        score += 10.0
    elif rel_tier == 1:
        score *= 0.72
    elif rel_tier == 0:
        score *= 0.35

    return round(min(100.0, max(0.0, score)), 1)


def rank_results(
    results: list[TrackResult],
    query: str = "",
    drop_junk: bool = True,
    canonical_query: str = "",
    canonical_artist: str = "",
    canonical_title: str = "",
) -> list[TrackResult]:
    valid: list[TrackResult] = []
    junk_list: list[TrackResult] = []

    for r in results:
        if not r.size_label and r.size_bytes > 0:
            r.size_label = format_size(r.size_bytes)

        junk, reason = is_junk(r)
        r.is_junk = junk
        r.junk_reason = reason

        detect_codec(r)

        if r.codec_tier == 5:
            r.is_junk = True
            r.junk_reason = "Áudio comprimido sem Dolby Atmos"

        r.score = compute_score(
            r,
            query=query,
            canonical_query=canonical_query,
            canonical_artist=canonical_artist,
            canonical_title=canonical_title,
        )

        rel_tier = int(r.extra.get("relevance_tier", 3))
        if (query or canonical_query or canonical_title) and rel_tier == 0:
            r.is_junk = True
            r.junk_reason = "Título não corresponde à música buscada"

        if r.is_junk:
            junk_list.append(r)
        else:
            valid.append(r)

    # Ordenação hierárquica rígida:
    #   1º Relevância musical (música certa SEMPRE acima de música diferente, mesmo se outra tiver Atmos!)
    #   2º Tier de áudio (Dolby Atmos 7.1.4 > Surround 5.1/7.1 > Ultra HD > HD)
    #   3º Verificado via ffprobe antes de não-verificado (no mesmo tier)
    #   4º Score geral + slot livre + velocidade (com cap anti-bot de 5 MB/s)
    tier_priority = {2: 4, 1: 3, 3: 2, 4: 1, 5: 0}
    valid.sort(
        key=lambda r: (
            int(r.extra.get("relevance_tier", 0)),
            tier_priority.get(r.codec_tier, 0),
            1 if (r.codec_tier in (1, 2) and r.extra.get("verified")) else 0,
            1 if r.quality_label in ("LOSSLESS", "ULTRA HD") else 0,
            r.score,
            1 if r.has_free_slot else 0,
            min(r.speed_bps, 5 * 1024 * 1024),
        ),
        reverse=True,
    )

    # Deduplicação + anti-flood de bots (máx 3 faixas não-Atmos de um mesmo usuário Soulseek)
    deduped: list[TrackResult] = []
    seen_sigs: dict[str, int] = {}
    peer_stereo_counts: dict[str, int] = {}

    for r in valid:
        sig = f"{r.provider}|{r.display_name.lower()}|{r.codec_tier}|{r.size_bytes // (1024 * 512)}"
        if seen_sigs.get(sig, 0) >= 2:
            continue

        if r.provider == "soulseek" and not r.is_atmos and r.remote_user:
            if peer_stereo_counts.get(r.remote_user, 0) >= 3:
                continue
            peer_stereo_counts[r.remote_user] = peer_stereo_counts.get(r.remote_user, 0) + 1

        seen_sigs[sig] = seen_sigs.get(sig, 0) + 1
        deduped.append(r)

    if drop_junk:
        return deduped
    return deduped + junk_list
