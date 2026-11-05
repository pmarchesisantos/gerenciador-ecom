"""Painel web do Sistema-Ecom.

Rotas abertas:
  /entrar            login
  /primeiro-acesso   cria o primeiro dono, só enquanto não existir nenhum
  /saude             diagnóstico (precisa ficar aberto para depurar deploy)

Rotas protegidas (exigem sessão):
  /                  painel com os números do período
  /pedidos           lista de pedidos com filtros
  /contas            contas conectadas (cadastro, conectar, renovar)
  /contas/{id}       tudo o que sabemos sobre uma conta
  /usuarios          quem tem acesso (só dono)
  /retorno/{canal}   volta da autorização das plataformas
  /ajuda             o caminho das pedras para gerar as chaves
"""

from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app import auth, db
from app.canais import CANAIS, ErroCanal, canal as pegar_canal
from app.config import (
    CHAVE_SESSAO,
    SUPABASE_ANON_KEY,
    SUPABASE_URL,
    FUSO,
    NOME_FUSO,
    PRODUCAO,
    SESSAO_SEGUNDOS,
    diagnostico,
    problemas_criticos,
    redirect_uri,
    supabase_configurado,
)
from app.cores import GRADE, GRUPO_COR, SERIE, canal as cor_canal
from app.cripto import chave_ok, erro_da_chave, origem_da_chave
from app.modelos import (
    CANAIS_ROTULO,
    COR_ENVIO,
    COR_GRUPO,
    ENVIOS,
    EXPLICA_ENVIO,
    EXPLICA_GRUPO,
    GRUPOS,
    ROTULO_ENVIO,
    ROTULO_GRUPO,
)
from app.servicos import consulta as servico_consulta
from app.servicos import contas as servico_contas
from app.servicos import exemplo as servico_exemplo
from app.servicos import categorias as servico_categorias
from app.servicos import estoque as servico_estoque
from app.servicos import importacao as servico_importacao
from app.servicos import produtos as servico_produtos
from app.servicos import usuarios as servico_usuarios

PASTA = Path(__file__).resolve().parent

app = FastAPI(title="Sistema-Ecom", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=PASTA / "static"), name="static")
templates = Jinja2Templates(directory=str(PASTA / "templates"))


def _versao_estilo() -> str:
    """Sufixo do CSS, para o navegador nao servir a folha antiga do cache.

    Muda sozinho sempre que o arquivo muda - sem isso, uma alteracao de
    layout so aparece depois de o usuario limpar o cache.
    """
    try:
        return str(int((PASTA / "static" / "estilo.css").stat().st_mtime))
    except OSError:
        return "1"


ESTILO_VERSAO = _versao_estilo()

ROTAS_ABERTAS = {"/entrar", "/sair", "/primeiro-acesso", "/recuperar", "/nova-senha"}


async def _exigir_login(request: Request, seguir):
    caminho = request.url.path
    if caminho.startswith("/static") or caminho in ROTAS_ABERTAS:
        return await seguir(request)
    if not auth.usuario_da_sessao(request):
        destino = caminho + (f"?{request.url.query}" if request.url.query else "")
        return auth.para_login(destino)
    return await seguir(request)


# a ordem importa: o ultimo add_middleware fica por fora, entao a sessao
# precisa ser adicionada depois do guarda que a le
app.add_middleware(BaseHTTPMiddleware, dispatch=_exigir_login)
app.add_middleware(
    SessionMiddleware,
    secret_key=CHAVE_SESSAO or secrets.token_urlsafe(32),
    session_cookie="ecom_sessao",
    max_age=SESSAO_SEGUNDOS,
    same_site="lax",
    https_only=PRODUCAO,
)


# ---------------------------------------------------------------------------
# formatacao para as telas
# ---------------------------------------------------------------------------
def f_moeda(v) -> str:
    try:
        n = Decimal(str(v or 0))
    except Exception:
        n = Decimal(0)
    texto = f"{n:,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")
    return f"R$ {texto}"


def f_inteiro(v) -> str:
    try:
        return f"{int(v or 0):,}".replace(",", ".")
    except Exception:
        return "0"


def f_pct(v) -> str:
    try:
        return f"{float(v or 0):.1f}%".replace(".", ",")
    except Exception:
        return "0,0%"


def _para_datetime(v):
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return v


def f_data(v) -> str:
    if not v:
        return "-"
    d = _para_datetime(v)
    if d is None:
        return str(v)
    if isinstance(d, datetime):
        return d.astimezone(FUSO).strftime("%d/%m/%Y %H:%M")
    return d.strftime("%d/%m/%Y")


def f_dia(v) -> str:
    return v.strftime("%d/%m") if hasattr(v, "strftime") else str(v)


def f_falta(v) -> str:
    """Quanto tempo falta para uma data (ou se ja passou)."""
    if not v:
        return "-"
    d = _para_datetime(v)
    if not isinstance(d, datetime):
        return str(v)
    seg = int((d - datetime.now(tz=FUSO)).total_seconds())
    if seg < 0:
        return "vencido"
    if seg < 3600:
        return f"em {seg // 60} min"
    if seg < 86400:
        return f"em {seg // 3600} h"
    return f"em {seg // 86400} dias"


for _nome, _fn in [
    ("moeda", f_moeda),
    ("inteiro", f_inteiro),
    ("pct", f_pct),
    ("data", f_data),
    ("dia", f_dia),
    ("falta", f_falta),
]:
    templates.env.filters[_nome] = _fn


# ---------------------------------------------------------------------------
# apoio
# ---------------------------------------------------------------------------
def _url(texto: str) -> str:
    return quote(str(texto)[:300], safe="")


def _periodo(inicio: str | None, fim: str | None) -> dict:
    """Le o periodo da URL, caindo para os ultimos 30 dias quando vier torto."""
    hoje = datetime.now(tz=FUSO).date()
    padrao_inicio, padrao_fim = hoje - timedelta(days=29), hoje

    def ler(texto: str | None, padrao: date) -> date:
        if not texto:
            return padrao
        try:
            return date.fromisoformat(texto.strip())
        except ValueError:
            return padrao

    d1, d2 = ler(inicio, padrao_inicio), ler(fim, padrao_fim)
    if d1 > d2:
        d1, d2 = d2, d1
    return {"inicio": d1, "fim": d2, "dias": (d2 - d1).days + 1}


def _atalhos() -> dict:
    hoje = datetime.now(tz=FUSO).date()
    return {
        "hoje": hoje,
        "sete": hoje - timedelta(days=6),
        "trinta": hoje - timedelta(days=29),
        "mes": hoje.replace(day=1),
    }


async def _mapa_sku() -> dict:
    """De/para do catalogo. Falha em silencio: unificar e um bonus, nao
    condicao para o painel existir."""
    try:
        return await servico_produtos.mapa_sku_canal()
    except Exception:
        return {}


