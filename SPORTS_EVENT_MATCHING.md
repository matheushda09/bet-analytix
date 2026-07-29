# Identificação automática da data/hora dos eventos

Este módulo tenta descobrir o início oficial do evento esportivo antes de criar
a aposta no Bet-Analytix. Ele segue uma regra conservadora: é preferível manter
a data/hora da mensagem a associar o sinal ao evento errado.

O fluxo existente continua sendo o fallback. Se a funcionalidade estiver
desativada, não houver provider, a consulta falhar, a cota terminar ou o match
for ambíguo, a aposta segue normalmente com a data/hora que já seria usada.

## Modos de operação

- `disabled`: comportamento anterior, sem consultas externas e sem alteração.
- `shadow`: consulta, normaliza, pontua, registra logs/auditoria, mas preserva a
  data/hora anterior no Bet-Analytix.
- `enabled`: usa a data/hora oficial somente quando o match é aceito.

O padrão é `disabled`. Em produção, o rollout recomendado é
`disabled -> shadow -> enabled`.

Se o próprio sinal já contiver uma data/hora explícita, ela sempre é preservada.
O módulo externo não sobrescreve esse valor.

## Esportes e providers

| Esporte | Ordem padrão |
| --- | --- |
| Futebol | API-Football, football-data.org, TheSportsDB |
| Basquete | API-Basketball, TheSportsDB |
| Tênis | Live Tennis API, TheSportsDB |

Providers sem credencial não impedem a inicialização: eles são omitidos da
cascata. O TheSportsDB usa a chave pública `123` por padrão e funciona como
fallback de cobertura; para maior confiabilidade, configure ao menos o provider
principal do esporte.

Cada provider implementa a mesma interface e devolve um modelo interno:
provider, ID externo, esporte, dois participantes, início em UTC, competição,
país, status e payload de auditoria.

## Como um match é aceito

1. O esporte precisa ser exatamente compatível. Futsal, eSoccer, tênis de mesa,
   3x3 e outras modalidades incompatíveis são rejeitados.
2. O nome do evento precisa ser separável em exatamente dois participantes.
3. O candidato precisa estar na janela configurada.
4. Os dois participantes precisam atingir a pontuação mínima, em ordem direta
   ou invertida.
5. Categorias precisam coincidir: principal, feminino, base, reservas, equipe B,
   duplas etc. não são misturadas.
6. O melhor candidato precisa superar a confiança mínima e ter distância
   suficiente do segundo colocado.
7. Horários materialmente conflitantes entre providers para o mesmo evento
   causam fallback; horários próximos são consolidados.

A normalização ignora maiúsculas/minúsculas, acentos, pontuação e sufixos
comuns de clubes. Há aliases seguros para variações como `São Paulo FC`,
`Athletico-PR`, `CAP` e `CR Vasco da Gama`. Tênis entende sobrenome/inicial de
forma limitada, e basquete possui aliases conservadores para cidades como
`LA`, `NY`, `OKC`, `GS`, `SA`, `NO`, `PHX` e `MIN`.

Aliases adicionais:

```env
SPORTS_EVENT_PARTICIPANT_ALIASES_JSON={"Athletico-PR":"Athletico Paranaense","Santos FC":"Santos"}
```

Também é aceito o formato em que a chave é o nome canônico e o valor é uma
lista:

```env
SPORTS_EVENT_PARTICIPANT_ALIASES_JSON={"Athletico Paranaense":["CAP","Athletico-PR"]}
```

## Cache, concorrência e cotas

`SPORTS_EVENT_CACHE_PATH` é um SQLite separado dos bancos de sinais. Ele usa WAL,
locks transacionais, cache positivo/negativo, single-flight entre processos,
histórico de chamadas, limites locais e bloqueio temporário após erros.

Em um container Railway que executa MATHEUS, RENAN, TARIK e JOAO, mantenha o
caminho sem prefixo:

```env
SPORTS_EVENT_CACHE_PATH=data/sports_schedule.sqlite3
```

Assim, uma busca de agenda feita por um subprocesso é reaproveitada pelos
outros. Cada usuário continua com seu próprio `DISCORD_SQLITE_PATH`; somente a
agenda externa e o controle de cota são compartilhados.

Os limites podem ser reduzidos conforme o plano contratado:

```env
SPORTS_EVENT_PROVIDER_DAILY_LIMITS_JSON={"api_football":100,"api_basketball":100}
SPORTS_EVENT_PROVIDER_MINUTE_LIMITS_JSON={"football_data":10,"thesportsdb":30}
```

Respostas 429 não entram em retry infinito. Timeouts e erros de provider fazem a
cascata avançar e nunca interrompem a criação da aposta.

## UTC e Bet-Analytix

Datas de providers são normalizadas para datetimes com timezone UTC. O writer
existente aceita datetimes conscientes de timezone e envia `date`/`time` em UTC,
evitando conversões duplas. O horário local continua sendo controlado por
`APP_TIMEZONE` apenas para valores locais sem timezone.

