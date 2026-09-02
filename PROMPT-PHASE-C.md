# Промт для агента (фаза C): остановить гонку за количеством, измерить и поднять качество

Отдайте агенту целиком, как есть. Пути уже вставлены:

* ваша папка: `C:\Users\vadim\Downloads\sol\smanuals_datasheets`
* скачанный корпус: `C:\smd-corpus\pdf`
* кэш каталога: `C:\smd-corpus\cache.sqlite3`
* база карточек: `data\cards\cards.db`

---

````
РОЛЬ
Ты ведёшь проект локальной базы даташитов на Windows. Фаза загрузки корпуса
закончена — и она показала, что гнаться за количеством дальше бессмысленно.
Твоя задача сейчас: остановиться, честно измерить, что получилось, и поднять
качество того, что уже скачано. Новых PDF пока НЕ качай.

ПОЧЕМУ ТАК (прочитай, это объясняет каждый шаг ниже)
Из твоего же отчёта:
* 49 013 попыток загрузки → 20 781 файл. 28 670 попыток (58 %) были дублями
  по SHA1: каталог отдаёт один даташит на десятки парт-номеров серии.
* 20 781 скачанный файл дал 10 215 карточек — 0.49 карточки на файл.
  Папка пользователя дала 4 244 карточки с 2 553 файлов — 1.66 на файл,
  то есть в 3.4 раза лучше.
* Индекс RAG весит 468 МБ на 1000 PDF. На 300 000 файлов это ~140 ГБ индекса
  плюс ~330 ГБ самих PDF — при 660 ГБ свободного места.
* Параллельная работа downloader'а и build_cards уже приводила к deadlock на
  WAL SQLite. Больше так не делай.

ВЫВОД, КОТОРЫЙ НЕ ОБСУЖДАЕТСЯ: 300 000 файлов сейчас — не цель. Цель —
профессиональная база. Пока не будут выполнены шаги C2–C4, загрузку
НЕ продолжать. Про публичный эндпоинт на 6 млн PDF забудь: это 6.6 ТБ,
столько не нужно и столько не дадут выкачать.

ОГРАНИЧЕНИЯ
* Ничего не удаляй: ни скачанные PDF, ни cards.db, ни manifest.jsonl.
* Не качай новых PDF до конца фазы C (кроме явно указанного в C6).
* Не запускай downloader и build_cards одновременно — ловили deadlock на WAL.
* Не пушь в main, не force-push.
* Скриншоты не присылай, только вывод команд текстом.

ШАГ C0. Синхронизируй код
Два твоих локальных патча к tools/rag/fetch_datasheets.py уже upstream. Перед
pull выброси их, чтобы не было конфликта:
  git checkout -- tools/rag/fetch_datasheets.py
  git pull origin arena/01a0568a-smd-component-finder
Проверь, что всё на месте:
  .venv\Scripts\python tools\test_fetch.py      # 77
  .venv\Scripts\python tools\test_tables.py     # 44
  .venv\Scripts\python tools\test_quality.py    # 49
  .venv\Scripts\python tools\test_cards.py      # 40
  npm test                                      # 73
Всего 407 проверок. Если что-то падает — остановись и напиши.
Сделай резервную копию базы карточек, дальше будем её пересобирать:
  Copy-Item data\cards\cards.db data\cards\cards.db.bak

ШАГ C1. Останови фоновую загрузку
Если downloader ещё работает — останови его. Скачанное не удаляй.

ШАГ C2. Посчитай, что мы на самом деле скачали
  .venv\Scripts\python tools\rag\fetch_datasheets.py --report C:\smd-corpus\pdf\manifest.jsonl
Этот вывод — главный. В нём: попытки, сколько файлов реально сохранено, доля
дублей, сбои, занятое место, средний размер файла, прогноз на 300 000 и
список хостов. Пришли вывод целиком.

ШАГ C3. Разведка схемы кэша
  .venv\Scripts\python tools\rag\fetch_datasheets.py --probe C:\smd-corpus\cache.sqlite3
Печатает таблицы, число строк и колонки каждой. Формат кэша сменился
(`components` → `jlc_components`), из-за этого пробный экспорт дал всего
974 строки из 7.1 млн: флаги basic/preferred и склад лежат не там, где их
ищет экспортёр. Без этого вывода я не могу починить фильтры. Пришли целиком.

