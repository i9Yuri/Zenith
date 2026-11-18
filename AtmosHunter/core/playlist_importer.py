# Copyright (c) 2026 Yuri Barbosa (@i9Yuri). All Rights Reserved.
# Proprietary source-available software (personal, non-commercial use only).
# Official repository: https://github.com/i9Yuri/Zenith
# Unauthorized commercial use, redistribution, or rebranding is prohibited. See LICENSE.
"""
AtmosHunter — Importador Inteligente de Playlists (v2.4)
Suporta:
  - Spotify (playlists e álbuns públicos via embed __NEXT_DATA__)
  - YouTube / YouTube Music (playlists via yt-dlp --flat-playlist)
  - Amazon Music (playlists e álbuns públicos via DOM renderizado)
  - Deezer (playlists e álbuns públicos via API oficial com paginação)
  - Apple Music (playlists e álbuns públicos via serialized-server-data)
  - Arquivos locais (.m3u, .m3u8, .csv, .txt) e lista colada manualmente

Recursos:
  - Extração precisa de título, artista, álbum, duração e VARIAÇÃO específica
    (Live, Acoustic, Remix, Pudding Mix, Radio Edit, Remaster, Instrumental, etc.)
  - Busca e seleção otimizada para JBL Bar 1000MK2 (ATMOS 3D 7.1.4 > SURROUND 7.1/5.1 > ULTRA HD > HD)
  - Organização em pasta dedicada ~/Music/DolbyAtmos/<Playlist>/ com numeração + .m3u
  - Failover automático entre peers e fontes
  - Relatório detalhado e persistido (.atmoshunter_playlist.json) com erros e rebaixamentos
"""
from __future__ import annotations

import csv
import html as html_lib
import io
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import requests

from core.dlna_bridge import _build_library_tags, _probe_file_full
from core.downloader import (
    DOWNLOAD_DIR,
    DownloadManager,
    DownloadStatus,
    _sanitize_filename,
)
from core.query_resolver import ResolvedQuery, resolve_music_query
from core.ranker import (
    AudioTag,
    TrackResult,
    _normalize_tokens,
    _strip_accents,
    rank_results,
)
from core.search import SearchEngine, _ascii_clean
from core.slsk_service import SoulseekService, get_slsk_service

logger = logging.getLogger("AtmosHunter.playlist")

REPORT_FILENAME = ".atmoshunter_playlist.json"

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.8",
}

# ──────────────────────────────────────────────────────────────
# MODELOS DE DADOS
# ──────────────────────────────────────────────────────────────

@dataclass
class PlaylistTrack:
    index: int
    title: str
    artist: str = ""
    album: str = ""
    variation: str = ""
    duration_sec: int = 0
    isrc: str = ""
    source_url: str = ""
    selected: bool = True

    @property
    def base_title(self) -> str:
        """Título limpo sem marcadores de vídeo/feat, preservando a essência da faixa."""
        return strip_variation_brackets(self.title)

    @property
    def display_name(self) -> str:
        if self.artist and self.title:
            if self.artist.lower() in self.title.lower():
                return self.title
            return f"{self.artist} - {self.title}"
        return self.title or self.artist or f"Faixa {self.index}"

    @property
    def search_query(self) -> str:
        core = f"{self.artist} {self.base_title}".strip() if self.artist else self.base_title
        if self.variation:
            # Incluir palavra-chave principal da variação quando ela muda a gravação
            primary_var = _primary_variation_keyword(self.variation)
            if primary_var and primary_var.lower() not in core.lower():
                return f"{core} {primary_var}".strip()
        return core


@dataclass
class ParsedPlaylist:
    name: str
    source_platform: str
    source_url: str = ""
    tracks: list[PlaylistTrack] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# Códigos de status por faixa na importação
STATUS_PENDING = "pending"
STATUS_SEARCHING = "searching"
STATUS_DOWNLOADING = "downloading"
STATUS_OPTIMAL_ATMOS = "optimal_atmos"
STATUS_DOWNGRADED_SURROUND = "downgraded_surround"
STATUS_DOWNGRADED_STEREO = "downgraded_stereo"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_CANCELLED = "cancelled"


@dataclass
class TrackImportReport:
    index: int
    requested_artist: str
    requested_title: str
    requested_album: str = ""
    requested_variation: str = ""
    status: str = STATUS_PENDING
    status_label: str = "Aguardando"
    achieved_spatial: str = ""
    achieved_quality: str = ""
    achieved_channels: str = ""
    verified_audio: str = ""
    matched_name: str = ""
    saved_filename: str = ""
    saved_path: str = ""
    source_used: str = ""
    variation_mismatch: bool = False
    issue_message: str = ""
    progress: float = 0.0
    tags: list[AudioTag] = field(default_factory=list)

    @property
    def has_issue(self) -> bool:
        return (
            self.status in (STATUS_DOWNGRADED_SURROUND, STATUS_DOWNGRADED_STEREO, STATUS_FAILED)
            or self.variation_mismatch
            or bool(self.issue_message)
        )

    def to_json_dict(self) -> dict:
        d = asdict(self)
        d["tags"] = [{"text": t.text, "kind": t.kind} for t in self.tags]
        return d

    @classmethod
    def from_json_dict(cls, data: dict) -> "TrackImportReport":
        raw_tags = data.get("tags") or []
        tags = [AudioTag(str(t.get("text", "")), str(t.get("kind", "format"))) for t in raw_tags if isinstance(t, dict)]
        clean = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "tags"}
        return cls(**clean, tags=tags)


