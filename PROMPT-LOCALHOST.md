# Промт для агента: поднять проект, разобрать существующие PDF и нарастить корпус до 300 000

Файл — не инструкция для вас, а **готовый промт**: скопируйте всё, что внутри
рамки ниже, и отдайте своему агенту (Claude Code / Cursor / Codex / любой, кто
умеет ходить в терминал). Он сам проверит машину, поставит зависимости,
разберёт вашу папку с PDF, измерит качество и скорость, а затем скачает корпус
из открытого каталога JLCPCB/LCSC.

Путь к вашей папке уже встален: `C:\Users\vadim\Downloads\sol\smanuals_datasheets`.
Менять больше ничего не нужно.

---

````
РОЛЬ
Ты — инженер, который поднимает готовый проект на локальной машине, разбирает
уже имеющиеся PDF и наращивает корпус до 300 000 файлов. Проект написан и лежит
в git. Твоя задача — не писать код, а развернуть, измерить, скачать и честно
отрапортовать. Код меняй только если что-то объективно сломано; о каждом таком
случае пиши отдельно.

ЦЕЛЬ
1. Поднять сайт на http://localhost:8000 (поиск по маркировке + карточки + RAG
   по даташитам + загрузка PDF через браузер).
2. Разобрать папку C:\Users\vadim\Downloads\sol\smanuals_datasheets — получить
   карточки, показать их на сайте, измерить полноту и скорость.
3. Нарастить корпус: скачать даташиты из открытого каталога JLCPCB/LCSC
   (jlcparts) и довести число PDF до ~300 000, не уронив машину.

ГДЕ ЛЕЖАТ PDF
  C:\Users\vadim\Downloads\sol\smanuals_datasheets
Это основная папка корпуса для шагов A. Проверь, что она существует и в ней
есть .pdf; если путь другой или папка пустая — скажи сразу, не импровизируй.

РЕПОЗИТОРИЙ
  git clone https://github.com/elmosmt-blip/smd-component-finder.git
  cd smd-component-finder
  git checkout arena/01a0568a-smd-component-finder
Ветка важна: в `main` лежит только пустой «Initial commit», весь проект — в
ветке `arena/01a0568a-smd-component-finder`.

АРХИТЕКТУРА (не переделывай, это решения пользователя)
* Парсер №1 — Docling (layout + TableFormer) для таблиц. Парсер №2 и дешёвый
  проход — pdfplumber. Docling включается только на страницах, где реально есть
  таблицы (`--docling-pages tables`), иначе 300k файлов не считаются никогда.
* Оркестрация и чанкование — LlamaIndex (`MarkdownElementNodeParser`),
  структурно по заголовкам Markdown. НЕ LangChain, НЕ Langflow.
* Карточки компонентов — SQLite (`data/cards/cards.db`) + статические шарды.
* Поиск — гибридный: BM25 + k-NN в OpenSearch, слияние RRF. Если OpenSearch
  недоступен, код сам продолжает на SQLite FTS5 (это норма, не ошибка).
* Веб-интерфейс — только на английском. Не переводи строки в UI на русский.
* Комментарии и сообщения в коде — на английском.

ОГРАНИЧЕНИЯ
* Не пушь в `main`, не делай force-push ни в какую ветку.
* Не удаляй `data/`. База и PDF — не в git (они в .gitignore, не добавляй их).
* Не выдумывай числа производительности: запускай `bench.py` и бери их оттуда.
* Если шаг не прошёл — остановись, напиши что именно и почему. Не
  импровизируй с архитектурой, чтобы «заработало».
* Скриншоты мне не присылай — я их не увижу. Только вывод команд текстом.

================================================================================
ЧАСТЬ A. ПОДНЯТЬ ПРОЕКТ НА СУЩЕСТВУЮЩЕЙ ПАПКЕ
================================================================================

