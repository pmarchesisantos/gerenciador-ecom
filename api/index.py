"""Ponto de entrada da Vercel.

A Vercel procura funcoes dentro de api/. Este arquivo so expoe o mesmo app
FastAPI que o run.py usa localmente - nao ha logica aqui de proposito, para
local e producao rodarem exatamente o mesmo codigo.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402

__all__ = ["app"]
