# AGENTS.md

Contexto do projeto `autodev` para qualquer agente (ou pessoa) que continue o trabalho.

## Visão

`autodev` é um CLI Python que orquestra agentes de IA para construir software em um projeto-alvo, rodando **local**. Ele recebe um pedido (funcionalidade, correção, melhoria) e o leva até o merge na branch principal:

```
Pedido → DISCOVERY → REFINAMENTO → [aprovação humana] → IMPLEMENTAÇÃO (TDD) → GATES → REVISÃO → QA → MERGE → DONE
```

- **Discovery**: briefing de requisitos com o usuário usando linguagem ubíqua (→ `domain/glossary.md`) e event storming (→ `domain/events.md`). Projeto vazio → briefing técnico (stack/arquitetura). Pedido que exige novos componentes → revisão de arquitetura → ADR.
- **Refinamento**: story com critérios de aceite, quebrada em tasks pequenas. Única parada humana obrigatória: aprovar a story.
- **Implementação**: subagentes por task, sempre test-first. O orquestrador **verifica** TDD: após o commit de teste a suíte tem que falhar (red), após a implementação tem que passar (green).
- **Gates determinísticos**: build, testes, lint (incluindo regras de lint geradas a partir de `standards/`). Depois, revisão por agente contra `standards/` e ADRs. Revisão local por padrão; PR no GitHub é opcional (fase futura).
- **QA**: avalia os critérios de aceite e devolve para retrabalho, com limite de tentativas (`limits.max_attempts`) → BLOCKED.
- **Merge**: branch `story/<id>`, rebase na main, conflitos resolvidos por agente, gates de novo, merge fast-forward, push se houver remote. Git é usado só para branch/commit/push/pull/conflitos.
- **Auto-melhoria** (`autodev improve`, periódico): retro do processo (lê `runs/`) e varredura de refatoração. **Propõe** (stories / mudanças de prompt), nunca aplica sozinha.

## Decisões de design (tomadas com o usuário)

