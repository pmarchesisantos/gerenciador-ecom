"""Importa um relatorio pela linha de comando.

    python importar.py samples\\shopee_pedidos.csv
    python importar.py relatorio.xlsx --canal mercadolivre --forcar
    python importar.py relatorio.csv --colunas    (so diagnostica o cabecalho)
"""

import argparse
from pathlib import Path

from app.db import SessionLocal, init_db
from app.importers import PERFIS, colunas_nao_reconhecidas, detectar_perfil
from app.services.importacao import importar_arquivo


def main():
    ap = argparse.ArgumentParser(description="Importa relatorio de vendas")
    ap.add_argument("arquivo", type=Path)
    ap.add_argument("--canal", choices=sorted(PERFIS), help="pula a deteccao automatica")
    ap.add_argument("--forcar", action="store_true", help="reimporta arquivo ja processado")
    ap.add_argument("--colunas", action="store_true", help="lista colunas nao reconhecidas")
    args = ap.parse_args()

    init_db()

    if args.colunas:
        perfil = PERFIS[args.canal] if args.canal else detectar_perfil(args.arquivo)
        if not perfil:
            print("Canal nao identificado. Use --canal.")
            return
        sobras = colunas_nao_reconhecidas(perfil, args.arquivo)
        print(f"Canal detectado: {perfil.nome}")
        print(f"Colunas ignoradas ({len(sobras)}):")
        for c in sobras:
            print(f"  - {c}")
        return

    with SessionLocal() as s:
        r = importar_arquivo(s, args.arquivo, args.canal, forcar=args.forcar)

    if not r.ok:
        print(f"[erro] {r.erro}")
        return
    print(f"{r.canal}: {r.linhas} linhas -> {r.pedidos_novos} novos, "
          f"{r.pedidos_atualizados} atualizados")
    if r.skus_novos:
        print(f"SKUs novos (provisorios): {', '.join(r.skus_novos[:10])}"
              + (" ..." if len(r.skus_novos) > 10 else ""))


if __name__ == "__main__":
    main()
