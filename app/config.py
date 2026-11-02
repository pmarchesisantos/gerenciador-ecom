"""Configuracao lida do .env (local) ou das variaveis de ambiente (Vercel)."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent

# Em serverless o disco e somente leitura fora de /tmp, entao nem tentamos
# criar a pasta de dados: ela so existe no uso local.
EM_SERVERLESS = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))

PASTA_DADOS = RAIZ / "data"
if not EM_SERVERLESS:
    try:
        PASTA_DADOS.mkdir(exist_ok=True)
    except OSError:
        pass
    load_dotenv(RAIZ / ".env")


def _texto(nome: str, padrao: str = "") -> str:
    return (os.getenv(nome) or padrao).strip()


# --- Supabase --------------------------------------------------------------
SUPABASE_URL = _texto("SUPABASE_URL").rstrip("/")
SUPABASE_SERVICE_KEY = _texto("SUPABASE_SERVICE_KEY")
SUPABASE_ANON_KEY = _texto("SUPABASE_ANON_KEY")

# --- aplicacao -------------------------------------------------------------
BASE_URL = _texto("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
CHAVE_CRIPTO = _texto("CHAVE_CRIPTO")
CHAVE_SESSAO = _texto("CHAVE_SESSAO")

AMBIENTE = _texto("AMBIENTE", "producao" if EM_SERVERLESS else "local").lower()
PRODUCAO = AMBIENTE.startswith("prod")

NOME_FUSO = _texto("FUSO", "America/Sao_Paulo")
try:
    FUSO = ZoneInfo(NOME_FUSO)
except Exception:  # fuso invalido ou tzdata ausente
    FUSO = ZoneInfo("UTC")
    NOME_FUSO = "UTC (fuso configurado nao encontrado)"

# segundos que uma consulta fica em cache antes de bater na API de novo.
# Em serverless o cache vive pouco: cada instancia fria comeca vazia.
CACHE_SEGUNDOS = 60

# duracao da sessao de login, em segundos
SESSAO_SEGUNDOS = 7 * 24 * 3600


def supabase_configurado() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)


def auth_configurado() -> bool:
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)


def redirect_uri(canal: str) -> str:
    """URL que voce precisa cadastrar na plataforma como redirect/callback."""
    return f"{BASE_URL}/retorno/{canal}"


def problemas_criticos() -> list[str]:
    """Erros que impedem o app de funcionar com seguranca em producao."""
    faltando = []
    if PRODUCAO:
        if not CHAVE_CRIPTO:
            faltando.append(
                "CHAVE_CRIPTO nao definida. Em producao a chave precisa vir de variavel "
                "de ambiente: o disco e efemero e um arquivo gerado some no proximo "
                "deploy, tornando ilegiveis todos os segredos ja guardados."
            )
        if not CHAVE_SESSAO:
            faltando.append(
                "CHAVE_SESSAO nao definida. Sem ela os cookies de login nao podem ser "
                "assinados de forma estavel entre instancias."
            )
        if BASE_URL.startswith("http://"):
            faltando.append(
                f"BASE_URL esta em http ({BASE_URL}). Em producao precisa ser https."
            )
    if not auth_configurado():
        faltando.append(
            "SUPABASE_ANON_KEY nao definida. E ela que permite validar e-mail e senha "
            "no Supabase Auth."
        )
    return faltando


def diagnostico() -> list[dict]:
    """Lista usada pela pagina /saude para mostrar o que falta configurar."""
    return [
        {
            "item": "SUPABASE_URL",
            "ok": bool(SUPABASE_URL),
            "valor": SUPABASE_URL or "(vazio)",
            "ajuda": "Supabase > Project Settings > Data API > Project URL",
        },
        {
            "item": "SUPABASE_SERVICE_KEY",
            "ok": bool(SUPABASE_SERVICE_KEY),
            "valor": "definida" if SUPABASE_SERVICE_KEY else "(vazio)",
            "ajuda": "Project Settings > API Keys > service_role (secreta)",
        },
        {
            "item": "SUPABASE_ANON_KEY",
            "ok": bool(SUPABASE_ANON_KEY),
            "valor": "definida" if SUPABASE_ANON_KEY else "(vazio)",
            "ajuda": "Project Settings > API Keys > anon / publishable. Usada no login.",
        },
        {
            "item": "CHAVE_CRIPTO",
            "ok": bool(CHAVE_CRIPTO) or not PRODUCAO,
            "valor": "definida" if CHAVE_CRIPTO else "gerada em arquivo (so vale local)",
            "ajuda": "Obrigatoria em producao. Gere uma vez e guarde no provedor.",
        },
        {
            "item": "CHAVE_SESSAO",
            "ok": bool(CHAVE_SESSAO) or not PRODUCAO,
            "valor": "definida" if CHAVE_SESSAO else "temporaria (so vale local)",
            "ajuda": "Assina o cookie de login. Obrigatoria em producao.",
        },
        {
            "item": "BASE_URL",
            "ok": bool(BASE_URL) and (BASE_URL.startswith("https://") or not PRODUCAO),
            "valor": BASE_URL,
            "ajuda": "Endereco publico do app. Entra no redirect URI das plataformas.",
        },
        {
            "item": "Ambiente",
            "ok": True,
            "valor": AMBIENTE,
            "ajuda": "Detectado automaticamente na Vercel; force com AMBIENTE no .env.",
        },
        {
            "item": "Fuso horario",
            "ok": NOME_FUSO.startswith("America") or NOME_FUSO == "UTC",
            "valor": NOME_FUSO,
            "ajuda": "Usado para agrupar pedidos por dia.",
        },
    ]
