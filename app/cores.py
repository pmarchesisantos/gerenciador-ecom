"""Paleta do painel.

Aqui fica só o **mapeamento semântico**: qual token de cor cada coisa usa.
O valor em si (o hex) mora no CSS, em `:root` para o tema claro e nos blocos
de tema escuro. É isso que permite trocar de tema em um lugar só, sem o
servidor saber qual tema o navegador está mostrando.

Duas famílias, com trabalhos diferentes - nunca se misturam:

CANAL    identidade. Diz "isto é Shopee", não "isto é bom ou ruim".
         A cor segue o canal, não a posição no ranking: se um canal sumir
         do filtro, os outros mantêm a cor que tinham.

GRUPO    estado. Diz "deu certo / está pendente / deu errado". Nunca usada
         para identificar série, e nunca aparece sem o rótulo ao lado.

Os hexes de referência estão nos comentários e no CSS. Os três tons de canal
foram validados para daltonismo nas duas superfícies:

  claro  (#ffffff): pior par ΔE 9.2 deuteranopia, 24.0 visão normal.
                    O aqua fica em 2.82:1 de contraste - abaixo de 3:1 - por
                    isso toda barra de canal leva rótulo direto com o valor.
  escuro (#15171c): pior par ΔE 9.4 deuteranopia, 20.9 visão normal,
                    e os três passam de 3:1.

O escuro não é o claro invertido: são passos próprios da mesma rampa,
escolhidos e validados contra a superfície escura.
"""

from app.modelos import (
    CANCELADA,
    DEVOLVIDA,
    EFETIVADA,
    ESTORNADA,
    PENDENTE,
)


def _var(token: str) -> str:
    return f"var({token})"


# --- identidade dos canais (paleta categórica, ordem fixa) -----------------
# claro: azul #2a78d6 | laranja #eb6834 | aqua #1baf7a
# escuro: azul #3987e5 | laranja #d95926 | aqua #199e70
CANAL_COR = {
    "mercadolivre": _var("--canal-mercadolivre"),
    "shopee": _var("--canal-shopee"),
    "tiktok": _var("--canal-tiktok"),
}
CANAL_COR_PADRAO = _var("--canal-outro")

# --- estado do pedido (paleta de status, igual nos dois temas) -------------
# good #0ca30c | warning #fab219 | neutro | serious #ec835a | critical #d03b3b
GRUPO_COR = {
    EFETIVADA: _var("--good"),
    PENDENTE: _var("--warning"),
    CANCELADA: _var("--neutro"),   # cancelar não é falha, é ausência de venda
    DEVOLVIDA: _var("--serious"),
    ESTORNADA: _var("--critical"),
}

# --- série única (quando não há identidade a distinguir) -------------------
SERIE = _var("--serie")

# --- cromo do gráfico ------------------------------------------------------
GRADE = _var("--grade")
TINTA_FRACA = _var("--tinta-3")


def canal(nome: str) -> str:
    return CANAL_COR.get(nome, CANAL_COR_PADRAO)


def grupo(nome: str) -> str:
    return GRUPO_COR.get(nome, TINTA_FRACA)
