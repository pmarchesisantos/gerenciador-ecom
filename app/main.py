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
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app import auth, db
from app.canais import CANAIS, ErroCanal
from app.config import (
    CHAVE_SESSAO,
    FUSO,
    NOME_FUSO,
    PRODUCAO,
    SESSAO_SEGUNDOS,
    diagnostico,
    problemas_criticos,
    redirect_uri,
    supabase_configurado,
)
from app.cripto import chave_ok, erro_da_chave, origem_da_chave
from app.modelos import CANAIS_ROTULO, COR_GRUPO, EXPLICA_GRUPO, GRUPOS, ROTULO_GRUPO
from app.servicos import consulta as servico_consulta
from app.servicos import contas as servico_contas
from app.servicos import exemplo as servico_exemplo
from app.servicos import usuarios as servico_usuarios

PASTA = Path(__file__).resolve().parent

app = FastAPI(title="Sistema-Ecom", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=PASTA / "static"), name="static")
templates = Jinja2Templates(directory=str(PASTA / "templates"))

ROTAS_ABERTAS = {"/entrar", "/sair", "/primeiro-acesso", "/saude"}


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
        "supabase_ok": supabase_configurado(),
        "usuario": usuario,
        "pode": lambda papel: auth.pode(usuario, papel),
        "rotulo_papel": auth.ROTULO_PAPEL,
        "aviso": request.query_params.get("aviso"),
        "erro": request.query_params.get("erro"),
        "criticos": problemas_criticos(),
        **extra,
    }


async def _carregar(periodo: dict, exemplo: bool, forcar: bool) -> tuple[dict, str]:
    """Devolve (dados, erro de infraestrutura)."""
    if exemplo:
        return servico_exemplo.resultado(periodo["inicio"], periodo["fim"]), ""
    try:
        dados = await servico_consulta.consultar(
            periodo["inicio"], periodo["fim"], forcar=forcar
        )
        return dados, ""
    except db.ErroBanco as e:
        vazio = {
            "pedidos": [],
            "resultados": [],
            "erros": [],
            "houve_consulta": False,
            "atualizado_em": datetime.now(tz=FUSO),
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
    periodo = _periodo(inicio, fim)
    dados, erro_infra = await _carregar(periodo, bool(exemplo), bool(atualizar))
    resumo = servico_consulta.resumir(dados["pedidos"])

    contas_resumo = []
    if not exemplo and not erro_infra:
        try:
            contas_resumo = await servico_contas.resumo()
        except db.ErroBanco as e:
            erro_infra = str(e)

    return templates.TemplateResponse(
        "painel.html",
        _base(
            request,
            periodo,
            resumo=resumo,
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
    q: str = "",
    exemplo: int = 0,
    atualizar: int = 0,
):
    periodo = _periodo(inicio, fim)
    dados, erro_infra = await _carregar(periodo, bool(exemplo), bool(atualizar))
    lista = servico_consulta.filtrar(dados["pedidos"], canal, grupo, q)

    return templates.TemplateResponse(
        "pedidos.html",
        _base(
            request,
            periodo,
            lista=lista,
            resumo=servico_consulta.resumir(lista),
            resumo_total=servico_consulta.resumir(dados["pedidos"]),
            dados=dados,
            filtro={"canal": canal, "grupo": grupo, "q": q},
            exemplo=bool(exemplo),
            erro_infra=erro_infra,
        ),
    )


# ===========================================================================
# contas
# ===========================================================================
@app.get("/contas", response_class=HTMLResponse)
async def contas_lista(request: Request):
    erro_infra, lista = "", []
    try:
        lista = await servico_contas.resumo()
    except db.ErroBanco as e:
        erro_infra = str(e)
    return templates.TemplateResponse(
        "contas.html",
        _base(
            request,
            _periodo(None, None),
            lista=lista,
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
    erro_infra, linha, info, eventos, erro_info = "", None, None, [], ""
    try:
        linha = await servico_contas.buscar(conta_id)
        if linha:
            eventos = await servico_contas.eventos(conta_id, 15)
    except db.ErroBanco as e:
        erro_infra = str(e)

    if not linha and not erro_infra:
        return _volta("/contas", erro="Conta não encontrada.")

    if linha and linha.get("access_token_cripto"):
        from app.canais import canal as pegar_canal

        try:
            conta = await servico_contas.garantir_token(linha)
            info = await pegar_canal(linha["canal"]).info_conta(conta)
            linha = await servico_contas.buscar(conta_id)
        except Exception as e:
            erro_info = str(e)

    detalhado = None
    if linha and not erro_infra:
        todas = await servico_contas.resumo()
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
        url = await servico_contas.iniciar_autorizacao(conta_id)
    except (auth.SemPermissao, ErroCanal, db.ErroBanco) as e:
        return _volta("/contas", erro=str(e))
    return RedirectResponse(url, status_code=303)


@app.post("/contas/{conta_id}/renovar")
async def conta_renovar(request: Request, conta_id: str):
    usuario = auth.usuario_da_sessao(request)
    try:
        auth.exigir(usuario, auth.OPERADOR)
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

    erro_infra, lista = "", []
    try:
        lista = await servico_usuarios.listar()
    except db.ErroBanco as e:
        erro_infra = str(e)

    return templates.TemplateResponse(
        "usuarios.html",
        _base(
            request,
            _periodo(None, None),
            lista=lista,
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
    return templates.TemplateResponse(
        "ajuda.html",
        _base(
            request,
            _periodo(None, None),
            aberto=canal if canal in CANAIS else "",
            redirects={n: redirect_uri(n) for n in CANAIS},
        ),
    )


@app.get("/saude", response_class=HTMLResponse)
async def saude(request: Request):
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
        ),
    )
