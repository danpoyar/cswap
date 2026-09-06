# Прогресс CON-2375 — что проверено, отвергнуто и где стоп (CON-2044)
<!-- Читатель — та же сессия после автосжатия контекста или преемник после пересадки тикета: читает этот файл ПЕРВЫМ, до паспорта JOURNAL.md. Писатель — агент тикета, ПО ХОДУ: после каждого рубежа (красная проба показана, вариант отвергнут, сьют прогнан, PR открыт). Коротко и указателями (файл:строка, команда, PR, коммит), не дампом разговора; потолок 120 строк. Коммить явным путём вместе со своими файлами. Хозяин формата — scripts/progress-file.sh. -->
создан: 2026-09-06 20:16 +0200 · сессия: fix-con-2375 · посадка: session:Yor

## Где стоп
- 06-09 20:45: ревью р.1 approve (0 Important, 3 нита; sid 5c77c8cd-c43a-4edc-9fba-a0a0a3920f6f, вердикт-файл .proofs/review-verdict-fix-con-2375.json на head 3c0fa17). Дальше: коммит вердикта → CI PR на новом head → мерж руками (squash, без --auto) → CI main → deploy дверью scripts/deploy.sh main → перевести CON-2375 в Done дверью статусов.

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
- `gh pr create --base main --head fix/con-2375 …` → https://github.com/danpoyar/cswap/pull/47; `gh pr edit 47 --body-file` (исправлено число смежных: 36 из 49).
- `python3 ~/projects/config/scripts/review-rounds.py judge --dir .` → кругов 0, следующий раунд 1, ok.
- `claude --agent code-reviewer --effort max --permission-mode bypassPermissions --session-id <sid> --output-format json -p …` (фон run_in_background, timeout 3600).
- `env -u FORCE_COLOR timeout 900 uv run --group dev pytest -q tests/test_bootstrap_family_guard.py tests/test_refresh.py tests/test_reseed.py tests/test_switch_heal.py` — 103 passed.
- `env -u FORCE_COLOR timeout 900 uv run --group dev pytest -q` — 2196 passed, 3 skipped, 1 xfailed.
- База для сравнения: `git worktree add --detach $CLAUDE_JOB_DIR/tmp/base-main origin/main` (снести в финале: `git worktree remove`).
- `env -u FORCE_COLOR timeout 600 uv run --group dev pytest -q tests/test_switch_heal.py -k TestSpilledAdoptionReconcile` — 1 failed (красный, ожидаемо), 9 passed.
- `python3 ~/projects/config/scripts/ticket_similarity.py door --title … --desc-file … --exclude CON-2375` — rc=5, CON-2374 похожий 0.43.
