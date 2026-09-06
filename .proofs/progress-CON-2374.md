# Прогресс CON-2374 — что проверено, отвергнуто и где стоп (CON-2044)
<!-- Читатель — та же сессия после автосжатия контекста или преемник после пересадки тикета: читает этот файл ПЕРВЫМ, до паспорта JOURNAL.md. Писатель — агент тикета, ПО ХОДУ: после каждого рубежа (красная проба показана, вариант отвергнут, сьют прогнан, PR открыт). Коротко и указателями (файл:строка, команда, PR, коммит), не дампом разговора; потолок 120 строк. Коммить явным путём вместе со своими файлами. Хозяин формата — scripts/progress-file.sh. -->
создан: 2026-09-06 20:05 +0200 · сессия: fix-con-2374 · посадка: session:Yor

## Где стоп
- Фикс и тест написаны, сьют файла зелёный (16/16). Следующий шаг: полный сьют → коммит → PR → ревьюер code-reviewer (риск-зона auth, effort max) → merge руками → CI main → перевести CON-2374 в Done дверью статусов.

## Проверено
- Тикет прочитан целиком (Linear, 0 комментов до меня); коммент актуальность+границы+альтернативы+практики положен (id b4954653).
- Красная проба: tests/test_bootstrap_family_guard.py::TestOneReadFeedsAdoptionAndGuard::test_stale_marker_over_unreadable_profile_and_empty_backup_keeps_the_family — на HEAD 3fb9e1c маркер снесён, лог «Invalidated session credentials for account 2» при занятом Keychain.
- Фикс: src/claude_swap/session.py, stale-ветка setup_session — суд по profile_read[1] только, raise SessionError(_unreadable_stale_message); докстринг _seed_backup_undecidable про слепоту над "".
- Сьют файла после фикса: 16 passed.
- Дверь дублей: CON-2375 «похожий» Todo (коллектор) — не стоп. docs/_adrs/ в репо нет.

## Отвергнуто
- Fall-through после сохранения маркера (как CON-1740): бутстрап падает «no stored credentials — re-add» — ложный совет под таймаутом Keychain + лишняя проба auth status.
- Учить _seed_backup_undecidable считать "" нерешаемым: общий страж трёх вызовов, двойной смысл байтов остаётся.
- Трёхзначный читатель бэкапа в credentials.py: правильно, но десяток вызовов в switcher.py — отдельная работа.

## Открытые вопросы
- (пусто — что не решено и кто решает)

## Команды, которые уже гоняли
- env -u FORCE_COLOR timeout 600 uv run --group dev pytest -q tests/test_bootstrap_family_guard.py -p no:cacheprovider — до фикса 1 failed/15 passed, после 16 passed.
- python3 ~/projects/config/scripts/ticket_similarity.py door … --exclude CON-2374 — rc=5, CON-2375 похожий Todo.