def _graficos(resumo: dict) -> dict:
    """Dados que vao para os graficos, ja com as cores resolvidas no servidor.

    Montar aqui (e nao no navegador) mantem uma unica fonte de verdade para
    cor e numero: a tela e o grafico nunca discordam.
    """
    barras_canal = [
        {
            "rotulo": c["rotulo"],
            "valor": float(c["grupos"]["efetivada"]["valor"]),
            "pedidos": c["grupos"]["efetivada"]["quantidade"],
            "cor": cor_canal(c["canal"]),
        }
        for c in resumo["canais"]
    ]
    total_canal = sum(b["valor"] for b in barras_canal) or 1.0
    for b in barras_canal:
        b["fatia"] = b["valor"] / total_canal * 100
    barras_canal.sort(key=lambda b: b["valor"], reverse=True)

    barras_grupo = []
    maior_grupo = max(
        (b["quantidade"] for b in resumo["por_grupo"].values()), default=0
    ) or 1
    for g in GRUPOS:
        bloco_g = resumo["por_grupo"][g]
        barras_grupo.append(
            {
                "grupo": g,
                "rotulo": ROTULO_GRUPO[g],
                "quantidade": bloco_g["quantidade"],
                "valor": float(bloco_g["valor"]),
                "cor": GRUPO_COR[g],
                "largura": bloco_g["quantidade"] / maior_grupo * 100,
            }
        )

    maior_sku = max((s["valor"] for s in resumo["top_sku"]), default=0) or 1
    barras_sku = [
        {
            "sku": s["sku"],
            "titulo": s["titulo"],
            "itens": s["itens"],
            "valor": float(s["valor"]),
            "largura": float(s["valor"]) / float(maior_sku) * 100,
        }
        for s in resumo["top_sku"][:8]
    ]

    # o gráfico lê as cores do CSS na hora de desenhar, para acompanhar o tema
    serie = {
        "rotulos": [d["dia"].strftime("%d/%m") for d in resumo["dias"]],
        "valores": [float(d["valor"]) for d in resumo["dias"]],
        "pedidos": [d["quantidade"] for d in resumo["dias"]],
    }

    return {
        "canais": barras_canal,
        "grupos": barras_grupo,
        "sku": barras_sku,
        "serie": serie,
    }


def _base(request: Request, periodo: dict, **extra) -> dict:
    usuario = auth.usuario_da_sessao(request)
    return {
        "request": request,
        "periodo": periodo,
        "atalhos": _atalhos(),
        "canais": CANAIS,
        "canais_rotulo": CANAIS_ROTULO,
        "grupos": GRUPOS,
        "rotulo_grupo": ROTULO_GRUPO,
        "explica_grupo": EXPLICA_GRUPO,
        "cor_grupo": COR_GRUPO,
        "envios": ENVIOS,
        "rotulo_envio": ROTULO_ENVIO,
        "explica_envio": EXPLICA_ENVIO,
        "cor_envio": COR_ENVIO,
        "supabase_ok": supabase_configurado(),
        "estilo_versao": ESTILO_VERSAO,
        "usuario": usuario,
        "pode": lambda papel: auth.pode(usuario, papel),
        "rotulo_papel": auth.ROTULO_PAPEL,
        "aviso": request.query_params.get("aviso"),
        "erro": request.query_params.get("erro"),
        "criticos": problemas_criticos(),
        **extra,
    }


async def _carregar(
    periodo: dict,
    exemplo: bool,
    forcar: bool,
    usuario: dict | None = None,
    com_envio: bool = False,
) -> tuple[dict, str]:
    """Devolve (dados, erro de infraestrutura)."""
    if exemplo:
        return servico_exemplo.resultado(periodo["inicio"], periodo["fim"]), ""
    try:
        dados = await servico_consulta.consultar(
            periodo["inicio"],
            periodo["fim"],
            forcar=forcar,
            usuario=usuario,
            com_envio=com_envio,
        )
        return dados, ""
    except db.ErroBanco as e:
        vazio = {
            "pedidos": [],
            "resultados": [],
            "erros": [],
            "houve_consulta": False,
            "atualizado_em": datetime.now(tz=FUSO),
            "escopo_limitado": False,
            "lojas_escondidas": 0,
            "lojas_visiveis": 0,
        }
        return vazio, str(e)


def _volta(destino: str, aviso: str = "", erro: str = "") -> RedirectResponse:
    sep = "&" if "?" in destino else "?"
    if aviso:
        destino = f"{destino}{sep}aviso={_url(aviso)}"
    elif erro:
        destino = f"{destino}{sep}erro={_url(erro)}"
    return RedirectResponse(destino, status_code=303)


# ===========================================================================
# login
# ===========================================================================
@app.get("/entrar", response_class=HTMLResponse)
async def entrar_tela(request: Request, proximo: str = "/"):
    if auth.usuario_da_sessao(request):
        return RedirectResponse(proximo or "/", status_code=303)

    primeiro = False
    erro_infra = ""
    try:
        primeiro = not await servico_usuarios.existe_algum()
    except db.ErroBanco as e:
        erro_infra = str(e)

    return templates.TemplateResponse(
        "login.html",
        _base(
            request,
            _periodo(None, None),
            proximo=proximo,
            primeiro=primeiro,
            erro_infra=erro_infra,
        ),
    )


@app.post("/entrar")
async def entrar_envio(
    request: Request,
    email: str = Form(...),
    senha: str = Form(...),
    proximo: str = Form("/"),
):
    try:
        usuario = await auth.entrar(email, senha)
        perfil = await servico_usuarios.buscar(usuario["id"])
    except (auth.ErroAuth, db.ErroBanco) as e:
        return _volta(f"/entrar?proximo={_url(proximo)}", erro=str(e))

    if not perfil:
        return _volta(
            "/entrar",
            erro="Sua senha confere, mas você ainda não tem perfil neste sistema. "
                 "Peça a quem é dono para liberar o seu acesso.",
        )
    if not perfil.get("ativo"):
        return _volta("/entrar", erro="Seu acesso está desativado.")

    auth.abrir_sessao(request, perfil)
    await servico_usuarios.marcar_acesso(perfil["id"])
    return RedirectResponse(proximo or "/", status_code=303)


@app.get("/sair")
async def sair(request: Request):
    auth.fechar_sessao(request)
    return _volta("/entrar", aviso="Você saiu do sistema.")


@app.post("/primeiro-acesso")
async def primeiro_acesso(
    request: Request,
    email: str = Form(...),
    senha: str = Form(...),
    nome: str = Form(""),
):
    if len(senha) < 8:
        return _volta("/entrar", erro="A senha precisa ter ao menos 8 caracteres.")
    try:
        perfil = await servico_usuarios.criar_primeiro_dono(email, senha, nome)
    except (auth.ErroAuth, db.ErroBanco) as e:
        return _volta("/entrar", erro=str(e))

    auth.abrir_sessao(request, perfil)
    await db.registrar_evento("acesso", "primeiro dono criado", usuario=perfil)
    return _volta("/contas", aviso="Acesso criado. Agora cadastre a primeira loja.")



