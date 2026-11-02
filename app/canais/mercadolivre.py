"""Mercado Livre.

O mais simples dos tres: voce cria a aplicacao sozinho no DevCenter, sem
aprovacao humana, e ja pode autorizar a sua propria conta.

Doc: https://developers.mercadolivre.com.br/pt_br/autenticacao-e-autorizacao
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import date, datetime, time
from urllib.parse import urlencode

import httpx

from app.canais.base import CanalBase, ErroCanal, dec
from app.config import FUSO, redirect_uri
from app.modelos import (
    CANCELADA,
    EFETIVADA,
    ESTORNADA,
    PENDENTE,
    InfoConta,
    ItemPedido,
    Pedido,
    Tokens,
)

AUTORIZACAO = "https://auth.mercadolivre.com.br/authorization"
API = "https://api.mercadolibre.com"

# status do pedido -> grupo do nosso painel
MAPA_STATUS = {
    "paid": EFETIVADA,
    "confirmed": PENDENTE,
    "payment_required": PENDENTE,
    "payment_in_process": PENDENTE,
    "partially_paid": PENDENTE,
    "cancelled": CANCELADA,
    "invalid": CANCELADA,
}

# status de pagamento que mandam mais que o status do pedido
PAGAMENTO_ESTORNO = {"refunded", "charged_back"}


class MercadoLivre(CanalBase):
    nome = "mercadolivre"
    rotulo = "Mercado Livre"
    rotulo_client_id = "App ID"
    rotulo_client_secret = "Secret Key"
    exige_aprovacao = False

    # ------------------------------------------------------------------
    # autorizacao (OAuth 2.0 + PKCE)
    # ------------------------------------------------------------------
    def url_autorizacao(self, conta: dict, state: str) -> tuple[str, str | None]:
        # PKCE: geramos um segredo aleatorio (verificador), mandamos o hash
        # dele na ida e o segredo em si na troca do codigo. Se a aplicacao
        # estiver com PKCE desligado no DevCenter, mandar isso nao atrapalha.
        verificador = secrets.token_urlsafe(64)
        desafio = (
            base64.urlsafe_b64encode(hashlib.sha256(verificador.encode()).digest())
            .decode()
            .rstrip("=")
        )
        params = {
            "response_type": "code",
            "client_id": conta["client_id"],
            "redirect_uri": redirect_uri(self.nome),
            "state": state,
            "code_challenge": desafio,
            "code_challenge_method": "S256",
        }
        return f"{AUTORIZACAO}?{urlencode(params)}", verificador

    async def _token(self, dados: dict) -> Tokens:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{API}/oauth/token",
                data=dados,
                headers={
                    "accept": "application/json",
                    "content-type": "application/x-www-form-urlencoded",
                },
            )
        if r.status_code >= 400:
            raise ErroCanal(
                f"Mercado Livre recusou o token ({r.status_code}): {r.text[:300]}"
            )
        j = r.json()
        if "access_token" not in j:
            raise ErroCanal(f"Resposta sem access_token: {str(j)[:300]}")
        return Tokens(
            access_token=j["access_token"],
            refresh_token=j.get("refresh_token"),
            expira_em_segundos=j.get("expires_in"),
            # O refresh token do ML e de uso unico: cada renovacao devolve um
            # novo e invalida o anterior. A validade de 6 meses abaixo e a
            # documentada; confirme na sua aplicacao.
            refresh_expira_em_segundos=180 * 24 * 3600,
            extra={"escopos": j.get("scope", ""), "usuario_id": j.get("user_id")},
        )

    async def trocar_codigo(
        self, conta: dict, codigo: str, verificador: str | None
    ) -> Tokens:
        dados = {
            "grant_type": "authorization_code",
            "client_id": conta["client_id"],
            "client_secret": conta["_secret"],
            "code": codigo,
            "redirect_uri": redirect_uri(self.nome),
        }
        if verificador:
            dados["code_verifier"] = verificador
        return await self._token(dados)

    async def renovar(self, conta: dict) -> Tokens:
        if not conta.get("_refresh"):
            raise ErroCanal("Sem refresh token guardado. Conecte a conta novamente.")
        return await self._token(
            {
                "grant_type": "refresh_token",
                "client_id": conta["client_id"],
                "client_secret": conta["_secret"],
                "refresh_token": conta["_refresh"],
            }
        )

    # ------------------------------------------------------------------
    # leitura
    # ------------------------------------------------------------------
    async def _get(self, conta: dict, caminho: str, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.get(
                f"{API}{caminho}",
                params=params,
                headers={"Authorization": f"Bearer {conta['_access']}"},
            )
        if r.status_code == 401:
            raise ErroCanal("Token expirado ou invalido (401). Renove a conta.")
        if r.status_code >= 400:
            raise ErroCanal(f"GET {caminho} falhou ({r.status_code}): {r.text[:300]}")
        return r.json()

    async def info_conta(self, conta: dict) -> InfoConta:
        u = await self._get(conta, "/users/me")
        reputacao = (u.get("seller_reputation") or {}).get("level_id") or "-"
        nome = " ".join(filter(None, [u.get("first_name"), u.get("last_name")]))
        return InfoConta(
            loja_id=str(u.get("id", "")),
            loja_nome=u.get("nickname") or nome or "",
            detalhes={
                "Apelido no ML": u.get("nickname", "-"),
                "Nome": nome or "-",
                "E-mail": u.get("email", "-"),
                "Pais": u.get("country_id") or "-",
                "Tipo de conta": u.get("user_type") or "-",
                "Reputacao de vendedor": reputacao,
                "Situacao no site": (u.get("status") or {}).get("site_status", "-"),
            },
        )

    def _momento(self, d: date, fim: bool) -> str:
        """ML pede data com fuso explicito e milissegundos."""
        h = time(23, 59, 59) if fim else time(0, 0, 0)
        return datetime.combine(d, h, tzinfo=FUSO).strftime("%Y-%m-%dT%H:%M:%S.000%z")

    async def listar_pedidos(self, conta: dict, inicio: date, fim: date) -> list[Pedido]:
        if not conta.get("loja_id"):
            raise ErroCanal("Conta sem seller_id. Conecte a conta para preencher.")

        pedidos: list[Pedido] = []
        offset, limite = 0, 50
        while True:
            j = await self._get(
                conta,
                "/orders/search",
                {
                    "seller": conta["loja_id"],
                    "order.date_created.from": self._momento(inicio, False),
                    "order.date_created.to": self._momento(fim, True),
                    "sort": "date_desc",
                    "offset": offset,
                    "limit": limite,
                },
            )
            lote = j.get("results") or []
            for cru in lote:
                pedidos.append(self._traduzir(cru, conta))
            total = (j.get("paging") or {}).get("total", 0)
            offset += limite
            # teto de seguranca: 1000 pedidos por consulta
            if not lote or offset >= total or offset >= 1000:
                break
        return pedidos

    def _traduzir(self, cru: dict, conta: dict) -> Pedido:
        status = (cru.get("status") or "").lower()
        grupo = MAPA_STATUS.get(status, PENDENTE)

        pagamentos = cru.get("payments") or []
        situacoes = {(p.get("status") or "").lower() for p in pagamentos}
        estornos = situacoes & PAGAMENTO_ESTORNO
        # Pagamento estornado manda mais que o status do pedido: o ML costuma
        # manter o pedido como "paid" e registrar o reembolso no pagamento.
        if estornos:
            grupo = ESTORNADA

        frete = sum((dec(p.get("shipping_cost")) for p in pagamentos), dec(0))

        itens = []
        for it in cru.get("order_items") or []:
            info = it.get("item") or {}
            sku = (
                info.get("seller_sku")
                or info.get("seller_custom_field")
                or info.get("id")
                or "-"
            )
            itens.append(
                ItemPedido(
                    sku=sku,
                    titulo=info.get("title", "-"),
                    quantidade=int(it.get("quantity") or 0),
                    valor_unitario=dec(it.get("unit_price")),
                )
            )

        detalhe = cru.get("status_detail")
        origem = status if not detalhe else f"{status} / {detalhe}"
        if estornos:
            origem += " (pagamento " + "/".join(sorted(estornos)) + ")"

        return Pedido(
            canal=self.nome,
            conta=conta["apelido"],
            pedido_id=str(cru.get("id", "")),
            criado_em=self._quando(cru.get("date_created")),
            grupo=grupo,
            status_origem=origem,
            itens=itens,
            comprador=(cru.get("buyer") or {}).get("nickname") or "-",
            valor_produtos=sum((i.valor_total for i in itens), dec(0)),
            frete=frete,
            desconto=dec((cru.get("coupon") or {}).get("amount")),
            valor_total=dec(cru.get("total_amount")),
            moeda=cru.get("currency_id") or "BRL",
            url=f"https://www.mercadolivre.com.br/vendas/{cru.get('id')}/detalhe",
        )
