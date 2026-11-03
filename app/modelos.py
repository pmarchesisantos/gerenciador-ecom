"""Modelo normalizado: o formato unico em que todo canal e traduzido.

A ideia central do sistema esta aqui. Shopee, Mercado Livre e TikTok chamam a
mesma coisa por nomes diferentes ("COMPLETED", "paid", "DELIVERED"...). Cada
canal traduz o status dele para um GRUPO desta lista, e o painel soma por
grupo. Assim "venda", "cancelado" e "estorno" significam a mesma coisa nos
tres lugares.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

# --- grupos: o vocabulario do painel -------------------------------------
PENDENTE = "pendente"      # comprou, ainda nao pagou / em analise
EFETIVADA = "efetivada"    # pagou: isso e venda
CANCELADA = "cancelada"    # desistiu ou caiu antes de concluir
DEVOLVIDA = "devolvida"    # produto voltou
ESTORNADA = "estornada"    # dinheiro voltou (refund / chargeback)

GRUPOS = [EFETIVADA, PENDENTE, CANCELADA, DEVOLVIDA, ESTORNADA]

ROTULO_GRUPO = {
    EFETIVADA: "Vendas efetivadas",
    PENDENTE: "Aguardando pagamento",
    CANCELADA: "Cancelados",
    DEVOLVIDA: "Devoluções",
    ESTORNADA: "Estornos",
}

# o mesmo grupo no singular, para o selo de um pedido isolado
ROTULO_PEDIDO = {
    EFETIVADA: "Efetivada",
    PENDENTE: "Aguardando pagamento",
    CANCELADA: "Cancelado",
    DEVOLVIDA: "Devolvido",
    ESTORNADA: "Estornado",
}

EXPLICA_GRUPO = {
    EFETIVADA: "Pedido pago e válido. É o que conta como faturamento.",
    PENDENTE: "Pedido criado mas sem pagamento confirmado. Pode virar venda ou cair.",
    CANCELADA: "Cancelado por você, pelo comprador ou pela plataforma. Não fatura.",
    DEVOLVIDA: "O produto voltou. Normalmente vem acompanhado de estorno.",
    ESTORNADA: "O valor foi devolvido ao comprador (reembolso ou chargeback).",
}

COR_GRUPO = {
    EFETIVADA: "verde",
    PENDENTE: "amarelo",
    CANCELADA: "cinza",
    DEVOLVIDA: "laranja",
    ESTORNADA: "vermelho",
}

# --- segunda dimensão: o envio ---------------------------------------------
# Pagar e despachar são coisas diferentes. Um pedido pago há três dias e ainda
# não despachado continua "efetivado" - o que está atrasado é o envio. Por isso
# envio é um campo separado, e não mais um valor na lista de grupos acima.
ENVIO_NAO_APLICA = "nao_aplica"      # não pago, cancelado: não há o que enviar
ENVIO_A_ENVIAR = "a_enviar"          # pago e esperando despacho - a fila de trabalho
ENVIO_ENVIADO = "enviado"            # despachado, a caminho
ENVIO_ENTREGUE = "entregue"
ENVIO_PROBLEMA = "problema"          # devolvido ao remetente, extraviado
ENVIO_DESCONHECIDO = "desconhecido"  # a plataforma não informou

ENVIOS = [
    ENVIO_A_ENVIAR,
    ENVIO_ENVIADO,
    ENVIO_ENTREGUE,
    ENVIO_PROBLEMA,
    ENVIO_NAO_APLICA,
    ENVIO_DESCONHECIDO,
]

ROTULO_ENVIO = {
    ENVIO_A_ENVIAR: "A enviar",
    ENVIO_ENVIADO: "Enviado",
    ENVIO_ENTREGUE: "Entregue",
    ENVIO_PROBLEMA: "Problema no envio",
    ENVIO_NAO_APLICA: "Sem envio",
    ENVIO_DESCONHECIDO: "Envio não informado",
}

EXPLICA_ENVIO = {
    ENVIO_A_ENVIAR: "Pago e esperando despacho. É a sua fila de trabalho do dia.",
    ENVIO_ENVIADO: "Já despachado, a caminho do comprador.",
    ENVIO_ENTREGUE: "Entregue ao comprador.",
    ENVIO_PROBLEMA: "Extraviado, recusado ou devolvido ao remetente.",
    ENVIO_NAO_APLICA: "Pedido não pago ou cancelado — não há o que enviar.",
    ENVIO_DESCONHECIDO: "A plataforma não informou o estado do envio.",
}

COR_ENVIO = {
    ENVIO_A_ENVIAR: "amarelo",
    ENVIO_ENVIADO: "azul",
    ENVIO_ENTREGUE: "verde",
    ENVIO_PROBLEMA: "vermelho",
    ENVIO_NAO_APLICA: "cinza",
    ENVIO_DESCONHECIDO: "cinza",
}

CANAIS_ROTULO = {
    "mercadolivre": "Mercado Livre",
    "shopee": "Shopee",
    "tiktok": "TikTok Shop",
}


@dataclass
class ItemPedido:
    sku: str
    titulo: str
    quantidade: int
    valor_unitario: Decimal = Decimal("0")

    @property
    def valor_total(self) -> Decimal:
        return self.valor_unitario * self.quantidade


@dataclass
class Pedido:
    """Um pedido, igual para qualquer canal."""

    canal: str
    conta: str                    # apelido da conta
    pedido_id: str
    criado_em: datetime
    grupo: str                    # um dos GRUPOS acima
    status_origem: str            # o texto cru que a plataforma devolveu
    envio: str = ENVIO_DESCONHECIDO
    envio_origem: str = ""        # o texto cru do envio, quando existe
    envio_id: str = ""            # id do envio na plataforma (para a etiqueta)
    etiqueta_pronta: bool = False  # a plataforma ja gerou o documento?
    itens: list[ItemPedido] = field(default_factory=list)
    comprador: str = ""
    valor_produtos: Decimal = Decimal("0")
    frete: Decimal = Decimal("0")
    desconto: Decimal = Decimal("0")
    valor_total: Decimal = Decimal("0")   # o que o comprador pagou
    moeda: str = "BRL"
    url: str | None = None

    @property
    def canal_rotulo(self) -> str:
        return CANAIS_ROTULO.get(self.canal, self.canal)

    @property
    def grupo_rotulo(self) -> str:
        return ROTULO_PEDIDO.get(self.grupo, self.grupo)

    @property
    def envio_rotulo(self) -> str:
        return ROTULO_ENVIO.get(self.envio, self.envio)

    @property
    def envio_cor(self) -> str:
        return COR_ENVIO.get(self.envio, "cinza")

    @property
    def pode_etiqueta(self) -> bool:
        """Pago, nao despachado, e com o documento ja gerado pela plataforma."""
        return self.envio == ENVIO_A_ENVIAR and bool(self.envio_id) and self.etiqueta_pronta

    @property
    def quantidade(self) -> int:
        return sum(i.quantidade for i in self.itens)

    @property
    def resumo_itens(self) -> str:
        if not self.itens:
            return "-"
        primeiro = f"{self.itens[0].quantidade}x {self.itens[0].titulo}"
        if len(self.itens) == 1:
            return primeiro
        return f"{primeiro} (+{len(self.itens) - 1})"


@dataclass
class InfoConta:
    """O que a plataforma conta sobre a loja depois de autorizada."""

    loja_id: str = ""
    loja_nome: str = ""
    detalhes: dict = field(default_factory=dict)


@dataclass
class Tokens:
    access_token: str
    refresh_token: str | None = None
    expira_em_segundos: int | None = None
    refresh_expira_em_segundos: int | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Etiquetas:
    """Arquivo de etiquetas pronto para o navegador baixar."""

    conteudo: bytes
    tipo: str                     # content-type
    nome: str                     # nome sugerido do arquivo
    impressas: int = 0
    ignorados: list[str] = field(default_factory=list)   # pedido_id que ficou de fora
    motivo_ignorados: str = ""


@dataclass
class ResultadoCanal:
    """Consulta de um canal pode falhar sozinha sem derrubar o painel."""

    canal: str
    conta: str
    ok: bool
    pedidos: list[Pedido] = field(default_factory=list)
    erro: str = ""
