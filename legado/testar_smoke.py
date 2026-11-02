"""Teste de fumaca do painel: percorre todas as rotas sem precisar subir servidor.

    python testar_smoke.py
"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

BASE = Path(__file__).resolve().parent


def checar(nome, cond, detalhe=""):
    print(f"  [{'ok ' if cond else 'FALHA'}] {nome}{(' - ' + detalhe) if detalhe else ''}")
    return cond


def main():
    falhas = 0
    with TestClient(app) as c:
        print("rotas GET:")
        for rota in ("/", "/pedidos", "/produtos", "/importar"):
            r = c.get(rota)
            falhas += not checar(rota, r.status_code == 200 and "<table" in r.text,
                                 f"HTTP {r.status_code}, {len(r.text)} bytes")

        print("filtros:")
        r = c.get("/", params={"inicio": "2026-08-01", "fim": "2026-08-31"})
        falhas += not checar("painel com periodo", r.status_code == 200 and "R$" in r.text)
        r = c.get("/pedidos", params={"canal": "shopee"})
        falhas += not checar("pedidos filtrados por canal",
                             r.status_code == 200 and "Shopee" in r.text)
        r = c.get("/", params={"inicio": "banana", "fim": ""})
        falhas += not checar("data invalida nao quebra", r.status_code == 200)

        print("upload:")
        arquivo = BASE / "samples" / "tiktok_orders.csv"
        r = c.post(
            "/importar",
            files={"arquivo": (arquivo.name, arquivo.read_bytes(), "text/csv")},
            data={"canal": "", "forcar": ""},
        )
        falhas += not checar("reenvio detectado como duplicado",
                             r.status_code == 200 and "ja foi importado" in r.text)

        r = c.post(
            "/importar",
            files={"arquivo": ("vazio.csv", b"coluna_a,coluna_b\n1,2\n", "text/csv")},
            data={"canal": "", "forcar": ""},
        )
        falhas += not checar("arquivo irreconhecivel tratado",
                             r.status_code == 200 and "Nao importei" in r.text.replace("ã", "a"))

    print(f"\n{'tudo ok' if not falhas else str(falhas) + ' falha(s)'}")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