1. **Fluxo é código, não prompt.** Uma máquina de estados em Python controla as transições e os gates; os LLMs só executam cada etapa. O LLM nunca pode pular um gate.
2. **Runtime de agentes: Claude Agent SDK** (`claude-agent-sdk`), isolado atrás da porta `AgentRuntime`. Confirmar a API do SDK na documentação oficial antes de implementar o adapter (inclusive como passar `effort`); não escrever de memória.
3. **Interface: CLI interativa** (`typer` + `rich`); discovery é um chat no terminal.
4. **Contexto no MVP: só a pasta `.agent/knowledge/`**; RAG entra depois via porta `ContextSource`.
5. **Tudo configurável por documentos** no projeto-alvo (`.agent/`).
6. **Agentes configuráveis por papel** (`model`, `effort`, `max_turns`, `tools`, `prompt`). Precedência: papel → `agents.defaults` → padrão embutido (`claude-opus-5-5`, `high`, 50). O template do `init` usa Opus para discovery/architect/refiner/reviewer/qa/improver e `claude-sonnet-5-5` (effort medium) para test_writer/implementer/merger.
7. **Saída dos agentes**: JSON na resposta final validado por pydantic, com uma nova tentativa se inválido.
8. **Portas (`Protocol`) só onde há mais de uma implementação pedida**: `AgentRuntime`, `ContextSource`, `StorySource` (sessão interativa / fila de arquivos; Jira/GitHub depois), `ReviewBackend` (local / GitHub). O resto é concreto.
9. **Tasks de uma story rodam em sequência** no MVP (paralelismo com git worktrees depois).
10. Reviewer e QA são só leitura (ferramentas restritas no config). Como recebem Bash para rodar testes, isso ainda é orientado por prompt, não garantido.
11. **Permissões dos agentes**: `permission_mode="dontAsk"` + só as ferramentas do perfil (`tools` resolvido; padrão Read/Write/Edit/Glob/Grep/Bash). Um hook PreToolUse nega comandos git que alteram o repositório: git é do orquestrador.
12. **Falhas**: violação de regra do TDD (ou erro do agente) → `reset_hard` para antes da tentativa e repete com feedback; testes/gates falhando → mantém o commit e corrige em cima. Orçamento `limits.max_attempts` por fase (red/green) por task; esgotou → BLOCKED. Commits de correção de review podem tocar testes, mas passam pelos gates.
13. **Estado do orquestrador fora do git**: `.agent/backlog/` e `.agent/runs/` são ignorados via `.agent/.gitignore` (criado pelo `init`), senão o estado mudaria ao trocar de branch e sujaria os commits. `autodev run` recusa se não estiverem ignorados.
14. **Settings/autenticação dos agentes** (`runtime` no config): por padrão só as settings do projeto-alvo (`setting_sources: [project]`). Quando a autenticação do Claude Code vem de um `apiKeyHelper` em `~/.claude/settings.json` (ex.: gateways corporativos), então é preciso `runtime.settings` (JSON mínimo, ex. só o `apiKeyHelper`; relativo a `.agent/`) ou `setting_sources: [user, project]` (herda também CLAUDE.md global, hooks, plugins). `cli_path` apontando para um wrapper do `claude` não funcionou (exit 71). Verificado só rodando de dentro de uma sessão do Claude Code. `max_budget_usd` opcional por papel.
15. **Discovery/refinamento**: o orquestrador conduz o chat (cada turno é um `DiscoveryTurn` JSON); termina quando o agente devolve `done` + resultado, ou pelo `/done` do usuário, com teto `limits.max_discovery_turns`. Agentes de discovery/refiner são só leitura (Read/Grep/Glob) e devolvem dados; o **código** escreve glossary.md e events.md. Conversa e documentos no idioma do pedido. O `approve` exige estar na main e commita só `.agent/domain` e `.agent/architecture` (`docs(<id>): ...`) antes de gravar APPROVED.
16. **QA e merge**: após o review aprovado, o QA verifica cada critério de aceite; reprovado → send_back (mesmo contador do review) → correção → review e QA de novo. Merge autônomo: pull (se `git.remote`), rebase na main, conflitos resolvidos pelo agente merger (o código confere que não restam marcadores; esgotou → `rebase --abort` → BLOCKED, main intacta), gates, `merge --ff-only`, push, DONE e `branch -d story/<id>`. Melhorias sugeridas pelo QA viram stories DRAFT **só depois do merge** (evita duplicar em retomadas); `autodev discover <id>` as leva ao discovery. Push falhou → BLOCKED com a main já mergeada localmente; o `resume` volta a IN_PROGRESS e refaz review/QA antes de tentar de novo.
17. **Briefing e arquitetura** (agente architect, só leitura; o código grava tudo): `autodev brief` (oferecido no `request`/`discover` quando não há ADR além do template) é um chat → proposta → aceitar/feedback → ADRs Accepted, `standards/stack.md`, `commands` no config.yaml (edição pontual, preserva comentários) → commit `docs: technical briefing` na main. Repositório vazio (nada versionado fora de `.agent/`) → o implementer cria o esqueleto + teste smoke, validado pelos **gates** (exceção controlada ao TDD); falhou → reset até o commit de docs. No discovery, `needs_architecture_review` → architect propõe ADRs/componentes → você aceita ou dá feedback → seção `## Architecture decisions` na story, que o refiner transforma em tasks.
18. **Auto-melhoria** (agente improver, só leitura; propõe, nunca aplica sozinho): `autodev improve --process` calcula métricas determinísticas dos logs → propostas de prompt/standard/config → você aceita ou pula cada uma → prompts viram `.agent/process/<role>.md` (valem automaticamente; o `prompt:` do config tem precedência), standards são acrescentados, config só recomendada → commit `chore(process): ...`. `--code`: churn do git + improver → refatorações → as escolhidas viram stories DRAFT. Depois de uma story DONE, o CLI sugere o improve quando há `improve.every_n_stories` (padrão 5) novas DONE desde a última vez (estado em `.agent/runs/improve-state.json`).
19. **Lint a partir dos standards**: `autodev improve --lint` (fora do padrão do `improve`) → improver propõe regras (ferramenta, regra, standard que aplica) → você escolhe → **uma** story DRAFT com tabela de regras e guia de TDD: fixture com a violação em `tests/`, excluída do lint normal, e um teste que roda o linter direto nela (red) até a regra ser habilitada (green). Aceitar um standard na retro faz o CLI sugerir o `--lint`.
20. **Projetos existentes (CLAUDE.md, AGENTS.md, rules, skills, hooks)**: com `setting_sources` contendo `project`, os agentes carregam o CLAUDE.md (o AGENTS.md só se o CLAUDE.md importar `@AGENTS.md`) e o `.claude/settings.json`. Toda instrução de papel termina com a seção fixa `## Precedence`: o processo do autodev (fases de TDD, git do orquestrador, papéis só leitura, formato de saída) vence; as convenções do projeto governam o código. A `KnowledgeFolder` inclui `CLAUDE.md`, `AGENTS.md` e `.claude/rules/**/*.md`. Skills por papel (`skills: all | [nomes]`, ausente = nenhuma → `skills=[]` no SDK; template: `all` em test_writer/implementer). O SDK só acrescenta `Skill` ao `--allowedTools`, então o adapter acrescenta `Skill` ao `tools` (e ao `allowed_tools` só com `all`, porque um `Skill` solto liberaria todas as skills de uma lista). O `brief` não repete convenções já documentadas, e o `stack.md` aponta para elas. `init` e `brief` avisam se o projeto tem hooks (eles também rodam nas sessões dos agentes e escapam do bloqueio de git).
21. **Onboarding** (`autodev onboard`): um comando idempotente para projeto novo ou existente: pasta/git → `.agent/` (+ `.gitignore` com backlog/, runs/, auth.json) → perguntas com padrões detectados (gravadas por `config_edit.set_value`, que preserva comentários) → convenções (`@AGENTS.md`, hooks) → `check_connection` (perfil do implementer com effort low, sem ferramentas, teto US$ 0,20) **antes** do commit `chore: onboard autodev` → briefing opcional. Autenticação mínima: `.agent/auth.json` com `apiKeyHelper` + env `ANTHROPIC_*` e `CLAUDE_CODE_API_KEY_HELPER_TTL_MS` de `~/.claude/settings.json` (verificado ao vivo), chmod 600, nunca impresso. `context.paths` referencia docs existentes do repo (arquivos, ou .md/.txt/.rst/.adoc de pastas) sem copiar.
22. **MCP** (`autodev mcp`, stdio, SDK `mcp` 2.x: `from mcp.server import MCPServer`; FastMCP não existe mais): cada comando de `app/commands.py` roda como **sessão** numa thread; o `BridgeIO.ask` bloqueia o fluxo e a ferramenta devolve `{session_id, state: awaiting_reply|working|finished|failed|cancelled, messages, question, error}`; `autodev_reply`/`autodev_poll`/`autodev_cancel` continuam. Espera até ~45 s por chamada e depois `working`. Uma sessão ativa por projeto. O cancelamento não interrompe uma chamada de agente em andamento. A skill `/autodev` obriga o Claude a repassar as perguntas literalmente, nunca aprovar no lugar do usuário e sempre passar o `path` absoluto. `autodev install-claude` registra `claude mcp add --scope user autodev -- <exe> mcp` e instala a skill, perguntando antes. O CLI e o MCP compartilham `app/commands.py`; nada pode escrever no stdout em modo MCP.
23. **Robustez do pipeline** (regras que vieram da validação num projeto real; ver a seção abaixo).

