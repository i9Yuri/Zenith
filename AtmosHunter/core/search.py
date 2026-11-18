# Copyright (c) 2026 Yuri Barbosa (@i9Yuri). All Rights Reserved.
# Proprietary source-available software (personal, non-commercial use only).
# Official repository: https://github.com/i9Yuri/Zenith
# Unauthorized commercial use, redistribution, or rebranding is prohibited. See LICENSE.
"""
AtmosHunter — Motor Híbrido Multi-Fontes de Busca Inteligente (v2.3)
Pipeline de busca:
  0. QueryResolver (Google Suggest + Deezer + iTunes): corrige erros de digitação e identifica
     o artista + título canônico (ex: 'lsoe my mind' -> 'Don Toliver Lose My Mind').
  1. Soulseek Atmos Explícito ('<artista titulo> atmos' / '<titulo> atmos')
  2. Soulseek M4A / Multicanal ('<artista titulo> m4a') — captura faixas Dolby Atmos E-AC-3 JOC
     salvas sem a palavra 'atmos' no nome da pasta (ex: 'Multichannel Music') + ALAC Ultra HD!
  3. Hi-Res Direct (tracks.monochrome.st) — FLAC 24-bit oficial via HTTP direto
  4. Internet Archive (archive.org) — FLAC / áudio sem perdas aberto via HTTP direto
  5. Fallback Surround 5.1 no Soulseek caso nenhum Atmos tenha retornado
"""
from __future__ import annotations

import logging
import os
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import requests

from core.query_resolver import ResolvedQuery, resolve_music_query
from core.ranker import TrackResult, format_size
from core.slsk_service import SoulseekService, get_slsk_service

logger = logging.getLogger("AtmosHunter.search")

