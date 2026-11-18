# Copyright (c) 2026 Yuri Barbosa (@i9Yuri). All Rights Reserved.
# Proprietary source-available software (personal, non-commercial use only).
# Official repository: https://github.com/i9Yuri/Zenith
# Unauthorized commercial use, redistribution, or rebranding is prohibited. See LICENSE.
"""
AtmosHunter — Ponte DLNA / UPnP (Rygel + Biblioteca Local + Cast Direto)
- Monitora saúde do servidor Rygel (systemd + SSDP)
- Cataloga ~/Music/DolbyAtmos com cache JSON de ffprobe (instantâneo)
- Permite enviar qualquer faixa da biblioteca direto para a TV Samsung (HDMI eARC -> JBL)
  ou para a JBL Bar 1000MK2 via UPnP AVTransport com 1 clique.
"""
from __future__ import annotations

import hashlib
import html
import http.server
import json
import logging
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

from core.audio_verify import classify_probed_audio, probe_audio_full
from core.ranker import AudioTag, format_size

# Registrar namespaces oficiais DIDL-Lite para evitar prefixos inválidos (<ns0:item>, <ns3:class>)
# que fazem a TV Samsung Tizen rejeitar o SetAVTransportURI!
ET.register_namespace("", "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/")
ET.register_namespace("dc", "http://purl.org/dc/elements/1.1/")
ET.register_namespace("upnp", "urn:schemas-upnp-org:metadata-1-0/upnp/")
ET.register_namespace("dlna", "urn:schemas-dlna-org:metadata-1-0/")

logger = logging.getLogger("AtmosHunter.dlna")

DOWNLOAD_DIR = Path.home() / "Music" / "DolbyAtmos"
PROBE_CACHE_FILE = Path.home() / ".cache" / "atmoshunter" / "library_probe.json"
MEDIA_EXTS = {".mp4", ".mkv", ".m4a", ".flac", ".mka", ".ac3", ".eac3", ".ec3", ".wav", ".alac"}


@dataclass
class LibraryItem:
    path: Path
    title: str
    filename: str
    size_bytes: int
    size_label: str
    duration_sec: int = 0
    codec_summary: str = ""
    is_atmos: bool = False
    is_surround: bool = False
    is_video_wrapped: bool = False
    tags: list[AudioTag] = field(default_factory=list)
    mtime: float = 0.0
    playlist_name: str = ""       # "" = raiz (Avulsas); nome da subpasta = Playlist importada
    track_order: int = 0          # ordem numérica dentro da playlist (01, 02...)


@dataclass
class PlaylistSummary:
    name: str
    folder: Path
    track_count: int = 0
    atmos_count: int = 0
    total_bytes: int = 0
    has_report: bool = False
    issue_count: int = 0


@dataclass
class DlnaRenderer:
    name: str
    ip: str
    location: str
    av_transport_url: str
    is_tv: bool = False
    is_jbl: bool = False


# ──────────────────────────────────────────────────────────────
# SAÚDE DO RYGEL
# ──────────────────────────────────────────────────────────────

def check_rygel_status() -> tuple[bool, str]:
    """Retorna (ativo, mensagem_curta) checando o serviço systemd do Rygel."""
    if not shutil.which("systemctl"):
        return False, "systemd ausente"
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", "rygel"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        active = proc.stdout.strip() == "active"
        if active:
            return True, "DLNA ativo"
        return False, "DLNA parado"
    except Exception:
        return False, "DLNA indisponível"


def restart_rygel(rebuild_db: bool = False) -> tuple[bool, str]:
    """Reinicia o Rygel de forma limpa (opcionalmente reconstruindo o media-export.db se inconsistente)."""
    try:
        if rebuild_db:
            subprocess.run(["systemctl", "--user", "stop", "rygel"], timeout=8, check=False)
            db_dir = Path.home() / ".cache" / "rygel"
            for db_f in db_dir.glob("media-export.db*"):
                db_f.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "restart", "rygel"], timeout=8, check=False)
        return check_rygel_status()
    except Exception as e:
        return False, f"Falha ao reiniciar DLNA: {e}"