ШАГ 0. Windows
  * venv: `.venv\Scripts\python` вместо `.venv/bin/python`, активация —
    `.venv\Scripts\activate`. Все команды ниже пиши через `.venv\Scripts\python`.
  * Кодировка консоли (cp1251/cp866) чинится сама — инструменты переключают
    вывод на UTF-8. Ручной PYTHONIOENCODING не требуется.
  * `docker` чаще всего отсутствует: тогда OpenSearch пропускаем, поиск идёт
    на SQLite (см. шаг 4), включая векторный канал.
  * Перенос строки в PowerShell — обратная кавычка `
  * `curl` в PowerShell — это псевдоним Invoke-WebRequest. Для JSON удобнее:
    (Invoke-WebRequest "http://localhost:8000/api/health").Content

ШАГ 1. Проверь машину
  python --version              # нужно 3.10+
  docker --version              # если «не найден» — это нормально, читай шаг 4
  Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory
  Get-PSDrive C | Select-Object Used,Free
  $env:NUMBER_OF_PROCESSORS     # логических ядер
Свободное место важно: на каждые 100 000 даташитов уходит примерно 60–120 ГБ
(средний даташит ~0.6–1.2 МБ). Запиши, сколько свободно, до начала ЧАСТИ B.

ШАГ 2. Зависимости
  python -m venv .venv
  .venv\Scripts\pip install --upgrade pip
  .venv\Scripts\pip install -r tools/requirements.txt
  node --version ; npm install              # тесты интерфейса (jsdom)

ШАГ 3. Проверь, что всё стоит — тесты
  npm test                                     # 73 проверки интерфейса и поиска
  .venv\Scripts\python tools\test_rag.py       # 26 (25 без botocore)
  .venv\Scripts\python tools\test_cards.py     # 40
  .venv\Scripts\python tools\test_ingest.py    # 23
  .venv\Scripts\python tools\test_opensearch.py# 43 (без кластера, на заглушке)
  .venv\Scripts\python tools\test_docling.py   # 32 (без весов моделей)
  .venv\Scripts\python tools\test_fetch.py     # 60
  .venv\Scripts\python tools\test_quality.py   # 49
  .venv\Scripts\python tools\test_tables.py    # 44
Всего 390 проверок. Если что-то падает — разберись до следующего шага и напиши
в отчёте, что именно упало.

ШАГ 4. OpenSearch (только если есть docker)
Если docker отсутствует — пропусти шаг целиком. Поиск останется на SQLite: это
нормальная конфигурация, а не деградация, BM25 и векторы работают и там.
Векторный канал без всякого Docker включается так:
  .venv\Scripts\pip install sentence-transformers
  .venv\Scripts\python tools\rag\pipeline.py --rebuild --embed sentence-transformers
OpenSearch понадобится позже, когда чанков станет больше ~1 млн (сейчас у
пользователя 144 067 чанков на 2 553 документа — SQLite ещё держит, но поиск
уже занимает 2.2 с).
Если docker есть:
  docker compose -f tools\rag\opensearch-compose.yml up -d
  # Windows/WSL2 перед этим: wsl -d docker-desktop sysctl -w vm.max_map_count=262144
  (Invoke-WebRequest http://localhost:9200).StatusCode    # ждать до 2 минут
Дальше во всех командах:
  $env:SMD_OPENSEARCH_URL = "http://localhost:9200"

ШАГ 5. Сними диагноз
  .venv\Scripts\python tools\rag\doctor.py
Он печатает одним текстом: железо, пакеты, OpenSearch, сколько в базе карточек
и чанков, полноту карточек, сбои разбора, сколько PDF в корпусе, отвечает ли
сайт. Вывод вставь в отчёт целиком.

ШАГ 6. Подними сайт
  .venv\Scripts\python tools\rag\serve.py --host 0.0.0.0 --port 8000 --jobs 4
Проверь:
  (Invoke-WebRequest "http://localhost:8000/api/health").Content
  (Invoke-WebRequest "http://localhost:8000/").StatusCode
Открой в браузере http://localhost:8000, первый блок «1 · Load your PDFs».

ШАГ 7. Разбор папки с PDF
Сначала на малом, чтобы увидеть всё целиком:
  .venv\Scripts\python tools\rag\build_cards.py --corpus C:\Users\vadim\Downloads\sol\smanuals_datasheets `
      --limit 100 --jobs <число_ядер> --no-shards --verbose