# ===========================================================================
# recuperacao de senha
# ===========================================================================
@app.get("/recuperar", response_class=HTMLResponse)
async def recuperar_tela(request: Request):
    if auth.usuario_da_sessao(request):
        return RedirectResponse("/minha-conta", status_code=303)
    return templates.TemplateResponse(
        "recuperar.html", _base(request, _periodo(None, None), enviado=False)
    )


@app.post("/recuperar", response_class=HTMLResponse)
async def recuperar_envio(request: Request, email: str = Form(...)):
    erro = ""
    try:
        await auth.pedir_recuperacao(email)
    except auth.ErroAuth as e:
        erro = str(e)
    # A tela mostra a mesma mensagem com ou sem conta cadastrada: senao ela
    # viraria um jeito de descobrir quem tem acesso ao sistema.
    return templates.TemplateResponse(
        "recuperar.html",
        _base(request, _periodo(None, None), enviado=not erro, erro=erro, email=email),
    )


@app.get("/nova-senha", response_class=HTMLResponse)
async def nova_senha(request: Request):
    # O token vem no fragmento da URL (#access_token=...), que o navegador
    # nunca manda para o servidor. Por isso esta tela resolve tudo no cliente,
    # falando direto com o Supabase.
    return templates.TemplateResponse(
        "nova_senha.html",
        _base(
            request,
            _periodo(None, None),
            supabase_url=SUPABASE_URL,
            supabase_anon=SUPABASE_ANON_KEY,
        ),
    )


# ===========================================================================
# minha conta
# ===========================================================================
@app.get("/minha-conta", response_class=HTMLResponse)
async def minha_conta(request: Request):
    usuario = auth.usuario_da_sessao(request)
    erro_infra, perfil, lojas = "", None, []
    try:
        perfil = await servico_usuarios.buscar(usuario["id"])
        minhas = await servico_contas.ids_visiveis(usuario)
        todas = await servico_contas.listar()
        lojas = [c for c in todas if minhas is None or c["id"] in minhas]
    except db.ErroBanco as e:
        erro_infra = str(e)

    return templates.TemplateResponse(
        "minha_conta.html",
        _base(
            request,
            _periodo(None, None),
            perfil=perfil,
            lojas=lojas,
            ve_todas=auth.pode(usuario, auth.DONO),
            erro_infra=erro_infra,
        ),
    )


@app.post("/minha-conta/nome")
async def minha_conta_nome(request: Request, nome: str = Form(...)):
    usuario = auth.usuario_da_sessao(request)
    try:
        perfil = await servico_usuarios.alterar_nome(usuario["id"], nome)
    except (auth.ErroAuth, db.ErroBanco) as e:
        return _volta("/minha-conta", erro=str(e))
    auth.abrir_sessao(request, perfil)  # o nome aparece no menu, atualiza já
    return _volta("/minha-conta", aviso="Nome atualizado.")


@app.post("/minha-conta/senha")
async def minha_conta_senha(
    request: Request,
    senha_atual: str = Form(...),
    nova: str = Form(...),
    confirma: str = Form(...),
):
    usuario = auth.usuario_da_sessao(request)
    try:
        if nova != confirma:
            raise auth.ErroAuth("A confirmação não bate com a nova senha.")
        await auth.trocar_propria_senha(usuario["email"], senha_atual, nova)
    except auth.ErroAuth as e:
        return _volta("/minha-conta", erro=str(e))
    await db.registrar_evento(
        "acesso", f"{usuario['email']} trocou a própria senha", usuario=usuario
    )
    return _volta("/minha-conta", aviso="Senha trocada.")

# ===========================================================================
# painel
# ===========================================================================
@app.get("/", response_class=HTMLResponse)
async def painel(
    request: Request,
    inicio: str | None = None,
    fim: str | None = None,
    exemplo: int = 0,
    atualizar: int = 0,
):
    usuario = auth.usuario_da_sessao(request)
    periodo = _periodo(inicio, fim)
    dados, erro_infra = await _carregar(periodo, bool(exemplo), bool(atualizar), usuario)
    # o catalogo diz quais SKUs de plataforma sao o mesmo produto
    resumo = servico_consulta.resumir(dados["pedidos"], await _mapa_sku())

    contas_resumo = []
    if not exemplo and not erro_infra:
        try:
            contas_resumo = await servico_contas.resumo(usuario)
        except db.ErroBanco as e:
            erro_infra = str(e)

    return templates.TemplateResponse(
        "painel.html",
        _base(
            request,
            periodo,
            resumo=resumo,
            graficos=_graficos(resumo),
            dados=dados,
            contas=contas_resumo,
            exemplo=bool(exemplo),
            erro_infra=erro_infra,
            ultimos=dados["pedidos"][:10],
        ),
    )


# ===========================================================================
# pedidos
# ===========================================================================
@app.get("/pedidos", response_class=HTMLResponse)
async def pedidos(
    request: Request,
    inicio: str | None = None,
    fim: str | None = None,
    canal: str = "",
    grupo: str = "",
    envio: str = "",
    q: str = "",
    exemplo: int = 0,
    atualizar: int = 0,
):
    usuario = auth.usuario_da_sessao(request)
    periodo = _periodo(inicio, fim)
    # so esta tela pede o estado do envio: no Mercado Livre ele custa uma
    # chamada por pedido, e o painel nao precisa disso
    dados, erro_infra = await _carregar(
        periodo, bool(exemplo), bool(atualizar), usuario, com_envio=True
    )
    lista = servico_consulta.filtrar(dados["pedidos"], canal, grupo, q, envio)

    # Baixa automatica: os pedidos ja despachados que ainda nao estao no livro
    # saem do estoque agora. E idempotente - dar F5 nao desconta de novo - e
    # falhar aqui nunca pode tirar a tela de pedidos do ar.
    baixa, projecao = {}, {}
    if not exemplo:
        try:
            catalogo = await servico_estoque.mapa_sku()
            baixa = await servico_estoque.conciliar(
                dados["pedidos"], usuario, catalogo
            )
            # o que fica no limite se o pedido sair agora - vira aviso na hora
            # de imprimir a etiqueta, que e quando da para fazer algo a respeito
            projecao = await servico_estoque.projetar(lista, catalogo)
        except Exception:
            baixa, projecao = {}, {}

    return templates.TemplateResponse(
        "pedidos.html",
        _base(
            request,
            periodo,
            lista=lista,
            resumo=servico_consulta.resumir(lista),
            resumo_total=servico_consulta.resumir(dados["pedidos"]),
            contagem_envio=servico_consulta.contar_envios(dados["pedidos"]),
            dados=dados,
            filtro={"canal": canal, "grupo": grupo, "q": q, "envio": envio},
            baixa=baixa,
            projecao_estoque=projecao,
            exemplo=bool(exemplo),
            erro_infra=erro_infra,
        ),
    )



