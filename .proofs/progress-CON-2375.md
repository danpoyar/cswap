# Прогресс CON-2375 — что проверено, отвергнуто и где стоп (CON-2044)
<!-- Читатель — та же сессия после автосжатия контекста или преемник после пересадки тикета: читает этот файл ПЕРВЫМ, до паспорта JOURNAL.md. Писатель — агент тикета, ПО ХОДУ: после каждого рубежа (красная проба показана, вариант отвергнут, сьют прогнан, PR открыт). Коротко и указателями (файл:строка, команда, PR, коммит), не дампом разговора; потолок 120 строк. Коммить явным путём вместе со своими файлами. Хозяин формата — scripts/progress-file.sh. -->
создан: 2026-09-06 20:16 +0200 · сессия: fix-con-2375 · посадка: session:Yor

## Где стоп
- 06-09 20:50: PR #47 слит руками (squash) 20:46:49 +0200, sha main ee24e95; CI main run 34052799907 — success; deploy дверью scripts/deploy.sh main → DEPLOY-OK (receipt 1788720562, cswap auto и cswap watch перезапущены). Осталось: самопроверка смежных каталогов → финал-коммент → перевести CON-2375 в Done дверью статусов → строка ящика финалов → выход.

## Проверено
- Тикет прочитан целиком; ADR в репо нет (docs/_adrs отсутствует); CLAUDE.md в репо нет.
- Красный тест: tests/test_switch_heal.py::TestSpilledAdoptionReconcile::test_unreadable_profile_keychain_defers_the_landing_and_keeps_every_copy — `assert None == 'token expired'`, warning «landing the spilled generation as-is» (switcher.py:2050).
- Дверь похожести: CON-2374 In Progress «похожий» без ссылки (session.py, ветка stale-marker) — коммент в оба тикета, не стоп.
- Практики: база — вердикта нет / No coverage; веб — Auth0 rotation, dev.to invalid_grant racing, better-auth #8512; детали в harness-details.tsv нет.
- Фикс: switcher.py `_profile_generation_past_spill` → `(ahead, unreadable)`; `_reconcile_spilled_rotation_locked` при unreadable → warning «deferring» + return None (сайдкар цел); `reconcile_pending_rotation_locked` → `str | None`; session.py/refresh.py/reseed.py — None → SessionError(undecidable spill) / DEFERRED / refuse(DEFERRED) ДО ветки «нет кредов».
- Тесты: test_switch_heal (пин перевёрнут, `_collect_pass` возвращает FetchRecord), test_refresh/test_reseed/test_bootstrap_family_guard — по классу на defer locked-вызывающего с гейтом `_keychain_busy_at_the_landing`; все 4 красные на коде origin/main (PYTHONPATH=base-main/src): `assert None == 'token expired'`, `'transient-error' == 'deferred'`, `DID NOT RAISE ReseedRefusal`, `DID NOT RAISE SessionError`.
- Полный сьют на своём дереве: 2196 passed, 3 skipped, 1 xfailed (74.7 с). ruff: число ошибок в задетых файлах = базе.
- Снимки смежных каталогов: $CLAUDE_JOB_DIR/tmp/snap-cswap.txt (1 строка), snap-config.txt (61 строка), старт snapshot-start.txt.

## Отвергнуто
- Ниты ревью не чиню (раунд 1 без Important — раунд 2 не зову): (1) размер 436 строк — не режется без потери смысла; (2) хелпер `_keychain_busy_at_the_landing` трижды — конвенция репо: хелперы тестов живут в каждом файле (`_creds`, `_make_switcher`), conftest вне границ; (3) except-ветка `_profile_generation_past_spill` «landing as-is» — предсуществующее, отдельный тикет.
- Ревьюер про метод красной пробы: голый PYTHONPATH не переключил бы код (pyproject pythonpath=src); моя проба шла с `--rootdir <база>` — pythonpath=src разрешился в src базы, вывод `claude_swap from base-main/src` и старая строка лога подтверждают.

## Открытые вопросы
- (пусто — что не решено и кто решает)

## Команды, которые уже гоняли
- `gh pr merge 47 --squash` → MERGED ee24e95 (2026-09-06T18:46:49Z); `gh run watch 34052799907` → success; `bash scripts/deploy.sh main` → DEPLOY-OK.
- Тикет-хвост на except-ветку: `linear-issue.sh … --owner src/claude_swap/switcher.py --class collector-spill-judge-exception` → DAN-143 (без -T дверь дала DAN) → перенесён в Quasar issueUpdate → CON-2420.
- `gh pr create --base main --head fix/con-2375 …` → https://github.com/danpoyar/cswap/pull/47; `gh pr edit 47 --body-file` (исправлено число смежных: 36 из 49).
- `python3 ~/projects/config/scripts/review-rounds.py judge --dir .` → кругов 0, следующий раунд 1, ok.
- `claude --agent code-reviewer --effort max --permission-mode bypassPermissions --session-id <sid> --output-format json -p …` (фон run_in_background, timeout 3600).
- `env -u FORCE_COLOR timeout 900 uv run --group dev pytest -q tests/test_bootstrap_family_guard.py tests/test_refresh.py tests/test_reseed.py tests/test_switch_heal.py` — 103 passed.
- `env -u FORCE_COLOR timeout 900 uv run --group dev pytest -q` — 2196 passed, 3 skipped, 1 xfailed.
- База для сравнения: `git worktree add --detach $CLAUDE_JOB_DIR/tmp/base-main origin/main` (снести в финале: `git worktree remove`).
- `env -u FORCE_COLOR timeout 600 uv run --group dev pytest -q tests/test_switch_heal.py -k TestSpilledAdoptionReconcile` — 1 failed (красный, ожидаемо), 9 passed.
- `python3 ~/projects/config/scripts/ticket_similarity.py door --title … --desc-file … --exclude CON-2375` — rc=5, CON-2374 похожий 0.43.
