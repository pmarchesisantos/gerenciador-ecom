"""Categoria e campos obrigatórios do anúncio, lidos da plataforma.

Por que isto existe: cada categoria pede um conjunto diferente de atributos, a
lista muda sem aviso, e nenhuma plataforma publica um anúncio sem os
obrigatórios. Chumbar essa lista no código seria errado em uma semana. Então o
sistema pergunta.

Hoje quem responde é o Mercado Livre, com duas chamadas de leitura que não
custam nada e usam a chave que já está conectada:

    título  -> /sites/MLB/domain_discovery/search  -> categoria provável
    categoria -> /categories/{id}/attributes       -> campos, com os obrigatórios

Shopee e TikTok têm o equivalente; quando as contas estiverem conectadas, cada
uma entra aqui como uma fonte a mais, sem mexer na tela.

O que fica guardado no produto é só o resultado: a categoria escolhida e os
valores preenchidos. A lista de campos nunca é persistida — ela é da
plataforma, não nossa.
"""

from __future__ import annotations

import time as _time

from app.canais import canal
from app.canais.base import ErroCanal
from app.servicos import contas as servico_contas

# A lista de atributos de uma categoria é estável por dias. Guardar por algumas
# horas evita uma ida ao ML em cada abertura da ficha do produto.
CACHE_SEGUNDOS = 6 * 3600

# Teto de opcionais mostrados na tela. Algumas categorias do ML têm mais de 80
# atributos; despejar todos transformaria a aba em um formulário inútil. Os
# mais relevantes vêm primeiro, e a tela diz quantos ficaram de fora.
TETO_OPCIONAIS = 15

# tags do ML que significam "não é você que preenche"
TAGS_FORA = ("hidden", "read_only", "fixed")

# tags que dizem "este atributo é por variação" — cor e tamanho caem aqui, e o
# lugar deles é a aba Variações, não a ficha do produto
TAGS_VARIACAO = ("variation_attribute", "allow_variations")

# {categoria_id: (momento, ficha)}
_cache: dict[str, tuple[float, dict]] = {}


def limpar_cache() -> None:
    _cache.clear()


class SemCanal(RuntimeError):
    """Nenhuma conta conectada sabe responder — com instrução de como resolver."""


# ---------------------------------------------------------------------------
# de onde vem a resposta
# ---------------------------------------------------------------------------
async def _conta_ml(usuario: dict | None) -> dict:
    """A primeira conta do Mercado Livre conectada que o usuário pode ver."""
    linhas = await servico_contas.listar()
    permitidos = await servico_contas.ids_visiveis(usuario)

    candidatas = [
        linha
        for linha in linhas
        if linha.get("canal") == "mercadolivre"
        and linha.get("ativa")
        and linha.get("access_token_cripto")
        and (permitidos is None or linha["id"] in permitidos)
    ]
    if not candidatas:
        raise SemCanal(
            "Quem responde quais campos o anúncio pede é a própria plataforma, "
            "e para perguntar preciso de uma conta conectada. Conecte o Mercado "
            "Livre em Contas e volte aqui — a consulta não custa nada."
        )
    return await servico_contas.garantir_token(candidatas[0])


# ---------------------------------------------------------------------------
# normalização
# ---------------------------------------------------------------------------
def _tem(tags: dict, nomes: tuple) -> bool:
    return any(bool(tags.get(n)) for n in nomes)


def _campo(cru: dict) -> dict:
    """Um atributo do ML traduzido para o que a tela precisa desenhar."""
    tags = cru.get("tags") or {}
    valores = [
        v.get("name")
        for v in (cru.get("values") or [])
        if v.get("name")
    ]
    unidades = [
        u.get("name")
        for u in (cru.get("allowed_units") or [])
        if u.get("name")
    ]

    tipo = (cru.get("value_type") or "string").lower()
    # lista fechada só vale como <select> se der para ler na tela; acima disso
    # um campo de texto com sugestões é mais rápido de usar
    if valores and len(valores) <= 80:
        tipo = "lista"
    elif tipo == "number_unit" and unidades:
        tipo = "numero_unidade"
    elif tipo in ("number", "boolean"):
        tipo = "numero" if tipo == "number" else "texto"
    else:
        tipo = "texto"

    return {
        "id": cru.get("id") or "",
        "nome": cru.get("name") or cru.get("id") or "",
        "tipo": tipo,
        "obrigatorio": _tem(tags, ("required", "catalog_required")),
        "por_variacao": _tem(tags, TAGS_VARIACAO),
        "dica": (cru.get("hint") or "")[:200],
        "valores": valores[:80],
        "unidades": unidades[:20],
        "unidade_padrao": cru.get("default_unit") or (unidades[0] if unidades else ""),
        "grupo": cru.get("attribute_group_name") or "",
        # no ML, relevância menor = mais importante; sem valor, vai para o fim
        "relevancia": cru.get("relevance") if isinstance(cru.get("relevance"), int) else 99,
    }


