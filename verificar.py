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

    # mudar a categoria grava no produto: leitor nao pode
    r = c.post("/produtos/abc/categoria", data={"categoria_id": "MLB1"},
               follow_redirects=False)
    checar("operador nao e barrado na categoria", r.status_code == 303)
    logar(c, "leitor")
    r = c.post("/produtos/abc/categoria", data={"categoria_id": "MLB1"},
               follow_redirects=False)
    checar("leitor nao define categoria",
           r.status_code == 303 and "erro=" in r.headers.get("location", ""),
           r.headers.get("location", ""))
    logar(c, "operador")

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
    checar("caixa marca por pedido quem ainda precisa de despacho",
           'data-despacha="1"' in r.text and 'data-despacha=""' in r.text)

    from app.canais.tiktok import TikTokShop as _TT
    conta_fake = {"apelido": "t", "extra": {}}
    ja_arranjado = _TT()._traduzir(
        {"id": "1", "status": "AWAITING_COLLECTION", "create_time": 1,
         "packages": [{"id": "P1"}], "payment": {}, "line_items": []}, conta_fake)
    a_arranjar = _TT()._traduzir(
        {"id": "2", "status": "AWAITING_SHIPMENT", "create_time": 1,
         "packages": [{"id": "P2"}], "payment": {}, "line_items": []}, conta_fake)
    checar("ja arranjado: imprime sem mexer na plataforma",
           ja_arranjado.pode_etiqueta and not ja_arranjado.exige_despacho)
    checar("ainda nao arranjado: imprimir avisa a plataforma",
           a_arranjar.pode_etiqueta and a_arranjar.exige_despacho)
    checar("os dois continuam na fila de despacho",
           ja_arranjado.envio == "a_enviar" and a_arranjar.envio == "a_enviar")
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


def botoes(c: TestClient) -> None:
    print("padronizacao dos botoes:")
    logar(c, "dono")
    css = c.get("/static/estilo.css").text

    checar("altura do botao vem de uma variavel",
           "--botao-altura: 38px" in css and "height: var(--botao-altura)" in css)
    checar("botao pequeno usa a propria variavel",
           "height: var(--botao-altura-p)" in css)
    checar("entrelinha fixada, para <a> e <button> baterem",
           "line-height: 1;" in css.split(".botao {")[1].split("}")[0])
    checar("botao nao encolhe nem quebra o texto",
           "flex: none" in css.split(".botao {")[1].split("}")[0]
           and "white-space: nowrap" in css.split(".botao {")[1].split("}")[0])
    checar("campo de formulario na mesma altura do botao",
           "box-sizing: border-box; height: var(--botao-altura)" in css)

    # a colisao que inflou os botoes contornados para 108px
    checar("estado vazio nao colide com modificador de botao",
           ".cartao.vazio {" in css and chr(10) + ".vazio {" not in css)
    checar("modificador de contorno tem nome proprio",
           ".botao.contorno" in css and ".botao.vazio" not in css)

    for rota in ("/contas", "/produtos", "/pedidos?exemplo=1", "/usuarios"):
        r = c.get(rota)
        checar(f"{rota} sem o nome antigo", "botao vazio" not in r.text)


def variacoes(c: TestClient) -> None:
    print("variacoes:")
    from app.servicos import produtos as sp

    checar("opcoes aparadas e sem repetido",
           sp._opcoes("Branco, Preto , Preto/Branco, branco")
           == ["Branco", "Preto", "Preto/Branco"])
    checar("quebra de linha tambem separa",
           len(sp._opcoes("37" + chr(10) + "38; 39")) == 3)

    grade = sp.combinar(["Branco", "Preto"], ["37", "38"])
    checar("grade e o produto cartesiano", len(grade) == 4)
    checar("ordem segue o que foi escrito", grade[0] == ("Branco", "37"))
    checar("um eixo so tambem vale",
           sp.combinar(["Branco"], []) == [("Branco", None)])
    checar("sem opcoes, nao gera nada", sp.combinar([], ["37"]) == [])

    checar("SKU sai do base, sem acento nem barra",
           sp.sku_da_variacao("AZERA", ("Preto/Branco", "37")) == "AZERA-PRETO-BRANCO-37")
    checar("SKU ignora eixo vazio",
           sp.sku_da_variacao("AZERA", ("Branco", None)) == "AZERA-BRANCO")
    checar("acento vira ascii",
           sp.sku_da_variacao("CAFÉ", ("Único",)) == "CAFE-UNICO")

    r = sp.resumo_grade([
        {"preco": 109.99, "estoque": 500, "ativa": True},
        {"preco": 89.90, "estoque": 10, "ativa": True},
        {"preco": None, "estoque": 0, "ativa": False},
    ])
    checar("resumo conta o que falta preencher", r["sem_preco"] == 1)
    checar("resumo soma o estoque", r["estoque"] == 510)
    checar("resumo acha a faixa de preco",
           r["preco_min"] == 89.90 and r["preco_max"] == 109.99)

    atrs = sp.montar_atributos(["Material", "", "Gênero"], ["Couro", "lixo", "Masculino"])
    checar("atributo sem nome e descartado",
           atrs == {"Material": "Couro", "Gênero": "Masculino"})

    print("campos do anuncio:")
    limpo = sp._limpar({
        "sku": "AZERA", "titulo": "Tenis", "ncm": " 64029990 ", "cest": "28.059.00",
        "origem_fiscal": "0", "cfop_dentro": "5102", "cfop_fora": "6102",
        "csosn": "102", "unidade": "PARES", "condicao": "novo",
        "categoria": "Sapatos > Tenis", "eixo1_nome": "Cor", "eixo2_nome": "Tamanho",
        "ativo": True,
    })
    for campo, valor in [("ncm", "64029990"), ("cest", "28.059.00"),
                         ("origem_fiscal", "0"), ("cfop_dentro", "5102"),
                         ("csosn", "102"), ("unidade", "PARES"),
                         ("eixo1_nome", "Cor"), ("eixo2_nome", "Tamanho")]:
        checar(f"campo {campo} preservado", limpo[campo] == valor, str(limpo[campo]))
    checar("unidade cai para UN quando vazia",
           sp._limpar({"sku": "A", "titulo": "B"})["unidade"] == "UN")

    print("telas de variacao:")
    logar(c, "operador")
    for rota, metodo in [("/produtos/x/variacoes/gerar", "post"),
                         ("/produtos/x/variacoes", "post"),
                         ("/produtos/x/variacoes/aplicar", "post")]:
        r = getattr(c, metodo)(rota, data={}, follow_redirects=False)
        checar(f"{rota} responde sem estourar", r.status_code == 303,
               f"HTTP {r.status_code}")

    logar(c, "leitor")
    r = c.post("/produtos/x/variacoes/gerar", data={"opcoes1": "a"},
               follow_redirects=False)
    checar("leitor nao gera grade", r.status_code == 303
           and "erro=" in r.headers.get("location", ""))

    r = c.get("/produtos/novo", follow_redirects=False)
    checar("leitor nao abre cadastro", r.status_code == 303)

    logar(c, "dono")
    r = c.get("/produtos/novo")
    for aba in ("Básico", "Especificação", "Fiscal", "Envio"):
        checar(f"aba {aba} na tela", aba in r.text)
    checar("campos fiscais presentes",
           'name="ncm"' in r.text and 'name="csosn"' in r.text
           and 'name="cfop_fora"' in r.text)
    checar("eixos de variacao presentes", 'name="atributo_chave"' in r.text)
    css = c.get("/static/estilo.css").text
    checar("dica nao herda o caixa-alta do rotulo",
           ".dica {" in css and "text-transform: none" in css)
    # um <select> cresce ate caber a opcao mais longa e estourava o cartao
    checar("controle nao fica mais largo que o container",
           "select, textarea {" in css and "max-width: 100%" in css)
    checar("campo do movimento pode encolher",
           ".form-movimento > * { min-width: 0; max-width: 100%; }" in css)