@app.post("/pedidos/etiquetas")
async def pedidos_etiquetas(request: Request):
    """Gera o PDF de etiquetas dos pedidos marcados na tela.

    A tela manda os numeros dos pedidos, nao os ids de envio: o servidor
    reconsulta o periodo e resolve os ids por conta propria. Assim ninguem
    imprime etiqueta de envio que nao esta no seu escopo so por mexer no
    formulario.
    """
    usuario = auth.usuario_da_sessao(request)
    form = await request.form()
    escolhidos = set(form.getlist("pedido_id"))
    periodo = _periodo(form.get("inicio"), form.get("fim"))
    volta = f"/pedidos?inicio={periodo['inicio']}&fim={periodo['fim']}"

    try:
        auth.exigir(usuario, auth.OPERADOR)
        if not escolhidos:
            raise ErroCanal("Selecione ao menos um pedido para imprimir.")

        dados, erro_infra = await _carregar(periodo, False, False, usuario, com_envio=True)
        if erro_infra:
            raise ErroCanal(erro_infra)

        alvos = [p for p in dados["pedidos"] if p.pedido_id in escolhidos]
        if not alvos:
            raise ErroCanal(
                "Não encontrei esses pedidos no período. Atualize a lista e tente de novo."
            )

        # etiqueta e por conta: cada loja tem o seu documento na plataforma
        contas_envolvidas = {(p.canal, p.conta) for p in alvos}
        if len(contas_envolvidas) > 1:
            nomes = ", ".join(sorted(c for _, c in contas_envolvidas))
            raise ErroCanal(
                f"Os pedidos escolhidos são de mais de uma loja ({nomes}). "
                "Filtre por canal e imprima uma loja por vez."
            )

        canal_nome, apelido = contas_envolvidas.pop()
        linha = next(
            (
                c
                for c in await servico_contas.listar()
                if c["canal"] == canal_nome and c["apelido"] == apelido
            ),
            None,
        )
        if not linha:
            raise ErroCanal("Conta não encontrada.")
        if not await servico_contas.pode_ver(usuario, linha["id"]):
            raise auth.SemPermissao("Esta loja não faz parte do seu acesso.")

        conta = await servico_contas.garantir_token(linha)
        arquivo = await pegar_canal(canal_nome).etiquetas(conta, alvos)
    except (auth.SemPermissao, ErroCanal, db.ErroBanco) as e:
        return _volta(volta, erro=str(e))

    await db.registrar_evento(
        "etiqueta",
        f"{arquivo.impressas} etiqueta(s) impressa(s)",
        linha["id"],
        canal_nome,
        usuario,
    )
    return Response(
        content=arquivo.conteudo,
        media_type=arquivo.tipo,
        headers={"Content-Disposition": f'attachment; filename="{arquivo.nome}"'},
    )


# ===========================================================================
# catalogo de produtos
# ===========================================================================
@app.get("/produtos", response_class=HTMLResponse)
async def produtos_lista(request: Request, q: str = ""):
    erro_infra, lista, fotos, vinculos, variacoes = "", [], {}, {}, {}
    try:
        lista = await servico_produtos.listar(q)
        fotos = await servico_produtos.contagem_fotos()
        vinculos = await servico_produtos.contagem_anuncios()
        variacoes = await servico_produtos.contagem_variacoes()
    except db.ErroBanco as e:
        erro_infra = str(e)

    return templates.TemplateResponse(
        "produtos.html",
        _base(
            request,
            _periodo(None, None),
            lista=lista,
            fotos=fotos,
            vinculos=vinculos,
            variacoes=variacoes,
            busca=q,
            erro_infra=erro_infra,
        ),
    )


@app.post("/produtos/remover-lote")
async def produtos_remover_lote(request: Request):
    """Apaga os produtos marcados na lista. Variacoes, fotos e vinculos vao junto."""
    usuario = auth.usuario_da_sessao(request)
    form = await request.form()
    volta = "/produtos"
    if form.get("q"):
        volta = f"/produtos?q={_url(form.get('q'))}"
    try:
        auth.exigir(usuario, auth.OPERADOR)
        r = await servico_produtos.remover_varios(form.getlist("produto_id"), usuario)
    except (auth.SemPermissao, servico_produtos.ErroProduto, db.ErroBanco) as e:
        return _volta(volta, erro=str(e))

    servico_consulta.limpar_cache()
    aviso = f"{r['apagados']} produto(s) removido(s)."
    if r["problemas"]:
        aviso += f" {len(r['problemas'])} não saíram: {r['problemas'][0]}"
    return _volta(volta, aviso=aviso)


@app.get("/produtos/importar", response_class=HTMLResponse)
async def produtos_importar_previa(request: Request, conta_id: str = ""):
    """Mostra o que a importacao faria, anuncio por anuncio. Nada e gravado."""
    usuario = auth.usuario_da_sessao(request)
    erro_infra, previa, contas = "", None, []
    try:
        auth.exigir(usuario, auth.OPERADOR)
        # so loja conectada: sem token nao ha o que ler na plataforma
        contas = [
            c for c in await servico_contas.resumo(usuario)
            if c.get("ativa") and c.get("access_token_cripto")
        ]
        if conta_id:
            previa = await servico_importacao.previa(conta_id, usuario)
    except auth.SemPermissao as e:
        return _volta("/produtos", erro=str(e))
    except (servico_importacao.ErroImportacao, ErroCanal, db.ErroBanco) as e:
        erro_infra = str(e)

    return templates.TemplateResponse(
        "importar.html",
        _base(
            request,
            _periodo(None, None),
            contas=contas,
            conta_id=conta_id,
            previa=previa,
            rotulo_situacao=servico_importacao.ROTULO_SITUACAO,
            explica_situacao=servico_importacao.EXPLICA_SITUACAO,
            cor_situacao=servico_importacao.COR_SITUACAO,
            ja_importado=servico_importacao.JA_IMPORTADO,
            erro_infra=erro_infra,
        ),
    )


@app.post("/produtos/importar/sincronizar")
async def produtos_sincronizar(request: Request, conta_id: str = Form(...)):
    """Completa o id da variacao nos produtos ja importados. Nao cria nada."""
    usuario = auth.usuario_da_sessao(request)
    volta = f"/produtos/importar?conta_id={conta_id}"
    try:
        auth.exigir(usuario, auth.OPERADOR)
        r = await servico_importacao.sincronizar(conta_id, usuario)
    except (auth.SemPermissao, servico_importacao.ErroImportacao, ErroCanal,
            db.ErroBanco) as e:
        return _volta(volta, erro=str(e))

    if not r["completadas"]:
        return _volta(volta, aviso="Nada a completar: a grade já está ligada à "
                                   "plataforma.")
    return _volta(
        volta,
        aviso=f"{r['completadas']} variação(ões) de {r['produtos']} produto(s) "
        "agora sabem qual par saiu em cada pedido. A baixa de estoque passa a "
        "funcionar para elas.",
    )


