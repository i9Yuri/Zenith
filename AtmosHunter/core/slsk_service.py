# Copyright (c) 2026 Yuri Barbosa (@i9Yuri). All Rights Reserved.
# Proprietary source-available software (personal, non-commercial use only).
# Official repository: https://github.com/i9Yuri/Zenith
# Unauthorized commercial use, redistribution, or rebranding is prohibited. See LICENSE.
"""
AtmosHunter — Serviço Persistente Soulseek (aioslsk)
Roda em thread dedicada com seu próprio loop asyncio durante toda a sessão.
- Auto-login silencioso (credenciais geradas automaticamente na primeira execução)
- Busca de faixas individuais em Dolby Atmos / E-AC-3 JOC / Multicanal
- Download direto de arquivo único com progresso em tempo real
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import socket
import string
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from aioslsk.client import SoulSeekClient
from aioslsk.events import (
    KickedEvent,
    SearchResultEvent,
    ServerReconnectedEvent,
    SessionDestroyedEvent,
    TransferProgressEvent,
)
from aioslsk.network.connection import ConnectionState
from aioslsk.protocol.primitives import AttributeKey
from aioslsk.settings import (
    CredentialsSettings,
    ListeningSettings,
    NetworkSettings,
    Settings,
    SharesSettings,
    UpnpSettings,
)
from aioslsk.transfer.state import TransferState

from core.ranker import (
    ALLOWED_AUDIO_EXTS,
    CONDITIONAL_CONTAINER_EXTS,
    MAX_TRACK_BYTES,
    MIN_TRACK_BYTES,
    TrackResult,
    clean_track_metadata_from_path,
    format_size,
)

logger = logging.getLogger("AtmosHunter.slsk")

# Silenciar ruído excessivo de peers desconectados do aioslsk
for _noisy in ("aioslsk", "aioslsk.network", "aioslsk.peer", "aioslsk.transfer", "aioslsk.search"):
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)

CONFIG_FILE = Path.home() / ".config" / "atmoshunter" / "slsk.json"
STAGING_DIR = Path.home() / ".cache" / "atmoshunter" / "staging"


def _pick_free_port(exclude: int = 0) -> int:
    """Seleciona uma porta TCP livre dinamicamente para evitar conflito entre instâncias."""
    for _ in range(10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("0.0.0.0", 0))
            port = int(s.getsockname()[1])
            if port != exclude:
                return port
    return 60120


def _load_or_create_credentials(force_new: bool = False) -> tuple[str, str]:
    """Cria credenciais únicas e silenciosas na primeira execução (sem cadastro manual)."""
    ephemeral = os.environ.get("ATMOSHUNTER_EPHEMERAL_SLSK") == "1"
    if not force_new and not ephemeral:
        try:
            if CONFIG_FILE.exists():
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                u = data.get("username", "").strip()
                p = data.get("password", "").strip()
                if u and p:
                    return u, p
        except Exception:
            pass

    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=9))
    user = f"atmos_{rand}"
    pwd = f"ah_{rand}_{random.randint(1000, 9999)}"
    if not ephemeral:
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps({"username": user, "password": pwd}, indent=2), encoding="utf-8")
            os.chmod(CONFIG_FILE, 0o600)
        except Exception as e:
            logger.debug(f"Não foi possível salvar credenciais: {e}")
    return user, pwd


class SoulseekService:
    """
    Singleton/serviço de rede Soulseek rodando em loop asyncio em background.
    Seguro para chamadas concorrentes a partir de threads da UI.
    """

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._client: Optional[SoulSeekClient] = None
        self._ready_event = threading.Event()
        self._connected = False
        self._connecting = False
        self._stopping = False
        self._last_error = ""
        self._last_query_ts = 0.0
        self._status_listeners: list[Callable[[bool, str], None]] = []

        # Mapeamento de ticket -> callback de resultados da busca ativa
        self._search_callbacks: dict[int, Callable[[list[TrackResult]], None]] = {}
        # Mapeamento de (username, remote_path) -> callback de progresso do download
        self._transfer_callbacks: dict[tuple[str, str], Callable[[dict], None]] = {}
        self._active_transfers: dict[tuple[str, str], object] = {}

    def _is_session_alive(self) -> bool:
        """Verifica o estado REAL da sessão e do socket TCP do aioslsk (nunca confia apenas em flag stale)."""
        client = self._client
        if not self._connected or client is None:
            return False
        if getattr(client, "session", None) is None:
            return False
        net = getattr(client, "network", None)
        srv_conn = getattr(net, "server_connection", None) if net else None
        if srv_conn is not None:
            st = getattr(srv_conn, "connection_state", None) or getattr(srv_conn, "state", None)
            if st is not None and st != ConnectionState.CONNECTED:
                return False
        return True

    @property
    def is_connected(self) -> bool:
        return self._is_session_alive()

    def add_status_listener(self, cb: Callable[[bool, str], None]):
        self._status_listeners.append(cb)

    def _notify_status(self, connected: bool, message: str):
        self._connected = connected
        for cb in list(self._status_listeners):
            try:
                cb(connected, message)
            except Exception:
                pass

    def start(self):
        """Inicia o thread asyncio, limpa staging antigo e conecta em background."""
        self._stopping = False
        if self._thread and self._thread.is_alive():
            return
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        self._cleanup_old_staging()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="slsk-loop")
        self._thread.start()

    def _cleanup_old_staging(self):
        """Remove arquivos parciais antigos (>6h) deixados por downloads interrompidos (inclusive em subpastas)."""
        now = time.time()
        try:
            for item in STAGING_DIR.rglob("*"):
                if item.is_file() and (now - item.stat().st_mtime) > 6 * 3600:
                    item.unlink(missing_ok=True)
            # Remover subpastas vazias
            for sub in sorted(STAGING_DIR.rglob("*"), reverse=True):
                if sub.is_dir():
                    try:
                        sub.rmdir()
                    except OSError:
                        pass
        except Exception:
            pass

    def reconnect(self, force_new_account: bool = False):
        """Força reconexão limpa se o Soulseek tiver caído ou sido desconectado."""
        if self._stopping:
            return
        if not self._loop or not self._loop.is_running():
            self._ready_event.clear()
            self.start()
            return
        if not self._connecting:
            self._ready_event.clear()
            asyncio.run_coroutine_threadsafe(
                self._async_connect(force_new_account=force_new_account),
                self._loop,
            )

    def stop(self):
        """Encerra conexão de forma limpa ao fechar o app."""
        self._stopping = True
        if self._loop and self._loop.is_running() and self._client:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._async_stop(), self._loop)
                fut.result(timeout=4)
            except Exception:
                pass
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._client = None
        self._connected = False
        self._ready_event.clear()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.create_task(self._async_connect())
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _async_stop(self):
        if self._client:
            try:
                await self._client.stop()
            except Exception:
                pass
        self._connected = False

    async def _on_kicked(self, _event: KickedEvent):
        if self._stopping:
            return
        logger.warning("Soulseek: sessão derrubada pelo servidor (KickedEvent). Reconectando com nova sessão...")
        self._connected = False
        self._ready_event.clear()
        self._notify_status(False, "Reconectando rede Dolby Atmos...")
        if self._loop and self._loop.is_running():
            self._loop.create_task(self._delayed_reconnect(delay=1.5, force_new_account=True))

    async def _on_session_destroyed(self, _event: SessionDestroyedEvent):
        if self._stopping:
            return
        logger.warning("Soulseek: sessão encerrada (SessionDestroyedEvent). Agendando reconexão...")
        self._connected = False
        self._ready_event.clear()
        self._notify_status(False, "Reconectando rede Dolby Atmos...")
        if self._loop and self._loop.is_running():
            self._loop.create_task(self._delayed_reconnect(delay=2.0, force_new_account=False))

    async def _on_server_reconnected(self, _event: ServerReconnectedEvent):
        if self._stopping:
            return
        self._connected = True
        self._ready_event.set()
        self._notify_status(True, "Rede Dolby Atmos pronta")

    async def _delayed_reconnect(self, delay: float = 2.0, force_new_account: bool = False):
        await asyncio.sleep(delay)
        if not self._stopping and not self._is_session_alive() and not self._connecting:
            await self._async_connect(force_new_account=force_new_account)

    async def _async_connect(self, force_new_account: bool = False):
        if self._connecting or self._stopping:
            return
        self._connecting = True
        self._ready_event.clear()
        self._notify_status(False, "Conectando à rede Dolby Atmos...")
        try:
            # Encerrar cliente antigo antes de abrir novo socket
            old_client = self._client
            self._client = None
            self._connected = False
            if old_client is not None:
                try:
                    await asyncio.wait_for(old_client.stop(), timeout=4.0)
                except Exception:
                    pass

            user, pwd = _load_or_create_credentials(force_new=force_new_account)
            p1 = _pick_free_port()
            p2 = _pick_free_port(exclude=p1)
            settings = Settings(
                credentials=CredentialsSettings(username=user, password=pwd),
                shares=SharesSettings(download=str(STAGING_DIR)),
                network=NetworkSettings(
                    listening=ListeningSettings(port=p1, obfuscated_port=p2),
                    upnp=UpnpSettings(enabled=False),
                ),
            )
            client = SoulSeekClient(settings)
            client.events.register(SearchResultEvent, self._on_search_result)
            client.events.register(TransferProgressEvent, self._on_transfer_progress)
            client.events.register(KickedEvent, self._on_kicked)
            client.events.register(SessionDestroyedEvent, self._on_session_destroyed)
            client.events.register(ServerReconnectedEvent, self._on_server_reconnected)

            await client.start()
            await asyncio.wait_for(client.login(), timeout=15)
            self._client = client
            self._last_error = ""
            self._connected = True
            self._ready_event.set()
            self._notify_status(True, "Rede Dolby Atmos pronta")
            logger.info(f"Soulseek conectado como {user}")
        except Exception as e:
            self._last_error = str(e)
            self._connected = False
            self._ready_event.set()
            self._notify_status(False, "Rede Atmos indisponível (clique para reconectar)")
            logger.warning(f"Falha ao conectar Soulseek: {e}")
        finally:
            self._connecting = False

    def wait_until_ready(self, timeout: float = 15.0) -> bool:
        if not self._thread or not self._thread.is_alive():
            self.start()
        elif not self._is_session_alive() and not self._connecting:
            self.reconnect()
        self._ready_event.wait(timeout=timeout)
        return self._is_session_alive()

    # ── EVENTOS INTERNOS ─────────────────────────────────────

    async def _on_search_result(self, event: SearchResultEvent):
        ticket = event.query.ticket
        cb = self._search_callbacks.get(ticket)
        if not cb:
            return

        res = event.result
        tracks: list[TrackResult] = []

        for f in res.shared_items:
            fn = f.filename or ""
            ext = os.path.splitext(fn)[1].lower()
            if ext not in ALLOWED_AUDIO_EXTS and ext not in CONDITIONAL_CONTAINER_EXTS:
                continue
            if f.filesize < MIN_TRACK_BYTES or f.filesize > MAX_TRACK_BYTES:
                continue

            artist, title, album, basename = clean_track_metadata_from_path(fn)

            # Extrair atributos técnicos opcionais (bitrate, duração, sample rate, bit depth)
            bitrate = 0
            duration = 0
            sample_rate = 0
            bit_depth = 0
            for attr in getattr(f, "attributes", []) or []:
                akey = getattr(attr, "key", None)
                aval = int(getattr(attr, "value", 0) or 0)
                if akey == AttributeKey.BITRATE or akey == 0:
                    bitrate = aval
                elif akey == AttributeKey.DURATION or akey == 1:
                    duration = aval
                elif akey == AttributeKey.SAMPLE_RATE or akey == 4:
                    sample_rate = aval
                elif akey == AttributeKey.BIT_DEPTH or akey == 5:
                    bit_depth = aval

            tracks.append(
                TrackResult(
                    title=title,
                    artist=artist,
                    album=album,
                    filename=basename,
                    extension=ext,
                    size_bytes=int(f.filesize or 0),
                    size_label=format_size(int(f.filesize or 0)),
                    duration_sec=duration,
                    bitrate_kbps=bitrate,
                    sample_rate=sample_rate,
                    bit_depth=bit_depth,
                    speed_bps=int(getattr(res, "avg_speed", 0) or 0),
                    has_free_slot=bool(getattr(res, "has_free_slots", False)),
                    queue_length=int(getattr(res, "queue_size", 0) or 0),
                    source="Soulseek",
                    provider="soulseek",
                    remote_user=res.username,
                    remote_path=fn,
                )
            )

        if tracks:
            try:
                cb(tracks)
            except Exception as e:
                logger.debug(f"Callback de busca falhou: {e}")

    async def _on_transfer_progress(self, event: TransferProgressEvent):
        for transfer, _prev, curr in event.updates:
            key = (transfer.username, transfer.remote_path)
            cb = self._transfer_callbacks.get(key)
            if not cb:
                continue
            payload = {
                "state": curr.state,
                "bytes_transferred": int(curr.bytes_transfered or 0),
                "total_bytes": int(transfer.filesize or 0),
                "speed": float(curr.speed or 0.0),
                "local_path": transfer.local_path,
                "fail_reason": getattr(curr, "fail_reason", None),
                "abort_reason": getattr(curr, "abort_reason", None),
            }
            try:
                cb(payload)
            except Exception:
                pass

    # ── API PÚBLICA SÍNCRONA (THREAD-SAFE) ───────────────────

    def search(
        self,
        queries: list[str],
        on_batch: Callable[[list[TrackResult]], None],
        duration_sec: float = 8.0,
        stop_event: Optional[threading.Event] = None,
    ) -> int:
        """
        Dispara uma ou mais queries no Soulseek com auto-cura de sessão e proteção anti-cooldown.
        Retorna a quantidade total de faixas recebidas nesta chamada.
        """
        if not self.wait_until_ready(timeout=15.0) or not self._loop or not self._client:
            return 0

        received_count = [0]

        def _counting_cb(batch: list[TrackResult]):
            received_count[0] += len(batch)
            on_batch(batch)

        async def _do_search():
            tickets: list[int] = []
            try:
                for q in queries:
                    if stop_event and stop_event.is_set():
                        break

                    # Blindagem global anti-cooldown do servidor Soulseek (>= 1.6s entre queries)
                    since_last = time.monotonic() - self._last_query_ts
                    if since_last < 1.6:
                        await asyncio.sleep(1.6 - since_last)
                    if stop_event and stop_event.is_set():
                        break

                    # Garantir sessão viva; se caiu, reconectar transparente antes de enviar a query
                    if not self._is_session_alive():
                        await self._async_connect(force_new_account=False)
                        if not self._is_session_alive():
                            break

                    try:
                        req = await self._client.searches.search(q)
                    except Exception as e:
                        logger.warning(f"Soulseek search falhou para '{q}' ({e}); reconectando e tentando novamente...")
                        await self._async_connect(force_new_account=True)
                        if not self._is_session_alive():
                            break
                        req = await self._client.searches.search(q)

                    self._last_query_ts = time.monotonic()
                    tickets.append(req.ticket)
                    self._search_callbacks[req.ticket] = _counting_cb

                elapsed = 0.0
                step = 0.25
                while elapsed < duration_sec:
                    if stop_event and stop_event.is_set():
                        break
                    await asyncio.sleep(step)
                    elapsed += step
            finally:
                for t in tickets:
                    self._search_callbacks.pop(t, None)

        fut = asyncio.run_coroutine_threadsafe(_do_search(), self._loop)
        try:
            fut.result(timeout=duration_sec + 22.0)
        except Exception as e:
            logger.warning(f"Soulseek search encerrada com aviso: {e}")
        return received_count[0]

    def download(
        self,
        username: str,
        remote_path: str,
        on_update: Callable[[dict], None],
        cancel_event: threading.Event,
        timeout_sec: float = 300.0,
    ) -> tuple[bool, Optional[Path], str]:
        """
        Baixa um arquivo único via Soulseek para STAGING_DIR.
        Retorna (sucesso, local_path, erro_msg).
        """
        if not self.wait_until_ready(timeout=12.0) or not self._loop or not self._client:
            return False, None, "Rede Dolby Atmos offline no momento."

        done_evt = threading.Event()
        outcome: dict = {"ok": False, "path": None, "err": ""}
        key = (username, remote_path)

        def _handle_progress(payload: dict):
            on_update(payload)
            st = payload["state"]
            if st == TransferState.State.COMPLETE:
                lp = payload.get("local_path")
                outcome["ok"] = bool(lp and Path(lp).exists())
                outcome["path"] = Path(lp) if lp else None
                if not outcome["ok"]:
                    outcome["err"] = "Arquivo finalizado não foi encontrado no disco."
                done_evt.set()
            elif st in (TransferState.State.FAILED, TransferState.State.ABORTED):
                reason = payload.get("fail_reason") or payload.get("abort_reason") or "Transferência recusada pelo peer."
                outcome["ok"] = False
                outcome["err"] = str(reason)
                done_evt.set()

        self._transfer_callbacks[key] = _handle_progress

        async def _start_dl():
            tr = await self._client.transfers.download(username, remote_path)
            self._active_transfers[key] = tr
            return tr

        try:
            fut = asyncio.run_coroutine_threadsafe(_start_dl(), self._loop)
            transfer = fut.result(timeout=15.0)
        except Exception as e:
            self._transfer_callbacks.pop(key, None)
            return False, None, f"Não foi possível iniciar transferência: {e}"

        start_ts = time.time()
        last_bytes = 0
        last_activity_ts = start_ts

        try:
            while not done_evt.is_set():
                if cancel_event.is_set():
                    self._abort_transfer(transfer)
                    self._cleanup_partial_transfer(transfer)
                    return False, None, "Download cancelado."

                now = time.time()
                cur_bytes = int(getattr(transfer, "bytes_transfered", 0) or 0)
                if cur_bytes > last_bytes:
                    last_bytes = cur_bytes
                    last_activity_ts = now

                # Se ficar sem iniciar (0 bytes) por 35s, avisar que o peer não respondeu
                if last_bytes == 0 and (now - start_ts) > 35.0:
                    self._abort_transfer(transfer)
                    self._cleanup_partial_transfer(transfer)
                    return False, None, "Peer não respondeu em 35s. Tente outro resultado da lista."

                # Se travar no meio por 45s sem avançar bytes
                if last_bytes > 0 and (now - last_activity_ts) > 45.0:
                    self._abort_transfer(transfer)
                    self._cleanup_partial_transfer(transfer)
                    return False, None, "Conexão com o peer estagnou. Tente outro resultado da lista."

                if (now - start_ts) > timeout_sec:
                    self._abort_transfer(transfer)
                    self._cleanup_partial_transfer(transfer)
                    return False, None, "Tempo limite de download atingido."

                done_evt.wait(timeout=0.4)

            # Fallback caso transfer tenha completado sem o último evento
            if not outcome["ok"] and transfer.local_path and Path(transfer.local_path).exists():
                if transfer.filesize and Path(transfer.local_path).stat().st_size >= transfer.filesize:
                    outcome["ok"] = True
                    outcome["path"] = Path(transfer.local_path)

            return outcome["ok"], outcome["path"], outcome["err"]
        finally:
            self._transfer_callbacks.pop(key, None)
            self._active_transfers.pop(key, None)

    @staticmethod
    def _cleanup_partial_transfer(transfer):
        """Apaga arquivo parcial deixado em STAGING_DIR após cancelamento, early-abort ou timeout."""
        try:
            lp = getattr(transfer, "local_path", None)
            if lp:
                p = Path(lp)
                if p.exists() and STAGING_DIR in p.resolve().parents:
                    p.unlink(missing_ok=True)
        except Exception:
            pass

    def _abort_transfer(self, transfer):
        if self._loop and self._client and transfer:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._client.transfers.abort(transfer),
                    self._loop,
                )
            except Exception:
                pass


# Instância global compartilhada pelo app
_SERVICE: Optional[SoulseekService] = None


def get_slsk_service() -> SoulseekService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = SoulseekService()
    return _SERVICE