@dataclass
class PlaylistImportReport:
    playlist_name: str
    source_platform: str
    source_url: str
    folder_path: str
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
    parse_warnings: list[str] = field(default_factory=list)
    tracks: list[TrackImportReport] = field(default_factory=list)
    cancelled: bool = False

    @property
    def total(self) -> int:
        return len(self.tracks)

    @property
    def count_atmos(self) -> int:
        return sum(1 for t in self.tracks if t.status == STATUS_OPTIMAL_ATMOS)

    @property
    def count_surround(self) -> int:
        return sum(1 for t in self.tracks if t.status == STATUS_DOWNGRADED_SURROUND)

    @property
    def count_stereo(self) -> int:
        return sum(1 for t in self.tracks if t.status == STATUS_DOWNGRADED_STEREO)

    @property
    def count_failed(self) -> int:
        return sum(1 for t in self.tracks if t.status == STATUS_FAILED)

    @property
    def count_variation_mismatch(self) -> int:
        return sum(1 for t in self.tracks if t.variation_mismatch)

    @property
    def count_downloaded(self) -> int:
        return self.count_atmos + self.count_surround + self.count_stereo

    @property
    def issues(self) -> list[TrackImportReport]:
        return [t for t in self.tracks if t.has_issue]

    def save(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / REPORT_FILENAME
        payload = {
            "playlist_name": self.playlist_name,
            "source_platform": self.source_platform,
            "source_url": self.source_url,
            "folder_path": str(folder),
            "created_at": self.created_at,
            "parse_warnings": self.parse_warnings,
            "cancelled": self.cancelled,
            "tracks": [t.to_json_dict() for t in self.tracks],
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    @classmethod
    def load(cls, folder: Path) -> Optional["PlaylistImportReport"]:
        fp = folder / REPORT_FILENAME
        if not fp.exists():
            return None
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            tracks = [TrackImportReport.from_json_dict(t) for t in (data.get("tracks") or []) if isinstance(t, dict)]
            return cls(
                playlist_name=str(data.get("playlist_name") or folder.name),
                source_platform=str(data.get("source_platform") or "Playlist"),
                source_url=str(data.get("source_url") or ""),
                folder_path=str(data.get("folder_path") or folder),
                created_at=str(data.get("created_at") or ""),
                parse_warnings=list(data.get("parse_warnings") or []),
                tracks=tracks,
                cancelled=bool(data.get("cancelled", False)),
            )
        except Exception as e:
            logger.debug(f"Falha ao ler relatório {fp}: {e}")
            return None


# ──────────────────────────────────────────────────────────────
# DETECÇÃO E LIMPEZA DE VARIAÇÕES MUSICAIS
# ──────────────────────────────────────────────────────────────

# Ruído exclusivo de vídeo (YouTube) — SEMPRE removido
VIDEO_NOISE_RE = re.compile(
    r"\s*[\(\[](?:official\s*(?:music\s*)?video|official\s*(?:hd\s*|4k\s*)?(?:audio|video|visualizer|lyric\s*video)|"
    r"lyric\s*video|lyrics?|audio\s*only|visualizer|music\s*video|hd|4k|hq|mv|clipe\s*oficial|"
    r"v[ií]deo\s*oficial|[áa]udio\s*oficial|explicit|clean\s*version)[\)\]]",
    re.I,
)

# Palavras-chave que definem uma VARIAÇÃO musical real (que muda a gravação/mix)
VARIATION_KEYWORDS: list[tuple[str, str]] = [
    (r"\bpudding\s+mix\b", "Pudding Mix"),
    (r"\b(?:ao\s+vivo|live(?:\s+(?:at|in|from|on)\s+[^)\]\-]+)?)\b", "Live"),
    (r"\b(?:ac[úu]stic[oa]?|acoustic|unplugged)\b", "Acoustic"),
    (r"\b(?:remix|rework|bootleg|club\s+mix|dub\s+mix|vip\s+mix)\b", "Remix"),
    (r"\b(?:extended(?:\s+(?:mix|version))?|12[\"']?\s*(?:mix|version))\b", "Extended"),
    (r"\b(?:radio\s+edit|single\s+version|single\s+edit|7[\"']?\s*(?:mix|version))\b", "Single/Radio Edit"),
    (r"\b(?:instrumental|karaoke)\b", "Instrumental"),
    (r"\b(?:a\s*capp?ella)\b", "Acapella"),
    (r"\b(?:demo(?:\s+version)?)\b", "Demo"),
    (r"\b(?:orchestral|symphonic|piano\s+version|stripped)\b", "Alternate Version"),
    (r"\b(?:slowed|sped\s+up|nightcore)\b", "Speed Edit"),
    (r"\b(?:(?:19|20)\d{2}\s+remaster(?:ed)?|remaster(?:ed)?(?:\s+(?:19|20)\d{2})?)\b", "Remaster"),
    (r"\b(?:deluxe|bonus\s+track|anniversary\s+edition)\b", "Edition"),
    (r"\b(?:dolby\s+atmos|spatial\s+audio|atmos\s+mix)\b", ""),
]

# Variações que ALTERAM drasticamente a faixa (se a playlist NÃO pediu, devemos penalizar no candidato!)
ALTERNATE_RECORDING_MARKERS = [
    "live", "ao vivo", "acoustic", "acustico", "acustica", "unplugged",
    "remix", "club mix", "dub mix", "extended", "pudding mix",
    "instrumental", "karaoke", "acapella", "a cappella", "demo",
    "slowed", "sped up", "nightcore", "orchestral", "stripped", "piano version", "cover",
]


def clean_video_noise(title: str) -> str:
    """Remove ruído de YouTube ('Official Music Video', 'Lyric Video') mantendo variações reais."""
    t = VIDEO_NOISE_RE.sub("", title)
    t = re.sub(r"\s+\|\s+.*$", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -|")
    return t


def clean_artist_name(artist: str) -> str:
    """Limpa sufixos automáticos de canais (' - Topic', 'VEVO', '\xa0')."""
    a = artist.replace("\xa0", " ").strip()
    a = re.sub(r"\s*-\s*Topic\s*$", "", a, flags=re.I)
    a = re.sub(r"(?<=[a-z0-9])VEVO$", "", a)
    a = re.sub(r"\s+", " ", a).strip(" ,-")
    return a


def extract_variation(title: str) -> str:
    """
    Extrai o descritor exato de variação da faixa (ex: 'Live at Wembley', 'Pudding Mix',
    'Acoustic', 'Tiësto Remix', '2023 Remaster', 'Radio Edit').
    Retorna string vazia para a versão padrão de estúdio.
    """
    found: list[str] = []

    # 1. Checar blocos entre parênteses / colchetes
    for m in re.finditer(r"[\(\[]([^\)\]]+)[\)\]]", title):
        inner = m.group(1).strip()
        low = _strip_accents(inner.lower())
        # Ignorar meras participações (feat. ...) ou trilha sonora (From ...) como variação de mix
        if re.match(r"^(?:feat\.?|ft\.?|with|from\s+|explicit|clean)\b", low):
            continue
        for pat, label in VARIATION_KEYWORDS:
            if re.search(pat, inner, re.I):
                if label:  # ignora "dolby atmos"
                    found.append(inner)
                break
        else:
            if re.search(r"\b(mix|version|edit|session|reprise|interlude|pt\.?\s*\d|part\s*\d)\b", low):
                found.append(inner)

    # 2. Checar sufixo após " - " (padrão Spotify/Deezer: "Blinding Lights - Live" ou "Title - 2011 Remaster")
    if " - " in title:
        tail = title.rsplit(" - ", 1)[1].strip()
        # Só considerar tail como sufixo de variação se NÃO contiver parênteses já capturados
        # e se o próprio texto do tail for um descritor curto de variação (<= 5 palavras)
        if "(" not in tail and "[" not in tail and len(tail.split()) <= 6:
            for pat, label in VARIATION_KEYWORDS:
                if re.search(pat, tail, re.I):
                    if label and tail not in found:
                        found.append(tail)
                    break

    return " · ".join(found)


def strip_variation_brackets(title: str) -> str:
    """Remove parênteses/colchetes de feat/variação e sufixo ' - Remaster/Live' para obter o núcleo do título."""
    t = clean_video_noise(title)
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t)
    if " - " in t:
        head, tail = t.rsplit(" - ", 1)
        if len(tail.split()) <= 6 and any(re.search(pat, tail, re.I) for pat, _ in VARIATION_KEYWORDS):
            t = head
    return re.sub(r"\s+", " ", t).strip(" -")


def _primary_variation_keyword(variation: str) -> str:
    """Retorna a palavra-chave curta da variação para enriquecer a query de busca."""
    low = _strip_accents(variation.lower())
    for key in (
        "pudding mix", "live", "ao vivo", "acoustic", "acustico", "unplugged",
        "remix", "extended", "instrumental", "acapella", "demo", "radio edit",
        "single version", "orchestral", "stripped",
    ):
        if key in low:
            return key
    # Se for apenas remaster ("2011 Remaster"), não engessar a busca Soulseek pois Atmos é mix novo
    if "remaster" in low or "edition" in low or "deluxe" in low:
        return ""
    tokens = _normalize_tokens(variation)
    return " ".join(tokens[:2]) if tokens else ""


def _is_meaningful_recording_variation(variation: str) -> bool:
    """True se a variação exige uma gravação distinta (Live/Remix/Acoustic/Pudding Mix/Instrumental/Extended)."""
    if not variation:
        return False
    low = _strip_accents(variation.lower())
    # Remaster / Single Version / Deluxe geralmente compartilham o mesmo master Dolby Atmos
    if re.fullmatch(r"[\d\s]*(?:remaster(?:ed)?|deluxe|anniversary|edition|bonus\s+track|single\s+version|radio\s+edit|explicit|clean)[\d\s]*", low):
        return False
    return True


def _is_pure_variation_suffix(text: str) -> bool:
    """True somente se o texto inteiro após ' - ' for uma variação pura (ex: 'Live', '2011 Remaster', 'Acoustic')."""
    s = text.strip()
    if not s or "(" in s or "[" in s or len(s.split()) > 5:
        return False
    return any(re.search(pat, s, re.I) for pat, _ in VARIATION_KEYWORDS)


def _split_combined_artist_title(raw_title: str, fallback_artist: str = "") -> tuple[str, str]:
    """Separa 'Artista - Título (Variação)' vindo do YouTube ou de lista colada."""
    cleaned = clean_video_noise(raw_title)
    fb_artist = clean_artist_name(fallback_artist)

    for sep in (" - ", " – ", " — "):
        if sep in cleaned:
            left, right = cleaned.split(sep, 1)
            left, right = left.strip(), right.strip()
            # Só evitar split se 'right' for puramente uma variação ("Blinding Lights - Live")
            # e não um título real com variação ("Wham! - Last Christmas (Pudding Mix)")
            if left and right and not _is_pure_variation_suffix(right):
                return clean_artist_name(left), right
            elif left and right and fb_artist and _strip_accents(fb_artist.lower()) in _strip_accents(left.lower()):
                return clean_artist_name(left), right

    return fb_artist, cleaned


# ──────────────────────────────────────────────────────────────
# EXTRACTORES DE PLAYLIST (MULTI-PLATAFORMA)
# ──────────────────────────────────────────────────────────────

def _parse_spotify(url: str) -> ParsedPlaylist:
    m = re.search(r"open\.spotify\.com/(?:intl-[a-z]{2}/)?(?:embed/)?(playlist|album)/([A-Za-z0-9]+)", url)
    if not m:
        raise ValueError("Link do Spotify inválido. Use um link de playlist ou álbum público do Spotify.")
    kind, item_id = m.group(1), m.group(2)
    embed_url = f"https://open.spotify.com/embed/{kind}/{item_id}"

    r = requests.get(embed_url, headers=HTTP_HEADERS, timeout=14)
    r.encoding = "utf-8"
    if r.status_code != 200:
        raise RuntimeError(f"Spotify retornou HTTP {r.status_code}. Verifique se a playlist é pública.")

    nm = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.S)
    if not nm:
        raise RuntimeError("Não foi possível ler os dados públicos do Spotify (a playlist pode ser privada).")

    data = json.loads(nm.group(1))
    entity = (
        data.get("props", {})
        .get("pageProps", {})
        .get("state", {})
        .get("data", {})
        .get("entity", {})
    )
    if not entity:
        raise RuntimeError("Playlist do Spotify vazia ou inacessível (confirme se está marcada como pública).")

    pl_name = str(entity.get("name") or f"Spotify {kind.title()}").strip()
    album_default = pl_name if kind == "album" else ""
    raw_tracks = entity.get("trackList") or []

    tracks: list[PlaylistTrack] = []
    for idx, t in enumerate(raw_tracks, start=1):
        title = str(t.get("title") or "").replace("\xa0", " ").strip()
        artist = clean_artist_name(str(t.get("subtitle") or ""))
        if not title:
            continue
        dur_ms = int(t.get("duration") or 0)
        dur_sec = dur_ms // 1000 if dur_ms > 1000 else dur_ms
        tracks.append(
            PlaylistTrack(
                index=idx,
                title=title,
                artist=artist,
                album=album_default,
                variation=extract_variation(title),
                duration_sec=dur_sec,
                source_url=str(t.get("uri") or ""),
            )
        )

    warnings: list[str] = []
    if len(tracks) == 100:
        warnings.append(
            "O embed público do Spotify expõe até 100 faixas por playlist sem login. "
            "Se a sua playlist tiver mais de 100 faixas, exporte um CSV/lista ou divida em partes."
        )
    if not tracks:
        raise RuntimeError("Nenhuma faixa encontrada nesta playlist do Spotify.")

    return ParsedPlaylist(
        name=pl_name,
        source_platform="Spotify",
        source_url=url,
        tracks=tracks,
        warnings=warnings,
    )


def _parse_deezer(url: str) -> ParsedPlaylist:
    # Resolver links encurtados (deezer.page.link / link.deezer.com)
    resolved_url = url
    if "page.link" in url or "link.deezer.com" in url:
        try:
            rr = requests.get(url, headers=HTTP_HEADERS, allow_redirects=True, timeout=10)
            resolved_url = rr.url
        except Exception:
            pass

    m = re.search(r"deezer\.com/(?:[a-z]{2}/)?(playlist|album)/(\d+)", resolved_url)
    if not m:
        raise ValueError("Link do Deezer inválido. Use um link de playlist ou álbum público do Deezer.")
    kind, item_id = m.group(1), m.group(2)

    api_url = f"https://api.deezer.com/{kind}/{item_id}"
    r = requests.get(api_url, headers=HTTP_HEADERS, timeout=12)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Deezer retornou erro: {data['error'].get('message', 'playlist inacessível')}.")

    pl_name = str(data.get("title") or f"Deezer {kind.title()}").strip()
    album_default = pl_name if kind == "album" else ""
    tracks_obj = data.get("tracks") or {}
    raw_items = list(tracks_obj.get("data") or [])

    # Paginação automática (até 300 faixas)
    next_url = tracks_obj.get("next")
    pages = 0
    while next_url and pages < 8:
        pages += 1
        try:
            rn = requests.get(next_url, headers=HTTP_HEADERS, timeout=10)
            rn.raise_for_status()
            nd = rn.json()
            raw_items.extend(nd.get("data") or [])
            next_url = nd.get("next")
        except Exception:
            break

    tracks: list[PlaylistTrack] = []
    for idx, it in enumerate(raw_items, start=1):
        title = str(it.get("title") or "").strip()
        artist = clean_artist_name(str((it.get("artist") or {}).get("name") or ""))
        album = str((it.get("album") or {}).get("title") or album_default).strip()
        if not title:
            continue
        tracks.append(
            PlaylistTrack(
                index=idx,
                title=title,
                artist=artist,
                album=album,
                variation=extract_variation(title),
                duration_sec=int(it.get("duration") or 0),
                isrc=str(it.get("isrc") or ""),
                source_url=str(it.get("link") or ""),
            )
        )

    if not tracks:
        raise RuntimeError("Nenhuma faixa encontrada na playlist do Deezer.")

    return ParsedPlaylist(
        name=pl_name,
        source_platform="Deezer",
        source_url=url,
        tracks=tracks,
    )


def _find_ytdlp_bin() -> Optional[str]:
    for cand in (str(Path.home() / ".local" / "bin" / "yt-dlp"), shutil.which("yt-dlp")):
        if cand and Path(cand).exists():
            return cand
    return None


def _parse_youtube_or_generic_ytdlp(url: str, platform_label: str = "YouTube Music") -> ParsedPlaylist:
    ytdlp = _find_ytdlp_bin()
    if not ytdlp:
        raise RuntimeError("Binário yt-dlp não encontrado no sistema para ler playlists do YouTube.")

    # Se for link de vídeo com ?list=..., normalizar para URL de playlist
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    target_url = url
    if "list" in qs and ("watch" in parsed.path or "youtu.be" in parsed.netloc):
        host = "music.youtube.com" if "music.youtube" in parsed.netloc else "www.youtube.com"
        target_url = f"https://{host}/playlist?list={qs['list'][0]}"

    from core.slsk_service import STAGING_DIR
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [
            ytdlp,
            "--flat-playlist",
            "--no-warnings",
            "--no-cache-dir",
            "--paths", f"home:{STAGING_DIR}",
            "--paths", f"temp:{STAGING_DIR}",
            "-J",
            target_url,
        ],
        cwd=str(STAGING_DIR),
        capture_output=True,
        text=True,
        timeout=45,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        err = (proc.stderr or "Erro desconhecido").strip().splitlines()[-1]
        raise RuntimeError(f"Falha ao ler playlist no {platform_label}: {err}")

    data = json.loads(proc.stdout)
    pl_name = str(data.get("title") or f"Playlist {platform_label}").strip()
    entries = data.get("entries") or []

    tracks: list[PlaylistTrack] = []
    warnings: list[str] = []
    skipped_unavail = 0

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_t = str(entry.get("title") or "").strip()
        if not raw_t or raw_t in ("[Deleted video]", "[Private video]", "Deleted video", "Private video"):
            skipped_unavail += 1
            continue

        uploader = str(entry.get("artist") or entry.get("creator") or entry.get("uploader") or entry.get("channel") or "")
        artist, title = _split_combined_artist_title(raw_t, fallback_artist=uploader)
        album = str(entry.get("album") or "").strip()
        dur = 0
        try:
            dur = int(float(entry.get("duration") or 0))
        except Exception:
            pass

        tracks.append(
            PlaylistTrack(
                index=len(tracks) + 1,
                title=title,
                artist=artist,
                album=album,
                variation=extract_variation(title),
                duration_sec=dur,
                source_url=str(entry.get("url") or entry.get("webpage_url") or ""),
            )
        )

    if skipped_unavail:
        warnings.append(f"{skipped_unavail} vídeo(s) privado(s) ou excluído(s) foram ignorados na playlist.")
    if not tracks:
        raise RuntimeError(f"Nenhuma faixa pública encontrada nesta playlist do {platform_label}.")

    return ParsedPlaylist(
        name=pl_name,
        source_platform=platform_label,
        source_url=url,
        tracks=tracks,
        warnings=warnings,
    )


def _parse_apple_music(url: str) -> ParsedPlaylist:
    r = requests.get(url, headers=HTTP_HEADERS, timeout=14)
    r.encoding = "utf-8"
    if r.status_code != 200:
        raise RuntimeError(f"Apple Music retornou HTTP {r.status_code}.")

    # Nome da playlist via <title>
    pl_name = "Apple Music Playlist"
    tm = re.search(r"<title>(.*?)</title>", r.text, re.S)
    if tm:
        raw_title = html_lib.unescape(tm.group(1)).replace("\u200e", "").strip()
        raw_title = re.sub(r"\s*[-–—]\s*(?:Playlist|Album|Álbum)\s*[-–—]\s*Apple Music.*$", "", raw_title, flags=re.I)
        raw_title = re.sub(r"\s*on Apple Music.*$", "", raw_title, flags=re.I).strip()
        if raw_title:
            pl_name = raw_title

    sm = re.search(r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', r.text, re.S)
    if not sm:
        raise RuntimeError("Não foi possível ler os dados públicos do Apple Music para este link.")

    data = json.loads(sm.group(1))
    found_objs: list[dict] = []

    def _walk(obj):
        if isinstance(obj, dict):
            if "title" in obj and "duration" in obj and ("subtitleLinks" in obj or "artistName" in obj):
                found_objs.append(obj)
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for x in obj:
                _walk(x)

    _walk(data)

    seen_ids: set[str] = set()
    tracks: list[PlaylistTrack] = []
    for obj in found_objs:
        oid = str(obj.get("id") or f"{obj.get('title')}|{obj.get('duration')}")
        if oid in seen_ids:
            continue
        seen_ids.add(oid)

        title = str(obj.get("title") or "").strip()
        if not title:
            continue

        artist = str(obj.get("artistName") or "").strip()
        if not artist:
            sub_links = obj.get("subtitleLinks") or []
            artist = ", ".join(str(sl.get("title") or "").strip() for sl in sub_links if isinstance(sl, dict) and sl.get("title"))
        artist = clean_artist_name(artist)

        album = ""
        tert_links = obj.get("tertiaryLinks") or []
        if tert_links and isinstance(tert_links[0], dict):
            album = str(tert_links[0].get("title") or "").strip()

        dur_ms = int(obj.get("duration") or 0)
        dur_sec = dur_ms // 1000 if dur_ms > 1000 else dur_ms

        tracks.append(
            PlaylistTrack(
                index=len(tracks) + 1,
                title=title,
                artist=artist,
                album=album,
                variation=extract_variation(title),
                duration_sec=dur_sec,
            )
        )

    if not tracks:
        raise RuntimeError("Nenhuma faixa encontrada neste link do Apple Music.")

    return ParsedPlaylist(
        name=pl_name,
        source_platform="Apple Music",
        source_url=url,
        tracks=tracks,
    )


def _find_headless_browser() -> Optional[str]:
    for cand in (
        str(Path.home() / ".local" / "bin" / "brave-browser"),
        shutil.which("brave-browser"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
    ):
        if cand and Path(cand).exists():
            return cand
    return None


def _parse_amazon_music(url: str) -> ParsedPlaylist:
    browser = _find_headless_browser()
    if not browser:
        raise RuntimeError(
            "Amazon Music carrega a playlist via JavaScript e nenhum navegador Chromium/Brave foi encontrado. "
            "Use a opção 'Colar lista de músicas' como alternativa."
        )

    dom = ""
    tracks: list[PlaylistTrack] = []
    pl_name = "Amazon Music Playlist"

    from core.slsk_service import STAGING_DIR
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    # Até 2 tentativas aumentando o tempo de hidratação da SPA (Amazon GraphQL pode oscilar 2-5s)
    for budget_ms in (16000, 24000):
        proc = subprocess.run(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--window-size=1280,14000",
                f"--virtual-time-budget={budget_ms}",
                "--dump-dom",
                url,
            ],
            cwd=str(STAGING_DIR),
            capture_output=True,
            text=True,
            timeout=45,
        )
        dom = proc.stdout or ""
        if not dom or "captcha" in dom.lower() or "validatecaptcha" in dom.lower():
            raise RuntimeError(
                "A página da Amazon Music exigiu verificação ou login para este link. "
                "Verifique se a playlist é pública ou cole a lista de músicas na aba abaixo."
            )

        # Extrair título real da playlist (headline do header > h1 > <title>)
        hm = re.search(r'<music-detail-header[^>]*\bheadline="([^"]+)"', dom)
        h1m = re.search(r"<h1[^>]*>(.*?)</h1>", dom, re.S)
        tm = re.search(r"<title>(.*?)</title>", dom, re.S)
        for raw_cand in (
            hm.group(1) if hm else "",
            re.sub(r"<[^>]+>", "", h1m.group(1)) if h1m else "",
            tm.group(1) if tm else "",
        ):
            t = html_lib.unescape(raw_cand).strip()
            t = re.sub(r"\s*(?:Playlist\s+)?on Amazon Music.*$", "", t, flags=re.I)
            t = re.sub(r"\s*no Amazon Music.*$", "", t, flags=re.I)
            t = t.strip(" |-")
            if t and t.lower() not in ("amazon music", "amazon music unlimited"):
                pl_name = t
                break

        tracks = []
        seen: set[str] = set()
        # Amazon Music usa <music-image-row> (playlists) e <music-text-row> (álbuns)
        for row_tag in re.findall(r"<music-(?:image|text)-row\b[^>]*>", dom):
            p_m = re.search(r'primary-text="([^"]+)"', row_tag)
            if not p_m:
                continue
            title = html_lib.unescape(p_m.group(1)).strip()
            a_m = re.search(r'secondary-text-1="([^"]+)"', row_tag)
            alb_m = re.search(r'secondary-text-2="([^"]+)"', row_tag)
            artist = clean_artist_name(html_lib.unescape(a_m.group(1))) if a_m else ""
            album = html_lib.unescape(alb_m.group(1)).strip() if alb_m else ""

            if not artist and "trackAsin=" not in row_tag and "/albums/" not in row_tag:
                continue

            sig = f"{artist.lower()}|{title.lower()}"
            if sig in seen:
                continue
            seen.add(sig)

            tracks.append(
                PlaylistTrack(
                    index=len(tracks) + 1,
                    title=title,
                    artist=artist,
                    album=album,
                    variation=extract_variation(title),
                )
            )
        if tracks:
            break

    if not tracks:
        raise RuntimeError(
            "Não foram encontradas faixas públicas neste link da Amazon Music "
            "(verifique se a playlist está compartilhada como pública ou cole a lista de músicas)."
        )

    warnings: list[str] = []
    if len(tracks) >= 55:
        warnings.append(
            f"Amazon Music renderizou {len(tracks)} faixas públicas visíveis. "
            "Se a playlist original for muito longa (>60 faixas), confira se todas constam na lista abaixo."
        )

    return ParsedPlaylist(
        name=pl_name,
        source_platform="Amazon Music",
        source_url=url,
        tracks=tracks,
        warnings=warnings,
    )


def _parse_text_or_file(raw_text: str, custom_name: str = "") -> ParsedPlaylist:
    """
    Aceita:
      - Caminho para arquivo local (.m3u, .m3u8, .csv Exportify/Soundiiz, .txt)
      - Texto colado com 1 música por linha ('Artista - Título (Variação)' ou '1. Título - Artista')
    """
    text = raw_text.strip()
    source_platform = "Lista de Músicas"
    pl_name = custom_name.strip() or f"Playlist {datetime.now().strftime('%d-%m %H%M')}"
    source_url = ""

    # Se for caminho de arquivo existente no disco
    if "\n" not in text and len(text) < 300:
        cand_path = Path(text.strip('"').strip("'")).expanduser()
        if cand_path.is_file():
            source_url = str(cand_path)
            pl_name = custom_name.strip() or cand_path.stem
            ext = cand_path.suffix.lower()
            content = cand_path.read_text(encoding="utf-8", errors="ignore")
            if ext == ".csv":
                return _parse_csv_content(content, pl_name, source_url)
            if ext in (".m3u", ".m3u8"):
                source_platform = "Arquivo M3U"
                text = content
            else:
                source_platform = "Arquivo de Texto"
                text = content

    # Detectar se o conteúdo colado é um CSV (ex: Exportify)
    first_line = text.splitlines()[0].lower() if text.splitlines() else ""
    if "," in first_line and ("track name" in first_line or "artist name" in first_line or "title,artist" in first_line):
        return _parse_csv_content(text, pl_name, source_url)

    tracks: list[PlaylistTrack] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            # Formato M3U: #EXTINF:267,Wham! - Last Christmas (Single Version)
            m = re.match(r"#EXTINF:\s*(-?\d+)\s*,\s*(.+)$", line)
            if m:
                dur = max(0, int(m.group(1)))
                artist, title = _split_combined_artist_title(m.group(2).strip())
                if title:
                    tracks.append(
                        PlaylistTrack(
                            index=len(tracks) + 1,
                            title=title,
                            artist=artist,
                            variation=extract_variation(title),
                            duration_sec=dur,
                        )
                    )
            continue
        if line.startswith("#"):
            continue
        # Ignorar caminhos de arquivo de M3U se já lemos #EXTINF
        if source_platform == "Arquivo M3U" and (line.startswith("/") or line.lower().endswith((".mp3", ".flac", ".m4a", ".mp4"))):
            if tracks:
                continue
            line = Path(line).stem

        # Remover numeração inicial ("01. ", "1) ", "1 - ")
        line = re.sub(r"^\s*\d{1,3}\s*[.)]\s*", "", line).strip()
        line = re.sub(r"^\s*\d{1,3}\s+-\s+(?=.+\s-\s)", "", line).strip()
        if not line:
            continue

        artist, title = _split_combined_artist_title(line)
        if not title:
            continue
        tracks.append(
            PlaylistTrack(
                index=len(tracks) + 1,
                title=title,
                artist=artist,
                variation=extract_variation(title),
            )
        )

    if not tracks:
        raise ValueError("Nenhuma faixa válida encontrada no texto informado. Use uma faixa por linha (ex: Artista - Música).")

    return ParsedPlaylist(
        name=pl_name,
        source_platform=source_platform,
        source_url=source_url,
        tracks=tracks,
    )


def _parse_csv_content(content: str, pl_name: str, source_url: str = "") -> ParsedPlaylist:
    reader = csv.DictReader(io.StringIO(content))
    tracks: list[PlaylistTrack] = []
    for row in reader:
        low_map = {str(k).strip().lower(): str(v or "").strip() for k, v in row.items() if k}
        title = (
            low_map.get("track name")
            or low_map.get("title")
            or low_map.get("track")
            or low_map.get("song")
            or low_map.get("música")
            or ""
        )
        artist = (
            low_map.get("artist name(s)")
            or low_map.get("artist name")
            or low_map.get("artist")
            or low_map.get("artista")
            or ""
        )
        album = low_map.get("album name") or low_map.get("album") or low_map.get("álbum") or ""
        isrc = low_map.get("isrc") or ""
        dur_raw = low_map.get("duration (ms)") or low_map.get("duration") or "0"
        dur_sec = 0
        try:
            dv = int(float(dur_raw))
            dur_sec = dv // 1000 if dv > 1000 else dv
        except Exception:
            pass
        if not title:
            continue
        # Exportify separa múltiplos artistas por ';'
        artist = clean_artist_name(artist.replace(";", ", "))
        tracks.append(
            PlaylistTrack(
                index=len(tracks) + 1,
                title=title,
                artist=artist,
                album=album,
                variation=extract_variation(title),
                duration_sec=dur_sec,
                isrc=isrc,
            )
        )
    if not tracks:
        raise ValueError("Nenhuma faixa encontrada no arquivo CSV.")
    return ParsedPlaylist(
        name=pl_name,
        source_platform="CSV",
        source_url=source_url,
        tracks=tracks,
    )


def parse_playlist_input(raw_input: str, custom_name: str = "") -> ParsedPlaylist:
    """
    Ponto único de entrada: detecta automaticamente a plataforma pelo link ou lê arquivo/texto colado.
    """
    text = raw_input.strip()
    if not text:
        raise ValueError("Cole um link de playlist (Spotify, YouTube Music, Amazon Music, Deezer, Apple Music) ou uma lista de músicas.")

    # Se for URL única
    if "\n" not in text and re.match(r"^https?://", text, re.I):
        low = text.lower()
        if "spotify.com" in low:
            pl = _parse_spotify(text)
        elif "deezer.com" in low or "deezer.page.link" in low:
            pl = _parse_deezer(text)
        elif "music.youtube.com" in low:
            pl = _parse_youtube_or_generic_ytdlp(text, platform_label="YouTube Music")
        elif "youtube.com" in low or "youtu.be" in low:
            pl = _parse_youtube_or_generic_ytdlp(text, platform_label="YouTube")
        elif "music.apple.com" in low:
            pl = _parse_apple_music(text)
        elif "music.amazon." in low:
            pl = _parse_amazon_music(text)
        else:
            # Tentar extrator genérico yt-dlp (SoundCloud, Bandcamp, Tidal, etc.)
            pl = _parse_youtube_or_generic_ytdlp(text, platform_label="Link Externo")

        if custom_name.strip():
            pl.name = custom_name.strip()
        return pl

    return _parse_text_or_file(text, custom_name=custom_name)


# ──────────────────────────────────────────────────────────────
# SELEÇÃO INTELIGENTE POR VARIAÇÃO + QUALIDADE + ESPACIALIDADE
# ──────────────────────────────────────────────────────────────

def _candidate_has_marker(cand: TrackResult, marker: str) -> bool:
    ctx = _strip_accents(f"{cand.title} {cand.album} {cand.filename} {cand.remote_path}".lower())
    m = _strip_accents(marker.lower())
    return bool(re.search(rf"\b{re.escape(m)}\b", ctx))


def _variation_alignment(track: PlaylistTrack, cand: TrackResult) -> tuple[float, bool]:
    """
    Retorna (ajuste_de_score, variation_mismatch).
    - Se a playlist pediu uma variação real (Live, Acoustic, Remix, Pudding Mix, Instrumental, Extended):
        * Bônus +22 se o candidato tem a variação
        * Penalidade -28 e mismatch=True se não tem
    - Se a playlist pediu a faixa PADRÃO (sem variação):
        * Penalidade -32 se o candidato for Live/Remix/Acoustic/Instrumental/Demo/Cover/Pudding Mix
    """
    req_var = track.variation.strip()
    cand_ctx = _strip_accents(f"{cand.title} {cand.filename}".lower())

    if _is_meaningful_recording_variation(req_var):
        var_tokens = [t for t in _normalize_tokens(req_var) if t not in ("version", "edit", "mix", "the")]
        if not var_tokens:
            var_tokens = _normalize_tokens(req_var)
        hits = sum(1 for t in var_tokens if t in cand_ctx)
        if var_tokens and hits >= max(1, (len(var_tokens) + 1) // 2):
            return +20.0, False
        # Checar sinônimos (ex: Ao Vivo <-> Live, Acústico <-> Acoustic)
        low_var = _strip_accents(req_var.lower())
        if ("ao vivo" in low_var or "live" in low_var) and re.search(r"\b(live|ao vivo)\b", cand_ctx):
            return +20.0, False
        if ("acustic" in low_var or "acoustic" in low_var or "unplugged" in low_var) and re.search(r"\b(acoustic|acustic|unplugged)\b", cand_ctx):
            return +20.0, False
        return -24.0, True

    # Playlist pediu faixa normal: evitar trocar por Remix / Live / Instrumental / Acoustic / Pudding Mix!
    req_title_low = _strip_accents(track.title.lower())
    for marker in ALTERNATE_RECORDING_MARKERS:
        if marker in req_title_low:
            continue
        if re.search(rf"\b{re.escape(marker)}\b", cand_ctx):
            return -32.0, True

    return 0.0, False


def _duration_alignment(track: PlaylistTrack, cand: TrackResult) -> float:
    """Penaliza candidatos cuja duração diverge muito da duração informada na playlist."""
    if track.duration_sec <= 30 or cand.duration_sec <= 30:
        return 0.0
    diff = abs(track.duration_sec - cand.duration_sec)
    if diff <= 12:
        return +6.0
    if diff <= 28:
        return 0.0
    if diff <= 60:
        return -14.0
    return -28.0


def _artist_alignment(track: PlaylistTrack, cand: TrackResult) -> float:
    if not track.artist:
        return 0.0
    # Usar apenas o primeiro artista principal quando há feat ("Zach Bryan & Lana Del Rey" -> "Zach Bryan")
    primary_artist = re.split(r"\s*(?:,|&|feat\.?|ft\.?|x|with)\s*", track.artist, maxsplit=1, flags=re.I)[0].strip()
    art_tokens = _normalize_tokens(primary_artist or track.artist)
    if not art_tokens:
        return 0.0
    cand_ctx = _strip_accents(f"{cand.artist} {cand.title} {cand.filename} {cand.remote_path}".lower())
    hits = sum(1 for t in art_tokens if t in cand_ctx)
    if hits == len(art_tokens):
        return +12.0
    if hits > 0:
        return +3.0
    return -26.0


def select_ranked_candidates_for_playlist_track(
    track: PlaylistTrack,
    raw_candidates: list[TrackResult],
    resolved: Optional[ResolvedQuery] = None,
) -> list[tuple[TrackResult, bool]]:
    """
    Ranqueia candidatos para uma faixa específica da playlist, retornando lista ordenada de
    (TrackResult, variation_mismatch), priorizando:
      1. Match real de artista + título + variação
      2. Melhor espacialidade/qualidade para o setup (ATMOS 3D 7.1.4 > SURROUND 7.1/5.1 > ULTRA HD > HD)
      3. Disponibilidade real (slot livre / HTTP direto)
    """
    canon_query = f"{track.artist} {track.base_title}".strip()
    if resolved and not track.artist and resolved.canonical_full:
        canon_query = resolved.canonical_full
    canon_artist = track.artist or (resolved.canonical_artist if resolved else "")

    base_ranked = rank_results(
        raw_candidates,
        query=track.search_query,
        drop_junk=True,
        canonical_query=canon_query,
        canonical_artist=canon_artist,
        canonical_title=track.base_title,
    )

    scored: list[tuple[float, TrackResult, bool]] = []
    for cand in base_ranked:
        var_adj, var_mismatch = _variation_alignment(track, cand)
        dur_adj = _duration_alignment(track, cand)
        art_adj = _artist_alignment(track, cand)

        # Prioridade explícita de tier para o setup JBL Bar 1000MK2:
        # Tier 2 (Atmos E-AC-3 JOC) e Tier 1 (Atmos TrueHD) vencem Tier 3 (Surround) que vence Tier 4 (Estéreo),
        # exceto quando o candidato Atmos é claramente da música errada ou de variação errada (e existe a variação certa).
        tier_bonus = {1: 42.0, 2: 45.0, 3: 24.0, 4: 8.0}.get(cand.codec_tier, 0.0)
        if cand.codec_tier == 4 and cand.quality_label == "ULTRA HD":
            tier_bonus += 4.0

        total_pts = cand.score + tier_bonus + var_adj + dur_adj + art_adj
        # Rejeitar candidatos que claramente são de outro artista/música
        if cand.match_score < 0.50 or (art_adj < -20 and cand.match_score < 0.78):
            continue

        scored.append((total_pts, cand, var_mismatch))

    scored.sort(key=lambda item: (item[0], 1 if item[1].has_free_slot else 0, item[1].speed_bps), reverse=True)
    return [(cand, mm) for _, cand, mm in scored]


# ──────────────────────────────────────────────────────────────
# ORQUESTRADOR DE IMPORTAÇÃO EM LOTE COM FAILOVER E RELATÓRIO
# ──────────────────────────────────────────────────────────────

class PlaylistImportRunner:
    """
    Executa a importação completa de uma playlist em thread de background:
      1. Cria a pasta dedicada ~/Music/DolbyAtmos/<Nome_da_Playlist>/
      2. Busca cada faixa (respeitando ritmo anti-cooldown do Soulseek)
      3. Seleciona a melhor opção disponível (Atmos 7.1.4 > Surround > Ultra HD > HD)
         respeitando a variação específica da faixa
      4. Baixa com failover automático entre peers (até 2 retries Soulseek + fallback HTTP)
      5. Empacota Atmos em .mp4/.mkv sem perda e verifica via ffprobe
      6. Gera .m3u e salva .atmoshunter_playlist.json com relatório completo
    """

    def __init__(
        self,
        playlist: ParsedPlaylist,
        dest_root: Path = DOWNLOAD_DIR,
        slsk_service: Optional[SoulseekService] = None,
        on_track_update: Optional[Callable[[int, TrackImportReport], None]] = None,
        on_overall_progress: Optional[Callable[[int, int, str], None]] = None,
        on_finished: Optional[Callable[[PlaylistImportReport], None]] = None,
        only_indices: Optional[set[int]] = None,
        existing_report: Optional[PlaylistImportReport] = None,
    ):
        self.playlist = playlist
        self.dest_root = dest_root
        self.slsk = slsk_service or get_slsk_service()
        self.on_track_update = on_track_update
        self.on_overall_progress = on_overall_progress
        self.on_finished = on_finished
        self.only_indices = only_indices
        self.existing_report = existing_report

        self._cancel_event = threading.Event()
        self._active_dl_manager: Optional[DownloadManager] = None
        self._active_engine: Optional[SearchEngine] = None
        self._folder = self._resolve_playlist_folder()

    @property
    def folder(self) -> Path:
        return self._folder

    def _resolve_playlist_folder(self) -> Path:
        safe_name = _sanitize_filename(self.playlist.name) or "Playlist_Importada"
        return self.dest_root / safe_name

    def cancel(self):
        self._cancel_event.set()
        if self._active_engine:
            self._active_engine.stop()
        if self._active_dl_manager:
            self._active_dl_manager.cancel_all()

    def _search_single_track(self, track: PlaylistTrack) -> tuple[list[TrackResult], Optional[ResolvedQuery]]:
        """Busca focada e rápida para uma faixa de playlist preservando artista + variação."""
        engine = SearchEngine(slsk_service=self.slsk)
        self._active_engine = engine
        try:
            # Se já temos artista + título claros da playlist, evitamos que o Google Suggest
            # reescreva a variação (ex: transformar "Pudding Mix" em versão padrão)
            if track.artist and track.title:
                rq = ResolvedQuery(
                    original=track.display_name,
                    corrected=track.search_query,
                    canonical_artist=track.artist,
                    canonical_title=track.base_title,
                    canonical_album=track.album,
                    canonical_full=f"{track.artist} {track.base_title}".strip(),
                    was_corrected=False,
                )
            else:
                rq = resolve_music_query(track.search_query)

            collected: list[TrackResult] = []
            lock = threading.Lock()
            slsk_atmos_hits = [0]

            def _add(batch: list[TrackResult], is_slsk: bool = False):
                if not batch or self._cancel_event.is_set():
                    return
                with lock:
                    collected.extend(batch)
                    if is_slsk:
                        for item in batch:
                            low = f"{item.remote_path} {item.filename}".lower()
                            if any(k in low for k in ("atmos", "multichannel", "spatial", "eac3", "joc", "truehd")):
                                slsk_atmos_hits[0] += 1

            # Queries HTTP (Hi-Res Direct) e Soulseek em paralelo
            from concurrent.futures import ThreadPoolExecutor

            primary_artist = re.split(r"\s*(?:,|&|feat\.?|ft\.?)\s*", rq.canonical_artist, maxsplit=1, flags=re.I)[0].strip()
            clean_canon = _ascii_clean(f"{primary_artist} {rq.canonical_title}".strip() or rq.corrected)
            var_kw = _ascii_clean(_primary_variation_keyword(track.variation))

            def _run_hires():
                if self._cancel_event.is_set():
                    return
                hq = []
                if var_kw:
                    hq.append(f"{clean_canon} {var_kw}".strip())
                if clean_canon:
                    hq.append(clean_canon)
                res = engine.hires.search(hq, limit=8)
                _add(res)

            def _run_slsk():
                if self._cancel_event.is_set():
                    return
                slsk_queries: list[str] = []
                if var_kw:
                    slsk_queries.append(f"{clean_canon} {var_kw} atmos".strip())
                    slsk_queries.append(f"{clean_canon} {var_kw} m4a".strip())
                else:
                    slsk_queries.append(f"{clean_canon} atmos".strip())
                    slsk_queries.append(f"{clean_canon} m4a".strip())

                self.slsk.search(
                    queries=slsk_queries,
                    on_batch=lambda b: _add(b, is_slsk=True),
                    duration_sec=5.5,
                    stop_event=self._cancel_event,
                )
                # Fallback rápido se pediu variação e não veio nada com a variação no Soulseek
                if slsk_atmos_hits[0] == 0 and var_kw and not self._cancel_event.is_set():
                    self.slsk.search(
                        queries=[f"{clean_canon} atmos"],
                        on_batch=lambda b: _add(b, is_slsk=True),
                        duration_sec=3.5,
                        stop_event=self._cancel_event,
                    )

            with ThreadPoolExecutor(max_workers=2) as pool:
                fh = pool.submit(_run_hires)
                fs = pool.submit(_run_slsk)
                fh.result()
                fs.result()

            return collected, rq
        finally:
            self._active_engine = None

    def _write_m3u(self, report: PlaylistImportReport):
        lines = ["#EXTM3U", f"#PLAYLIST:{report.playlist_name}"]
        for tr in report.tracks:
            if tr.saved_filename and tr.status in (STATUS_OPTIMAL_ATMOS, STATUS_DOWNGRADED_SURROUND, STATUS_DOWNGRADED_STEREO):
                lines.append(f"#EXTINF:-1,{tr.requested_artist} - {tr.requested_title}" if tr.requested_artist else f"#EXTINF:-1,{tr.requested_title}")
                lines.append(tr.saved_filename)
        if len(lines) > 2:
            m3u_name = _sanitize_filename(report.playlist_name) + ".m3u"
            (self._folder / m3u_name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run(self) -> PlaylistImportReport:
        self._folder.mkdir(parents=True, exist_ok=True)
        dm = DownloadManager(slsk_service=self.slsk, dest_dir=self._folder)
        self._active_dl_manager = dm

        # Montar lista inicial de relatórios por faixa (reaproveitando já concluídas em caso de retry!)
        existing_map: dict[int, TrackImportReport] = {}
        if self.existing_report:
            for tr in self.existing_report.tracks:
                existing_map[tr.index] = tr

        report_tracks: list[TrackImportReport] = []
        for t in self.playlist.tracks:
            if self.only_indices is not None and t.index not in self.only_indices and t.index in existing_map:
                report_tracks.append(existing_map[t.index])
                continue
            if not t.selected:
                report_tracks.append(
                    TrackImportReport(
                        index=t.index,
                        requested_artist=t.artist,
                        requested_title=t.title,
                        requested_album=t.album,
                        requested_variation=t.variation,
                        status=STATUS_SKIPPED,
                        status_label="Ignorada",
                    )
                )
            else:
                report_tracks.append(
                    TrackImportReport(
                        index=t.index,
                        requested_artist=t.artist,
                        requested_title=t.title,
                        requested_album=t.album,
                        requested_variation=t.variation,
                        status=STATUS_PENDING,
                        status_label="Na fila",
                    )
                )

        report = PlaylistImportReport(
            playlist_name=self.playlist.name,
            source_platform=self.playlist.source_platform,
            source_url=self.playlist.source_url,
            folder_path=str(self._folder),
            parse_warnings=list(self.playlist.warnings),
            tracks=report_tracks,
        )

        # Determinar quais posições processar
        work_items: list[tuple[int, PlaylistTrack, TrackImportReport]] = []
        for pos, (t, tr) in enumerate(zip(self.playlist.tracks, report.tracks)):
            if not t.selected:
                continue
            if self.only_indices is not None and t.index not in self.only_indices:
                continue
            work_items.append((pos, t, tr))

        total_work = len(work_items)
        pad_width = max(2, len(str(len(self.playlist.tracks))))

        # Smart Resume: detectar faixas já baixadas anteriormente em Dolby Atmos real nesta pasta
        # para evitar re-baixar e duplicar quando o usuário reimportar a mesma playlist!
        existing_atmos_by_prefix: dict[str, Path] = {}
        if self.only_indices is None and self._folder.exists():
            for fp in self._folder.iterdir():
                if not fp.is_file() or fp.suffix.lower() not in (".mp4", ".mkv", ".m4a"):
                    continue
                pm = re.match(r"^(\d{2,3})\s*-\s*", fp.name)
                if pm:
                    pinfo = _probe_file_full(fp)
                    if pinfo:
                        from core.audio_verify import classify_probed_audio
                        if classify_probed_audio(pinfo, ext=fp.suffix.lower()).is_atmos:
                            existing_atmos_by_prefix[pm.group(1)] = fp

        for step_idx, (pos, track, tr) in enumerate(work_items):
            if self._cancel_event.is_set():
                tr.status = STATUS_CANCELLED
                tr.status_label = "Cancelada"
                report.cancelled = True
                if self.on_track_update:
                    self.on_track_update(pos, tr)
                continue

            # Se já existe faixa Dolby Atmos verificada nesta posição da pasta, reaproveitar sem duplicar!
            idx_key = f"{track.index:0{pad_width}d}"
            already_file = existing_atmos_by_prefix.get(idx_key)
            if already_file and already_file.exists():
                pinfo = _probe_file_full(already_file)
                real_tags, _, summary = _build_library_tags(pinfo, already_file.suffix.lower())
                tr.status = STATUS_OPTIMAL_ATMOS
                tr.status_label = "Já salva (Atmos)"
                tr.achieved_spatial = "Dolby Atmos"
                tr.achieved_quality = "LOSSLESS" if pinfo.get("codec_name") == "truehd" else "DD+ JOC"
                tr.achieved_channels = "7.1.4"
                tr.verified_audio = summary
                tr.saved_filename = already_file.name
                tr.saved_path = str(already_file)
                tr.tags = real_tags
                tr.progress = 1.0
                if self.on_track_update:
                    self.on_track_update(pos, tr)
                continue

            # Pausa anti-cooldown entre buscas consecutivas no Soulseek
            if step_idx > 0:
                for _ in range(15):
                    if self._cancel_event.is_set():
                        break
                    time.sleep(0.1)
                if self._cancel_event.is_set():
                    tr.status = STATUS_CANCELLED
                    tr.status_label = "Cancelada"
                    report.cancelled = True
                    if self.on_track_update:
                        self.on_track_update(pos, tr)
                    continue

            if self.on_overall_progress:
                self.on_overall_progress(step_idx, total_work, f"Buscando {step_idx + 1}/{total_work}: {track.display_name}")

            tr.status = STATUS_SEARCHING
            tr.status_label = "Buscando..."
            tr.issue_message = ""
            if self.on_track_update:
                self.on_track_update(pos, tr)

            try:
                raw_cands, rq = self._search_single_track(track)
                ranked_pairs = select_ranked_candidates_for_playlist_track(track, raw_cands, resolved=rq)
            except Exception as e:
                logger.exception(f"Erro buscando faixa da playlist: {track.display_name}")
                ranked_pairs = []

            if self._cancel_event.is_set():
                tr.status = STATUS_CANCELLED
                tr.status_label = "Cancelada"
                report.cancelled = True
                if self.on_track_update:
                    self.on_track_update(pos, tr)
                continue

            if not ranked_pairs:
                tr.status = STATUS_FAILED
                tr.status_label = "Não encontrada"
                tr.issue_message = "Nenhuma fonte retornou arquivo válido (Atmos, Surround ou Lossless) para esta faixa."
                if self.on_track_update:
                    self.on_track_update(pos, tr)
                report.save(self._folder)
                continue

            # Tentar baixar os melhores candidatos (failover: até 3 peers Soulseek + 1 HTTP)
            attempt_list: list[tuple[TrackResult, bool]] = []
            slsk_added = 0
            http_added = 0
            for cand, mm in ranked_pairs:
                if cand.provider == "soulseek" and slsk_added < 3:
                    attempt_list.append((cand, mm))
                    slsk_added += 1
                elif cand.provider == "http" and http_added < 1:
                    attempt_list.append((cand, mm))
                    http_added += 1
                if slsk_added >= 3 and http_added >= 1:
                    break

            prefix = f"{track.index:0{pad_width}d} - "
            last_err = ""
            # Guardar arquivo anterior em caso de retry (para limpar duplicata apenas se conseguirmos um melhor,
            # e preservar o antigo caso o retry falhe!)
            prev_saved_path = Path(existing_map[track.index].saved_path) if (
                self.only_indices is not None
                and track.index in existing_map
                and existing_map[track.index].saved_path
            ) else None
            prev_status_rank = {
                STATUS_OPTIMAL_ATMOS: 3,
                STATUS_DOWNGRADED_SURROUND: 2,
                STATUS_DOWNGRADED_STEREO: 1,
            }.get(existing_map[track.index].status, 0) if (self.only_indices is not None and track.index in existing_map) else 0

            # Melhor resultado obtido nesta rodada: (tier_rank, report_snapshot_dict, saved_path)
            best_obtained: Optional[tuple[int, dict, Path]] = None

            def _classify_download(saved_path: Path, verified_str: str, cand: TrackResult, var_mismatch: bool) -> tuple[int, dict]:
                from core.audio_verify import classify_probed_audio

                probe_info = _probe_file_full(saved_path)
                real_tags = list(cand.tags)
                va = classify_probed_audio(probe_info, ext=saved_path.suffix.lower()) if probe_info else None
                if va:
                    real_tags = list(va.tags)
                    is_real_atmos = va.is_atmos
                    is_real_surround = va.is_surround
                    is_real_ultrahd = va.quality_label == "ULTRA HD"
                    verified_str = va.summary or verified_str
                else:
                    ver_low = verified_str.lower()
                    is_real_atmos = "dolby atmos" in ver_low
                    is_real_surround = (not is_real_atmos) and (
                        "ac3" in ver_low or "aac 5.1" in ver_low or "5.1" in ver_low or "7.1" in ver_low or "6ch" in ver_low or "8ch" in ver_low
                    )
                    is_real_ultrahd = "24-bit" in ver_low or cand.quality_label == "ULTRA HD"

                issues: list[str] = []
                if is_real_atmos:
                    rank_val = 3
                    st = STATUS_OPTIMAL_ATMOS
                    sp = "Dolby Atmos"
                    ql = va.quality_label if va else ("LOSSLESS" if "truehd" in verified_str.lower() else "DD+ JOC")
                    ch = "7.1.4"
                    lbl = "Dolby Atmos (7.1.4)"
                elif is_real_surround:
                    rank_val = 2
                    st = STATUS_DOWNGRADED_SURROUND
                    sp = "SURROUND"
                    ql = va.quality_label if va else ("DD 5.1" if "ac3" in verified_str.lower() else (cand.quality_label or "HD"))
                    ch = va.channels_label if va else ("7.1" if "7.1" in verified_str else "5.1")
                    lbl = f"Surround ({ql})"
                    issues.append(f"Dolby Atmos 7.1.4 indisponível; arquivo baixado é {ql} ({verified_str}).")
                else:
                    rank_val = 1
                    st = STATUS_DOWNGRADED_STEREO
                    sp = "ESTÉREO"
                    ql = va.quality_label if va else ("ULTRA HD" if is_real_ultrahd else "HD")
                    ch = va.channels_label if va else "2.0"
                    lbl = f"{ql} ({ch})"
                    issues.append(f"Dolby Atmos/Surround indisponível; baixada em {ql} Estéreo {ch}.")

                if var_mismatch:
                    if track.variation:
                        issues.append(f"Variação pedida ('{track.variation}') não foi confirmada no arquivo baixado ('{cand.title}').")
                    else:
                        issues.append(f"Possível variação diferente da original de estúdio ('{cand.title}').")

                return rank_val, {
                    "status": st,
                    "status_label": lbl,
                    "achieved_spatial": sp,
                    "achieved_quality": ql,
                    "achieved_channels": ch,
                    "verified_audio": verified_str,
                    "matched_name": cand.display_name,
                    "saved_filename": saved_path.name,
                    "saved_path": str(saved_path),
                    "source_used": cand.source,
                    "variation_mismatch": var_mismatch,
                    "issue_message": " ".join(issues),
                    "tags": real_tags,
                }

            for attempt_no, (cand, var_mismatch) in enumerate(attempt_list, start=1):
                if self._cancel_event.is_set():
                    break
                # Se já temos um resultado salvo (ex: Surround 5.1), só tentar o próximo candidato se ele prometer Atmos!
                if best_obtained is not None and (best_obtained[0] >= 3 or cand.codec_tier not in (1, 2)):
                    break

                tr.status = STATUS_DOWNLOADING
                tr.matched_name = cand.display_name
                tr.source_used = cand.source
                tr.tags = list(cand.tags)
                tr.status_label = f"Baixando ({cand.spatial_label})..." if attempt_no == 1 else f"Tentativa {attempt_no} ({cand.source})..."
                if self.on_track_update:
                    self.on_track_update(pos, tr)
                if self.on_overall_progress:
                    self.on_overall_progress(step_idx, total_work, f"Baixando {step_idx + 1}/{total_work}: {cand.display_name}")

                def _on_dl_prog(job):
                    if job.status == DownloadStatus.DOWNLOADING:
                        tr.progress = job.progress
                        tr.status_label = f"Baixando {int(job.progress * 100)}%"
                    elif job.status == DownloadStatus.VERIFYING:
                        tr.progress = 0.99
                        tr.status_label = "Empacotando / ffprobe..."
                    if self.on_track_update:
                        self.on_track_update(pos, tr)

                job = dm.download_track_sync(
                    track=cand,
                    dest_dir=self._folder,
                    filename_prefix=prefix,
                    status_cb=_on_dl_prog,
                    cancel_event=self._cancel_event,
                )

                if job.status == DownloadStatus.CANCELLED or self._cancel_event.is_set():
                    tr.status = STATUS_CANCELLED
                    tr.status_label = "Cancelada"
                    report.cancelled = True
                    break

                if job.status == DownloadStatus.DONE and job.saved_path:
                    verified_str = job.verified_audio or cand.codec_label
                    rank_val, snap = _classify_download(job.saved_path, verified_str, cand, var_mismatch)

                    if best_obtained is None or rank_val > best_obtained[0]:
                        # Se superamos um download anterior da mesma rodada, remover o arquivo inferior
                        if best_obtained is not None and best_obtained[2] != job.saved_path:
                            best_obtained[2].unlink(missing_ok=True)
                        best_obtained = (rank_val, snap, job.saved_path)
                    else:
                        # Este segundo peer não melhorou a qualidade; descartar o duplicado
                        if job.saved_path != best_obtained[2]:
                            job.saved_path.unlink(missing_ok=True)

                    # Se já obtivemos Atmos 3D real verificado pelo ffprobe, concluído!
                    if best_obtained[0] >= 3:
                        break
                else:
                    last_err = job.error_msg or "Transferência falhou."
                    logger.info(f"Tentativa {attempt_no} falhou para {track.display_name}: {last_err}")

            if best_obtained is not None:
                best_rank, snap, best_path = best_obtained
                # Em caso de retry, se já tínhamos um arquivo anterior de qualidade >= à nova, restaurar/manter o antigo
                if prev_saved_path and prev_saved_path.exists() and prev_saved_path != best_path:
                    if best_rank >= prev_status_rank:
                        prev_saved_path.unlink(missing_ok=True)
                    else:
                        best_path.unlink(missing_ok=True)
                        old_rep = existing_map[track.index]
                        snap = {
                            "status": old_rep.status,
                            "status_label": old_rep.status_label,
                            "achieved_spatial": old_rep.achieved_spatial,
                            "achieved_quality": old_rep.achieved_quality,
                            "achieved_channels": old_rep.achieved_channels,
                            "verified_audio": old_rep.verified_audio,
                            "matched_name": old_rep.matched_name,
                            "saved_filename": old_rep.saved_filename,
                            "saved_path": old_rep.saved_path,
                            "source_used": old_rep.source_used,
                            "variation_mismatch": old_rep.variation_mismatch,
                            "issue_message": old_rep.issue_message,
                            "tags": list(old_rep.tags),
                        }

                tr.progress = 1.0
                for k, v in snap.items():
                    setattr(tr, k, v)
            elif tr.status != STATUS_CANCELLED:
                # Se era retry e havia um arquivo anterior válido no disco, preservar em vez de sobrescrever com erro
                if prev_saved_path and prev_saved_path.exists():
                    old_rep = existing_map[track.index]
                    tr.status = old_rep.status
                    tr.status_label = old_rep.status_label
                    tr.achieved_spatial = old_rep.achieved_spatial
                    tr.achieved_quality = old_rep.achieved_quality
                    tr.achieved_channels = old_rep.achieved_channels
                    tr.verified_audio = old_rep.verified_audio
                    tr.saved_filename = old_rep.saved_filename
                    tr.saved_path = old_rep.saved_path
                    tr.tags = list(old_rep.tags)
                    tr.issue_message = old_rep.issue_message
                else:
                    tr.status = STATUS_FAILED
                    tr.status_label = "Erro no download"
                    tr.issue_message = f"Todas as fontes falharam ({last_err})."

            if self.on_track_update:
                self.on_track_update(pos, tr)

            # Salvar progresso incrementalmente (se fechar ou cair energia, nada se perde)
            self._write_m3u(report)
            report.save(self._folder)

        self._write_m3u(report)
        report.save(self._folder)
        self._active_dl_manager = None

        if self.on_overall_progress:
            self.on_overall_progress(
                total_work,
                total_work,
                f"Concluído: {report.count_downloaded}/{total_work} baixadas ({report.count_atmos} Atmos 3D, {report.count_failed} erros)",
            )
        if self.on_finished:
            self.on_finished(report)
        return report