@app.post("/produtos/importar")
async def produtos_importar(request: Request):
    usuario = auth.usuario_da_sessao(request)
    form = await request.form()
    conta_id = form.get("conta_id") or ""
    volta = f"/produtos/importar?conta_id={conta_id}"
    try:
        auth.exigir(usuario, auth.OPERADOR)
        r = await servico_importacao.importar(
            conta_id, form.getlist("anuncio_id"), usuario
        )
    except (auth.SemPermissao, servico_importacao.ErroImportacao, ErroCanal,
            db.ErroBanco) as e:
        return _volta(volta, erro=str(e))

    servico_consulta.limpar_cache()
    partes = [f"{r['criados']} produto(s) criado(s)",
              f"{r['vinculados']} anúncio(s) vinculado(s)"]
    if r["variacoes"]:
        partes.append(f"{r['variacoes']} variação(ões)")
    if r["fotos"]:
        partes.append(f"{r['fotos']} foto(s)")
    aviso = ", ".join(partes) + "."
    if r["problemas"]:
        aviso += f" {len(r['problemas'])} anúncio(s) ficaram de fora: " + "; ".join(
            f"{p['titulo'][:40]} ({p['erro']})" for p in r["problemas"][:3]
        )
    return _volta("/produtos", aviso=aviso)


@app.get("/produtos/novo", response_class=HTMLResponse)
async def produto_novo(request: Request):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.OPERADOR)
    except auth.SemPermissao as e:
        return _volta("/produtos", erro=str(e))
    return templates.TemplateResponse(
        "produto_detalhe.html",
        _base(
            request,
            _periodo(None, None),
            produto=None,
            fotos=[],
            vinculos=[],
            contas=[],
            grade=[],
            resumo_grade=servico_produtos.resumo_grade([]),
            ficha_ml=None,
            sugestoes=[],
            erro_ml="",
        ),
    )


@app.post("/produtos/novo")
async def produto_criar(request: Request):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.OPERADOR)
        form = dict(await request.form())
        form["ativo"] = form.get("ativo") == "1"
        form["medidas_por_variacao"] = form.get("medidas_por_variacao") == "1"
        bruto = await request.form()
        form["atributos"] = servico_produtos.montar_atributos(
            bruto.getlist("atributo_chave"), bruto.getlist("atributo_valor")
        )
        # campos vindos da categoria: chaveados pelo ID do atributo na
        # plataforma, nao pelo rotulo que a tela mostra
        if bruto.get("ml_campos_na_tela"):
            form["atributos_ml"] = servico_categorias.montar_valores(
                bruto.getlist("ml_atributo_id"),
                bruto.getlist("ml_atributo_valor"),
                bruto.getlist("ml_atributo_unidade"),
            )
        novo = await servico_produtos.criar(form, usuario)
    except (auth.SemPermissao, servico_produtos.ErroProduto, db.ErroBanco) as e:
        return _volta("/produtos/novo", erro=str(e))
    return _volta(
        f"/produtos/{novo['id']}", aviso="Produto cadastrado. Agora adicione as fotos."
    )


@app.get("/produtos/{produto_id}", response_class=HTMLResponse)
async def produto_detalhe(request: Request, produto_id: str, sugerir: int = 0):
    usuario = auth.usuario_da_sessao(request)
    erro_infra, produto, fotos, vinculos, contas, grade = "", None, [], [], [], []
    try:
        produto = await servico_produtos.buscar(produto_id)
        if produto:
            fotos = await servico_produtos.fotos(produto_id)
            vinculos = await servico_produtos.anuncios(produto_id)
            contas = await servico_contas.resumo(usuario)
            grade = await servico_produtos.variacoes(produto_id)
    except db.ErroBanco as e:
        erro_infra = str(e)

    if not produto and not erro_infra:
        return _volta("/produtos", erro="Produto não encontrado.")

    # Campos que o anúncio pede. Vem da plataforma, nunca do nosso banco, e
    # a falha nunca derruba a ficha: sem categoria escolhida ou sem conta
    # conectada, a aba Especificação apenas explica o que fazer.
    ficha_ml, sugestoes, erro_ml = None, [], ""
    if produto:
        try:
            if sugerir:
                sugestoes = await servico_categorias.sugerir(
                    produto.get("titulo") or "", usuario
                )
            if produto.get("ml_categoria_id"):
                ficha_ml = await servico_categorias.ficha(
                    produto["ml_categoria_id"], usuario
                )
        except (servico_categorias.SemCanal, ErroCanal, db.ErroBanco) as e:
            erro_ml = str(e)

    return templates.TemplateResponse(
        "produto_detalhe.html",
        _base(
            request,
            _periodo(None, None),
            produto=produto,
            fotos=fotos,
            vinculos=vinculos,
            contas=contas,
            contas_por_id={c["id"]: c for c in contas},
            grade=grade,
            resumo_grade=servico_produtos.resumo_grade(grade),
            ficha_ml=ficha_ml,
            sugestoes=sugestoes,
            erro_ml=erro_ml,
            faltando_ml=servico_categorias.faltando(
                ficha_ml or {}, (produto or {}).get("atributos_ml") or {}
            ),
            erro_infra=erro_infra,
        ),
    )


@app.post("/produtos/{produto_id}/categoria")
async def produto_categoria(
    request: Request,
    produto_id: str,
    categoria_id: str = Form(...),
    categoria_nome: str = Form(""),
):
    """Fixa a categoria escolhida. Dela sai a lista de campos obrigatórios."""
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.OPERADOR)
        await servico_produtos.definir_categoria(
            produto_id, categoria_id, categoria_nome, usuario
        )
    except (auth.SemPermissao, servico_produtos.ErroProduto, db.ErroBanco) as e:
        return _volta(f"/produtos/{produto_id}", erro=str(e))
    return _volta(
        f"/produtos/{produto_id}",
        aviso=f"Categoria definida: {categoria_nome or categoria_id}. "
        "Os campos dela estão na aba Especificação.",
    )


@app.post("/produtos/{produto_id}")
async def produto_salvar(request: Request, produto_id: str):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.OPERADOR)
        form = dict(await request.form())
        form["ativo"] = form.get("ativo") == "1"
        form["medidas_por_variacao"] = form.get("medidas_por_variacao") == "1"
        bruto = await request.form()
        form["atributos"] = servico_produtos.montar_atributos(
            bruto.getlist("atributo_chave"), bruto.getlist("atributo_valor")
        )
        # campos vindos da categoria: chaveados pelo ID do atributo na
        # plataforma, nao pelo rotulo que a tela mostra
        if bruto.get("ml_campos_na_tela"):
            form["atributos_ml"] = servico_categorias.montar_valores(
                bruto.getlist("ml_atributo_id"),
                bruto.getlist("ml_atributo_valor"),
                bruto.getlist("ml_atributo_unidade"),
            )
        await servico_produtos.atualizar(produto_id, form, usuario)
    except (auth.SemPermissao, servico_produtos.ErroProduto, db.ErroBanco) as e:
        return _volta(f"/produtos/{produto_id}", erro=str(e))
    return _volta(f"/produtos/{produto_id}", aviso="Produto atualizado.")