Потом проверь механизм докачки (должен всё пропустить):
  .venv\Scripts\python tools\rag\build_cards.py --corpus C:\Users\vadim\Downloads\sol\smanuals_datasheets `
      --limit 100 --jobs <число> --no-shards
  # ожидаемо: "Nothing new: every file is already in the database"
И полный прогон папки:
  .venv\Scripts\python tools\rag\build_cards.py --corpus C:\Users\vadim\Downloads\sol\smanuals_datasheets `
      --jobs <лучшее_число> --no-shards
  .venv\Scripts\python tools\rag\build_cards.py --stats
  .venv\Scripts\python tools\rag\build_cards.py --show <любой part_number из --stats>

ШАГ 8. Замер скорости (обязательно, без него дальше нет смысла)
  .venv\Scripts\python tools\rag\bench.py --corpus C:\Users\vadim\Downloads\sol\smanuals_datasheets `
      --files 200 --jobs 8,16,24,32
Скрипт печатает PDF/с, МБ/с, пик RSS и extrapolate на 300 000 файлов. Пришли
таблицу целиком — по ней выбираем число рабочих для большого прогона.

ШАГ 9. Почему карточки неровные (обязательно)
Карточка такая, какой был PDF: скан честно даёт ноль полей, и это не баг. Но
«неровно» надо уметь измерять:
  .venv\Scripts\python tools\rag\audit_cards.py --top 10
  .venv\Scripts\python tools\rag\audit_cards.py --corpus C:\Users\vadim\Downloads\sol\smanuals_datasheets `
      --scan-check --scan-limit 300
Скрипт печатает заполненность каждого поля, группы (полные/средние/бедные/
пустые), причины пустых карточек и худшие файлы. --scan-check открывает сами
PDF и считает символы на первой странице: ноль символов — скан без текстового
слоя, ему нужен OCR.
Ориентир на настоящем корпусе: корпус находится в 60–80 % карточек, распиновка
в 20–40 %. Если распиновка ниже 15 % или сканов больше 20 % — пришли вывод
целиком, это лечится.

