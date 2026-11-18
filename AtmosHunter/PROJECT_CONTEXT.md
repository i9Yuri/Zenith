# AtmosHunter — Documento Mestre de Arquitetura e Contexto Integral

## 1. Visão Geral e Objetivo do Sistema

**AtmosHunter** é uma aplicação desktop leve em Python 3 + PySide6 que busca e baixa **músicas individuais (arquivo único)** em **Dolby Atmos (E-AC-3 JOC `.m4a` / TrueHD `.mka`)**, **Multicanal 5.1/7.1** e **FLAC Hi-Res 24-bit** diretamente para `~/Music/DolbyAtmos/`.

**Hardware de saída:** JBL Bar 1000 (7.1.4) recebendo bitstream puro via **Rygel** (`enable-transcoding=false`) e **Jellyfin**, transmitido do Android via BubbleUPnP / Symfonium.

**Diferencial central (v2.0):** Zero qBittorrent, zero arquivos ZIP de 70 GB, zero filmes/séries disfarçados e zero configuração manual. O app abre com duplo clique, conecta-se automaticamente em segundo plano à rede Soulseek + API Hi-Res Direct, filtra rigorosamente para **arquivos de música individuais (2 MB – 450 MB)**, baixa o arquivo único internamente e valida o codec real com `ffprobe`.

---

## 2. Mapa Estrutural do Repositório

```
AtmosHunter/
├── main.py                  # Entry point — inicia ui.main_window.run()
├── iniciar.sh               # Launcher zero-esforço usando .venv pré-instalado
├── AtmosHunter.desktop      # Atalho instalado em ~/.local/share/applications/
├── PROJECT_CONTEXT.md       # Especificação mestra de arquitetura
├── core/
│   ├── query_resolver.py    # Autocorreção de typos (Google Suggest) + artista/título canônico (Deezer/iTunes)
│   ├── audio_verify.py      # Fonte única de verdade (ffprobe + sondagem precoce MP4 moov/stsd + cache ~/.cache/atmoshunter/verified_tracks.json)
│   ├── ranker.py            # Matching fuzzy anti-typo, consulta ao cache verificado, selo 'A verificar'/'Verificado', sem falso Atmos em pastas Multichannel/AAC 5.1
│   ├── slsk_service.py      # Cliente Soulseek persistente (aioslsk), portas dinâmicas, sem UPnP, auto-reconexão
│   ├── search.py            # Orquestrador multi-fontes: Soulseek (Atmos + M4A/Multicanal + 5.1) + Hi-Res Direct + Internet Archive
│   ├── playlist_importer.py # Extratores multi-plataforma (Spotify, YT Music, Amazon Music, Deezer, Apple Music, M3U/CSV/Texto), matching de variações, failover e relatório JSON
│   ├── downloader.py        # Fila sequencial + sondagem precoce MP4 + failover automático de fontes Atmos + remux rápido (-c:a copy) em STAGING_DIR + nomeação 100% honesta via ffprobe
│   ├── dlna_bridge.py       # Saúde do Rygel, catálogo local recursivo com cache ffprobe, servidor HTTP Range embutido + cast limpo UPnP AVTransport
│   └── tv_control.py        # Automação Samsung Tizen (Wake-on-LAN, pareamento único wss://:8002, Picture Off e standby seguro)
├── tests/
│   └── test_atmoshunter.py  # Suíte automatizada 7/7 (unitários + remux + 5 plataformas de playlist + variações + E2E + DLNA + estresse GUI)
└── ui/
    ├── main_window.py       # GUI PySide6 com 3 abas (Buscar Faixas | Importar Playlist | Minha Biblioteca com filtro por playlist)
    └── playlist_view.py     # Aba Importar Playlist + PlaylistReportDialog (KPIs, alertas, inconsistências e retry de falhas)
```

---

## 3. Arquitetura de Telas, Estados e Fluxo

```
MainWindow
├── [Header] AtmosHunter + indicador de conexão Soulseek + botão "Pasta DolbyAtmos"
├── [SearchFrame] Campo único (Enter / Buscar) + barra fina de progresso
├── [Toolbar] Filtros rápidos (Todas | Dolby Atmos / Multicanal | FLAC Hi-Res) + contagem
├── [Stack]
│   ├── Página 0 (Empty State): descrição clara + sugestões clicáveis
│   └── Página 1 (QTableView): 7 colunas (# | Faixa | Formato | Tamanho | Disponibilidade | Score | Ação)
├── [DownloadPanel] Barra de progresso real (% + MB/s), status de fila/verificação ffprobe, Cancelar / Abrir pasta
└── [Footer] Legenda de tiers + caminho ~/Music/DolbyAtmos
```

