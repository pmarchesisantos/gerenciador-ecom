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


-- ---------------------------------------------------------------------------
-- produtos: o cadastro único. É aqui que o SKU passa a ter um dono.
-- ---------------------------------------------------------------------------
create table if not exists public.produtos (
    id            uuid primary key default gen_random_uuid(),
    sku           text not null,
    titulo        text not null,
    descricao     text,
    marca         text,
    preco         numeric(12, 2),
    estoque       integer not null default 0,
    peso_g        integer,
    comprimento_cm integer,
    largura_cm    integer,
    altura_cm     integer,
    ativo         boolean not null default true,
    criado_por    uuid references public.perfis (id) on delete set null,
    criado_em     timestamptz not null default now(),
    atualizado_em timestamptz not null default now(),
    unique (sku)
);

create index if not exists produtos_titulo_idx on public.produtos (titulo);

drop trigger if exists produtos_toca on public.produtos;
create trigger produtos_toca before update on public.produtos
    for each row execute function public.toca_atualizada_em();

-- ---------------------------------------------------------------------------
-- produto_fotos: ponteiros para os arquivos no Supabase Storage.
-- A imagem precisa ter URL pública: as plataformas buscam o arquivo por ela.
-- ---------------------------------------------------------------------------
create table if not exists public.produto_fotos (
    id         uuid primary key default gen_random_uuid(),
    produto_id uuid not null references public.produtos (id) on delete cascade,
    caminho    text not null,
    url        text not null,
    ordem      integer not null default 0,
    criado_em  timestamptz not null default now()
);

create index if not exists produto_fotos_produto_idx
    on public.produto_fotos (produto_id, ordem);

-- ---------------------------------------------------------------------------
-- produto_anuncios: o de/para.
--
-- É a tabela que resolve a bagunça de SKU entre canais: o mesmo produto tem
-- código diferente em cada plataforma, e é aqui que eles se encontram. Serve
-- tanto para publicar quanto para agrupar o que já foi vendido.
-- ---------------------------------------------------------------------------
create table if not exists public.produto_anuncios (
    id              uuid primary key default gen_random_uuid(),
    produto_id      uuid not null references public.produtos (id) on delete cascade,
    conta_id        uuid not null references public.contas (id) on delete cascade,
    anuncio_id      text not null,
    sku_canal       text,
    url             text,
    situacao        text,
    sincronizado_em timestamptz,
    criado_em       timestamptz not null default now(),
    unique (conta_id, anuncio_id)
);

create index if not exists produto_anuncios_produto_idx
    on public.produto_anuncios (produto_id);
create index if not exists produto_anuncios_sku_idx
    on public.produto_anuncios (sku_canal);

-- mesma política das outras tabelas
alter table public.produtos         enable row level security;
alter table public.produto_fotos    enable row level security;
alter table public.produto_anuncios enable row level security;

grant all privileges on public.produtos         to service_role;
grant all privileges on public.produto_fotos    to service_role;
grant all privileges on public.produto_anuncios to service_role;


-- ---------------------------------------------------------------------------
-- O produto vira a BASE. Preço e estoque dele passam a ser o padrão usado
-- quando não há variação; com variações, quem manda é a linha da variação.
-- ---------------------------------------------------------------------------
alter table public.produtos add column if not exists categoria       text;
alter table public.produtos add column if not exists condicao        text default 'novo';
alter table public.produtos add column if not exists eixo1_nome      text;
alter table public.produtos add column if not exists eixo2_nome      text;
alter table public.produtos add column if not exists atributos       jsonb not null default '{}'::jsonb;
alter table public.produtos add column if not exists medidas_por_variacao boolean not null default false;

-- fiscais (Brasil). Colunas próprias, não JSON: são padronizadas e entram
-- em validação, em nota e no cadastro de todas as plataformas.
alter table public.produtos add column if not exists ncm             text;
alter table public.produtos add column if not exists cest            text;
alter table public.produtos add column if not exists origem_fiscal   text;
alter table public.produtos add column if not exists cfop_dentro     text;
alter table public.produtos add column if not exists cfop_fora       text;
alter table public.produtos add column if not exists csosn           text;
alter table public.produtos add column if not exists unidade         text default 'UN';
alter table public.produtos add column if not exists pis_cofins_cst  text;
alter table public.produtos add column if not exists tributos_pct    numeric(5, 2);

