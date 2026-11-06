# autodev

CLI em Python que orquestra agentes de IA para construir software. Ele leva um pedido (funcionalidade, correção ou melhoria) até o merge na branch principal. No caminho, entende o domínio com você, quebra o trabalho em stories, implementa com TDD e valida com gates automáticos e revisão por agentes.

> **Status:** roadmap do MVP concluído; ainda não testado de ponta a ponta com o modelo real. O ciclo completo está implementado e coberto por testes: pedido com discovery e refinamento (`request`, `discover`, `approve`, `refine`, `show`), e implementação com TDD, revisão, QA e merge na main até DONE (`run`, `resume`). Também há briefing técnico com bootstrap de projeto vazio (`brief`), revisão de arquitetura no discovery e auto-melhoria (`improve`, incluindo regras de lint derivadas dos standards com `--lint`).

## Como funciona

```
Pedido → Discovery → Refinamento → [você aprova] → Implementação (TDD) → Gates → Revisão → QA → Merge → Pronto
```

- **Discovery:** conversa com você usando linguagem ubíqua e event storming para entender o problema. Em projeto vazio, faz também o briefing técnico (stack e arquitetura). Se o pedido exigir novos componentes, revisa a arquitetura e registra um ADR.
- **Refinamento:** monta a story com critérios de aceite e a divide em tasks pequenas. Este é o único ponto em que o processo para e espera a sua aprovação.
- **Implementação:** subagentes escrevem o teste primeiro e depois o código. O próprio autodev verifica que o teste falhou antes da implementação e passou depois.
- **Gates:** build, testes e lint precisam passar, sem exceção. Depois, um agente revisa o diff contra os padrões e ADRs do projeto.
- **QA:** confere os critérios de aceite e devolve para correção quando algo falha. Depois de um número configurável de tentativas, a story é bloqueada e espera você.
- **Merge:** cada story tem uma branch própria. O autodev faz rebase na branch principal, resolve conflitos, roda os gates de novo e faz o merge.
- **Auto-melhoria:** de tempos em tempos, analisa as execuções e o código e propõe melhorias no processo e refatorações. Ele só propõe, nunca aplica sozinho.

Tudo roda localmente. O git é usado apenas para branch, commit, push, pull e resolução de conflitos.

## Instalação

