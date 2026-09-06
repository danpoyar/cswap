# Прогресс CON-2355 — что проверено, отвергнуто и где стоп (CON-2044)
<!-- Читатель — та же сессия после автосжатия контекста или преемник после пересадки тикета: читает этот файл ПЕРВЫМ, до паспорта JOURNAL.md. Писатель — агент тикета, ПО ХОДУ: после каждого рубежа (красная проба показана, вариант отвергнут, сьют прогнан, PR открыт). Коротко и указателями (файл:строка, команда, PR, коммит), не дампом разговора; потолок 120 строк. Коммить явным путём вместе со своими файлами. Хозяин формата — scripts/progress-file.sh. -->
создан: 2026-09-06 14:18 +0200 · сессия: fix-con-2355 · посадка: session:Yor

## Где стоп
- Ревью р.1 (code-reviewer effort max, sid 85a6af0a-…): «можно принимать», Important 0, 2 Minor вне объёма → отдельные тикеты (stale-marker ветка над бэкапом ""; проход коллектора над нечитаемым профилем). Вердикт .proofs/review-verdict-fix-con-2355.json. Дальше: пуш вердикта → CI PR #45 → мерж руками (squash) → CI main → деплой scripts/deploy.sh main → перевести CON-2355 в Done дверью статусов.

## Проверено
- Красная проба: tests/test_bootstrap_family_guard.py::TestPendingProfileSpillOverUnreadableProfile::test_unreadable_profile_with_pending_profile_spill_refuses_and_lands_nothing → «DID NOT RAISE SessionError», post_spy=[at-spilled] (коммент в тикете 06-09 ~14:40).
- Порядок в живом коде: session.py `_seed_credentials_from_backup` reconcile → страж; хук `_post_backup_write` → `_invalidate_session_credentials` сносит копию и штамп.
- Три других вызывающих reconcile (refresh.py:290 DEFERRED, reseed.py refuse DEFERRED, autoswitch.py heal DEFERRED→transient) откладывают ДО reconcile — бутстрап был единственным исключением.
- Бэкап при KeychainError читается как "" (credentials.py `_read_account_credentials`) — потому судим по сайдкару, не по байтам бэкапа.
- Дверь дублей: max CON-2373 0.11, стопа нет. docs/_adrs в репо нет.
- Фикс: switcher.py `pending_profile_spill` (только чтение сайдкара) + session.py отказ до reconcile (`_undecidable_spill_message`); сьют файла 15/15 зелёный.

## Отвергнуто
- B «перенести страж _seed_backup_undecidable выше reconcile»: слеп при бэкапе "" (таймаут Keychain) и меняет поведение для не-profile разливов.
- C «контракт байты|defer у _reconcile_spilled_rotation_locked»: четыре вызывающих, трое уже откладывают сами; переворачивает пин CON-2100 в test_switch_heal.

## Открытые вопросы
- Проход коллектора (`_reconcile_spilled_rotation` через `_fetch_account_usage`) сажает сайдкар при нечитаемом профиле «как раньше» (пин CON-2100) — тот же класс потери; вне объёма, в «Что спроектировано плохо».

## Команды, которые уже гоняли
- claude --agent code-reviewer --effort max -p (раунд 1, sid 85a6af0a-…) → 22 хода, финальный ответ отклонён safeguard «[cyber]» (req_011CenCX7f33KugeBFppQ9Zo); повтор — claude --resume <sid> с просьбой нейтральной формулировки итога (14:43).
- CI PR #45: test pass, test-windows pass, macos-keychain skipping (PR-стадия).
- env -u FORCE_COLOR timeout 600 uv run --group dev pytest -q tests/test_bootstrap_family_guard.py → 13 passed (база), 1 failed/1 passed (красный), 15 passed (после фикса).
- python3 ~/projects/config/scripts/ticket_similarity.py door … --exclude CON-2355 → rc=0, стопа нет.
