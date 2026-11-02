"""Ciclo de vida das contas: cadastrar, autorizar, renovar token, remover.

Tudo que e segredo passa por app.cripto antes de ir para o Supabase e volta
descriptografado apenas em memoria, nas chaves que comecam com "_".
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from app import db
from app.canais import CANAIS, ErroCanal, canal
from app.cripto import mascarar, proteger, revelar
from app.modelos import CANAIS_ROTULO, Tokens

# margem antes de considerar o token vencido
MARGEM_SEGUNDOS = 120


# ---------------------------------------------------------------------------
# leitura
# ---------------------------------------------------------------------------
async def listar() -> list[dict]:
    return await db.selecionar("contas", ordem="canal.asc,apelido.asc")


async def buscar(conta_id: str) -> dict | None:
    return await db.um("contas", {"id": f"eq.{conta_id}"})


def preparar(linha: dict) -> dict:
    """Abre os segredos da linha do banco para uso pelos canais."""
    conta = dict(linha)
    conta["_secret"] = revelar(linha.get("client_secret_cripto"))
    conta["_access"] = revelar(linha.get("access_token_cripto"))
    conta["_refresh"] = revelar(linha.get("refresh_token_cripto"))
    conta["extra"] = linha.get("extra") or {}
    return conta


def _quando(texto: str | None) -> datetime | None:
    if not texto:
        return None
    try:
        return datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return None


def token_vencido(linha: dict) -> bool:
    prazo = _quando(linha.get("access_expira_em"))
    if not prazo:
        return True
    return prazo <= datetime.now(timezone.utc) + timedelta(seconds=MARGEM_SEGUNDOS)


def situacao(linha: dict) -> dict:
    """Resumo legivel do estado da conta, usado nas telas."""
    if not linha.get("access_token_cripto"):
        return {"codigo": "nao_conectada", "rotulo": "Nao conectada", "cor": "cinza",
                "detalhe": "Falta autorizar a loja."}
    if not revelar(linha.get("client_secret_cripto")):
        return {"codigo": "chave_perdida", "rotulo": "Chave de criptografia trocada",
                "cor": "vermelho",
                "detalhe": "Os segredos desta conta nao abrem mais. Cadastre de novo."}
    refresh_prazo = _quando(linha.get("refresh_expira_em"))
    if refresh_prazo and refresh_prazo <= datetime.now(timezone.utc):
        return {"codigo": "autorizacao_expirada", "rotulo": "Autorizacao expirada",
                "cor": "vermelho", "detalhe": "Reautorize a loja na plataforma."}
    if token_vencido(linha):
        return {"codigo": "token_vencido", "rotulo": "Token vencido", "cor": "amarelo",
                "detalhe": "Sera renovado sozinho na proxima consulta."}
    return {"codigo": "ativa", "rotulo": "Conectada", "cor": "verde",
            "detalhe": "Token valido."}


async def resumo() -> list[dict]:
    """Linhas enriquecidas para a tela de contas e para o painel."""
    saida = []
    for linha in await listar():
        c = CANAIS.get(linha["canal"])
        saida.append(
            {
                **linha,
                "canal_rotulo": CANAIS_ROTULO.get(linha["canal"], linha["canal"]),
                "situacao": situacao(linha),
                "secret_mascarado": mascarar(revelar(linha.get("client_secret_cripto"))),
                "rotulo_client_id": c.rotulo_client_id if c else "Client ID",
                "rotulo_client_secret": c.rotulo_client_secret if c else "Client Secret",
                "access_expira_em_dt": _quando(linha.get("access_expira_em")),
                "refresh_expira_em_dt": _quando(linha.get("refresh_expira_em")),
            }
        )
    return saida


# ---------------------------------------------------------------------------
# escrita
# ---------------------------------------------------------------------------
async def criar(
    canal_nome: str,
    apelido: str,
    client_id: str,
    client_secret: str,
    extra: dict,
    usuario: dict | None = None,
) -> dict:
    if canal_nome not in CANAIS:
        raise ErroCanal(f"Canal desconhecido: {canal_nome}")
    nova = await db.inserir(
        "contas",
        {
            "canal": canal_nome,
            "apelido": apelido.strip(),
            "client_id": client_id.strip(),
            "client_secret_cripto": proteger(client_secret.strip()),
            "extra": extra or {},
            "criada_por": (usuario or {}).get("id"),
        },
    )
    await db.registrar_evento(
        "cadastro", f"conta {apelido} cadastrada", nova.get("id"), canal_nome, usuario
    )
    return nova


async def remover(conta_id: str, usuario: dict | None = None) -> None:
    linha = await buscar(conta_id)
    await db.apagar("contas", {"id": f"eq.{conta_id}"})
    if linha:
        await db.registrar_evento(
            "remocao", f"conta {linha.get('apelido')} removida",
            None, linha.get("canal"), usuario,
        )


async def salvar_extra(conta_id: str, extra: dict) -> None:
    await db.atualizar("contas", {"id": f"eq.{conta_id}"}, {"extra": extra})


async def _gravar_tokens(conta_id: str, t: Tokens, extra_atual: dict) -> None:
    agora = datetime.now(timezone.utc)
    dados: dict = {
        "access_token_cripto": proteger(t.access_token),
        "access_expira_em": (
            (agora + timedelta(seconds=t.expira_em_segundos)).isoformat()
            if t.expira_em_segundos
            else None
        ),
    }
    if t.refresh_token:
        dados["refresh_token_cripto"] = proteger(t.refresh_token)
    if t.refresh_expira_em_segundos:
        dados["refresh_expira_em"] = (
            agora + timedelta(seconds=t.refresh_expira_em_segundos)
        ).isoformat()
    if t.extra:
        dados["extra"] = {**(extra_atual or {}), **{k: v for k, v in t.extra.items() if v}}
    await db.atualizar("contas", {"id": f"eq.{conta_id}"}, dados)


# ---------------------------------------------------------------------------
# autorizacao
# ---------------------------------------------------------------------------
async def iniciar_autorizacao(conta_id: str) -> str:
    linha = await buscar(conta_id)
    if not linha:
        raise ErroCanal("Conta nao encontrada.")
    conta = preparar(linha)
    c = canal(conta["canal"])

    state = secrets.token_urlsafe(24)
    url, verificador = c.url_autorizacao(conta, state)

    await db.inserir(
        "oauth_estados",
        {
            "state": state,
            "conta_id": conta_id,
            "canal": conta["canal"],
            "verificador": verificador,
        },
    )
    return url


async def concluir_autorizacao(
    canal_nome: str,
    state: str,
    codigo: str,
    extras_url: dict,
    usuario: dict | None = None,
) -> dict:
    """Chamada pela rota /retorno/{canal} quando a plataforma devolve o codigo."""
    pendente = await db.um("oauth_estados", {"state": f"eq.{state}"})
    if not pendente:
        raise ErroCanal(
            "Nao reconheci este pedido de autorizacao (state invalido ou ja usado). "
            "Comece de novo em Contas."
        )
    await db.apagar("oauth_estados", {"state": f"eq.{state}"})

    linha = await buscar(pendente["conta_id"])
    if not linha:
        raise ErroCanal("A conta foi removida durante a autorizacao.")

    conta = preparar(linha)
    c = canal(canal_nome)

    # A Shopee devolve o shop_id junto do codigo, na propria URL.
    if extras_url.get("shop_id"):
        conta["loja_id"] = extras_url["shop_id"]
        await db.atualizar(
            "contas", {"id": f"eq.{conta['id']}"}, {"loja_id": extras_url["shop_id"]}
        )

    tokens = await c.trocar_codigo(conta, codigo, pendente.get("verificador"))
    await _gravar_tokens(conta["id"], tokens, conta.get("extra") or {})

    # recarrega com os tokens novos e busca os dados da loja
    conta = preparar(await buscar(conta["id"]))
    try:
        extra_pos = await c.pos_autorizacao(conta)
        if extra_pos:
            novo_extra = {**(conta.get("extra") or {}), **extra_pos}
            await salvar_extra(conta["id"], novo_extra)
            conta["extra"] = novo_extra
        info = await c.info_conta(conta)
        await db.atualizar(
            "contas",
            {"id": f"eq.{conta['id']}"},
            {"loja_id": info.loja_id or conta.get("loja_id"), "loja_nome": info.loja_nome},
        )
    except Exception as e:
        await db.registrar_evento(
            "erro", f"autorizou mas falhou ao ler a loja: {e}", conta["id"], canal_nome
        )

    await db.registrar_evento(
        "autorizacao", "loja autorizada", conta["id"], canal_nome, usuario
    )
    return await buscar(conta["id"])


async def garantir_token(linha: dict) -> dict:
    """Devolve a conta pronta para uso, renovando o token se preciso."""
    conta = preparar(linha)
    if not conta.get("_access"):
        raise ErroCanal("Conta nao conectada. Clique em Conectar na tela de Contas.")
    if not token_vencido(linha):
        return conta

    c = canal(conta["canal"])
    try:
        tokens = await c.renovar(conta)
    except ErroCanal as e:
        await db.registrar_evento("erro", f"falha ao renovar: {e}", conta["id"], conta["canal"])
        raise
    await _gravar_tokens(conta["id"], tokens, conta.get("extra") or {})
    await db.registrar_evento("renovacao", "token renovado", conta["id"], conta["canal"])
    return preparar(await buscar(conta["id"]))


async def renovar_manual(conta_id: str, usuario: dict | None = None) -> str:
    linha = await buscar(conta_id)
    if not linha:
        raise ErroCanal("Conta nao encontrada.")
    conta = preparar(linha)
    c = canal(conta["canal"])
    tokens = await c.renovar(conta)
    await _gravar_tokens(conta_id, tokens, conta.get("extra") or {})
    await db.registrar_evento(
        "renovacao", "renovado manualmente", conta_id, conta["canal"], usuario
    )
    return "Token renovado."


async def eventos(conta_id: str, limite: int = 10) -> list[dict]:
    return await db.selecionar(
        "eventos",
        {"conta_id": f"eq.{conta_id}", "limit": str(limite)},
        ordem="criado_em.desc",
    )
