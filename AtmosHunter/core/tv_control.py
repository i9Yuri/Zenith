# Copyright (c) 2026 Yuri Barbosa (@i9Yuri). All Rights Reserved.
# Proprietary source-available software (personal, non-commercial use only).
# Official repository: https://github.com/i9Yuri/Zenith
# Unauthorized commercial use, redistribution, or rebranding is prohibited. See LICENSE.
"""
AtmosHunter — Controlador Automático da TV Samsung Tizen (Top 1)
Permite:
  - Acordar a TV Samsung automaticamente via Wake-on-LAN (Magic Packet UDP no MAC cabeado/Wi-Fi)
    e aguardar o serviço DLNA (porta 9197) subir antes de iniciar a música Dolby Atmos
  - Pareamento único via WebSocket seguro (wss://<ip>:8002) com salvamento automático do token
    em ~/.config/atmoshunter/tv_samsung.json (permissão 0600)
  - Apagar apenas a tela da TV (KEY_PICTURE_OFF) mantendo o som Dolby Atmos 7.1.4 via HDMI eARC
  - Colocar a TV de volta em standby com segurança (só envia KEY_POWER se PowerState == "on")
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import socket
import ssl
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

import requests
from websockets.sync.client import connect as ws_connect

logger = logging.getLogger("AtmosHunter.tv")

TV_CONFIG_FILE = Path.home() / ".config" / "atmoshunter" / "tv_samsung.json"

DEFAULT_TV_IP = ""
DEFAULT_TV_MAC = ""
DEFAULT_TV_NAME = "Samsung TV"


@dataclass
class TVConfig:
    ip: str = DEFAULT_TV_IP
    mac: str = DEFAULT_TV_MAC
    name: str = DEFAULT_TV_NAME
    token: str = ""
    auto_wake: bool = True
    auto_screen_off: bool = True

    def save(self, path: Path = TV_CONFIG_FILE) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(path, 0o600)
        except Exception as e:
            logger.debug(f"Falha ao salvar config da TV: {e}")

    @classmethod
    def load(cls, path: Path = TV_CONFIG_FILE) -> "TVConfig":
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                valid = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
                return cls(**valid)
            except Exception as e:
                logger.debug(f"Falha ao ler config da TV: {e}")
        cfg = cls()
        cfg.save(path)
        return cfg


def build_wol_packet(mac: str) -> bytes:
    """Constrói um Magic Packet Wake-on-LAN padrão de 102 bytes (6x 0xFF + 16x MAC)."""
    clean = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(clean) != 12:
        raise ValueError(f"Endereço MAC inválido para Wake-on-LAN: {mac!r}")
    mac_bytes = bytes.fromhex(clean)
    return (b"\xff" * 6) + (mac_bytes * 16)


def send_wol(mac: str, target_ip: str = "") -> None:
    """Envia Magic Packet UDP em broadcast local + sub-rede + unicast para máxima confiabilidade."""
    packet = build_wol_packet(mac)
    destinations: list[tuple[str, int]] = [("255.255.255.255", 9), ("255.255.255.255", 7)]
    if target_ip and target_ip.count(".") == 3:
        parts = target_ip.split(".")
        subnet_bcast = f"{parts[0]}.{parts[1]}.{parts[2]}.255"
        destinations.append((subnet_bcast, 9))
        destinations.append((target_ip, 9))

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(3):
            for dest in destinations:
                try:
                    s.sendto(packet, dest)
                except OSError:
                    pass
            time.sleep(0.15)


class SamsungTVController:
    """Controlador da TV Samsung Tizen (QN50Q60C) via REST v2 + Wake-on-LAN + WebSocket wss://:8002."""

    def __init__(self, config_path: Path = TV_CONFIG_FILE):
        self._config_path = config_path
        self.config = TVConfig.load(config_path)

    @property
    def is_paired(self) -> bool:
        return bool(self.config.token.strip())

    def reload(self) -> None:
        self.config = TVConfig.load(self._config_path)

    def save(self) -> None:
        self.config.save(self._config_path)

    def update_endpoint(self, ip: str = "", mac: str = "", name: str = "") -> None:
        changed = False
        if ip and ip != self.config.ip:
            self.config.ip = ip
            changed = True
        if mac and mac.lower() != self.config.mac.lower():
            self.config.mac = mac.lower()
            changed = True
        if name and name != self.config.name:
            self.config.name = name
            changed = True
        if changed:
            self.save()

    @staticmethod
    def is_awake_state(state: str) -> bool:
        """Retorna True se a TV está acordada (tela acesa 'on' ou tela apagada 'pictureoff')."""
        return state in ("on", "pictureoff")

    def get_power_state(self, timeout: float = 2.5) -> str:
        """Consulta http://<ip>:8001/api/v2/ e retorna 'on', 'pictureoff', 'standby' ou 'offline'."""
        if not self.config.ip:
            # Descoberta automática inicial via SSDP caso ainda não haja IP salvo em ~/.config/atmoshunter/tv_samsung.json
            try:
                from core.dlna_bridge import discover_renderers

                tv_rnd = next((r for r in discover_renderers() if r.is_tv), None)
                if tv_rnd and tv_rnd.ip:
                    self.update_endpoint(ip=tv_rnd.ip, name=tv_rnd.name)
            except Exception:
                pass
        if not self.config.ip:
            return "offline"
        try:
            r = requests.get(f"http://{self.config.ip}:8001/api/v2/", timeout=timeout)
            if r.status_code != 200:
                return "offline"
            data = r.json()
            dev = data.get("device") or {}
            mac = str(dev.get("wifiMac") or "").strip()
            name = str(dev.get("name") or "").strip()
            if mac or name:
                self.update_endpoint(mac=mac, name=name)
            ps = str(dev.get("PowerState") or "").strip().lower()
            if ps in ("on", "standby", "pictureoff"):
                return ps
            return "on"
        except Exception:
            return "offline"

    def is_dlna_port_open(self, port: int = 9197, timeout: float = 0.8) -> bool:
        if not self.config.ip:
            return False
        try:
            with socket.create_connection((self.config.ip, port), timeout=timeout):
                return True
        except OSError:
            return False

    def is_dlna_ready(self, port: int = 9197, timeout: float = 1.5) -> bool:
        """Verifica não só se a porta 9197 abriu, mas se o serviço DMR UPnP da TV já responde HTTP 200."""
        if not self.config.ip or not self.is_dlna_port_open(port=port, timeout=min(0.8, timeout)):
            return False
        try:
            r = requests.get(f"http://{self.config.ip}:{port}/dmr", timeout=timeout)
            return r.status_code == 200 and "AVTransport" in r.text
        except Exception:
            return False

    def _build_ws_url(self, include_token: bool = True) -> str:
        app_b64 = base64.b64encode(b"AtmosHunter").decode("ascii")
        url = f"wss://{self.config.ip}:8002/api/v2/channels/samsung.remote.control?name={app_b64}"
        if include_token and self.config.token:
            url += f"&token={self.config.token}"
        return url

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def pair(self, timeout: float = 25.0) -> tuple[bool, str]:
        """
        Inicia pareamento único com a TV Samsung: exibe 'Permitir' na tela da TV
        e salva o token retornado para uso futuro sem pedir permissão novamente.
        """
        state = self.get_power_state()
        if state != "on":
            return False, "Ligue a TV Samsung primeiro para aceitar o aviso 'Permitir' na tela (só precisa 1 vez)."

        url = self._build_ws_url(include_token=bool(self.config.token))
        try:
            with ws_connect(url, ssl=self._ssl_context(), open_timeout=6.0, close_timeout=2.0) as ws:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    rem = max(0.5, deadline - time.monotonic())
                    raw = ws.recv(timeout=rem)
                    msg = json.loads(raw)
                    ev = str(msg.get("event") or "")
                    if ev == "ms.channel.connect":
                        tok = str((msg.get("data") or {}).get("token") or "").strip()
                        if tok:
                            self.config.token = tok
                            self.save()
                        if self.config.token:
                            return True, "TV Samsung pareada com sucesso! Token salvo permanentemente."
                        return True, "Conectado à TV Samsung."
                    if ev in ("ms.channel.unauthorized", "ms.channel.timeOut"):
                        return False, "Pareamento recusado ou expirou na tela da TV. Tente novamente e clique em 'Permitir'."
            return False, "Tempo esgotado aguardando confirmação na tela da TV."
        except Exception as e:
            return False, f"Erro ao parear com a TV Samsung: {e}"

    def send_key(self, key: str, cmd: str = "Click", timeout: float = 6.0) -> tuple[bool, str]:
        """Envia uma tecla de controle remoto Samsung via WebSocket (wss://:8002)."""
        if not self.config.ip:
            return False, "IP da TV Samsung não configurado."
        url = self._build_ws_url(include_token=True)
        try:
            with ws_connect(url, ssl=self._ssl_context(), open_timeout=timeout, close_timeout=2.0) as ws:
                deadline = time.monotonic() + timeout
                connected_msg = None
                while time.monotonic() < deadline:
                    rem = max(0.3, deadline - time.monotonic())
                    raw = ws.recv(timeout=rem)
                    msg = json.loads(raw)
                    ev = str(msg.get("event") or "")
                    if ev == "ms.channel.connect":
                        connected_msg = msg
                        break
                    if ev in ("ms.channel.unauthorized", "ms.channel.timeOut"):
                        return False, "Controle não autorizado — use 'TV Samsung → Parear controle (1ª vez)' com a TV ligada."

                if connected_msg is None:
                    return False, "Tempo esgotado aguardando conexão com a TV Samsung."

                new_tok = str((connected_msg.get("data") or {}).get("token") or "").strip()
                if new_tok and new_tok != self.config.token:
                    self.config.token = new_tok
                    self.save()

                payload = {
                    "method": "ms.remote.control",
                    "params": {
                        "Cmd": cmd,
                        "DataOfCmd": key,
                        "Option": "false",
                        "TypeOfRemote": "SendRemoteKey",
                    },
                }
                ws.send(json.dumps(payload))
                time.sleep(0.25)
                return True, f"Comando {key} enviado para a TV Samsung."
        except Exception as e:
            return False, f"Falha ao enviar comando para a TV: {e}"

    def wake_and_wait(
        self,
        status_cb: Optional[Callable[[str], None]] = None,
        timeout: float = 25.0,
    ) -> tuple[bool, str]:
        """
        Acorda a TV Samsung automaticamente (Wake-on-LAN + fallback KEY_POWER se pareada)
        e aguarda até PowerState estar acordada ('on'/'pictureoff') e o serviço DLNA DMR (/dmr) responder HTTP 200.
        """
        if self.is_awake_state(self.get_power_state()) and self.is_dlna_ready():
            return True, "TV Samsung já está ligada e pronta."

        if status_cb:
            status_cb("Acordando TV Samsung automaticamente (Wake-on-LAN)...")

        try:
            send_wol(self.config.mac, self.config.ip)
        except Exception as e:
            logger.warning(f"Erro no Wake-on-LAN: {e}")

        t0 = time.monotonic()
        tried_ws_power = False
        tried_ssdp_rediscover = False
        while time.monotonic() - t0 < timeout:
            ps = self.get_power_state(timeout=1.5)
            if self.is_awake_state(ps):
                if self.is_dlna_ready():
                    return True, "TV Samsung acordada e pronta para Dolby Atmos."
                if status_cb:
                    status_cb("TV ligada — aguardando serviço DLNA iniciar...")
            elif ps == "standby" and self.is_paired and not tried_ws_power and (time.monotonic() - t0) >= 3.0:
                # Reforço: se o Magic Packet sozinho não acordou em 3s e já temos token, envia KEY_POWER
                tried_ws_power = True
                if status_cb:
                    status_cb("Enviando comando Power (WebSocket) para acordar TV Samsung...")
                self.send_key("KEY_POWER", timeout=4.0)
            elif ps == "offline" and not tried_ssdp_rediscover and (time.monotonic() - t0) >= 6.0:
                # Resiliência a troca de IP via DHCP: se o IP antigo não respondeu após WoL, redescobre via SSDP
                tried_ssdp_rediscover = True
                try:
                    from core.dlna_bridge import discover_renderers

                    tv_rnd = next((r for r in discover_renderers() if r.is_tv), None)
                    if tv_rnd and tv_rnd.ip:
                        self.update_endpoint(ip=tv_rnd.ip)
                        if self.is_dlna_ready():
                            return True, "TV Samsung acordada e pronta para Dolby Atmos."
                except Exception:
                    pass
            time.sleep(1.0)

        return False, "A TV Samsung não respondeu ao Wake-on-LAN em 25s (verifique se 'Ligar com celular' está ativo na TV)."

    def turn_screen_off(self) -> tuple[bool, str]:
        """
        Apaga apenas o painel da TV (Picture Off) mantendo o áudio Dolby Atmos via HDMI eARC.
        Protegido contra duplo-toggle: se a TV já estiver em PowerState == 'pictureoff', NÃO reenvia a tecla!
        """
        if not self.is_paired:
            return False, "Pareie o controle da TV (1ª vez) em 'TV Samsung' para apagar a tela automaticamente."
        ps = self.get_power_state()
        if ps == "pictureoff":
            return True, "Tela da TV já está apagada (som Dolby Atmos continua na JBL)."
        if ps != "on":
            return False, "A TV Samsung não está ligada no momento."
        ok, msg = self.send_key("KEY_PICTURE_OFF")
        if ok:
            return True, "Tela da TV apagada (som Dolby Atmos continua na JBL)."
        return False, msg

    def turn_screen_on(self) -> tuple[bool, str]:
        """Acende novamente o painel da TV se ela estiver no modo Picture Off ('pictureoff')."""
        if not self.is_paired:
            return False, "Pareie o controle da TV (1ª vez) em 'TV Samsung' primeiro."
        ps = self.get_power_state()
        if ps == "on":
            return True, "A tela da TV Samsung já está acesa."
        if ps != "pictureoff":
            return False, "A TV Samsung está em standby (use 'Acordar TV agora')."
        ok, msg = self.send_key("KEY_PICTURE_OFF")
        if ok:
            return True, "Tela da TV Samsung acesa novamente."
        return False, msg

    def standby_tv(self) -> tuple[bool, str]:
        """Coloca a TV Samsung em standby com segurança (funciona tanto de 'on' quanto de 'pictureoff')."""
        if not self.is_paired:
            return False, "Pareie o controle da TV (1ª vez) em 'TV Samsung' para desligar pelo app."
        ps = self.get_power_state()
        if not self.is_awake_state(ps):
            return True, "TV Samsung já está em standby."
        ok, msg = self.send_key("KEY_POWER")
        if ok:
            return True, "TV Samsung colocada em standby."
        return False, msg


_TV_CONTROLLER: Optional[SamsungTVController] = None


def get_tv_controller() -> SamsungTVController:
    global _TV_CONTROLLER
    if _TV_CONTROLLER is None:
        _TV_CONTROLLER = SamsungTVController()
    return _TV_CONTROLLER