# ---------------------------------------------------------------------------
# resposta crua do ML, como /categories/{id}/attributes devolve
ATRIBUTOS_ML = [
    {"id": "BRAND", "name": "Marca", "value_type": "string",
     "tags": {"required": True}, "hint": "Como aparece na etiqueta", "relevance": 1},
    {"id": "MODEL", "name": "Modelo", "value_type": "string",
     "tags": {"catalog_required": True}, "relevance": 1},
    {"id": "FOOTWEAR_TYPE", "name": "Tipo de calcado", "value_type": "list",
     "tags": {}, "values": [{"id": "1", "name": "Tenis"}, {"id": "2", "name": "Bota"}],
     "relevance": 2},
    {"id": "COLOR", "name": "Cor", "value_type": "list",
     "tags": {"allow_variations": True}, "values": [{"id": "9", "name": "Branco"}]},
    {"id": "SHAFT_HEIGHT", "name": "Altura do cano", "value_type": "number_unit",
     "tags": {}, "allowed_units": [{"id": "cm", "name": "cm"}, {"id": "mm", "name": "mm"}],
     "default_unit": "cm", "relevance": 3},
    {"id": "ITEM_CONDITION", "name": "Condicao", "value_type": "list",
     "tags": {"hidden": True, "read_only": True}, "values": [{"id": "x", "name": "Novo"}]},
    {"id": "SELLER_SKU", "name": "SKU", "value_type": "string", "tags": {"fixed": True}},
]


def _pronto(valor):
    """Embrulha um valor ja calculado em algo que da await."""

    async def feito():
        return valor

    return feito()


def categorias_campos() -> None:
    print("campos da categoria:")
    from app.servicos import categorias as sc

    g = sc.organizar(ATRIBUTOS_ML)

    def ids(lista):
        return [c["id"] for c in lista]

    checar("obrigatorio por required e por catalog_required",
           ids(g["obrigatorios"]) == ["BRAND", "MODEL"], str(ids(g["obrigatorios"])))
    checar("cor vai para o grupo de variacao",
           ids(g["por_variacao"]) == ["COLOR"], str(ids(g["por_variacao"])))
    todos = ids(g["obrigatorios"] + g["opcionais"] + g["por_variacao"])
    checar("hidden/read_only/fixed ficam fora",
           "ITEM_CONDITION" not in todos and "SELLER_SKU" not in todos)
    checar("opcional sai na ordem de relevancia",
           ids(g["opcionais"]) == ["FOOTWEAR_TYPE", "SHAFT_HEIGHT"],
           str(ids(g["opcionais"])))

    tipos = {c["id"]: c["tipo"] for c in g["obrigatorios"] + g["opcionais"]}
    checar("lista com valores vira select", tipos["FOOTWEAR_TYPE"] == "lista")
    checar("number_unit mantem a unidade", tipos["SHAFT_HEIGHT"] == "numero_unidade")
    checar("string continua texto", tipos["BRAND"] == "texto")
    unidade = [c for c in g["opcionais"] if c["id"] == "SHAFT_HEIGHT"][0]
    checar("unidade padrao vem do ML", unidade["unidade_padrao"] == "cm")

    # teto: categoria com 40 opcionais nao despeja 40 campos na tela
    muitos = [{"id": "A%d" % i, "name": "Attr %d" % i, "value_type": "string",
               "tags": {}, "relevance": i} for i in range(40)]
    g2 = sc.organizar(muitos)
    checar("opcionais tem teto", len(g2["opcionais"]) == sc.TETO_OPCIONAIS)
    checar("tela diz quantos ficaram de fora",
           g2["opcionais_de_fora"] == 40 - sc.TETO_OPCIONAIS)
    checar("lista vazia nao quebra", sc.organizar([])["obrigatorios"] == [])
    checar("lixo na lista e ignorado",
           sc.organizar([None, {}, "x", {"id": "OK", "name": "Ok", "tags": {}}])
           ["opcionais"][0]["id"] == "OK")

    # montagem do que o formulario devolve
    valores = sc.montar_valores(
        ["BRAND", "SHAFT_HEIGHT", "MODEL", ""],
        ["Nike", "40", "   ", "ignorado"],
        ["", "cm", "", ""],
    )
    checar("valor com unidade e juntado",
           valores.get("SHAFT_HEIGHT") == "40 cm", str(valores))
    checar("campo em branco nao entra", "MODEL" not in valores)
    checar("id em branco nao entra", len(valores) == 2, str(valores))
    checar("listas de tamanhos diferentes nao estouram",
           sc.montar_valores(["A", "B"], ["1"], []) == {"A": "1"})

    ficha = {"obrigatorios": [{"id": "BRAND", "nome": "Marca"},
                              {"id": "MODEL", "nome": "Modelo"}]}
    checar("faltando aponta so o que esta vazio",
           sc.faltando(ficha, {"BRAND": "Nike"}) == ["Modelo"])
    checar("faltando com tudo preenchido e vazio",
           sc.faltando(ficha, {"BRAND": "Nike", "MODEL": "Air"}) == [])
    checar("sem categoria nao ha obrigatorio", sc.faltando({}, {}) == [])