ШАГ 9.6. Аудит таблиц (обязателен, если таблицы в PDF есть, а полей нет)
  .venv\Scripts\python tools\rag\audit_tables.py --corpus C:\Users\vadim\Downloads\sol\smanuals_datasheets `
      --limit 50 --top 30
Он разбирает случайные 50 PDF, прогоняет каждую таблицу через тот же
классификатор, что и пайплайн, и печатает: сколько таблиц найдено, сколько
распознано, каких видов, и заголовки НЕраспознанных таблиц по частоте, с
примерами строк. Пришли вывод целиком — по нему я дополню словарь заголовков.
Логика простая: если «PDF без таблиц» — это единицы процентов, а распиновка
находится в четверти карточек, то таблицы есть, просто их заголовки нам
неизвестны. Лечится парой строчек в словаре, но угадывать их нельзя — нужен
этот вывод.

ШАГ 10. Индекс поиска
  .venv\Scripts\python tools\rag\pipeline.py --rebuild --backend auto --verbose
  (Invoke-WebRequest "http://localhost:8000/api/search?q=MMBT3904&k=5").Content
`--backend auto` возьмёт OpenSearch, если он отвечает, иначе SQLite.

ШАГ 11. Docling (тяжёлый шаг, делай после того, как всё остальное работает)
  .venv\Scripts\pip install docling        # ~2–3 ГБ: torch, transformers
Первый запуск скачает веса моделей с HuggingFace (~1–2 ГБ) — нужен интернет.
  .venv\Scripts\python tools\rag\pipeline.py --rebuild --parser docling `
      --docling-pages tables --limit 20 --verbose
  .venv\Scripts\python tools\rag\bench.py --corpus C:\Users\vadim\Downloads\sol\smanuals_datasheets `
      --parser docling --files 20 --jobs 8
Для офлайн-машин: `export DOCLING_ARTIFACTS_PATH=C:\models`.
Docling работает только на страницах с таблицами, на остальных — pdfplumber.
Это сделано специально, не «исправляй».

ШАГ 12. Прогон из браузера
Открой http://localhost:8000, блок «1 · Load your PDFs»: введи путь до папки →
«Parse folder»; либо «Choose folder» → выбрать папку → «Upload & parse».

================================================================================
ЧАСТЬ B. НАРАСТИТЬ КОРПУС ДО 300 000 PDF (источник — открытый каталог JLCPCB/LCSC)
================================================================================

ЧТО ЭТО И ПОЧЕМУ ИМЕННО ОН
`jlcparts` (https://yaqwsx.github.io/jlcparts/) — открытый дамп каталога JLCPCB
(он же магазин LCSC): около 7 миллионов SMD-позиций, у каждой парт-номер,
производитель, корпус и ссылка на PDF даташита. Скачивается целиком, без API,
ключей и месячных лимитов. Это ровно SMD-комплектующие — то, что нам и нужно.

Почему не другие источники — это проверено, не переспрашивай:
* Octopart/Nexar — бесплатный план ~1000 деталей в месяц. На 300 тысяч не годится.
* Digi-Key — условия API прямо запрещают массовую выгрузку и построение базы из
  его данных. Годится только для уточнения отдельных позиций.
* Mouser/TME — то же самое: отдельные позиции, не склад.
* Агрегаторы вроде alldatasheet.com — много сканов без текстового слоя, такие
  файлы распарсятся пустыми.

ШАГ B1. Скачай базу каталога (~11 ГБ)
Файл лежит здесь и разбит на тома по 50 МБ:
  https://yaqwsx.github.io/jlcparts/data/cache.zip
  https://yaqwsx.github.io/jlcparts/data/cache.z01
  https://yaqwsx.github.io/jlcparts/data/cache.z02
  ... и так далее, пока сервер не вернёт 404.
Томов может быть сколько угодно (бывало больше двухсот) — не угадывай число,
качай в цикле до первой 404. Имена после z99 идут как cache.z100, cache.z101.

PowerShell (вставь целиком, это один блок):
  $dir = 'C:\smd-corpus'
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  Set-Location $dir
  $base = 'https://yaqwsx.github.io/jlcparts/data'
  Invoke-WebRequest "$base/cache.zip" -OutFile cache.zip
  $i = 1
  while ($true) {
      if ($i -le 99) { $n = '{0:d2}' -f $i } else { $n = [string]$i }
      try {
          Invoke-WebRequest "$base/cache.z$n" -OutFile "cache.z$n" -ErrorAction Stop
      } catch {
          Write-Host "тома кончились на cache.z$n"
          break
      }
      Write-Host "скачан cache.z$n"
      $i++
  }
Если PowerShell 5.1 ругается на Invoke-WebRequest — добавь `-UseBasicParsing`.

Распаковка. Это многотомный zip, поэтому нужен 7-Zip: встроенный
Expand-Archive такие архивы не понимает.
  winget install 7zip.7zip          # если 7-Zip ещё не стоит
  7z x cache.zip                    # получится cache.sqlite3, ~11 ГБ
Место: архив ~3–4 ГБ + распакованная база ~11 ГБ, итого держи 20 ГБ свободными.
Время скачки — от получаса, зависит от канала.
После распаковки тома можно удалить:
  Remove-Item cache.z* -Force
Сам cache.sqlite3 не удаляй — он понадобится, если захочешь другой срез
(например, только транзисторы или только позиции со складом).

ШАГ B2. Преврати каталог в список деталей
  .venv\Scripts\python tools\rag\fetch_datasheets.py --from-jlcparts C:\smd-corpus\cache.sqlite3 `
      --to-csv C:\smd-corpus\parts.csv --basic-only --min-stock 100