## Auditoria e reagendamento

Cada sinal consultado grava uma linha em `sports_event_matches`, dentro do banco
Discord daquela instância. São armazenados:

- sinal e participantes normalizados;
- provider e ID oficial;
- participantes/competição/país retornados;
- horário UTC e status;
- confiança, pontuações, segundo colocado e quantidade de candidatos;
- providers consultados, origem cache/API e motivo da decisão;
- horário de fallback, vínculo com a aposta e erros de reconsulta.

Alterações observadas depois da criação são registradas em
`sports_event_match_history`.

No modo `enabled`, eventos aceitos são reconsultados pelo ID oficial:

- até 24 horas do evento: padrão de 30 minutos;
- entre 24 horas e 7 dias: padrão de 6 horas;
- mais distantes: padrão de 24 horas.

Se o provider alterar o início por pelo menos um minuto, o bot atualiza
automaticamente apenas apostas simples. Mudanças em acumuladoras e status
cancelado, adiado, suspenso, abandonado ou finalizado são auditadas, mas não
alteradas automaticamente. Se um evento adiado voltar a um status seguro, a
diferença ainda é comparada com o último horário realmente aplicado, permitindo
uma atualização posterior correta.

## Configuração

Principais variáveis:

```env
SPORTS_EVENT_MATCHING_MODE=shadow
SPORTS_EVENT_CACHE_PATH=data/sports_schedule.sqlite3
SPORTS_EVENT_LOOKBACK_HOURS=24
SPORTS_EVENT_LOOKAHEAD_DAYS=7
SPORTS_EVENT_MIN_CONFIDENCE=0.90
SPORTS_EVENT_MIN_SCORE_GAP=0.10
SPORTS_EVENT_PARTICIPANT_MIN_SCORE=0.86
SPORTS_EVENT_TIME_TOLERANCE_MINUTES=15
SPORTS_EVENT_CACHE_TTL_SECONDS=21600
SPORTS_EVENT_NEGATIVE_CACHE_TTL_SECONDS=900
SPORTS_EVENT_TOTAL_TIMEOUT_SECONDS=15
SPORTS_EVENT_RECHECK_INTERVAL_SECONDS=900
SPORTS_EVENT_RECHECK_WITHIN_24H_SECONDS=1800
SPORTS_EVENT_RECHECK_WITHIN_7D_SECONDS=21600
SPORTS_EVENT_RECHECK_FAR_SECONDS=86400
```

Credenciais e ordem:

```env
SPORTS_EVENT_FOOTBALL_PROVIDERS=api_football,football_data,thesportsdb
SPORTS_EVENT_BASKETBALL_PROVIDERS=api_basketball,thesportsdb
SPORTS_EVENT_TENNIS_PROVIDERS=live_tennis,thesportsdb
API_FOOTBALL_KEY=
API_BASKETBALL_KEY=
FOOTBALL_DATA_API_KEY=
LIVE_TENNIS_API_KEY=
THESPORTSDB_ENABLED=true
THESPORTSDB_API_KEY=123
```

As URLs-base e flags individuais também podem ser sobrescritas:
`API_FOOTBALL_BASE_URL`, `API_BASKETBALL_BASE_URL`,
`FOOTBALL_DATA_BASE_URL`, `LIVE_TENNIS_API_BASE_URL`,
`THESPORTSDB_BASE_URL` e os respectivos `*_ENABLED`.

## Rollout seguro

1. Faça deploy com `SPORTS_EVENT_MATCHING_MODE=shadow`.
2. Observe por alguns dias os logs estruturados `sports_event_match`.
3. Consulte `sports_event_matches` nos bancos de cada usuário, dando atenção a
   `fallback`, `ambiguous_candidates`, `confidence_below_threshold` e
   `provider_time_conflict`.
4. Ajuste apenas aliases comprovados. Não reduza os thresholds para aumentar
   cobertura sem analisar falsos positivos.
5. Troque para `enabled`.

Voltar para `disabled` restaura imediatamente o comportamento anterior. Os
registros de auditoria existentes não são apagados.

## Testes

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
```

A suíte cobre normalização, ordem invertida, ambiguidades, homônimos perigosos,
modalidade/categoria errada, tênis, basquete, conflitos de horário, payloads dos
providers, 429/timeouts, cache compartilhado, locks, cotas, persistência e
histórico de reagendamento.

## Adicionando outro provider

1. Implemente `SportsScheduleProvider`.
2. Normalize toda data para UTC e rejeite payload sem horário real.
3. Não retorne modalidades aproximadas.
4. Registre o provider no factory e em `PROVIDER_QUALITY`.
5. Defina limites locais conservadores.
6. Adicione fixtures de payload, falhas, paginação e timezone aos testes.

Nunca registre chaves, headers de autenticação ou senhas nos logs.
