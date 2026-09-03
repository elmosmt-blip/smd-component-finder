# ФАЗА D — восстановление и рост качества

Этот файл — задание для агента на машине с Windows. Отдай его целиком.
Всё, что описано ниже, уже **написано и проверено в апстриме**; задача —
подтянуть код, прогнать команды и прислать цифры.

---

## Почему стало хуже, и что из этого исправлено

По отчёту фазы C:

| Что случилось | Причина | Что сделано в апстриме |
|---|---|---|
| `pipeline.py` завис на 3 519 файлах | Docling печатал в stderr тысячи `MatchingPostProcessor WARNING`; на Windows-консоли писатель затыкается, родитель ждёт результатов за забитым каналом | воркеры больше не пишут в консоль вообще (`SMD_WORKER_LOG=<путь>` вернёт лог в файл), логгеры Docling переведены в ERROR |
| `index.db` повреждён и удалён | прогон не коммитился видимым образом, FTS5 остался несогласованным | коммит после каждого документа; в конце — проверка целостности; `--check` / `--repair` пересобирают FTS5 из таблицы чанков за минуты, без переразбора |
| Docling дал 8 карточек из 854 сканов | `pages=tables` сначала ищет таблицы дешёвым проходом; на скане таблиц нет **потому что нет текста**, и парсер решал, что Docling делать нечего | если текстового слоя нет — файл целиком уходит в Docling **с OCR** (`SMD_DOCLING_OCR=auto` по умолчанию). Для обычного PDF OCR не включается |
| выгрузка из кэша — 974 строки вместо миллионов | в v2-схеме колонки `basic` нет, `--basic-only` фильтрует по `preferred`, а таких ~1000 | экспорт печатает воронку по каждому фильтру и предупреждает вслух; новые `--dedupe`, `--popular-first`, `--library-type`; `--probe` показывает те же цифры до выгрузки |
| таблицы регистров МК забили аудит | 385 «нераспознаных» — это `Core Registers`, `INTCON | bit 7 GIE`, `0x0F` | распознаются как `registers` и отбрасываются (в карточку не идут); в аудите показаны отдельной строкой |
| «не распознано 20.7 %» | в отчёте печаталась сумма только по топ-25 заголовкам, а не по всем | печатается полное число и уточняется, что показаны первые N |

---

## Шаг D0. Подтянуть код и прогнать тесты

```powershell
cd C:\путь\к\smd-component-finder
git checkout -- tools/rag/fetch_datasheets.py
git pull
git log --oneline -1
.\.venv\Scripts\python tools\test_fetch.py
.\.venv\Scripts\python tools\test_rag.py
.\.venv\Scripts\python tools\test_tables.py
.\.venv\Scripts\python tools\test_docling.py
.\.venv\Scripts\python tools\test_cards.py
.\.venv\Scripts\python tools\test_quality.py
.\.venv\Scripts\python tools\test_ingest.py
.\.venv\Scripts\python tools\test_opensearch.py
npm test
```

Ожидается: **552 + 75 = 627 проверок, 0 провалов**
(test_fetch 96, test_rag 48, test_tables 68, test_docling 41, test_cards 40,
test_quality 49, test_ingest 23, test_opensearch 43, test_element14 82,
test_card_index 30, test_enrich 32 — итого 552; npm 75 = matcher 30 + ui 45).

Сюиты element14, card_index и enrich — новые, добавь их в прогон:

```powershell
.\.venv\Scripts\python tools\test_element14.py
.\.venv\Scripts\python tools\test_card_index.py
.\.venv\Scripts\python tools\test_enrich.py
```

**Ничего не удаляй.** `cards.db.bak2_*`, `C:\smd-corpus\pdf`, `scans`,
`good`, `parsed` — всё остаётся на месте.

---

## Шаг D1. cards.db: сначала посмотреть, потом решать

```powershell
.\.venv\Scripts\python tools\rag\build_cards.py --stats
```

В конце теперь печатается блок **«Чем разобрано»** — сколько карточек сделано
Docling и сколько pdfplumber. Пришли его: если на `--parser docling` там
«pdfplumber 845», Docling в том прогоне не работал вообще, и пересчитывать
сканы надо после установки OCR-движка (шаг D3).

---