def ficha_do_produto(c: TestClient) -> None:
    """Desenha a ficha de verdade, com banco e ML simulados.

    E o unico teste que percorre rota + template + macro dos campos. Sem ele,
    um erro no macro so apareceria com o Supabase ligado.
    """
    print("ficha do produto com categoria:")
    import re

    from app.servicos import categorias as sc
    from app.servicos import contas as scontas
    from app.servicos import produtos as sp

    pid = "11111111-1111-1111-1111-111111111111"
    produto = {
        "id": pid, "sku": "AZERA", "titulo": "Tenis Azera Masculino",
        "preco": 129.9, "estoque": 5, "ativo": True, "condicao": "novo",
        "criado_em": "2026-01-02T10:00:00+00:00",
        "ml_categoria_id": "MLB108746", "ml_categoria_nome": "Tenis",
        "atributos": {"Garantia": "90 dias"},
        "atributos_ml": {"BRAND": "Azera", "SHAFT_HEIGHT": "40 cm"},
    }
    ficha = dict(sc.organizar(ATRIBUTOS_ML))
    ficha.update({"categoria_id": "MLB108746", "nome": "Tenis",
                  "caminho": "Calcados > Tenis"})

    async def nada(*a, **k):
        return []

    originais = (sp.buscar, sp.fotos, sp.anuncios, sp.variacoes,
                 scontas.resumo, sc.ficha, sc.sugerir)
    try:
        sp.buscar = lambda *a, **k: _pronto(produto)
        sp.fotos = nada
        sp.anuncios = nada
        sp.variacoes = nada
        scontas.resumo = nada
        sc.ficha = lambda *a, **k: _pronto(ficha)
        sc.sugerir = lambda *a, **k: _pronto(
            [{"categoria_id": "MLB108746", "nome": "Tenis", "dominio": "Tenis",
              "adivinhados": {}}])

        r = c.get("/produtos/" + pid)
        checar("ficha abre", r.status_code == 200, "HTTP %s" % r.status_code)
        checar("categoria aparece pelo caminho, nao pelo codigo cru",
               "Calcados &gt; Tenis" in r.text or "Calcados > Tenis" in r.text)
        checar("obrigatorio marcado na tela", "obrigat" in r.text and "Marca" in r.text)
        checar("campo guardado volta preenchido", 'value="Azera"' in r.text)
        checar("numero e unidade voltam separados",
               'value="40"' in r.text and 'name="ml_atributo_unidade"' in r.text)
        checar("campo obrigatorio em branco gera aviso",
               "Modelo" in r.text and "sobe sem eles" in r.text)
        checar("cor nao vira campo do produto", 'value="COLOR"' not in r.text)
        checar("marcador de que os campos foram desenhados",
               'name="ml_campos_na_tela"' in r.text)
        checar("atributo livre continua existindo",
               'name="atributo_chave"' in r.text and "Outros atributos" in r.text)

        # as tres listas precisam sair com o mesmo tamanho, ou o servidor
        # casaria o valor de um campo com o id de outro
        n_id = len(re.findall('name="ml_atributo_id"', r.text))
        n_val = len(re.findall('name="ml_atributo_valor"', r.text))
        n_uni = len(re.findall('name="ml_atributo_unidade"', r.text))
        checar("id, valor e unidade saem alinhados",
               n_id == n_val == n_uni == 4, "%s/%s/%s" % (n_id, n_val, n_uni))

        r = c.get("/produtos/" + pid + "?sugerir=1")
        checar("sugestao aparece quando pedida",
               "sugere estas categorias" in r.text, "HTTP %s" % r.status_code)

        # ML fora do ar: a ficha continua abrindo e explica o que fazer
        def explode(*a, **k):
            raise sc.SemCanal("Conecte o Mercado Livre em Contas")

        sc.ficha = explode
        r = c.get("/produtos/" + pid)
        checar("ML fora do ar nao derruba a ficha", r.status_code == 200,
               "HTTP %s" % r.status_code)
        checar("e a tela diz o que fazer", "Conecte o Mercado Livre" in r.text)
        checar("sem campos desenhados, nada de marcador",
               'name="ml_campos_na_tela"' not in r.text)
    finally:
        (sp.buscar, sp.fotos, sp.anuncios, sp.variacoes,
         scontas.resumo, sc.ficha, sc.sugerir) = originais


# ---------------------------------------------------------------------------
# Banco de mentira: tabelas em memoria, so o bastante para o servico de
# estoque rodar. Vale o custo porque a baixa automatica e a unica parte do
# sistema que escreve sozinha, sem ninguem clicar - um erro ali corroi o
# estoque em silencio, e so apareceria quando faltasse par para despachar.
# ---------------------------------------------------------------------------
class BancoFalso:
    def __init__(self, tabelas):
        self.t = {k: [dict(r) for r in v] for k, v in tabelas.items()}

    @staticmethod
    def _casa(linha, filtros):
        for campo, cond in (filtros or {}).items():
            if not str(cond).startswith("eq."):
                continue
            if str(linha.get(campo)) != str(cond)[3:]:
                return False
        return True

    async def selecionar(self, tabela, filtros=None, ordem=None):
        return [dict(r) for r in self.t.get(tabela, []) if self._casa(r, filtros)]

    async def um(self, tabela, filtros):
        linhas = await self.selecionar(tabela, filtros)
        return linhas[0] if linhas else None

    async def inserir(self, tabela, dados):
        linha = dict(dados)
        linha.setdefault("id", "mov-%d" % (len(self.t.setdefault(tabela, [])) + 1))
        linha.setdefault("criado_em", "2026-11-04T12:00:00+00:00")
        self.t[tabela].append(linha)
        return linha

    async def inserir_varios(self, tabela, linhas, ignorar_repetidos=False):
        gravados = []
        for dados in linhas:
            # imita o indice unico parcial (canal, pedido_id, sku) do envio
            if dados.get("motivo") == "envio":
                chave = (dados.get("canal"), dados.get("pedido_id"), dados.get("sku"))
                existe = any(
                    (r.get("canal"), r.get("pedido_id"), r.get("sku")) == chave
                    and r.get("motivo") == "envio"
                    for r in self.t.get(tabela, [])
                )
                if existe:
                    if ignorar_repetidos:
                        continue
                    raise AssertionError("violou o indice unico: %s" % (chave,))
            gravados.append(await self.inserir(tabela, dados))
        return gravados

    async def atualizar(self, tabela, filtros, dados):
        for linha in self.t.get(tabela, []):
            if self._casa(linha, filtros):
                linha.update(dados)
                return dict(linha)
        return None

    async def registrar_evento(self, *a, **k):
        return None