def organizar(atributos: list[dict]) -> dict:
    """Separa a lista crua em obrigatórios, por variação e opcionais."""
    uteis = []
    for cru in atributos or []:
        if not isinstance(cru, dict) or not cru.get("id"):
            continue
        if _tem(cru.get("tags") or {}, TAGS_FORA):
            continue  # o ML preenche sozinho ou não aceita escrita
        uteis.append(_campo(cru))

    obrigatorios, por_variacao, opcionais = [], [], []
    for campo in uteis:
        if campo["por_variacao"]:
            por_variacao.append(campo)
        elif campo["obrigatorio"]:
            obrigatorios.append(campo)
        else:
            opcionais.append(campo)

    opcionais.sort(key=lambda c: (c["relevancia"], c["nome"]))
    return {
        "obrigatorios": obrigatorios,
        "por_variacao": por_variacao,
        "opcionais": opcionais[:TETO_OPCIONAIS],
        "opcionais_de_fora": max(0, len(opcionais) - TETO_OPCIONAIS),
    }


# ---------------------------------------------------------------------------
# uso pelas telas
# ---------------------------------------------------------------------------
async def sugerir(titulo: str, usuario: dict | None = None) -> list[dict]:
    conta = await _conta_ml(usuario)
    return await canal("mercadolivre").sugerir_categoria(conta, titulo)


async def ficha(categoria_id: str, usuario: dict | None = None) -> dict:
    """Nome, caminho e campos de uma categoria, já separados por grupo."""
    cid = (categoria_id or "").strip()
    if not cid:
        return {}

    guardado = _cache.get(cid)
    if guardado and _time.time() - guardado[0] < CACHE_SEGUNDOS:
        return guardado[1]

    conta = await _conta_ml(usuario)
    cru = await canal("mercadolivre").atributos_categoria(conta, cid)
    resultado = {
        "categoria_id": cid,
        "nome": cru.get("nome") or cid,
        "caminho": cru.get("caminho") or "",
        **organizar(cru.get("atributos") or []),
    }
    _cache[cid] = (_time.time(), resultado)
    return resultado


def montar_valores(ids: list[str], valores: list[str], unidades: list[str]) -> dict:
    """Monta {ID_DO_ATRIBUTO: "valor"} a partir do que veio do formulário.

    Campo com unidade virá "40" + "cm" e é guardado como "40 cm": é o formato
    que o ML aceita em value_name, então na hora de publicar não há conversão.
    Campo vazio não entra — valor em branco é diferente de valor informado.
    """
    saida: dict[str, str] = {}
    for i, chave in enumerate(ids or []):
        nome = (chave or "").strip()
        if not nome:
            continue
        valor = (valores[i] if i < len(valores) else "").strip()
        if not valor:
            continue
        unidade = (unidades[i] if i < len(unidades) else "").strip()
        saida[nome[:60]] = (f"{valor} {unidade}".strip() if unidade else valor)[:300]
    return saida


def faltando(ficha_categoria: dict, preenchidos: dict) -> list[str]:
    """Nomes dos campos obrigatórios ainda em branco — o aviso da tela."""
    if not ficha_categoria:
        return []
    return [
        c["nome"]
        for c in ficha_categoria.get("obrigatorios") or []
        if not (preenchidos or {}).get(c["id"])
    ]


__all__ = [
    "ErroCanal",
    "SemCanal",
    "faltando",
    "ficha",
    "limpar_cache",
    "montar_valores",
    "organizar",
    "sugerir",
]