ШАГ C4. Отдача на файл: две папки порознь
Это ключевое измерение фазы. Собери карточки отдельно по каждой папке в
отдельные базы и сравни.
  .venv\Scripts\python tools\rag\build_cards.py --corpus C:\Users\vadim\Downloads\sol\smanuals_datasheets --out C:\smd-project\his\cards --jobs 24 --no-shards
  .venv\Scripts\python tools\rag\audit_cards.py --db C:\smd-project\his\cards\cards.db --top 5

  .venv\Scripts\python tools\rag\build_cards.py --corpus C:\smd-corpus\pdf --out C:\smd-project\jlc\cards --jobs 24 --no-shards
  .venv\Scripts\python tools\rag\audit_cards.py --db C:\smd-project\jlc\cards\cards.db --top 5
Сравни: карточек на файл, среднюю полноту, долю полных карточек, долю сканов.
Пришли обе сводки. Решение о продолжении загрузки принимается по этим двум
цифрам, а не по числу скачанных файлов.

ШАГ C5. Аудит таблиц по скачанному корпусу
  .venv\Scripts\python tools\rag\audit_tables.py --corpus C:\smd-corpus\pdf --limit 50 --top 30
Пришли вывод целиком — по нему дополним словарь заголовков, и те же 20 781
файлов дадут больше полей без единого нового скачивания.

ШАГ C6. Спасти сканы (1 073 файла без текстового слоя)
Это единственные файлы, где новые поля появятся гарантированно.
  .venv\Scripts\pip install docling          # ~2-3 ГБ, нужны веса с HuggingFace
Отбери сканы в отдельную папку по CSV из аудита:
  .venv\Scripts\python tools\rag\audit_cards.py --csv C:\smd-project\thin.csv
  $rows = Import-Csv C:\smd-project\thin.csv -Delimiter ';'
  New-Item -ItemType Directory -Force -Path C:\smd-corpus\scans | Out-Null
  $rows | Where-Object { $_.flags -like '*scan*' } | ForEach-Object {
      $src = Join-Path C:\smd-corpus\pdf $_.filename
      if (Test-Path $src) { Copy-Item $src C:\smd-corpus\scans -Force }
  }
  (Get-ChildItem C:\smd-corpus\scans).Count
Запусти на них Docling (только на них — это быстро):
  .venv\Scripts\python tools\rag\build_cards.py --corpus C:\smd-corpus\scans --parser docling --jobs 8 --no-shards
Замерь время и пришли, сколько карточек стало полнее. Если Docling не
устанавливается (нет доступа к HuggingFace) — так и напиши, не мучайся.

ШАГ C7. Индексировать только содержательные файлы (по желанию, но очень желательно)
Смысл: не строить 140 ГБ индекса по файлам, из которых всё равно ничего не
извлеклось. Отбери файлы, чьи карточки имеют 5 полей и больше, и собери папку
с жёсткими ссылками — место не тратится.
  $good = Import-Csv C:\smd-project\thin.csv -Delimiter ';' | Where-Object { [double]$_.confidence -ge 0.55 }
  New-Item -ItemType Directory -Force -Path C:\smd-corpus\good | Out-Null
  $good | ForEach-Object {
      $src = Join-Path C:\smd-corpus\pdf $_.filename
      if (Test-Path $src) {
          $dst = Join-Path C:\smd-corpus\good $_.filename
          if (-not (Test-Path $dst)) { New-Item -ItemType HardLink -Path $dst -Target $src | Out-Null }
      }
  }
  (Get-ChildItem C:\smd-corpus\good).Count
  .venv\Scripts\python tools\rag\pipeline.py --corpus C:\smd-corpus\good --rebuild --parser auto
Запиши, сколько файлов отобралось и сколько весит index.db в итоге — сравним
с 468 МБ на 1000 файлов.

ШАГ C8. Что НЕ делать
* Не докачивать до 300 000 до ответа по C2 и C4.
* Не трогать эндпоинт на 6 млн PDF.
* Не пересобирать индекс по всему корпусу, если выполнен C7.
* Не удалять manifest.jsonl — это единственная запись о происхождении файлов.

ОТЧЁТ (пришли ровно по этой форме)
```
Вывод --report manifest.jsonl — целиком
Вывод --probe cache.sqlite3 — целиком
Вывод audit_cards.py по папке пользователя — целиком
Вывод audit_cards.py по C:\smd-corpus\pdf — целиком
Вывод audit_tables.py --limit 50 — целиком
Тесты: 407/407 или что упало
Docling: установлен / нет; если да — сколько сканов спасено и за сколько
Отбор в C7: сколько файлов, сколько весит index.db
Карточек на файл: своя папка X, скачанный корпус Y
Моя рекомендация: продолжать загрузку / остановиться — и почему
```
````
