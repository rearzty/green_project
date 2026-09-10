# GreenProject

Сервис автоматического проектирования озеленения города с учётом расположения подземных коммуникаций и городской среды (международный хакатон 2026, задача «Социальные задачи»).

Детерминированный геометрический алгоритм (буферы нормативных отступов → допустимые зоны → кандидаты посадок → отбор) + ML-ранжирование кандидатов, веб-интерфейс на карте, экспорт итогового плана в DXF.

Подробности архитектуры, API-контракта и обоснований решений — в [docs/architecture.md](docs/architecture.md), [docs/api_contract.md](docs/api_contract.md), [docs/decision_log.md](docs/decision_log.md).

## Быстрый старт (Docker, рекомендуется)

```bash
docker compose -f infra/docker-compose.yml up --build
```

- Backend: http://localhost:8000/docs
- Frontend: http://localhost:3000

## Локальная разработка без Docker

### Backend

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"

python -m scripts.generate_synthetic_data   # тестовые территории в data/synthetic/
python -m ml_scoring.train_synthetic        # обучить ML-скоринг на синтетике
pytest                                      # прогнать тесты

# нужен запущенный PostgreSQL+PostGIS (или GREENPROJECT_DATABASE_URL на внешний)
python -m uvicorn backend.app.main:app --reload
```

### Frontend

```bash
cd frontend
npm install
cp .env.local.example .env.local
npm run dev
```

## Статус

Проект в разработке до открытия реальных данных хакатона (15.09.2026) собран и проверен на синтетических территориях: весь пайплайн (импорт → буферы → кандидаты → скоринг → отбор → DXF-экспорт) работает end-to-end, backend API реализован по контракту, ML-модель обучена на weak-labeled синтетике. После 15.09 — интеграция реальных геоданных Мосгеотреста и полировка UX (см. `docs/architecture.md`, раздел «Контекст и таймлайн»).
