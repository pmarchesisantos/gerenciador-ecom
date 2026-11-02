"""Consulta ao vivo nas plataformas e agregacao dos numeros do painel.

Nada aqui e gravado no banco: a cada pedido de pagina batemos nas APIs e
guardamos o resultado por CACHE_SEGUNDOS em memoria, para nao estourar os
limites de chamada de ninguem.
"""

from __future__ import annotations

import asyncio
import time as _time
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from app.canais import canal
from app.config import CACHE_SEGUNDOS, FUSO
from app.modelos import (
    CANAIS_ROTULO,
    CANCELADA,
    DEVOLVIDA,
    EFETIVADA,
    ESTORNADA,
    GRUPOS,
    PENDENTE,
    Pedido,
    ResultadoCanal,
)
from app.servicos import contas as servico_contas

ZERO = Decimal("0")

# chave -> (momento, lista de pedidos)
_cache: dict[tuple, tuple[float, list[Pedido]]] = {}


def limpar_cache() -> None:
    _cache.clear()


async def _consultar_conta(
    linha: dict, inicio: date, fim: date, forcar: bool, com_envio: bool
) -> ResultadoCanal:
    apelido = linha.get("apelido", "?")
    # com_envio entra na chave: a versao enriquecida custa chamadas extras,
    # entao as duas sao guardadas em separado
    chave = (linha["id"], inicio.isoformat(), fim.isoformat(), com_envio)

    if not forcar and chave in _cache:
        quando, pedidos = _cache[chave]
        if _time.time() - quando < CACHE_SEGUNDOS:
            return ResultadoCanal(linha["canal"], apelido, True, pedidos)

    try:
        conta = await servico_contas.garantir_token(linha)
        c = canal(linha["canal"])
        pedidos = await c.listar_pedidos(conta, inicio, fim)
        if com_envio:
            await c.enriquecer_envio(conta, pedidos)
    except Exception as e:
        return ResultadoCanal(linha["canal"], apelido, False, [], str(e))

    _cache[chave] = (_time.time(), pedidos)
    return ResultadoCanal(linha["canal"], apelido, True, pedidos)


async def consultar(
    inicio: date,
    fim: date,
    canais: list[str] | None = None,
    forcar: bool = False,
    usuario: dict | None = None,
    com_envio: bool = False,
) -> dict:
    """Consulta em paralelo as contas ativas que o usuario pode ver."""
    linhas = [c for c in await servico_contas.listar() if c.get("ativa")]

    # dono ve todas; os demais, so as lojas atribuidas a eles
    permitidos = await servico_contas.ids_visiveis(usuario)
    total_existentes = len(linhas)
    linhas = servico_contas.filtrar_visiveis(linhas, permitidos)
    escondidas = total_existentes - len(linhas)

    if canais:
        linhas = [c for c in linhas if c["canal"] in canais]

    conectadas = [c for c in linhas if c.get("access_token_cripto")]
    pendentes = [c for c in linhas if not c.get("access_token_cripto")]

    resultados: list[ResultadoCanal] = []
    if conectadas:
        resultados = list(
            await asyncio.gather(
                *(
                    _consultar_conta(c, inicio, fim, forcar, com_envio)
                    for c in conectadas
                )
            )
        )
    for c in pendentes:
        resultados.append(
            ResultadoCanal(
                c["canal"], c.get("apelido", "?"), False, [], "conta cadastrada mas nao autorizada"
            )
        )

    pedidos: list[Pedido] = []
    for r in resultados:
        pedidos.extend(r.pedidos)
    pedidos.sort(key=lambda p: p.criado_em, reverse=True)

    return {
        "pedidos": pedidos,
        "resultados": resultados,
        "erros": [r for r in resultados if not r.ok],
        "houve_consulta": bool(conectadas),
        "atualizado_em": datetime.now(tz=FUSO),
        "escopo_limitado": permitidos is not None,
        "lojas_escondidas": escondidas,
        "lojas_visiveis": len(linhas),
    }


# ---------------------------------------------------------------------------
# agregacao
# ---------------------------------------------------------------------------
def _bloco() -> dict:
    return {"quantidade": 0, "itens": 0, "valor": ZERO}


