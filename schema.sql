-- ===========================================================================
-- Sistema-Ecom - estrutura completa no Supabase
--
-- Como aplicar:
--   Supabase > SQL Editor > New query > cole TUDO > Run
--
-- Pode rodar mais de uma vez sem problema: tudo usa "if not exists".
--
-- O que guardamos aqui: as credenciais de acesso das plataformas e quem tem
-- acesso ao painel. Pedidos nao sao gravados: sao consultados ao vivo nas APIs.
-- ===========================================================================

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- contas: uma linha por loja conectada (voce pode ter varias por canal)
-- ---------------------------------------------------------------------------
create table if not exists public.contas (
    id                  uuid primary key default gen_random_uuid(),
    canal               text not null check (canal in ('mercadolivre', 'shopee', 'tiktok')),
    apelido             text not null,

    -- credenciais do SEU aplicativo na plataforma
    client_id           text not null,          -- ML: App ID | Shopee: Partner ID | TikTok: App Key
    client_secret_cripto text not null,          -- guardado criptografado

    -- identificacao da loja (preenchida depois da autorizacao)
    loja_id             text,                   -- ML: seller_id | Shopee: shop_id | TikTok: shop_id
    loja_nome           text,

    -- tokens de acesso (criptografados)
    access_token_cripto  text,
    refresh_token_cripto text,
    access_expira_em     timestamptz,
    refresh_expira_em    timestamptz,

    -- campos especificos por canal (shop_cipher do TikTok, regiao da Shopee, ...)
    extra               jsonb not null default '{}'::jsonb,

    ativa               boolean not null default true,
    criada_em           timestamptz not null default now(),
    atualizada_em       timestamptz not null default now(),

    unique (canal, apelido)
);

create index if not exists contas_canal_idx on public.contas (canal);

-- ---------------------------------------------------------------------------
-- oauth_estados: guarda o "state" e o code_verifier (PKCE) entre o clique
-- em "Conectar" e a volta da plataforma. Linhas velhas podem ser apagadas.
-- ---------------------------------------------------------------------------
create table if not exists public.oauth_estados (
    state       text primary key,
    conta_id    uuid not null references public.contas (id) on delete cascade,
    canal       text not null,
    verificador text,
    criado_em   timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- eventos: trilha do que aconteceu com cada conta (autorizou, renovou, falhou)
-- E o que deixa o painel explicar "por que esta conta parou de funcionar".
-- ---------------------------------------------------------------------------
create table if not exists public.eventos (
    id        bigserial primary key,
    conta_id  uuid references public.contas (id) on delete cascade,
    canal     text,
    tipo      text not null,   -- autorizacao | renovacao | consulta | erro
    detalhe   text,
    criado_em timestamptz not null default now()
);

create index if not exists eventos_conta_idx on public.eventos (conta_id, criado_em desc);

-- ---------------------------------------------------------------------------
-- Seguranca: ninguem acessa estas tabelas com a chave publica (anon).
-- O app usa a chave service_role, que passa por cima da RLS.
-- Como nao criamos nenhuma policy, a chave anon nao le nem escreve nada.
-- ---------------------------------------------------------------------------
alter table public.contas        enable row level security;
alter table public.oauth_estados enable row level security;
alter table public.eventos       enable row level security;

-- gatilho para manter atualizada_em em dia
create or replace function public.toca_atualizada_em()
returns trigger language plpgsql as $$
begin
    new.atualizada_em = now();
    return new;
end;
$$;

drop trigger if exists contas_toca on public.contas;
create trigger contas_toca before update on public.contas
    for each row execute function public.toca_atualizada_em();


-- ---------------------------------------------------------------------------
-- perfis: quem tem acesso ao painel e com qual papel.
-- O Supabase Auth (tabela auth.users, que ele mesmo cria) guarda e-mail e
-- senha. Esta tabela guarda o resto. O id e o mesmo nos dois lados.
-- ---------------------------------------------------------------------------
create table if not exists public.perfis (
    id            uuid primary key references auth.users (id) on delete cascade,
    email         text not null unique,
    nome          text,
    papel         text not null default 'leitor'
                  check (papel in ('dono', 'operador', 'leitor')),
    ativo         boolean not null default true,
    criado_em     timestamptz not null default now(),
    ultimo_acesso timestamptz
);

create index if not exists perfis_papel_idx on public.perfis (papel) where ativo;

-- quem fez o quê: a trilha de eventos passa a registrar o responsável
alter table public.eventos add column if not exists usuario_id    uuid;
alter table public.eventos add column if not exists usuario_email text;

-- A tabela contas continua sem dono: todo mundo que entra enxerga as mesmas
-- lojas, e o papel decide quem pode alterar. Se um dia cada pessoa precisar
-- ver só as lojas dela, o caminho é acrescentar dono_id aqui e filtrar na
-- consulta -- por isso a coluna abaixo já fica preparada, sem uso ainda.
alter table public.contas add column if not exists criada_por uuid;

-- Mesma política das outras tabelas: RLS ligada e nenhuma policy, então a
-- chave anon não lê nada. Quem acessa é o app, com a service_role.
alter table public.perfis enable row level security;


-- ---------------------------------------------------------------------------
-- Permissoes.
--
-- A RLS acima bloqueia quem chega pela chave publica. Estes GRANTs sao a
-- outra metade: liberam o acesso para o papel service_role, que e o que o
-- app usa no servidor. Sem eles o Postgres recusa com
-- "permission denied for table contas", mesmo a chave estando correta.
--
-- De proposito NAO concedemos nada a anon nem a authenticated: ninguem
-- alcanca estas tabelas pelo navegador.
-- ---------------------------------------------------------------------------
grant usage on schema public to service_role;

grant all privileges on public.contas        to service_role;
grant all privileges on public.oauth_estados to service_role;
grant all privileges on public.eventos       to service_role;
grant all privileges on public.perfis        to service_role;

-- eventos.id e bigserial: sem a sequencia o insert falha
grant usage, select on all sequences in schema public to service_role;

-- vale tambem para o que for criado daqui para frente
alter default privileges in schema public
    grant all on tables to service_role;
alter default privileges in schema public
    grant usage, select on sequences to service_role;


-- ---------------------------------------------------------------------------
-- conta_acessos: quem enxerga qual loja.
--
-- Quem e dono ve todas as lojas e nao precisa de linha aqui. Para operador e
-- leitor vale o contrario: so enxergam as lojas em que foram incluidos. Sem
-- nenhuma linha, a pessoa entra e ve o painel vazio.
--
-- E uma tabela de ligacao (e nao uma coluna dono_id em contas) para permitir
-- duas situacoes que aparecem na pratica: duas pessoas cuidando da mesma loja,
-- e uma pessoa cuidando de duas lojas.
-- ---------------------------------------------------------------------------
create table if not exists public.conta_acessos (
    conta_id   uuid not null references public.contas (id) on delete cascade,
    usuario_id uuid not null references public.perfis (id) on delete cascade,
    criado_em  timestamptz not null default now(),
    primary key (conta_id, usuario_id)
);

create index if not exists conta_acessos_usuario_idx
    on public.conta_acessos (usuario_id);

alter table public.conta_acessos enable row level security;

grant all privileges on public.conta_acessos to service_role;
