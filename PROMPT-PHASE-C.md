# Промт для агента (фаза C): только то, чего ещё нет

Отчёт по фазам A и B уже получен: корпус скачан (20 781 файл, 22.8 ГБ),
карточки собраны (14 459), индекс построен на 1 000 файлов. Ниже — только
пять вещей, которых в том отчёте не было. Всё остальное уже сделано,
повторять не нужно.

---

````
РОЛЬ
Ты ведёшь проект локальной базы даташитов на Windows. Фаза загрузки закончена.
Сейчас задача — поднять качество того, что уже скачано, и добить индекс.
Новых PDF не качай.

ЧТО УЖЕ ИЗВЕСТНО (не переделывай, не переспрашивай)
* 20 781 файл, 22.81 ГБ, 58 % попыток были дублями по SHA1.
* Отдача: скачанный корпус 0.49 карточки на файл, своя папка 1.66 на файл.
* Аудит карточек есть: корпус 47 %, распиновка 28.6 %, габариты 8.7 %,
  сканов 7.4 %, таблиц в PDF нет только у 8.8 %.
* bench есть: 24 jobs — оптимум (59.9 файл/с, 3.4 ГБ RAM).
* Кэш jlcparts — новая схема v2: jlc_components, lcsc_components, meta.
* Docling у тебя установлен.

ОГРАНИЧЕНИЯ
* Ничего не удаляй: ни PDF, ни cards.db, ни manifest.jsonl.
* Не качай новых PDF.
* Не запускай downloader и build_cards одновременно — уже ловили deadlock на WAL.
* Скриншоты не присылай, только вывод команд текстом.

ШАГ C1. Обновись и пересобери карточки новым классификатором
  git checkout -- tools/rag/fetch_datasheets.py
  git pull origin arena/01a0568a-smd-component-finder
  Copy-Item data\cards\cards.db data\cards\cards.db.bak
  .venv\Scripts\python tools\test_fetch.py       # 77
  .venv\Scripts\python tools\test_tables.py      # 44
  .venv\Scripts\python tools\test_rag.py         # 33
  .venv\Scripts\python tools\test_quality.py     # 49
  .venv\Scripts\python tools\test_cards.py       # 40
  npm test                                       # 73
Всего 414 проверок. Если что-то падает — остановись и напиши.
В коммите, который ты сейчас получишь, исправлен классификатор таблиц: раньше
«mm» совпадало с парт-номером MMBT3904 в склеенном заголовке, и таблица кодов
заказа уходила в габариты — из-за этого терялся корпус. Пересобери карточки,
это ~6 минут при 24 воркерах:
  .venv\Scripts\python tools\rag\build_cards.py --corpus C:\smd-corpus\pdf --jobs 24 --no-shards --rebuild
  .venv\Scripts\python tools\rag\audit_cards.py --top 10
Сравни с прежним: корпус 47 %, габариты 8.7 %, распиновка 28.6 %.
Жду роста по корпусу и габаритам. Пришли новый вывод аудита целиком.

ШАГ C2. Аудит таблиц по скачанному корпусу (этого не было совсем)
  .venv\Scripts\python tools\rag\audit_tables.py --corpus C:\smd-corpus\pdf --limit 50 --top 30
Пришли вывод целиком. В нём — заголовки таблиц, которые классификатор не
узнал. По ним дополню словарь, и те же 20 781 файл дадут больше полей без
единого нового скачивания.

ШАГ C3. Схема кэша — нужны колонки
  .venv\Scripts\python tools\rag\fetch_datasheets.py --probe C:\smd-corpus\cache.sqlite3
Нужно понять, почему `--basic-only --min-stock 100` дал всего 974 строки из
7.1 млн: флаги basic/preferred и склад в v2-схеме лежат не там, где их ищет
экспортёр. Пришли вывод целиком — по нему поправлю фильтры.

ШАГ C4. Спасти 1 073 скана (Docling уже стоит)
Это единственные файлы, где новые поля появятся гарантированно.
  .venv\Scripts\python tools\rag\audit_cards.py --csv C:\smd-project\thin.csv
  $rows = Import-Csv C:\smd-project\thin.csv -Delimiter ';'
  New-Item -ItemType Directory -Force -Path C:\smd-corpus\scans | Out-Null
  $rows | Where-Object { $_.flags -like '*scan*' } | ForEach-Object {
      $src = Join-Path C:\smd-corpus\pdf $_.filename
      if (Test-Path $src) { Copy-Item $src C:\smd-corpus\scans -Force }
  }
  (Get-ChildItem C:\smd-corpus\scans).Count
  .venv\Scripts\python tools\rag\build_cards.py --corpus C:\smd-corpus\scans --parser docling --jobs 8 --no-shards
Пришли: сколько файлов отобралось, сколько времени заняло, сколько карточек
стало полнее (audit_cards по ним). Если Docling не может скачать веса — так и
напиши, не мучайся.

ШАГ C5. Добить индекс — теперь параллельно
Раньше pipeline.py работал в один поток: 1 000 PDF за 35 минут, и остаток в
19 781 файл оценивался в 70 часов. Сейчас у него есть --jobs. Сначала
отбери содержательные файлы — нечего индексировать то, из чего всё равно
ничего не извлеклось (жёсткие ссылки, место не тратится):
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
  .venv\Scripts\python tools\rag\pipeline.py --corpus C:\smd-corpus\good --rebuild --jobs 24
Пришли: сколько файлов отобралось, сколько времени заняло, сколько чанков и
сколько весит index.db (было 468 МБ на 1 000 файлов).
Векторы пока не включай — сначала убедимся, что индекс вообще влезает.

ОТЧЁТ (пришли ровно по этой форме)
```
Тесты: 414/414 или что упало
audit_cards.py после пересборки — целиком
audit_tables.py --limit 50 — целиком
--probe cache.sqlite3 — целиком
Docling по сканам: файлов N, время T, стало полнее M
Индекс: файлов N, время T, чанков C, размер index.db
Моя рекомендация: что делать дальше
```
````