# ──────────────────────────────────────────────────────────────
# CACHE DE FFPROBE E LEITURA DA BIBLIOTECA LOCAL
# ──────────────────────────────────────────────────────────────

def _load_probe_cache() -> dict:
    try:
        if PROBE_CACHE_FILE.exists():
            return json.loads(PROBE_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_probe_cache(cache: dict):
    try:
        PROBE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROBE_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug(f"Não foi possível gravar cache ffprobe: {e}")


def _probe_file_full(path: Path) -> dict:
    return probe_audio_full(path)


def _build_library_tags(info: dict, ext: str) -> tuple[list[AudioTag], bool, str]:
    va = classify_probed_audio(info, ext=ext, verified_pill=False)
    return va.tags, va.is_atmos, va.summary


def scan_local_library(directory: Path = DOWNLOAD_DIR) -> list[LibraryItem]:
    """
    Lista todas as músicas em ~/Music/DolbyAtmos (raiz + subpastas de playlists)
    com tags verificadas por ffprobe e cache JSON.
    """
    directory.mkdir(parents=True, exist_ok=True)
    cache = _load_probe_cache()
    cache_dirty = False
    items: list[LibraryItem] = []

    media_entries: list[tuple[Path, str]] = []
    for p in directory.iterdir():
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS:
            media_entries.append((p, ""))
        elif p.is_dir() and not p.name.startswith("."):
            for sub in p.iterdir():
                if sub.is_file() and sub.suffix.lower() in MEDIA_EXTS:
                    media_entries.append((sub, p.name))

    active_keys = set()
    for p, pl_name in media_entries:
        try:
            st = p.stat()
        except OSError:
            continue
        rel_key = f"{pl_name}/{p.name}" if pl_name else p.name
        ckey = f"{rel_key}|{st.st_size}|{int(st.st_mtime)}"
        active_keys.add(ckey)

        info = cache.get(ckey)
        if not info:
            info = _probe_file_full(p)
            if info:
                cache[ckey] = info
                cache_dirty = True
            else:
                info = {}

        va = classify_probed_audio(info, ext=p.suffix.lower(), verified_pill=False)
        tags, is_atmos, summary = va.tags, va.is_atmos, va.summary
        clean_title = p.stem
        track_order = 0
        if pl_name:
            om = re.match(r"^(\d{1,3})\s*-\s*(.+)$", clean_title)
            if om:
                track_order = int(om.group(1))
                clean_title = om.group(2).strip()

        for suffix_tag in (" [Dolby Atmos TrueHD]", " [Dolby Atmos]", " [Surround 7.1]", " [Surround 5.1]", " [Multicanal]"):
            if clean_title.endswith(suffix_tag):
                clean_title = clean_title[: -len(suffix_tag)].strip()

        items.append(
            LibraryItem(
                path=p,
                title=clean_title,
                filename=p.name,
                size_bytes=st.st_size,
                size_label=format_size(st.st_size),
                duration_sec=int(info.get("duration") or 0),
                codec_summary=summary,
                is_atmos=is_atmos,
                is_surround=va.is_surround,
                is_video_wrapped=bool(info.get("has_video")),
                tags=tags,
                mtime=st.st_mtime,
                playlist_name=pl_name,
                track_order=track_order,
            )
        )

    # Ordenação: dentro de playlists preserva ordem numérica; na visão geral, mais recentes primeiro
    items.sort(key=lambda it: (0 if not it.playlist_name else 1, it.playlist_name.lower(), it.track_order if it.track_order else 9999, -it.mtime))

    # Limpar entradas antigas do cache apenas quando escaneando a pasta padrão
    if directory == DOWNLOAD_DIR:
        stale = [k for k in cache if k not in active_keys]
        if stale:
            for k in stale:
                cache.pop(k, None)
            cache_dirty = True

    if cache_dirty:
        _save_probe_cache(cache)

    return items


def summarize_playlists(items: list[LibraryItem], directory: Path = DOWNLOAD_DIR) -> list[PlaylistSummary]:
    """Agrupa pastas de playlists importadas com contagem de faixas, Atmos e alertas do relatório."""
    by_name: dict[str, PlaylistSummary] = {}

    # Também listar pastas de playlist que possuem relatório salvo mesmo se 0 faixas baixaram
    if directory.exists():
        for sub in sorted(directory.iterdir(), key=lambda x: x.name.lower()):
            if sub.is_dir() and not sub.name.startswith("."):
                rep_file = sub / ".atmoshunter_playlist.json"
                has_media = any(f.is_file() and f.suffix.lower() in MEDIA_EXTS for f in sub.iterdir())
                if rep_file.exists() or has_media:
                    issue_n = 0
                    if rep_file.exists():
                        try:
                            raw = json.loads(rep_file.read_text(encoding="utf-8"))
                            for t in raw.get("tracks") or []:
                                if t.get("status") in ("downgraded_surround", "downgraded_stereo", "failed") or t.get("variation_mismatch") or t.get("issue_message"):
                                    issue_n += 1
                        except Exception:
                            pass
                    by_name[sub.name] = PlaylistSummary(
                        name=sub.name,
                        folder=sub,
                        has_report=rep_file.exists(),
                        issue_count=issue_n,
                    )

    for it in items:
        if not it.playlist_name:
            continue
        ps = by_name.get(it.playlist_name)
        if not ps:
            ps = PlaylistSummary(name=it.playlist_name, folder=it.path.parent)
            by_name[it.playlist_name] = ps
        ps.track_count += 1
        if it.is_atmos:
            ps.atmos_count += 1
        ps.total_bytes += it.size_bytes

    return sorted(by_name.values(), key=lambda p: p.name.lower())


# ──────────────────────────────────────────────────────────────
# DESCOBERTA UPnP DE RENDERERS (TV SAMSUNG + JBL) E RYGEL CAST
# ──────────────────────────────────────────────────────────────

def _ssdp_search(st: str, timeout: float = 2.0) -> list[tuple[str, str]]:
    msg = "\r\n".join([
        "M-SEARCH * HTTP/1.1",
        "HOST: 239.255.255.250:1900",
        'MAN: "ssdp:discover"',
        "MX: 1",
        f"ST: {st}",
        "", "",
    ]).encode()
    found: list[tuple[str, str]] = []
    seen = set()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.settimeout(timeout)
    try:
        s.sendto(msg, ("239.255.255.250", 1900))
        while True:
            try:
                data, addr = s.recvfrom(4096)
                for ln in data.decode("utf-8", errors="ignore").splitlines():
                    if ln.lower().startswith("location:"):
                        loc = ln.split(":", 1)[1].strip()
                        if (addr[0], loc) not in seen:
                            seen.add((addr[0], loc))
                            found.append((addr[0], loc))
                        break
            except socket.timeout:
                break
    except Exception:
        pass
    finally:
        s.close()
    return found


def discover_renderers() -> list[DlnaRenderer]:
    """Descobre TV Samsung e JBL Bar 1000MK2 na rede local via UPnP MediaRenderer."""
    renderers: list[DlnaRenderer] = []
    ns = {"u": "urn:schemas-upnp-org:device-1-0"}

    for ip, loc in _ssdp_search("urn:schemas-upnp-org:device:MediaRenderer:1", timeout=2.0):
        try:
            r = requests.get(loc, timeout=3)
            r.raise_for_status()
            root = ET.fromstring(r.text)
            fname_el = root.find(".//u:friendlyName", ns)
            mname_el = root.find(".//u:modelName", ns)
            fname = fname_el.text.strip() if fname_el is not None and fname_el.text else ip
            mname = mname_el.text.strip() if mname_el is not None and mname_el.text else ""

            av_ctrl = ""
            for svc in root.findall(".//u:service", ns):
                st_el = svc.find("u:serviceType", ns)
                cu_el = svc.find("u:controlURL", ns)
                if st_el is not None and "AVTransport" in (st_el.text or "") and cu_el is not None and cu_el.text:
                    base = loc.split("/", 3)[:3]
                    origin = "/".join(base)
                    path = cu_el.text if cu_el.text.startswith("/") else "/" + cu_el.text
                    av_ctrl = origin + path
                    break

            if not av_ctrl:
                continue

            low = f"{fname} {mname}".lower()
            is_jbl = "jbl" in low or "bar 1000" in low
            is_tv = "samsung" in low or "qn50" in low or "tv" in low or fname.lower() == "a3"
            label = f"TV Samsung ({fname})" if (is_tv and "samsung" not in fname.lower()) else fname
            renderers.append(DlnaRenderer(name=label, ip=ip, location=loc, av_transport_url=av_ctrl, is_tv=is_tv, is_jbl=is_jbl))
        except Exception as e:
            logger.debug(f"Renderer ignorado ({loc}): {e}")

    # Priorizar TV Samsung (para Atmos eARC) seguido da JBL
    renderers.sort(key=lambda d: (0 if d.is_tv else (1 if d.is_jbl else 2), d.name))
    return renderers


def _find_rygel_media_url(
    title_hint: str,
    filename_ext: str,
    expected_size_bytes: int = 0,
    filename_stem: str = "",
) -> Optional[tuple[str, str]]:
    """Localiza no Rygel a URL HTTP e o metadado DIDL-Lite exatos da faixa (casando tamanho e título)."""
    servers = _ssdp_search("urn:schemas-upnp-org:service:ContentDirectory:3", timeout=2.0)
    if not servers:
        servers = _ssdp_search("urn:schemas-upnp-org:device:MediaServer:1", timeout=2.0)

    rygel_base = None
    for _ip, loc in servers:
        try:
            r = requests.get(loc, timeout=2.5)
            if "Rygel" in r.text or "Dolby Atmos Server" in r.text:
                rygel_base = loc.rsplit("/", 1)[0]
                break
        except Exception:
            continue

    if not rygel_base:
        return None

    ctrl = rygel_base + "/Control/MediaExport/RygelContentDirectory"
    # Buscar por parte limpa do título (removendo prefixo numérico de playlist tipo '01 - ')
    clean_hint = re.sub(r"^\d{1,3}\s*-\s*", "", title_hint).strip()
    q = clean_hint.split(" - ")[-1].split("(")[0].replace('"', "").strip() or clean_hint.replace('"', "")
    sbody = f'''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:Search xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
      <ContainerID>0</ContainerID>
      <SearchCriteria>dc:title contains "{q}"</SearchCriteria>
      <Filter>*</Filter>
      <StartingIndex>0</StartingIndex>
      <RequestedCount>30</RequestedCount>
      <SortCriteria></SortCriteria>
    </u:Search>
  </s:Body>
</s:Envelope>'''
    try:
        res = requests.post(
            ctrl,
            data=sbody.encode("utf-8"),
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": '"urn:schemas-upnp-org:service:ContentDirectory:1#Search"',
            },
            timeout=4,
        )
        res.raise_for_status()
        env = ET.fromstring(res.text)
        result_el = env.find(".//Result")
        if result_el is None or not result_el.text:
            return None
        didl_xml = result_el.text
        didl = ET.fromstring(didl_xml)

        best_match: Optional[tuple[int, str, str]] = None
        clean_hint_low = clean_hint.lower()
        stem_low = filename_stem.lower()

        for item in didl:
            dc_title = ""
            for child in item:
                if child.tag.endswith("}title") and child.text:
                    dc_title = child.text.strip().lower()
            for child in item:
                if child.tag.endswith("}res") and child.text and child.text.lower().endswith(filename_ext.lower()):
                    score = 1
                    res_size = int(child.attrib.get("size") or 0)
                    if expected_size_bytes > 0 and res_size == expected_size_bytes:
                        score += 100
                    if stem_low and stem_low in dc_title:
                        score += 20
                    elif clean_hint_low and clean_hint_low in dc_title:
                        score += 10

                    # Serializar DIDL-Lite limpo (sem ns0:/ns3:) para máxima compatibilidade com TV Samsung Tizen e JBL
                    didl_root = ET.Element("{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}DIDL-Lite")
                    didl_root.append(item)
                    single_didl = ET.tostring(didl_root, encoding="unicode")
                    if best_match is None or score > best_match[0]:
                        best_match = (score, child.text.strip(), single_didl)

        if best_match:
            return best_match[1], best_match[2]
    except Exception as e:
        logger.warning(f"Busca no Rygel falhou: {e}")
    return None