-- ---------------------------------------------------------------------------
-- produto_variacoes: uma linha por combinação vendável.
--
-- Dois eixos, não N. É o que Shopee, Mercado Livre e TikTok expõem na prática
-- (Variação1 / Variação2), e cobre cor × tamanho sem o custo de uma estrutura
-- genérica de eixos que ninguém usaria.
-- ---------------------------------------------------------------------------
create table if not exists public.produto_variacoes (
    id            uuid primary key default gen_random_uuid(),
    produto_id    uuid not null references public.produtos (id) on delete cascade,
    sku           text not null,
    eixo1_valor   text,
    eixo2_valor   text,
    preco         numeric(12, 2),
    preco_promo   numeric(12, 2),
    estoque       integer not null default 0,
    gtin          text,
    peso_g        integer,
    comprimento_cm integer,
    largura_cm    integer,
    altura_cm     integer,
    foto_id       uuid references public.produto_fotos (id) on delete set null,
    ativa         boolean not null default true,
    ordem         integer not null default 0,
    criado_em     timestamptz not null default now(),
    unique (sku)
);

create index if not exists produto_variacoes_produto_idx
    on public.produto_variacoes (produto_id, ordem);

-- o de/para passa a poder apontar para uma variação específica
alter table public.produto_anuncios
    add column if not exists variacao_id uuid
    references public.produto_variacoes (id) on delete set null;

alter table public.produto_variacoes enable row level security;
grant all privileges on public.produto_variacoes to service_role;

-- ---------------------------------------------------------------------------
-- Categoria na plataforma e os campos que ela exige.
--
-- Guardamos o resultado, nunca a lista de campos: quais atributos uma
-- categoria pede e a propria plataforma quem diz, e isso muda sem aviso.
-- `atributos_ml` vai chaveado pelo ID do atributo no Mercado Livre
-- (BRAND, MATERIAL, ...), que e exatamente o que a publicacao vai mandar.
-- ---------------------------------------------------------------------------
alter table public.produtos add column if not exists ml_categoria_id   text;
alter table public.produtos add column if not exists ml_categoria_nome text;
alter table public.produtos add column if not exists atributos_ml      jsonb not null default '{}'::jsonb;

-- ---------------------------------------------------------------------------
-- Estoque.
--
-- O saldo vive em produtos.estoque (ou produto_variacoes.estoque, quando ha
-- grade). O livro abaixo nao duplica esse numero: ele EXPLICA como o numero
-- chegou onde esta, e e o que torna a baixa automatica segura.
--
-- Sem o livro, cada F5 na tela de pedidos descontaria os mesmos envios de
-- novo. Com ele, a baixa por envio e unica por (canal, pedido, sku) - o indice
-- parcial la embaixo e quem garante isso no banco, nao a boa vontade do codigo.
-- ---------------------------------------------------------------------------
alter table public.produtos add column if not exists estoque_minimo   integer not null default 5;
alter table public.produtos add column if not exists controla_estoque boolean not null default true;

-- null = herda o minimo do produto. Um tamanho raro pode ter minimo proprio.
alter table public.produto_variacoes add column if not exists estoque_minimo integer;

create table if not exists public.estoque_movimentos (
    id           uuid primary key default gen_random_uuid(),
    produto_id   uuid not null references public.produtos (id) on delete cascade,
    variacao_id  uuid references public.produto_variacoes (id) on delete cascade,
    sku          text not null,
    delta        integer not null,          -- positivo entra, negativo sai
    saldo_depois integer,                   -- para a tela mostrar o antes/depois
    motivo       text not null,             -- entrada | envio | ajuste | devolucao
    pedido_id    text,
    canal        text,
    conta_id     uuid references public.contas (id) on delete set null,
    observacao   text,
    usuario_id   uuid,
    criado_em    timestamptz not null default now()
);

-- a trava da baixa automatica: o mesmo envio nao desconta duas vezes
create unique index if not exists estoque_mov_envio_unico
    on public.estoque_movimentos (canal, pedido_id, sku)
    where motivo = 'envio';

create index if not exists estoque_mov_produto_idx
    on public.estoque_movimentos (produto_id, criado_em desc);

alter table public.estoque_movimentos enable row level security;
grant all privileges on public.estoque_movimentos to service_role;
