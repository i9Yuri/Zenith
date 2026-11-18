# Zenith — AtmosHunter

Aplicação desktop em Python e PySide6 para busca, verificação real de codecs, gerenciamento de biblioteca e reprodução direta via DLNA/UPnP de faixas individuais em Dolby Atmos (E-AC-3 JOC e TrueHD), áudio multicanal (5.1/7.1) e estéreo sem perdas (FLAC/ALAC 24-bit).

Projetado para sistemas de home theater e soundbars 7.1.4 com passthrough HDMI eARC (como TVs Samsung Tizen conectadas à JBL Bar 1000MK2), eliminando transcodificação destrutiva e falsos positivos comuns em redes P2P.

---

## Principais Recursos

### Busca Híbrida e Ranqueamento Estrito
- **Resolução de consulta e correção ortográfica:** identifica artista e título canônicos via Google Suggest, Deezer e iTunes, com matching fuzzy tolerante a erros de digitação.
- **Múltiplas fontes paralelas:** integra rede Soulseek (`aioslsk` em thread `asyncio` dedicada, sem UPnP e com auto-reconexão), catálogo Hi-Res Direct (`tracks.monochrome.st`) e Internet Archive.
- **Filtro rigoroso de faixa única:** descarta automaticamente filmes, episódios de séries, arquivos compactados e faixas fora da janela de 2 MB a 450 MB.
- **Ordenação hierárquica:** prioriza correspondência exata da música acima do formato de áudio, impedindo que faixas diferentes em Atmos fiquem acima da faixa buscada em Ultra HD.

### Verificação Real de Codec e Proteção Contra Falso Atmos
- **Fonte única de verdade (`core/audio_verify.py`):** classifica streams reais via `ffprobe`, distinguindo rigorosamente `Dolby Atmos (E-AC-3 JOC)` e `TrueHD Atmos` de `DD+ 5.1` (sem JOC), `DD 5.1 (AC-3)`, `AAC 5.1`, `ALAC` e `FLAC`.
- **Sondagem antecipada de cabeçalho MP4 (`moov/stsd`):** durante o download de arquivos `.m4a`/`.mp4`, inspeciona o cabeçalho nos primeiros megabytes; se o stream interno for `mp4a` (AAC) ou `ac-3`, aborta a transferência cedo e tenta automaticamente o próximo peer.
- **Cache persistente de verificação:** grava o codec real auditado em `~/.cache/atmoshunter/verified_tracks.json`. Em buscas futuras, resultados já auditados exibem a classificação real com o selo `Verificado`, enquanto resultados ainda não auditados da rede P2P exibem `A verificar`.
- **Empacotamento sem perda (`-c:a copy`):** encapsula fluxos `E-AC-3 JOC` em `.mp4` (`AVC_MP4_HP_HD_EAC3` com `faststart`) e `TrueHD` em `.mkv` dentro de diretório isolado de staging (`~/.cache/atmoshunter/staging/`), sem re-encodar o áudio e sem corromper bancos `inotify` de servidores DLNA locais.

### Importação de Playlists Multi-Plataforma
- Extração de playlists públicas do **Spotify**, **YouTube / YouTube Music** (`yt-dlp`), **Amazon Music**, **Deezer**, **Apple Music**, além de arquivos `.m3u`, `.csv` (Exportify) e listas de texto coladas.
- Preservação de variações específicas (`Live`, `Acoustic`, `Remix`, `Extended`, `Remaster`, entre outras) durante a seleção de candidatos.
- Organização em subpastas dedicadas (`~/Music/DolbyAtmos/<Playlist>/`) com numeração sequencial, arquivo `.m3u`, retomada inteligente (*Smart Resume*) e relatório estruturado (`.atmoshunter_playlist.json`) com retentativa para falhas e rebaixamentos.

### Reprodução Direta DLNA/UPnP e Automação de TV Samsung Tizen
- **Servidor HTTP Range embutido (`core/dlna_bridge.py`):** entrega mídia local diretamente ao reprodutor UPnP AVTransport com `Content-Length` real, suporte a HTTP 206 (`Range`) e `DIDL-Lite` limpo, evitando erros de decodificação em TVs Tizen.
- **Roteamento consciente de hardware:** direciona faixas Dolby Atmos e Surround 5.1/7.1 para a TV (HDMI eARC) e permite envio direto de FLAC/estéreo 2.0 para a soundbar.
- **Automação Samsung Tizen (`core/tv_control.py`):**
  - Despertar automático via **Wake-on-LAN** (Magic Packet UDP) com reforço via WebSocket e espera pela disponibilidade real do serviço DMR (`/dmr`).
  - Pareamento único via WebSocket seguro (`wss://<ip>:8002`) com token armazenado localmente em `~/.config/atmoshunter/tv_samsung.json` (permissão `0600`).
  - Desligamento automático apenas do painel (`KEY_PICTURE_OFF`) mantendo a reprodução Dolby Atmos ativa via HDMI eARC, com reconhecimento do estado `PowerState: pictureoff` para evitar duplo-toggle.
  - Ações integradas de acender tela, colocar em standby e encerrar reprodução + standby em um único clique.

---

## Estrutura do Repositório

