# Журнал работы

Хронологический лог того, что и зачем делалось в репозитории — короткая история для команды (и для любого Claude, который подключится позже), отдельно от `decision_log.md` (там — обоснования архитектурных решений, здесь — что конкретно было сделано и когда). Новые записи добавляются сверху вниз, в конец файла.

## 2026-09-10 — Основа проекта: пайплайн, backend, frontend, инфра

Контекст: реальные данные хакатона открываются только 15.09.2026, поэтому вся основа собрана и проверена на синтетических территориях, чтобы после открытия данных не терять время на написание алгоритма с нуля. Подробности решений — в [decision_log.md](decision_log.md), архитектура — в [architecture.md](architecture.md).

- [`b50d2b6`](https://github.com/rearzty/green_project/commit/b50d2b6) Скаффолдинг `geo_engine` (буферы отступов, кандидаты посадок, жадный отбор, импорт/экспорт DXF/GeoJSON/SHP) и `ml_scoring` (эвристический скоринг + ML на weak-labeled синтетике), каркас backend (FastAPI, SQLAlchemy-модели, Pydantic-схемы). Python зафиксирован на 3.11 (был 3.14 — риск проблем со сборкой geo-зависимостей на Windows).
- [`a636ba7`](https://github.com/rearzty/green_project/commit/a636ba7) Собран backend API по контракту (`docs/api_contract.md`), сервисный слой связывает `geo_engine`+`ml_scoring`. Добавлен pytest (буферы/кандидаты/непересечение посадок, DXF round-trip, регистрация роутов) — 11 тестов.
- [`2ecd802`](https://github.com/rearzty/green_project/commit/2ecd802) Docker-инфра (`docker-compose`: PostGIS + backend + frontend), Next.js+Tailwind+react-leaflet фронтенд (карта, панель загрузки/генерации/экспорта), документация (`architecture.md`, `api_contract.md`, `decision_log.md`).
- [`e2a6da0`](https://github.com/rearzty/green_project/commit/e2a6da0) Баги, найденные при живом прогоне всего стека через docker compose + браузер (не ловились юнит-тестами): не была подключена репроекция геометрии в WGS84 (карту заливало из-за жёсткого центра на Москву), NaN из GeoPandas ломал вставку в БД, рассинхрон схемы синтетических данных с ридером, преждевременное «нарушений не найдено» до реального вызова валидации.
- [`5d3aa8b`](https://github.com/rearzty/green_project/commit/5d3aa8b) README переписан под онбординг новых участников (и их Claude-сессий) — контекст задачи, роли, статус, что дальше.

**Итог на конец дня:** весь пайплайн (загрузка территории → буферы → кандидаты → скоринг эвристикой/ML → отбор → валидация нормативов → экспорт DXF) работает end-to-end на синтетике, проверено через `pytest`, `docker compose up` и ручной проход в браузере. Репозиторий запушен в [github.com/rearzty/green_project](https://github.com/rearzty/green_project).

**Не сделано (не блокирует, но нужно после 15.09):** обратная репроекция геометрии при графической правке (drag на карте — WGS84, хранение — CRS проекта), проверка ODA File Converter на реальном DWG, ревизия точных значений нормативов по полному тексту ТЗ, разведка открытых слоёв data.mos.ru.

## 2026-09-10 — Ревизия пайплайна: два реальных бага (не плейсхолдеры), найдены и исправлены

Контекст: по просьбе пользователя Claude-сессия прошлась по всему пайплайну в поисках ошибок (не только известных плейсхолдеров из `CLAUDE.md`). Нашла и исправила два бага, воспроизводимых уже на синтетике — оба не были пойманы юнит-тестами, потому что ни один тест не гонял ридер до `generate_plan`.

- **DXF-импорт не мог сгенерировать план.** `dxf_reader.DEFAULT_LAYER_MAP` мапил слой `BOUNDARY` в `zone_type="boundary"`, а `pipeline_service._territory_polygon()` ищет ровно `"territory"` — GeoJSON/SHP-путь работал только потому, что `generate_synthetic_data.py` проставляет `"territory"` напрямую, это нигде не было общим контрактом. Починено: `BOUNDARY` теперь маплится в `"territory"` (см. `CLAUDE.md`). Регрессия: `test_dxf_boundary_layer_imports_as_territory_zone`.
- **`existing_greenery` не исключался геометрически.** Использовался только как мягкий признак скоринга (`existing_greenery_gap_score`, вес 0.15) — `buildable_area()` считала твёрдыми препятствиями только `building`/`road`, так что кандидаты на посадку могли оказаться внутри уже существующей зелени (воспроизведено на синтетике: 2 из 179 кандидатов на дерево попадали внутрь полигона `existing_greenery`). Починено: `existing_greenery` добавлен в `buffers.HARD_OBSTACLE_ZONE_TYPES`. Регрессия: `test_buildable_area_excludes_existing_greenery`. `ml_scoring/artifacts/model.joblib` переобучен (`python -m ml_scoring.train_synthetic`) под новую геометрию `buildable_area`.

## 2026-09-11 — Ручная правка `planting_type` могла рассинхронизировать тип с геометрией

Контекст: живьём воспроизведено через попап точечного объекта на карте — клик "Заменить тип" → случайный клик по "Газон" на объекте-кустарнике давал `planting_type="lawn"` с `geometry.type="Point"` без единой ошибки где-либо в пайплайне. `lawn` везде в `geo_engine` — полигон (`generate_area_candidates`), `tree`/`shrub` — всегда точка (`generate_point_candidates`); такая комбинация не воспроизводима генерацией плана и ничего в `ml_scoring`/DXF-экспорте её не ожидает.

- Фронтенд (`frontend/components/MapView.tsx::bindPlanItemInteractions`) уже сузили до retype-целей tree/shrub для точечных объектов — закрывает только этот один UI-путь.
- Но `backend/app/services/edit_service.py` не проверял геометрию ни в `apply_item_patch` (PATCH `.../items/{id}`), ни в `apply_structured_edit`'s `replace_type_in_zone` (полигональная "Заменить тип в области") — та же плохая комбинация всё ещё достижима напрямую через API или через давно существующий UI-путь с полигоном. Починено: `_assert_planting_type_matches_geometry()` в обоих местах отклоняет несовпадение `planting_type`/`geometry.geom_type`, подняв `PlantingTypeGeometryMismatchError` → HTTP 400 в `routes_edit.py`. Регрессия: `tests/test_api/test_edit_service.py` (7 тестов, без реального PostGIS-соединения — фейковая `Session` и transient ORM-объекты, как договорились в `test_routes_registered.py`).
