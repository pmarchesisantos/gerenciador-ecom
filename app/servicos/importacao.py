"""Importar para o catálogo o que já está anunciado na plataforma.

Quem já vende há um tempo não vai redigitar duzentos anúncios para começar a
usar o painel. Então o caminho é o inverso: a plataforma já sabe tudo (título,
preço, grade de cor e tamanho, atributos da categoria, fotos), e o sistema lê
de lá.

Duas decisões que fazem a diferença entre "importar" e "bagunçar o catálogo":

**Nada entra sem você ver antes.** A prévia classifica cada anúncio em três
situações e diz exatamente o que vai acontecer com ele. Você marca o que quer.
Importação que roda sozinha e cria 200 produtos duplicados é pior que não ter
importação nenhuma.

**SKU igual não vira produto novo, vira vínculo.** Se o anúncio tem o SKU de um
produto que já está aqui, o certo é amarrar os dois — era exatamente essa a
função do de/para. Criar um segundo produto com o mesmo SKU quebraria o índice
único e, pior, separaria as vendas que o painel deveria somar.

As fotos entram como referência à URL do próprio Mercado Livre, sem passar pelo
Storage: são imagens que já estão publicadas e servidas por eles, e baixar e
resubir duzentas delas estouraria o tempo da requisição sem ganhar nada.
"""

from __future__ import annotations

from app import db
from app.canais import canal
from app.canais.base import ErroCanal
from app.servicos import contas as servico_contas
from app.servicos import produtos as servico_produtos

# o que vai acontecer com cada anúncio da prévia
NOVO = "novo"
VINCULA = "vincula"
JA_IMPORTADO = "ja_importado"

ROTULO_SITUACAO = {
    NOVO: "Cria produto novo",
    VINCULA: "Só vincula (SKU já existe aqui)",
    JA_IMPORTADO: "Já importado",
}

EXPLICA_SITUACAO = {
    NOVO: "Não existe nada com este SKU no catálogo. Vira um produto novo, com "
          "a grade, os atributos e as fotos que o anúncio já tem.",
    VINCULA: "Já existe um produto com este SKU aqui. Em vez de duplicar, o "
             "anúncio é amarrado a ele — que era o objetivo do de/para.",
    JA_IMPORTADO: "Este anúncio já está vinculado a um produto. Nada a fazer.",
}

COR_SITUACAO = {NOVO: "verde", VINCULA: "azul", JA_IMPORTADO: "cinza"}

# teto por importação: acima disso o tempo da requisição vira problema
TETO = 200


class ErroImportacao(RuntimeError):
    pass


async def _conta_pronta(conta_id: str, usuario: dict | None) -> dict:
    linha = await servico_contas.buscar(conta_id)
    if not linha:
        raise ErroImportacao("Conta não encontrada.")
    if not await servico_contas.pode_ver(usuario, conta_id):
        raise ErroImportacao("Esta loja não faz parte do seu acesso.")
    if not linha.get("access_token_cripto"):
        raise ErroImportacao(
            f"A conta {linha.get('apelido')} não está conectada. "
            "Clique em Conectar na tela de Contas."
        )
    c = canal(linha["canal"])
    if not hasattr(c, "listar_anuncios"):
        raise ErroImportacao(
            f"A importação ainda não foi implementada para {c.rotulo}. "
            "Hoje ela funciona no Mercado Livre."
        )
    return linha