def _pedido_falso(pedido_id, canal, envio, quando, itens):
    from app.modelos import ItemPedido, Pedido

    return Pedido(
        canal=canal, conta="loja", pedido_id=pedido_id, criado_em=quando,
        grupo=EFETIVADA, status_origem="", envio=envio,
        itens=[ItemPedido(sku=s, titulo=s, quantidade=q) for s, q in itens],
    )


def estoque_regras() -> None:
    print("estoque (regras):")
    from app.servicos import estoque as se

    checar("zero e zerado", se.situacao(0, 5) == se.ZERADO)
    checar("no minimo ja e acabando", se.situacao(5, 5) == se.ACABANDO)
    checar("abaixo do minimo e acabando", se.situacao(3, 5) == se.ACABANDO)
    checar("acima do minimo esta ok", se.situacao(6, 5) == se.OK)
    checar("negativo conta como zerado", se.situacao(-2, 5) == se.ZERADO)

    produto = {"id": "p1", "sku": "AZERA", "titulo": "Tenis", "estoque": 7,
               "estoque_minimo": 4, "controla_estoque": True}
    linha = se._linha(produto, None)
    checar("sem grade, o saldo e o do produto", linha["estoque"] == 7)
    checar("sem grade, nao ha variacao", linha["variacao_id"] is None)

    v = {"id": "v1", "sku": "AZERA-PRETO-39", "estoque": 2, "estoque_minimo": None,
         "eixo1_valor": "Preto", "eixo2_valor": "39", "ativa": True}
    linha = se._linha(produto, v)
    checar("variacao sem minimo herda o do produto", linha["minimo"] == 4)
    checar("variacao mostra os dois eixos", linha["variacao"] == "Preto / 39")
    checar("variacao com 2 e minimo 4 esta acabando",
           linha["situacao"] == se.ACABANDO)

    v2 = dict(v, estoque_minimo=1, estoque=2)
    checar("minimo proprio da variacao manda",
           se._linha(produto, v2)["situacao"] == se.OK)

    base = se._linha(produto, None)
    checar("produto sem controle sai dos avisos",
           se.alertas([dict(base, situacao=se.ZERADO, controla=False)]) == [])
    checar("variacao inativa sai dos avisos",
           se.alertas([dict(base, situacao=se.ZERADO, ativa=False)]) == [])
    checar("zerado entra nos avisos",
           len(se.alertas([dict(base, situacao=se.ZERADO)])) == 1)

    r = se.resumo([
        dict(base, estoque=10, situacao=se.OK),
        dict(base, estoque=0, situacao=se.ZERADO),
        dict(base, estoque=3, situacao=se.ACABANDO),
        dict(base, estoque=99, situacao=se.OK, controla=False),
    ])
    checar("resumo ignora quem nao controla", r["itens"] == 3, str(r))
    checar("resumo soma as unidades", r["pares"] == 13, str(r))
    checar("resumo conta zerados e acabando",
           r["zerados"] == 1 and r["acabando"] == 1, str(r))