## Robustez do pipeline (validação real)

O autodev foi validado de ponta a ponta num projeto real (uma API FastAPI de biblioteca, criada do zero com Sonnet em todos os papéis): briefing + bootstrap e três stories entregues (cadastro, empréstimos, devolução com multa), cada uma conferida por um teste de aceite independente. Cada bloqueio real virou uma regra do orquestrador:

- **Comandos autossuficientes.** Os gates rodam num shell novo, sem ambiente ativado; `commands.*` precisam preparar o próprio ambiente (ex.: `uv run pytest`, `npm ci && npm test`). O architect e o bootstrap são instruídos assim. `commands.format` (opcional) roda antes de todo commit de agente; no red, arquivos de produção alterados só pelo formatador são restaurados.
- **Sem tentativas desperdiçadas.** Se o agente não muda nada depois de um commit mantido, os gates rodam de novo (em vez de contar "no changes" como falha), e a mensagem final traz a saída real do gate. Uma task pendente recomeça do zero ao retomar: a branch volta para antes do primeiro commit `test|feat|fix(<task>):`. `autodev bootstrap` refaz só o esqueleto a partir dos ADRs.
- **Quem corrige os testes.** No green o implementer nunca toca testes. Dois turnos do test_writer cobrem o que só ele pode corrigir: (a) falhas de gate que apontam **só** para arquivos de teste versionados (lint/formatação/tipos; caminhos da `.venv` não contam); (b) um `test_defect` declarado pelo implementer (teste, fixture ou fake errado). Guardas: só arquivos de teste, e a contagem de testes (`tdd.test_name_pattern`) não pode diminuir; violação → reset. Para falhas de teste o sinal vem do diagnóstico do implementer, não dos caminhos, porque um `assert` falhando aponta para o arquivo de teste mesmo quando o bug é da produção.
- **Refinamento sob controle.** `limits.max_tasks_per_story` (padrão 8) é garantido por código (o refiner refaz até caber, senão `RunError`); toda task precisa de comportamento provável por um teste que falha; o número da task sai do título; o assunto de todo commit tem no máximo 72 caracteres, com o título completo no corpo. O `events.md` não guarda seções vazias de template.
- **Testes confiáveis** (com `tdd.junit_report`, um JUnit XML que o comando de teste grava num caminho ignorado, ex. `uv run pytest --junitxml=.agent/runs/junit.xml`; o brief preenche): o orquestrador conhece o resultado **de cada teste**. Red: se falhar só por import/coleta, o implementer faz um **esqueleto** (declarações que lançam `NotImplementedError`, sem tocar testes); todo teste novo precisa **rodar e falhar** contra ele, e um teste que passa contra o esqueleto é vazio → volta ao test_writer. Green: cada teste novo, por id, precisa passar. **Re-checagem final**: no fim da task a produção volta ao estado do esqueleto, com os testes finais, e todo teste da task tem que falhar (pega asserções enfraquecidas depois do red). O test_writer mapeia cada teste aos critérios de aceite (`coverage`, salvo na task); o reviewer recebe o mapa e o diff dos testes alterados depois do red, e o código converte em reprovação uma aprovação que não confirme essas mudanças ou que deixe critério sem teste e sem justificativa. Sem `junit_report`, vale o comportamento antigo (red/green da suíte inteira).
- **Tasks sem gambiarras.** O refiner ordena as tasks por dependência (domínio → aplicação → infra → API) e cada task deixa build e testes verdes. Linhas **adicionadas** à produção que casem com `tdd.forbidden_patterns` (padrão: `type: ignore`, `noqa`, `NotImplementedError`, `TODO`, `FIXME`) reprovam a tentativa; esqueleto esquecido após o green é pego assim.
- **Limites conhecidos** (ainda não resolvidos): se um teste prova a coisa **certa** continua sendo semântica (a re-checagem e o review tornam visível, não garantem; testes de mutação ficam para depois); não há orçamento por story nem deduplicação das melhorias sugeridas pelo QA; o servidor MCP precisa ser reconectado após atualizar o autodev; o bloqueio de git dos agentes é uma regex e o "só leitura" de reviewer/QA é só prompt.

