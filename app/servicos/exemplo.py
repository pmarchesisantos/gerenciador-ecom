"""Dados de exemplo, para conhecer o painel antes de ter as chaves.

So entra em cena com ?exemplo=1 na URL, e a tela avisa em vermelho que os
numeros sao inventados. Nada aqui toca o banco nem as plataformas.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from app.config import FUSO
from app.modelos import (
    CANCELADA,
    DEVOLVIDA,
    EFETIVADA,
    ENVIO_A_ENVIAR,
    ENVIO_ENTREGUE,
    ENVIO_ENVIADO,
    ENVIO_NAO_APLICA,
    ENVIO_PROBLEMA,
    ESTORNADA,
    PENDENTE,
    ItemPedido,
    Pedido,
)

CATALOGO = [
    ("CAM-PRE-M", "Camiseta basica preta M", "39.90"),
    ("CAM-BRA-G", "Camiseta basica branca G", "39.90"),
    ("CAN-360", "Caneca ceramica 360ml", "24.50"),
    ("FON-BT5", "Fone bluetooth TWS", "89.90"),
    ("CAB-USBC", "Cabo USB-C 2m reforcado", "19.90"),
    ("SUP-NOTE", "Suporte de notebook aluminio", "129.00"),
]

# peso de cada situacao, para o exemplo parecer com a vida real
SORTEIO = (
    [EFETIVADA] * 74 + [PENDENTE] * 10 + [CANCELADA] * 9 + [DEVOLVIDA] * 4 + [ESTORNADA] * 3
)

ORIGEM = {
    "mercadolivre": {
        EFETIVADA: "paid",
        PENDENTE: "payment_in_process",
        CANCELADA: "cancelled",
        DEVOLVIDA: "paid / return",
        ESTORNADA: "paid (pagamento refunded)",
    },
    "shopee": {
        EFETIVADA: "COMPLETED",
        PENDENTE: "UNPAID",
        CANCELADA: "CANCELLED",
        DEVOLVIDA: "TO_RETURN",
        ESTORNADA: "CANCELLED / refund",
    },
    "tiktok": {
        EFETIVADA: "COMPLETED",
        PENDENTE: "UNPAID",
        CANCELADA: "CANCELLED",
        DEVOLVIDA: "RETURN_AND_REFUND",
        ESTORNADA: "REFUND",
    },
}

# dentro de um pedido efetivado, o envio ainda pode estar em qualquer etapa
SORTEIO_ENVIO = (
    [ENVIO_ENTREGUE] * 55 + [ENVIO_ENVIADO] * 28 + [ENVIO_A_ENVIAR] * 15
    + [ENVIO_PROBLEMA] * 2
)

CONTAS = {
    "mercadolivre": "Loja ML (exemplo)",
    "shopee": "Loja Shopee (exemplo)",
    "tiktok": "Loja TikTok (exemplo)",
}


def pedidos(inicio: date, fim: date) -> list[Pedido]:
    rnd = random.Random(f"{inicio}{fim}")  # mesmo periodo, mesmos numeros
    dias = max(1, (fim - inicio).days + 1)
    saida: list[Pedido] = []

    for i in range(dias * 6):
        dia = inicio + timedelta(days=rnd.randrange(dias))
        quando = datetime.combine(
            dia, time(rnd.randrange(8, 23), rnd.randrange(60)), tzinfo=FUSO
        )
        canal = rnd.choice(["mercadolivre", "shopee", "shopee", "tiktok"])
        grupo = rnd.choice(SORTEIO)

        itens = []
        for sku, titulo, preco in rnd.sample(CATALOGO, rnd.choice([1, 1, 1, 2])):
            itens.append(
                ItemPedido(
                    sku=sku,
                    titulo=titulo,
                    quantidade=rnd.choice([1, 1, 1, 2, 3]),
                    valor_unitario=Decimal(preco),
                )
            )

        produtos = sum((it.valor_total for it in itens), Decimal("0"))
        frete = Decimal(rnd.choice(["0.00", "0.00", "12.90", "19.90", "24.90"]))

        if grupo == EFETIVADA:
            envio = rnd.choice(SORTEIO_ENVIO)
        elif grupo == DEVOLVIDA:
            envio = ENVIO_PROBLEMA
        elif grupo == ESTORNADA:
            envio = rnd.choice([ENVIO_ENTREGUE, ENVIO_PROBLEMA])
        else:
            envio = ENVIO_NAO_APLICA

        saida.append(
            Pedido(
                canal=canal,
                conta=CONTAS[canal],
                pedido_id=f"{canal[:2].upper()}-{200000 + i}",
                criado_em=quando,
                grupo=grupo,
                status_origem=ORIGEM[canal][grupo],
                envio=envio,
                envio_origem=envio,
                envio_id=f"ENV{rnd.randrange(10000, 99999)}" if envio != ENVIO_NAO_APLICA else "",
                # espelha a vida real: ML e TikTok entregam etiqueta por API,
                # a Shopee ainda nao
                etiqueta_pronta=envio == ENVIO_A_ENVIAR
                and canal in ("mercadolivre", "tiktok"),
                itens=itens,
                comprador=f"comprador{rnd.randrange(100, 999)}",
                valor_produtos=produtos,
                frete=frete,
                valor_total=produtos + frete,
                moeda="BRL",
            )
        )

    saida.sort(key=lambda p: p.criado_em, reverse=True)
    return saida


def resultado(inicio: date, fim: date) -> dict:
    from app.modelos import ResultadoCanal

    lista = pedidos(inicio, fim)
    por_canal: dict[str, list[Pedido]] = {}
    for p in lista:
        por_canal.setdefault(p.canal, []).append(p)
    return {
        "pedidos": lista,
        "resultados": [
            ResultadoCanal(c, CONTAS[c], True, ps) for c, ps in sorted(por_canal.items())
        ],
        "erros": [],
        "houve_consulta": True,
        "atualizado_em": datetime.now(tz=FUSO),
        "escopo_limitado": False,
        "lojas_escondidas": 0,
        "lojas_visiveis": len(por_canal),
    }