def estoque_baixa() -> None:
    print("estoque (baixa automatica):")
    import asyncio
    from datetime import datetime, timedelta, timezone

    from app import db as db_real
    from app.servicos import estoque as se

    comeco = datetime(2026, 11, 1, tzinfo=timezone.utc)
    banco = BancoFalso({
        "produtos": [{"id": "p1", "sku": "AZERA", "titulo": "Tenis Azera",
                      "estoque": 0, "estoque_minimo": 5, "controla_estoque": True}],
        "produto_variacoes": [
            {"id": "v39", "produto_id": "p1", "sku": "AZERA-PRETO-39",
             "estoque": 10, "eixo1_valor": "Preto", "eixo2_valor": "39", "ativa": True},
            {"id": "v42", "produto_id": "p1", "sku": "AZERA-PRETO-42",
             "estoque": 6, "eixo1_valor": "Preto", "eixo2_valor": "42", "ativa": True},
        ],
        "produto_anuncios": [],
        # o controle comecou aqui: e a entrada que a pessoa digitou
        "estoque_movimentos": [
            {"id": "m0", "produto_id": "p1", "variacao_id": "v39",
             "sku": "AZERA-PRETO-39", "delta": 10, "motivo": "entrada",
             "criado_em": comeco.isoformat()},
            {"id": "m1", "produto_id": "p1", "variacao_id": "v42",
             "sku": "AZERA-PRETO-42", "delta": 6, "motivo": "entrada",
             "criado_em": comeco.isoformat()},
        ],
    })

    originais = {n: getattr(db_real, n) for n in
                 ("selecionar", "um", "inserir", "inserir_varios", "atualizar",
                  "registrar_evento")}
    for nome in originais:
        setattr(db_real, nome, getattr(banco, nome))
    try:
        depois = comeco + timedelta(days=1)
        antes_do_controle = comeco - timedelta(days=10)
        pedidos = [
            _pedido_falso("A1", "mercadolivre", "enviado", depois,
                          [("AZERA-PRETO-39", 2)]),
            _pedido_falso("A2", "mercadolivre", "entregue", depois,
                          [("AZERA-PRETO-42", 1)]),
            # ainda nao saiu daqui
            _pedido_falso("A3", "shopee", "a_enviar", depois,
                          [("AZERA-PRETO-39", 5)]),
            # vendido antes de existir controle: o saldo digitado ja desconta
            _pedido_falso("A4", "mercadolivre", "enviado", antes_do_controle,
                          [("AZERA-PRETO-39", 4)]),
            # SKU que nao esta no catalogo
            _pedido_falso("A5", "tiktok", "enviado", depois, [("DESCONHECIDO", 1)]),
        ]

        r = asyncio.run(se.conciliar(pedidos, {"id": "u1"}))
        checar("baixou so os despachados", r["baixados"] == 2, str(r["baixados"]))
        checar("somou as unidades certas", r["pares"] == 3, str(r["pares"]))
        saldo = {v["sku"]: v["estoque"] for v in banco.t["produto_variacoes"]}
        checar("39 saiu de 10 para 8", saldo["AZERA-PRETO-39"] == 8, str(saldo))
        checar("42 saiu de 6 para 5", saldo["AZERA-PRETO-42"] == 5, str(saldo))
        checar("pedido a enviar nao descontou", saldo["AZERA-PRETO-39"] != 3)
        checar("SKU fora do catalogo e reportado",
               [s["sku"] for s in r["sem_cadastro"]] == ["DESCONHECIDO"],
               str(r["sem_cadastro"]))
        envios = [m for m in banco.t["estoque_movimentos"] if m["motivo"] == "envio"]
        checar("o livro registra o pedido que causou a baixa",
               sorted(m["pedido_id"] for m in envios) == ["A1", "A2"])
        checar("o livro diz em quanto ficou",
               {m["sku"]: m["saldo_depois"] for m in envios}
               == {"AZERA-PRETO-39": 8, "AZERA-PRETO-42": 5})

        # a prova que importa: rodar de novo nao pode mexer em nada
        r2 = asyncio.run(se.conciliar(pedidos, {"id": "u1"}))
        saldo2 = {v["sku"]: v["estoque"] for v in banco.t["produto_variacoes"]}
        checar("segunda passada nao desconta de novo", r2["baixados"] == 0,
               str(r2["baixados"]))
        checar("saldo intacto depois do F5", saldo2 == saldo, str(saldo2))

        # o mesmo pedido com mais um par depois: so a diferenca entra
        pedidos.append(_pedido_falso("A6", "mercadolivre", "enviado", depois,
                                     [("AZERA-PRETO-39", 3)]))
        r3 = asyncio.run(se.conciliar(pedidos, {"id": "u1"}))
        saldo3 = {v["sku"]: v["estoque"] for v in banco.t["produto_variacoes"]}
        checar("pedido novo desconta normalmente",
               r3["baixados"] == 1 and saldo3["AZERA-PRETO-39"] == 5, str(saldo3))

        # movimento manual: entrada soma e deixa rastro
        linha = asyncio.run(se.movimentar("p1", "v39", 10, se.ENTRADA, {"id": "u1"},
                                          "NF 777"))
        checar("entrada soma no saldo", linha["estoque"] == 15, str(linha["estoque"]))
        checar("entrada volta a situacao para ok", linha["situacao"] == se.OK)
        ultimo = banco.t["estoque_movimentos"][-1]
        checar("entrada guarda a observacao", ultimo["observacao"] == "NF 777")

        # nao deixa o saldo ficar negativo
        try:
            asyncio.run(se.movimentar("p1", "v42", -99, se.AJUSTE, {"id": "u1"}))
            checar("ajuste nao pode zerar abaixo de zero", False, "nao levantou")
        except se.ErroEstoque as e:
            checar("ajuste nao pode zerar abaixo de zero", "5" in str(e), str(e))
    finally:
        for nome, fn in originais.items():
            setattr(db_real, nome, fn)


def tela_estoque(c: TestClient) -> None:
    print("tela de estoque:")
    from app.servicos import estoque as se

    linhas = [
        {"produto_id": "p1", "variacao_id": "v39", "produto": "Tenis Azera",
         "sku": "AZERA-PRETO-39", "variacao": "Preto / 39", "estoque": 0,
         "minimo": 5, "situacao": se.ZERADO, "controla": True, "ativa": True},
        {"produto_id": "p1", "variacao_id": "v42", "produto": "Tenis Azera",
         "sku": "AZERA-PRETO-42", "variacao": "Preto / 42", "estoque": 3,
         "minimo": 5, "situacao": se.ACABANDO, "controla": True, "ativa": True},
        {"produto_id": "p2", "variacao_id": None, "produto": "Meia", "sku": "MEIA",
         "variacao": "", "estoque": 40, "minimo": 5, "situacao": se.OK,
         "controla": True, "ativa": True},
    ]
    movimentos = [
        {"id": "m1", "produto_id": "p1", "sku": "AZERA-PRETO-39", "delta": -2,
         "saldo_depois": 0, "motivo": "envio", "pedido_id": "2000012345",
         "canal": "mercadolivre", "observacao": None,
         "criado_em": "2026-11-03T10:00:00+00:00"},
        {"id": "m2", "produto_id": "p1", "sku": "AZERA-PRETO-39", "delta": 10,
         "saldo_depois": 10, "motivo": "entrada", "pedido_id": None, "canal": None,
         "observacao": "NF 1234", "criado_em": "2026-11-01T10:00:00+00:00"},
    ]

    originais = (se.posicao, se.historico)
    try:
        se.posicao = lambda *a, **k: _pronto(linhas)
        se.historico = lambda *a, **k: _pronto(movimentos)

        r = c.get("/estoque")
        checar("/estoque abre", r.status_code == 200, "HTTP %s" % r.status_code)
        checar("mostra o que tem de cada item",
               "AZERA-PRETO-39" in r.text and "AZERA-PRETO-42" in r.text)
        checar("aviso de reposicao aparece",
               "sem estoque" in r.text.lower() and "acabando" in r.text.lower())
        checar("aviso diz quantos restam", "restam" in r.text)
        # MEIA aparece na tabela e no seletor de movimento; o que importa e
        # que ela NAO esteja dentro do aviso de reposicao
        aviso = r.text.split('alerta-estoque', 1)[-1].split('</div>', 1)[0]
        checar("item saudavel nao entra no aviso", "MEIA" not in aviso)
        checar("o aviso lista justamente os que faltam",
               "AZERA-PRETO-39" in aviso and "AZERA-PRETO-42" in aviso)
        checar("livro mostra o pedido que causou a baixa", "2000012345" in r.text)
        checar("livro mostra a entrada manual", "NF 1234" in r.text)
        checar("baixa por envio nao e oferecida como motivo manual",
               'value="envio"' not in r.text)
        checar("totais na tela", "Unidades em casa" in r.text)

        # o aviso NAO fica fixo nas telas: o lugar dele e a hora de imprimir
        r = c.get("/produtos")
        checar("tela de produtos sem faixa fixa de estoque",
               "alerta-estoque" not in r.text, "HTTP %s" % r.status_code)
        r = c.get("/pedidos?exemplo=1")
        checar("tela de pedidos sem faixa fixa de estoque",
               'id="aviso-reposicao"' not in r.text
               or 'style="display:none' in r.text)

        logar(c, "leitor")
        r = c.get("/estoque")
        checar("leitor ve, mas nao movimenta",
               r.status_code == 200 and 'action="/estoque/movimento"' not in r.text)
        r = c.post("/estoque/movimento",
                   data={"produto_id": "p1", "quantidade": "5", "motivo": "entrada"},
                   follow_redirects=False)
        checar("leitor nao registra movimento",
               r.status_code == 303 and "erro=" in r.headers.get("location", ""))
        logar(c, "dono")

        r = c.post("/estoque/movimento",
                   data={"produto_id": "p1", "quantidade": "3", "motivo": "envio"},
                   follow_redirects=False)
        checar("baixa por envio nao se faz na mao",
               "erro=" in r.headers.get("location", ""))
        r = c.post("/estoque/movimento",
                   data={"produto_id": "p1", "quantidade": "abc", "motivo": "entrada"},
                   follow_redirects=False)
        checar("quantidade nao numerica e recusada",
               "erro=" in r.headers.get("location", ""))
    finally:
        se.posicao, se.historico = originais


