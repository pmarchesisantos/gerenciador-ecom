"""Registro dos canais disponiveis."""

from app.canais.base import CanalBase, ErroCanal
from app.canais.mercadolivre import MercadoLivre
from app.canais.shopee import Shopee
from app.canais.tiktok import TikTokShop

CANAIS: dict[str, CanalBase] = {
    MercadoLivre.nome: MercadoLivre(),
    Shopee.nome: Shopee(),
    TikTokShop.nome: TikTokShop(),
}


def canal(nome: str) -> CanalBase:
    if nome not in CANAIS:
        raise ErroCanal(f"Canal desconhecido: {nome}")
    return CANAIS[nome]


__all__ = ["CANAIS", "canal", "CanalBase", "ErroCanal"]
