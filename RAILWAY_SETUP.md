# Deploy no Railway

Este projeto está pronto para rodar no Railway com múltiplas instâncias simultâneas, cada uma com seu próprio ENV e banco SQLite.

## Arquivos novos

- `railway.json` — configura build (Dockerfile) e deploy (`python run_railway.py`).
- `run_railway.py` — runner que sobe N instâncias do bot em subprocessos, separando bancos e configs por padrão.
- `RAILWAY_ENVIRONMENT_EXAMPLE.txt` — exemplo completo das variáveis.

## Como funciona

`run_railway.py` lê a variável `RAILWAY_INSTANCES` e sobe uma instância para cada nome, por exemplo:

```env
RAILWAY_INSTANCES=MAIN,RENAN,TARIK
```

Cada instância roda como um processo independente. Você pode isolar configs usando prefixos:

```env
RENAN_DISCORD_USER_TOKEN=...
RENAN_COPYTRADE_BANKROLL_ID=...
TARIK_DISCORD_USER_TOKEN=...
TARIK_COPYTRADE_BANKROLL_ID=...
```

Ou criar um service separado no Railway para cada instância e usar variáveis sem prefixo.

O runner também separa automaticamente os bancos SQLite:

- `MAIN` → `data/main_notified_bets.sqlite3`
- `RENAN` → `data/renan_discord_signals.sqlite3`
- `TARIK` → `data/tarik_discord_signals.sqlite3`

## Passo a passo no Railway

1. Crie um projeto no Railway.
2. Faça deploy a partir deste repositório.
3. No dashboard do service, vá em **Variables**.
4. Cole o conteúdo de `RAILWAY_ENVIRONMENT_EXAMPLE.txt` (ajustando com seus dados reais).
5. Adicione um **Volume** montado em `/app/data`.
6. O `railway.json` já define o start command como `python run_railway.py`.
7. Deploy.

## Userbot

O userbot precisa de uma sessão MTProto (`*.session`). Gere localmente:

```bash
python userbot_login.py
```

Depois faça upload do arquivo gerado em `data/telegram_userbot.session` para o volume do Railway.

## Múltiplos services vs um service só

- **Um service só**: coloque todas as instâncias em `RAILWAY_INSTANCES` e use prefixos. Mais barato no Hobby.
- **Um service por instância**: crie services separados no Railway, cada um com suas próprias Variables, e use `RAILWAY_INSTANCES=RENAN` (etc). Melhor isolamento, mas consome mais recursos.

## Custo

O plano Hobby do Railway inclui USD 5 de crédito por mês. Rodar várias instâncias 24/7 pode extrapolar esse crédito dependendo do consumo de CPU/RAM. Acompanhe o billing no dashboard.

## Atenção

- Nunca commite tokens, senhas ou arquivos `.env` no repositório.
- A `.dockerignore` já impede que `.env`, `data/` e logs entrem na imagem.
- Self-bots do Discord violam os Termos de Serviço. Use por conta e risco.