# item do Mercado Livre como o /items devolve, com grade de cor e tamanho
ITEM_ML = {
    "id": "MLB123456789",
    "title": "Tenis Azera Masculino Casual",
    "seller_custom_field": "AZERA",
    "price": 129.9,
    "available_quantity": 16,
    "condition": "new",
    "category_id": "MLB108746",
    "permalink": "https://produto.mercadolivre.com.br/MLB-123456789",
    "attributes": [
        {"id": "BRAND", "value_name": "Azera"},
        {"id": "MATERIAL", "value_name": "Sintetico"},
        {"id": "SEM_VALOR", "value_name": None},
    ],
    "pictures": [
        {"secure_url": "https://http2.mlstatic.com/a.jpg"},
        {"url": "https://http2.mlstatic.com/b.jpg"},
    ],
    "variations": [
        {"id": 991, "seller_custom_field": "AZERA-PRETO-39", "price": 129.9,
         "available_quantity": 10,
         "attribute_combinations": [
             {"name": "Cor", "value_name": "Preto"},
             {"name": "Tamanho", "value_name": "39"}]},
        {"id": 992, "seller_custom_field": "", "price": 139.9,
         "available_quantity": 6,
         "attribute_combinations": [
             {"name": "Cor", "value_name": "Branco"},
             {"name": "Tamanho", "value_name": "42"}]},
    ],
}


def importacao_traducao() -> None:
    print("importacao (traducao do anuncio):")
    from app.canais.mercadolivre import MercadoLivre

    a = MercadoLivre._traduzir_anuncio(ITEM_ML)
    checar("pega o SKU do vendedor, nao o codigo do ML", a["sku"] == "AZERA")
    checar("marca sai dos atributos", a["marca"] == "Azera")
    checar("atributo sem valor e descartado", "SEM_VALOR" not in a["atributos"])
    checar("categoria vem junto", a["categoria_id"] == "MLB108746")
    checar("condicao traduzida", a["condicao"] == "novo")
    checar("eixos saem das combinacoes", a["eixos"] == ["Cor", "Tamanho"],
           str(a["eixos"]))
    checar("duas variacoes", len(a["variacoes"]) == 2)
    checar("variacao traz os valores dos dois eixos",
           a["variacoes"][0]["valores"] == {"Cor": "Preto", "Tamanho": "39"})
    checar("estoque por variacao", a["variacoes"][0]["estoque"] == 10)
    checar("fotos aceitam secure_url e url", len(a["fotos"]) == 2, str(a["fotos"]))

    vazio = MercadoLivre._traduzir_anuncio({"id": "MLB1", "title": "X"})
    checar("anuncio sem grade nao inventa eixo", vazio["eixos"] == [])
    checar("anuncio sem nada nao quebra",
           vazio["variacoes"] == [] and vazio["fotos"] == [])


