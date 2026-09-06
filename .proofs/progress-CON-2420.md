# Прогресс CON-2420 — что проверено, отвергнуто и где стоп (CON-2044)
<!-- Читатель — та же сессия после автосжатия контекста или преемник после пересадки тикета: читает этот файл ПЕРВЫМ, до паспорта JOURNAL.md. Писатель — агент тикета, ПО ХОДУ: после каждого рубежа (красная проба показана, вариант отвергнут, сьют прогнан, PR открыт). Коротко и указателями (файл:строка, команда, PR, коммит), не дампом разговора; потолок 120 строк. Коммить явным путём вместе со своими файлами. Хозяин формата — scripts/progress-file.sh. -->
создан: 2026-09-06 21:29 +0200 · сессия: fix-con-2420 · посадка: session:Yor

## Где стоп
- Фикс + тест + FORK.md готовы, полный сьют зелёный (2199 passed), origin/main слит (up to date). Следующий шаг: коммит, пуш, PR; затем ревьюер code-reviewer effort max (риск-зона auth), вердикт-файл .proofs/review-verdict-fix-con-2420.json; мерж руками (без --auto), CI main, деплой дверью scripts/deploy.sh main, финал Done.

## Проверено
- Проблема жива на HEAD ee24e95: except-ветка switcher.py:2098–2104 → `return None, None` (сажает сайдкар вслепую).
- Дверь дублей ticket_similarity.py door: ярусов дубль/тот же хозяин нет (макс 0.07). ADR в репо нет (docs/ отсутствует).
- Базовый прогон TestSpilledAdoptionReconcile на HEAD: 10 passed.
- Коммент в тикет: критерий + практики + варианты (A defer без предела — выбран) + границы — положен 06-09 ~21:45.
- Красная проба показана комментом (FileNotFoundError на копии профиля — хук снёс её после слепой посадки).
- Фикс: switcher.py except-ветка `_profile_generation_past_spill` → `(None, "judgement failed (…)")`; тест зелёный; полный pytest 2199 passed, 3 skipped, 1 xfailed (75 с); merge origin/main — Already up to date, conflict-markers 0.

## Отвергнуто
- B defer с пределом → тихая потеря после N; C сузить except → падение коллектора целиком (контракт «Never raises»); D отставить сайдкар как forensics → потеря усыновления, если профиль не ротировал. Подробно — коммент тикета.

## Открытые вопросы
- (пусто — что не решено и кто решает)

## Команды, которые уже гоняли
- `env -u FORCE_COLOR timeout 600 uv run --group dev pytest -q tests/test_switch_heal.py -k TestSpilledAdoptionReconcile` — 10 passed (база до правок).
- `… pytest -q tests/test_switch_heal.py -k test_a_failed_profile_judgement…` — 1 failed до фикса (красный), 1 passed после.
- `env -u FORCE_COLOR timeout 900 uv run --group dev pytest -q` — 2199 passed, 3 skipped, 1 xfailed.
- `python3 ~/projects/config/scripts/review-rounds.py judge --dir $PWD` — кругов 0, следующий раунд 1 → ok.