**Fluxo de busca e download:**
1. Ao abrir o app, `SoulseekService` gera/carrega credenciais em `~/.config/atmoshunter/slsk.json` e conecta automaticamente em background.
2. Ao digitar uma música (ex: `Last Christmas`), `SearchEngine` dispara em paralelo:
   - **Hi-Res Direct (`tracks.monochrome.st`):** retorna faixas individuais em FLAC 24-bit em ~400 ms.
   - **Soulseek (`aioslsk`):** dispara `{query} atmos` e `{query} m4a`, recebendo faixas individuais `.m4a` (Dolby Atmos E-AC-3 JOC) e `.flac` multicanal em 2–8 s.
3. `rank_results()` descarta qualquer vídeo, série (`S01E01`), ZIP ou arquivo fora de 2 MB–450 MB, remove resultados sem relevância com a busca, deduplica peers e coloca o melhor Dolby Atmos disponível no topo (`#1`).
4. Um clique em **Baixar** transfere apenas aquele arquivo único diretamente para `~/Music/DolbyAtmos/Artista - Título [Dolby Atmos].mp4` e confirma o stream via `ffprobe`.

---

## 4. Configurações e Dependências

- **Runtime:** Python 3.12+ em Linux (`.venv` com `--system-site-packages`)
- **Bibliotecas Python:** `PySide6`, `aioslsk>=1.7`, `requests`, `mutagen`, `websockets`
- **Binários do sistema:** `ffmpeg` e `ffprobe` (remux sem perda e verificação pós-download)
- **Diretório padrão de destino:** `~/Music/DolbyAtmos/`
- **Credenciais automáticas Soulseek:** `~/.config/atmoshunter/slsk.json` (permissão `0600`)
- **Configuração TV Samsung:** `~/.config/atmoshunter/tv_samsung.json` (permissão `0600`)
- **Staging temporário:** `~/.cache/atmoshunter/staging/`

---

## 5. Instruções Críticas para IAs que Modificarem Este Código

### O que NUNCA fazer
- **Não reintroduzir trackers públicos de torrent (Bitsearch/1337x) como fonte primária** — eles retornam filmes 4K de 50 GB e episódios de séries em vez de faixas individuais.
- **Não permitir vazamento de fragmentos temporários (.frag, .part, .tmp) na raiz do repositório** — todo subprocesso (`ffmpeg`, `ffprobe`, `yt-dlp`, Chromium headless) deve operar estritamente isolado em `STAGING_DIR` (`~/.cache/atmoshunter/staging`), mantendo o workspace limpo.
- **Não usar hex CSS de 8 dígitos `#rrggbbaa` em QSS do Qt** — o Qt interpreta `#aarrggbb`, causando cores erradas; use sempre `rgba(r, g, b, alpha)`.
- **Não bloquear a thread principal do Qt** — o `SoulseekService` mantém seu próprio event loop `asyncio` em thread separada e comunica via sinais Qt (`Signal`).
- **Não exigir configuração manual ou login do usuário** — o app deve abrir com duplo clique e funcionar imediatamente.

### Comandos de execução e teste
```bash
# Abrir aplicação
./AtmosHunter/iniciar.sh

# Teste de busca e ranking (headless)
cd AtmosHunter
.venv/bin/python3 -c "from core.search import SearchEngine; from core.ranker import rank_results; e=SearchEngine(); r=rank_results(e.search('last christmas'), 'last christmas'); print(len(r), r[0].display_name, r[0].codec_label)"

# Teste de interface offscreen
QT_QPA_PLATFORM=offscreen .venv/bin/python3 -c "from PySide6.QtWidgets import QApplication; from ui.main_window import MainWindow; app=QApplication([]); w=MainWindow(); print('UI OK')"
```

---

## 6. Histórico de Versões e Decisões Críticas

