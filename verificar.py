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
from app.config import FUSO as FUSO_T  # noqa: E402
from decimal import Decimal  # noqa: E402

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
    for rota in ("/", "/pedidos", "/contas", "/usuarios", "/ajuda", "/saude"):
        r = c.get(rota, follow_redirects=False)
        ok = r.status_code == 303 and "/entrar" in r.headers.get("location", "")
        checar(f"{rota} exige login", ok, f"HTTP {r.status_code}")

    r = c.get("/entrar")
    checar("/entrar fica aberta", r.status_code == 200, f"HTTP {r.status_code}")

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

    for rota in ("/ajuda", "/saude"):
        r = c.get(rota, follow_redirects=False)
        checar(f"operador nao entra em {rota}", r.status_code == 303,
               f"HTTP {r.status_code}")

    logar(c, "leitor")
    r = c.get("/")
    checar("leitor nao ve os links de dono no menu",
           "/usuarios" not in r.text and "/saude" not in r.text
           and "/ajuda" not in r.text)

    logar(c, "dono")
    r = c.get("/usuarios")
    checar("dono entra em usuarios", r.status_code == 200)

    r = c.get("/contas/nova?canal=shopee")
    checar("dono cadastra conta", r.status_code == 200)

    for rota in ("/ajuda", "/saude"):
        r = c.get(rota)
        checar(f"dono entra em {rota}", r.status_code == 200)


def escopo(c: TestClient) -> None:
    print("visibilidade por loja:")
    from app.servicos.contas import filtrar_visiveis

    linhas = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    checar("dono ve todas", filtrar_visiveis(linhas, None) == linhas)
    checar("sem nenhuma atribuida, ve zero", filtrar_visiveis(linhas, set()) == [])
    checar("ve so as atribuidas",
           [x["id"] for x in filtrar_visiveis(linhas, {"a", "c"})] == ["a", "c"])

    logar(c, "leitor")
    r = c.get("/contas")
    # o que importa e que nenhuma loja aparece, seja por escopo vazio ou por
    # erro de infraestrutura: em nenhum caso o leitor pode ver credenciais
    checar("leitor nao ve credencial de loja",
           r.status_code == 200 and "Secret Key" not in r.text
           and "Token de acesso vence" not in r.text)
    checar("leitor nao ve o bloco de atribuicao",
           "Quem enxerga esta loja" not in r.text)

    r = c.post("/contas/00000000-0000-0000-0000-000000000000/acessos",
               data={"usuario_id": "x"}, follow_redirects=False)
    checar("leitor nao altera quem enxerga", r.status_code == 303)

    r = c.get("/contas/00000000-0000-0000-0000-000000000000")
    checar("leitor nao abre loja de fora do escopo",
           "Credenciais guardadas" not in r.text)

    logar(c, "operador")
    r = c.post("/contas/00000000-0000-0000-0000-000000000000/acessos",
               data={"usuario_id": "x"}, follow_redirects=False)
    checar("operador nao altera quem enxerga", r.status_code == 303)

    logar(c, "dono")


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


def dashboard(c: TestClient) -> None:
    print("dashboard:")
    logar(c, "dono")
    r = c.get("/?exemplo=1")
    t = r.text
    checar("hero com o faturamento", 'class="figura"' in t and "R$" in t)
    checar("barras de situacao", t.count('class="barra-trilho"') >= 5)
    checar("grafico diario presente", 'id="serie-dia"' in t and "chart.umd.min.js" in t)
    checar("tabela-espelho do grafico", "Ver como tabela" in t)

    from app.cores import CANAL_COR, GRUPO_COR
    checar("cada canal tem token proprio",
           all(c.startswith("var(--") for c in CANAL_COR.values())
           and len(set(CANAL_COR.values())) == 3)
    checar("status nao reutiliza token de canal",
           not (set(GRUPO_COR.values()) & set(CANAL_COR.values())))
    for canal_nome, token in CANAL_COR.items():
        checar(f"{canal_nome} usa {token}", token in t or canal_nome not in t)

    r = c.get("/")
    checar("painel real tambem renderiza", r.status_code == 200)