@app.post("/produtos/{produto_id}/variacoes/gerar")
async def produto_gerar_variacoes(
    request: Request,
    produto_id: str,
    opcoes1: str = Form(""),
    opcoes2: str = Form(""),
):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.OPERADOR)
        criadas = await servico_produtos.gerar_variacoes(
            produto_id, opcoes1, opcoes2, usuario
        )
    except (auth.SemPermissao, servico_produtos.ErroProduto, db.ErroBanco) as e:
        return _volta(f"/produtos/{produto_id}", erro=str(e))
    aviso = (
        f"{criadas} variação(ões) criadas."
        if criadas
        else "Nenhuma combinação nova — as que você listou já existem."
    )
    return _volta(f"/produtos/{produto_id}", aviso=aviso)


@app.post("/produtos/{produto_id}/variacoes")
async def produto_salvar_variacoes(request: Request, produto_id: str):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.OPERADOR)
        form = await request.form()
        ids = form.getlist("variacao_id")
        ativas = set(form.getlist("variacao_ativa"))
        linhas = [
            {
                "id": vid,
                "sku": form.getlist("variacao_sku")[i],
                "preco": form.getlist("variacao_preco")[i],
                "preco_promo": form.getlist("variacao_promo")[i],
                "estoque": form.getlist("variacao_estoque")[i],
                "gtin": form.getlist("variacao_gtin")[i],
                "ativa": vid in ativas,
            }
            for i, vid in enumerate(ids)
        ]
        await servico_produtos.salvar_variacoes(produto_id, linhas, usuario)
    except (auth.SemPermissao, servico_produtos.ErroProduto, db.ErroBanco, IndexError) as e:
        return _volta(f"/produtos/{produto_id}", erro=str(e) or "Formulário incompleto.")
    servico_consulta.limpar_cache()
    return _volta(f"/produtos/{produto_id}", aviso="Grade salva.")


@app.post("/produtos/{produto_id}/variacoes/aplicar")
async def produto_aplicar_variacoes(
    request: Request, produto_id: str, preco: str = Form(""), estoque: str = Form("")
):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.OPERADOR)
        n = await servico_produtos.aplicar_a_todas(produto_id, preco, estoque, usuario)
    except (auth.SemPermissao, servico_produtos.ErroProduto, db.ErroBanco) as e:
        return _volta(f"/produtos/{produto_id}", erro=str(e))
    return _volta(f"/produtos/{produto_id}", aviso=f"Aplicado a {n} variação(ões).")


@app.post("/produtos/{produto_id}/variacoes/{variacao_id}/remover")
async def produto_remover_variacao(request: Request, produto_id: str, variacao_id: str):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.OPERADOR)
        await servico_produtos.remover_variacao(variacao_id)
    except (auth.SemPermissao, db.ErroBanco) as e:
        return _volta(f"/produtos/{produto_id}", erro=str(e))
    servico_consulta.limpar_cache()
    return _volta(f"/produtos/{produto_id}", aviso="Variação removida.")

@app.post("/produtos/{produto_id}/foto")
async def produto_foto(request: Request, produto_id: str):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.OPERADOR)
        form = await request.form()
        arquivo = form.get("foto")
        if arquivo is None or not getattr(arquivo, "filename", ""):
            raise servico_produtos.ErroProduto("Escolha um arquivo de imagem.")
        conteudo = await arquivo.read()
        await servico_produtos.adicionar_foto(produto_id, arquivo.filename, conteudo)
    except (auth.SemPermissao, servico_produtos.ErroProduto, db.ErroBanco) as e:
        return _volta(f"/produtos/{produto_id}", erro=str(e))
    return _volta(f"/produtos/{produto_id}", aviso="Foto adicionada.")


@app.post("/produtos/{produto_id}/foto/{foto_id}/remover")
async def produto_foto_remover(request: Request, produto_id: str, foto_id: str):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.OPERADOR)
        await servico_produtos.remover_foto(foto_id)
    except (auth.SemPermissao, db.ErroBanco) as e:
        return _volta(f"/produtos/{produto_id}", erro=str(e))
    return _volta(f"/produtos/{produto_id}", aviso="Foto removida.")


@app.post("/produtos/{produto_id}/vincular")
async def produto_vincular(
    request: Request,
    produto_id: str,
    conta_id: str = Form(...),
    anuncio_id: str = Form(...),
    sku_canal: str = Form(""),
):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.OPERADOR)
        if not await servico_contas.pode_ver(usuario, conta_id):
            raise auth.SemPermissao("Esta loja não faz parte do seu acesso.")
        await servico_produtos.vincular(
            produto_id, conta_id, anuncio_id, sku_canal, usuario
        )
    except (auth.SemPermissao, servico_produtos.ErroProduto, db.ErroBanco) as e:
        return _volta(f"/produtos/{produto_id}", erro=str(e))
    servico_consulta.limpar_cache()
    return _volta(f"/produtos/{produto_id}", aviso="Anúncio vinculado.")


@app.post("/produtos/{produto_id}/vinculo/{vinculo_id}/remover")
async def produto_desvincular(request: Request, produto_id: str, vinculo_id: str):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.OPERADOR)
        await servico_produtos.desvincular(vinculo_id)
    except (auth.SemPermissao, db.ErroBanco) as e:
        return _volta(f"/produtos/{produto_id}", erro=str(e))
    servico_consulta.limpar_cache()
    return _volta(f"/produtos/{produto_id}", aviso="Vínculo removido.")


@app.post("/produtos/{produto_id}/remover")
async def produto_remover(request: Request, produto_id: str):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.OPERADOR)
        await servico_produtos.remover(produto_id, usuario)
    except (auth.SemPermissao, db.ErroBanco) as e:
        return _volta(f"/produtos/{produto_id}", erro=str(e))
    servico_consulta.limpar_cache()
    return _volta("/produtos", aviso="Produto removido.")

# ===========================================================================
# estoque
# ===========================================================================
@app.get("/estoque", response_class=HTMLResponse)
async def estoque_tela(request: Request, produto: str = "", q: str = ""):
    erro_infra, linhas, movimentos = "", [], []
    try:
        linhas = await servico_estoque.posicao()
        movimentos = await servico_estoque.historico(produto)
    except db.ErroBanco as e:
        erro_infra = str(e)

    if q:
        alvo = q.strip().lower()
        linhas = [
            l for l in linhas
            if alvo in l["sku"].lower() or alvo in l["produto"].lower()
        ]

    # o livro mostra SKU e titulo, nao uuid
    nomes = {l["produto_id"]: l["produto"] for l in linhas}
    for m in movimentos:
        m["produto_nome"] = nomes.get(m.get("produto_id"), m.get("sku"))

    return templates.TemplateResponse(
        "estoque.html",
        _base(
            request,
            _periodo(None, None),
            linhas=linhas,
            resumo_estoque=servico_estoque.resumo(linhas),
            alertas_estoque=servico_estoque.alertas(linhas),
            movimentos=movimentos,
            busca=q,
            rotulo_motivo=servico_estoque.ROTULO_MOTIVO,
            rotulo_situacao=servico_estoque.ROTULO_SITUACAO,
            explica_situacao=servico_estoque.EXPLICA_SITUACAO,
            cor_situacao=servico_estoque.COR_SITUACAO,
            erro_infra=erro_infra,
        ),
    )