TRACKS_API_BASE = "https://tracks.monochrome.st"
ARCHIVE_SEARCH_URL = "https://archive.org/advancedsearch.php"
ARCHIVE_META_URL = "https://archive.org/metadata/{identifier}"

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _ascii_clean(text: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-zA-Z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class HiResDirectProvider:
    """Busca faixas individuais oficiais em FLAC Lossless / 24-bit via tracks.monochrome.st."""

    name = "Hi-Res Direct"

    def search(self, queries: list[str], limit: int = 10) -> list[TrackResult]:
        seen_ids: set[str] = set()
        results: list[TrackResult] = []

        for q in queries:
            clean_q = re.sub(r"\b(dolby\s*atmos|atmos|eac3|truehd|flac|spatial)\b", "", q, flags=re.I)
            clean_q = re.sub(r"\s+", " ", clean_q).strip() or q.strip()
            if not clean_q:
                continue

            try:
                r = requests.get(
                    f"{TRACKS_API_BASE}/search/tracks",
                    params={"q": clean_q, "limit": limit},
                    headers=HTTP_HEADERS,
                    timeout=7,
                )
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                logger.warning(f"[Hi-Res Direct] busca '{clean_q}' falhou: {e}")
                continue

            raw_tracks = data.get("tracks", []) if isinstance(data, dict) else []
            for item in raw_tracks[:limit]:
                tid = str(item.get("trackId") or item.get("id") or "").strip()
                title = str(item.get("title") or "").strip()
                if not tid or not title or tid in seen_ids:
                    continue
                seen_ids.add(tid)

                artists = item.get("artistNames") or []
                if not artists and isinstance(item.get("artists"), list):
                    artists = [a.get("name", "") for a in item["artists"] if isinstance(a, dict)]
                artist = ", ".join(a for a in artists if a) or "Artista Desconhecido"

                album = str(item.get("releaseTitle") or item.get("albumTitle") or "").strip()
                duration = int(item.get("duration") or 0)
                if duration > 10000:
                    duration = duration // 1000

                est_bytes = int(item.get("size") or (max(duration, 180) * 180_000))
                artwork = str(item.get("artwork") or "")
                safe_fn = f"{artist} - {title}.flac"

                results.append(
                    TrackResult(
                        title=title,
                        artist=artist,
                        album=album,
                        filename=safe_fn,
                        extension=".flac",
                        codec_label="FLAC ULTRA HD Estéreo",
                        codec_tier=4,
                        bit_depth=24,
                        size_bytes=est_bytes,
                        size_label=f"~{format_size(est_bytes)}" if not item.get("size") else format_size(est_bytes),
                        duration_sec=duration,
                        speed_bps=8 * 1024 * 1024,
                        has_free_slot=True,
                        queue_length=0,
                        source=self.name,
                        provider="http",
                        download_url=f"{TRACKS_API_BASE}/track/{tid}",
                        artwork_url=artwork,
                    )
                )

            if len(results) >= limit:
                break

        return results


class InternetArchiveProvider:
    """Busca faixas individuais sem perdas (FLAC / M4A) no acervo público do Internet Archive."""

    name = "Internet Archive"

    def search(self, query: str, limit: int = 4) -> list[TrackResult]:
        tokens = [t for t in _ascii_clean(query).split() if len(t) > 2][:4]
        if not tokens:
            return []

        q_expr = " AND ".join(f'title:"{t}"' for t in tokens)
        full_q = f"({q_expr}) AND mediatype:audio AND format:FLAC"
        try:
            r = requests.get(
                ARCHIVE_SEARCH_URL,
                params={
                    "q": full_q,
                    "fl[]": ["identifier", "title", "creator"],
                    "rows": 3,
                    "output": "json",
                },
                headers=HTTP_HEADERS,
                timeout=6,
            )
            r.raise_for_status()
            docs = (r.json().get("response") or {}).get("docs") or []
        except Exception as e:
            logger.debug(f"[Internet Archive] busca ignorada: {e}")
            return []

        results: list[TrackResult] = []
        for doc in docs[:3]:
            ident = doc.get("identifier")
            if not ident:
                continue
            creator = doc.get("creator")
            if isinstance(creator, list):
                creator = creator[0] if creator else ""
            creator = str(creator or "").strip()
            album_title = str(doc.get("title") or "").strip()

            try:
                rm = requests.get(ARCHIVE_META_URL.format(identifier=ident), headers=HTTP_HEADERS, timeout=5)
                if rm.status_code != 200:
                    continue
                files = rm.json().get("files") or []
            except Exception:
                continue

            for f in files:
                fname = str(f.get("name") or "")
                if not fname.lower().endswith((".flac", ".m4a")):
                    continue
                size_b = int(f.get("size") or 0)
                if size_b < 5 * 1024 * 1024 or size_b > 250 * 1024 * 1024:
                    continue
                track_title = str(f.get("title") or os.path.splitext(os.path.basename(fname))[0]).strip()
                # Verificar se bate minimamente com os tokens buscados
                low_all = f"{creator} {track_title} {fname}".lower()
                if not any(t.lower() in low_all for t in tokens):
                    continue
                dur = 0
                try:
                    dur = int(float(f.get("length") or 0))
                except Exception:
                    pass
                dl_url = f"https://archive.org/download/{ident}/{requests.utils.quote(fname)}"
                results.append(
                    TrackResult(
                        title=track_title,
                        artist=str(f.get("artist") or creator or ""),
                        album=album_title,
                        filename=os.path.basename(fname),
                        extension=os.path.splitext(fname)[1].lower(),
                        size_bytes=size_b,
                        size_label=format_size(size_b),
                        duration_sec=dur,
                        speed_bps=4 * 1024 * 1024,
                        has_free_slot=True,
                        source=self.name,
                        provider="http",
                        download_url=dl_url,
                    )
                )
                if len(results) >= limit:
                    return results

        return results


class SearchEngine:
    """
    Orquestrador de busca inteligente com autocorreção de typos e múltiplas fontes:
      - QueryResolver (Google Suggest + Deezer + iTunes)
      - Soulseek (estratégia dupla: 'atmos' + 'm4a' para capturar Atmos implícito e ALAC!)
      - Hi-Res Direct (tracks.monochrome.st)
      - Internet Archive (archive.org)
    """

    def __init__(
        self,
        slsk_service: Optional[SoulseekService] = None,
        progress_cb: Optional[Callable[[str, list[TrackResult]], None]] = None,
        resolved_cb: Optional[Callable[[ResolvedQuery], None]] = None,
    ):
        self.slsk = slsk_service or get_slsk_service()
        self.hires = HiResDirectProvider()
        self.archive = InternetArchiveProvider()
        self.progress_cb = progress_cb
        self.resolved_cb = resolved_cb
        self.last_resolved: Optional[ResolvedQuery] = None
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def _build_slsk_queries(self, rq: ResolvedQuery) -> list[str]:
        """
        Gera 2 queries estratégicas e complementares para o Soulseek:
          1. '<base> atmos' (encontra pastas rotuladas com Dolby Atmos / TrueHD)
          2. '<artista titulo> m4a' (encontra faixas Dolby Atmos E-AC-3 JOC em pastas
             'Multichannel Music' / 'Spatial' sem a palavra 'atmos' + ALAC 24-bit!)
        """
        canon = _ascii_clean(rq.canonical_full) if rq.canonical_full else _ascii_clean(rq.corrected)
        short_title = _ascii_clean(rq.canonical_title) if rq.canonical_title else _ascii_clean(rq.corrected)

        queries: list[str] = []
        # Query 1: Atmos explícito (curta o suficiente para maximizar acerto)
        q_atmos_base = short_title if (short_title and len(short_title.split()) >= 2 and not rq.canonical_artist) else canon
        if q_atmos_base:
            queries.append(f"{q_atmos_base} atmos")

        # Query 2: M4A com artista + título canônico (precisão cirúrgica p/ pegar kingbushido e afins!)
        if canon and len(canon.split()) >= 2:
            queries.append(f"{canon} m4a")
        elif short_title:
            queries.append(f"{short_title} m4a")

        # Deduplicar preservando ordem
        seen = set()
        out = []
        for q in queries:
            ql = q.lower()
            if ql not in seen:
                seen.add(ql)
                out.append(q)
        return out

    def search(self, query: str) -> list[TrackResult]:
        self._stop_event.clear()

        # 0. Autocorreção e resolução canônica rápida (<400ms)
        rq = resolve_music_query(query)
        self.last_resolved = rq
        if self.resolved_cb:
            try:
                self.resolved_cb(rq)
            except Exception:
                pass

        collected: list[TrackResult] = []
        slsk_atmos_hits = [0]
        lock = threading.Lock()

        def _emit(source: str, batch: list[TrackResult]):
            if not batch or self._stop_event.is_set():
                return
            with lock:
                collected.extend(batch)
                if source == "Soulseek":
                    for item in batch:
                        low = f"{item.remote_path} {item.filename}".lower()
                        if any(k in low for k in ("atmos", "multichannel", "spatial", "eac3", "joc", "truehd")):
                            slsk_atmos_hits[0] += 1
            if self.progress_cb:
                self.progress_cb(source, batch)

        def _run_hires():
            if self._stop_event.is_set():
                return
            h_queries = []
            if rq.canonical_full:
                h_queries.append(rq.canonical_full)
            if rq.corrected and rq.corrected.lower() not in [x.lower() for x in h_queries]:
                h_queries.append(rq.corrected)
            if query.strip().lower() not in [x.lower() for x in h_queries]:
                h_queries.append(query.strip())
            res = self.hires.search(h_queries, limit=10)
            _emit("Hi-Res Direct", res)

        def _run_archive():
            if self._stop_event.is_set():
                return
            res = self.archive.search(rq.canonical_full or rq.corrected, limit=3)
            _emit("Internet Archive", res)

        def _run_slsk():
            if self._stop_event.is_set():
                return
            queries = self._build_slsk_queries(rq)
            got_n = self.slsk.search(
                queries=queries,
                on_batch=lambda batch: _emit("Soulseek", batch),
                duration_sec=7.5,
                stop_event=self._stop_event,
            )

            # Se retornou ZERO resultados no Soulseek (ex.: sessão silenciosamente estagnada/rate-limited),
            # força reconexão limpa e repete uma vez antes de desistir!
            if got_n == 0 and not self._stop_event.is_set():
                logger.info("Soulseek retornou 0 faixas; reconectando sessão e tentando novamente...")
                self.slsk.reconnect(force_new_account=True)
                if self.slsk.wait_until_ready(timeout=12.0) and not self._stop_event.is_set():
                    got_n = self.slsk.search(
                        queries=queries[:1],
                        on_batch=lambda batch: _emit("Soulseek", batch),
                        duration_sec=6.5,
                        stop_event=self._stop_event,
                    )

            # Se ainda não veio nenhum Atmos/Multicanal, disparar fallback inteligente:
            # 1) '<titulo_curto> atmos' (encontra peers que não repetem o artista no caminho!)
            # 2) '<canon> 5.1 flac' (encontra Surround 5.1/7.1 discreto)
            if slsk_atmos_hits[0] == 0 and not self._stop_event.is_set():
                extra_q = []
                short_t = _ascii_clean(rq.canonical_title or rq.corrected)
                canon = _ascii_clean(rq.canonical_full or rq.corrected)
                existing_low = {q.lower() for q in queries}
                if short_t and len(short_t) >= 4 and f"{short_t} atmos".lower() not in existing_low:
                    extra_q.append(f"{short_t} atmos")
                if canon and f"{canon} 5.1".lower() not in existing_low:
                    extra_q.append(f"{canon} 5.1")
                if extra_q:
                    self.slsk.search(
                        queries=extra_q,
                        on_batch=lambda batch: _emit("Soulseek", batch),
                        duration_sec=6.0,
                        stop_event=self._stop_event,
                    )

        with ThreadPoolExecutor(max_workers=3) as pool:
            f_hires = pool.submit(_run_hires)
            f_arch = pool.submit(_run_archive)
            f_slsk = pool.submit(_run_slsk)
            f_hires.result()
            f_arch.result()
            f_slsk.result()

        return collected