def envios(c: TestClient) -> None:
    print("dimensao de envio:")
    logar(c, "dono")

    r = c.get("/pedidos", params={"exemplo": 1})
    checar("coluna de envio aparece", "Envio" in r.text and "A enviar" in r.text)

    r = c.get("/pedidos", params={"exemplo": 1, "envio": "a_enviar"})
    checar("filtro a_enviar responde", r.status_code == 200)
    checar("explica o que e a fila",
           "esperando despacho" in r.text.lower())

    from app.modelos import (
        CANCELADA, EFETIVADA, ENVIO_A_ENVIAR, ENVIO_NAO_APLICA, PENDENTE,
    )
    from app.servicos import exemplo as ex

    pedidos = ex.pedidos(date(2026, 9, 1), date(2026, 9, 30))

    checar("pedido nao pago nao entra na fila de envio",
           all(p.envio == ENVIO_NAO_APLICA
               for p in pedidos if p.grupo in (PENDENTE, CANCELADA)))
    checar("so pedido efetivado fica a enviar",
           all(p.grupo == EFETIVADA
               for p in pedidos if p.envio == ENVIO_A_ENVIAR))

    fila = servico_consulta.filtrar(pedidos, envio=ENVIO_A_ENVIAR)
    checar("filtrar por envio", all(p.envio == ENVIO_A_ENVIAR for p in fila),
           f"{len(fila)} a enviar")

    contagem = servico_consulta.contar_envios(pedidos)
    checar("contagem fecha com o total",
           sum(contagem.values()) == len(pedidos))

    # pagamento e envio sao independentes: filtrar um nao mexe no outro
    efetivados = servico_consulta.filtrar(pedidos, grupo=EFETIVADA)
    checar("efetivado tem envio em varias etapas",
           len({p.envio for p in efetivados}) > 1,
           ", ".join(sorted({p.envio for p in efetivados})))


def tema(c: TestClient) -> None:
    print("tema claro/escuro:")
    logar(c, "dono")
    r = c.get("/")
    t = r.text
    checar("tema entra antes da pintura", "localStorage.getItem('tema')" in t)
    checar("botao de tema na lateral", "data-tema-botao" in t)
    checar("grafico avisado da troca", "tema-trocado" in t)

    r = c.get("/entrar", headers={"cookie": ""})
    checar("botao de tema tambem na entrada", "data-tema-botao" in r.text)

    css = c.get("/static/estilo.css").text
    checar("tema escuro por preferencia do sistema",
           "prefers-color-scheme: dark" in css)
    checar("escolha explicita vence o sistema",
           ':root[data-tema="escuro"]' in css and ':not([data-tema="claro"])' in css)

    for token in ("--canal-mercadolivre", "--canal-shopee", "--canal-tiktok",
                  "--serie", "--grade", "--papel"):
        # cada token precisa existir nos dois temas, nao so no claro
        checar(f"{token} definido nos dois temas", css.count(token + ":") >= 3,
               f"{css.count(token + ':')} definicoes")

    checar("grafico nao recebe hex do servidor",
           "#2a78d6" not in t and "borderColor: d." not in t)


def senhas(c: TestClient) -> None:
    print("recuperacao e troca de senha:")

    c.cookies.clear()
    for rota in ("/recuperar", "/nova-senha"):
        r = c.get(rota)
        checar(f"{rota} aberta sem login", r.status_code == 200, f"HTTP {r.status_code}")

    r = c.get("/entrar")
    checar("login oferece recuperacao", 'href="/recuperar"' in r.text)
    checar("sumiu o recado antigo",
           "dono do sistema pode troca" not in r.text)

    r = c.get("/nova-senha")
    checar("troca acontece no cliente, nao no servidor",
           "location.hash" in r.text and "/auth/v1/user" in r.text)
    checar("nova senha exige 8 caracteres", "nova.length < 8" in r.text)

    r = c.get("/minha-conta", follow_redirects=False)
    checar("minha conta exige login", r.status_code == 303)

    logar(c, "leitor")
    r = c.get("/minha-conta")
    checar("leitor tem minha conta", r.status_code == 200
           and "Trocar senha" in r.text)
    checar("pede a senha atual", 'name="senha_atual"' in r.text)

    r = c.post("/minha-conta/senha",
               data={"senha_atual": "x", "nova": "abcdefgh", "confirma": "outracoisa"},
               follow_redirects=False)
    checar("confirmacao diferente e recusada", r.status_code == 303
           and "erro=" in r.headers.get("location", ""))

    r = c.post("/minha-conta/senha",
               data={"senha_atual": "x", "nova": "curta", "confirma": "curta"},
               follow_redirects=False)
    checar("senha curta e recusada", r.status_code == 303
           and "erro=" in r.headers.get("location", ""))

    from app.auth import url_nova_senha
    checar("link do e-mail aponta para o app",
           url_nova_senha().endswith("/nova-senha"), url_nova_senha())

    logar(c, "dono")
    r = c.get("/saude")
    checar("diagnostico mostra a url a cadastrar no Supabase",
           url_nova_senha() in r.text and "Redirect URLs" in r.text)
    checar("diagnostico avisa do fallback para localhost",
           "localhost:3000" in r.text)
    r = c.get("/ajuda")
    checar("passo a passo mostra a url a cadastrar", url_nova_senha() in r.text)

    logar(c, "dono")


