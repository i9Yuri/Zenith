# Copyright (c) 2026 Yuri Barbosa (@i9Yuri). All Rights Reserved.
# Proprietary source-available software (personal, non-commercial use only).
# Official repository: https://github.com/i9Yuri/Zenith
# Unauthorized commercial use, redistribution, or rebranding is prohibited. See LICENSE.
"""
AtmosHunter — Entry point
"""
import os
import sys
from pathlib import Path

# Garante que o diretório do projeto está no path
APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

# Isola o diretório de trabalho do processo em STAGING_DIR para que nenhum subprocesso
# ou biblioteca externa (yt-dlp, ffmpeg, aioslsk, tempfiles) crie fragmentos (.frag/.part) na raiz do projeto
STAGING_DIR = Path.home() / ".cache" / "atmoshunter" / "staging"
STAGING_DIR.mkdir(parents=True, exist_ok=True)
try:
    os.chdir(STAGING_DIR)
except OSError:
    pass

from core.slsk_service import cleanup_stray_artifacts

# Limpeza preventiva ativa de qualquer fragmento residual nas pastas do projeto e staging
cleanup_stray_artifacts([APP_DIR.parent, APP_DIR, STAGING_DIR])

from ui.main_window import run

if __name__ == "__main__":
    run()