# ---------------------------------------------------------------------------
# prévia
# ---------------------------------------------------------------------------
async def previa(conta_id: str, usuario: dict | None = None) -> dict:
    """Lê os anúncios e diz, um a um, o que a importação faria com eles."""
    linha = await _conta_pronta(conta_id, usuario)
    conta = await servico_contas.garantir_token(linha)
    anuncios = await canal(linha["canal"]).listar_anuncios(conta, TETO)

    ja_vinculados = {
        v.get("anuncio_id")
        for v in await db.selecionar("produto_anuncios", {"conta_id": f"eq.{conta_id}"})
    }
    por_sku = {
        (p.get("sku") or "").strip(): p for p in await db.selecionar("produtos")
    }

    itens = []
    for a in anuncios:
        # sem SKU no anuncio, o codigo do anuncio serve de muleta: o produto
        # entra e o estoque funciona (pelo id da variacao), mas ele nao se
        # une ao mesmo tenis das outras plataformas ate ganhar SKU de verdade
        sem_sku = not a["sku"]
        sku = a["sku"] or a["anuncio_id"]
        if a["anuncio_id"] in ja_vinculados:
            situacao, existente = JA_IMPORTADO, None
        elif sku in por_sku:
            situacao, existente = VINCULA, por_sku[sku]
        else:
            situacao, existente = NOVO, None
        itens.append(
            {
                **a,
                "sku": sku,
                "sem_sku": sem_sku,
                "situacao": situacao,
                "produto_existente": (existente or {}).get("id"),
                "produto_existente_titulo": (existente or {}).get("titulo"),
            }
        )

    itens.sort(key=lambda i: ({NOVO: 0, VINCULA: 1, JA_IMPORTADO: 2}[i["situacao"]],
                              i["titulo"]))
    return {
        "conta": linha,
        "itens": itens,
        "novos": sum(1 for i in itens if i["situacao"] == NOVO),
        "vincula": sum(1 for i in itens if i["situacao"] == VINCULA),
        "ja_importados": sum(1 for i in itens if i["situacao"] == JA_IMPORTADO),
        "sem_sku": sum(
            1 for i in itens if i["sem_sku"] and i["situacao"] != JA_IMPORTADO
        ),
    }


# ---------------------------------------------------------------------------
# aplicar
# ---------------------------------------------------------------------------
def _eixo(valores: dict, eixos: list[str], indice: int) -> str | None:
    if indice >= len(eixos):
        return None
    return valores.get(eixos[indice])


async def _criar_produto(item: dict, usuario: dict) -> dict:
    dados = {
        "sku": item["sku"],
        "titulo": item["titulo"][:160],
        "marca": item.get("marca") or "",
        "condicao": item.get("condicao") or "novo",
        "preco": item.get("preco"),
        # com grade, o estoque de verdade mora em cada variação
        "estoque": 0 if item.get("variacoes") else item.get("estoque") or 0,
        "ativo": True,
        "controla_estoque": True,
        "estoque_minimo": 5,
        "ml_categoria_id": item.get("categoria_id") or "",
        "atributos_ml": item.get("atributos") or {},
        "eixo1_nome": (item.get("eixos") or [None])[0],
        "eixo2_nome": (item.get("eixos") or [None, None])[1]
        if len(item.get("eixos") or []) > 1
        else None,
    }
    # preco e estoque chegam como numero; _limpar espera texto ou numero e
    # ja sabe lidar com os dois
    return await servico_produtos.criar(dados, usuario)


async def _criar_variacoes(produto: dict, item: dict) -> int:
    eixos = item.get("eixos") or []
    linhas = []
    for ordem, v in enumerate(item.get("variacoes") or []):
        valores = v.get("valores") or {}
        v1 = _eixo(valores, eixos, 0)
        v2 = _eixo(valores, eixos, 1)
        sku = v.get("sku") or servico_produtos.sku_da_variacao(
            produto["sku"], (v1, v2)
        )
        linhas.append(
            {
                "produto_id": produto["id"],
                "sku": sku,
                # sem isto, pedido de anuncio sem SKU nao diz qual par saiu
                "anuncio_variacao_id": v.get("anuncio_variacao_id") or None,
                "eixo1_valor": v1,
                "eixo2_valor": v2,
                "preco": v.get("preco"),
                "estoque": int(v.get("estoque") or 0),
                "ordem": ordem,
            }
        )
    if not linhas:
        return 0
    # SKU de variação é único no banco: o que já existir fica como está
    gravadas = await db.inserir_varios(
        "produto_variacoes", linhas, ignorar_repetidos=True
    )
    return len(gravadas)


async def _criar_fotos(produto: dict, item: dict) -> int:
    urls = item.get("fotos") or []
    if not urls:
        return 0
    linhas = [
        {
            "produto_id": produto["id"],
            # marcador: não é arquivo nosso no Storage, é imagem da plataforma
            "caminho": f"externo:{item['anuncio_id']}:{i}",
            "url": url,
            "ordem": i,
        }
        for i, url in enumerate(urls)
    ]
    return len(await db.inserir_varios("produto_fotos", linhas))


