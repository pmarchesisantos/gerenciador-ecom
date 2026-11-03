"""TikTok Shop (Partner API, versao 202309).

Particularidades que explicam o codigo:

1. A URL de autorizacao usa o Service ID (nao o App Key). O App Key e o App
   Secret entram depois, na troca do codigo.
2. A troca de token acontece em um host separado (auth.tiktok-shops.com) e
   NAO leva assinatura. Todo o resto vai assinado (HMAC-SHA256) e com o token
   no cabecalho x-tts-access-token.
3. Cada loja autorizada tem um "shop_cipher", que vai em quase toda chamada.
4. Devolucao e estorno vivem em uma API separada (return_refund). Buscamos ela
   tambem e, quando casa com um pedido, o pedido vira Devolucao/Estorno.

Doc: https://partner.tiktokshop.com/docv2
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time as _time
from datetime import date, datetime, time
from urllib.parse import urlencode

import httpx

from app.canais.base import CanalBase, ErroCanal, dec
from app.config import FUSO, redirect_uri
from app.modelos import (
    CANCELADA,
    DEVOLVIDA,
    EFETIVADA,
    ENVIO_A_ENVIAR,
    ENVIO_ENTREGUE,
    ENVIO_ENVIADO,
    ENVIO_NAO_APLICA,
    ESTORNADA,
    PENDENTE,
    InfoConta,
    ItemPedido,
    Pedido,
    Tokens,
)

AUTORIZACAO = "https://services.tiktokshop.com/open/authorize"
HOST_AUTH = "https://auth.tiktok-shops.com"
HOST_API = "https://open-api.tiktokglobalshop.com"

MAPA_STATUS = {
    "UNPAID": PENDENTE,
    "ON_HOLD": PENDENTE,
    "AWAITING_SHIPMENT": EFETIVADA,
    "AWAITING_COLLECTION": EFETIVADA,
    "PARTIALLY_SHIPPING": EFETIVADA,
    "IN_TRANSIT": EFETIVADA,
    "DELIVERED": EFETIVADA,
    "COMPLETED": EFETIVADA,
    "CANCELLED": CANCELADA,
}


MAPA_ENVIO = {
    "UNPAID": ENVIO_NAO_APLICA,
    "ON_HOLD": ENVIO_NAO_APLICA,
    "CANCELLED": ENVIO_NAO_APLICA,
    "AWAITING_SHIPMENT": ENVIO_A_ENVIAR,
    "AWAITING_COLLECTION": ENVIO_A_ENVIAR,
    "PARTIALLY_SHIPPING": ENVIO_ENVIADO,
    "IN_TRANSIT": ENVIO_ENVIADO,
    "DELIVERED": ENVIO_ENTREGUE,
    "COMPLETED": ENVIO_ENTREGUE,
}


class TikTokShop(CanalBase):
    nome = "tiktok"
    rotulo = "TikTok Shop"
    rotulo_client_id = "App Key"
    rotulo_client_secret = "App Secret"
    campos_extra = {"service_id": "Service ID (aba Authorization do app)"}
    exige_aprovacao = True
    motivo_sem_etiqueta = (
        "No TikTok a etiqueta só é gerada depois de arranjar o envio — uma ação "
        "que altera o pedido na plataforma e avisa o comprador. Enquanto o "
        "pacote está em TO_FULFILL, a API responde \"Documents couldn't be "
        "printed before shipped\". Imprima pelo painel do TikTok por enquanto."
    )

    # ------------------------------------------------------------------
    # assinatura
    # ------------------------------------------------------------------
    @staticmethod
    def _assinatura(segredo: str, caminho: str, params: dict, corpo: str = "") -> str:
        # ordem alfabetica das chaves, ignorando sign e access_token;
        # o resultado e envelopado pelo proprio app_secret nas duas pontas
        chaves = sorted(k for k in params if k not in ("sign", "access_token"))
        base = caminho + "".join(f"{k}{params[k]}" for k in chaves)
        base = f"{segredo}{base}{corpo}{segredo}"
        return hmac.new(segredo.encode(), base.encode(), hashlib.sha256).hexdigest()

    async def _chamar(
        self,
        conta: dict,
        caminho: str,
        extra_params: dict | None = None,
        corpo: dict | None = None,
        com_cifra: bool = True,
    ) -> dict:
        """com_cifra=False para os endpoints que recusam o shop_cipher.

        A maioria das chamadas exige o cipher da loja, mas as de autorizacao
        (que listam as lojas do app) respondem 400 se ele vier junto:
        "The 'shop_cipher' query parameter is not required for this request".
        """
        if not conta.get("_access"):
            raise ErroCanal("Conta TikTok sem access_token. Conecte de novo.")

        params: dict = {
            "app_key": conta["client_id"],
            "timestamp": int(_time.time()),
        }
        cifra = (conta.get("extra") or {}).get("shop_cipher")
        if cifra and com_cifra:
            params["shop_cipher"] = cifra
        if extra_params:
            params.update(extra_params)

        corpo_texto = json.dumps(corpo, separators=(",", ":")) if corpo is not None else ""
        params["sign"] = self._assinatura(conta["_secret"], caminho, params, corpo_texto)

        cabecalhos = {
            "x-tts-access-token": conta["_access"],
            "content-type": "application/json",
        }
        url = f"{HOST_API}{caminho}"
        async with httpx.AsyncClient(timeout=40) as c:
            if corpo is None:
                r = await c.get(url, params=params, headers=cabecalhos)
            else:
                r = await c.post(url, params=params, headers=cabecalhos, content=corpo_texto)

        if r.status_code >= 400:
            raise ErroCanal(f"TikTok {caminho} HTTP {r.status_code}: {r.text[:300]}")
        j = r.json()
        # o TikTok devolve 200 com code != 0 quando da errado
        if j.get("code") not in (0, None):
            raise ErroCanal(f"TikTok {caminho}: code {j.get('code')} - {j.get('message', '')}")
        return j.get("data") or {}

    # ------------------------------------------------------------------
    # autorizacao
    # ------------------------------------------------------------------
    def url_autorizacao(self, conta: dict, state: str) -> tuple[str, str | None]:
        service_id = (conta.get("extra") or {}).get("service_id")
        if not service_id:
            raise ErroCanal(
                "Falta o Service ID. Edite a conta e preencha o campo "
                "(fica no app, aba Authorization, no Partner Center)."
            )
        params = {"service_id": service_id, "state": state}
        return f"{AUTORIZACAO}?{urlencode(params)}", None

    async def _token(self, caminho: str, params: dict) -> Tokens:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{HOST_AUTH}{caminho}", params=params)
        if r.status_code >= 400:
            raise ErroCanal(f"TikTok recusou o token ({r.status_code}): {r.text[:300]}")
        j = r.json()
        if j.get("code") not in (0, None):
            raise ErroCanal(f"TikTok token: code {j.get('code')} - {j.get('message', '')}")
        d = j.get("data") or {}
        if not d.get("access_token"):
            raise ErroCanal(f"Resposta sem access_token: {str(j)[:300]}")
        agora = int(_time.time())
        return Tokens(
            access_token=d["access_token"],
            refresh_token=d.get("refresh_token"),
            expira_em_segundos=max(0, int(d.get("access_token_expire_in") or 0) - agora) or None,
            refresh_expira_em_segundos=(
                max(0, int(d.get("refresh_token_expire_in") or 0) - agora) or None
            ),
            extra={},
        )

    async def trocar_codigo(
        self, conta: dict, codigo: str, verificador: str | None
    ) -> Tokens:
        return await self._token(
            "/api/v2/token/get",
            {
                "app_key": conta["client_id"],
                "app_secret": conta["_secret"],
                "auth_code": codigo,
                "grant_type": "authorized_code",
            },
        )

    async def renovar(self, conta: dict) -> Tokens:
        if not conta.get("_refresh"):
            raise ErroCanal("Sem refresh token guardado. Conecte a conta novamente.")
        return await self._token(
            "/api/v2/token/refresh",
            {
                "app_key": conta["client_id"],
                "app_secret": conta["_secret"],
                "refresh_token": conta["_refresh"],
                "grant_type": "refresh_token",
            },
        )

    # ------------------------------------------------------------------
    # leitura
    # ------------------------------------------------------------------
    async def lojas_autorizadas(self, conta: dict) -> list[dict]:
        """Precisa rodar logo depois da autorizacao: e daqui que sai o cipher."""
        d = await self._chamar(
            conta, "/authorization/202309/shops", com_cifra=False
        )
        return d.get("shops") or []

    async def pos_autorizacao(self, conta: dict) -> dict:
        """Guarda o shop_cipher: sem ele as chamadas de pedido nao passam."""
        lojas = await self.lojas_autorizadas(conta)
        if not lojas:
            raise ErroCanal("Nenhuma loja autorizada para este app.")
        if len(lojas) > 1:
            # cada loja precisaria da sua propria linha em contas
            nomes = ", ".join(str(l.get("name")) for l in lojas)
            raise ErroCanal(
                f"Este app tem {len(lojas)} lojas autorizadas ({nomes}). "
                "Cadastre uma conta por loja."
            )
        return {"shop_cipher": lojas[0].get("cipher"), "shop_code": lojas[0].get("code")}

    async def info_conta(self, conta: dict) -> InfoConta:
        lojas = await self.lojas_autorizadas(conta)
        if not lojas:
            raise ErroCanal("Nenhuma loja autorizada para este app.")
        alvo = str(conta.get("loja_id") or "")
        loja = next((l for l in lojas if str(l.get("id")) == alvo), lojas[0])
        return InfoConta(
            loja_id=str(loja.get("id", "")),
            loja_nome=loja.get("name") or "",
            detalhes={
                "Nome da loja": loja.get("name", "-"),
                "Regiao": loja.get("region", "-"),
                "Codigo da loja": loja.get("code", "-"),
                "Tipo": loja.get("seller_type", "-"),
                "Lojas autorizadas neste app": str(len(lojas)),
            },
        )

    def _epoch(self, d: date, fim: bool) -> int:
        h = time(23, 59, 59) if fim else time(0, 0, 0)
        return int(datetime.combine(d, h, tzinfo=FUSO).timestamp())

    async def listar_pedidos(self, conta: dict, inicio: date, fim: date) -> list[Pedido]:
        de, ate = self._epoch(inicio, False), self._epoch(fim, True)

        pedidos: list[Pedido] = []
        pagina = ""
        while True:
            params = {"page_size": 50, "sort_field": "create_time", "sort_order": "DESC"}
            if pagina:
                params["page_token"] = pagina
            d = await self._chamar(
                conta,
                "/order/202309/orders/search",
                extra_params=params,
                corpo={"create_time_ge": de, "create_time_lt": ate},
            )
            for cru in d.get("orders") or []:
                pedidos.append(self._traduzir(cru, conta))
            pagina = d.get("next_page_token") or ""
            if not pagina or len(pedidos) >= 1000:
                break

        await self._marcar_devolucoes(conta, pedidos, de, ate)
        return pedidos

    async def _marcar_devolucoes(
        self, conta: dict, pedidos: list[Pedido], de: int, ate: int
    ) -> None:
        """Cruza a API de devolucao/estorno com os pedidos ja carregados.

        Se essa chamada falhar (permissao do app, por exemplo), os pedidos
        continuam com o status original em vez de o painel inteiro quebrar.
        """
        try:
            d = await self._chamar(
                conta,
                "/return_refund/202309/returns/search",
                extra_params={"page_size": 50},
                corpo={"create_time_ge": de, "create_time_lt": ate},
            )
        except ErroCanal:
            return

        por_pedido = {p.pedido_id: p for p in pedidos}
        for dev in d.get("return_orders") or []:
            alvo = por_pedido.get(str(dev.get("order_id")))
            if not alvo:
                continue
            situacao = (dev.get("return_status") or "").upper()
            if "SUCCESS" not in situacao and "COMPLETE" not in situacao:
                continue
            tipo = (dev.get("return_type") or "").upper()
            alvo.grupo = DEVOLVIDA if "RETURN" in tipo else ESTORNADA
            alvo.status_origem = f"{alvo.status_origem} / {tipo or situacao}"

    def _traduzir(self, cru: dict, conta: dict) -> Pedido:
        status = (cru.get("status") or "").upper()
        grupo = MAPA_STATUS.get(status, PENDENTE)

        # No TikTok cada line_item costuma ser uma unidade; agrupamos por SKU
        # para a tabela nao ficar com dez linhas do mesmo produto.
        por_sku: dict[str, ItemPedido] = {}
        for it in cru.get("line_items") or []:
            sku = it.get("seller_sku") or it.get("sku_id") or it.get("product_id") or "-"
            qtd = int(it.get("quantity") or 1)
            if sku in por_sku:
                por_sku[sku].quantidade += qtd
            else:
                por_sku[sku] = ItemPedido(
                    sku=sku,
                    titulo=it.get("product_name", "-"),
                    quantidade=qtd,
                    valor_unitario=dec(it.get("sale_price")),
                )
        itens = list(por_sku.values())

        pag = cru.get("payment") or {}
        origem = status
        if cru.get("cancel_reason"):
            origem = f"{status} / {cru['cancel_reason']}"

        return Pedido(
            canal=self.nome,
            conta=conta["apelido"],
            pedido_id=str(cru.get("id", "")),
            criado_em=self._quando(cru.get("create_time")),
            grupo=grupo,
            status_origem=origem,
            envio=MAPA_ENVIO.get(status, ENVIO_NAO_APLICA),
            envio_origem=status,
            itens=itens,
            comprador=cru.get("buyer_email") or "-",
            valor_produtos=dec(pag.get("sub_total")) or sum((i.valor_total for i in itens), dec(0)),
            frete=dec(pag.get("shipping_fee")),
            desconto=dec(pag.get("seller_discount")),
            valor_total=dec(pag.get("total_amount")),
            moeda=pag.get("currency") or "BRL",
            url=None,
        )
