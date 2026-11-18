# Copyright (c) 2026 Yuri Barbosa (@i9Yuri). All Rights Reserved.
# Proprietary source-available software (personal, non-commercial use only).
# Official repository: https://github.com/i9Yuri/Zenith
# Unauthorized commercial use, redistribution, or rebranding is prohibited. See LICENSE.
"""
AtmosHunter — Resolvedor de Intenção Musical e Autocorreção de Typos
Combina:
  1. Google Suggest (correção rápida de erros de digitação, ex: 'bliding lights' -> 'blinding lights')
  2. Deezer Public API (identificação do artista + título canônico mais popular, tolerante a typos)
  3. iTunes Search API (fallback caso o Deezer não responda)
Tudo com timeout curto (<2.5s) e fallback seguro para a query original se offline.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import requests

logger = logging.getLogger("AtmosHunter.resolver")

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}


@dataclass
class ResolvedQuery:
    original: str
    corrected: str
    canonical_artist: str = ""
    canonical_title: str = ""
    canonical_album: str = ""
    canonical_full: str = ""
    was_corrected: bool = False
    alt_queries: list[str] = field(default_factory=list)

    @property
    def display_hint(self) -> str:
        if self.canonical_artist and self.canonical_title:
            return f"{self.canonical_artist} - {self.canonical_title}"
        if self.was_corrected and self.corrected.lower() != self.original.lower():
            return self.corrected
        return ""


def _strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )


def _clean_music_text(text: str) -> str:
    """Remove termos técnicos e sufixos entre parênteses/colchetes tipo '(feat. ...)' ou '[From ...]'."""
    t = re.sub(r"\b(dolby\s*atmos|atmos|eac3|e-ac-3|truehd|flac|spatial|hi-?res)\b", "", text, flags=re.I)
    t = re.sub(
        r"\s*[\(\[][^\)\]]*(?:\bfeat\b|\bft\.|\bfrom\b|\bremaster|\bofficial|\bradio\s+edit|\bsingle\s+version|\bmovie\b|\balbum\b)[^\)\]]*[\)\]]",
        "",
        t,
        flags=re.I,
    )
    t = re.sub(r"\s+", " ", t).strip(" -")
    return t


def _fuzzy_ratio(a: str, b: str) -> float:
    na = re.sub(r"[^a-z0-9 ]+", "", _strip_accents(a.lower())).strip()
    nb = re.sub(r"[^a-z0-9 ]+", "", _strip_accents(b.lower())).strip()
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _google_spell_suggest(query: str) -> str:
    """Usa Google Suggest para corrigir typos de digitação ('bliding lights' -> 'blinding lights')."""
    try:
        r = requests.get(
            "https://suggestqueries.google.com/complete/search",
            params={"client": "firefox", "q": f"{query} song"},
            headers=HTTP_HEADERS,
            timeout=2.2,
        )
        if r.status_code != 200:
            return query
        payload = r.json()
        suggestions = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
        for raw_s in suggestions[:4]:
            cand = re.sub(r"\s+(?:song|lyrics|music|audio|meaning|chords|mp3|download)\b.*$", "", str(raw_s), flags=re.I).strip()
            if not cand:
                continue
            # Aceitar sugestão apenas se for realmente uma correção próxima do que foi digitado
            sim = _fuzzy_ratio(query, cand)
            if sim >= 0.72 and cand.lower() != query.lower():
                return cand
    except Exception as e:
        logger.debug(f"Google Suggest ignorado: {e}")
    return query


def _deezer_lookup(query: str) -> list[dict]:
    try:
        r = requests.get(
            "https://api.deezer.com/search",
            params={"q": query, "limit": 8},
            headers=HTTP_HEADERS,
            timeout=3.0,
        )
        if r.status_code != 200:
            return []
        items = r.json().get("data") or []
        results = []
        for it in items:
            title = str(it.get("title") or "").strip()
            artist = str((it.get("artist") or {}).get("name") or "").strip()
            album = str((it.get("album") or {}).get("title") or "").strip()
            rank = int(it.get("rank") or 0)
            if title and artist:
                results.append({"artist": artist, "title": title, "album": album, "rank": rank})
        return results
    except Exception as e:
        logger.debug(f"Deezer lookup ignorado: {e}")
        return []


def _itunes_lookup(query: str) -> list[dict]:
    try:
        r = requests.get(
            "https://itunes.apple.com/search",
            params={"term": query, "media": "music", "entity": "song", "limit": 6},
            headers=HTTP_HEADERS,
            timeout=3.0,
        )
        if r.status_code != 200:
            return []
        items = r.json().get("results") or []
        results = []
        for idx, it in enumerate(items):
            title = str(it.get("trackName") or "").strip()
            artist = str(it.get("artistName") or "").strip()
            album = str(it.get("collectionName") or "").strip()
            if title and artist:
                results.append({"artist": artist, "title": title, "album": album, "rank": max(1, 500000 - idx * 50000)})
        return results
    except Exception as e:
        logger.debug(f"iTunes lookup ignorado: {e}")
        return []


def resolve_music_query(raw_query: str) -> ResolvedQuery:
    """
    Resolve erros de digitação e identifica o artista + título canônico da música.
    Ex:
      'lsoe my mind'       -> Don Toliver - Lose My Mind
      'bliding lights'     -> The Weeknd - Blinding Lights
      'wham last cristmas' -> Wham! - Last Christmas
      'Lose my mind'       -> Don Toliver - Lose My Mind
    """
    original = raw_query.strip()
    cleaned = _clean_music_text(original) or original

    # 1. Autocorreção ortográfica leve (Google Suggest)
    corrected = _google_spell_suggest(cleaned)
    was_corrected = _strip_accents(corrected.lower()) != _strip_accents(cleaned.lower())

    # 2. Resolver artista e faixa mais popular (Deezer -> fallback iTunes)
    candidates = _deezer_lookup(corrected)
    if not candidates and was_corrected:
        candidates = _deezer_lookup(cleaned)
    if not candidates:
        candidates = _itunes_lookup(corrected)

    if not candidates:
        return ResolvedQuery(
            original=original,
            corrected=corrected,
            canonical_full=corrected,
            was_corrected=was_corrected,
        )

    # Pontuar candidatos combinando popularidade (rank) e aderência fuzzy à busca
    def _cand_score(c: dict) -> float:
        clean_t = _clean_music_text(c["title"])
        sim_title = _fuzzy_ratio(corrected, clean_t)
        sim_full = _fuzzy_ratio(corrected, f"{c['artist']} {clean_t}")
        sim = max(sim_title, sim_full)
        # Normalizar rank do Deezer (0..1_000_000)
        pop = min(1.0, c.get("rank", 0) / 850_000.0)
        low_t = c["title"].lower()
        penalty = 0.35 if any(w in low_t for w in ("cover", "karaoke", "lofi", "tribute", "instrumental")) else 0.0
        # Quando o título bate bem (>=0.72), popularidade desempata fortemente a versão original famosa
        if sim >= 0.72:
            return (sim * 0.48) + (pop * 0.52) - penalty
        return (sim * 0.72) + (pop * 0.28) - penalty

    candidates.sort(key=_cand_score, reverse=True)
    best = candidates[0]

    best_artist = best["artist"].strip()
    best_title = _clean_music_text(best["title"]) or best["title"].strip()
    best_album = best.get("album", "").strip()

    # Verificar se o melhor candidato faz sentido (evita substituir algo totalmente diferente)
    best_sim = max(
        _fuzzy_ratio(corrected, best_title),
        _fuzzy_ratio(corrected, f"{best_artist} {best_title}"),
        _fuzzy_ratio(cleaned, best_title),
        _fuzzy_ratio(cleaned, f"{best_artist} {best_title}"),
    )
    if best_sim < 0.42:
        return ResolvedQuery(
            original=original,
            corrected=corrected,
            canonical_full=corrected,
            was_corrected=was_corrected,
        )

    # Montar lista de queries alternativas (ex: segundo artista diferente para títulos ambíguos)
    alts: list[str] = []
    seen_artists = {best_artist.lower()}
    for c in candidates[1:4]:
        a = c["artist"].strip()
        t = _clean_music_text(c["title"])
        if a.lower() not in seen_artists and _fuzzy_ratio(corrected, t) >= 0.75:
            seen_artists.add(a.lower())
            alts.append(f"{a} {t}")
            if len(alts) >= 1:
                break

    canonical_full = f"{best_artist} {best_title}".strip()
    # Considerar corrigido se houve typo OU se preenchemos o artista faltante
    if _strip_accents(best_title.lower()) not in _strip_accents(cleaned.lower()):
        was_corrected = True

    return ResolvedQuery(
        original=original,
        corrected=best_title if len(cleaned.split()) <= 4 and best_artist.lower() not in cleaned.lower() else canonical_full,
        canonical_artist=best_artist,
        canonical_title=best_title,
        canonical_album=best_album,
        canonical_full=canonical_full,
        was_corrected=was_corrected,
        alt_queries=alts,
    )
