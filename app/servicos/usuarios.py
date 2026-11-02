"""Tabela perfis: quem tem acesso ao painel e com qual papel.

O Supabase Auth guarda e-mail e senha. Aqui fica o resto - nome, papel,
se esta ativo - com o mesmo id do usuario no Auth, para os dois lados
andarem juntos.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app import auth, db


async def listar() -> list[dict]:
    return await db.selecionar("perfis", ordem="criado_em.asc")


async def buscar(usuario_id: str) -> dict | None:
    return await db.um("perfis", {"id": f"eq.{usuario_id}"})


async def por_email(email: str) -> dict | None:
    return await db.um("perfis", {"email": f"eq.{email.strip().lower()}"})


async def existe_algum() -> bool:
    """False significa sistema novo: ninguem tem acesso ainda."""
    return bool(await db.selecionar("perfis", {"limit": "1"}))


async def criar(
    email: str, senha: str, nome: str, papel: str = auth.LEITOR
) -> dict:
    """Cria no Supabase Auth e no perfil, nessa ordem."""
    if papel not in auth.FORCA:
        papel = auth.LEITOR
    if await por_email(email):
        raise auth.ErroAuth("Já existe alguém com esse e-mail no sistema.")

    usuario = await auth.criar_usuario(email, senha)
    try:
        return await db.inserir(
            "perfis",
            {
                "id": usuario["id"],
                "email": email.strip().lower(),
                "nome": nome.strip() or email.split("@")[0],
                "papel": papel,
                "ativo": True,
            },
        )
    except Exception:
        # nao deixa usuario orfao no Auth se o perfil falhar
        try:
            await auth.apagar_usuario(usuario["id"])
        except Exception:
            pass
        raise


async def criar_primeiro_dono(email: str, senha: str, nome: str) -> dict:
    """So funciona enquanto nao existir nenhum perfil."""
    if await existe_algum():
        raise auth.ErroAuth(
            "O primeiro acesso já foi criado. Entre com ele, ou peça a quem é dono "
            "para cadastrar o seu."
        )
    return await criar(email, senha, nome, auth.DONO)


async def alterar_papel(usuario_id: str, papel: str, quem: dict) -> None:
    if papel not in auth.FORCA:
        raise auth.ErroAuth("Papel inválido.")
    alvo = await buscar(usuario_id)
    if not alvo:
        raise auth.ErroAuth("Usuário não encontrado.")
    if alvo["papel"] == auth.DONO and papel != auth.DONO:
        await _conferir_ultimo_dono(usuario_id)
    await db.atualizar("perfis", {"id": f"eq.{usuario_id}"}, {"papel": papel})
    await db.registrar_evento(
        "acesso",
        f"{quem['email']} mudou o papel de {alvo['email']} para {papel}",
    )


async def alterar_ativo(usuario_id: str, ativo: bool, quem: dict) -> None:
    alvo = await buscar(usuario_id)
    if not alvo:
        raise auth.ErroAuth("Usuário não encontrado.")
    if not ativo:
        if alvo["id"] == quem["id"]:
            raise auth.ErroAuth("Você não pode desativar o seu próprio acesso.")
        if alvo["papel"] == auth.DONO:
            await _conferir_ultimo_dono(usuario_id)
    await db.atualizar("perfis", {"id": f"eq.{usuario_id}"}, {"ativo": ativo})
    await db.registrar_evento(
        "acesso",
        f"{quem['email']} {'ativou' if ativo else 'desativou'} {alvo['email']}",
    )


async def remover(usuario_id: str, quem: dict) -> None:
    alvo = await buscar(usuario_id)
    if not alvo:
        return
    if alvo["id"] == quem["id"]:
        raise auth.ErroAuth("Você não pode remover o seu próprio acesso.")
    if alvo["papel"] == auth.DONO:
        await _conferir_ultimo_dono(usuario_id)
    await auth.apagar_usuario(usuario_id)
    await db.apagar("perfis", {"id": f"eq.{usuario_id}"})
    await db.registrar_evento("acesso", f"{quem['email']} removeu {alvo['email']}")


async def _conferir_ultimo_dono(usuario_id: str) -> None:
    """Impede o sistema de ficar sem ninguem que possa administrar."""
    donos = await db.selecionar("perfis", {"papel": "eq.dono", "ativo": "is.true"})
    restantes = [d for d in donos if d["id"] != usuario_id]
    if not restantes:
        raise auth.ErroAuth(
            "Este é o único dono ativo. Promova outra pessoa a dono antes de "
            "mudar ou remover este acesso."
        )


async def marcar_acesso(usuario_id: str) -> None:
    try:
        await db.atualizar(
            "perfis",
            {"id": f"eq.{usuario_id}"},
            {"ultimo_acesso": datetime.now(timezone.utc).isoformat()},
        )
    except Exception:
        pass