# ──────────────────────────────────────────────────────────────
# MICRO-SERVIDOR HTTP RANGE EMBUTIDO PARA CAST DIRETO (TV / JBL)
# Elimina dependência de indexação do Rygel e o bug HEAD Content-Length=0
# ──────────────────────────────────────────────────────────────

def _get_local_ip_for(remote_ip: str) -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((remote_ip, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _media_mime_and_dlna(path: Path, is_atmos: bool = False) -> tuple[str, str, str]:
    """Retorna (mime_type, upnp_class, dlna_features) compatíveis com TV Samsung Tizen e JBL."""
    ext = path.suffix.lower()
    if ext == ".mp4":
        pn = "DLNA.ORG_PN=AVC_MP4_HP_HD_EAC3;" if is_atmos else ""
        return "video/mp4", "object.item.videoItem", f"{pn}DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000"
    if ext == ".mkv":
        return "video/x-mkv", "object.item.videoItem", "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000"
    if ext == ".flac":
        return "audio/flac", "object.item.audioItem.musicTrack", "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000"
    if ext == ".wav":
        return "audio/wav", "object.item.audioItem.musicTrack", "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000"
    return "audio/mp4", "object.item.audioItem.musicTrack", "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000"


def _format_dlna_duration(seconds: int) -> str:
    if seconds <= 0:
        return ""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}.000"


class _DirectCastServer:
    """Servidor HTTP Range leve (thread daemon) para entregar arquivos diretos para TV Samsung / JBL."""

    def __init__(self):
        self._lock = threading.Lock()
        self._files: dict[str, tuple[Path, str, str]] = {}
        self._server: Optional[http.server.ThreadingHTTPServer] = None
        self._port: int = 0

    def ensure_started(self) -> int:
        with self._lock:
            if self._server is not None:
                return self._port

            files_ref = self._files
            lock_ref = self._lock

            class _Handler(http.server.BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def _serve(self, head_only: bool):
                    token = urllib.parse.unquote(self.path.lstrip("/").split("?", 1)[0])
                    with lock_ref:
                        entry = files_ref.get(token)
                    if not entry:
                        self.send_error(404)
                        return
                    fp, mime, dlna_feat = entry
                    if not fp.exists():
                        self.send_error(404)
                        return

                    size = fp.stat().st_size
                    rng = self.headers.get("Range")
                    start, end = 0, size - 1
                    status = 200
                    if rng and rng.lower().startswith("bytes="):
                        spec = rng.split("=", 1)[1].split(",", 1)[0].strip()
                        parts = spec.split("-", 1)
                        try:
                            if parts[0]:
                                start = int(parts[0])
                                end = int(parts[1]) if (len(parts) > 1 and parts[1]) else size - 1
                            elif len(parts) > 1 and parts[1]:
                                suffix_len = int(parts[1])
                                start = max(0, size - suffix_len)
                                end = size - 1
                        except ValueError:
                            start, end = 0, size - 1
                        start = max(0, min(start, size - 1))
                        end = max(start, min(end, size - 1))
                        status = 206

                    length = end - start + 1
                    self.send_response(status)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(length))
                    self.send_header("Accept-Ranges", "bytes")
                    if status == 206:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.send_header("transferMode.dlna.org", "Streaming")
                    self.send_header("contentFeatures.dlna.org", dlna_feat)
                    self.send_header("Connection", "close")
                    self.end_headers()

                    if head_only:
                        return
                    try:
                        with open(fp, "rb") as f:
                            f.seek(start)
                            rem = length
                            while rem > 0:
                                chunk = f.read(min(262144, rem))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                rem -= len(chunk)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass

                def do_HEAD(self):
                    self._serve(head_only=True)

                def do_GET(self):
                    self._serve(head_only=False)

                def log_message(self, format, *args):
                    pass

            srv = http.server.ThreadingHTTPServer(("0.0.0.0", 0), _Handler)
            srv.daemon_threads = True
            self._server = srv
            self._port = int(srv.server_address[1])
            threading.Thread(target=srv.serve_forever, daemon=True, name="direct-cast-http").start()
            logger.info(f"Servidor HTTP direto de cast iniciado na porta {self._port}")
            return self._port

    def register(self, item: LibraryItem, remote_ip: str) -> tuple[str, str]:
        """Registra o arquivo e retorna (url_http, didl_lite_limpo_samsung)."""
        port = self.ensure_started()
        local_ip = _get_local_ip_for(remote_ip)
        mime, upnp_class, dlna_feat = _media_mime_and_dlna(item.path, is_atmos=item.is_atmos)

        ext = item.path.suffix.lower() or ".mp4"
        digest = hashlib.sha1(str(item.path).encode("utf-8")).hexdigest()[:12]
        token = f"track_{digest}{ext}"
        with self._lock:
            self._files[token] = (item.path, mime, dlna_feat)

        url = f"http://{local_ip}:{port}/{token}"
        size = item.path.stat().st_size if item.path.exists() else item.size_bytes
        dur_attr = f' duration="{_format_dlna_duration(item.duration_sec)}"' if item.duration_sec > 0 else ""
        res_attr = ' resolution="1280x720" nrAudioChannels="6" sampleFrequency="48000"' if upnp_class.endswith("videoItem") else ""
        pinfo = f"http-get:*:{mime}:{dlna_feat}"

        # DIDL-Lite minimalista compatível com Samsung Tizen (1 único <res>, sem thumbnail PNG falsa nem dlnaManaged!)
        didl = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/" '
            'xmlns:sec="http://www.sec.co.kr/">'
            '<item id="1" parentID="0" restricted="1">'
            f'<dc:title>{html.escape(item.title)}</dc:title>'
            f'<upnp:class>{upnp_class}</upnp:class>'
            f'<res size="{size}"{dur_attr}{res_attr} protocolInfo="{pinfo}">{html.escape(url)}</res>'
            '</item></DIDL-Lite>'
        )
        return url, didl

    def shutdown(self) -> None:
        with self._lock:
            srv = self._server
            self._server = None
            self._port = 0
            self._files.clear()
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass


