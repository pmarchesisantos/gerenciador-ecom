"""Mercado Livre.

O mais simples dos tres: voce cria a aplicacao sozinho no DevCenter, sem
aprovacao humana, e ja pode autorizar a sua propria conta.

Doc: https://developers.mercadolivre.com.br/pt_br/autenticacao-e-autorizacao
"""

from __future__ import annotations

import asyncio
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
    Etiquetas,
    EFETIVADA,
    ENVIO_A_ENVIAR,
    ENVIO_DESCONHECIDO,
    ENVIO_ENTREGUE,
    ENVIO_ENVIADO,
    ENVIO_NAO_APLICA,
    ENVIO_PROBLEMA,
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

# status do envio (recurso separado) -> dimensao de envio do painel
MAPA_ENVIO = {
    "pending": ENVIO_A_ENVIAR,
    "handling": ENVIO_A_ENVIAR,
    "ready_to_ship": ENVIO_A_ENVIAR,
    "shipped": ENVIO_ENVIADO,
    "delivered": ENVIO_ENTREGUE,
    "not_delivered": ENVIO_PROBLEMA,
    "cancelled": ENVIO_NAO_APLICA,
    "to_be_agreed": ENVIO_NAO_APLICA,
}

# teto de chamadas extras por consulta, para nao estourar o tempo da requisicao
TETO_ENVIOS = 200
SIMULTANEAS = 8

# so da para imprimir etiqueta de envio nesta situacao; nos outros a
# plataforma ainda nao gerou o documento
ENVIO_IMPRIMIVEL = "ready_to_ship"

# o ML aceita varios envios na mesma chamada e devolve um PDF unico;
# acima disso ele recusa o lote inteiro
TETO_ETIQUETAS = 50