## Estrutura do `.agent/` no projeto-alvo

```
config.yaml          comandos build/test/lint, git, limites, agentes por papel
knowledge/           documentação livre (fonte de contexto)
domain/glossary.md   linguagem ubíqua
domain/events.md     event storming
architecture/adr/    ADRs
standards/           padrões que o revisor aplica e que viram regras de lint
process/             prompts por papel que sobrescrevem os embutidos (ex.: implementer.md)
backlog/             stories (S-0001.md: frontmatter YAML + descrição)
runs/                logs de execução (alimentam a auto-melhoria)
```

## Arquitetura do código

```
src/autodev/
  cli.py               (fino) mcp | install-claude | onboard | doctor | bootstrap | init | status | check | brief | request | discover | approve | refine | show | run | resume | improve
  config/              models.py (pydantic, extra="forbid"), loader.py (load_config, ConfigError)
  domain/              role.py (Role), story.py (Story, Task, StoryStatus, InvalidTransition)
  pipeline/            state_store.py (StoryStore: save/get/has/values/next_id)
                       story_runner.py (StoryRunner: red → green → gates por task → review ⇄ QA → merge → DONE)
                       merge.py (Merger: pull, rebase, conflitos via agente merger, gates, ff-merge, push)
                       prompts.py (instruções por papel + builders dos prompts de tarefa)
                       outputs.py (saídas tipadas dos agentes), run_log.py (JSONL em .agent/runs/)
                       discovery.py (chat: DiscoveryTurn por turno, /done, limite de turnos), refinement.py
                       domain_docs.py (mescla glossary.md e acrescenta events.md, por código), story_docs.py
                       request_flow.py (pedido → DRAFT → discovery → [arquitetura] → docs → REFINED), approval.py
                       conversation.py (converse: motor genérico de chat Turn[R]; ask_accept_or_feedback)
                       briefing.py (brief → ADRs/standards/commands → commit → bootstrap se repo vazio)
                       architecture.py (revisão de arquitetura com aceite do usuário), adr.py, config_edit.py
  prompts/             instruções embutidas por papel (test_writer, implementer, reviewer)
  ports/               agent_runtime.py (AgentRuntime.run / start_conversation → Conversation), context_source.py, user_io.py
  templates/agent/     template do .agent/ copiado pelo `autodev init`
  gates/               runner.py (CommandGate, run_gates: build→test→lint, para na 1ª falha)
                       tdd.py (TddGuard: check_red / check_green)
  adapters/            git.py (GitRepo: ... reset_hard, is_ignored), claude_sdk_runtime.py (build_options, deny_git_hook),
                       git_guard.py (is_forbidden_git_command), knowledge_folder.py (ContextSource)
  adapters/console_io.py (UserIO no terminal)
  app/                 commands.py (um async por comando, fala só via UserIO; CommandError), runtime.py (make_runtime)
  mcp/                 bridge.py (BridgeIO), sessions.py (SessionManager), server.py (16 ferramentas), install.py
  templates/claude/    skill /autodev instalada em ~/.claude/skills/autodev
  onboarding/          onboard.py (fluxo idempotente), auth.py (auth.json mínimo), doctor.py (check_connection), scaffold.py
  improve/             metrics.py (ProcessStats dos runs/*.jsonl), state.py (dica a cada N), process_retro.py, code_scan.py,
                       lint_rules.py, selection.py
  futuro: file_queue_source, local_review (GitHub PR)
tests/fakes.py         FakeAgentRuntime roteirizável (passos por papel, podem editar arquivos)
```

