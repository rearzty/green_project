# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Контекст проекта

Хакатонный сервис автопроектирования городского озеленения с учётом отступов от инженерных сетей (задача, команда, таймлайн — в [README.md](README.md)). Ключевой факт, объясняющий многие решения в коде: **реальные данные организаторов открываются только 15.09.2026**, поэтому весь пайплайн сейчас разработан и проверен на синтетических территориях (`scripts/generate_synthetic_data.py` → `data/synthetic/*.geojson`, гитигнорится). Из-за этого некоторые вещи *намеренно* не захардкожены — см. раздел "Плейсхолдеры" ниже.

Подробности архитектуры, API-контракта, обоснований решений и истории изменений — в [docs/architecture.md](docs/architecture.md), [docs/api_contract.md](docs/api_contract.md), [docs/decision_log.md](docs/decision_log.md), [docs/worklog.md](docs/worklog.md).

## Команды

Python зафиксирован на 3.11 (`.python-version`) — на более новых версиях риск проблем со сборкой GDAL/Fiona/Shapely/pyproj.

```bash
pip install -e ".[dev]"                       # установка (backend + geo_engine + ml_scoring)

pytest                                        # все тесты
pytest tests/test_geo_engine/test_pipeline.py # один файл
pytest tests/test_geo_engine/test_pipeline.py::test_buildable_area_is_subset_of_territory  # один тест

python -m scripts.generate_synthetic_data     # тестовые территории -> data/synthetic/
python -m ml_scoring.train_synthetic          # обучить ML-скоринг (пишет ml_scoring/artifacts/model.joblib,
                                               # без него scoring_mode="ml" упадёт с ModelNotTrainedError)

python -m uvicorn backend.app.main:app --reload  # backend (нужен PostgreSQL+PostGIS, см. GREENPROJECT_DATABASE_URL)
```

Фронтенд (`frontend/`): `npm install`, `npm run dev`, `npm run build`, `npm run lint`.

Полный стек одной командой (собирает свежую ML-модель внутри образа при билде):
```bash
docker compose -f infra/docker-compose.yml up --build
```
backend + Swagger — `localhost:8000/docs`, frontend — `localhost:3000`.

Тесты лежат целиком в корневом `tests/` (зеркалит `geo_engine`/`ml_scoring`/API), не внутри `backend/`.

## Архитектура

Монорепо из трёх независимых Python-пакетов и одного слоя-склейки:

- **`geo_engine/`** — чистая геометрия, ноль импортов FastAPI/SQLAlchemy. Доменная модель в `model.py` (`Utility`, `Zone`, `PlantingCandidate`, `PlantingItem`).
- **`ml_scoring/`** — скоринг кандидатов, зависит только от `geo_engine.model`/`geo_engine.norms`, тоже без FastAPI/БД. Две взаимозаменяемые стратегии (`HeuristicScorer`, `MLScorer`) за одним интерфейсом `ScoringStrategy`.
- **`backend/app/services/pipeline_service.py`** — единственное место, где `geo_engine` и `ml_scoring` импортируются вместе. Это намеренно: оба пакета можно тестировать и использовать отдельно от сервера.

Порядок пайплайна (каждый шаг — реальная функция, не только концепция):
```
read (geo_engine/io/*) -> Utility/Zone (нормализация по слоям/полям)
  -> build_exclusion_zone + buildable_area (buffers.py)
  -> generate_candidates (candidates.py)
  -> ScoringStrategy.score (ml_scoring)
  -> greedy_select (placement.py)
  -> Plan/PlantingItemRow в БД -> GeoJSON на фронт / DXF через io/dxf_writer.py
```

Нормативы — конфиг, не код: `geo_engine/config/planting_norms.yaml`, читается через `geo_engine/norms.py::load_norms()`. Значения — черновик из текста задания, ждут сверки по полному ТЗ 15.09.

**Геометрия в БД (`backend/app/db/models.py`)**: колонки — `Geometry(geometry_type="GEOMETRY", srid=0)`, намеренно без фиксированного SRID, потому что CRS каждого проекта заранее неизвестна. `Project.source_crs` (свободный текст) хранит, в какой CRS реально лежит геометрия проекта; перепроекция в WGS84 для GeoJSON делается в `backend/app/services/geo_io.py::_to_wgs84()` через `pyproj.Transformer` напрямую, а не через PostGIS SRID transform.

**Известный пробел**: `geo_engine/crs.py::ensure_metric_crs()` (авто-определение UTM-зоны и репроекция в метрическую CRS) написана, но нигде не вызывается в реальном потоке — `project_service.create_project_from_file()` кладёт геометрию как есть. Пока всё на синтетике (уже в метрах) это не страшно, но до доверия к буферам на реальных геоданных это нужно подключить.

**Frontend**: `frontend/lib/api.ts` — единый типизированный клиент под весь API-контракт. `frontend/components/MapView.tsx` подгоняет вид карты под фактические данные (`FitBounds`), а не центрируется на фиксированную точку — не возвращай туда хардкод центра, это уже приводило к багу (карта заливкой на весь мир), см. `docs/worklog.md`.

## Плейсхолдеры — не "чинить" на конкретные значения без вопроса пользователю

- `geo_engine/io/dxf_reader.py::DEFAULT_LAYER_MAP` — соответствие "имя слоя DXF → object_type/zone_type" придумано для синтетики, реальные имена слоёв Мосгеотреста неизвестны до 15.09.
- `type_field` в `shp_geojson_reader.py::read_vector_file()` — то же самое для GeoJSON/SHP.
- CRS нигде не захардкожена как МСК-77/UTM — см. пробел выше.
- Точные значения в `planting_norms.yaml` — черновик, не итог.