Requer Python 3.12+ e [uv](https://docs.astral.sh/uv/).

```bash
git clone <repo> autodev && cd autodev
uv sync
uv run autodev --help
```

## Uso

```bash
uv run autodev init /caminho/do/projeto     # cria /caminho/do/projeto/.agent/
uv run autodev brief --path /caminho        # briefing técnico: stack, ADRs, comandos; esqueleto se o projeto estiver vazio
uv run autodev request "quero X" --path /caminho   # discovery no terminal (/done encerra) → refinamento → aprovar
uv run autodev show S-0001 --path /caminho
uv run autodev refine S-0001 -m "ajuste" --path /caminho
uv run autodev approve S-0001 --path /caminho
uv run autodev status /caminho/do/projeto   # lista as stories do backlog
uv run autodev check /caminho/do/projeto    # roda os gates de build, testes e lint
uv run autodev run S-0001 --path /caminho   # implementa, revisa, faz QA e merge na main (DONE)
uv run autodev discover S-0002 --path /caminho  # discovery de uma story DRAFT (ex.: melhoria sugerida pelo QA)
uv run autodev resume S-0001 --path /caminho  # desbloqueia uma story BLOCKED
uv run autodev improve --path /caminho      # retro do processo (métricas → propostas) e scan de código (→ stories)
uv run autodev improve --lint --path /caminho  # regras de lint a partir de standards/ (→ story)
```

## Colocar o autodev num projeto (novo ou existente)

```bash
uv tool install --editable ~/Desktop/projects/agent   # uma vez; o comando `autodev` fica no PATH
autodev onboard ~/caminho/do/projeto                  # ou `autodev onboard` dentro da pasta
```

O `onboard` é interativo e pode ser rodado de novo com segurança (só completa o que falta):

1. cria a pasta e o repositório git se faltarem, e o `.agent/`;
2. pergunta, com padrões detectados: branch principal, se pode fazer push no remote (padrão: não), autenticação (se o seu Claude Code usa `apiKeyHelper`, cria `.agent/auth.json` só com ele, fora do git), teto de custo por execução e documentação existente (`README.md`, `docs/`) como contexto, sem copiar;
3. confere as convenções: oferece incluir `@AGENTS.md` no `CLAUDE.md` e avisa sobre hooks;
4. testa a conexão com o modelo (centavos) antes de qualquer commit;
5. commita `chore: onboard autodev` e oferece o briefing técnico (projeto vazio: cria o esqueleto).

`--yes` aceita os padrões; `--no-brief` deixa o briefing para depois. `autodev doctor` testa a conexão a qualquer momento.

Se o projeto já tem `CLAUDE.md`, `AGENTS.md`, `.claude/rules/` ou skills:

- os agentes carregam o `CLAUDE.md` (o `AGENTS.md` só se o `CLAUDE.md` importar `@AGENTS.md`), e o reviewer/QA cobram essas convenções;
- em conflito, o **processo do autodev vence** (TDD, git do orquestrador, papéis só leitura); as convenções do projeto governam o código;
- skills do projeto valem por papel (`skills: all` em test_writer/implementer no template);
- hooks em `.claude/settings.json` também rodam nas sessões dos agentes; o `init` avisa: garanta que nenhum faça commit, push ou troque de branch.

## Configuração

Toda a configuração fica em documentos na pasta `.agent/` do projeto-alvo:

| Caminho | Para quê |
|---|---|
| `config.yaml` | comandos de build/test/lint, git, limites e agentes por papel |
| `knowledge/` | documentação do projeto que os agentes leem como contexto |
| `domain/glossary.md` | linguagem ubíqua |
| `domain/events.md` | resultado do event storming |
| `architecture/adr/` | decisões de arquitetura |
| `standards/` | padrões de código aplicados na revisão e nas regras de lint |
| `process/` | prompts que substituem os embutidos, por papel (ex.: `implementer.md`); valem automaticamente |
| `backlog/` | stories (`S-0001.md`); fora do git |
| `runs/` | logs das execuções (JSONL); fora do git |

### Agentes

Cada papel (`discovery`, `architect`, `refiner`, `test_writer`, `implementer`, `reviewer`, `qa`, `merger`, `improver`) pode ter modelo, esforço, limite de turnos, ferramentas e prompt próprios:

```yaml
agents:
  defaults:
    model: claude-opus-5-5
    effort: high          # low | medium | high | xhigh | max
    max_turns: 50
  roles:
    implementer:
      model: claude-sonnet-5-5
      effort: medium
    reviewer:
      tools: [Read, Grep, Glob, Bash]
```

Os agentes só podem usar as ferramentas listadas (padrão: Read, Write, Edit, Glob, Grep, Bash) e nunca rodam comandos git que alteram o repositório; `max_budget_usd` define um teto de custo opcional.

Cada campo segue a precedência: valor do papel → `defaults` → padrão embutido. Chaves desconhecidas são rejeitadas, então erros de digitação falham logo no carregamento.

### Autenticação dos agentes

Os agentes rodam via Claude Agent SDK e, por padrão, carregam só as settings do projeto. Se a sua autenticação do Claude Code vem de `~/.claude/settings.json` (por exemplo, um `apiKeyHelper`), configure uma das opções:

```yaml
runtime:
  settings: auth.json                # .agent/auth.json com só o necessário, ex. {"apiKeyHelper": "..."}
  # ou: setting_sources: [user, project]   # herda todo o ~/.claude (inclui CLAUDE.md global, hooks, plugins)
```

## Roadmap

| Fase | Entrega | Status |
|---|---|---|
| 1 | Esqueleto, configuração, domínio Story/Task, backlog, `init`/`status` | ✅ |
| 2 | Gates de build/test/lint, git, gate de TDD, `autodev check` | ✅ |
| 3 | Runtime de agentes (Claude Agent SDK), implementação e revisão, `autodev run`/`resume` | ✅ |
| 4 | Discovery interativo e refinamento, `request`/`approve`/`refine`/`show` | ✅ |
| 5 | QA, retrabalho, merge e conflitos, `discover` | ✅ |
| 6 | Briefing técnico e revisão de arquitetura com ADRs, `brief` | ✅ |
| 7a | `autodev improve`: retro do processo e scan de código | ✅ |
| 7b | Regras de lint a partir de `standards/`, `improve --lint` | ✅ |
| depois | RAG, PR no GitHub, tasks em paralelo, Jira | |

## Desenvolvimento

```bash
uv run pytest                     # testes
uv run ruff check . && uv run ruff format --check .
uv run mypy                       # strict
```

O contexto de design para quem for contribuir (incluindo agentes) está em [AGENTS.md](AGENTS.md).
