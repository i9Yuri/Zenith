# Copyright (c) 2026 Yuri Barbosa (@i9Yuri). All Rights Reserved.
# Proprietary source-available software (personal, non-commercial use only).
# Official repository: https://github.com/i9Yuri/Zenith
# Unauthorized commercial use, redistribution, or rebranding is prohibited. See LICENSE.
"""
AtmosHunter — Entry point
"""
import sys
import os

# Garante que o diretório do projeto está no path
sys.path.insert(0, os.path.dirname(__file__))

from ui.main_window import run

if __name__ == "__main__":
    run()
