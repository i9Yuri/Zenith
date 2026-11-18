# Copyright (c) 2026 Yuri Barbosa (@i9Yuri). All Rights Reserved.
# Proprietary source-available software (personal, non-commercial use only).
# Official repository: https://github.com/i9Yuri/Zenith
# Unauthorized commercial use, redistribution, or rebranding is prohibited. See LICENSE.
"""
AtmosHunter — Suíte Completa de Testes (v2.4 — Unitários + Playlists Multi-Plataforma + E2E + Estresse GUI)
Executar:
  QT_QPA_PLATFORM=offscreen .venv/bin/python3 -W default tests/test_atmoshunter.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Usar sessão Soulseek efêmera nos testes para NUNCA derrubar uma janela do AtmosHunter já aberta pelo usuário!
os.environ.setdefault("ATMOSHUNTER_EPHEMERAL_SLSK", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.dlna_bridge import (
    check_rygel_status,
    discover_renderers,
    scan_local_library,
    summarize_playlists,
)
from core.downloader import (
    DOWNLOAD_DIR,
    _build_target_path,
    probe_audio_file,
    remux_atmos_for_tv_soundbar,
)
from core.playlist_importer import (
    STATUS_DOWNGRADED_STEREO,
    STATUS_DOWNGRADED_SURROUND,
    STATUS_FAILED,
    STATUS_OPTIMAL_ATMOS,
    ParsedPlaylist,
    PlaylistImportReport,
    PlaylistImportRunner,
    PlaylistTrack,
    parse_playlist_input,
    select_ranked_candidates_for_playlist_track,
)
from core.query_resolver import resolve_music_query
from core.ranker import (
    TrackResult,
    clean_track_metadata_from_path,
    is_junk,
    rank_results,
)
from core.slsk_service import get_slsk_service
from core.tv_control import SamsungTVController, TVConfig, build_wol_packet
from core.audio_verify import (
    classify_probed_audio,
    inspect_mp4_prefix,
    lookup_verified_track,
    record_verified_track,
    reset_cache_memory,
)


def test_ranker_and_filters():
    print("[1/7] Testando ranker, filtro anti-lixo e tags...")

    artist, title, album, fn = clean_track_metadata_from_path(
        r"@@abc\Music\Wham!\2024 - Make It Big (EU, Sony - Epic, 19658870759)\04 Bonus Tracks 1 Dolby Atmos\02 - Last Christmas (Pudding Mix).mka"
    )
    assert artist == "Wham!", f"Artista incorreto: {artist!r}"
    assert title == "Last Christmas (Pudding Mix)", f"Título incorreto: {title!r}"

    junk_cases = [
        TrackResult(title="Last Christmas 2019", filename="Last.Christmas.2019.2160p.UHD.Blu-ray.HEVC.TrueHD.Atmos.mkv", extension=".mkv", size_bytes=52 * 1024**3),
        TrackResult(title="Last Christmas", filename="The.Summer.I.Turned.Pretty.S03E02.Last.Christmas.1080p.AMZN.WEB-DL.DDP5.1.Atmos.H.264.mp4", extension=".mp4", size_bytes=3900 * 1024**2),
        TrackResult(title="Last Christmas", filename="Wham - Discography.zip", extension=".zip", size_bytes=900 * 1024**2),
        TrackResult(title="Last Christmas", filename="Wham - Last Christmas.mp3", extension=".mp3", size_bytes=10 * 1024**2),
        TrackResult(title="Last Christmas", filename="preview.m4a", extension=".m4a", size_bytes=500 * 1024),
    ]
    for jc in junk_cases:
        j, _ = is_junk(jc)
        assert j, f"Deveria ser lixo: {jc.filename}"

    t_joc = TrackResult(
        title="Last Christmas (Single Version)", artist="Wham!",
        filename="01 Last Christmas.m4a", extension=".m4a",
        remote_path=r"Music\Wham! - Last Christmas [Dolby Atmos]\01 Last Christmas.m4a",
        size_bytes=27 * 1024**2, duration_sec=267, speed_bps=2_200_000, has_free_slot=True,
        provider="soulseek", source="Soulseek",
    )
    t_truehd = TrackResult(
        title="Last Christmas", artist="Wham!",
        filename="01 Last Christmas.mka", extension=".mka",
        remote_path=r"Music\Wham! [Dolby Atmos TrueHD]\01 Last Christmas.mka",
        size_bytes=260 * 1024**2, duration_sec=267, speed_bps=3_000_000, has_free_slot=True,
        provider="soulseek", source="Soulseek",
    )
    t_surround = TrackResult(
        title="Last Christmas", artist="Wham!",
        filename="01 Last Christmas.flac", extension=".flac",
        remote_path=r"Music\Wham! [Dolby Atmos]\01 Last Christmas.flac",
        size_bytes=126 * 1024**2, bit_depth=24, speed_bps=4_000_000, has_free_slot=True,
        provider="soulseek", source="Soulseek",
    )
    t_hires = TrackResult(
        title="Last Christmas", artist="Wham!",
        filename="Wham! - Last Christmas.flac", extension=".flac",
        size_bytes=45 * 1024**2, bit_depth=24, provider="http", source="Hi-Res Direct",
    )

    ranked = rank_results([t_hires, t_surround, t_truehd, t_joc] + junk_cases, query="last christmas", drop_junk=True)
    assert len(ranked) == 4
    assert ranked[0].quality_label == "DD+ JOC" and ranked[0].channels_label == "7.1.4"
    assert ranked[1].quality_label == "LOSSLESS" and ranked[1].channels_label == "7.1.4"
    assert ranked[2].spatial_label == "SURROUND" and ranked[2].channels_label == "7.1"
    assert ranked[3].spatial_label == "ESTÉREO" and ranked[3].quality_label == "ULTRA HD"

    # 4. Regressão da print 'the reason':
    #    - 'The Chemical Brothers - No Reason' (Atmos), 'Pure Reason Revolution - The Gallows',
    #      'King Crimson - Epitaph Including March For No Reason' e 'Hoobastank - The Reason (Wedding Edition 8 MB AAC)'
    #      NUNCA podem ficar acima de 'Hoobastank - The Reason' (FLAC Ultra HD)!
    tr_hoob_flac = TrackResult(
        title="The Reason", artist="Hoobastank", album="The Reason",
        filename="Hoobastank - The Reason.flac", extension=".flac",
        size_bytes=40 * 1024**2, duration_sec=233, bit_depth=24, provider="http", source="Hi-Res Direct",
    )
    tr_hoob_wedding_fake = TrackResult(
        title="The Reason", artist="Hoobastank", album="KuschelRock - Always & Forever (Wedding Edition)",
        filename="106 - Hoobastank - The Reason.m4a", extension=".m4a",
        remote_path=r"Music\Always & Forever (Wedding Edition)\106 - Hoobastank - The Reason.m4a",
        size_bytes=8 * 1024**2, duration_sec=234, speed_bps=2_500_000, has_free_slot=True, provider="soulseek", source="Soulseek",
    )
    tr_chem_no_reason = TrackResult(
        title="No Reason", artist="The Chemical Brothers", album="For That Beautiful Feeling (Atmos)",
        filename="04 - No Reason.m4a", extension=".m4a",
        remote_path=r"Music\The Chemical Brothers\For That Beautiful Feeling (Atmos)\04 - No Reason.m4a",
        size_bytes=28 * 1024**2, duration_sec=292, speed_bps=4_500_000, has_free_slot=True, provider="soulseek", source="Soulseek",
    )
    tr_baby_rose = TrackResult(
        title="The Reason", artist="Baby Rose", album="YEARNALISM",
        filename="02 - The Reason.m4a", extension=".m4a",
        remote_path=r"Music\Baby Rose [Dolby Atmos]\02 - The Reason.m4a",
        size_bytes=21 * 1024**2, duration_sec=227, speed_bps=1_100_000, has_free_slot=True, provider="soulseek", source="Soulseek",
    )
    tr_pure_reason = TrackResult(
        title="The Gallows", artist="Pure Reason Revolution", album="Coming Up To Consciousness",
        filename="03 - The Gallows.flac", extension=".flac",
        remote_path=r"Music\Pure Reason Revolution [5.1]\03 - The Gallows.flac",
        size_bytes=108 * 1024**2, duration_sec=275, bit_depth=24, provider="soulseek", source="Soulseek",
    )
    tr_king_crimson = TrackResult(
        title="Epitaph Including March For No Reason And Tomorrow And Tomorrow", artist="King Crimson",
        filename="03 - Epitaph.flac", extension=".flac",
        remote_path=r"Music\King Crimson (Atmos)\03 - Epitaph.flac",
        size_bytes=148 * 1024**2, duration_sec=539, bit_depth=24, provider="soulseek", source="Soulseek",
    )
    tr_david_byrne = TrackResult(
        title="What Is The Reason For It (feat. Hayley Williams)", artist="David Byrne",
        filename="05 - What Is The Reason For It.m4a", extension=".m4a",
        remote_path=r"Music\David Byrne (Dolby Atmos)\05 - What Is The Reason For It.m4a",
        size_bytes=24 * 1024**2, duration_sec=190, provider="soulseek", source="Soulseek",
    )

    ranked_reason = rank_results(
        [tr_chem_no_reason, tr_hoob_wedding_fake, tr_baby_rose, tr_pure_reason, tr_king_crimson, tr_david_byrne, tr_hoob_flac],
        query="the reason",
        drop_junk=True,
        canonical_query="Hoobastank The Reason",
        canonical_artist="Hoobastank",
        canonical_title="The Reason",
    )
    names_reason = [r.display_name for r in ranked_reason]
    assert ranked_reason[0].artist == "Hoobastank" and ranked_reason[0].extension == ".flac", (
        f"#1 deveria ser Hoobastank - The Reason (FLAC), veio: {names_reason}"
    )
    assert all("Chemical Brothers" not in n for n in names_reason), f"No Reason vazou: {names_reason}"
    assert all("Pure Reason" not in n for n in names_reason), f"The Gallows vazou: {names_reason}"
    assert all("King Crimson" not in n for n in names_reason), f"King Crimson vazou: {names_reason}"
    assert all("Wedding" not in r.album for r in ranked_reason), "AAC 8MB Wedding Edition vazou como Atmos!"
    print("  OK: ranker, anti-lixo, anti-falso-Atmos (Wedding 8MB) e relevância estrita ('the reason') 100% válidos")


def test_anti_collision_and_fast_remux():
    print("[2/7] Testando anti-sobrescrita, prefixo de playlist e remux rápido (<5s)...")
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        tr = TrackResult(title="Test Track", artist="Artist", codec_tier=2, extension=".m4a")
        p1 = _build_target_path(tr, ".m4a", directory=d, filename_prefix="01 - ")
        assert p1.name.startswith("01 - "), f"Prefixo faltando: {p1.name}"
        p1.with_suffix(".mp4").write_bytes(b"existing")
        p2 = _build_target_path(tr, ".m4a", directory=d, filename_prefix="01 - ")
        assert p2.stem.endswith("(2)"), f"Colisão .mp4 não detectada: {p2.name}"

        existing_mp4 = next(DOWNLOAD_DIR.glob("*.mp4"), None)
        if existing_mp4:
            sample_m4a = d / "sample.m4a"
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-t", "20", "-i", str(existing_mp4), "-vn", "-c:a", "copy", "-f", "mp4", str(sample_m4a)],
                check=True,
            )
            t0 = time.monotonic()
            out = remux_atmos_for_tv_soundbar(sample_m4a, tr)
            dt = time.monotonic() - t0
            assert out.suffix == ".mp4" and out.exists()
            info_str = probe_audio_file(out)
            assert "Dolby Atmos" in info_str, f"Atmos perdido no remux: {info_str}"
            assert dt < 6.0, f"Remux muito lento: {dt:.1f}s"
            print(f"  OK: remux concluído em {dt:.2f}s | {info_str}")
        else:
            print("  OK: anti-sobrescrita e prefixo verificados")


def test_playlist_parsers_multiplatform():
    print("[3/7] Testando extração real de playlists (Spotify, Deezer, YT Music, Apple, Amazon, Texto/CSV/M3U)...")

    # 3a. Texto colado com variações específicas
    txt_pl = parse_playlist_input(
        "1. Wham! - Last Christmas (Pudding Mix)\n"
        "2. Don Toliver - Lose My Mind (Official Music Video)\n"
        "3. Pink Floyd - Time (Live at Wembley)\n"
        "4. Coldplay - Fix You (Acoustic)\n",
        custom_name="Teste Variacoes",
    )
    assert len(txt_pl.tracks) == 4
    assert txt_pl.tracks[0].variation == "Pudding Mix", f"Esperado Pudding Mix, obtido {txt_pl.tracks[0].variation!r}"
    assert txt_pl.tracks[1].variation == "", f"Official Music Video deveria ser limpo: {txt_pl.tracks[1].title!r}"
    assert "Official" not in txt_pl.tracks[1].title
    assert "Live" in txt_pl.tracks[2].variation
    assert "Acoustic" in txt_pl.tracks[3].variation
    print(f"  OK Texto: {len(txt_pl.tracks)} faixas com variações detectadas corretamente")

    # 3b. CSV Exportify
    csv_pl = parse_playlist_input(
        "Track Name,Artist Name(s),Album Name,Duration (ms),ISRC\n"
        "Last Christmas (Pudding Mix),Wham!,Make It Big,404000,GBBBN8400001\n"
        "Blinding Lights,The Weeknd,After Hours,200040,USUG11904206\n",
        custom_name="Exportify Test",
    )
    assert len(csv_pl.tracks) == 2 and csv_pl.tracks[0].variation == "Pudding Mix" and csv_pl.tracks[0].duration_sec == 404
    print("  OK CSV Exportify: 2 faixas")

    # 3c. Spotify público real
    sp = parse_playlist_input("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
    assert sp.source_platform == "Spotify" and len(sp.tracks) >= 10
    print(f"  OK Spotify: '{sp.name}' ({len(sp.tracks)} faixas) · #1: {sp.tracks[0].display_name}")

    # 3d. Deezer público real (pegar 1 playlist do chart)
    import requests
    dz_id = requests.get("https://api.deezer.com/chart/0/playlists?limit=1", timeout=8).json()["data"][0]["id"]
    dz = parse_playlist_input(f"https://www.deezer.com/playlist/{dz_id}")
    assert dz.source_platform == "Deezer" and len(dz.tracks) >= 10
    print(f"  OK Deezer: '{dz.name}' ({len(dz.tracks)} faixas) · #1: {dz.tracks[0].display_name} [{dz.tracks[0].variation or 'Original'}]")

    # 3e. YouTube Music público real
    yt = parse_playlist_input("https://music.youtube.com/playlist?list=RDCLAK5uy_kmPRjHDECIcuVwnKsx2Ng7fyNgFKWNJFs")
    assert yt.source_platform == "YouTube Music" and len(yt.tracks) >= 5
    print(f"  OK YouTube Music: '{yt.name}' ({len(yt.tracks)} faixas) · #1: {yt.tracks[0].display_name}")

    # 3f. Apple Music público real
    ap = parse_playlist_input("https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb")
    assert ap.source_platform == "Apple Music" and len(ap.tracks) >= 10
    print(f"  OK Apple Music: '{ap.name}' ({len(ap.tracks)} faixas) · #1: {ap.tracks[0].display_name} ({ap.tracks[0].album})")

    # 3g. Amazon Music público real
    am = parse_playlist_input("https://music.amazon.com/playlists/B01M11SBC8")
    assert am.source_platform == "Amazon Music" and len(am.tracks) >= 10
    print(f"  OK Amazon Music: '{am.name}' ({len(am.tracks)} faixas) · #1: {am.tracks[0].display_name}")


def test_variation_aware_selection():
    print("[4/7] Testando seleção precisa de variações (Pudding Mix vs Single Version vs Live)...")
    c_single = TrackResult(
        title="Last Christmas (Single Version)", artist="Wham!",
        filename="01 Last Christmas (Single Version).m4a", extension=".m4a",
        remote_path=r"Music\Wham! [Dolby Atmos]\01 Last Christmas (Single Version).m4a",
        size_bytes=27 * 1024**2, duration_sec=267, speed_bps=2_200_000, has_free_slot=True,
        provider="soulseek", source="Soulseek",
    )
    c_pudding = TrackResult(
        title="Last Christmas (Pudding Mix)", artist="Wham!",
        filename="02 Last Christmas (Pudding Mix).m4a", extension=".m4a",
        remote_path=r"Music\Wham! [Dolby Atmos]\02 Last Christmas (Pudding Mix).m4a",
        size_bytes=39 * 1024**2, duration_sec=404, speed_bps=2_200_000, has_free_slot=True,
        provider="soulseek", source="Soulseek",
    )
    c_live = TrackResult(
        title="Last Christmas (Live at Wembley Arena)", artist="Wham!",
        filename="03 Last Christmas (Live at Wembley Arena).m4a", extension=".m4a",
        remote_path=r"Music\Wham! [Dolby Atmos]\03 Last Christmas (Live at Wembley Arena).m4a",
        size_bytes=34 * 1024**2, duration_sec=320, speed_bps=2_200_000, has_free_slot=True,
        provider="soulseek", source="Soulseek",
    )

    # Caso 1: Playlist pede especificamente Pudding Mix
    t_req_pudding = PlaylistTrack(index=1, title="Last Christmas (Pudding Mix)", artist="Wham!", variation="Pudding Mix", duration_sec=404)
    sel1 = select_ranked_candidates_for_playlist_track(t_req_pudding, [c_single, c_live, c_pudding])
    assert sel1[0][0].title == "Last Christmas (Pudding Mix)" and sel1[0][1] is False

    # Caso 2: Playlist pede versão padrão (NÃO deve escolher Pudding Mix nem Live!)
    t_req_std = PlaylistTrack(index=2, title="Last Christmas", artist="Wham!", variation="", duration_sec=267)
    sel2 = select_ranked_candidates_for_playlist_track(t_req_std, [c_pudding, c_live, c_single])
    assert sel2[0][0].title == "Last Christmas (Single Version)" and sel2[0][1] is False

    # Caso 3: Playlist pede Acoustic, mas só existe versão de estúdio -> sinaliza variation_mismatch=True!
    t_req_ac = PlaylistTrack(index=3, title="Last Christmas (Acoustic)", artist="Wham!", variation="Acoustic", duration_sec=260)
    sel3 = select_ranked_candidates_for_playlist_track(t_req_ac, [c_single])
    assert len(sel3) == 1 and sel3[0][1] is True, f"Deveria sinalizar mismatch: {sel3}"
    print("  OK: seleção de variações e detecção de divergência 100% precisas")


def test_samsung_tv_controller_silent():
    """Testes 100% offline/mockados do controlador Samsung TV (ZERO pacotes enviados na rede)."""
    print("[5b/8] Testando controlador TV Samsung (WoL + Pareamento + Picture Off + Standby seguro — 100% mockado)...")
    import json
    from unittest.mock import MagicMock, patch

    # 1. Magic Packet Wake-on-LAN (102 bytes: 6x FF + 16x MAC)
    pkt = build_wol_packet("aa:bb:cc:dd:ee:ff")
    assert len(pkt) == 102
    assert pkt[:6] == b"\xff" * 6
    assert pkt[6:12] == bytes.fromhex("aabbccddeeff")
    assert pkt[6:] == bytes.fromhex("aabbccddeeff") * 16
    try:
        build_wol_packet("invalid-mac")
        raise AssertionError("MAC inválido deveria levantar ValueError")
    except ValueError:
        pass

    # 2. Persistência TVConfig com permissão 0600 em diretório temporário
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "tv_samsung.json"
        ctrl = SamsungTVController(config_path=cfg_path)
        ctrl.update_endpoint(ip="192.0.2.10", mac="aa:bb:cc:dd:ee:ff", name="TestTV")
        assert cfg_path.exists()
        mode = cfg_path.stat().st_mode & 0o777
        assert mode == 0o600, f"Permissão esperada 0600, obtida {oct(mode)}"
        assert ctrl.is_paired is False
        assert ctrl.config.auto_wake is True and ctrl.config.auto_screen_off is True

        # 3. Pareamento quando TV está em standby deve avisar sem abrir WebSocket
        with patch.object(ctrl, "get_power_state", return_value="standby"):
            ok, msg = ctrl.pair()
            assert not ok and "Ligue a TV" in msg

        # 4. Pareamento bem-sucedido quando TV está ligada -> salva token permanentemente
        fake_ws = MagicMock()
        fake_ws.__enter__.return_value = fake_ws
        fake_ws.recv.return_value = json.dumps({"event": "ms.channel.connect", "data": {"token": "98765432"}})
        with patch.object(ctrl, "get_power_state", return_value="on"), patch("core.tv_control.ws_connect", return_value=fake_ws):
            ok, msg = ctrl.pair(timeout=5.0)
            assert ok, msg
            assert ctrl.is_paired and ctrl.config.token == "98765432"
        reloaded = TVConfig.load(cfg_path)
        assert reloaded.token == "98765432"

        # 5. send_key ignora evento intermediário e envia KEY_PICTURE_OFF com sucesso
        fake_ws2 = MagicMock()
        fake_ws2.__enter__.return_value = fake_ws2
        fake_ws2.recv.side_effect = [
            json.dumps({"event": "ed.installedApp.get", "data": {}}),
            json.dumps({"event": "ms.channel.connect", "data": {"token": "98765432"}}),
        ]
        with patch.object(ctrl, "get_power_state", return_value="on"), patch("core.tv_control.ws_connect", return_value=fake_ws2), patch("core.tv_control.time.sleep"):
            ok, msg = ctrl.turn_screen_off()
            assert ok, msg
            sent_payload = json.loads(fake_ws2.send.call_args[0][0])
            assert sent_payload["params"]["DataOfCmd"] == "KEY_PICTURE_OFF"

        # 6. standby_tv NUNCA envia KEY_POWER se TV já está em standby, mas ENVIA se estiver em 'pictureoff'!
        with patch.object(ctrl, "get_power_state", return_value="standby"), patch.object(ctrl, "send_key") as mock_send:
            ok, msg = ctrl.standby_tv()
            assert ok and "já está em standby" in msg
            mock_send.assert_not_called()

        with patch.object(ctrl, "get_power_state", return_value="pictureoff"), patch.object(ctrl, "send_key", return_value=(True, "ok")) as mock_send:
            ok, msg = ctrl.standby_tv()
            assert ok and "standby" in msg
            mock_send.assert_called_once_with("KEY_POWER")

        # 6b. turn_screen_off NUNCA faz duplo-toggle se PowerState já é 'pictureoff' (não acende a tela de volta!)
        with patch.object(ctrl, "get_power_state", return_value="pictureoff"), patch.object(ctrl, "send_key") as mock_send:
            ok, msg = ctrl.turn_screen_off()
            assert ok and "já está apagada" in msg
            mock_send.assert_not_called()

        # 7. wake_and_wait envia WoL e aguarda transição standby -> on + serviço DLNA /dmr pronto
        states = iter(["standby", "standby", "on"])
        dlna_states = iter([False, True])
        with (
            patch.object(ctrl, "get_power_state", side_effect=lambda timeout=2.5: next(states, "on")),
            patch.object(ctrl, "is_dlna_ready", side_effect=lambda port=9197, timeout=1.5: next(dlna_states, True)),
            patch("core.tv_control.send_wol") as mock_wol,
            patch("core.tv_control.time.sleep"),
        ):
            ok, msg = ctrl.wake_and_wait(timeout=10.0)
            assert ok, msg
            mock_wol.assert_called_once_with(ctrl.config.mac, ctrl.config.ip)

    print("  OK: Magic Packet WoL, TVConfig (0600), pareamento, KEY_PICTURE_OFF, standby seguro e wake_and_wait 100% válidos (0 pacotes reais)")


def test_audio_verification_truth_and_failover():
    """Valida classificação real (AAC 5.1 / AC-3 / DD+ 5.1 sem JOC / Atmos JOC), cache de peers e failover automático."""
    print("[5c/9] Testando verificação honesta de áudio (AAC 5.1 vs Atmos real, cache de peers e failover automático)...")
    from unittest.mock import patch
    import core.audio_verify as av
    from core.downloader import DownloadManager, DownloadStatus

    # 1. Classificação rigorosa via ffprobe (fonte única de verdade)
    va_joc = classify_probed_audio(
        {"codec_name": "eac3", "profile": "Dolby Digital Plus + Dolby Atmos", "channels": 6, "sample_rate": 48000, "bit_rate": 768000},
        ext=".mp4",
    )
    assert va_joc.is_atmos and va_joc.codec_tier == 2 and va_joc.quality_label == "DD+ JOC" and va_joc.filename_tag == " [Dolby Atmos]"

    # E-AC-3 5.1 SEM JOC (profile vazio) NÃO pode ser chamado de Dolby Atmos!
    va_ddp = classify_probed_audio(
        {"codec_name": "eac3", "profile": "", "channels": 6, "sample_rate": 48000, "bit_rate": 640000},
        ext=".mp4",
    )
    assert (not va_ddp.is_atmos) and va_ddp.is_surround and va_ddp.quality_label == "DD+ 5.1" and va_ddp.filename_tag == " [Surround 5.1]"

    # AAC 5.1 nunca é Atmos, nunca leva [Dolby Atmos] no nome e exibe tag explícita AAC 5.1!
    va_aac51 = classify_probed_audio(
        {"codec_name": "aac", "profile": "LC", "channels": 6, "sample_rate": 48000, "bit_rate": 384000},
        ext=".m4a",
    )
    assert (not va_aac51.is_atmos) and va_aac51.is_surround and va_aac51.quality_label == "AAC 5.1"
    assert va_aac51.filename_tag == " [Surround 5.1]"
    assert any(t.text == "AAC 5.1" for t in va_aac51.tags)

    # ALAC 24-bit deve aparecer como ULTRA HD Estéreo (não SD/genérico)
    va_alac = classify_probed_audio(
        {"codec_name": "alac", "profile": "", "channels": 2, "sample_rate": 96000, "bits": 24, "bit_rate": 2400000},
        ext=".m4a",
    )
    assert (not va_alac.is_atmos) and va_alac.codec_tier == 4 and va_alac.quality_label == "ULTRA HD" and "ALAC" in va_alac.format_label

    # 2. Busca honesta: pasta "Multichannel Music" SEM "atmos" NÃO vira Dolby Atmos; e Atmos não verificado recebe "A verificar"
    tr_mc = TrackResult(
        title="Lose My Mind", artist="Don Toliver",
        filename="Don Toliver - Lose My Mind.m4a", extension=".m4a",
        remote_path=r"@@eewpi\Music\Multichannel Music\Don Toliver - Lose My Mind.m4a",
        size_bytes=20 * 1024**2, duration_sec=209, provider="soulseek", source="Soulseek", remote_user="peer_mc",
    )
    tr_aac_labeled = TrackResult(
        title="Lose My Mind", artist="Don Toliver",
        filename="Don Toliver - Lose My Mind [AAC 5.1].m4a", extension=".m4a",
        remote_path=r"Music\Dolby Atmos\Don Toliver - Lose My Mind [AAC 5.1].m4a",
        size_bytes=18 * 1024**2, duration_sec=209, provider="soulseek", source="Soulseek", remote_user="peer_aac",
    )
    tr_atmos_claim = TrackResult(
        title="Lose My Mind", artist="Don Toliver",
        filename="01 Lose My Mind.m4a", extension=".m4a",
        remote_path=r"Music\Don Toliver [Dolby Atmos]\01 Lose My Mind.m4a",
        size_bytes=21 * 1024**2, duration_sec=209, provider="soulseek", source="Soulseek", remote_user="peer_atmos",
    )
    with tempfile.TemporaryDirectory() as tmp:
        tmp_cache = Path(tmp) / "verified_tracks.json"
        with patch.object(av, "VERIFIED_CACHE_FILE", tmp_cache):
            reset_cache_memory()
            ranked = rank_results([tr_mc, tr_aac_labeled, tr_atmos_claim], query="Don Toliver Lose My Mind", drop_junk=True)
            assert ranked[0].remote_user == "peer_atmos" and ranked[0].codec_tier == 2
            assert any(t.text == "A verificar" for t in ranked[0].tags), f"Faltou selo 'A verificar': {ranked[0].tags}"
            assert all(r.codec_tier == 3 for r in ranked[1:]), f"Multichannel/AAC 5.1 deveriam ser Tier 3: {[(r.remote_user, r.codec_tier) for r in ranked]}"

            # 3. Cache persistente: após descobrir que peer_atmos mentiu (era AAC 5.1), re-ranquear rebaixa imediatamente e mostra Verificado!
            record_verified_track(
                "peer_atmos", tr_atmos_claim.remote_path, tr_atmos_claim.filename, tr_atmos_claim.size_bytes,
                {"codec_name": "aac", "profile": "LC", "channels": 6, "sample_rate": 48000, "bit_rate": 384000},
                cache_file=tmp_cache,
            )
            hit = lookup_verified_track("peer_atmos", tr_atmos_claim.remote_path, tr_atmos_claim.filename, tr_atmos_claim.size_bytes, cache_file=tmp_cache)
            assert hit and hit["codec_name"] == "aac"
            tr_recheck = TrackResult(
                title="Lose My Mind", artist="Don Toliver",
                filename="01 Lose My Mind.m4a", extension=".m4a",
                remote_path=tr_atmos_claim.remote_path,
                size_bytes=21 * 1024**2, duration_sec=209, provider="soulseek", source="Soulseek", remote_user="peer_atmos",
            )
            ranked2 = rank_results([tr_recheck], query="Don Toliver Lose My Mind", drop_junk=True)
            assert ranked2[0].codec_tier == 3 and ranked2[0].quality_label == "AAC 5.1"
            assert any(t.text == "Verificado" for t in ranked2[0].tags)

            # 4. Failover automático em DownloadManager: peer1 envia AAC 5.1 falso -> app troca para peer2 com Atmos JOC real!
            fake_stage = Path(tmp) / "fake_peer1.m4a"
            real_stage = Path(tmp) / "real_peer2.m4a"
            fake_stage.write_bytes(b"0" * 600_000)
            real_stage.write_bytes(b"1" * 600_000)

            p1 = TrackResult(title="Test Song", artist="Artist", filename="test.m4a", extension=".m4a", codec_tier=2, provider="soulseek", source="Soulseek", remote_user="liar_peer", remote_path=r"Atmos\test.m4a", size_bytes=20*1024**2)
            p2 = TrackResult(title="Test Song", artist="Artist", filename="test2.m4a", extension=".m4a", codec_tier=2, provider="soulseek", source="Soulseek", remote_user="good_peer", remote_path=r"Atmos\test2.m4a", size_bytes=22*1024**2)

            class _MockSlsk:
                def download(self, username, remote_path, on_update=None, cancel_event=None):
                    return (True, fake_stage if username == "liar_peer" else real_stage, "")

            def _mock_probe(path, timeout=8.0):
                if "fake_peer1" in str(path):
                    return {"codec_name": "aac", "profile": "LC", "channels": 6, "sample_rate": 48000, "bit_rate": 384000}
                return {"codec_name": "eac3", "profile": "Dolby Digital Plus + Dolby Atmos", "channels": 6, "sample_rate": 48000, "bit_rate": 768000}

            dest_dir = Path(tmp) / "out_lib"
            dm = DownloadManager(slsk_service=_MockSlsk(), dest_dir=dest_dir)
            verified_events = []
            done_evt = []
            with (
                patch("core.downloader.probe_audio_full", side_effect=_mock_probe),
                patch("core.downloader.remux_atmos_for_tv_soundbar", side_effect=lambda p, t, work_dir=None: p),
            ):
                job = dm.start_download(
                    p1,
                    status_cb=lambda j: done_evt.append(j.status) if j.status in (DownloadStatus.DONE, DownloadStatus.ERROR) else None,
                    fallback_tracks=[p2],
                    on_peer_verified=lambda c, v: verified_events.append((c.remote_user, v.is_atmos)),
                )
                for _ in range(50):
                    if done_evt:
                        break
                    time.sleep(0.05)

            assert job.status == DownloadStatus.DONE, f"Job falhou: {job.error_msg}"
            assert job.actual_track and job.actual_track.remote_user == "good_peer"
            assert job.saved_path and "[Dolby Atmos]" in job.saved_path.name
            assert ("liar_peer", False) in verified_events and ("good_peer", True) in verified_events
            reset_cache_memory()

    # 5. Inspeção rápida de prefixo MP4 nos arquivos reais da biblioteca (se existirem)
    for mp4 in DOWNLOAD_DIR.glob("*.mp4"):
        hdr = inspect_mp4_prefix(mp4)
        assert hdr.get("moov_complete") is True
        if "[Dolby Atmos]" in mp4.name:
            assert hdr.get("has_atmos_capable") is True, f"Esperava ec-3 em {mp4.name}: {hdr}"

    print("  OK: classificação real (AAC 5.1 / DD+ 5.1 / Atmos JOC / ALAC), cache de peers, prefixo MP4 e failover automático 100% válidos")


def test_dlna_bridge():
    print("[5/7] Testando ponte DLNA (Rygel + Biblioteca + Subpastas de Playlists + Renderers)...")
    ok, msg = check_rygel_status()
    assert ok, f"Rygel não está ativo: {msg}"
    items = scan_local_library(DOWNLOAD_DIR)
    renderers = discover_renderers()
    print(f"  OK: {msg} | {len(items)} faixas na biblioteca | {len(renderers)} reprodutores ({', '.join(r.name for r in renderers)})")


def test_live_search_and_e2e_playlist_import():
    print("[6/7] Testando busca com typo + importação E2E real de mini-playlist (Atmos + Relatório + Pasta)...")
    for typo, expected_artist, expected_word in [
        ("lsoe my mind", "don toliver", "lose"),
        ("bliding lights", "weeknd", "blinding"),
        ("wham last cristmas", "wham", "christmas"),
    ]:
        rq = resolve_music_query(typo)
        assert expected_artist in rq.canonical_artist.lower()
        assert expected_word in rq.canonical_title.lower()

    slsk = get_slsk_service()
    slsk.start()
    assert slsk.wait_until_ready(timeout=15), "Soulseek não conectou"

    # Importação E2E real em pasta temporária (não suja ~/Music/DolbyAtmos)
    # 3 faixas deliberadamente escolhidas para testar:
    #   1) Don Toliver - Lose My Mind (tem Atmos 3D ou Ultra HD)
    #   2) Wham! - Last Christmas (Pudding Mix) (variação específica!)
    #   3) Faixa inexistente proposital (para validar relatório de erro!)
    mini_pl = ParsedPlaylist(
        name="Mini Teste Atmos",
        source_platform="Lista de Músicas",
        tracks=[
            PlaylistTrack(index=1, title="Lose My Mind", artist="Don Toliver", album="F1 The Album", duration_sec=209),
            PlaylistTrack(index=2, title="Last Christmas (Pudding Mix)", artist="Wham!", album="Make It Big", variation="Pudding Mix", duration_sec=404),
            PlaylistTrack(index=3, title="Xyzqwk Faixa Inexistente 99999", artist="Artista Ficticio Zzz", duration_sec=180),
        ],
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        runner = PlaylistImportRunner(playlist=mini_pl, dest_root=tmp_root, slsk_service=slsk)
        report = runner.run()

        pl_folder = tmp_root / "Mini Teste Atmos"
        assert pl_folder.is_dir(), "Pasta da playlist não foi criada"
        assert (pl_folder / ".atmoshunter_playlist.json").exists(), "Relatório JSON não foi salvo"
        assert (pl_folder / "Mini Teste Atmos.m3u").exists(), "Arquivo .m3u não foi criado"

        reloaded = PlaylistImportReport.load(pl_folder)
        assert reloaded is not None and reloaded.total == 3
        assert reloaded.tracks[0].status in (STATUS_OPTIMAL_ATMOS, STATUS_DOWNGRADED_SURROUND, STATUS_DOWNGRADED_STEREO), f"Faixa 1 falhou: {reloaded.tracks[0]}"
        assert reloaded.tracks[1].status in (STATUS_OPTIMAL_ATMOS, STATUS_DOWNGRADED_SURROUND, STATUS_DOWNGRADED_STEREO), f"Faixa 2 falhou: {reloaded.tracks[1]}"
        assert reloaded.tracks[2].status == STATUS_FAILED, f"Faixa inexistente deveria falhar: {reloaded.tracks[2]}"
        assert len(reloaded.issues) >= 1, "Relatório deveria listar pelo menos a faixa inexistente"

        # Validar leitura pela ponte DLNA (scan_local_library + summarize_playlists)
        lib_items = scan_local_library(tmp_root)
        summaries = summarize_playlists(lib_items, tmp_root)
        assert len(lib_items) >= 2, f"Biblioteca deveria listar >=2 faixas: {len(lib_items)}"
        assert all(it.playlist_name == "Mini Teste Atmos" for it in lib_items)
        assert len(summaries) == 1 and summaries[0].name == "Mini Teste Atmos" and summaries[0].has_report

        for t in reloaded.tracks:
            print(f"    #{t.index:02d} {t.requested_artist} - {t.requested_title:<32} -> {t.status_label:<22} | {t.verified_audio or t.issue_message[:60]}")
        print(f"  OK E2E Playlist: {reloaded.count_downloaded}/3 baixadas ({reloaded.count_atmos} Atmos 3D, {reloaded.count_failed} erro esperado), .m3u + JSON + scanner validados!")


def test_gui_stress_and_screenshots():
    print("[7/7] Testando GUI 3 abas (Buscar | Importar Playlist | Biblioteca) + Diálogo de Relatório...")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from ui.main_window import TAG_STYLES, MainWindow, T
    from ui.playlist_view import PL_COLUMNS, PlaylistReportDialog

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.show()
    app.processEvents()

    # 1. Estresse de busca e cancelamento rápido + renderizar tela Buscar com todos os tipos de tags Amazon Music
    win.search_input.setText("last christmas")
    win._on_search()
    time.sleep(0.3)
    app.processEvents()
    win._cancel_search()
    app.processEvents()

    search_samples = [
        TrackResult(
            title="Last Christmas (Single Version)", artist="Wham!", album="Last Christmas",
            filename="01 Last Christmas.m4a", extension=".m4a",
            remote_path=r"Music\Wham! [Dolby Atmos]\01 Last Christmas.m4a",
            size_bytes=27 * 1024**2, duration_sec=267, speed_bps=2_400_000, has_free_slot=True,
            provider="soulseek", source="Soulseek", remote_user="peer1",
        ),
        TrackResult(
            title="Last Christmas (Blu-ray Master)", artist="Wham!", album="Make It Big",
            filename="01 Last Christmas.mka", extension=".mka",
            remote_path=r"Music\Wham! [Dolby Atmos TrueHD]\01 Last Christmas.mka",
            size_bytes=260 * 1024**2, duration_sec=267, speed_bps=3_100_000, has_free_slot=True,
            provider="soulseek", source="Soulseek", remote_user="peer2",
        ),
        TrackResult(
            title="Last Christmas (Surround Mix)", artist="Wham!", album="Make It Big",
            filename="01 Last Christmas 5.1.flac", extension=".flac",
            remote_path=r"Music\Wham! [Surround 5.1]\01 Last Christmas 5.1.flac",
            size_bytes=126 * 1024**2, duration_sec=267, bit_depth=24, speed_bps=4_000_000, has_free_slot=True,
            provider="soulseek", source="Soulseek", remote_user="peer3",
        ),
        TrackResult(
            title="Last Christmas", artist="Wham!", album="Make It Big",
            filename="Wham! - Last Christmas.flac", extension=".flac",
            size_bytes=45 * 1024**2, duration_sec=267, bit_depth=24, sample_rate=96000,
            provider="http", source="Hi-Res Direct", download_url="https://tracks.monochrome.st/track/1",
        ),
    ]
    win._model.clear(query="last christmas")
    win._model.add_batch(search_samples)
    win._sync_action_buttons()
    win.stack.setCurrentIndex(1)
    app.processEvents()

    shot_dir = Path(tempfile.gettempdir()) / "atmoshunter_screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    win.grab().save(str(shot_dir / "atmoshunter_v25_search.png"))

    # 2. Aba Importar Playlist: simular playlist analisada com status misto (Atmos, Ultra HD, Erro)
    win._switch_page(1)
    sample_pl = ParsedPlaylist(
        name="Hits Dolby Atmos",
        source_platform="Spotify",
        source_url="https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
        tracks=[
            PlaylistTrack(index=1, title="Lose My Mind", artist="Don Toliver, Doja Cat", album="F1 The Album", duration_sec=209),
            PlaylistTrack(index=2, title="Last Christmas (Pudding Mix)", artist="Wham!", album="Make It Big", variation="Pudding Mix", duration_sec=404),
            PlaylistTrack(index=3, title="Time (Live at Wembley)", artist="Pink Floyd", album="Dark Side Live", variation="Live at Wembley", duration_sec=390),
            PlaylistTrack(index=4, title="Faixa Rara Sem Atmos", artist="Banda Indie", album="Demo EP", duration_sec=215),
        ],
    )
    win.playlist_tab._on_parse_done(sample_pl)
    app.processEvents()

    # Testar toggle de remover/incluir faixa antes do download
    win.playlist_tab._on_toggle_track(3)
    assert "3 de 4" in win.playlist_tab.lbl_pl_stats.text(), f"Contador não atualizou: {win.playlist_tab.lbl_pl_stats.text()}"
    win.playlist_tab._on_toggle_track(3)
    assert "4 de 4" in win.playlist_tab.lbl_pl_stats.text()

    # Simular relatório concluído na tabela
    from core.playlist_importer import TrackImportReport
    from core.ranker import AudioTag
    rep = PlaylistImportReport(
        playlist_name="Hits Dolby Atmos",
        source_platform="Spotify",
        source_url=sample_pl.source_url,
        folder_path=str(DOWNLOAD_DIR / "Hits Dolby Atmos"),
        tracks=[
            TrackImportReport(
                index=1, requested_artist="Don Toliver, Doja Cat", requested_title="Lose My Mind",
                status=STATUS_OPTIMAL_ATMOS, status_label="Dolby Atmos (7.1.4)",
                verified_audio="Dolby Atmos (E-AC-3 JOC) · 5.1 · 48.0 kHz · 768 kbps",
                saved_filename="01 - Don Toliver - Lose My Mind [Dolby Atmos].mp4",
                tags=[AudioTag("Dolby Atmos", "atmos"), AudioTag("DD+ JOC", "hd"), AudioTag("7.1.4", "channels"), AudioTag("MP4 · 768k", "format")],
            ),
            TrackImportReport(
                index=2, requested_artist="Wham!", requested_title="Last Christmas (Pudding Mix)", requested_variation="Pudding Mix",
                status=STATUS_OPTIMAL_ATMOS, status_label="Dolby Atmos (7.1.4)",
                verified_audio="Dolby Atmos (E-AC-3 JOC) · 5.1 · 48.0 kHz · 768 kbps",
                saved_filename="02 - Wham! - Last Christmas (Pudding Mix) [Dolby Atmos].mp4",
                tags=[AudioTag("Dolby Atmos", "atmos"), AudioTag("DD+ JOC", "hd"), AudioTag("7.1.4", "channels"), AudioTag("MP4 · 768k", "format")],
            ),
            TrackImportReport(
                index=3, requested_artist="Pink Floyd", requested_title="Time (Live at Wembley)", requested_variation="Live at Wembley",
                status=STATUS_DOWNGRADED_STEREO, status_label="ULTRA HD (2.0)",
                verified_audio="FLAC 24-bit · 96.0 kHz",
                variation_mismatch=True,
                issue_message="Dolby Atmos/Surround indisponível; baixada em ULTRA HD Estéreo 2.0. Variação pedida ('Live at Wembley') não confirmada.",
                tags=[AudioTag("ESTÉREO", "stereo"), AudioTag("ULTRA HD", "lossless"), AudioTag("2.0", "channels"), AudioTag("FLAC · 24-bit", "format")],
            ),
            TrackImportReport(
                index=4, requested_artist="Banda Indie", requested_title="Faixa Rara Sem Atmos",
                status=STATUS_FAILED, status_label="Não encontrada",
                issue_message="Nenhuma fonte retornou arquivo válido (Atmos, Surround ou Lossless) para esta faixa.",
            ),
        ],
    )
    for i, tr in enumerate(rep.tracks):
        win.playlist_tab._on_track_update(i, tr)
    # Atualizar estado final sem abrir modal bloqueante durante teste
    win.playlist_tab._report = rep
    win.playlist_tab.btn_report.setText(f"Ver Relatório ({len(rep.issues)} alertas)")
    win.playlist_tab.btn_report.show()
    win.playlist_tab.btn_open_lib.show()
    win.playlist_tab._sync_row_buttons()
    app.processEvents()

    # Validar células da tabela de playlist
    for r in range(win.playlist_tab._model.rowCount()):
        for c in range(len(PL_COLUMNS)):
            idx = win.playlist_tab._model.index(r, c)
            for role in (Qt.DisplayRole, Qt.ForegroundRole, Qt.BackgroundRole, Qt.FontRole, Qt.ToolTipRole):
                win.playlist_tab._model.data(idx, role)

    win.grab().save(str(shot_dir / "atmoshunter_v25_playlist.png"))

    # Renderizar também o diálogo de Relatório (incluindo caracteres especiais < & > para validar html.escape)
    rep.tracks[3].requested_title = "Faixa <Especial> & Rara"
    dlg = PlaylistReportDialog(report=rep, tokens=T, tag_styles=TAG_STYLES, allow_retry=True, parent=win)
    html_out = dlg._build_html()
    assert "&lt;Especial&gt; &amp; Rara" in html_out, "html.escape não aplicado no relatório!"
    dlg.show()
    app.processEvents()
    dlg.grab().save(str(shot_dir / "atmoshunter_v24_report.png"))
    dlg.close()

    # 3. Aba Minha Biblioteca com 12 playlists simuladas para validar rolagem horizontal sem quebrar largura da janela
    win._switch_page(2)
    items = scan_local_library(DOWNLOAD_DIR)
    from core.dlna_bridge import PlaylistSummary
    win._on_library_loaded(items)
    win._playlist_summaries = [
        PlaylistSummary(name=f"Playlist Muito Longa Exemplo Numero {i:02d}", folder=DOWNLOAD_DIR / f"pl_{i}", track_count=10 + i, atmos_count=8, has_report=True, issue_count=i % 3)
        for i in range(1, 13)
    ]
    win._rebuild_library_scope_pills()
    app.processEvents()
    assert win.width() <= 1300, f"Barra de playlists estourou a largura da janela: {win.width()}px"
    win.grab().save(str(shot_dir / "atmoshunter_v27_library_all.png"))

    # Validar também a visualização de uma playlist específica (com barra contextual + TV Samsung ▾)
    win._set_library_scope("Playlist Muito Longa Exemplo Numero 01")
    app.processEvents()
    assert win.playlist_actions_bar.isVisible(), "Barra contextual da playlist deveria estar visível"
    assert win.width() <= 1300, f"Modo playlist estourou a largura da janela: {win.width()}px"
    win.grab().save(str(shot_dir / "atmoshunter_v27_library_playlist.png"))

    win.close()
    app.processEvents()
    print(f"  OK: GUI 3 abas + relatório renderizados e fechados sem erros! Prints em {shot_dir}")


if __name__ == "__main__":
    test_ranker_and_filters()
    test_anti_collision_and_fast_remux()
    test_playlist_parsers_multiplatform()
    test_variation_aware_selection()
    test_samsung_tv_controller_silent()
    test_audio_verification_truth_and_failover()
    test_dlna_bridge()
    test_live_search_and_e2e_playlist_import()
    test_gui_stress_and_screenshots()
    print("\n=== SUITE COMPLETA ATMOSHUNTER v2.8: 9/9 PASSOU SEM ERROS ===")
