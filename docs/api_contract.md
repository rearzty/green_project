# API-контракт backend ↔ frontend

Все геометрические ответы — GeoJSON (RFC 7946), `EPSG:4326` (WGS84), готовые для Leaflet. Базовый URL берётся фронтендом из `NEXT_PUBLIC_API_URL`.

| Метод | Путь | Описание |
|---|---|---|
| `POST` | `/api/projects` | Загрузка территории (`multipart/form-data`: `name`, `file` — DXF/GeoJSON/SHP, опц. `source_crs`) → `{project_id}` |
| `GET` | `/api/projects/{project_id}` | Метаданные проекта + все слои (коммуникации, здания, зонирование, территория) в виде `FeatureCollection` |
| `POST` | `/api/projects/{project_id}/generate` | Генерация плана — создаёт **новый** `Plan` (не перезаписывает предыдущий), помечает его `is_current`. Тело: `{planting_types: ["tree","shrub","lawn"], scoring_mode: "heuristic"|"ml"}` → план (`plan_id`, `scoring_mode`, `features`) |
| `GET` | `/api/projects/{project_id}/plans` | История всех планов проекта (не только текущий) — `[{plan_id, scoring_mode, created_at, is_current, item_count}]`, без геометрии (для списка/переключателя в UI) |
| `GET` | `/api/projects/{project_id}/plans/{plan_id}` | Один конкретный план (полная геометрия) |
| `PATCH` | `/api/projects/{project_id}/plans/{plan_id}/items/{item_id}` | Точечная графическая правка (перенос/смена типа/вида) → обновлённый `Feature`. `geometry` — в WGS84 (родная CRS Leaflet), сервер сам перепроецирует в `source_crs` проекта |
| `POST` | `/api/projects/{project_id}/plans/{plan_id}/edit-structured` | Структурированная «текстовая» правка. Тело: `{operation, params}`, операции: `remove_within_radius {x,y,radius_m}`, `replace_type_in_zone {polygon, planting_type}`, `exclude_polygon {polygon}`. Координаты (`x`/`y`, `polygon`) — в WGS84, как выше; `radius_m` — в метрах, не перепроецируется |
| `POST` | `/api/projects/{project_id}/plans/{plan_id}/validate` | Перепроверка плана на нормативы → `{violations: [{item_id, message}]}` |
| `GET` | `/api/projects/{project_id}/plans/{plan_id}/export.dxf` | Скачать итоговый план в DXF |
| `GET` | `/api/config/planting-norms` | Текущий справочник нормативов (для легенды и клиентской валидации) |
| `GET` | `/health` | Liveness-проверка |

## Почему структурированные, а не свободнотекстовые правки

MVP сознательно не включает свободный NL/LLM в критический путь редактирования: `edit-structured` — это фиксированный параметризованный набор операций. «Текстовый» интерфейс на фронте — просто удобная форма поверх них. Тонкий LLM-слой (function calling → маппинг на эти же операции) можно добавить как stretch goal во вторую неделю, не меняя API.

## Не блокирующая валидация

`validate` не запрещает нарушения нормативов после ручной правки — у проектировщика может быть обоснованная причина отступить от нормы. Ответ — предупреждение (`violations`), а не отказ в сохранении правки.