@app.post("/estoque/movimento")
async def estoque_movimento(
    request: Request,
    produto_id: str = Form(...),
    variacao_id: str = Form(""),
    quantidade: str = Form(...),
    motivo: str = Form(servico_estoque.ENTRADA),
    observacao: str = Form(""),
):
    """Entrada, devolucao ou ajuste. Tudo passa por aqui e tudo vira livro."""
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.OPERADOR)
        try:
            delta = int(str(quantidade).strip())
        except ValueError:
            raise servico_estoque.ErroEstoque("A quantidade precisa ser um número.")
        if motivo not in servico_estoque.ROTULO_MOTIVO:
            raise servico_estoque.ErroEstoque("Motivo inválido.")
        if motivo == servico_estoque.ENVIO:
            raise servico_estoque.ErroEstoque(
                "A baixa por envio é automática: ela sai dos pedidos despachados, "
                "não da mão."
            )
        # entrada e devolucao sempre somam; ajuste aceita os dois sinais
        if motivo in (servico_estoque.ENTRADA, servico_estoque.DEVOLUCAO):
            delta = abs(delta)

        linha = await servico_estoque.movimentar(
            produto_id, variacao_id or None, delta, motivo, usuario, observacao
        )
    except (auth.SemPermissao, servico_estoque.ErroEstoque, db.ErroBanco) as e:
        return _volta("/estoque", erro=str(e))
    return _volta(
        "/estoque",
        aviso=f"{linha['sku']}: {delta:+d}. Ficou com {linha['estoque']}.",
    )


# ===========================================================================
# contas
# ===========================================================================
@app.get("/contas", response_class=HTMLResponse)
async def contas_lista(request: Request):
    usuario = auth.usuario_da_sessao(request)
    erro_infra, lista, pessoas, acessos = "", [], [], {}
    try:
        lista = await servico_contas.resumo(usuario)
        if auth.pode(usuario, auth.DONO):
            # so o dono monta a lista de quem enxerga cada loja
            pessoas = [
                p for p in await servico_usuarios.listar() if p["papel"] != auth.DONO
            ]
            acessos = await servico_contas.acessos_por_conta()
    except db.ErroBanco as e:
        erro_infra = str(e)
    return templates.TemplateResponse(
        "contas.html",
        _base(
            request,
            _periodo(None, None),
            lista=lista,
            pessoas=pessoas,
            acessos=acessos,
            erro_infra=erro_infra,
            redirects={n: redirect_uri(n) for n in CANAIS},
        ),
    )


@app.get("/contas/nova", response_class=HTMLResponse)
async def conta_nova(request: Request, canal: str = "mercadolivre"):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.DONO)
    except auth.SemPermissao as e:
        return _volta("/contas", erro=str(e))

    if canal not in CANAIS:
        canal = "mercadolivre"
    return templates.TemplateResponse(
        "conta_nova.html",
        _base(
            request,
            _periodo(None, None),
            escolhido=canal,
            c=CANAIS[canal],
            redirect=redirect_uri(canal),
        ),
    )


@app.post("/contas/nova")
async def conta_criar(
    request: Request,
    canal: str = Form(...),
    apelido: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.DONO)
    except auth.SemPermissao as e:
        return _volta("/contas", erro=str(e))

    form = await request.form()
    if canal not in CANAIS:
        return _volta("/contas", erro="Canal inválido.")
    if not apelido.strip() or not client_id.strip() or not client_secret.strip():
        return _volta(f"/contas/nova?canal={canal}", erro="Preencha todos os campos.")

    extra = {}
    for chave in CANAIS[canal].campos_extra:
        valor = (form.get(chave) or "").strip()
        if valor:
            extra[chave] = valor

    try:
        nova = await servico_contas.criar(
            canal, apelido, client_id, client_secret, extra, usuario
        )
    except Exception as e:
        return _volta("/contas", erro=str(e))

    return _volta(
        f"/contas#conta-{nova.get('id', '')}",
        aviso="Conta cadastrada. Agora clique em Conectar loja.",
    )


@app.get("/contas/{conta_id}", response_class=HTMLResponse)
async def conta_detalhe(request: Request, conta_id: str):
    usuario = auth.usuario_da_sessao(request)
    erro_infra, linha, info, eventos, erro_info = "", None, None, [], ""
    try:
        if not await servico_contas.pode_ver(usuario, conta_id):
            return _volta("/contas", erro="Esta loja não faz parte do seu acesso.")
        linha = await servico_contas.buscar(conta_id)
        if linha:
            eventos = await servico_contas.eventos(conta_id, 15)
    except db.ErroBanco as e:
        erro_infra = str(e)

    if not linha and not erro_infra:
        return _volta("/contas", erro="Conta não encontrada.")

    if linha and linha.get("access_token_cripto"):
        try:
            conta = await servico_contas.garantir_token(linha)
            info = await pegar_canal(linha["canal"]).info_conta(conta)
            linha = await servico_contas.buscar(conta_id)
        except Exception as e:
            erro_info = str(e)

    detalhado = None
    if linha and not erro_infra:
        todas = await servico_contas.resumo(usuario)
        detalhado = next((c for c in todas if c["id"] == conta_id), None)

    return templates.TemplateResponse(
        "conta_detalhe.html",
        _base(
            request,
            _periodo(None, None),
            conta=detalhado or linha,
            info=info,
            erro_info=erro_info,
            eventos=eventos,
            erro_infra=erro_infra,
            redirect=redirect_uri(linha["canal"]) if linha else "",
            c=CANAIS.get(linha["canal"]) if linha else None,
        ),
    )


@app.get("/contas/{conta_id}/conectar")
async def conta_conectar(request: Request, conta_id: str):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.OPERADOR)
        if not await servico_contas.pode_ver(usuario, conta_id):
            raise auth.SemPermissao("Esta loja não faz parte do seu acesso.")
        url = await servico_contas.iniciar_autorizacao(conta_id)
    except (auth.SemPermissao, ErroCanal, db.ErroBanco) as e:
        return _volta("/contas", erro=str(e))
    return RedirectResponse(url, status_code=303)


@app.post("/contas/{conta_id}/renovar")
async def conta_renovar(request: Request, conta_id: str):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.OPERADOR)
        if not await servico_contas.pode_ver(usuario, conta_id):
            raise auth.SemPermissao("Esta loja não faz parte do seu acesso.")
        msg = await servico_contas.renovar_manual(conta_id, usuario)
    except (auth.SemPermissao, ErroCanal, db.ErroBanco) as e:
        return _volta(f"/contas/{conta_id}", erro=str(e))
    servico_consulta.limpar_cache()
    return _volta(f"/contas/{conta_id}", aviso=msg)


@app.post("/contas/{conta_id}/remover")
async def conta_remover(request: Request, conta_id: str):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.DONO)
        await servico_contas.remover(conta_id, usuario)
    except (auth.SemPermissao, db.ErroBanco) as e:
        return _volta("/contas", erro=str(e))
    servico_consulta.limpar_cache()
    return _volta("/contas", aviso="Conta removida.")