## Шаг D2. Индекс: дособрать, а не пересобирать

Тот же `--out`, что был в прерванном прогоне (по умолчанию — `data\rag`,
именно там лежит кэш на 6 994 файла):

```powershell
.\.venv\Scripts\python tools\rag\pipeline.py --corpus C:\smd-corpus\good --jobs 16
```

Что изменилось и что нужно увидеть:

* строка `Уже в индексе: N   из кэша parsed/: M   к разбору: K`;
* прогресс — одна строка на 25 файлов (`3500/3519  12.4 файл/с`);
* в конце `Индекс FTS: ok`.
* **Старый кэш не подхватится**: прежняя версия писала только `.md` и
  `.meta.json`, чанков в них нет, поэтому первый прогон разберёт эти файлы
  заново (pdfplumber, 3 519 файлов — минуты). Все последующие — мгновенно.

Если индекс всё-таки не поднимается:

```powershell
.\.venv\Scripts\python tools\rag\pipeline.py --check
.\.venv\Scripts\python tools\rag\pipeline.py --repair
```

`--repair` пересобирает FTS5 из таблицы `chunks`. **Не удаляй index.db** —
сейчас это почти никогда не нужно.

Пришли: сколько проиндексировано, сколько взято из кэша, время, последнюю
строку про FTS, и размер `index.db` + `index.db-wal`.

---

## Шаг D3. Сканы: Docling с OCR

Сначала проверь, что Docling вообще установлен:

```powershell
.\.venv\Scripts\python tools\rag\doctor.py
```

Если Docling есть, нужен OCR-движок (один из двух):

```powershell
.\.venv\Scripts\pip install easyocr       # либо установи tesseract и добавь в PATH
```

**Стоп. Прошлый прогон D3 именно так и был запущен — и потерял 14 459
карточек.** `--rebuild` очистил базу, Docling за 50 минут дал 56 карточек,
прогон умер, база осталась почти пустой. Два правила, теперь они в коде:

1. **Никогда не `--rebuild` в основную базу.** Собирай сканы в отдельный
   `--out`, а потом вливай. Теперь `build_cards.py` перед `--rebuild` сам
   делает бэкап `cards.db.pre-rebuild-<время>` и предупреждает, если в корпусе
   PDF меньше, чем карточек в базе, — но рассчитывать на бэкап вместо
   правильного `--out` не надо.
2. **Сначала замерь на 20 файлах, потом считай на 854.** У нас уже есть
   число: **56 карточек за 50 минут на 8 воркерах**, то есть ~1.1 карточки в
   минуту. 854 скана — это **около 13 часов**, а не «прогон на вечер». easyocr
   тянет за собой torch и тратит время на инициализацию в каждом воркере.

Поэтому сначала:

```powershell
.\.venv\Scripts\python tools\rag\build_cards.py --corpus C:\smd-corpus\scans ^
    --parser docling --jobs 8 --limit 20 --out C:\smd-corpus\cards-scans --no-shards
```

Посмотри на «время на файл» и прикинь полный прогон, прежде чем запускать его
на ночь. Если выходит больше 6–8 часов — не запускай на всё сразу: ночной
прогон, который не доезжает, ничего не даёт.

Затем, если время приемлемо (и только тогда):

```powershell
set SMD_DOCLING_OCR=auto
.\.venv\Scripts\python tools\rag\build_cards.py --corpus C:\smd-corpus\scans ^
    --parser docling --jobs 8 --out C:\smd-corpus\cards-scans --no-shards
```

`auto` = OCR включается только там, где нет текстового слоя. Обычные PDF
ничего за это не платят.

Пришли блок **«Чем разобрано»**, время на файл и, если Docling отказал,
строки `! ...` (причина печатается один раз, а не 854).

---

## Шаг D4. Экспорт из кэша заново (v2-схема)

`--basic-only` в этой версии кэша не работает — не используй его.

```powershell
.\.venv\Scripts\python tools\rag\fetch_datasheets.py ^
    --from-jlcparts C:\smd-corpus\cache.sqlite3 --to-csv C:\smd-corpus\parts2.csv ^
    --min-stock 100 --popular-first --dedupe --prefer-vendor --limit 50000 --no-explain
```

