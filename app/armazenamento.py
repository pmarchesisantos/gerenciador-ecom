"""Fotos de produto no Supabase Storage.

A imagem precisa ter URL pública: na hora de publicar o anúncio, é a
plataforma que vai buscar o arquivo por essa URL. Por isso o bucket é
público — são fotos destinadas a vitrine de marketplace, não dado sensível.

O que não é público é a escrita: subir e apagar passa pela chave service_role,
que só o servidor tem.
"""

from __future__ import annotations

import mimetypes
import re
import unicodedata
import uuid

import httpx

from app.config import SUPABASE_SERVICE_KEY, SUPABASE_URL, supabase_configurado
from app.db import ErroBanco

BUCKET = "produtos"

# o que as plataformas aceitam sem reclamar
TIPOS_ACEITOS = {"image/jpeg", "image/png", "image/webp"}
TAMANHO_MAXIMO = 8 * 1024 * 1024  # 8 MB


def _cabecalhos(extra: dict | None = None) -> dict:
    if not supabase_configurado():
        raise ErroBanco("Supabase não configurado: não dá para guardar fotos.")
    h = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    if extra:
        h.update(extra)
    return h


def url_publica(caminho: str) -> str:
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{caminho}"


def _nome_seguro(nome: str) -> str:
    """Tira acento, espaço e qualquer coisa que atrapalhe numa URL."""
    base = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-.")
    return (base or "foto")[-60:]


async def garantir_bucket() -> tuple[bool, str]:
    """Cria o bucket se ainda não existir. Idempotente."""
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(
            f"{SUPABASE_URL}/storage/v1/bucket/{BUCKET}", headers=_cabecalhos()
        )
        if r.status_code == 200:
            publico = (r.json() or {}).get("public")
            if not publico:
                await c.put(
                    f"{SUPABASE_URL}/storage/v1/bucket/{BUCKET}",
                    headers=_cabecalhos({"Content-Type": "application/json"}),
                    json={"public": True},
                )
                return True, "bucket existia privado e foi aberto para leitura pública"
            return True, "bucket pronto"

        r = await c.post(
            f"{SUPABASE_URL}/storage/v1/bucket",
            headers=_cabecalhos({"Content-Type": "application/json"}),
            json={
                "id": BUCKET,
                "name": BUCKET,
                "public": True,
                "file_size_limit": TAMANHO_MAXIMO,
                "allowed_mime_types": sorted(TIPOS_ACEITOS),
            },
        )
    if r.status_code in (200, 201):
        return True, "bucket criado"
    if r.status_code == 409:
        return True, "bucket pronto"
    return False, f"não consegui criar o bucket ({r.status_code}): {r.text[:200]}"


async def subir(produto_id: str, nome_arquivo: str, conteudo: bytes) -> dict:
    """Guarda a imagem e devolve {caminho, url}."""
    if not conteudo:
        raise ErroBanco("Arquivo vazio.")
    if len(conteudo) > TAMANHO_MAXIMO:
        raise ErroBanco(
            f"A imagem tem {len(conteudo) // 1024 // 1024} MB. O limite é "
            f"{TAMANHO_MAXIMO // 1024 // 1024} MB."
        )

    tipo = mimetypes.guess_type(nome_arquivo)[0] or "application/octet-stream"
    if tipo not in TIPOS_ACEITOS:
        aceitos = ", ".join(sorted(t.split("/")[1] for t in TIPOS_ACEITOS))
        raise ErroBanco(f"Formato não aceito ({tipo}). Use {aceitos}.")

    # prefixo aleatório evita colisão e impede adivinhar o caminho de outra foto
    caminho = f"{produto_id}/{uuid.uuid4().hex[:8]}-{_nome_seguro(nome_arquivo)}"

    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{caminho}",
            headers=_cabecalhos({"Content-Type": tipo, "x-upsert": "true"}),
            content=conteudo,
        )
    if r.status_code not in (200, 201):
        raise ErroBanco(f"Não consegui guardar a imagem ({r.status_code}): {r.text[:200]}")

    return {"caminho": caminho, "url": url_publica(caminho)}


async def apagar(caminho: str) -> None:
    """Remove o arquivo. Silencioso se já não existir."""
    async with httpx.AsyncClient(timeout=20) as c:
        await c.delete(
            f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{caminho}",
            headers=_cabecalhos(),
        )


async def testar() -> tuple[bool, str]:
    """Usado pela página /saude."""
    if not supabase_configurado():
        return False, "Supabase não configurado"
    try:
        return await garantir_bucket()
    except Exception as e:
        return False, str(e)