@app.post("/contas/{conta_id}/acessos")
async def conta_acessos(request: Request, conta_id: str):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.DONO)
        form = await request.form()
        await servico_contas.definir_acessos(
            conta_id, form.getlist("usuario_id"), usuario
        )
    except (auth.SemPermissao, db.ErroBanco) as e:
        return _volta("/contas", erro=str(e))
    servico_consulta.limpar_cache()
    return _volta(f"/contas#conta-{conta_id}", aviso="Acesso à loja atualizado.")

# ===========================================================================
# volta da autorizacao
# ===========================================================================
@app.get("/retorno/{canal}")
async def retorno(request: Request, canal: str):
    p = dict(request.query_params)
    if canal not in CANAIS:
        return _volta("/contas", erro="Canal inválido.")

    if p.get("error") or p.get("error_description"):
        motivo = p.get("error_description") or p.get("error") or "autorização negada"
        return _volta("/contas", erro=motivo)

    # cada plataforma chama o codigo de um jeito
    codigo = p.get("code") or p.get("auth_code") or ""
    state = p.get("state") or ""
    if not codigo:
        return _volta("/contas", erro="A plataforma voltou sem código de autorização.")

    try:
        linha = await servico_contas.concluir_autorizacao(
            canal, state, codigo, p, auth.usuario_da_sessao(request)
        )
    except (ErroCanal, db.ErroBanco) as e:
        return _volta("/contas", erro=str(e))

    servico_consulta.limpar_cache()
    nome = linha.get("loja_nome") or linha.get("apelido", "")
    return _volta(f"/contas/{linha['id']}", aviso=f"Loja conectada: {nome}")


# ===========================================================================
# usuarios
# ===========================================================================
@app.get("/usuarios", response_class=HTMLResponse)
async def usuarios_lista(request: Request):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.DONO)
    except auth.SemPermissao as e:
        return _volta("/", erro=str(e))

    erro_infra, lista, contas, acessos = "", [], [], {}
    try:
        lista = await servico_usuarios.listar()
        contas = await servico_contas.listar()
        acessos = await servico_contas.acessos_por_usuario()
    except db.ErroBanco as e:
        erro_infra = str(e)

    return templates.TemplateResponse(
        "usuarios.html",
        _base(
            request,
            _periodo(None, None),
            lista=lista,
            contas={c["id"]: c for c in contas},
            acessos=acessos,
            erro_infra=erro_infra,
            papeis=auth.FORCA,
            explica_papel=auth.EXPLICA_PAPEL,
        ),
    )


@app.post("/usuarios/novo")
async def usuario_novo(
    request: Request,
    email: str = Form(...),
    senha: str = Form(...),
    nome: str = Form(""),
    papel: str = Form(auth.LEITOR),
):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.DONO)
        if len(senha) < 8:
            raise auth.ErroAuth("A senha precisa ter ao menos 8 caracteres.")
        novo = await servico_usuarios.criar(email, senha, nome, papel)
    except (auth.SemPermissao, auth.ErroAuth, db.ErroBanco) as e:
        return _volta("/usuarios", erro=str(e))

    await db.registrar_evento(
        "acesso", f"{usuario['email']} criou o acesso de {novo['email']}", usuario=usuario
    )
    return _volta(
        "/usuarios",
        aviso=f"Acesso criado para {novo['email']}. Passe a senha para a pessoa "
              "e peça que troque assim que entrar.",
    )


@app.post("/usuarios/{usuario_id}/papel")
async def usuario_papel(request: Request, usuario_id: str, papel: str = Form(...)):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.DONO)
        await servico_usuarios.alterar_papel(usuario_id, papel, usuario)
    except (auth.SemPermissao, auth.ErroAuth, db.ErroBanco) as e:
        return _volta("/usuarios", erro=str(e))
    return _volta("/usuarios", aviso="Papel atualizado.")


@app.post("/usuarios/{usuario_id}/ativo")
async def usuario_ativo(request: Request, usuario_id: str, ativo: str = Form("")):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.DONO)
        await servico_usuarios.alterar_ativo(usuario_id, ativo == "1", usuario)
    except (auth.SemPermissao, auth.ErroAuth, db.ErroBanco) as e:
        return _volta("/usuarios", erro=str(e))
    return _volta("/usuarios", aviso="Acesso atualizado.")


@app.post("/usuarios/{usuario_id}/senha")
async def usuario_senha(request: Request, usuario_id: str, senha: str = Form(...)):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.DONO)
        if len(senha) < 8:
            raise auth.ErroAuth("A senha precisa ter ao menos 8 caracteres.")
        await auth.trocar_senha(usuario_id, senha)
    except (auth.SemPermissao, auth.ErroAuth) as e:
        return _volta("/usuarios", erro=str(e))
    return _volta("/usuarios", aviso="Senha trocada.")


@app.post("/usuarios/{usuario_id}/remover")
async def usuario_remover(request: Request, usuario_id: str):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.DONO)
        await servico_usuarios.remover(usuario_id, usuario)
    except (auth.SemPermissao, auth.ErroAuth, db.ErroBanco) as e:
        return _volta("/usuarios", erro=str(e))
    return _volta("/usuarios", aviso="Acesso removido.")


# ===========================================================================
# ajuda e diagnostico
# ===========================================================================
@app.get("/ajuda", response_class=HTMLResponse)
async def ajuda(request: Request, canal: str = ""):
    try:
        auth.exigir(auth.usuario_da_sessao(request), auth.DONO)
    except auth.SemPermissao as e:
        return _volta("/", erro=str(e))
    return templates.TemplateResponse(
        "ajuda.html",
        _base(
            request,
            _periodo(None, None),
            aberto=canal if canal in CANAIS else "",
            redirects={n: redirect_uri(n) for n in CANAIS},
            url_recuperacao=auth.url_nova_senha(),
        ),
    )


@app.get("/saude", response_class=HTMLResponse)
async def saude(request: Request):
    # Nao esta em ROTAS_ABERTAS: exige sessao. Quem nao e dono nao entra.
    # Um deploy quebrado continua diagnosticavel pela propria tela de login,
    # que mostra a faixa de configuracao incompleta antes de qualquer acesso.
    try:
        auth.exigir(auth.usuario_da_sessao(request), auth.DONO)
    except auth.SemPermissao as e:
        return _volta("/", erro=str(e))
    banco_ok, banco_msg = await db.testar_conexao()
    return templates.TemplateResponse(
        "saude.html",
        _base(
            request,
            _periodo(None, None),
            itens=diagnostico(),
            banco_ok=banco_ok,
            banco_msg=banco_msg,
            chave=origem_da_chave(),
            chave_ok=chave_ok(),
            chave_erro=erro_da_chave(),
            fuso=NOME_FUSO,
            producao=PRODUCAO,
            redirects={n: redirect_uri(n) for n in CANAIS},
            url_recuperacao=auth.url_nova_senha(),
        ),
    )