```text
Zenith/
├── README.md
├── LICENSE
├── NOTICE
└── AtmosHunter/
    ├── main.py                      # Ponto de entrada da aplicação
    ├── iniciar.sh                   # Script de inicialização com criação automática de .venv
    ├── requirements.txt             # Dependências Python
    ├── AtmosHunter.desktop          # Modelo de lançador desktop (Freedesktop)
    ├── PROJECT_CONTEXT.md           # Documentação arquitetural interna
    ├── core/
    │   ├── audio_verify.py          # Classificador ffprobe, sondagem MP4 moov/stsd e cache de peers
    │   ├── query_resolver.py        # Correção ortográfica e resolução canônica de faixas
    │   ├── ranker.py                # Filtros anti-lixo, pontuação multicritério e ordenação hierárquica
    │   ├── slsk_service.py          # Cliente Soulseek assíncrono com auto-reconexão
    │   ├── search.py                # Orquestrador de busca multi-fonte
    │   ├── downloader.py            # Fila sequencial, failover Atmos e remux sem perda
    │   ├── playlist_importer.py     # Extratores multi-plataforma e importação em lote
    │   ├── dlna_bridge.py           # Catálogo local, saúde do Rygel e cast direto UPnP AVTransport
    │   └── tv_control.py            # Wake-on-LAN, WebSocket wss://:8002 e Picture Off (Samsung Tizen)
    ├── ui/
    │   ├── main_window.py           # Interface PySide6 (Buscar Faixas, Importar Playlist, Minha Biblioteca)
    │   └── playlist_view.py         # Fluxo de importação e relatório de playlists
    └── tests/
        └── test_atmoshunter.py      # Suíte de testes unitários, E2E e renderização offscreen
```

---

## Requisitos

- **Sistema Operacional:** Linux (testado em Nobara / Fedora com `systemd` e `firewalld`).
- **Python:** 3.12 ou superior.
- **Ferramentas de sistema obrigatórias:** `ffmpeg` e `ffprobe`.
- **Ferramentas opcionais:**
  - `rygel` — para disponibilizar a biblioteca via servidor DLNA permanente na rede local.
  - `yt-dlp` — para importação de playlists do YouTube e YouTube Music.
  - Navegador baseado em Chromium (`brave-browser`, `chromium` ou `google-chrome`) — para importação de playlists públicas do Amazon Music.

---

## Instalação e Execução

```bash
git clone https://github.com/i9Yuri/Zenith.git
cd Zenith/AtmosHunter
chmod +x iniciar.sh
./iniciar.sh
```

Na primeira execução, `iniciar.sh` cria o ambiente virtual `.venv` e instala automaticamente as dependências listadas em `AtmosHunter/requirements.txt`:

```text
PySide6>=6.7
aioslsk>=1.7
requests>=2.31
mutagen>=1.47
websockets>=13.0
```

Os arquivos baixados são salvos por padrão em `~/Music/DolbyAtmos/`. Configurações e credenciais locais nunca ficam no repositório e são mantidas em:

- `~/.config/atmoshunter/slsk.json` (`0600`) — credenciais geradas automaticamente para a sessão Soulseek.
- `~/.config/atmoshunter/tv_samsung.json` (`0600`) — endereço, MAC, preferências e token de pareamento da TV Samsung.
- `~/.cache/atmoshunter/` — cache `ffprobe` da biblioteca, cache de peers verificados e diretório de staging.

---

## Configuração de Rede Local (DLNA e TV Samsung)

Para uso com servidor DLNA local (`rygel`) e descoberta SSDP em distribuições com `firewalld`:

```bash
sudo firewall-cmd --add-service=ssdp --permanent
sudo firewall-cmd --add-protocol=igmp --permanent
sudo firewall-cmd --reload
```

Para habilitar o despertar automático e o modo tela apagada em TVs Samsung Tizen:

1. Na TV, habilite **Ligar com celular** (*Power on with Mobile*) nas configurações de rede.
2. Com a TV ligada, abra o AtmosHunter, vá em **Minha Biblioteca -> TV Samsung -> Parear controle da TV (1ª vez)** e aceite o aviso na tela da TV.
3. O token fica salvo em `~/.config/atmoshunter/tv_samsung.json`. A partir daí, clicar em **Tocar na TV** acorda a TV automaticamente, inicia a reprodução e apaga o painel.

---

## Testes

Execução dos testes unitários offline e validação de interface em modo offscreen (sem enviar comandos para dispositivos na rede):

```bash
cd AtmosHunter
QT_QPA_PLATFORM=offscreen ATMOSHUNTER_EPHEMERAL_SLSK=1 .venv/bin/python3 -W default -c "
import sys
sys.path.insert(0, '.')
import tests.test_atmoshunter as T
T.test_ranker_and_filters()
T.test_anti_collision_and_fast_remux()
T.test_variation_aware_selection()
T.test_samsung_tv_controller_silent()
T.test_audio_verification_truth_and_failover()
"
```

---

## Licença e Direitos Autorais

**Copyright (c) 2026 Yuri Barbosa (@i9Yuri). Todos os direitos reservados.**

Este repositório é **público apenas para visualização e uso pessoal não comercial** (*source-available*, proprietário). Não é distribuído sob licença open-source permissiva.

Sem autorização prévia e por escrito do autor, é estritamente proibido:

- Utilizar este projeto ou partes do código para fins comerciais, monetização ou prestação de serviços;
- Redistribuir, revender, republicar ou empacotar cópias originais ou modificadas;
- Remover avisos de autoria, alterar créditos ou apropriar-se do projeto sob outro nome;
- Criar e distribuir obras derivadas baseadas neste código-fonte.

Consulte os arquivos [LICENSE](LICENSE) e [NOTICE](NOTICE) para os termos completos.