| Versão | Decisão |
|--------|---------|
| v1.0 | Protótipo inicial baseado em indexadores torrent + qBittorrent Flatpak |
| v2.0 | Reconstrução completa orientada a **faixas individuais**: motor híbrido **Soulseek nativo (`aioslsk`)** + **Hi-Res Direct (`tracks.monochrome.st`)**, download 100% interno sem qBittorrent, validação pós-download com `ffprobe`, limite rígido de 2–450 MB e UI Hallmark/Impeccable |
| v2.1 | Tags identificadoras por faixa (`ATMOS 3D`, `LOSSLESS`/`ULTRA HD`/`DD+ JOC`, `7.1.4`/`5.1`/`2.0`, formato/bitrate), remux automático sem perda (`-c:a copy`) de faixas Atmos para `.mp4` (`AVC_MP4_HP_HD_EAC3` com capa 720p `yuv420p` + `faststart`) / `.mkv` para passthrough HDMI eARC na TV Samsung Tizen -> JBL Bar 1000MK2, e hardening do Rygel (`port=0`, `Restart=always`, `Linger=yes`) |
| v2.2 | Rodada de revisão/refatoração: correção do `RuntimeError` de `QThread` deletado via `shiboken6.isValid`, remux acelerado de 12.4s para ~1s (pré-escala 1x da capa), fila sequencial de downloads, anti-colisão `.m4a`/`.mp4`/`.mkv`, portas dinâmicas + UPnP desativado no Soulseek, fallback surround 5.1 na busca, nova aba **Minha Biblioteca** com cache `ffprobe` + envio direto UPnP para TV/JBL + status clicável do Rygel, e suíte `tests/test_atmoshunter.py` |
| v2.3 | Motor de busca inteligente com `core/query_resolver.py` (autocorreção de typos via Google Suggest + Deezer/iTunes), matching fuzzy por token (`SequenceMatcher >= 0.78`), detecção implícita de Dolby Atmos em pastas `Multichannel Music` + ALAC Ultra HD, anti-flood de bots Soulseek e fonte extra `InternetArchiveProvider` |
| v2.4 | Importação completa de playlists (`core/playlist_importer.py` + `ui/playlist_view.py`): extração real de Spotify, YouTube Music/YouTube, Amazon Music, Deezer, Apple Music, M3U/CSV e texto colado; detecção e matching de variações musicais (`Live`, `Acoustic`, `Remix`, `Pudding Mix`, `Extended`, `Remaster`); download em lote com failover de peers e classificação real via `ffprobe`; organização em subpasta dedicada + `.m3u` + `.atmoshunter_playlist.json`; acesso exclusivo por playlist na aba **Minha Biblioteca** e modal de relatório de inconsistências com retentativa |
| v2.5 | Vistoria completa e endurecimento: upgrade automático de peer quando o 1º candidato entrega AC-3 5.1/estéreo e há outro peer Atmos; retry sem duplicatas e sem apagar faixa válida se a retentativa falhar; retry de falhas direto pelo relatório na aba **Minha Biblioteca**; casamento exato por tamanho/título no Rygel (`_find_rygel_media_url`); extração do título real na Amazon Music (`headline`/`<h1>`); barra horizontal com rolagem (`QLayout.SetFixedSize`) suportando dezenas de playlists; `html.escape()` em diálogos HTML e código 100% livre de imports mortos |
| v2.6 | Tags redesenhadas no estilo flat frosted-glass do **Amazon Music** (ícone vetorial oficial **Dolby Double-D** + `Dolby` Medium + `Atmos` Light); motor de relevância hierárquica estrita (música exata sempre acima de música diferente + remoção do falso positivo `dd` em `Wedding Edition` + piso físico >=420 kbps para Atmos); auto-cura de sessão Soulseek (`KickedEvent`/`SessionDestroyedEvent` + anti-cooldown + botão clicável); remux 100% isolado em `~/.cache/atmoshunter/staging/` (nunca corrompe banco do Rygel); `DIDL-Lite` limpo sem `ns0:`/`ns3:` + servidor HTTP Range embutido + nomeação honesta `[Multicanal]` quando peer entrega AC-3 5.1; **Smart Resume** em playlists, filtro instantâneo + `Só Atmos` e botão `Parar TV/JBL` na **Minha Biblioteca** |
| v2.7 | Automação completa da TV Samsung (`core/tv_control.py`): Wake-on-LAN automático ao clicar em **Tocar na TV** mesmo em standby, pareamento único via WebSocket `wss://:8002` com token salvo (`0600`), modo automático **Apagar Tela (Picture Off)** com reconhecimento de `PowerState == "pictureoff"` e menu **TV Samsung ▾** na aba **Minha Biblioteca** |
| v2.8 | Verificação honesta e unificada (`core/audio_verify.py`): fonte única de verdade `classify_probed_audio()` entre Busca, Download, Playlists e Biblioteca (distingue `Dolby Atmos DD+ JOC` de `DD+ 5.1 sem Atmos`, `DD 5.1`, `AAC 5.1` e `ALAC Ultra HD`); remoção do falso positivo de pastas genéricas `Multichannel`/`5.1` na busca; selos `A verificar` vs. `Verificado` com cache persistente (`verified_tracks.json`); sondagem precoce do cabeçalho MP4 (`moov/stsd`) para abortar falsos Atmos nos primeiros % e failover automático entre até 4 peers Atmos também na busca de faixa única |
