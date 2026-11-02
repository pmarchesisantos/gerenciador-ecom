"""Login e controle de acesso, apoiado no Supabase Auth.

Divisao de responsabilidade:

  Supabase Auth  guarda e-mail e senha (com hash), e valida o login.
  tabela perfis  guarda quem e quem aqui dentro: nome e papel.
  cookie assinado  mantem a sessao entre requisicoes.

Nao guardamos o token do Supabase no cookie. Depois que o Supabase confirma
a senha, emitimos a nossa propria sessao (cookie assinado pelo Starlette),
com o id, o e-mail e o papel. Assim cada requisicao resolve o acesso sem uma
ida extra a rede - o que importa em serverless, onde cada ida conta.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from fastapi import Request
from fastapi.responses import RedirectResponse

from app.config import SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY, SUPABASE_URL

# ---------------------------------------------------------------------------
# papeis
# ---------------------------------------------------------------------------
DONO = "dono"
OPERADOR = "operador"
LEITOR = "leitor"

# quanto maior o numero, mais pode
FORCA = {LEITOR: 1, OPERADOR: 2, DONO: 3}

ROTULO_PAPEL = {
    DONO: "Dono",
    OPERADOR: "Operador",
    LEITOR: "Leitor",
}

EXPLICA_PAPEL = {
    DONO: "Vê tudo, conecta e remove lojas, e gerencia quem tem acesso.",
    OPERADOR: "Vê tudo e pode reautorizar ou renovar o token de uma loja, "
              "mas não cadastra nem remove credenciais.",
    LEITOR: "Só consulta o painel e os pedidos. Não altera nada.",
}


class ErroAuth(RuntimeError):
    pass


class SemPermissao(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Supabase Auth (REST)
# ---------------------------------------------------------------------------
def _url(caminho: str) -> str:
    return f"{SUPABASE_URL}/auth/v1{caminho}"


async def entrar(email: str, senha: str) -> dict:
    """Valida e-mail e senha no Supabase. Devolve os dados do usuario."""
    if not SUPABASE_ANON_KEY:
        raise ErroAuth(
            "SUPABASE_ANON_KEY não configurada: sem ela não dá para validar o login."
        )
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            _url("/token"),
            params={"grant_type": "password"},
            headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
            json={"email": email.strip().lower(), "password": senha},
        )
    if r.status_code == 400:
        raise ErroAuth("E-mail ou senha incorretos.")
    if r.status_code >= 400:
        raise ErroAuth(f"Supabase recusou o login ({r.status_code}): {r.text[:200]}")
    j = r.json()
    usuario = j.get("user") or {}
    if not usuario.get("id"):
        raise ErroAuth("Resposta de login sem usuário.")
    return usuario


async def criar_usuario(email: str, senha: str) -> dict:
    """Cria o usuario ja confirmado, usando a chave service_role.

    Usamos a API de admin de proposito: assim o dono cadastra a equipe sem
    depender de configuracao de e-mail no Supabase, e ninguem se cadastra
    sozinho so por descobrir o endereco do app.
    """
    if not SUPABASE_SERVICE_KEY:
        raise ErroAuth("SUPABASE_SERVICE_KEY não configurada.")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            _url("/admin/users"),
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "email": email.strip().lower(),
                "password": senha,
                "email_confirm": True,
            },
        )
    if r.status_code == 422 or (r.status_code >= 400 and "already" in r.text.lower()):
        raise ErroAuth("Já existe um usuário com esse e-mail.")
    if r.status_code >= 400:
        raise ErroAuth(f"Não consegui criar o usuário ({r.status_code}): {r.text[:200]}")
    return r.json()


async def trocar_senha(usuario_id: str, senha: str) -> None:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.put(
            _url(f"/admin/users/{usuario_id}"),
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
            },
            json={"password": senha},
        )
    if r.status_code >= 400:
        raise ErroAuth(f"Não consegui trocar a senha ({r.status_code}): {r.text[:200]}")


async def apagar_usuario(usuario_id: str) -> None:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.delete(
            _url(f"/admin/users/{usuario_id}"),
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            },
        )
    if r.status_code >= 400 and r.status_code != 404:
        raise ErroAuth(f"Não consegui remover o usuário ({r.status_code}): {r.text[:200]}")


# ---------------------------------------------------------------------------
# sessao
# ---------------------------------------------------------------------------
def abrir_sessao(request: Request, perfil: dict) -> None:
    request.session["usuario"] = {
        "id": perfil["id"],
        "email": perfil["email"],
        "nome": perfil.get("nome") or perfil["email"].split("@")[0],
        "papel": perfil.get("papel", LEITOR),
        "desde": datetime.now(timezone.utc).isoformat(),
    }


def fechar_sessao(request: Request) -> None:
    request.session.clear()


def usuario_da_sessao(request: Request) -> dict | None:
    u = request.session.get("usuario")
    return u if isinstance(u, dict) and u.get("id") else None


# ---------------------------------------------------------------------------
# permissoes
# ---------------------------------------------------------------------------
def pode(usuario: dict | None, papel_minimo: str) -> bool:
    if not usuario:
        return False
    return FORCA.get(usuario.get("papel", LEITOR), 0) >= FORCA[papel_minimo]


def exigir(usuario: dict | None, papel_minimo: str) -> None:
    if not pode(usuario, papel_minimo):
        atual = ROTULO_PAPEL.get((usuario or {}).get("papel", LEITOR), "sem acesso")
        preciso = ROTULO_PAPEL[papel_minimo]
        raise SemPermissao(
            f"Esta ação exige o papel {preciso}, e seu acesso é {atual}. "
            "Peça a quem é dono do sistema."
        )


def para_login(destino: str = "/") -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(f"/entrar?proximo={quote(destino, safe='')}", status_code=303)