Regras do TDD guard (em `gates/tdd.py`; arquivos de teste = `tdd.test_globs`, fnmatch no caminho relativo): sem `commands.test` → reprova (nunca pula); commit red deve mudar ≥1 arquivo, só arquivos de teste, e a suíte tem que falhar (timeout não conta como red); commit green não pode tocar arquivos de teste e a suíte tem que passar. Os testes rodam na working tree atual: fazer checkout do commit antes. Com `tdd.junit_report` configurado, as regras por teste da seção "Robustez do pipeline" se somam a estas (erro de import não basta como red).

Regras da `Story` (em `domain/story.py`): avanço só pela cadeia `draft→discovered→refined→approved→in_progress→in_review→in_qa→done`; aprovar exige tasks e critérios de aceite; ir para review exige todas as tasks `done`; `send_back` (só de in_review/in_qa) conta tentativa e bloqueia ao atingir o limite; `block` de qualquer status exceto done/blocked; `resume` zera tentativas e volta para in_progress se bloqueada em review/QA; `done` é terminal.

## Roadmap

| Fase | Entrega | Status |
|---|---|---|
| 1 | Esqueleto, config, domínio Story/Task, StoryStore, `init`/`status` | ✅ feito |
| 2 | Gates build/test/lint, adapter git, gate TDD red→green (sem LLM), `autodev check` | ✅ feito |
| 3 | `ClaudeSdkRuntime` + `FakeAgentRuntime`, implementação e revisão por agente, `autodev run <story>`, `autodev resume` | ✅ feito (teste live passou) |
| 4 | Discovery interativo (UL, event storming) + refinamento + `request`/`approve`/`refine`/`show` | ✅ feito (validado com o modelo real) |
| 5 | QA, loop de retrabalho → BLOCKED, merge e conflitos → DONE, `autodev discover` | ✅ feito |
| 6 | Briefing técnico (`autodev brief`, bootstrap de projeto vazio) e revisão de arquitetura/ADRs no discovery | ✅ feito |
| 7a | `autodev improve` (métricas + retro de processo, scan de código → stories, dica a cada N stories, prompts por convenção) | ✅ feito |
| 7b | `autodev improve --lint`: regras de lint a partir de `standards/` viram uma story normal | ✅ feito |
| validação | Projeto real criado do zero (biblioteca FastAPI): briefing, bootstrap e 3 stories até DONE | ✅ feito |
| depois | RAG, PR no GitHub, worktrees/paralelismo, Jira | |