def responsivo(c: TestClient) -> None:
    print("tela estreita:")
    logar(c, "dono")
    r = c.get("/")
    checar("barra superior do celular", 'class="barra-mobile"' in r.text)
    checar("gaveta com fundo escuro", 'id="fundo-menu"' in r.text)
    checar("gaveta fecha no Escape", "'Escape'" in r.text)

    r = c.get("/pedidos?exemplo=1")
    checar("tabela rola, nao empilha",
           'class="rolagem"' in r.text and "empilha" not in r.text)
    checar("sem rotulos de empilhamento", "data-rotulo" not in r.text)

    css = c.get("/static/estilo.css").text
    checar("menu vira sobreposicao no celular",
           'html[data-menu="aberto"] aside.lateral' in css)
    checar("formulario de uma coluna no celular",
           "form.filtros { flex-direction: column" in css)
    checar("indicadores viram faixa que rola",
           ".kpis .tiles {" in css and "overflow-x: auto" in css)
    checar("tabela mantem a largura e rola",
           ".rolagem table { min-width: max-content; }" in css)


def entrada(c: TestClient) -> None:
    print("tela de entrada:")
    c.cookies.clear()
    for rota in ("/entrar", "/recuperar", "/nova-senha"):
        r = c.get(rota)
        checar(f"{rota} abre", r.status_code == 200, f"HTTP {r.status_code}")
        checar(f"{rota} sem painel decorativo",
               "entrada-arte" not in r.text and "entrada-tela" not in r.text)
        checar(f"{rota} e um cartao centralizado",
               'class="entrada"' in r.text and 'class="caixa"' in r.text)

    css = c.get("/static/estilo.css").text
    checar("entrada centralizada no css",
           ".entrada {" in css and "place-items: center" in css)
    checar("css do split removido",
           "entrada-arte" not in css and "entrada-tela" not in css)
    logar(c, "dono")