class MercadoLivre(CanalBase):
    nome = "mercadolivre"
    rotulo = "Mercado Livre"
    rotulo_client_id = "App ID"
    rotulo_client_secret = "Secret Key"
    exige_aprovacao = False
    gera_etiqueta = True

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
    # etiquetas
    # ------------------------------------------------------------------
    async def etiquetas(self, conta: dict, pedidos: list) -> Etiquetas:
        """Baixa as etiquetas de varios envios em um PDF so.

        O Mercado Livre aceita a lista inteira numa chamada e devolve um
        documento unico - nao precisamos juntar PDF nenhum aqui.

        So entram envios em ready_to_ship: nos demais a etiqueta ainda nao
        existe, e mandar um deles faz o ML recusar o lote todo. Os que ficam
        de fora voltam na resposta para a tela explicar o motivo.
        """
        prontos, fora = [], []
        for p in pedidos:
            if p.envio_id and (p.envio_origem or "").startswith(ENVIO_IMPRIMIVEL):
                prontos.append(p)
            else:
                fora.append(p.pedido_id)

        if not prontos:
            raise ErroCanal(
                "Nenhum dos pedidos escolhidos tem etiqueta disponivel. No "
                "Mercado Livre ela so aparece quando o envio chega em "
                "'ready_to_ship' - antes disso a plataforma ainda nao gerou o "
                "documento."
            )
        if len(prontos) > TETO_ETIQUETAS:
            raise ErroCanal(
                f"Selecione no maximo {TETO_ETIQUETAS} pedidos por impressao. "
                f"Voce escolheu {len(prontos)}."
            )

        ids = ",".join(p.envio_id for p in prontos)
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get(
                f"{API}/shipment_labels",
                params={"shipment_ids": ids, "response_type": "pdf"},
                headers={"Authorization": f"Bearer {conta['_access']}"},
            )

        if r.status_code == 401:
            raise ErroCanal("Token expirado ou invalido (401). Renove a conta.")
        if r.status_code == 403:
            raise ErroCanal(
                "O Mercado Livre recusou por permissao (403). Confira o escopo "
                "de 'Venda e envios' no DevCenter e clique em Reautorizar."
            )
        if r.status_code >= 400:
            raise ErroCanal(self._erro_etiqueta(r))

        tipo = r.headers.get("content-type", "application/pdf").split(";")[0]
        if "pdf" not in tipo:
            # as vezes o erro vem com 200 e corpo JSON
            raise ErroCanal(f"Resposta inesperada do Mercado Livre: {r.text[:300]}")

        hoje = datetime.now(tz=FUSO).strftime("%Y-%m-%d")
        return Etiquetas(
            conteudo=r.content,
            tipo="application/pdf",
            nome=f"etiquetas-mercadolivre-{hoje}.pdf",
            impressas=len(prontos),
            ignorados=fora,
            motivo_ignorados=(
                "a etiqueta so fica disponivel quando o envio chega em "
                "'ready_to_ship'"
            ),
        )

    @staticmethod
    def _erro_etiqueta(r: httpx.Response) -> str:
        """Traduz o erro do /shipment_labels, que vem detalhado por envio.

        O corpo traz failed_shipments[], cada um com o pedido e o motivo. Sem
        isso a tela mostraria so "400", e o motivo mais comum - o envio ainda
        (ou ja) nao estar em situacao de imprimir - ficaria invisivel.
        """
        try:
            j = r.json()
        except Exception:
            return f"Nao consegui baixar as etiquetas ({r.status_code}): {r.text[:300]}"

        falhas = j.get("failed_shipments") or []
        if not falhas:
            return (
                f"Nao consegui baixar as etiquetas ({r.status_code}): "
                f"{j.get('message') or r.text[:300]}"
            )

        nao_imprimivel = [f for f in falhas if f.get("cause") == "NOT_PRINTABLE_STATUS"]
        if len(nao_imprimivel) == len(falhas):
            pedidos = ", ".join(str(f.get("order_id")) for f in falhas[:5])
            resto = f" e mais {len(falhas) - 5}" if len(falhas) > 5 else ""
            return (
                f"O Mercado Livre nao imprime etiqueta destes envios no estado "
                f"atual: pedido(s) {pedidos}{resto}. Isso acontece quando o "
                f"envio ja saiu ou ja foi entregue - a etiqueta so existe "
                f"enquanto ele esta aguardando despacho."
            )

        linhas = [
            f"pedido {f.get('order_id')}: {f.get('message') or f.get('cause')}"
            for f in falhas[:4]
        ]
        return "O Mercado Livre recusou " + str(len(falhas)) + " envio(s). " + "; ".join(linhas)

    # ------------------------------------------------------------------
    # categoria e campos do anuncio
    #
    # Duas chamadas de leitura que o ML oferece sem custo nenhum e que
    # resolvem a pergunta "quais campos este anuncio pede": ele preve a
    # categoria a partir do titulo e, dada a categoria, lista os atributos
    # com os obrigatorios marcados. E a fonte da verdade - a lista varia por
    # categoria e muda sem avisar, entao nao da para chumbar no codigo.
    # ------------------------------------------------------------------
    async def sugerir_categoria(self, conta: dict, titulo: str) -> list[dict]:
        """Categorias que o ML acha que combinam com este titulo, em ordem."""
        texto = (titulo or "").strip()
        if len(texto) < 3:
            raise ErroCanal("Escreva o titulo do produto antes de pedir a sugestao.")

        j = await self._get(
            conta,
            "/sites/MLB/domain_discovery/search",
            {"q": texto[:200], "limit": 8},
        )
        if not isinstance(j, list):
            return []

        saida = []
        for item in j:
            cid = item.get("category_id")
            if not cid:
                continue
            saida.append(
                {
                    "categoria_id": cid,
                    "nome": item.get("category_name") or cid,
                    "dominio": item.get("domain_name") or "",
                    # o ML ja devolve o que conseguiu adivinhar do titulo
                    "adivinhados": {
                        a.get("id"): a.get("value_name")
                        for a in (item.get("attributes") or [])
                        if a.get("id") and a.get("value_name")
                    },
                }
            )
        return saida

    async def atributos_categoria(self, conta: dict, categoria_id: str) -> dict:
        """Nome, caminho e lista de atributos de uma categoria."""
        cid = (categoria_id or "").strip()
        if not cid:
            raise ErroCanal("Informe a categoria.")

        categoria = await self._get(conta, f"/categories/{cid}")
        atributos = await self._get(conta, f"/categories/{cid}/attributes")
        caminho = " > ".join(
            p.get("name", "") for p in (categoria.get("path_from_root") or [])
        )
        return {
            "categoria_id": cid,
            "nome": categoria.get("name") or cid,
            "caminho": caminho or categoria.get("name") or cid,
            "atributos": atributos if isinstance(atributos, list) else [],
        }

    # ------------------------------------------------------------------
    # anuncios ja publicados (para importar o que ja existe na loja)
    # ------------------------------------------------------------------
    async def listar_anuncios(self, conta: dict, teto: int = 200) -> list[dict]:
        """Os anuncios da loja, com variacoes e atributos, ja normalizados.

        Duas chamadas por lote: a busca devolve so os codigos, e o multiget
        (/items?ids=) traz os detalhes de ate 20 de uma vez. Com um anuncio
        por chamada isso levaria minutos e estouraria o tempo da Vercel.
        """
        if not conta.get("loja_id"):
            raise ErroCanal("Conta sem seller_id. Conecte a conta novamente.")

        codigos: list[str] = []
        offset = 0
        while len(codigos) < teto:
            j = await self._get(
                conta,
                f"/users/{conta['loja_id']}/items/search",
                {"status": "active", "limit": 50, "offset": offset},
            )
            lote = j.get("results") or []
            codigos.extend(lote)
            total = (j.get("paging") or {}).get("total", 0)
            offset += 50
            if not lote or offset >= total:
                break
        codigos = codigos[:teto]
        if not codigos:
            return []

        anuncios: list[dict] = []
        for i in range(0, len(codigos), 20):
            fatia = codigos[i : i + 20]
            resposta = await self._get(conta, "/items", {"ids": ",".join(fatia)})
            for envelope in resposta if isinstance(resposta, list) else []:
                if envelope.get("code") != 200:
                    continue
                corpo = envelope.get("body") or {}
                if corpo.get("id"):
                    anuncios.append(self._traduzir_anuncio(corpo))
        return anuncios

    @staticmethod
    def _traduzir_anuncio(item: dict) -> dict:
        """Item do ML -> o formato que o catalogo daqui entende."""
        atributos = {
            a.get("id"): a.get("value_name")
            for a in (item.get("attributes") or [])
            if a.get("id") and a.get("value_name")
        }

        # nos eixos, o ML chama de attribute_combinations o que para nos e
        # Variacao1/Variacao2 - cor e tamanho, na pratica
        eixos: list[str] = []
        variacoes = []
        for v in item.get("variations") or []:
            combinacao = v.get("attribute_combinations") or []
            for c in combinacao:
                nome = c.get("name")
                if nome and nome not in eixos:
                    eixos.append(nome)
            valores = {c.get("name"): c.get("value_name") for c in combinacao}
            variacoes.append(
                {
                    "anuncio_variacao_id": str(v.get("id") or ""),
                    "sku": (v.get("seller_custom_field") or "").strip(),
                    "valores": valores,
                    "preco": v.get("price"),
                    "estoque": int(v.get("available_quantity") or 0),
                }
            )

        return {
            "anuncio_id": item.get("id"),
            "titulo": (item.get("title") or "").strip(),
            "sku": (item.get("seller_custom_field") or "").strip(),
            "preco": item.get("price"),
            "estoque": int(item.get("available_quantity") or 0),
            "condicao": "novo" if item.get("condition") == "new" else "usado",
            "categoria_id": item.get("category_id") or "",
            "marca": atributos.get("BRAND") or "",
            "atributos": atributos,
            "eixos": eixos[:2],
            "variacoes": variacoes,
            "fotos": [
                f.get("secure_url") or f.get("url")
                for f in (item.get("pictures") or [])
                if f.get("secure_url") or f.get("url")
            ][:10],
            "url": item.get("permalink") or "",
        }

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

    async def enriquecer_envio(self, conta: dict, pedidos: list) -> None:
        """Busca o estado de cada envio.

        O Mercado Livre nao devolve isso junto do pedido: e preciso uma
        chamada por envio. Por isso roda so quando a tela de pedidos pede,
        em paralelo e com teto - a tela do painel nao paga esse custo.
        """
        alvos = [
            p for p in pedidos
            if p.envio == ENVIO_DESCONHECIDO and p.envio_id
        ][:TETO_ENVIOS]
        if not alvos:
            return

        porteiro = asyncio.Semaphore(SIMULTANEAS)

        async def um(pedido) -> None:
            async with porteiro:
                try:
                    j = await self._get(conta, f"/shipments/{pedido.envio_id}")
                except ErroCanal:
                    return  # fica "nao informado" em vez de derrubar a tela
            situacao = (j.get("status") or "").lower()
            detalhe = j.get("substatus") or ""
            pedido.envio = MAPA_ENVIO.get(situacao, ENVIO_DESCONHECIDO)
            pedido.envio_origem = f"{situacao}/{detalhe}" if detalhe else situacao
            pedido.etiqueta_pronta = situacao == ENVIO_IMPRIMIVEL

        await asyncio.gather(*(um(p) for p in alvos))

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
                    sku_variacao=str(info.get("variation_id") or ""),
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
            envio=ENVIO_NAO_APLICA if grupo in (CANCELADA, PENDENTE) else ENVIO_DESCONHECIDO,
            envio_id=str((cru.get("shipping") or {}).get("id") or ""),
            comprador=(cru.get("buyer") or {}).get("nickname") or "-",
            valor_produtos=sum((i.valor_total for i in itens), dec(0)),
            frete=frete,
            desconto=dec((cru.get("coupon") or {}).get("amount")),
            valor_total=dec(cru.get("total_amount")),
            moeda=cru.get("currency_id") or "BRL",
            url=f"https://www.mercadolivre.com.br/vendas/{cru.get('id')}/detalhe",
        )