def importacao_previa() -> None:
    print("importacao (previa):")
    import asyncio

    from app import db as db_real
    from app.canais import mercadolivre as ml
    from app.servicos import contas as scontas
    from app.servicos import importacao as si

    banco = BancoFalso({
        # ja existe um produto com o SKU AZERA: tem de vincular, nao duplicar
        "produtos": [{"id": "p-ja", "sku": "AZERA", "titulo": "Tenis Azera (daqui)"}],
        "produto_anuncios": [
            {"id": "a1", "produto_id": "p-ja", "conta_id": "c1",
             "anuncio_id": "MLB999", "sku_canal": "OUTRO"},
        ],
    })
    conta = {"id": "c1", "canal": "mercadolivre", "apelido": "Minha loja",
             "access_token_cripto": "x", "ativa": True}

    originais_db = {n: getattr(db_real, n) for n in ("selecionar", "um", "inserir")}
    orig_buscar = scontas.buscar
    orig_pode = scontas.pode_ver
    orig_token = scontas.garantir_token
    orig_listar = ml.MercadoLivre.listar_anuncios
    try:
        for nome in originais_db:
            setattr(db_real, nome, getattr(banco, nome))
        scontas.buscar = lambda *a, **k: _pronto(conta)
        scontas.pode_ver = lambda *a, **k: _pronto(True)
        scontas.garantir_token = lambda *a, **k: _pronto(dict(conta, _access="t"))

        traduzido = ml.MercadoLivre._traduzir_anuncio(ITEM_ML)
        outro = dict(traduzido, anuncio_id="MLB999", sku="JA-VINCULADO")
        novo = dict(traduzido, anuncio_id="MLB777", sku="NOVO-SKU",
                    titulo="Chinelo", variacoes=[], eixos=[])

        async def falsa(self, c, teto=200):
            return [traduzido, outro, novo]

        ml.MercadoLivre.listar_anuncios = falsa

        p = asyncio.run(si.previa("c1", {"id": "u1"}))
        por_id = {i["anuncio_id"]: i for i in p["itens"]}
        checar("SKU que ja existe aqui vira vinculo",
               por_id["MLB123456789"]["situacao"] == si.VINCULA,
               por_id["MLB123456789"]["situacao"])
        checar("e aponta para o produto existente",
               por_id["MLB123456789"]["produto_existente"] == "p-ja")
        checar("anuncio ja vinculado e marcado como importado",
               por_id["MLB999"]["situacao"] == si.JA_IMPORTADO)
        checar("SKU inedito vira produto novo",
               por_id["MLB777"]["situacao"] == si.NOVO)
        checar("contadores batem",
               (p["novos"], p["vincula"], p["ja_importados"]) == (1, 1, 1),
               str((p["novos"], p["vincula"], p["ja_importados"])))
        checar("novos aparecem primeiro na lista",
               p["itens"][0]["situacao"] == si.NOVO)

        # anuncio sem SKU proprio cai no codigo do anuncio, nunca em branco
        sem_sku = dict(traduzido, anuncio_id="MLB555", sku="")

        async def so_um(self, c, teto=200):
            return [sem_sku]

        ml.MercadoLivre.listar_anuncios = so_um
        p2 = asyncio.run(si.previa("c1", {"id": "u1"}))
        checar("anuncio sem SKU usa o codigo do anuncio",
               p2["itens"][0]["sku"] == "MLB555", p2["itens"][0]["sku"])

        # importar sem marcar nada precisa reclamar, nao gravar
        try:
            asyncio.run(si.importar("c1", [], {"id": "u1"}))
            checar("importar vazio e recusado", False, "nao levantou")
        except si.ErroImportacao:
            checar("importar vazio e recusado", True)
    finally:
        for nome, fn in originais_db.items():
            setattr(db_real, nome, fn)
        scontas.buscar = orig_buscar
        scontas.pode_ver = orig_pode
        scontas.garantir_token = orig_token
        ml.MercadoLivre.listar_anuncios = orig_listar


def tela_importar(c: TestClient) -> None:
    print("tela de importacao:")
    logar(c, "leitor")
    r = c.get("/produtos/importar", follow_redirects=False)
    checar("leitor nao importa", r.status_code == 303)
    logar(c, "dono")

    r = c.get("/produtos/importar")
    checar("/produtos/importar abre", r.status_code == 200, "HTTP %s" % r.status_code)
    checar("explica que SKU repetido nao duplica",
           "n&atilde;o vira produto duplicado" in r.text or
           "não vira produto duplicado" in r.text)
    r = c.get("/produtos")
    checar("botao de importar na tela de produtos",
           "/produtos/importar" in r.text)


def estoque_por_id_de_variacao() -> None:
    """Anuncio sem SKU: o pedido traz o codigo do ANUNCIO, igual para todas as
    numeracoes. Quem diz qual par saiu e o id da variacao."""
    print("estoque (anuncio sem SKU proprio):")
    import asyncio
    from datetime import datetime, timedelta, timezone

    from app import db as db_real
    from app.modelos import ItemPedido, Pedido
    from app.servicos import estoque as se

    comeco = datetime(2026, 11, 1, tzinfo=timezone.utc)
    banco = BancoFalso({
        "produtos": [{"id": "p1", "sku": "MLB4827552571", "titulo": "Galocha",
                      "estoque": 0, "estoque_minimo": 5, "controla_estoque": True}],
        "produto_variacoes": [
            # SKU inventado na importacao; quem vale e o id da plataforma
            {"id": "v33", "produto_id": "p1", "sku": "MLB4827552571-PRETO-33",
             "anuncio_variacao_id": "900033", "estoque": 4,
             "eixo1_valor": "Preto", "eixo2_valor": "33", "ativa": True},
            {"id": "v34", "produto_id": "p1", "sku": "MLB4827552571-PRETO-34",
             "anuncio_variacao_id": "900034", "estoque": 7,
             "eixo1_valor": "Preto", "eixo2_valor": "34", "ativa": True},
        ],
        "produto_anuncios": [],
        "estoque_movimentos": [
            {"id": "m0", "produto_id": "p1", "variacao_id": "v33",
             "sku": "MLB4827552571-PRETO-33", "delta": 4, "motivo": "entrada",
             "criado_em": comeco.isoformat()},
        ],
    })

    originais = {n: getattr(db_real, n) for n in
                 ("selecionar", "um", "inserir", "inserir_varios", "atualizar",
                  "registrar_evento")}
    for nome in originais:
        setattr(db_real, nome, getattr(banco, nome))
    try:
        catalogo = asyncio.run(se.mapa_sku())
        checar("o id da variacao vira chave do catalogo",
               "900033" in catalogo and "900034" in catalogo)
        checar("o SKU continua sendo chave",
               "MLB4827552571-PRETO-33" in catalogo)

        # precedencia: com os dois, o SKU manda
        item = ItemPedido(sku="MLB4827552571-PRETO-34", titulo="x", quantidade=1,
                          sku_variacao="900033")
        checar("SKU manda sobre o id da variacao",
               se.achar(catalogo, item)["sku"] == "MLB4827552571-PRETO-34")
        # sem SKU util, cai no id
        item2 = ItemPedido(sku="MLB4827552571", titulo="x", quantidade=1,
                           sku_variacao="900033")
        checar("sem SKU proprio, o id da variacao resolve",
               se.achar(catalogo, item2)["sku"] == "MLB4827552571-PRETO-33")
        item3 = ItemPedido(sku="NADA", titulo="x", quantidade=1)
        checar("sem nenhum dos dois, nao casa", se.achar(catalogo, item3) is None)

        # UM pedido com DUAS numeracoes do mesmo anuncio: ambas tem o mesmo
        # "sku" cru. Se a chave do livro fosse o sku cru, uma sumiria.
        depois = comeco + timedelta(days=1)
        pedido = Pedido(
            canal="mercadolivre", conta="loja", pedido_id="P1", criado_em=depois,
            grupo=EFETIVADA, status_origem="", envio="enviado",
            itens=[
                ItemPedido(sku="MLB4827552571", titulo="33", quantidade=1,
                           sku_variacao="900033"),
                ItemPedido(sku="MLB4827552571", titulo="34", quantidade=2,
                           sku_variacao="900034"),
            ],
        )
        # o 34 tambem precisa ter inicio de controle
        banco.t["estoque_movimentos"].append(
            {"id": "m1", "produto_id": "p1", "variacao_id": "v34",
             "sku": "MLB4827552571-PRETO-34", "delta": 7, "motivo": "entrada",
             "criado_em": comeco.isoformat()})

        r = asyncio.run(se.conciliar([pedido], {"id": "u1"}))
        saldo = {v["sku"]: v["estoque"] for v in banco.t["produto_variacoes"]}
        checar("as duas numeracoes do mesmo pedido descontam",
               r["baixados"] == 2, str(r["baixados"]))
        checar("33 saiu de 4 para 3",
               saldo["MLB4827552571-PRETO-33"] == 3, str(saldo))
        checar("34 saiu de 7 para 5",
               saldo["MLB4827552571-PRETO-34"] == 5, str(saldo))
        envios = [m for m in banco.t["estoque_movimentos"] if m["motivo"] == "envio"]
        checar("o livro guarda o NOSSO sku, nao o codigo do anuncio",
               sorted(m["sku"] for m in envios)
               == ["MLB4827552571-PRETO-33", "MLB4827552571-PRETO-34"])

        r2 = asyncio.run(se.conciliar([pedido], {"id": "u1"}))
        checar("continua idempotente com o id da variacao",
               r2["baixados"] == 0 and
               {v["sku"]: v["estoque"] for v in banco.t["produto_variacoes"]} == saldo)
    finally:
        for nome, fn in originais.items():
            setattr(db_real, nome, fn)


