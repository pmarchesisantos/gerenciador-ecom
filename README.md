# Sistema-Ecom

Painel único para consultar pedidos de **Mercado Livre**, **Shopee** e **TikTok Shop**.

Não é um importador nem um ERP: a cada consulta o app chama as APIs das plataformas ao
vivo, traduz os status de cada uma para um vocabulário comum e soma tudo em um painel.
No banco ficam guardadas **apenas as credenciais de acesso**, criptografadas, e **quem
tem acesso ao sistema**.

## Como rodar local

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python.exe run.py
```

O painel sobe em <http://127.0.0.1:8000>. Sem nenhuma configuração ele já abre — use o
link **dados de exemplo** para conhecer as telas antes de ter as chaves.

Para conferir se está tudo de pé: `.venv\Scripts\python.exe verificar.py`
(56 checagens, sem banco e sem internet).

## Ordem de configuração

1. **Supabase** — crie o projeto, copie `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` e
   `SUPABASE_ANON_KEY` para o `.env`, e rode [`schema.sql`](schema.sql) inteiro
   no SQL Editor.
2. **Publicar na Vercel** — antes de cadastrar as chaves das plataformas, porque o
   endereço publicado é o que vai nas URIs de redirect.
3. **Primeiro acesso** — na primeira visita o app oferece criar o usuário dono.
4. **Mercado Livre** — chaves saem na hora, sem aprovação. Comece por ele.
5. **Shopee** e **TikTok Shop** — o aplicativo passa por análise da plataforma antes de
   liberar produção. Protocole cedo.

O passo a passo detalhado fica **dentro do app**, em `/ajuda`, já com as URLs de retorno
preenchidas conforme o seu `BASE_URL`. A página `/saude` diz o que ainda falta.

## Deploy na Vercel

O repositório já traz [`api/index.py`](api/index.py) e [`vercel.json`](vercel.json) —
a Vercel reconhece sozinha. O que você precisa fazer é cadastrar as variáveis em
**Settings → Environment Variables**:

| Variável | De onde vem |
| --- | --- |
| `SUPABASE_URL` | Supabase → Project Settings → Data API |
| `SUPABASE_SERVICE_KEY` | Supabase → API Keys → service_role |
| `SUPABASE_ANON_KEY` | Supabase → API Keys → anon |
| `CHAVE_CRIPTO` | gere com `python -m app.cripto` |
| `CHAVE_SESSAO` | gere com `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `BASE_URL` | o endereço final, https, sem barra no fim |
| `FUSO` | `America/Sao_Paulo` |

### Por que `CHAVE_CRIPTO` é obrigatória em produção

Local, se a variável estiver vazia o app gera a chave e guarda em `data/chave.key`.
Na Vercel isso seria uma armadilha: o disco é efêmero, o arquivo sumiria no próximo
deploy e **todas as credenciais já gravadas ficariam ilegíveis**. Por isso, quando o
ambiente é produção, o app recusa funcionar sem a variável em vez de gerar uma chave
nova em silêncio. Guarde a sua: trocá-la obriga a recadastrar todas as lojas.

### Atenção ao tempo de resposta

Cada página consulta as APIs ao vivo. O `vercel.json` pede `maxDuration: 60`, mas o
limite real depende do plano — o gratuito costuma ser bem menor. As contas são
consultadas em paralelo, o que normalmente mantém a resposta em poucos segundos, mas
com volume alto ou períodos longos pode estourar. Se acontecer, o caminho é carregar
um canal por vez via requisições separadas, em vez de todos juntos.

## Acesso e papéis

O **Supabase Auth** guarda e-mail e senha. A tabela `perfis` guarda o papel de cada um.
Ninguém se cadastra sozinho: quem é dono cria os acessos em `/usuarios`.

| Papel | Pode |
| --- | --- |
| **Dono** | tudo, inclusive cadastrar/remover lojas e gerenciar quem tem acesso |
| **Operador** | ver tudo e reautorizar ou renovar token de uma loja |
| **Leitor** | só consultar painel e pedidos |

Todas as pessoas enxergam as mesmas lojas — o papel decide quem altera, não o que se vê.
O sistema impede ficar sem nenhum dono ativo e impede você remover o próprio acesso.
Para um dia separar o que cada um vê, a coluna `contas.criada_por` já existe, sem uso.

## As telas

| Rota | O que faz |
| --- | --- |
| `/` | Painel: pedidos por situação, faturamento, ticket médio, por canal, por dia, top SKUs |
| `/pedidos` | Lista completa com filtros de período, canal, situação e busca |
| `/contas` | Cadastro das credenciais, conectar/reautorizar loja, renovar token |
| `/contas/{id}` | Tudo sobre uma conta: dados da loja ao vivo, prazos dos tokens, histórico |
| `/usuarios` | Quem tem acesso e com qual papel (só dono) |
| `/ajuda` | Como gerar as chaves em cada plataforma e publicar |
| `/saude` | Diagnóstico da configuração — fica aberto, para depurar deploy quebrado |

## A ideia central

Cada plataforma nomeia as coisas do seu jeito (`COMPLETED`, `paid`, `DELIVERED`...).
Todo canal traduz o status dele para um dos cinco grupos de
[`app/modelos.py`](app/modelos.py):

| Grupo | Significa |
| --- | --- |
| `efetivada` | pagou — é isso que conta como faturamento |
| `pendente` | criado, sem pagamento confirmado |
| `cancelada` | caiu antes de concluir |
| `devolvida` | o produto voltou |
| `estornada` | o dinheiro voltou |

A tela de pedidos mostra lado a lado o status cru da plataforma e o grupo traduzido,
para você conferir se alguma tradução ficou errada.

## Estrutura

```
api/index.py     ponto de entrada da Vercel
app/
  canais/        um arquivo por marketplace (contrato em base.py)
  servicos/      contas, usuarios, consulta (agregação), exemplo
  templates/     telas
  auth.py        login, sessão e papéis
  config.py      .env e variáveis de ambiente
  cripto.py      criptografia dos segredos
  db.py          Supabase via REST
  modelos.py     o modelo normalizado — comece a ler por aqui
schema.sql       todas as tabelas (pode rodar de novo sem quebrar)
verificar.py     teste de fumaça
legado/          esqueleto anterior, de importação por CSV
```

Para somar um marketplace novo: crie uma classe herdando de `CanalBase`, registre em
`app/canais/__init__.py`, e nenhuma tela precisa mudar.

## Segurança

- `client_secret`, `access_token` e `refresh_token` são criptografados (Fernet) antes de
  ir para o banco.
- As tabelas sobem com RLS ligada e sem nenhuma policy: a chave pública (anon) do
  Supabase não lê nada. Só o app, com a `service_role`, enxerga os dados.
- Cookie de sessão assinado, `httpOnly`, e `secure` quando em produção.
- A trilha de eventos registra quem conectou, renovou ou removeu cada coisa.
- O `.env` e a chave local estão no `.gitignore`.

## Limites conhecidos

- **Valores são o que o comprador pagou**, com frete, sem descontar comissão da
  plataforma. Faturamento ≠ o que cai na conta.
- **SKU não é unificado entre canais**: o mesmo produto com código diferente aparece em
  linhas separadas no top de produtos.
- **Mercado Livre**: devolução não é identificada separadamente — o reembolso aparece
  como estorno, que é como o ML registra.
- **Teto de 1000 pedidos por conta/consulta**, para não estourar limite de chamadas.
- O cache de 60 segundos vive em memória, então em serverless ele se perde a cada
  instância fria — na prática ajuda pouco na Vercel.
