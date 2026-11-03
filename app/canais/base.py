"""Contrato que todo canal implementa.

Para adicionar um marketplace novo (Amazon, Magalu, Shein...), crie um arquivo
em app/canais/ com uma classe que herda de CanalBase e registre em
app/canais/__init__.py. Nada no painel precisa mudar.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from decimal import Decimal

from app.modelos import InfoConta, Pedido, Tokens


def dec(valor) -> Decimal:
    """Converte qualquer coisa vinda de JSON em Decimal, sem explodir."""
    if valor is None or valor == "":
        return Decimal("0")
    try:
        return Decimal(str(valor))
    except Exception:
        return Decimal("0")


class ErroCanal(RuntimeError):
    """Falha ao falar com a plataforma, com mensagem legivel para a tela."""


class CanalBase(ABC):
    nome: str = ""
    rotulo: str = ""
    # nomes dos campos de credencial, como a plataforma os chama
    rotulo_client_id: str = "Client ID"
    rotulo_client_secret: str = "Client Secret"
    # campos extras pedidos no cadastro (chave -> rotulo)
    campos_extra: dict[str, str] = {}
    # True quando a propria plataforma exige aprovacao do app antes de usar
    exige_aprovacao: bool = False

    # ---------------- autorizacao ----------------
    @abstractmethod
    def url_autorizacao(self, conta: dict, state: str) -> tuple[str, str | None]:
        """Devolve (url para onde mandar o lojista, verificador PKCE ou None)."""

    @abstractmethod
    async def trocar_codigo(self, conta: dict, codigo: str, verificador: str | None) -> Tokens:
        """Troca o codigo que voltou na URL pelos tokens de acesso."""

    @abstractmethod
    async def renovar(self, conta: dict) -> Tokens:
        """Usa o refresh_token para pegar um access_token novo."""

    async def pos_autorizacao(self, conta: dict) -> dict:
        """Campos extras a guardar logo depois de autorizar (ex.: shop_cipher).

        A maioria dos canais nao precisa de nada aqui.
        """
        return {}

    # ---------------- leitura ----------------
    @abstractmethod
    async def info_conta(self, conta: dict) -> InfoConta:
        """Dados da loja: id, nome e o que mais a plataforma contar."""

    @abstractmethod
    async def listar_pedidos(self, conta: dict, inicio: date, fim: date) -> list[Pedido]:
        """Pedidos criados no periodo, ja normalizados."""

    # True quando o canal ja sabe gerar etiqueta por aqui
    gera_etiqueta: bool = False

    async def etiquetas(self, conta: dict, pedidos: list):
        """Devolve um objeto Etiquetas com o arquivo pronto para baixar."""
        raise ErroCanal(
            f"Impressao de etiqueta ainda nao implementada para {self.rotulo}."
        )

    async def enriquecer_envio(self, conta: dict, pedidos: list) -> None:
        """Preenche o estado do envio, quando isso custa chamadas extras.

        Shopee e TikTok ja dizem o envio no proprio status do pedido, entao
        nao precisam de nada aqui. O Mercado Livre guarda o envio em outro
        recurso, e so ele implementa este metodo.
        """
        return None

    # ---------------- utilidades ----------------
    @staticmethod
    def _quando(valor) -> datetime:
        """Converte epoch (int) ou ISO 8601 (str) em datetime com fuso."""
        from datetime import timezone

        if valor is None:
            return datetime.now(timezone.utc)
        if isinstance(valor, (int, float)):
            return datetime.fromtimestamp(valor, tz=timezone.utc)
        texto = str(valor).strip().replace("Z", "+00:00")
        try:
            d = datetime.fromisoformat(texto)
        except ValueError:
            return datetime.now(timezone.utc)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
