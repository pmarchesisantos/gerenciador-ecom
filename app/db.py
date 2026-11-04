"""Acesso ao Supabase via API REST (PostgREST).

Nao usamos a biblioteca oficial de proposito: o PostgREST e so HTTP, e assim
fica visivel o que esta acontecendo e sobra uma dependencia a menos.

Regra de ouro: a chave service_role nunca sai do servidor.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import SUPABASE_SERVICE_KEY, SUPABASE_URL, supabase_configurado


class ErroBanco(RuntimeError):
    pass


def _cabecalhos(extra: dict[str, str] | None = None) -> dict[str, str]:
    if not supabase_configurado():
        raise ErroBanco(
            "Supabase nao configurado. Preencha SUPABASE_URL e "
            "SUPABASE_SERVICE_KEY no arquivo .env (veja .env.example)."
        )
    h = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _url(tabela: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{tabela}"


def _conferir(r: httpx.Response) -> Any:
    if r.status_code >= 400:
        corpo = r.text[:400]
        if "does not exist" in corpo or r.status_code == 404:
            raise ErroBanco(
                f"Tabela ausente no Supabase ({r.status_code}). "
                "Rode o conteudo de schema.sql no SQL Editor. Detalhe: " + corpo
            )
        raise ErroBanco(f"Supabase respondeu {r.status_code}: {corpo}")
    if not r.content:
        return []
    return r.json()


async def selecionar(
    tabela: str, filtros: dict[str, str] | None = None, ordem: str | None = None
) -> list[dict]:
    params: dict[str, str] = {"select": "*"}
    if filtros:
        params.update(filtros)
    if ordem:
        params["order"] = ordem
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(_url(tabela), headers=_cabecalhos(), params=params)
    return _conferir(r)


async def um(tabela: str, filtros: dict[str, str]) -> dict | None:
    linhas = await selecionar(tabela, filtros)
    return linhas[0] if linhas else None


async def inserir(tabela: str, dados: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            _url(tabela),
            headers=_cabecalhos({"Prefer": "return=representation"}),
            json=dados,
        )
    linhas = _conferir(r)
    return linhas[0] if linhas else {}


async def inserir_varios(
    tabela: str, linhas: list[dict], ignorar_repetidos: bool = False
) -> list[dict]:
    """Insere um lote em uma chamada so e devolve o que REALMENTE entrou.

    ignorar_repetidos usa o resolution=ignore-duplicates do PostgREST: linha
    que bate em indice unico e pulada em silencio, em vez de derrubar o lote.
    E o que deixa a baixa de estoque ser chamada quantas vezes for sem medo.

    Devolver as linhas gravadas, e nao uma contagem, importa: quem chama
    precisa saber exatamente quais entraram para nao descontar duas vezes o
    que o indice unico recusou.
    """
    if not linhas:
        return []
    prefer = ["return=representation"]
    if ignorar_repetidos:
        prefer.append("resolution=ignore-duplicates")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            _url(tabela),
            headers=_cabecalhos({"Prefer": ",".join(prefer)}),
            json=linhas,
        )
    return _conferir(r) or []


async def atualizar(tabela: str, filtros: dict[str, str], dados: dict) -> dict | None:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.patch(
            _url(tabela),
            headers=_cabecalhos({"Prefer": "return=representation"}),
            params=filtros,
            json=dados,
        )
    linhas = _conferir(r)
    return linhas[0] if linhas else None


async def apagar(tabela: str, filtros: dict[str, str]) -> None:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.delete(_url(tabela), headers=_cabecalhos(), params=filtros)
    _conferir(r)


async def registrar_evento(
    tipo: str,
    detalhe: str,
    conta_id: str | None = None,
    canal: str | None = None,
    usuario: dict | None = None,
) -> None:
    """Grava na trilha de eventos. Nunca derruba a requisicao principal."""
    try:
        await inserir(
            "eventos",
            {
                "conta_id": conta_id,
                "canal": canal,
                "tipo": tipo,
                "detalhe": detalhe[:1000],
                "usuario_id": (usuario or {}).get("id"),
                "usuario_email": (usuario or {}).get("email"),
            },
        )
    except Exception:
        pass


async def testar_conexao() -> tuple[bool, str]:
    """Usado pela pagina /saude."""
    if not supabase_configurado():
        return False, "SUPABASE_URL ou SUPABASE_SERVICE_KEY em branco no .env"
    try:
        await selecionar("contas", {"limit": "1"})
        return True, "conectado, tabelas encontradas"
    except ErroBanco as e:
        return False, str(e)
    except Exception as e:  # rede, DNS, etc.
        return False, f"nao consegui falar com o Supabase: {e}"
