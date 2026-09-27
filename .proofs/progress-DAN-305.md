# DAN-305 · Наблюдение команд, переданных как запрос модели

## Где стоп
- Детектор и тесты написаны, разбор в отдельной копии config. Идёт полный pytest.
- Далее: итог тестов, коммиты, phase pr, два PR, свежий независимый ревьюер через дверь.

## Проверено
- Тикет прочитан целиком: /tmp/dan-305-start/issue.txt; контракт cswap корректен.
- CLAUDE.md/REVIEW.md в cswap отсутствуют; AGENTS.md прочитан. Для config прочитан CLAUDE.md/REVIEW.md.
- Дверь похожести rc=0; CON-3770 прочитан, общий приёмник, не детектор. Комменты в оба тикета.
- Исходный JSONL: user=claude 2026-09-27T01:08:13.778Z, user=python3 01:12:22.102Z; абсолютный путь в DAN-305.
- fake exec: ['/fake/claude','claude','logs','69a90090']; ['/fake/claude','python3','-c','CODE'].
- Реальный call site config/scripts/slot-org-probe.sh:227 — claude -p; сохраняется.
- Границы до правок — коммент DAN-305 72fd9b0e-a4b9-4f61-8e73-c8e0bb7f670f.
- Единственный детектор: SessionManager._exec → _observe_command_as_prompt; только feed, без prompt/code в ленте.
- config worktree: /Users/philosopher/Projects/config/.worktrees/fix-dan-305-postmortem, ветка fix/dan-305-postmortem от origin/master.
- Карта и каталог записаны под hooks/shared-file-lock.sh; payload канонического файла блокирует общий замок, запись только в worktree.
- Реестр: critic-week, command-as-prompt-labels.tsv; без доли ложных и промоции.
- База consensus/search: релевантного нет (search WEAK); веб clig.dev/#errors + Python argparse.error открыт 27-09-2026.
- paperthin/skills/depth/mandela/SKILL.md, LICENSE MIT прочитаны как источник; 8 паттернов разобраны в failure-modes.md.
- Снимки главных копий и UTC старта: /tmp/dan-305-start/{time,cswap.status,config.status}.

## Отвергнуто
- Блок/предупреждение/переписывание argv: намерение по форме ввода недоказуемо, контракт сохраняется.
- Общий запрет claude/python3: ломает легальные prompts и существующий call site.
- Установка в системный Python, live model calls, деплой и перелогин — не делались.

## Открытые вопросы
- Живой замер ложных ещё невозможен без развёртывания; назначен critic-week, стадия feed.
- Проверить независимым ревью полноту тестов и границы обоих PR.

## Команды, которые уже гоняли
- UV_CACHE_DIR=/tmp/dan-305-uv-cache timeout 180 uv sync --locked --group dev → изолированная .venv.
- timeout 900 .venv/bin/python3 -m pytest tests/test_run_observation.py -q → RED 4 failed, 12 passed (до кода).
- timeout 900 .venv/bin/python3 -m pytest tests/test_run_observation.py tests/test_cli.py tests/test_session.py -q → GREEN 282 passed in 5.89s.
- timeout 900 .venv/bin/python3 -m pytest tests -q → идёт, вывод /tmp/dan-305-start/full.txt.
- config: failure-catalog.py index --write/check → 194 записей, индекс совпадает.
- config: system-md-index-check.sh → OK, 332 имён.
- config: guard-registry.py lint --base origin/master --head HEAD → 0 находок; повторить после коммита.