Флаги:
  --basic-only   только basic/preferred — то, что JLCPCB ставит без доплаты
  --min-stock N  отсечь позиции без склада
  --category X   фильтр по категории, например --category Transistors
                 (ищет и по категории, и по подкатегории)
  --limit N      только первые N строк (для пробы)
Экспортёр сам читает схему файла (PRAGMA table_info) и ищет колонки по именам,
поэтому переживёт смену формата. В настоящей базе таблица называется
`components`, есть view `v_components` (в нём производитель и категория уже
приджойнены) — экспортёр предпочтёт view. Внимание к ловушке: колонка `mfr`
там означает ПАРТ-НОМЕР, а не производителя; производитель лежит в отдельной
таблице `manufacturers`. Код это знает.
Если файл окажется не таким, экспортёр напечатает список колонок, которые
нашёл. Тогда пришли мне этот вывод — я добавлю отображение.
Проверь результат:
  Get-Content C:\smd-corpus\parts.csv -TotalCount 3
  (Get-Content C:\smd-corpus\parts.csv | Measure-Object -Line).Lines

ШАГ B3. Скачай даташиты — сначала пробную тысячу, потом всё
Куда класть: отдельная папка C:\smd-corpus\pdf, чтобы не мешать с существующей
папкой из ЧАСТИ A.
  .venv\Scripts\python tools\rag\fetch_datasheets.py --list C:\smd-corpus\parts.csv `
      --out C:\smd-corpus\pdf --limit 1000 --delay 0.2 --workers 8
Про `--workers` и `--delay`: скорость задаёт `--delay` — это минимальный
промежуток между СТАРТАМИ запросов на один хост (1/delay запросов в секунду).
`--workers` держит несколько соединений одновременно, чтобы латентность не
съедала эту скорость; он не отменяет вежливость. Для CDN вроде LCSC
разумно начать с `--delay 0.2 --workers 8` (≈5 файлов/с). Если сервер не
жалуется и нужно быстрее — подними workers до 16 и опусти delay до 0.05
(≈20 файлов/с, 300 000 файлов примерно за 4 часа). Не ставь delay 0 на 300 000
файлов: это уже нагрузка, а не загрузка.
Скрипт сам печатает скорость и оценку времени на 300 000 файлов — пришли её.
Загрузчик проверяет, что скачанное действительно PDF (магические байты и
размер), повторяет при 5xx, не хранит дубли по SHA1, уважает robots.txt и пишет
manifest.jsonl с происхождением каждого файла. Прерванную загрузку можно
продолжить той же командой — уже скачанное пропустится.

ПОСЛЕ ПРОБНОЙ ТЫСЯЧИ — ОБЯЗАТЕЛЬНО ОСТАНОВИСЬ И ПОСЧИТАЙ МЕСТО:
  $n = (Get-ChildItem C:\smd-corpus\pdf -Filter *.pdf).Count
  $mb = (Get-ChildItem C:\smd-corpus\pdf -Filter *.pdf | Measure-Object Length -Sum).Sum / 1MB
  Write-Host ("файлов: {0}, всего {1:N0} МБ, средний {2:N2} МБ" -f $n, $mb, ($mb / $n))
  Write-Host ("на 300 000 файлов понадобится {0:N0} ГБ" -f (300000 * ($mb / $n) / 1024))
Если места не хватает — скажи в отчёте, сколько получается и сколько свободно.
Не начинай полную загрузку, не показав мне эти цифры.

ШАГ B4. Полная загрузка (только после того, как место посчитано)
  .venv\Scripts\python tools\rag\fetch_datasheets.py --list C:\smd-corpus\parts.csv `
      --out C:\smd-corpus\pdf --delay 0.2 --workers 16
