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

Ожидается: **393 + 73 = 466 проверок, 0 провалов**
(было 316: test_fetch 96, test_rag 48, test_tables 53, test_docling 41,
test_cards 40, test_quality 49, test_ingest 23, test_opensearch 43, npm 73).

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

Затем:

```powershell
set SMD_DOCLING_OCR=auto
.\.venv\Scripts\python tools\rag\build_cards.py --corpus C:\smd-corpus\scans ^
    --parser docling --jobs 8 --rebuild --no-shards
```

`auto` = OCR включается только там, где нет текстового слоя. Обычные PDF
ничего за это не платят.

Пришли блок **«Чем разобрано»** и, если Docling отказал, строки `! ...`
(причина печатается один раз, а не 854). А также: сколько заняло время на
831 файл.

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
.\.venv\Scripts\python tools\rag\audit_tables.py --corpus C:\smd-corpus\pdf --limit 50 --json audit2.json
```

Теперь: регистры МК считаются отдельной строкой «распознано и отброшено»,
строка «НЕ распознано» показывает **полное** число таблиц, а не сумму по
топ-25. Пришли весь вывод — по нему дополню словарь.

---

## Не делай

* не удаляй `index.db`, `cards.db`, `parsed`, `C:\smd-corpus\pdf`;
* не запускай `pipeline.py` и `build_cards.py` одновременно (WAL-дедлок уже
  ловили);
* не используй `--basic-only` на этом кэше;
* не качай с `lcsc.com` и агрегаторов — только `wmsc.lcsc.com` и сайты
  производителей.

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
