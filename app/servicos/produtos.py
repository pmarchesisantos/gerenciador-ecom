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
from app.servicos import estoque as servico_estoque


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
    """{sku_na_plataforma: produto base} — usado para unificar o que foi vendido.

    Entram tres coisas: o SKU cadastrado no vinculo, o codigo do anuncio, e o
    SKU de cada variacao. O ultimo e o que mais aparece nos pedidos - quem
    vende cor e tamanho recebe AZERA-BRANCO-37, nao AZERA. Todos apontam para
    o produto base, porque o painel agrupa por produto, nao por numeracao.
    """
    produtos = {p["id"]: p for p in await db.selecionar("produtos")}
    if not produtos:
        return {}

    saida: dict[str, dict] = {}
    for v in await db.selecionar("produto_variacoes"):
        produto = produtos.get(v["produto_id"])
        if produto and v.get("sku"):
            saida[str(v["sku"]).strip()] = produto

    for v in await db.selecionar("produto_anuncios"):
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
    if valor in ("", None):
        return None
    try:
        if not isinstance(valor, str):
            # ja e numero (veio do banco): nao passa pela limpeza de pontuacao
            return int(valor) if inteiro else Decimal(str(valor))
        texto = valor.strip()
        if not texto:
            return None
        if inteiro:
            return int(texto.replace(".", ""))
        # formato brasileiro: 1.234,56 -> 1234.56
        return Decimal(texto.replace(".", "").replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
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

    def texto(campo: str, limite: int = 120) -> str | None:
        return ((dados.get(campo) or "").strip() or None) if dados.get(campo) is not None else None

    return {
        "sku": sku,
        "titulo": titulo,
        "descricao": texto("descricao"),
        "marca": texto("marca"),
        "categoria": texto("categoria"),
        "condicao": texto("condicao") or "novo",
        "preco": float(preco) if preco is not None else None,
        "estoque": estoque,
        "peso_g": _numero(dados.get("peso_g"), "Peso", inteiro=True),
        "comprimento_cm": _numero(dados.get("comprimento_cm"), "Comprimento", inteiro=True),
        "largura_cm": _numero(dados.get("largura_cm"), "Largura", inteiro=True),
        "altura_cm": _numero(dados.get("altura_cm"), "Altura", inteiro=True),
        "medidas_por_variacao": bool(dados.get("medidas_por_variacao")),
        "controla_estoque": bool(dados.get("controla_estoque")),
        "estoque_minimo": _numero(dados.get("estoque_minimo"), "Estoque mínimo",
                                  inteiro=True) or 0,
        "eixo1_nome": texto("eixo1_nome"),
        "eixo2_nome": texto("eixo2_nome"),
        # categoria na plataforma: o id e o que vale na publicacao, o nome e
        # so para a tela nao mostrar "MLB108746" para gente nenhuma
        "ml_categoria_id": texto("ml_categoria_id"),
        "ml_categoria_nome": texto("ml_categoria_nome"),
        # fiscais
        "ncm": texto("ncm"),
        "cest": texto("cest"),
        "origem_fiscal": texto("origem_fiscal"),
        "cfop_dentro": texto("cfop_dentro"),
        "cfop_fora": texto("cfop_fora"),
        "csosn": texto("csosn"),
        "unidade": texto("unidade") or "UN",
        "pis_cofins_cst": texto("pis_cofins_cst"),
        "tributos_pct": float(_numero(dados.get("tributos_pct"), "% de tributos") or 0) or None,
        "ativo": bool(dados.get("ativo")),
    }


def montar_atributos(chaves: list[str], valores: list[str]) -> dict:
    """Pares da aba Especificacao. Linha sem nome e descartada."""
    saida: dict[str, str] = {}
    for chave, valor in zip(chaves, valores):
        nome = (chave or "").strip()
        if nome:
            saida[nome[:60]] = (valor or "").strip()[:300]
    return saida


async def criar(dados: dict, usuario: dict) -> dict:
    campos = _limpar(dados)
    if await por_sku(campos["sku"]):
        raise ErroProduto(f"Já existe um produto com o SKU {campos['sku']}.")
    campos["criado_por"] = usuario.get("id")
    for campo in ("atributos", "atributos_ml"):
        if dados.get(campo) is not None:
            campos[campo] = dados[campo]
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
    for campo in ("atributos", "atributos_ml"):
        if dados.get(campo) is not None:
            campos[campo] = dados[campo]

    antes = await buscar(produto_id)
    atualizado = await db.atualizar("produtos", {"id": f"eq.{produto_id}"}, campos)
    if antes and not await variacoes(produto_id):
        # sem grade, o estoque do produto e o saldo de verdade
        try:
            await servico_estoque.anotar_ajuste(
                produto_id, None, campos["sku"],
                int(antes.get("estoque") or 0), int(campos["estoque"] or 0), usuario,
            )
        except Exception:
            pass
    await db.registrar_evento(
        "produto", f"produto {campos['sku']} alterado", usuario=usuario
    )
    return atualizado


async def definir_categoria(
    produto_id: str, categoria_id: str, categoria_nome: str, usuario: dict
) -> None:
    """Grava so a categoria escolhida.

    De proposito nao passa por _limpar: a escolha vem de um clique, nao do
    formulario inteiro, e revalidar preco e estoque aqui so criaria chance de
    estragar o que ja estava salvo.
    """
    cid = (categoria_id or "").strip()
    if not cid:
        raise ErroProduto("Escolha uma categoria.")
    await db.atualizar(
        "produtos",
        {"id": f"eq.{produto_id}"},
        {
            "ml_categoria_id": cid,
            "ml_categoria_nome": (categoria_nome or "").strip() or None,
        },
    )
    await db.registrar_evento(
        "produto", f"categoria definida como {categoria_nome or cid}", usuario=usuario
    )


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
# variacoes
# ---------------------------------------------------------------------------
async def variacoes(produto_id: str) -> list[dict]:
    return await db.selecionar(
        "produto_variacoes", {"produto_id": f"eq.{produto_id}"}, ordem="ordem.asc"
    )


async def contagem_variacoes() -> dict[str, int]:
    saida: dict[str, int] = {}
    for v in await db.selecionar("produto_variacoes"):
        saida[v["produto_id"]] = saida.get(v["produto_id"], 0) + 1
    return saida


def _opcoes(texto: str) -> list[str]:
    """Quebra "Branco, Preto, Preto/Branco" numa lista limpa e sem repetidos."""
    vistos, saida = set(), []
    for parte in (texto or "").replace(";", ",").replace(chr(10), ",").split(","):
        valor = parte.strip()
        if valor and valor.lower() not in vistos:
            vistos.add(valor.lower())
            saida.append(valor)
    return saida


def combinar(opcoes1: list[str], opcoes2: list[str]) -> list[tuple[str, str | None]]:
    """Produto cartesiano dos dois eixos, na ordem em que foram escritos."""
    if not opcoes1:
        return []
    if not opcoes2:
        return [(a, None) for a in opcoes1]
    return [(a, b) for a in opcoes1 for b in opcoes2]


def sku_da_variacao(base: str, valores: tuple) -> str:
    """AZERA + (Branco, 37) -> AZERA-BRANCO-37, como no padrao da Shopee."""
    import re
    import unicodedata

    partes = [base] + [v for v in valores if v]
    limpo = []
    for parte in partes:
        texto = unicodedata.normalize("NFKD", str(parte))
        texto = texto.encode("ascii", "ignore").decode().upper()
        texto = re.sub(r"[^A-Z0-9]+", "-", texto).strip("-")
        if texto:
            limpo.append(texto)
    return "-".join(limpo)


async def gerar_variacoes(
    produto_id: str, texto1: str, texto2: str, usuario: dict
) -> int:
    """Cria a grade de combinacoes, sem mexer no que ja existe.

    Regerar depois de acrescentar uma cor acrescenta so as linhas novas: as
    antigas mantem preco, estoque e GTIN ja preenchidos.
    """
    produto = await buscar(produto_id)
    if not produto:
        raise ErroProduto("Produto não encontrado.")

    opcoes1, opcoes2 = _opcoes(texto1), _opcoes(texto2)
    if not opcoes1:
        raise ErroProduto(f"Liste ao menos uma opção de {produto.get('eixo1_nome') or 'variação'}.")

    combinacoes = combinar(opcoes1, opcoes2)
    if len(combinacoes) > 200:
        raise ErroProduto(
            f"Isso geraria {len(combinacoes)} variações. O limite é 200 por produto."
        )

    existentes = {
        (v.get("eixo1_valor"), v.get("eixo2_valor")) for v in await variacoes(produto_id)
    }
    base_sku = produto["sku"]
    criadas = 0
    for ordem, (v1, v2) in enumerate(combinacoes):
        if (v1, v2) in existentes:
            continue
        await db.inserir(
            "produto_variacoes",
            {
                "produto_id": produto_id,
                "sku": sku_da_variacao(base_sku, (v1, v2)),
                "eixo1_valor": v1,
                "eixo2_valor": v2,
                "preco": produto.get("preco"),
                "estoque": 0,
                "ordem": ordem,
            },
        )
        criadas += 1

    if criadas:
        await db.registrar_evento(
            "produto", f"{criadas} variação(ões) criadas em {base_sku}", usuario=usuario
        )
    return criadas


async def salvar_variacoes(produto_id: str, linhas: list[dict], usuario: dict) -> None:
    """Grava preço, estoque, SKU e GTIN de cada linha da grade."""
    antes = {v["id"]: v for v in await variacoes(produto_id)}
    for linha in linhas:
        vid = linha.get("id")
        if not vid:
            continue
        sku = (linha.get("sku") or "").strip()
        if not sku:
            raise ErroProduto("Toda variação precisa de SKU.")

        preco = _numero(linha.get("preco"), "Preço da variação")
        promo = _numero(linha.get("preco_promo"), "Preço promocional")
        if preco is not None and promo is not None and promo > preco:
            raise ErroProduto(
                f"Na variação {sku}, o preço promocional está maior que o preço."
            )
        estoque = _numero(linha.get("estoque"), "Estoque da variação", inteiro=True) or 0
        if estoque < 0:
            raise ErroProduto(f"Estoque negativo na variação {sku}.")

        await db.atualizar(
            "produto_variacoes",
            {"id": f"eq.{vid}"},
            {
                "sku": sku,
                "preco": float(preco) if preco is not None else None,
                "preco_promo": float(promo) if promo is not None else None,
                "estoque": estoque,
                "gtin": (linha.get("gtin") or "").strip() or None,
                "ativa": bool(linha.get("ativa")),
            },
        )
        anterior = antes.get(vid)
        if anterior is not None:
            try:
                await servico_estoque.anotar_ajuste(
                    produto_id, vid, sku,
                    int(anterior.get("estoque") or 0), estoque, usuario,
                    observacao="editado na grade de variações",
                )
            except Exception:
                pass
    await db.registrar_evento("produto", "grade de variações salva", usuario=usuario)


async def aplicar_a_todas(
    produto_id: str, preco: str, estoque: str, usuario: dict
) -> int:
    """O "Aplicar a todos" da Shopee: preenche a coluna inteira de uma vez."""
    campos: dict = {}
    if (preco or "").strip():
        valor = _numero(preco, "Preço")
        campos["preco"] = float(valor) if valor is not None else None
    if (estoque or "").strip():
        campos["estoque"] = _numero(estoque, "Estoque", inteiro=True) or 0
    if not campos:
        raise ErroProduto("Informe preço, estoque, ou os dois.")

    linhas = await variacoes(produto_id)
    for v in linhas:
        await db.atualizar("produto_variacoes", {"id": f"eq.{v['id']}"}, campos)
    await db.registrar_evento(
        "produto", f"valores aplicados a {len(linhas)} variação(ões)", usuario=usuario
    )
    return len(linhas)


async def remover_variacao(variacao_id: str) -> None:
    await db.apagar("produto_variacoes", {"id": f"eq.{variacao_id}"})


def resumo_grade(linhas: list[dict]) -> dict:
    """Totais que a tela mostra no topo da grade."""
    precos = [float(v["preco"]) for v in linhas if v.get("preco") is not None]
    return {
        "quantas": len(linhas),
        "ativas": sum(1 for v in linhas if v.get("ativa")),
        "estoque": sum(int(v.get("estoque") or 0) for v in linhas),
        "sem_preco": sum(1 for v in linhas if v.get("preco") is None),
        "preco_min": min(precos) if precos else None,
        "preco_max": max(precos) if precos else None,
    }


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
