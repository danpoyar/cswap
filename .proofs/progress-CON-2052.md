# Прогресс CON-2052 — что проверено, отвергнуто и где стоп (CON-2044)
<!-- Читатель — преемник ротации: читает этот файл ПЕРВЫМ, до паспорта JOURNAL.md. Писатель — агент тикета, ПО ХОДУ: после каждого рубежа (красная проба показана, вариант отвергнут, сьют прогнан, PR открыт) и у потолка контекста. Коротко и указателями (файл:строка, команда, PR, коммит), не дампом разговора; потолок 120 строк. Коммить явным путём вместе со своими файлами. Хозяин формата — scripts/progress-file.sh. -->
создан: 2026-09-04 15:04 +0200 · сессия: fix-con-2052 · посадка: session:Yor

## Где стоп
- 16:05 +0200: ревью р.1 — не принимать (1 Important: ложная тревога last-account на early/consume-first; 1 Minor: API-key last resort после фильтра; 4 нита). Починено Important, Minor, 2 нита (приглушение события в TUI/CLI, detail хвоста); тесты добавлены, полный pytest зелёный (2105). Следующий шаг: коммит → раунд 2 тем же маршрутом (свежий прогон с отчётом р.1, судит только Important) → вердикт-файл р.2 → merge → deploy.sh main → приёмка по логу → финал.

## Проверено
- Проблема жива: демон переключал 21→32 ещё 09:47:35Z (at-limit) и 12:39:47Z (at-limit) — ~/.claude/logs/cswap-auto.log; `env -u CLAUDE_CONFIG_DIR cswap status` 15:09:47 +0200 → Account-32.
- PR #32 был задеплоен ДО 11:00 (строки return-home-wait «cswap run session holds Account-21» кончились 08:25:07Z, return-home на 21 прошёл 08:25:42Z — поведение #32). Значит ворота стояли, а судья ответил «не делит».
- Ворота в демоне УЖЕ есть: autoswitch.py `_freshen_target` (строка ~1172) — все триггеры цикла freshen+switch + return-home; судья `switcher.py:_live_session_shares_login` (~5754).
- Проба судьи на живом слоте 32 ($CLAUDE_JOB_DIR/tmp/probe_judge.py): fp профиля c73… ≠ fp бэкапа 497… ≠ seed 2cb… (файл .seed-fingerprint от 03-09 12:03) → ветка «бэкап двигался после посева» → False. PID 28157 = `claude -c --name Yor` (старт 03-09 12:19:05). Токен профиля выдан 08:27:56 +0200 (exp 16:27:56), бэкапа — 08:48:44 (exp 16:48:44).
- Существующий тест этой формы: tests/test_switcher.py::TestLiveSessionSharesLoginJudge::test_backup_rewritten_after_seeding_never_shares (три поколения → False) — его ожидание переворачивается фиксом.
- Флот: yor-slot-move.sh лечит через `cswap reseed`, не `cswap switch` → ужесточение судьи флоту не мешает (grep ~/projects/config/scripts).
- ADR: docs/_adrs/ в форке нет; решения — FORK.md §CON-2030 (строки 307–343) и §reseed.

## Проверено (после фикса)
- Красное показано: судья False на трёх поколениях; `_freshen_target` → «ok»; демон в тесте SWITCHED на слот с живой сессией (tests/test_autoswitch.py::TestLiveLoginSlotSkip, tests/test_switcher.py::TestLiveSessionSharesLoginJudge).
- Первый полный pytest после фикса: 2100 зелёных, 2 красных — оба старых теста строят ту же форму «три поколения» и ждали warn-and-proceed (test_switch_heal.py::test_stale_marked_profile_with_live_session_keeps_its_copy, test_switcher.py::test_superseded_profile_family_keeps_warn_and_proceed) — пересужены: отказ + проход под --even-if-live.

- Ревью р.1: вердикт-файл .proofs/review-verdict-fix-con-2052.json (revise, head 9589d70); репро ревьюера /tmp/con2052-review/test_repro_alert.py показал красное на ветке (last-account на здоровом парке) — покрыто тестами test_early_swap_stays_put_without_the_last_account_cry / test_consume_first_stays_put_without_the_last_account_cry / test_api_key_last_resort_stands_after_the_live_login_filter.

## Отвергнуто
- Ниты р.1 не взяты: двойной account_kind_for в _freshen_target (дешёвое чтение sequence.json, ранний return для api_key нужен до PID-скана); двухфазный refetch до фильтра (только цена fetch'ей, перестройка вне объёма).
- Слепой PID-скип в демоне (откат #32 для proactive/at-limit): парк встаёт на стене при выжженном доме и живых сессиях на всех кандидатах; дефект судьи остаётся для ручного switch.
- Судить по expiresAt «кто новее»: сегодня бэкап новее → демон всё равно сажает логин на слот Yor, а reseed отказывает активному слоту (ACTIVE_SLOT) → Yor нельзя вылечить, пока логин на 32.
- Ручное `cswap switch 21` для разгрузки — граница брифа (делает Yor).

## Открытые вопросы
- Логин сейчас на 32 (демон 12:39:47Z); токены обеих копий обновятся ~16:28 и ~16:49 +0200 — если семья одна, одна копия умрёт. Решает Yor (переключить рукой), не агент.

## Команды, которые уже гоняли
- `env -u FORCE_COLOR timeout 600 uv run --directory <wt> pytest -q tests/test_switcher.py::TestLiveSessionSharesLoginJudge tests/test_autoswitch.py::TestLiveLoginSlotSkip` — до фикса 8 красных, после 16 зелёных.
- `env -u CLAUDE_CONFIG_DIR timeout 120 uv run --directory <wt> python $CLAUDE_JOB_DIR/tmp/probe_judge.py 32 21` — 32: False (три поколения), 21: True (профиль нечитаем, PID нет).
- `security find-generic-password -s "Claude Code-credentials-3baa964a"` (без -w) — mdat 20260904062756Z.
