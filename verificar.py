"""Teste de fumaca: percorre o app inteiro sem Supabase e sem internet.

    python verificar.py

Usa ?exemplo=1 nas telas de dados e forja o cookie de sessao, entao nao
depende de banco nem de chave de plataforma nenhuma. Serve para garantir que
rotas, permissoes, templates e a traducao de status nao quebraram.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import date, timedelta

# precisa vir antes de importar o app: fixa a chave que assina a sessao,
# para conseguirmos montar um cookie valido mais abaixo
os.environ.setdefault("CHAVE_SESSAO", "chave-apenas-para-teste-local-0123456789")

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app.main import app  # noqa: E402
from app.modelos import CANCELADA, EFETIVADA, ESTORNADA, PENDENTE  # noqa: E402
from app.servicos import consulta as servico_consulta  # noqa: E402
from app.servicos import exemplo as servico_exemplo  # noqa: E402

falhas = 0


def checar(nome: str, condicao: bool, detalhe: str = "") -> None:
    global falhas
    print(f"  [{'ok ' if condicao else 'FALHA'}] {nome}" + (f" - {detalhe}" if detalhe else ""))
    if not condicao:
        falhas += 1


def logar(c: TestClient, papel: str = "dono") -> None:
    """Monta o cookie de sessao na mao, no mesmo formato do SessionMiddleware."""
    dados = {
        "usuario": {
            "id": "00000000-0000-0000-0000-0000000000aa",
            "email": f"{papel}@exemplo.test",
            "nome": papel.capitalize(),
            "papel": papel,
        }
    }
    bruto = base64.b64encode(json.dumps(dados).encode())
    assinado = TimestampSigner(os.environ["CHAVE_SESSAO"]).sign(bruto).decode()
    # limpa antes: o jar do httpx acumula cookies de mesmo nome em vez de trocar
    c.cookies.clear()
    c.cookies.set("ecom_sessao", assinado)


# ---------------------------------------------------------------------------
def portaria(c: TestClient) -> None:
    print("portaria (sem login):")
    for rota in ("/", "/pedidos", "/contas", "/usuarios", "/ajuda"):
        r = c.get(rota, follow_redirects=False)
        ok = r.status_code == 303 and "/entrar" in r.headers.get("location", "")
        checar(f"{rota} exige login", ok, f"HTTP {r.status_code}")

    for rota in ("/entrar", "/saude"):
        r = c.get(rota)
        checar(f"{rota} fica aberta", r.status_code == 200, f"HTTP {r.status_code}")

    r = c.get("/static/estilo.css")
    checar("/static fica aberto", r.status_code == 200)


def telas(c: TestClient) -> None:
    print("telas (logado como dono):")
    for rota in ("/", "/pedidos", "/contas", "/usuarios", "/ajuda", "/saude",
                 "/contas/nova?canal=mercadolivre",
                 "/contas/nova?canal=shopee",
                 "/contas/nova?canal=tiktok"):
        r = c.get(rota)
        checar(rota, r.status_code == 200, f"HTTP {r.status_code}, {len(r.text)} bytes")

    print("telas com dados de exemplo:")
    for rota in ("/?exemplo=1", "/pedidos?exemplo=1"):
        r = c.get(rota)
        ok = r.status_code == 200 and "R$" in r.text and "exemplo" in r.text.lower()
        checar(rota, ok, f"HTTP {r.status_code}")


def permissoes(c: TestClient) -> None:
    print("permissoes:")

    logar(c, "leitor")
    r = c.get("/", follow_redirects=False)
    checar("leitor ve o painel", r.status_code == 200)

    r = c.get("/contas")
    checar("leitor nao ve botao de cadastrar", r.status_code == 200
           and "/contas/nova?canal=mercadolivre" not in r.text)

    r = c.get("/usuarios", follow_redirects=False)
    checar("leitor nao entra em usuarios", r.status_code == 303)

    r = c.get("/contas/nova?canal=shopee", follow_redirects=False)
    checar("leitor nao cadastra conta", r.status_code == 303)

    logar(c, "operador")
    r = c.get("/contas/nova?canal=shopee", follow_redirects=False)
    checar("operador tambem nao cadastra conta", r.status_code == 303)

    r = c.get("/usuarios", follow_redirects=False)
    checar("operador nao entra em usuarios", r.status_code == 303)

    logar(c, "dono")
    r = c.get("/usuarios")
    checar("dono entra em usuarios", r.status_code == 200)

    r = c.get("/contas/nova?canal=shopee")
    checar("dono cadastra conta", r.status_code == 200)


def filtros(c: TestClient) -> None:
    print("filtros:")
    r = c.get("/pedidos", params={"exemplo": 1, "grupo": "cancelada"})
    checar("filtro por situacao", r.status_code == 200 and "Cancelados" in r.text)

    r = c.get("/pedidos", params={"exemplo": 1, "canal": "shopee"})
    checar("filtro por canal", r.status_code == 200 and "Shopee" in r.text)

    r = c.get("/pedidos", params={"exemplo": 1, "q": "CAN-360"})
    checar("busca por SKU", r.status_code == 200)

    r = c.get("/", params={"inicio": "banana", "fim": "", "exemplo": 1})
    checar("data invalida nao quebra", r.status_code == 200)

    r = c.get("/", params={"inicio": "2026-10-01", "fim": "2026-09-01", "exemplo": 1})
    checar("periodo invertido e corrigido", r.status_code == 200)


def rotas_de_escrita(c: TestClient) -> None:
    print("rotas que dependem de banco (devem avisar, nao estourar):")
    r = c.get("/contas/00000000-0000-0000-0000-000000000000", follow_redirects=False)
    checar("conta inexistente e tratada", r.status_code in (200, 303), f"HTTP {r.status_code}")

    r = c.post("/contas/nova",
               data={"canal": "mercadolivre", "apelido": "", "client_id": "",
                     "client_secret": ""},
               follow_redirects=False)
    checar("cadastro vazio e recusado", r.status_code == 303)

    r = c.get("/retorno/mercadolivre", follow_redirects=False)
    checar("retorno sem codigo e tratado", r.status_code == 303)

    r = c.get("/retorno/inexistente", follow_redirects=False)
    checar("canal invalido no retorno e tratado", r.status_code == 303)

    r = c.post("/usuarios/novo",
               data={"email": "x@y.z", "senha": "curta", "nome": "", "papel": "leitor"},
               follow_redirects=False)
    checar("senha curta e recusada", r.status_code == 303)


def canais() -> None:
    print("canais:")
    from app.canais import CANAIS

    checar("tres canais registrados", len(CANAIS) == 3, ", ".join(CANAIS))
    for nome, canal in CANAIS.items():
        conta = {"id": "x", "apelido": "t", "canal": nome, "client_id": "123",
                 "_secret": "segredo", "extra": {"service_id": "9", "ambiente": "sandbox"}}
        try:
            url, _ = canal.url_autorizacao(conta, "estado123")
            checar(f"{nome}: monta url de autorizacao", url.startswith("https://"), url[:55])
        except Exception as e:
            checar(f"{nome}: monta url de autorizacao", False, str(e))


def cripto() -> None:
    print("criptografia:")
    from app.cripto import mascarar, proteger, revelar

    segredo = "APP_SECRET_123456"
    guardado = proteger(segredo)
    checar("texto cifrado nao contem o segredo", segredo not in (guardado or ""))
    checar("decifra de volta", revelar(guardado) == segredo)
    checar("mascara mostra so o final", mascarar(segredo).endswith("3456")
           and "APP" not in mascarar(segredo))
    checar("valor corrompido devolve None", revelar("lixo-nao-cifrado") is None)


def agregacao() -> None:
    print("agregacao:")
    inicio = date(2026, 9, 1)
    fim = inicio + timedelta(days=29)
    pedidos = servico_exemplo.pedidos(inicio, fim)
    checar("exemplo gera pedidos", len(pedidos) > 50, f"{len(pedidos)} pedidos")

    r = servico_consulta.resumir(pedidos)
    checar("total confere", r["total_pedidos"] == len(pedidos))

    soma = sum(b["quantidade"] for b in r["por_grupo"].values())
    checar("todo pedido caiu em um grupo", soma == len(pedidos), f"{soma} de {len(pedidos)}")

    efetivadas = [p for p in pedidos if p.grupo == EFETIVADA]
    checar("faturamento = soma dos efetivados",
           r["faturamento"] == sum(p.valor_total for p in efetivadas))
    checar("ticket medio coerente",
           r["ticket_medio"] > 0 and r["ticket_medio"] < r["faturamento"])
    checar("cancelado nao entra no faturamento",
           all(p.grupo != CANCELADA for p in efetivadas))
    checar("taxa de cancelamento entre 0 e 100",
           0 <= r["taxa_cancelamento"] <= 100, f"{r['taxa_cancelamento']:.1f}%")

    filtrado = servico_consulta.filtrar(pedidos, grupo=PENDENTE)
    checar("filtrar por grupo", all(p.grupo == PENDENTE for p in filtrado),
           f"{len(filtrado)} pendentes")
    checar("estornos separados dos cancelados",
           len(servico_consulta.filtrar(pedidos, grupo=ESTORNADA)) > 0)


def main() -> int:
    with TestClient(app) as c:
        portaria(c)
        logar(c, "dono")
        telas(c)
        filtros(c)
        rotas_de_escrita(c)
        permissoes(c)
    canais()
    cripto()
    agregacao()
    print(f"\n{'tudo ok' if not falhas else str(falhas) + ' falha(s)'}")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
