"""Catálogo próprio de produtos e o de/para com os anúncios das plataformas.

Este é o primeiro dado de negócio que o sistema passa a guardar — até aqui o
banco só tinha credenciais e acessos. Produto precisa ser persistido: ele
existe antes de qualquer plataforma e é a referência que une as três.

O de/para (`produto_anuncios`) é o que resolve a bagunça de SKU: o mesmo
produto tem código diferente em cada canal, e é por aqui que o painel passa a
somar "Fone bluetooth" em vez de três linhas separadas.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app import armazenamento, db


class ErroProduto(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# leitura
# ---------------------------------------------------------------------------
async def listar(busca: str = "", so_ativos: bool = False) -> list[dict]:
    filtros: dict[str, str] = {}
    if so_ativos:
        filtros["ativo"] = "is.true"
    linhas = await db.selecionar("produtos", filtros, ordem="titulo.asc")
    if busca:
        t = busca.strip().lower()
        linhas = [
            p
            for p in linhas
            if t in (p.get("sku") or "").lower()
            or t in (p.get("titulo") or "").lower()
            or t in (p.get("marca") or "").lower()
        ]
    return linhas


async def buscar(produto_id: str) -> dict | None:
    return await db.um("produtos", {"id": f"eq.{produto_id}"})


async def por_sku(sku: str) -> dict | None:
    return await db.um("produtos", {"sku": f"eq.{sku.strip()}"})


async def fotos(produto_id: str) -> list[dict]:
    return await db.selecionar(
        "produto_fotos", {"produto_id": f"eq.{produto_id}"}, ordem="ordem.asc"
    )


async def anuncios(produto_id: str) -> list[dict]:
    return await db.selecionar(
        "produto_anuncios", {"produto_id": f"eq.{produto_id}"}, ordem="criado_em.asc"
    )


async def contagem_fotos() -> dict[str, int]:
    saida: dict[str, int] = {}
    for f in await db.selecionar("produto_fotos"):
        saida[f["produto_id"]] = saida.get(f["produto_id"], 0) + 1
    return saida


async def contagem_anuncios() -> dict[str, int]:
    saida: dict[str, int] = {}
    for a in await db.selecionar("produto_anuncios"):
        saida[a["produto_id"]] = saida.get(a["produto_id"], 0) + 1
    return saida


async def mapa_sku_canal() -> dict[str, dict]:
    """{sku_na_plataforma: produto} — usado para unificar o que foi vendido."""
    vinculos = await db.selecionar("produto_anuncios")
    if not vinculos:
        return {}
    produtos = {p["id"]: p for p in await db.selecionar("produtos")}
    saida: dict[str, dict] = {}
    for v in vinculos:
        produto = produtos.get(v["produto_id"])
        if not produto:
            continue
        for chave in (v.get("sku_canal"), v.get("anuncio_id")):
            if chave:
                saida[str(chave).strip()] = produto
    return saida


# ---------------------------------------------------------------------------
# escrita
# ---------------------------------------------------------------------------
def _numero(valor, campo: str, inteiro: bool = False):
    texto = (valor or "").strip() if isinstance(valor, str) else valor
    if texto in ("", None):
        return None
    try:
        if inteiro:
            return int(str(texto).replace(".", "").strip())
        return Decimal(str(texto).replace(".", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ErroProduto(f"{campo} precisa ser um número.")


def _limpar(dados: dict) -> dict:
    sku = (dados.get("sku") or "").strip()
    titulo = (dados.get("titulo") or "").strip()
    if not sku:
        raise ErroProduto("O SKU é obrigatório: é ele que liga o produto às plataformas.")
    if not titulo:
        raise ErroProduto("O título é obrigatório.")

    preco = _numero(dados.get("preco"), "Preço")
    if preco is not None and preco < 0:
        raise ErroProduto("O preço não pode ser negativo.")
    estoque = _numero(dados.get("estoque"), "Estoque", inteiro=True) or 0
    if estoque < 0:
        raise ErroProduto("O estoque não pode ser negativo.")

    return {
        "sku": sku,
        "titulo": titulo,
        "descricao": (dados.get("descricao") or "").strip() or None,
        "marca": (dados.get("marca") or "").strip() or None,
        "preco": float(preco) if preco is not None else None,
        "estoque": estoque,
        "peso_g": _numero(dados.get("peso_g"), "Peso", inteiro=True),
        "comprimento_cm": _numero(dados.get("comprimento_cm"), "Comprimento", inteiro=True),
        "largura_cm": _numero(dados.get("largura_cm"), "Largura", inteiro=True),
        "altura_cm": _numero(dados.get("altura_cm"), "Altura", inteiro=True),
        "ativo": bool(dados.get("ativo")),
    }


async def criar(dados: dict, usuario: dict) -> dict:
    campos = _limpar(dados)
    if await por_sku(campos["sku"]):
        raise ErroProduto(f"Já existe um produto com o SKU {campos['sku']}.")
    campos["criado_por"] = usuario.get("id")
    novo = await db.inserir("produtos", campos)
    await db.registrar_evento(
        "produto", f"produto {campos['sku']} cadastrado", usuario=usuario
    )
    return novo


async def atualizar(produto_id: str, dados: dict, usuario: dict) -> dict:
    campos = _limpar(dados)
    outro = await por_sku(campos["sku"])
    if outro and outro["id"] != produto_id:
        raise ErroProduto(f"O SKU {campos['sku']} já é de outro produto.")
    atualizado = await db.atualizar("produtos", {"id": f"eq.{produto_id}"}, campos)
    await db.registrar_evento(
        "produto", f"produto {campos['sku']} alterado", usuario=usuario
    )
    return atualizado


async def remover(produto_id: str, usuario: dict) -> None:
    produto = await buscar(produto_id)
    if not produto:
        return
    # as fotos saem do Storage junto: a linha some por cascata, o arquivo não
    for foto in await fotos(produto_id):
        try:
            await armazenamento.apagar(foto["caminho"])
        except Exception:
            pass
    await db.apagar("produtos", {"id": f"eq.{produto_id}"})
    await db.registrar_evento(
        "produto", f"produto {produto.get('sku')} removido", usuario=usuario
    )


# ---------------------------------------------------------------------------
# fotos
# ---------------------------------------------------------------------------
async def adicionar_foto(produto_id: str, nome: str, conteudo: bytes) -> dict:
    produto = await buscar(produto_id)
    if not produto:
        raise ErroProduto("Produto não encontrado.")
    atuais = await fotos(produto_id)
    if len(atuais) >= 10:
        raise ErroProduto("Máximo de 10 fotos por produto.")

    await armazenamento.garantir_bucket()
    guardada = await armazenamento.subir(produto_id, nome, conteudo)
    return await db.inserir(
        "produto_fotos",
        {
            "produto_id": produto_id,
            "caminho": guardada["caminho"],
            "url": guardada["url"],
            "ordem": len(atuais),
        },
    )


async def remover_foto(foto_id: str) -> None:
    foto = await db.um("produto_fotos", {"id": f"eq.{foto_id}"})
    if not foto:
        return
    try:
        await armazenamento.apagar(foto["caminho"])
    except Exception:
        pass
    await db.apagar("produto_fotos", {"id": f"eq.{foto_id}"})


# ---------------------------------------------------------------------------
# de/para com os anúncios
# ---------------------------------------------------------------------------
async def vincular(
    produto_id: str, conta_id: str, anuncio_id: str, sku_canal: str, usuario: dict
) -> dict:
    anuncio_id = (anuncio_id or "").strip()
    if not anuncio_id:
        raise ErroProduto("Informe o código do anúncio na plataforma.")
    ja = await db.um(
        "produto_anuncios",
        {"conta_id": f"eq.{conta_id}", "anuncio_id": f"eq.{anuncio_id}"},
    )
    if ja:
        raise ErroProduto(f"O anúncio {anuncio_id} já está vinculado a um produto.")
    novo = await db.inserir(
        "produto_anuncios",
        {
            "produto_id": produto_id,
            "conta_id": conta_id,
            "anuncio_id": anuncio_id,
            "sku_canal": (sku_canal or "").strip() or None,
        },
    )
    await db.registrar_evento(
        "produto", f"anúncio {anuncio_id} vinculado", conta_id, None, usuario
    )
    return novo


async def desvincular(vinculo_id: str) -> None:
    await db.apagar("produto_anuncios", {"id": f"eq.{vinculo_id}"})
