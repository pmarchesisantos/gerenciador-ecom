"""Shopee (Open Platform v2).

Diferencas em relacao ao Mercado Livre, que explicam o codigo daqui:

1. Toda chamada e assinada (HMAC-SHA256) com a sua Partner Key. A "base
   string" muda entre APIs publicas e APIs de loja - veja _assinar.
2. A Shopee nao devolve um parametro "state", entao embutimos o nosso state
   na propria URL de retorno.
3. get_order_list aceita no maximo 15 dias por chamada, e so devolve os
   codigos dos pedidos. Os valores vem de get_order_detail, em lotes de 50.

Doc: https://open.shopee.com/documents
"""

from __future__ import annotations

import hashlib
import hmac
import time as _time
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

import httpx

from app.canais.base import CanalBase, ErroCanal, dec
from app.config import FUSO, redirect_uri
from app.modelos import (
    CANCELADA,
    DEVOLVIDA,
    EFETIVADA,
    PENDENTE,
    InfoConta,
    ItemPedido,
    Pedido,
    Tokens,
)

HOST_PRODUCAO = "https://partner.shopeemobile.com"
HOST_SANDBOX = "https://partner.test-stable.shopeemobile.com"

MAPA_STATUS = {
    "UNPAID": PENDENTE,
    "READY_TO_SHIP": EFETIVADA,
    "PROCESSED": EFETIVADA,
    "RETRY_SHIP": EFETIVADA,
    "SHIPPED": EFETIVADA,
    "TO_CONFIRM_RECEIVE": EFETIVADA,
    "COMPLETED": EFETIVADA,
    "INVOICE_PENDING": EFETIVADA,
    "IN_CANCEL": CANCELADA,
    "CANCELLED": CANCELADA,
    "TO_RETURN": DEVOLVIDA,
}

CAMPOS_DETALHE = ",".join(
    [
        "order_status",
        "total_amount",
        "buyer_username",
        "item_list",
        "actual_shipping_fee",
        "estimated_shipping_fee",
        "payment_method",
        "cancel_reason",
    ]
)