def etiquetas(c: TestClient) -> None:
    print("etiquetas:")
    from app.canais import CANAIS
    from app.canais.mercadolivre import MercadoLivre
    from app.modelos import ENVIO_A_ENVIAR, ENVIO_ENTREGUE, Pedido

    checar("ML e TikTok geram etiqueta; Shopee ainda nao",
           sorted(n for n, ch in CANAIS.items() if ch.gera_etiqueta)
           == ["mercadolivre", "tiktok"])
    checar("so o TikTok despacha ao imprimir",
           [n for n, ch in CANAIS.items() if ch.despacha_ao_imprimir] == ["tiktok"])

    def pedido(envio, cru, eid="99", pronta=False):
        from datetime import datetime
        return Pedido(canal="mercadolivre", conta="t", pedido_id="P" + eid,
                      criado_em=datetime.now(), grupo=EFETIVADA, status_origem="paid",
                      envio=envio, envio_origem=cru, envio_id=eid, etiqueta_pronta=pronta)

    pronto = pedido(ENVIO_A_ENVIAR, "ready_to_ship/ready_to_print", "1", True)
    cedo = pedido(ENVIO_A_ENVIAR, "handling", "2")
    tarde = pedido(ENVIO_ENTREGUE, "delivered", "3")

    checar("so marca o que tem documento pronto",
           pronto.pode_etiqueta and not cedo.pode_etiqueta and not tarde.pode_etiqueta)

    ml = MercadoLivre()
    try:
        asyncio.get_event_loop()
    except Exception:
        pass
    import asyncio as aio
    try:
        aio.run(ml.etiquetas({"_access": "x"}, [cedo, tarde]))
        checar("recusa lote sem nenhum imprimivel", False, "nao levantou erro")
    except Exception as e:
        checar("recusa lote sem nenhum imprimivel", "ready_to_ship" in str(e))

    try:
        aio.run(ml.etiquetas({"_access": "x"}, [pronto] * 51))
        checar("recusa lote acima do teto", False, "nao levantou erro")
    except Exception as e:
        checar("recusa lote acima do teto", "maximo 50" in str(e), str(e)[:60])

    import httpx
    corpo = {"failed_shipments": [
        {"shipment_id": "1", "order_id": 777, "cause": "NOT_PRINTABLE_STATUS",
         "message": "status is delivered"}]}
    msg = MercadoLivre._erro_etiqueta(httpx.Response(400, json=corpo))
    checar("erro do ML vira texto legivel", "777" in msg and "entregue" in msg)

    print("tiktok: despachar e imprimir:")
    from app.canais.tiktok import HANDOVER_PADRAO, SEM_DESPACHO, TikTokShop

    tt = TikTokShop()
    checar("codigo de 'ainda nao despachado' conhecido", SEM_DESPACHO == 21042104)
    checar("entrega padrao e levar ao ponto", HANDOVER_PADRAO == "DROP_OFF")

    import httpx as _hx
    from app.canais.base import ErroCanal as _Erro
    try:
        aio2 = __import__("asyncio")
        class _Falso:
            def __init__(self,*a,**k): pass
            async def __aenter__(self): return self
            async def __aexit__(self,*a): return False
            async def get(self,*a,**k):
                return _hx.Response(401, text='{"code":105005,"message":"Access denied"}')
            async def post(self,*a,**k): return await self.get()
        from unittest.mock import patch as _patch
        with _patch.object(_hx, "AsyncClient", _Falso):
            aio2.run(tt._chamar({"_access":"x","client_id":"1","_secret":"s","extra":{}},
                                "/fulfillment/202309/packages/1/ship"))
        checar("escopo faltando vira texto util", False, "nao levantou")
    except _Erro as e:
        checar("escopo faltando vira texto util",
               "Fulfillment" in str(e) and "Reautorizar" in str(e), str(e)[:70])

    import asyncio as aio2
    try:
        aio2.run(tt.etiquetas({"_access": "x", "client_id": "1", "_secret": "s"},
                              [pedido(ENVIO_A_ENVIAR, "AWAITING_SHIPMENT", "", False)]))
        checar("sem pacote, recusa", False, "nao levantou erro")
    except Exception as e:
        checar("sem pacote, recusa", "pacote" in str(e).lower(), str(e)[:60])

    com_pacote = Pedido(canal="tiktok", conta="t", pedido_id="T1",
                        criado_em=__import__("datetime").datetime.now(tz=FUSO_T),
                        grupo=EFETIVADA, status_origem="AWAITING_SHIPMENT",
                        envio=ENVIO_A_ENVIAR, envio_id="PKG1", etiqueta_pronta=True)
    try:
        aio2.run(tt.etiquetas({"_access": "x", "client_id": "1", "_secret": "s"},
                              [com_pacote] * 21))
        checar("recusa lote acima do teto", False, "nao levantou erro")
    except Exception as e:
        checar("recusa lote acima do teto", "maximo 20" in str(e), str(e)[:60])

    # juntar PDFs de verdade, sem rede
    import io
    from pypdf import PdfWriter
    def pdf_de(paginas):
        w = PdfWriter()
        for _ in range(paginas):
            w.add_blank_page(width=200, height=300)
        b = io.BytesIO(); w.write(b); return b.getvalue()

    import httpx as hx
    from unittest.mock import patch

    class RespostaFake:
        def __init__(self, c): self.status_code = 200; self.content = c
    class ClienteFake:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): return RespostaFake(pdf_de(1))

    with patch.object(hx, "AsyncClient", ClienteFake):
        juntado = aio2.run(TikTokShop._juntar(["a", "b", "c"]))
    from pypdf import PdfReader
    checar("tres etiquetas viram um PDF de tres paginas",
           len(PdfReader(io.BytesIO(juntado)).pages) == 3)


    print("rota de etiquetas:")
    c.cookies.clear()
    r = c.post("/pedidos/etiquetas", data={"pedido_id": "1"}, follow_redirects=False)
    checar("exige login", r.status_code == 303 and "/entrar" in r.headers.get("location", ""))

    logar(c, "leitor")
    r = c.post("/pedidos/etiquetas", data={"pedido_id": "1"}, follow_redirects=False)
    checar("leitor nao imprime", r.status_code == 303
           and "erro=" in r.headers.get("location", ""))

    logar(c, "dono")
    r = c.post("/pedidos/etiquetas", data={}, follow_redirects=False)
    checar("selecao vazia e recusada", r.status_code == 303
           and "erro=" in r.headers.get("location", ""))

    r = c.get("/pedidos?exemplo=1")
    checar("tela oferece impressao quando ha pedido pronto",
           "Imprimir etiquetas" in r.text and "marca-etiqueta" in r.text)
    checar("titulo da pagina limpo",
           "<title>Pedidos - Sistema-Ecom</title>" in r.text)
    checar("script fica no corpo, nao no titulo",
           r.text.index("<script>") > r.text.index("</title>"))

    r = c.get("/pedidos?exemplo=1&grupo=cancelada")
    # a string "Imprimir etiquetas" tambem vive no script: checa o elemento
    checar("sem pedido pronto, nao mostra a barra",
           'id="barra-etiquetas"' not in r.text)
    checar("sem pedido pronto, nao ha caixa de marcar",
           'class="marca-etiqueta"' not in r.text)

    r = c.get("/pedidos?exemplo=1&envio=a_enviar")
    checar("a barra aparece quando ha o que imprimir",
           'id="barra-etiquetas"' in r.text)
    checar("caixa diz se o canal despacha ao imprimir",
           'data-despacha="1"' in r.text)
    checar("confirma antes de despachar",
           "realmente levar ao ponto de coleta" in r.text)

    print("por que o canal nao imprime:")
    from app.canais import CANAIS
    for nome in ("tiktok", "shopee"):
        checar(f"{nome} explica o motivo", bool(CANAIS[nome].motivo_sem_etiqueta))
    checar("TikTok avisa que imprimir tambem despacha",
           "avisa e imprime" in CANAIS["tiktok"].motivo_sem_etiqueta)
    checar("TikTok separa o aviso da coleta fisica",
           "ponto bipa" in CANAIS["tiktok"].motivo_sem_etiqueta)

    r = c.get("/pedidos?exemplo=1&envio=a_enviar")
    checar("tela avisa sobre quem nao imprime",
           "sem etiqueta disponível aqui" in r.text
           or "aguardando despacho sem" in r.text)

    print("grade larga:")
    checar("tabela de pedidos pede a barra flutuante",
           'class="rolagem" data-rolagem-lateral' in r.text)
    checar("tabela tem largura minima para nao espremer",
           'class="grade-pedidos"' in r.text)
    checar("titulo longo nao estica a linha", 'class="item-linha"' in r.text)
    css = c.get("/static/estilo.css").text
    checar("cabecalho fica fixo ao rolar",
           ".grade-pedidos thead th" in css and "position: sticky" in css)
    checar("valor em dinheiro nao quebra linha",
           "td.num, th.num { white-space: nowrap; }" in css)
    checar("barra flutuante tem estilo",
           ".barra-lateral-flutuante" in css and "position: fixed" in css)
    base = c.get("/").text
    checar("barra flutuante some quando nao precisa",
           "barra.style.display = precisa" in base)