def canais_id_de_variacao() -> None:
    print("canais (id da variacao no pedido):")
    from app.canais.mercadolivre import MercadoLivre

    conta = {"apelido": "loja", "loja_id": "1"}
    p = MercadoLivre()._traduzir(
        {"id": 1, "status": "paid", "order_items": [
            {"quantity": 1, "unit_price": 10,
             "item": {"id": "MLB1", "title": "T", "variation_id": 900033}}]},
        conta)
    checar("ML traz o variation_id no item",
           p.itens[0].sku_variacao == "900033", p.itens[0].sku_variacao)
    checar("sem SKU, o sku do item cai no codigo do anuncio",
           p.itens[0].sku == "MLB1")


def exclusao_em_massa(c: TestClient) -> None:
    print("exclusao em massa:")
    import asyncio

    from app.servicos import produtos as sp

    logar(c, "leitor")
    r = c.post("/produtos/remover-lote", data={"produto_id": "x"},
               follow_redirects=False)
    checar("leitor nao remove em lote",
           r.status_code == 303 and "erro=" in r.headers.get("location", ""))
    logar(c, "dono")

    r = c.post("/produtos/remover-lote", data={}, follow_redirects=False)
    checar("remover sem marcar nada e recusado",
           "erro=" in r.headers.get("location", ""))

    try:
        # ids distintos: repetidos sao deduplicados de proposito e nao
        # chegariam ao teto
        muitos = ["id-%d" % i for i in range(sp.TETO_REMOCAO + 1)]
        asyncio.run(sp.remover_varios(muitos, {"id": "u"}))
        checar("teto de remocao respeitado", False, "nao levantou")
    except sp.ErroProduto as e:
        checar("teto de remocao respeitado", str(sp.TETO_REMOCAO) in str(e))

    try:
        asyncio.run(sp.remover_varios(["", None], {"id": "u"}))
        checar("lista so com vazios e recusada", False, "nao levantou")
    except sp.ErroProduto:
        checar("lista so com vazios e recusada", True)


def scripts_das_telas() -> None:
    """Procura string de JS partida no meio por uma quebra de linha.

    Existe porque isso aconteceu duas vezes: um "\n" escrito no gerador virou
    quebra de verdade dentro de aspas, o <script> inteiro morreu com
    SyntaxError e a pagina continuou abrindo normalmente - so que sem nenhum
    comportamento. Erro silencioso e o pior tipo.
    """
    print("scripts das telas:")
    import glob
    import io as _io
    import os
    import re

    suspeitas = []
    for caminho in sorted(glob.glob("app/templates/*.html")):
        texto = _io.open(caminho, encoding="utf-8").read()
        for script in re.findall(r"<script>(.*?)</script>", texto, re.S):
            for n, linha in enumerate(script.split(chr(10)), 1):
                nua = linha.strip()
                if nua.startswith("//") or "{{" in nua or "{%" in nua:
                    continue
                # aspas simples nao escapadas; impar = string aberta na linha
                barra = chr(92)
                padrao = '(?<!' + barra + barra + ')' + chr(39)
                if len(re.findall(padrao, nua)) % 2:
                    suspeitas.append("%s:%d %s" % (os.path.basename(caminho), n,
                                                   nua[:60]))
    checar("nenhuma string de JS partida por quebra de linha",
           not suspeitas, "; ".join(suspeitas[:3]))


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
        variacoes(c)
        categorias_campos()
        ficha_do_produto(c)
        estoque_regras()
        estoque_baixa()
        estoque_por_id_de_variacao()
        canais_id_de_variacao()
        exclusao_em_massa(c)
        scripts_das_telas()
        tela_estoque(c)
        importacao_traducao()
        importacao_previa()
        tela_importar(c)
        botoes(c)
        responsivo(c)
        entrada(c)
    canais()
    cripto()
    agregacao()
    print(f"\n{'tudo ok' if not falhas else str(falhas) + ' falha(s)'}")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
