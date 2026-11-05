"""Estoque: o saldo, o livro que explica o saldo, e a baixa automática.

Três ideias, nesta ordem:

1. **O saldo** mora em `produto_variacoes.estoque` quando o produto tem grade,
   e em `produtos.estoque` quando não tem. Um produto com cor e tamanho não tem
   "um estoque": tem um por par vendável. Quem controla é a linha.

2. **O livro** (`estoque_movimentos`) não duplica o saldo — ele explica como o
   saldo chegou onde está. Entrou 10, saiu 7 nestes pedidos, logo tem 3. Sem
   isso o número é um palpite que ninguém consegue auditar.

3. **A baixa automática** olha os pedidos que a consulta ao vivo já trouxe e
   registra a saída dos que foram despachados. Como o sistema não guarda
   pedido, a única defesa contra descontar duas vezes é o livro: a chave
   (canal, pedido, sku) é única no banco para o motivo `envio`. Pode chamar
   quantas vezes quiser — inclusive a cada F5 — que desconta uma vez só.

Uma regra que evita o erro mais caro: **o estoque de um produto só começa a
descontar depois do primeiro movimento dele**. Se você digitar hoje "tenho 10
pares" e o sistema olhar 30 dias de envios passados, o saldo que você acabou de
conferir na prateleira seria destruído. Pedido anterior ao primeiro movimento
não desconta, e a tela diz desde quando cada produto conta.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app import db
from app.modelos import ENVIO_ENTREGUE, ENVIO_ENVIADO

# situações de uma linha de estoque
ZERADO = "zerado"
ACABANDO = "acabando"
OK = "ok"

ROTULO_SITUACAO = {
    ZERADO: "Sem estoque",
    ACABANDO: "Acabando",
    OK: "Suficiente",
}

EXPLICA_SITUACAO = {
    ZERADO: "Não tem nenhum aqui. Se vender, você não tem o que despachar.",
    ACABANDO: "Chegou no mínimo que você definiu. Hora de repor.",
    OK: "Acima do mínimo definido para este item.",
}

COR_SITUACAO = {ZERADO: "vermelho", ACABANDO: "amarelo", OK: "verde"}

# motivos de movimento
ENTRADA = "entrada"
ENVIO = "envio"
AJUSTE = "ajuste"
DEVOLUCAO = "devolucao"

ROTULO_MOTIVO = {
    ENTRADA: "Entrada",
    ENVIO: "Baixa por envio",
    AJUSTE: "Ajuste de inventário",
    DEVOLUCAO: "Devolução",
}

# despachado ou já entregue: em ambos o par saiu daqui
ENVIOS_QUE_BAIXAM = (ENVIO_ENVIADO, ENVIO_ENTREGUE)

MINIMO_PADRAO = 5


class ErroEstoque(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# leitura
# ---------------------------------------------------------------------------
def _quando(valor) -> datetime | None:
    if not valor:
        return None
    if isinstance(valor, datetime):
        return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def situacao(estoque: int, minimo: int) -> str:
    if estoque <= 0:
        return ZERADO
    return ACABANDO if estoque <= minimo else OK


def _linha(produto: dict, variacao: dict | None) -> dict:
    """Uma unidade controlável: o produto, ou uma combinação da grade."""
    minimo_produto = produto.get("estoque_minimo")
    if minimo_produto is None:
        minimo_produto = MINIMO_PADRAO

    if variacao:
        minimo = variacao.get("estoque_minimo")
        minimo = minimo_produto if minimo is None else minimo
        estoque = int(variacao.get("estoque") or 0)
        sku = variacao.get("sku") or produto.get("sku") or ""
        nome_variacao = " / ".join(
            v for v in (variacao.get("eixo1_valor"), variacao.get("eixo2_valor")) if v
        )
    else:
        minimo = minimo_produto
        estoque = int(produto.get("estoque") or 0)
        sku = produto.get("sku") or ""
        nome_variacao = ""

    return {
        "produto_id": produto["id"],
        "variacao_id": (variacao or {}).get("id"),
        "produto": produto.get("titulo") or sku,
        "sku": sku,
        "variacao": nome_variacao,
        "estoque": estoque,
        "minimo": int(minimo),
        "situacao": situacao(estoque, int(minimo)),
        "controla": bool(produto.get("controla_estoque", True)),
        "ativa": True if not variacao else bool(variacao.get("ativa", True)),
    }


async def posicao() -> list[dict]:
    """Todas as linhas controláveis, as mais críticas primeiro."""
    produtos = await db.selecionar("produtos", ordem="titulo.asc")
    if not produtos:
        return []

    por_produto: dict[str, list[dict]] = {}
    for v in await db.selecionar("produto_variacoes", ordem="ordem.asc"):
        por_produto.setdefault(v["produto_id"], []).append(v)

    linhas = []
    for p in produtos:
        grade = por_produto.get(p["id"]) or []
        if grade:
            linhas.extend(_linha(p, v) for v in grade)
        else:
            linhas.append(_linha(p, None))

    ordem = {ZERADO: 0, ACABANDO: 1, OK: 2}
    linhas.sort(key=lambda l: (ordem[l["situacao"]], l["estoque"], l["produto"], l["sku"]))
    return linhas


def alertas(linhas: list[dict]) -> list[dict]:
    """Só o que precisa de reposição — é o que vira aviso na tela."""
    return [
        l
        for l in linhas
        if l["controla"] and l["ativa"] and l["situacao"] in (ZERADO, ACABANDO)
    ]


def resumo(linhas: list[dict]) -> dict:
    controladas = [l for l in linhas if l["controla"]]
    return {
        "itens": len(controladas),
        "pares": sum(l["estoque"] for l in controladas),
        "zerados": sum(1 for l in controladas if l["situacao"] == ZERADO),
        "acabando": sum(1 for l in controladas if l["situacao"] == ACABANDO),
    }


async def historico(produto_id: str = "", limite: int = 60) -> list[dict]:
    filtros = {"produto_id": f"eq.{produto_id}"} if produto_id else {}
    linhas = await db.selecionar(
        "estoque_movimentos", filtros, ordem="criado_em.desc"
    )
    return linhas[:limite]


# ---------------------------------------------------------------------------
# escrita
# ---------------------------------------------------------------------------
async def _gravar_saldo(linha: dict, novo: int) -> None:
    if linha.get("variacao_id"):
        await db.atualizar(
            "produto_variacoes", {"id": f"eq.{linha['variacao_id']}"}, {"estoque": novo}
        )
    else:
        await db.atualizar(
            "produtos", {"id": f"eq.{linha['produto_id']}"}, {"estoque": novo}
        )


async def _achar_linha(produto_id: str, variacao_id: str | None) -> dict:
    produto = await db.um("produtos", {"id": f"eq.{produto_id}"})
    if not produto:
        raise ErroEstoque("Produto não encontrado.")
    variacao = None
    if variacao_id:
        variacao = await db.um("produto_variacoes", {"id": f"eq.{variacao_id}"})
        if not variacao:
            raise ErroEstoque("Variação não encontrada.")
    return _linha(produto, variacao)


async def movimentar(
    produto_id: str,
    variacao_id: str | None,
    delta: int,
    motivo: str,
    usuario: dict | None = None,
    observacao: str = "",
) -> dict:
    """Um movimento manual: entrada, ajuste ou devolução.

    Grava o livro e o saldo. Nunca deixa o saldo negativo — se você tentar
    tirar mais do que tem, o erro diz quanto tem, em vez de inventar um número.
    """
    if delta == 0:
        raise ErroEstoque("Informe uma quantidade diferente de zero.")

    linha = await _achar_linha(produto_id, variacao_id)
    novo = linha["estoque"] + delta
    if novo < 0:
        raise ErroEstoque(
            f"{linha['sku']} tem {linha['estoque']} em estoque — "
            f"não dá para tirar {abs(delta)}."
        )

    await db.inserir(
        "estoque_movimentos",
        {
            "produto_id": linha["produto_id"],
            "variacao_id": linha["variacao_id"],
            "sku": linha["sku"],
            "delta": delta,
            "saldo_depois": novo,
            "motivo": motivo,
            "observacao": (observacao or "").strip()[:300] or None,
            "usuario_id": (usuario or {}).get("id"),
        },
    )
    await _gravar_saldo(linha, novo)
    await db.registrar_evento(
        "estoque",
        f"{ROTULO_MOTIVO.get(motivo, motivo)} de {delta:+d} em {linha['sku']} "
        f"(ficou {novo})",
        usuario=usuario,
    )
    linha["estoque"] = novo
    linha["situacao"] = situacao(novo, linha["minimo"])
    return linha


async def anotar_ajuste(
    produto_id: str,
    variacao_id: str | None,
    sku: str,
    antes: int,
    depois: int,
    usuario: dict | None = None,
    observacao: str = "",
) -> None:
    """Registra no livro uma mudança de saldo que outra tela já gravou.

    O campo de estoque continua existindo na ficha do produto e na grade de
    variações, e é cômodo. Mas uma edição por lá que não deixasse rastro
    abriria de novo o buraco que este arquivo existe para fechar: um número
    que mudou e ninguém sabe por quê. Então ela vira um ajuste no livro.

    Falhar aqui não pode desfazer o que a outra tela salvou - por isso quem
    chama engole o erro. Livro incompleto é ruim; salvar pela metade é pior.
    """
    if antes == depois:
        return
    await db.inserir(
        "estoque_movimentos",
        {
            "produto_id": produto_id,
            "variacao_id": variacao_id,
            "sku": sku,
            "delta": depois - antes,
            "saldo_depois": depois,
            "motivo": AJUSTE,
            "observacao": observacao or "editado na ficha do produto",
            "usuario_id": (usuario or {}).get("id"),
        },
    )


# ---------------------------------------------------------------------------
# baixa automática a partir dos pedidos que a consulta ao vivo trouxe
# ---------------------------------------------------------------------------
async def mapa_sku() -> dict[str, dict]:
    """Chaves que um pedido pode trazer -> a linha de estoque que elas consomem.

    Três famílias de chave, e a ordem importa na hora de procurar:

    1. **O SKU da variação** — o código do lojista. É o que mais aparece em
       pedido de quem vende numeração, e o único que une o mesmo tênis no
       Mercado Livre, na Shopee e no TikTok. Manda sempre que existe.
    2. **O id da variação na plataforma** — a rede de segurança para quem não
       preencheu SKU no anúncio: nesse caso o pedido chega com o código do
       anúncio, igual para todas as numerações. Vale só dentro da plataforma,
       mas continua valendo mesmo que o SKU mude depois.
    3. **O código do anúncio e o SKU do vínculo** — só para produto sem grade.
       Com grade eles não dizem qual par saiu, e chutar seria pior que não
       baixar.
    """
    produtos = {p["id"]: p for p in await db.selecionar("produtos")}
    if not produtos:
        return {}

    grades: dict[str, list[dict]] = {}
    for v in await db.selecionar("produto_variacoes"):
        grades.setdefault(v["produto_id"], []).append(v)

    saida: dict[str, dict] = {}
    for produto in produtos.values():
        grade = grades.get(produto["id"]) or []
        for v in grade:
            linha = _linha(produto, v)
            for chave in (v.get("sku"), v.get("anuncio_variacao_id")):
                if chave:
                    saida[str(chave).strip()] = linha
        if not grade and produto.get("sku"):
            saida[str(produto["sku"]).strip()] = _linha(produto, None)

    for vinculo in await db.selecionar("produto_anuncios"):
        produto = produtos.get(vinculo["produto_id"])
        if not produto or grades.get(vinculo["produto_id"]):
            continue  # com grade, o vinculo nao identifica a variacao
        for chave in (vinculo.get("sku_canal"), vinculo.get("anuncio_id")):
            if chave:
                saida.setdefault(str(chave).strip(), _linha(produto, None))
    return saida


def achar(catalogo: dict, item) -> dict | None:
    """A linha de estoque de um item de pedido. SKU primeiro, id depois."""
    for chave in (getattr(item, "sku", ""), getattr(item, "sku_variacao", "")):
        linha = catalogo.get((chave or "").strip())
        if linha:
            return linha
    return None


async def projetar(pedidos: list, catalogo: dict | None = None) -> dict:
    """Por pedido, o que fica no limite se ele for despachado agora.

    Serve ao aviso da hora de imprimir a etiqueta: nao adianta dizer "acabou"
    depois que o par ja saiu daqui. O pedido que vai sair ainda nao descontou -
    ele esta em "a enviar" - entao a projecao e o saldo de hoje menos o que vai
    no pacote.
    """
    if catalogo is None:
        catalogo = await mapa_sku()
    if not catalogo:
        return {}

    saida: dict[str, list[dict]] = {}
    for pedido in pedidos:
        avisos = []
        for item in pedido.itens:
            linha = achar(catalogo, item)
            if not linha or not linha["controla"]:
                continue
            resta = linha["estoque"] - int(item.quantidade or 0)
            if resta > linha["minimo"]:
                continue
            avisos.append(
                {
                    "sku": linha["sku"],
                    "resta": max(resta, 0),
                    "minimo": linha["minimo"],
                    "negativo": resta < 0,
                }
            )
        if avisos:
            saida[pedido.pedido_id] = avisos
    return saida


async def conciliar(
    pedidos: list, usuario: dict | None = None, catalogo: dict | None = None
) -> dict:
    """Desconta os envios já despachados que ainda não constam no livro.

    Idempotente por construção: o par (canal, pedido, sku) é único no banco
    para o motivo `envio`. Chamar de novo não desconta de novo.

    Devolve o que aconteceu, para a tela poder contar a história — inclusive
    os itens que não bateram com nenhum SKU do catálogo, que são exatamente os
    que o dono precisa ir cadastrar.
    """
    resultado = {"baixados": 0, "pares": 0, "sem_cadastro": [], "linhas": []}
    despachados = [p for p in pedidos if getattr(p, "envio", "") in ENVIOS_QUE_BAIXAM]
    if not despachados:
        return resultado

    if catalogo is None:
        catalogo = await mapa_sku()
    if not catalogo:
        return resultado

    movimentos = await db.selecionar("estoque_movimentos")
    ja_feitos = {
        (m.get("canal"), m.get("pedido_id"), m.get("sku"))
        for m in movimentos
        if m.get("motivo") == ENVIO
    }
    # desde quando cada produto conta: antes do primeiro movimento, o saldo
    # que a pessoa digitou ja refletia as vendas antigas
    comeco: dict[str, datetime] = {}
    for m in movimentos:
        quando = _quando(m.get("criado_em"))
        pid = m.get("produto_id")
        if quando and pid and (pid not in comeco or quando < comeco[pid]):
            comeco[pid] = quando

    novos: list[dict] = []
    por_linha: dict[tuple, int] = {}
    vistos_sem_cadastro: set[str] = set()

    for pedido in despachados:
        for item in pedido.itens:
            sku = (item.sku or "").strip()
            linha = achar(catalogo, item)
            if not linha:
                if sku and sku != "-" and sku not in vistos_sem_cadastro:
                    vistos_sem_cadastro.add(sku)
                    resultado["sem_cadastro"].append(
                        {"sku": sku, "titulo": item.titulo, "canal": pedido.canal}
                    )
                continue
            if not linha["controla"]:
                continue

            # A chave e o NOSSO SKU, nao o que veio no pedido. Sem SKU no
            # anuncio, dois tamanhos do mesmo produto chegam com o mesmo
            # codigo de anuncio: a chave crua colidiria e o indice unico
            # descartaria uma das numeracoes em silencio.
            chave = (pedido.canal, pedido.pedido_id, linha["sku"])
            if chave in ja_feitos:
                continue
            # antes do primeiro movimento do produto, o saldo que a pessoa
            # conferiu na prateleira ja embutia as vendas antigas
            inicio = comeco.get(linha["produto_id"])
            se_quando = _quando(pedido.criado_em)
            if not inicio or not se_quando or se_quando <= inicio:
                continue

            ja_feitos.add(chave)
            quantidade = int(item.quantidade or 0)
            if quantidade <= 0:
                continue
            destino = (linha["produto_id"], linha["variacao_id"])
            if destino not in por_linha:
                por_linha[destino] = {"linha": linha, "saldo": linha["estoque"]}
            por_linha[destino]["saldo"] = max(
                0, por_linha[destino]["saldo"] - quantidade
            )
            novos.append(
                {
                    "produto_id": linha["produto_id"],
                    "variacao_id": linha["variacao_id"],
                    "sku": linha["sku"],
                    "delta": -quantidade,
                    "saldo_depois": por_linha[destino]["saldo"],
                    "motivo": ENVIO,
                    "pedido_id": pedido.pedido_id,
                    "canal": pedido.canal,
                    "usuario_id": (usuario or {}).get("id"),
                }
            )

    if not novos:
        return resultado

    # o lote inteiro em uma chamada; repetido e ignorado pelo indice unico
    gravados = await db.inserir_varios(
        "estoque_movimentos", novos, ignorar_repetidos=True
    )
    if not gravados:
        return resultado

    # Descontar o que o banco recusou arrancaria pares do saldo sem nenhuma
    # linha no livro para justificar. So entra no saldo o que foi gravado.
    saiu_por_linha: dict[tuple, int] = {}
    for m in gravados:
        destino = (m.get("produto_id"), m.get("variacao_id"))
        saiu_por_linha[destino] = saiu_por_linha.get(destino, 0) + abs(
            int(m.get("delta") or 0)
        )

    for destino, saiu in saiu_por_linha.items():
        guardado = por_linha.get(destino)
        linha = dict(guardado["linha"]) if guardado else await _achar_linha(*destino)
        novo = max(0, linha["estoque"] - saiu)
        await _gravar_saldo(linha, novo)
        linha["estoque"] = novo
        linha["situacao"] = situacao(novo, linha["minimo"])
        resultado["linhas"].append(linha)

    resultado["baixados"] = len(gravados)
    resultado["pares"] = sum(saiu_por_linha.values())
    await db.registrar_evento(
        "estoque",
        f"baixa automática: {resultado['pares']} unidade(s) em "
        f"{len(por_linha)} item(ns)",
        usuario=usuario,
    )
    return resultado