_DIRECT_CAST_SERVER = _DirectCastServer()


def shutdown_cast_server() -> None:
    """Encerra o micro-servidor HTTP Range embutido ao fechar o aplicativo."""
    _DIRECT_CAST_SERVER.shutdown()


def play_on_renderer(item: LibraryItem, renderer: DlnaRenderer) -> tuple[bool, str]:
    """
    Envia uma faixa da biblioteca diretamente para a TV Samsung / JBL via UPnP AVTransport
    usando o servidor HTTP Range embutido (Content-Length real + DIDL-Lite limpo sem bugs do Rygel).
    """
    if renderer.is_jbl and (item.is_video_wrapped or item.is_atmos or item.is_surround or item.path.suffix.lower() in (".mp4", ".mkv")):
        return (
            False,
            "A JBL Bar 1000MK2 não aceita áudio multicanal/Atmos direto por DLNA — envie para a TV Samsung (a3) via HDMI eARC.",
        )

    if not item.path.exists():
        return False, f"Arquivo não encontrado no disco: {item.filename}"

    media_url, didl_meta = _DIRECT_CAST_SERVER.register(item, renderer.ip)
    escaped_meta = html.escape(didl_meta)
    escaped_url = html.escape(media_url)

    stop_body = '''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
      <InstanceID>0</InstanceID>
    </u:Stop>
  </s:Body>
</s:Envelope>'''

    set_uri_body = f'''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
      <InstanceID>0</InstanceID>
      <CurrentURI>{escaped_url}</CurrentURI>
      <CurrentURIMetaData>{escaped_meta}</CurrentURIMetaData>
    </u:SetAVTransportURI>
  </s:Body>
</s:Envelope>'''

    play_body = '''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
      <InstanceID>0</InstanceID>
      <Speed>1</Speed>
    </u:Play>
  </s:Body>
</s:Envelope>'''

    info_body = '''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
      <InstanceID>0</InstanceID>
    </u:GetTransportInfo>
  </s:Body>
</s:Envelope>'''

    try:
        # Limpar estado anterior (ex.: TRANSITIONING travado) antes de carregar a nova faixa
        try:
            requests.post(
                renderer.av_transport_url,
                data=stop_body.encode("utf-8"),
                headers={
                    "Content-Type": 'text/xml; charset="utf-8"',
                    "SOAPAction": '"urn:schemas-upnp-org:service:AVTransport:1#Stop"',
                },
                timeout=3,
            )
            time.sleep(0.25)
        except Exception:
            pass

        r1 = requests.post(
            renderer.av_transport_url,
            data=set_uri_body.encode("utf-8"),
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"',
            },
            timeout=5,
        )
        if r1.status_code != 200:
            return False, f"{renderer.name} recusou a faixa (HTTP {r1.status_code})."

        # TVs Samsung Tizen precisam de ~0.7s após SetAVTransportURI para abrir o player antes do Play
        if renderer.is_tv:
            time.sleep(0.7)

        r2 = requests.post(
            renderer.av_transport_url,
            data=play_body.encode("utf-8"),
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": '"urn:schemas-upnp-org:service:AVTransport:1#Play"',
            },
            timeout=5,
        )
        if r2.status_code != 200:
            return False, f"{renderer.name} falhou ao iniciar reprodução (HTTP {r2.status_code})."

        # Confirmar se a TV não retornou ERROR_OCCURRED imediatamente
        if renderer.is_tv:
            time.sleep(1.5)
            try:
                r3 = requests.post(
                    renderer.av_transport_url,
                    data=info_body.encode("utf-8"),
                    headers={
                        "Content-Type": 'text/xml; charset="utf-8"',
                        "SOAPAction": '"urn:schemas-upnp-org:service:AVTransport:1#GetTransportInfo"',
                    },
                    timeout=3,
                )
                if "ERROR_OCCURRED" in r3.text:
                    return (
                        False,
                        f"{renderer.name} reportou erro interno no player Tizen — tente segurar Power 5s na TV ou tocar pelo BubbleUPnP.",
                    )
            except Exception:
                pass

        return True, f"Tocando em {renderer.name}"
    except Exception as e:
        return False, f"Erro ao comunicar com {renderer.name}: {e}"


def stop_renderers(renderers: list[DlnaRenderer]) -> tuple[bool, str]:
    """Envia comando UPnP Stop para os reprodutores descobertos (TV Samsung / JBL)."""
    if not renderers:
        renderers = discover_renderers()
    if not renderers:
        return False, "Nenhum reprodutor DLNA encontrado na rede."

    stop_body = '''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
      <InstanceID>0</InstanceID>
    </u:Stop>
  </s:Body>
</s:Envelope>'''

    stopped_names: list[str] = []
    for rnd in renderers:
        try:
            r = requests.post(
                rnd.av_transport_url,
                data=stop_body.encode("utf-8"),
                headers={
                    "Content-Type": 'text/xml; charset="utf-8"',
                    "SOAPAction": '"urn:schemas-upnp-org:service:AVTransport:1#Stop"',
                },
                timeout=3,
            )
            if r.status_code == 200:
                stopped_names.append(rnd.name)
        except Exception:
            pass

    if stopped_names:
        return True, f"Reprodução parada ({', '.join(stopped_names)})."
    return False, "Não foi possível parar os reprodutores agora."
