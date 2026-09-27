# DAN-305 · Наблюдение команд, переданных как запрос модели

## Где стоп
- Детектор, тесты и разбор закоммичены, полный pytest зелёный.
- Р.1 получен; замечания исправлены. Закоммитить, прогнать проверки и продолжить ТОГО ЖЕ ревьюера ca471088 после завершения текущего хода.

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
- Общий запрет claude/python3: ломает допустимые prompts и существующий call site; существование вызова не доказывает корректность.
- Установка в системный Python, live model calls, деплой и перелогин — не делались.

## Открытые вопросы
- Живой замер ложных ещё невозможен без развёртывания; назначен critic-week, стадия feed.
- Р.1: cswap approve, config revise (major: ошибочно оправдан call site). Все 7 замечаний приняты, исправления подготовлены; нужен confirm того же ревьюера.

## Команды, которые уже гоняли
- UV_CACHE_DIR=/tmp/dan-305-uv-cache timeout 180 uv sync --locked --group dev → изолированная .venv.
- timeout 900 .venv/bin/python3 -m pytest tests/test_run_observation.py -q → RED 4 failed, 12 passed (до кода).
- timeout 900 .venv/bin/python3 -m pytest tests/test_run_observation.py tests/test_cli.py tests/test_session.py -q → GREEN 282 passed in 5.89s.
- Полный pytest в sandbox: 1 failed, 2232 passed; process env probe недоступен. Вне sandbox: 2234 passed, 3 skipped, 1 xfailed in 97.72s; /tmp/dan-305-start/full-unsandboxed.txt.
- config: failure-catalog.py index --write/check → 195 записей, индекс совпадает.
- config: system-md-index-check.sh → OK, 332 имён.
- После Windows-кейса целевой прогон → 283 passed in 9.31s; отдельно детектор 17 passed.
- config: guard-registry.py lint --base origin/master --head HEAD → 0 находок после коммита.
- Обе ветки fetch+merge актуальной базы → Already up to date.
- Config run-all: запятая в --only не поддержана, пустой набор rc=3; два --only прошли сьюты, но коммит автора внутри окна вызвал герметичность; повтор полного прямого набора на неизменном HEAD → SUITES-VERDICT: green 5 (99s).

- PR: https://github.com/danpoyar/cswap/pull/51 (Linux/Windows SUCCESS); https://github.com/3c418/config/pull/2848.
- Ревьюер: /tmp/dan-305-start/reviewer-spawn.txt; риск tests/thresholds, автомерж запрещён наказом двери.
- Сверка главных копий: новых status-строк нет, commit: с начала нет в обеих.

- Первичный источник нового вывода: config/.proofs/progress-CON-2698.md:40 — повторное claude становится prompt, правильная форма без него.
- Р.1: .proofs/review-verdict-fix-dan-305.json и config/.proofs/review-verdict-fix-dan-305-postmortem.json; файлы валидны.
- Двойной отказ: /tmp/dan-305-review/probe_failures.py прочитан; RED stderr 3 failed, 17 passed → GREEN targeted 286 passed in 3.79s.
- Исправлены: stderr=None/EPIPE/closed не рвут запуск; UTC assert; тест не называет blind spot легальным; каталог, связь карты с session.py, ограничения поиска транскрипта.
- Хвост: слот-проба и docs с лишним claude — отдельная сверка хозяина, ущерб auth-пробе не доказан; не править здесь, тикет-следствие не создан.
