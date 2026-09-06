# Прогресс CON-2100 — что проверено, отвергнуто и где стоп (CON-2044)
<!-- Читатель — та же сессия после автосжатия контекста или преемник после пересадки тикета: читает этот файл ПЕРВЫМ, до паспорта JOURNAL.md. Писатель — агент тикета, ПО ХОДУ: после каждого рубежа (красная проба показана, вариант отвергнут, сьют прогнан, PR открыт). Коротко и указателями (файл:строка, команда, PR, коммит), не дампом разговора; потолок 120 строк. Коммить явным путём вместе со своими файлами. Хозяин формата — scripts/progress-file.sh. -->
создан: 2026-09-06 09:44 +0200 · сессия: guard-con-2100 · посадка: session:Yor

## Где стоп
- Ревью р.2 (~10:15, продолжение того же ревьюера): фактических нет, нитов нет, «можно принимать»; вердикт-файл round 2 head e9ac0fb в PR. Дальше: CI PR на e9ac0fb → `gh pr merge 44 --squash --repo danpoyar/cswap` (авто-мерж репо выключен, защиты ветки нет) → CI main зелёный → `bash scripts/deploy.sh main` (DEPLOY-OK) → перевести CON-2100 в Done дверью статусов с финал-комментом.

## Проверено
- Красная проба жива на origin/main 89b969f: tests/test_probe_con2100.py (временный, из коммента 05-09 00:33) — `assert 'at-profile-2' == 'at-profile-3'`, лог Invalidated(2206) → Reconciled(1896).
- Дверь ticket_similarity (из config и из cswap worktree): стопов нет; decisions --since 2026-09-04 задачу не отменяют; docs/_adrs/ в cswap нет.
- Снимки смежных каталогов: $CLAUDE_JOB_DIR/tmp/snap-config.txt (62 строки), snap-cswap.txt (1: ` M .gitignore` — чужая), start-time.txt.

## Проверено (после правки)
- Новый тест `test_session_rotated_and_exited_before_the_reconcile_lands_the_profile_generation_not_the_sidecar` красный на 89b969f (1 failed, 8 passed) → зелёный после правки; полный сьют форка 2190 passed, 3 skipped, 1 xfailed (74 с); test_switch_heal.py 34 passed.
- Два теста живой ветки (rotated_between_spill / exiting_between_hook_and_settling) переведены на ожидание profile_3 — следствие правила, страховые проверки сохранены.

## Проверено (ревью)
- р.1 Agent tool code-reviewer (effort — наследие сессии, Agent tool параметра effort не имеет; CLI-маршрут --effort max не брал: под парком CLI-ревьюеры рубились таймаутом без вердикта, CON-2126): 0 фактических, 5 нитов; мутации ревьюера: settle no-op → 4 failed, второй скан живости → 1 failed — перенацеленные тесты не слепые. р.2: 0/0.

## Отвергнуто
- B re-judge только idle (второй скан pids — гонка с хуком, r.1 PR #37); C правка хука _post_backup_write (chokepoint всех записей, нет predecessor/origin); D всегда брать профиль (теряет сайдкар при пере-засеве) — разбор в комменте тикета.

## Открытые вопросы
- (пусто — что не решено и кто решает)

## Команды, которые уже гоняли
- полный pytest после нитов — 2191 passed, 3 skipped, 1 xfailed (71.5 с); review-verdict-check.sh --size — 283 строки (нит > 200, < p90 500).
- `env -u FORCE_COLOR timeout 900 uv run --group dev pytest -q` (worktree cswap, слитое дерево) — 2190 passed, 3 skipped, 1 xfailed.
- `env -u FORCE_COLOR timeout 900 uv run --group dev pytest -q -s tests/test_probe_con2100.py` в worktree cswap — 1 failed (красная проба, ожидаемо).