class Shopee(CanalBase):
    nome = "shopee"
    rotulo = "Shopee"
    rotulo_client_id = "Partner ID"
    rotulo_client_secret = "Partner Key"
    campos_extra = {"ambiente": "Ambiente (producao ou sandbox)"}
    exige_aprovacao = True

    # ------------------------------------------------------------------
    # assinatura
    # ------------------------------------------------------------------
    def _host(self, conta: dict) -> str:
        extra = conta.get("extra") or {}
        if (extra.get("ambiente") or "producao").lower().startswith("sand"):
            return HOST_SANDBOX
        return HOST_PRODUCAO

    def _assinar(self, conta: dict, caminho: str, publico: bool) -> dict:
        """Monta os parametros comuns de query, incluindo a assinatura.

        API publica (autorizacao/token): partner_id + caminho + timestamp
        API de loja: partner_id + caminho + timestamp + access_token + shop_id
        """
        partner_id = str(conta["client_id"])
        agora = int(_time.time())
        base = f"{partner_id}{caminho}{agora}"
        params = {"partner_id": partner_id, "timestamp": agora}

        if not publico:
            token = conta.get("_access") or ""
            loja = str(conta.get("loja_id") or "")
            if not token or not loja:
                raise ErroCanal("Conta Shopee sem access_token ou shop_id. Conecte de novo.")
            base += f"{token}{loja}"
            params["access_token"] = token
            params["shop_id"] = loja

        params["sign"] = hmac.new(
            conta["_secret"].encode(), base.encode(), hashlib.sha256
        ).hexdigest()
        return params

    async def _chamar(
        self,
        conta: dict,
        caminho: str,
        publico: bool = False,
        extra_params: dict | None = None,
        corpo: dict | None = None,
    ) -> dict:
        params = self._assinar(conta, caminho, publico)
        if extra_params:
            params.update(extra_params)
        url = f"{self._host(conta)}{caminho}"
        async with httpx.AsyncClient(timeout=40) as c:
            if corpo is None:
                r = await c.get(url, params=params)
            else:
                r = await c.post(url, params=params, json=corpo)
        if r.status_code >= 400:
            raise ErroCanal(f"Shopee {caminho} HTTP {r.status_code}: {r.text[:300]}")
        j = r.json()
        # A Shopee devolve 200 com campo "error" preenchido quando da errado.
        if j.get("error"):
            raise ErroCanal(f"Shopee {caminho}: {j.get('error')} - {j.get('message', '')}")
        return j

    # ------------------------------------------------------------------
    # autorizacao
    # ------------------------------------------------------------------
    def url_autorizacao(self, conta: dict, state: str) -> tuple[str, str | None]:
        caminho = "/api/v2/shop/auth_partner"
        params = self._assinar(conta, caminho, publico=True)
        # o state vai dentro da propria URL de retorno, porque a Shopee
        # nao repassa parametros extras por conta propria
        params["redirect"] = f"{redirect_uri(self.nome)}?state={state}"
        return f"{self._host(conta)}{caminho}?{urlencode(params)}", None

    async def trocar_codigo(
        self, conta: dict, codigo: str, verificador: str | None
    ) -> Tokens:
        loja = conta.get("loja_id")
        if not loja:
            raise ErroCanal(
                "A Shopee devolve o shop_id junto com o codigo. "
                "Nao recebi o shop_id na URL de retorno."
            )
        j = await self._chamar(
            conta,
            "/api/v2/auth/token/get",
            publico=True,
            corpo={
                "code": codigo,
                "shop_id": int(loja),
                "partner_id": int(conta["client_id"]),
            },
        )
        return self._tokens(j)

    async def renovar(self, conta: dict) -> Tokens:
        if not conta.get("_refresh"):
            raise ErroCanal("Sem refresh token guardado. Conecte a conta novamente.")
        j = await self._chamar(
            conta,
            "/api/v2/auth/access_token/get",
            publico=True,
            corpo={
                "refresh_token": conta["_refresh"],
                "shop_id": int(conta["loja_id"]),
                "partner_id": int(conta["client_id"]),
            },
        )
        return self._tokens(j)

    def _tokens(self, j: dict) -> Tokens:
        if not j.get("access_token"):
            raise ErroCanal(f"Resposta sem access_token: {str(j)[:300]}")
        return Tokens(
            access_token=j["access_token"],
            refresh_token=j.get("refresh_token"),
            expira_em_segundos=j.get("expire_in"),
            # a autorizacao da loja vale 1 ano; depois o lojista reautoriza
            refresh_expira_em_segundos=365 * 24 * 3600,
            extra={"merchant_id": j.get("merchant_id")},
        )

    # ------------------------------------------------------------------
    # leitura
    # ------------------------------------------------------------------
    async def info_conta(self, conta: dict) -> InfoConta:
        j = await self._chamar(conta, "/api/v2/shop/get_shop_info")
        return InfoConta(
            loja_id=str(conta.get("loja_id") or ""),
            loja_nome=j.get("shop_name") or "",
            detalhes={
                "Nome da loja": j.get("shop_name", "-"),
                "Regiao": j.get("region", "-"),
                "Situacao": j.get("status", "-"),
                "Loja oficial": "sim" if j.get("is_sip") else "nao",
                "Autorizada desde": self._texto_data(j.get("auth_time")),
                "Autorizacao expira em": self._texto_data(j.get("expire_time")),
            },
        )

    @staticmethod
    def _texto_data(epoch) -> str:
        if not epoch:
            return "-"
        try:
            return datetime.fromtimestamp(int(epoch), tz=FUSO).strftime("%d/%m/%Y %H:%M")
        except Exception:
            return "-"

    def _janelas(self, inicio: date, fim: date) -> list[tuple[int, int]]:
        """Quebra o periodo em blocos de 15 dias (limite da Shopee)."""
        janelas = []
        atual = inicio
        while atual <= fim:
            termino = min(atual + timedelta(days=14), fim)
            de = int(datetime.combine(atual, time(0, 0, 0), tzinfo=FUSO).timestamp())
            ate = int(datetime.combine(termino, time(23, 59, 59), tzinfo=FUSO).timestamp())
            janelas.append((de, ate))
            atual = termino + timedelta(days=1)
        return janelas

    async def listar_pedidos(self, conta: dict, inicio: date, fim: date) -> list[Pedido]:
        codigos: list[str] = []
        for de, ate in self._janelas(inicio, fim):
            cursor = ""
            while True:
                j = await self._chamar(
                    conta,
                    "/api/v2/order/get_order_list",
                    extra_params={
                        "time_range_field": "create_time",
                        "time_from": de,
                        "time_to": ate,
                        "page_size": 100,
                        "cursor": cursor,
                    },
                )
                resp = j.get("response") or {}
                for item in resp.get("order_list") or []:
                    codigos.append(item["order_sn"])
                if not resp.get("more"):
                    break
                cursor = resp.get("next_cursor") or ""
                if not cursor:
                    break

        pedidos: list[Pedido] = []
        for i in range(0, len(codigos), 50):
            lote = codigos[i : i + 50]
            j = await self._chamar(
                conta,
                "/api/v2/order/get_order_detail",
                extra_params={
                    "order_sn_list": ",".join(lote),
                    "response_optional_fields": CAMPOS_DETALHE,
                },
            )
            for cru in (j.get("response") or {}).get("order_list") or []:
                pedidos.append(self._traduzir(cru, conta))
        return pedidos

    def _traduzir(self, cru: dict, conta: dict) -> Pedido:
        status = (cru.get("order_status") or "").upper()
        grupo = MAPA_STATUS.get(status, PENDENTE)

        itens = []
        for it in cru.get("item_list") or []:
            sku = it.get("model_sku") or it.get("item_sku") or str(it.get("item_id", "-"))
            itens.append(
                ItemPedido(
                    sku=sku,
                    titulo=it.get("item_name", "-"),
                    quantidade=int(it.get("model_quantity_purchased") or 0),
                    valor_unitario=dec(it.get("model_discounted_price")),
                )
            )

        frete = dec(cru.get("actual_shipping_fee") or cru.get("estimated_shipping_fee"))
        origem = status
        if cru.get("cancel_reason"):
            origem = f"{status} / {cru['cancel_reason']}"

        return Pedido(
            canal=self.nome,
            conta=conta["apelido"],
            pedido_id=str(cru.get("order_sn", "")),
            criado_em=self._quando(cru.get("create_time")),
            grupo=grupo,
            status_origem=origem,
            itens=itens,
            comprador=cru.get("buyer_username") or "-",
            valor_produtos=sum((i.valor_total for i in itens), dec(0)),
            frete=frete,
            valor_total=dec(cru.get("total_amount")),
            moeda=cru.get("currency") or "BRL",
            url=None,
        )
