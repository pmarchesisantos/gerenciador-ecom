# Arquivos do primeiro desenho (importação de CSV/XLSX)

Estes dois arquivos são do esqueleto anterior, que importava relatórios
exportados dos seller centers e gravava os pedidos no banco.

O sistema mudou de rumo: agora consulta as APIs ao vivo e guarda no Supabase
apenas as credenciais. Eles apontam para módulos que não existem mais
(`app.importers`, `app.services.importacao`), então **não rodam**.

Ficam aqui porque a importação por arquivo ainda é um caminho útil — é o
plano B quando a Shopee ou o TikTok demoram para aprovar o app. Se for
retomada, o lugar dela é `app/importadores/`, convertendo cada linha para o
mesmo `app.modelos.Pedido` que os canais já produzem.
