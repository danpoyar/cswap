# Прогресс CON-2420 — что проверено, отвергнуто и где стоп (CON-2044)
<!-- Читатель — та же сессия после автосжатия контекста или преемник после пересадки тикета: читает этот файл ПЕРВЫМ, до паспорта JOURNAL.md. Писатель — агент тикета, ПО ХОДУ: после каждого рубежа (красная проба показана, вариант отвергнут, сьют прогнан, PR открыт). Коротко и указателями (файл:строка, команда, PR, коммит), не дампом разговора; потолок 120 строк. Коммить явным путём вместе со своими файлами. Хозяин формата — scripts/progress-file.sh. -->
создан: 2026-09-06 21:29 +0200 · сессия: fix-con-2420 · посадка: session:Yor

## Где стоп
- ГОТОВО: PR #48 слит (squash) 06-09 21:53 +0200, sha main 8a0a25c; CI main run 34056298915 зелёный; деплой scripts/deploy.sh main → DEPLOY-OK (receipt 1788724589); CON-2420 в Done (финал-коммент с Проверка/Ревью/Альтернативы/ДОКЛАД); сосед DAN-144 (тексты deferral у вызывающих). Преемнику делать нечего.

## Проверено
- Проблема жива на HEAD ee24e95: except-ветка switcher.py:2098–2104 → `return None, None` (сажает сайдкар вслепую).
- Дверь дублей ticket_similarity.py door: ярусов дубль/тот же хозяин нет (макс 0.07). ADR в репо нет (docs/ отсутствует).
- Базовый прогон TestSpilledAdoptionReconcile на HEAD: 10 passed.
- Коммент в тикет: критерий + практики + варианты (A defer без предела — выбран) + границы — положен 06-09 21:33 +0200 (createdAt Linear 19:33:29Z; первый коммит 21:37:54 +0200).
- Красная проба показана комментом (FileNotFoundError на копии профиля — хук снёс её после слепой посадки).
- Ревью р.1 (code-reviewer effort max, CLI): «фактических нет, 2 нита», можно принимать; ниты — тексты deferral только про Keychain (в границах — починил в switcher.py; вне границ — DAN-144), время коммента границ в этом файле (починил). Сьюты switch_heal/refresh/reseed/bootstrap_family_guard после правки — 106 passed.
- Ревью р.2: approve (head d681956), оба нита закрыты, спот-чек ревьюера 2 passed; review-verdict-check --dir OK.
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