def catalogo(c: TestClient) -> None:
    print("catalogo:")
    from app.servicos import produtos as sp

    # validacao pura, sem banco
    try:
        sp._limpar({"sku": "", "titulo": "x"})
        checar("SKU obrigatorio", False, "nao levantou erro")
    except sp.ErroProduto as e:
        checar("SKU obrigatorio", "SKU" in str(e))
    try:
        sp._limpar({"sku": "A", "titulo": ""})
        checar("titulo obrigatorio", False, "nao levantou erro")
    except sp.ErroProduto as e:
        checar("titulo obrigatorio", "título" in str(e).lower())
    try:
        sp._limpar({"sku": "A", "titulo": "x", "preco": "abc"})
        checar("preco invalido e recusado", False, "nao levantou erro")
    except sp.ErroProduto:
        checar("preco invalido e recusado", True)
    try:
        sp._limpar({"sku": "A", "titulo": "x", "estoque": "-5"})
        checar("estoque negativo e recusado", False, "nao levantou erro")
    except sp.ErroProduto:
        checar("estoque negativo e recusado", True)

    limpo = sp._limpar({"sku": " A1 ", "titulo": " Bota ", "preco": "129,90",
                        "estoque": "7", "ativo": True})
    checar("preco com virgula vira numero", float(limpo["preco"]) == 129.90,
           str(limpo["preco"]))
    checar("espaco em volta e aparado",
           limpo["sku"] == "A1" and limpo["titulo"] == "Bota")

    # nome de arquivo das fotos
    from app.armazenamento import _nome_seguro
    checar("nome de arquivo fica seguro para URL",
           _nome_seguro("Foto Boníta (1).JPG") == "Foto-Bonita-1-.JPG",
           _nome_seguro("Foto Boníta (1).JPG"))

    # unificacao de SKU no painel
    from datetime import datetime
    from app.modelos import EFETIVADA, ItemPedido, Pedido

    def venda(canal, sku, titulo, valor):
        return Pedido(canal=canal, conta="t", pedido_id=sku + canal,
                      criado_em=datetime.now(tz=FUSO_T), grupo=EFETIVADA,
                      status_origem="paid", valor_total=Decimal(valor),
                      itens=[ItemPedido(sku=sku, titulo=titulo, quantidade=1,
                                        valor_unitario=Decimal(valor))])

    pedidos = [venda("mercadolivre", "ML-9", "Fone ML", "100"),
               venda("tiktok", "TT-9", "Fone TikTok", "120")]

    solto = servico_consulta.resumir(pedidos)
    checar("sem catalogo, o mesmo produto aparece duas vezes",
           len(solto["top_sku"]) == 2 and solto["sku_unificados"] == 0)

    mapa = {"ML-9": {"sku": "FONE-01", "titulo": "Fone bluetooth"},
            "TT-9": {"sku": "FONE-01", "titulo": "Fone bluetooth"}}
    junto = servico_consulta.resumir(pedidos, mapa)
    checar("com catalogo, soma numa linha so", len(junto["top_sku"]) == 1)
    checar("usa o titulo do catalogo",
           junto["top_sku"][0]["titulo"] == "Fone bluetooth")
    checar("soma os valores dos dois canais",
           junto["top_sku"][0]["valor"] == Decimal("220"))
    checar("conta quantos foram unificados", junto["sku_unificados"] == 1)
    checar("faturamento nao muda com a unificacao",
           junto["faturamento"] == solto["faturamento"])

    print("telas do catalogo:")
    logar(c, "leitor")
    r = c.get("/produtos")
    checar("leitor ve a lista", r.status_code == 200)
    checar("leitor nao ve botao de cadastrar", "/produtos/novo" not in r.text)
    r = c.get("/produtos/novo", follow_redirects=False)
    checar("leitor nao cadastra", r.status_code == 303)

    logar(c, "operador")
    r = c.get("/produtos/novo")
    checar("operador cadastra", r.status_code == 200 and "SKU" in r.text)
    r = c.post("/produtos/novo", data={"sku": "", "titulo": ""}, follow_redirects=False)
    checar("cadastro vazio e recusado", r.status_code == 303
           and "erro=" in r.headers.get("location", ""))

    logar(c, "dono")


def main() -> int:
    with TestClient(app) as c:
        portaria(c)
        logar(c, "dono")
        telas(c)
        filtros(c)
        rotas_de_escrita(c)
        permissoes(c)
        escopo(c)
        dashboard(c)
        envios(c)
        tema(c)
        senhas(c)
        etiquetas(c)
        catalogo(c)
        responsivo(c)
        entrada(c)
    canais()
    cripto()
    agregacao()
    print(f"\n{'tudo ok' if not falhas else str(falhas) + ' falha(s)'}")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