Это долгий процесс (часы). Запускай его так, чтобы пережить закрытие терминала,
и периодически проверяй manifest.jsonl. Докачка — той же командой.

ШАГ B5. Влей в базу карточек
  .venv\Scripts\python tools\rag\build_cards.py --corpus C:\smd-corpus\pdf `
      --jobs <лучшее_число_из_ШАГА_8> --no-shards
  .venv\Scripts\python tools\rag\audit_cards.py --corpus C:\smd-corpus\pdf --scan-check
  .venv\Scripts\python tools\rag\pipeline.py --rebuild --backend auto
После этого на сайте появятся карточки и по новым файлам. Статические шарды для
сайта (если нужны) — `--dump-shards`, но на 300 000 карточек они не нужны: сайт
работает через API.

ШАГ B6. Главное правило
Не пытайся сделать 300 000 за один присест. Тысяча → замер → десять тысяч →
замер → всё остальное. На каждом шаге пиши, сколько получилось, сколько упало и
какая скорость.

================================================================================
ОТЧЁТ (пришли ровно по этой форме)
================================================================================
```
Вывод tools\rag\doctor.py — целиком, без сокращений
Вывод tools\rag\audit_cards.py (с --scan-check) — целиком
Вывод tools\rag\audit_tables.py --limit 50 — целиком
ОС: Windows 11 (сборка ...), docker есть / нет
Машина: CPU ..., ядер N, RAM ... ГБ, свободно на диске ... ГБ
Тесты: 390/390 (если меньше — что упало и почему)
OpenSearch: поднят / не поднят (причина)
Сайт: http://localhost:8000 — работает / нет
Существующая папка: N PDF, M карточек, P ошибок, время T
Скорость разбора: X PDF/с при Y рабочих → оценка на 300 000: <из bench.py>
Полнота карточек: полных X, средних Y, бедных Z, пустых W; корпус A %,
  распиновка B %, сканов C % (из audit_cards.py)
Корпус jlcparts: база скачана (размер, время) / не скачана (почему);
  строк в parts.csv: N; скачано PDF: M; средний размер: K МБ;
  прогноз места на 300 000: G ГБ; скорость: S файлов/с
Docling: установлен / нет; если да — скорость и качество таблиц
Проблемы: список того, что не заработало, с командами и выводом
Что делать дальше: 3–5 пунктов, по моему мнению
```

ЕСЛИ ЧТО-ТО НЕ ПОШЛО — типичные причины
* `docker: не найден` — так и надо, OpenSearch пропускаем, поиск идёт на SQLite.
* `ModuleNotFoundError: pdfplumber` — не активирован venv; используй
  `.venv\Scripts\python`, а не системный python.
* `SMD_OPENSEARCH_URL` не подхватывается — `$env:SMD_OPENSEARCH_URL = "..."` в
  том же терминале, где запускаешь команду, либо `--backend opensearch`.
* Docling не может скачать веса — нет доступа к huggingface.co: положи веса
  вручную и укажи DOCLING_ARTIFACTS_PATH.
* Порт 8000 занят — `--port 8001` (и поменяй URL в отчёте).
* `WinError 10106 / WSAStartup` в тестах — окружение подпроцесса обрезано (нет
  SystemRoot); запусти тест из обычного PowerShell, а не из Start-Process.
* Экспорт из cache.sqlite3 пишет «cannot map ...» — пришли мне список колонок,
  который он напечатал; это новый формат базы, добавлю отображение.
* 7-Zip не может открыть cache.zip — проверь, что скачаны ВСЕ тома (цикл шёл
  до 404) и что они лежат в одной папке с cache.zip.
* Карточки пустые, а PDF вроде нормальный — запусти audit_cards.py
  --scan-check: если «символов: 0», это скан, нужен Docling с OCR. Если текста
  много, а таблиц 0 — попробуй `--parser docling` на паре файлов.
* Скриншоты в чат не присылай — их не увижу. Присылай вывод doctor.py и
  audit_cards.py текстом.
````
