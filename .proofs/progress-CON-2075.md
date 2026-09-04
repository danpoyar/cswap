# Прогресс CON-2075 — что проверено, отвергнуто и где стоп (CON-2044)
<!-- Читатель — преемник ротации: читает этот файл ПЕРВЫМ, до паспорта JOURNAL.md. Писатель — агент тикета, ПО ХОДУ: после каждого рубежа (красная проба показана, вариант отвергнут, сьют прогнан, PR открыт) и у потолка контекста. Коротко и указателями (файл:строка, команда, PR, коммит), не дампом разговора; потолок 120 строк. Коммить явным путём вместе со своими файлами. Хозяин формата — scripts/progress-file.sh. -->
создан: 2026-09-04 23:28 +0200 · сессия: chore-con-2075 · посадка: session:Yor

## Где стоп
- 00:02 05-09 (date): ревью р.2 approve (resume сессии р.1, 0 находок; вердикт-файл round 2, head 2cfa4ea). Следующий шаг: коммит вердикта (.proofs только — вердикт не протухает) → CI зелёный → `gh pr merge 37 --squash` руками (без --auto) → `bash scripts/deploy.sh main` (timeout 600) → Done с «Ревью:», «Проверка:», ДОКЛАД, Альтернативы, Что спроектировано плохо → SendMessage Yor.

## Проверено
- Баг жив на c3529fc: после reconcile разлива при живом PID лгут ОБА оракула `_backup_is_newer` (refresh.py:136): маркер стоит (switcher.py `_post_backup_write` ~511) И seed-штамп = предшественник (reconcile не перештамповывает, adopt_profile_family — да, switcher.py ~3121–3145). Второй `switch --even-if-live` сажает consumed at-profile-2. Проба: $CLAUDE_JOB_DIR/tmp/test_red_probe.py (копия логики уйдёт в тест).
- Сьют tests/test_switch_heal.py на базе: 16 passed; новые 5 тестов до фикса: 3 красных (marker exists) + 2 зелёных (негатив чужого разлива, idle-профиль без штампа) — стражи; после фикса 21 passed.
- Фикс (switcher.py): константа SPILL_ORIGIN_PROFILE; persist_backup_credentials(origin=) → _spill_rotated_credentials пишет payload["origin"]; _reconcile_spilled_rotation_locked после посадки зовёт _settle_live_profile_after_spill (живой PID + не drifted + профиль читается + (origin==profile или fp(profile)==fp(landed)) → _stamp_profile_generation: seed := fp, маркер снят); adopt_profile_family ставит origin и штампует тем же хелпером.
- docs/_adrs/ в репо cswap нет; решений новее тикета нет (только #36 — замки, предмет не задет).
- Спилл рождают только persist_backup_credentials (коллектор switcher.py ~3882, autoswitch.py ~1334, adopt_profile_family ~3111); reconcile зовут: коллектор ~3868, autoswitch ~1309, bootstrap session.py:1041, reseed.py:322, refresh.py:259.

## Отвергнуто
- Вариант «снять маркер в `_post_backup_write` по равенству поколений» — чтение профиля (Keychain) на КАЖДОЙ записи бэкапа под живой сессией, seed всё равно не чинит, окно «ротация между разливом и reconcile» не закрывает.
- Вариант «правило только fp(spill)==fp(profile) без происхождения разлива» — оставляет окно: сессия ротировала между разливом и reconcile → бэкап = consumed под маркером → следующий heal сажает мёртвое.
- Вариант «судья `_backup_is_newer` не верит маркеру» — после посадки разлива seed ≠ fp(бэкапа), из состояния диска правду не восстановить без происхождения.

## Открытые вопросы
- Окно «сессия ротировала И вышла до reconcile» (idle): хук инвалидирует профиль, новое поколение теряется — вне объёма, отдельный тикет (завести в финале).

## Команды, которые уже гоняли
- `env -u FORCE_COLOR timeout 900 uv run pytest -q` — 2117 passed, 3 skipped, 1 xfailed (72 с) на 50ac832.
- ревью р.1: `claude --agent code-reviewer --effort max -p --permission-mode bypassPermissions --output-format json` (фон Bash) — 41 ход, approve (1 minor, 3 nit).
- ревью р.2: то же с `--resume 79165e7b-c424-438b-bf99-05cda17e2e39` — 7 ходов, approve, 0 находок.
- тест exiting_between на 50ac832 (оверлей старого switcher.py) — 1 failed; на 2cfa4ea — 22 passed; полный сьют 2118 passed, 3 skipped, 1 xfailed.
- `bash ~/projects/config/scripts/conflict-markers-check.sh $PWD` — 0 OK.
- `env -u FORCE_COLOR timeout 900 uv run pytest -q tests/test_switch_heal.py` — 16 passed (база).
- красная проба tests/_probe_con2075.py (временный файл, удалён) — FAILED: marker exists True, landed at-profile-2.
- `env -u FORCE_COLOR timeout 900 uv run pytest -q tests/test_switch_heal.py -k TestSpilledAdoptionReconcile` — до фикса 3 failed / 2 passed; после фикса весь файл 21 passed.