def _somar(destino: dict, p: Pedido) -> None:
    destino["quantidade"] += 1
    destino["itens"] += p.quantidade
    destino["valor"] += p.valor_total


def resumir(pedidos: list[Pedido]) -> dict:
    """Todos os numeros que o painel mostra, calculados de uma vez."""
    por_grupo = {g: _bloco() for g in GRUPOS}
    por_canal: dict[str, dict] = defaultdict(_bloco)
    por_canal_grupo: dict[tuple[str, str], dict] = defaultdict(_bloco)
    por_dia: dict[date, dict] = defaultdict(_bloco)
    por_sku: dict[str, dict] = defaultdict(lambda: {"titulo": "", "itens": 0, "valor": ZERO})

    frete_total = ZERO
    for p in pedidos:
        _somar(por_grupo[p.grupo], p)
        _somar(por_canal[p.canal], p)
        _somar(por_canal_grupo[(p.canal, p.grupo)], p)
        if p.grupo == EFETIVADA:
            dia = p.criado_em.astimezone(FUSO).date()
            _somar(por_dia[dia], p)
            frete_total += p.frete
            for item in p.itens:
                alvo = por_sku[item.sku]
                alvo["titulo"] = alvo["titulo"] or item.titulo
                alvo["itens"] += item.quantidade
                alvo["valor"] += item.valor_total

    efetivadas = por_grupo[EFETIVADA]
    ticket = (
        efetivadas["valor"] / efetivadas["quantidade"] if efetivadas["quantidade"] else ZERO
    )

    total_fechados = efetivadas["quantidade"] + por_grupo[CANCELADA]["quantidade"]
    taxa_cancelamento = (
        por_grupo[CANCELADA]["quantidade"] / total_fechados * 100 if total_fechados else 0.0
    )
    perdido = (
        por_grupo[CANCELADA]["valor"]
        + por_grupo[DEVOLVIDA]["valor"]
        + por_grupo[ESTORNADA]["valor"]
    )

    canais = []
    for nome, bloco in sorted(por_canal.items(), key=lambda kv: kv[1]["valor"], reverse=True):
        canais.append(
            {
                "canal": nome,
                "rotulo": CANAIS_ROTULO.get(nome, nome),
                "total": bloco,
                "grupos": {g: por_canal_grupo.get((nome, g), _bloco()) for g in GRUPOS},
            }
        )

    dias = [{"dia": d, **b} for d, b in sorted(por_dia.items())]
    pico = max((d["valor"] for d in dias), default=ZERO) or ZERO

    top_sku = sorted(por_sku.items(), key=lambda kv: kv[1]["valor"], reverse=True)[:10]

    return {
        "total_pedidos": len(pedidos),
        "por_grupo": por_grupo,
        "canais": canais,
        "dias": dias,
        "pico_dia": pico,
        "ticket_medio": ticket,
        "taxa_cancelamento": taxa_cancelamento,
        "valor_perdido": perdido,
        "frete_efetivado": frete_total,
        "top_sku": [{"sku": s, **d} for s, d in top_sku],
        "faturamento": efetivadas["valor"],
        "itens_vendidos": efetivadas["itens"],
        "pendente_valor": por_grupo[PENDENTE]["valor"],
    }


def contar_envios(pedidos: list[Pedido]) -> dict[str, int]:
    saida: dict[str, int] = {}
    for p in pedidos:
        saida[p.envio] = saida.get(p.envio, 0) + 1
    return saida


def filtrar(
    pedidos: list[Pedido],
    canal_nome: str = "",
    grupo: str = "",
    busca: str = "",
    envio: str = "",
) -> list[Pedido]:
    saida = pedidos
    if canal_nome:
        saida = [p for p in saida if p.canal == canal_nome]
    if grupo:
        saida = [p for p in saida if p.grupo == grupo]
    if envio:
        saida = [p for p in saida if p.envio == envio]
    if busca:
        t = busca.strip().lower()
        saida = [
            p
            for p in saida
            if t in p.pedido_id.lower()
            or t in p.comprador.lower()
            or any(t in i.sku.lower() or t in i.titulo.lower() for i in p.itens)
        ]
    return saida
