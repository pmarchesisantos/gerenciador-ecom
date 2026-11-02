"""Criptografia dos segredos antes de irem para o Supabase.

Motivo: client_secret e refresh_token dao acesso as suas vendas. Se o banco
vazar, eles saem ilegiveis. A chave nunca vai para o banco.

Local: se CHAVE_CRIPTO nao estiver no .env, geramos uma e guardamos em
data/chave.key, para o app subir sem configuracao nenhuma.

Producao: o arquivo NAO serve. Em hospedagem serverless (Vercel) o disco e
efemero, entao uma chave gerada em arquivo desaparece no proximo deploy e
todos os segredos ja gravados ficam ilegiveis. Por isso, em producao,
CHAVE_CRIPTO e obrigatoria e a falta dela falha de forma explicita, em vez
de gerar uma chave nova em silencio.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import CHAVE_CRIPTO, PASTA_DADOS, PRODUCAO

ARQUIVO_CHAVE = PASTA_DADOS / "chave.key"


class ChaveAusente(RuntimeError):
    pass


def gerar_chave() -> str:
    """Util para o comando de setup: python -m app.cripto"""
    return Fernet.generate_key().decode()


def _carregar_chave() -> bytes:
    if CHAVE_CRIPTO:
        return CHAVE_CRIPTO.encode()

    if PRODUCAO:
        raise ChaveAusente(
            "CHAVE_CRIPTO e obrigatoria em producao. Gere uma com "
            "'python -m app.cripto' e cadastre como variavel de ambiente."
        )

    if ARQUIVO_CHAVE.exists():
        return ARQUIVO_CHAVE.read_bytes().strip()
    chave = Fernet.generate_key()
    ARQUIVO_CHAVE.write_bytes(chave)
    return chave


_fernet: Fernet | None = None
_erro_chave: str = ""

try:
    _fernet = Fernet(_carregar_chave())
except Exception as e:
    # nao derruba o import: a pagina /saude precisa subir para explicar o erro
    _erro_chave = str(e)


def chave_ok() -> bool:
    return _fernet is not None


def erro_da_chave() -> str:
    return _erro_chave


def origem_da_chave() -> str:
    if not _fernet:
        return "AUSENTE"
    return "variavel CHAVE_CRIPTO" if CHAVE_CRIPTO else f"arquivo {ARQUIVO_CHAVE.name}"


def proteger(valor: str | None) -> str | None:
    if not valor:
        return None
    if not _fernet:
        raise ChaveAusente(_erro_chave or "Chave de criptografia indisponivel.")
    return _fernet.encrypt(valor.encode()).decode()


def revelar(valor: str | None) -> str | None:
    if not valor or not _fernet:
        return None
    try:
        return _fernet.decrypt(valor.encode()).decode()
    except InvalidToken:
        # chave trocada depois de gravar: o segredo e irrecuperavel
        return None


def mascarar(valor: str | None, visivel: int = 4) -> str:
    """Mostra so o final de um segredo, para conferencia na tela."""
    if not valor:
        return "-"
    if len(valor) <= visivel:
        return "*" * len(valor)
    return "*" * (len(valor) - visivel) + valor[-visivel:]


if __name__ == "__main__":
    print(gerar_chave())