* `--dedupe` — одна строка на один PDF. Сорок позиций серии ведут на один
  файл, качать его сорок раз незачем (отсюда 58 % дублей).
* `--popular-first` — сначала самые складские: `--limit 50000` возьмёт то, что
  реально заказывают.
* `--prefer-vendor` — файлы с сайта производителя вместо копии LCSC.

Сначала пришли воронку (запусти без `--no-explain`, на 11 ГБ это минута):

```
Источник: jlc_components
  строк всего:            ?
  есть ссылка на PDF:     ?
  склад >= 100:           ?
  после GROUP BY p.datasheet:  ?
```

Эти четыре цифры и есть ответ на «где взять 300 000 PDF».

---

## Шаг D5. Аудит таблиц новым словарём

```powershell
.\.venv\Scripts\python tools\rag\audit_tables.py --corpus C:\smd-corpus\pdf --limit 50 ^
    --parser pdfplumber --json audit2.json
```

**`--parser pdfplumber` обязателен.** В прошлый раз аудит упал через 45 минут
с кодом `-1073741571` = `0xC00000FD` (STATUS_STACK_OVERFLOW): Docling тянет
torch, а table-transformer рекурсирует глубже, чем 1 МБ стека, который
Windows даёт python.exe. Такое падение не ловится никаким `except` — процесс
умирает сразу, и 45 минут работы пропали.

Что изменилось в коде:

* `--json` теперь **перезаписывается после каждого файла**, поэтому падение на
  41-м файле из 50 оставляет на диске ответ по 41 файлу;
* разбор идёт в потоке с большим стеком (256 МБ) — это лечит сам
  STATUS_STACK_OVERFLOW;
* `--parser pdfplumber` позволяет вообще не звать torch. Классификация таблиц
  от парсера не зависит: вопрос «какие заголовки мы не узнаём» — к
  `extract.classify_table`, и pdfplumber отвечает на него так же.

Незавершённый `audit2.json` печатается с предупреждением «прогон прерван,
обработано N из M» — не выдавай его за полный.

Регистры МК считаются отдельной строкой «распознано и отброшено», строка «НЕ
распознано» показывает **полное** число таблиц, а не сумму по топ-25. Пришли
весь вывод — по нему дополню словарь.

---

## Шаг D6. Обогащение карточек атрибутами element14

После D4 у тебя есть `parts2.csv` на 50 000 деталей. По нему можно собрать
атрибуты каталога и влить в карточки — это закрывает производителя и корпус
там, где парсер их не нашёл:

```powershell
.\.venv\Scripts\python tools\rag\element14.py --parts parts2.csv --store uk.farnell.com ^
    --api-key ТВОЙ_КЛЮЧ --attributes-out attrs.jsonl --skip-good data\cards\cards.db

.\.venv\Scripts\python tools\rag\pipeline.py --enrich attrs.jsonl ^
    --from-cards data\cards\cards.db --out data\rag --rebuild
```

Правила обогащения (они же проверяются тестами): заполняются только пустые
поля, остальное идёт в отдельный блок `extra_specs` с пометкой источника,
`confidence` не растёт. Замер на 13 даташитах: производитель 84.6 % → 100 %,
корпус 69.2 % → 100 %.

---

## Не делай

* не удаляй `index.db`, `cards.db`, `parsed`, `C:\smd-corpus\pdf`;
* не запускай `pipeline.py` и `build_cards.py` одновременно (WAL-дедлок уже
  ловили);
* не используй `--basic-only` на этом кэше;
* не качай с `lcsc.com` и агрегаторов — только `wmsc.lcsc.com` и сайты
  производителей;
* не запускай `--rebuild` на `data\cards\cards.db` — собирай новую порцию в
  отдельный `--out` (см. шаг D3).

---

## Форма отчёта

```
Тесты:            N пройдено, M провалено
cards.db --stats: карточек / Чем разобрано (docling N, pdfplumber M)
pipeline good:    проиндексировано N, из кэша M, время, FTS: ok
                  index.db + -wal: N МБ
docling scans:    файлов, время, Чем разобрано (docling N, pdfplumber M)
экспорт воронка:  строк всего / со ссылкой / склад>=100 / после dedupe
audit_tables:     таблиц, распознано %, отброшено (регистры), НЕ распознано
```
