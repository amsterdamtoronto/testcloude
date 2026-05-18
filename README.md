# Kugoo × FastMotion — Brand Dashboard

Авто-обновляемый веб-дашборд по коллабе FastMotion (`@fastmotionelectric`) с
брендом Kugoo. Собирает метрики со всех роликов канала, у которых в описании
есть подстрока `kugoo.ru`. Обновляется по cron каждый час.

```
backend/fetch.py     YouTube Data API → SQLite history + frontend/data.json
frontend/index.html  Минималистичный дашборд (читает data.json)
frontend/data.json   Снапшот текущих метрик (генерируется backend)
backend/data.db      SQLite-история снапшотов (для будущих графиков)
backend/logs/        Логи запусков
```

## Метрики на дашборде

- Всего роликов
- Сумма просмотров / лайков / комментариев (формат `2.32M`)
- Средние просмотры на ролик
- Engagement Rate = `(Σ likes + Σ comments) / Σ views × 100%`
- Топ-3 ролика по просмотрам (превью, заголовок, дата, ER, просмотры)
- Последнее обновление в `Europe/Moscow`

## Локальный запуск

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export YOUTUBE_API_KEY="AIza..."
python backend/fetch.py
# Откройте frontend/index.html в браузере
```

Открывать `index.html` напрямую двойным кликом работает, но `fetch()`
к `data.json` может упереться в CORS в некоторых браузерах. Если так —
поднимите простой статик-сервер:

```bash
cd frontend && python3 -m http.server 8000
# http://localhost:8000
```

## 1. Получение YouTube Data API key

1. Откройте https://console.cloud.google.com/
2. Создайте проект (например `fastmotion-dashboard`).
3. В меню → **APIs & Services → Library** → найдите **YouTube Data API v3** →
   **Enable**.
4. **APIs & Services → Credentials → Create credentials → API key**.
5. Скопируйте ключ. Рекомендуется в **Restrict key**:
   - **API restrictions** → выберите только YouTube Data API v3.
   - **Application restrictions** можно оставить None (ключ серверный, в env).
6. Дневная квота — 10 000 юнитов. Один запуск тратит ~30 юнитов (1× channels +
   ~N/50 playlistItems + ~N/50 videos). Запас огромный.

## 2. Деплой backend на PythonAnywhere (бесплатный)

PythonAnywhere раздаёт бесплатный аккаунт с поддержкой одного daily cron.
На бесплатном тарифе нет hourly cron, но есть три обхода (выберите один):

- **Tier Hacker** ($5/мес) — даёт hourly cron;
- Бесплатный workaround: запустить через **Always-on task** с внутренним
  циклом (`while True: ... time.sleep(3600)`) — это разрешено правилами;
- Или перенести cron на Railway / Fly.io / GitHub Actions (см. § 4).

### 2.1 Создать аккаунт и склонировать репо

```bash
# в Bash console PythonAnywhere:
git clone https://github.com/<YOUR_USER>/<YOUR_REPO>.git
cd <YOUR_REPO>
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2.2 Настроить git push c PythonAnywhere

Сгенерируйте на GitHub **Fine-grained personal access token**:
Settings → Developer settings → Personal access tokens → Fine-grained →
доступ только к нужному репо, scope `Contents: read & write`.

Настройте remote с встроенным токеном (хранится только на PA):

```bash
git remote set-url origin https://x-access-token:<TOKEN>@github.com/<YOUR_USER>/<YOUR_REPO>.git
git config user.email "bot@fastmotion.local"
git config user.name  "fastmotion-bot"
```

### 2.3 Скрипт обёртки `run.sh`

Создайте в корне репо файл `run.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
export YOUTUBE_API_KEY="AIza..."         # ваш ключ
git pull --rebase origin claude/youtube-brand-dashboard-lAZkN
python backend/fetch.py
if ! git diff --quiet -- frontend/data.json; then
  git add frontend/data.json
  git commit -m "data: $(date -u +%FT%TZ) snapshot"
  git push origin claude/youtube-brand-dashboard-lAZkN
fi
```

```bash
chmod +x run.sh
./run.sh   # тестовый прогон
```

### 2.4 Cron на PythonAnywhere

В разделе **Tasks** добавьте задачу:

- Hourly (или daily для free tier):
  `/home/<YOUR_USER>/<YOUR_REPO>/run.sh >> /home/<YOUR_USER>/<YOUR_REPO>/backend/logs/cron.log 2>&1`

## 3. Деплой frontend на GitHub Pages

`frontend/data.json` коммитится скриптом обратно в этот же репо — нужно
только включить Pages и направить его на папку `frontend/`.

GitHub Pages умеет сервить только с корня репозитория или из `/docs`,
поэтому есть два варианта:

### Вариант А (рекомендуемый) — отдельный repo для Pages

1. Создайте новый репозиторий, например `kugoo-dashboard-x7n2k` (используйте
   неугадываемое имя — это и есть «obscure URL»).
2. Скопируйте в него `frontend/index.html` и `frontend/data.json`.
3. В **Settings → Pages** включите: Source = `Deploy from a branch`,
   Branch = `main` / `(root)`.
4. В `run.sh` на PythonAnywhere вместо `git push origin <ветка>` пушите
   `data.json` именно в этот Pages-репо (например, склонируйте его в
   соседнюю папку и пушьте оттуда).
5. URL дашборда: `https://<YOUR_USER>.github.io/kugoo-dashboard-x7n2k/`.

### Вариант Б — этот же repo, папка `/docs`

1. Переименуйте папку `frontend/` в `docs/` (и поправьте путь в
   `backend/fetch.py`: `FRONTEND_DIR = ROOT / "docs"`).
2. В **Settings → Pages** включите: Source = `Deploy from a branch`,
   Branch = текущая, Folder = `/docs`.
3. URL: `https://<YOUR_USER>.github.io/<YOUR_REPO>/`.

В обоих вариантах поделитесь URL с брендом — паролей нет, но URL не
индексируется поисковиками (`robots: noindex` уже в HTML) и угадать его
извне практически невозможно.

## 4. Альтернатива: GitHub Actions вместо PythonAnywhere

Можно полностью отказаться от PA и крутить cron внутри GitHub Actions:

```yaml
# .github/workflows/fetch.yml
name: fetch
on:
  schedule: [{ cron: "5 * * * *" }]   # каждый час в :05
  workflow_dispatch:
jobs:
  fetch:
    runs-on: ubuntu-latest
    permissions: { contents: write }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt
      - run: python backend/fetch.py
        env:
          YOUTUBE_API_KEY: ${{ secrets.YOUTUBE_API_KEY }}
      - run: |
          if ! git diff --quiet -- frontend/data.json; then
            git config user.email "bot@github.actions"
            git config user.name  "fastmotion-bot"
            git add frontend/data.json
            git commit -m "data: hourly snapshot"
            git push
          fi
```

Сикрет `YOUTUBE_API_KEY` положите в **Settings → Secrets and variables → Actions**.

## 5. Как добавить новый ролик в коллабу

Ничего не нужно. Просто **поставьте `kugoo.ru` в описание ролика**
на YouTube — при следующем запуске скрипт его подхватит автоматически.
Backend каждый час обходит весь канал, фильтрует по подстроке и
пересчитывает агрегаты.

## 6. Переменные окружения

| Variable           | По умолчанию              | Назначение                              |
|--------------------|---------------------------|-----------------------------------------|
| `YOUTUBE_API_KEY`  | —                         | YouTube Data API v3 (обязательно)       |
| `YOUTUBE_CHANNEL_ID`| `UCvy7FIEQYztchmNfCROlgDw`| ID канала                               |
| `COLLAB_MARKER`    | `kugoo.ru`                | Подстрока для фильтра (lowercase)       |
| `COLLAB_SINCE`     | `2025-04-14T00:00:00Z`    | Не брать ролики раньше этой даты        |

## 7. Поведение при сбоях

- API упал → старый `data.json` остаётся на месте (atomic write через temp).
- Все ошибки пишутся в `backend/logs/fetch.log` (ротация 1 MB × 3 файла).
- Скрипт возвращает `0` при успехе, `1` при сбое API/пустом результате,
  `2` если не задан `YOUTUBE_API_KEY` — удобно проверять в `run.sh`.

## 8. История метрик (следующий этап)

Все снапшоты пишутся в `backend/data.db` (`snapshots` table). Из этой
истории потом можно построить графики VPH, динамику просмотров по ролику
и т.д. Сам файл `data.db` лежит только на сервере (в `.gitignore`).