async def sincronizar(conta_id: str, usuario: dict) -> dict:
    """Completa o que falta nos produtos JA importados, sem criar nada.

    Existe por um motivo concreto: quem importou antes de o sistema guardar o
    id da variação ficou com uma grade que o pedido não consegue apontar. Sem
    este reparo a correção só valeria para importação nova, e o catálogo que
    já está aqui continuaria sem baixa de estoque.

    Casa a variação da plataforma com a nossa pelos valores dos eixos (Preto /
    39) e, como alternativa, pelo SKU. Só preenche o que está vazio — nunca
    sobrescreve o que você ajustou à mão.
    """
    linha = await _conta_pronta(conta_id, usuario)
    conta = await servico_contas.garantir_token(linha)
    anuncios = await canal(linha["canal"]).listar_anuncios(conta, TETO)

    vinculos = {
        v["anuncio_id"]: v["produto_id"]
        for v in await db.selecionar(
            "produto_anuncios", {"conta_id": f"eq.{conta_id}"}
        )
        if v.get("anuncio_id") and v.get("produto_id")
    }
    if not vinculos:
        raise ErroImportacao(
            "Nenhum anúncio desta loja está vinculado ainda. Use a importação "
            "primeiro."
        )

    por_produto: dict[str, list[dict]] = {}
    for v in await db.selecionar("produto_variacoes"):
        por_produto.setdefault(v["produto_id"], []).append(v)

    completadas, produtos_tocados = 0, set()
    for a in anuncios:
        produto_id = vinculos.get(a["anuncio_id"])
        if not produto_id:
            continue
        nossas = por_produto.get(produto_id) or []
        if not nossas:
            continue

        eixos = a.get("eixos") or []
        por_valores = {
            (str(v.get("eixo1_valor") or ""), str(v.get("eixo2_valor") or "")): v
            for v in nossas
        }
        por_sku = {str(v.get("sku") or ""): v for v in nossas}

        for v in a.get("variacoes") or []:
            id_plataforma = v.get("anuncio_variacao_id")
            if not id_plataforma:
                continue
            valores = v.get("valores") or {}
            chave = (
                str(_eixo(valores, eixos, 0) or ""),
                str(_eixo(valores, eixos, 1) or ""),
            )
            nossa = por_valores.get(chave) or por_sku.get(v.get("sku") or "")
            if not nossa or nossa.get("anuncio_variacao_id"):
                continue
            await db.atualizar(
                "produto_variacoes",
                {"id": f"eq.{nossa['id']}"},
                {"anuncio_variacao_id": id_plataforma},
            )
            nossa["anuncio_variacao_id"] = id_plataforma
            completadas += 1
            produtos_tocados.add(produto_id)

    if completadas:
        await db.registrar_evento(
            "produto",
            f"sincronização: {completadas} variação(ões) ganharam o id da "
            f"plataforma em {len(produtos_tocados)} produto(s)",
            conta_id,
            linha.get("canal"),
            usuario,
        )
    return {"completadas": completadas, "produtos": len(produtos_tocados)}


async def importar(
    conta_id: str, anuncios_escolhidos: list[str], usuario: dict
) -> dict:
    """Cria (ou vincula) só os anúncios marcados na prévia."""
    escolhidos = {a for a in anuncios_escolhidos if a}
    if not escolhidos:
        raise ErroImportacao("Marque ao menos um anúncio para importar.")

    dados = await previa(conta_id, usuario)
    alvos = [
        i
        for i in dados["itens"]
        if i["anuncio_id"] in escolhidos and i["situacao"] != JA_IMPORTADO
    ]
    if not alvos:
        raise ErroImportacao(
            "Os anúncios marcados já estão importados. Nada a fazer."
        )

    criados, vinculados, variacoes, fotos, problemas = 0, 0, 0, 0, []
    for item in alvos:
        try:
            if item["situacao"] == VINCULA:
                produto_id = item["produto_existente"]
            else:
                produto = await _criar_produto(item, usuario)
                produto_id = produto["id"]
                criados += 1
                variacoes += await _criar_variacoes(produto, item)
                fotos += await _criar_fotos(produto, item)

            await servico_produtos.vincular(
                produto_id, conta_id, item["anuncio_id"], item["sku"], usuario
            )
            vinculados += 1
        except (servico_produtos.ErroProduto, ErroCanal, db.ErroBanco) as e:
            # um anúncio torto não pode abortar os outros 199
            problemas.append({"anuncio_id": item["anuncio_id"],
                              "titulo": item["titulo"], "erro": str(e)})

    await db.registrar_evento(
        "produto",
        f"importação: {criados} produto(s) criado(s), {vinculados} anúncio(s) "
        f"vinculado(s)",
        conta_id,
        dados["conta"].get("canal"),
        usuario,
    )
    return {
        "criados": criados,
        "vinculados": vinculados,
        "variacoes": variacoes,
        "fotos": fotos,
        "problemas": problemas,
    }