## Comandos

Python 3.12+ via `uv` (o python do sistema é 3.9; não usar).

O `uv.lock` aponta para o PyPI público. Se o seu ambiente define outro índice (`UV_INDEX_URL`/`UV_DEFAULT_INDEX`), use `uv run --frozen` / `uv sync --frozen` para não reescrever o lock; para atualizar dependências, rode `uv lock` sem essas variáveis e com acesso ao PyPI público.

```bash
uv sync                                   # instalar dependências
uv run pytest                             # testes
uv run pytest tests/unit/test_story.py::test_done_is_terminal   # um teste
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # strict em src e tests
uv run autodev init <path>                # gerar .agent/ num projeto
uv run autodev check <path>               # rodar os gates build/test/lint
uv run autodev run S-0001 --path <path>   # implementar uma story aprovada (chama o modelo!)
uv run autodev resume S-0001 --path <path>
AUTODEV_LIVE_SETTING_SOURCES=user,project uv run pytest -m live   # SDK real (custa centavos; excluído por padrão)
```

Uma fase só está pronta com `pytest`, `ruff check`, `ruff format --check` e `mypy` passando.

## Como trabalhar neste repositório

- Processo de design: discutir → planejar → refinar com o usuário → implementar. Não decidir design em silêncio; levar ao usuário.
- TDD em tudo que é domínio/feature: testes primeiro, ver falhar, implementar.
- Tipagem completa (`mypy --strict`), dataclasses no domínio, pydantic só para config/contratos externos, `StrEnum`.
- O orquestrador inteiro deve ser testável sem LLM (via `FakeAgentRuntime`); testes de integração usam repositórios git temporários.
- Conversa com o usuário em português; código, templates e prompts embutidos em inglês.
- Não fazer commit sem o usuário pedir.
