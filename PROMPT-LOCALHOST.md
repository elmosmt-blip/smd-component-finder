# Промт для агента: поднять проект на localhost и сделать тестовый прогон

Файл — не инструкция для вас, а **готовый промт**: скопируйте всё, что внутри
рамки ниже, и отдайте своему агенту (Claude Code / Cursor / Codex / любой, кто
умеет ходить в терминал). Он сам проверит машину, поставит зависимости,
поднимет OpenSearch и сайт, проведёт тестовый прогон и пришлёт отчёт.

Перед копированием замените одно: путь к вашей папке с PDF
(`/mnt/datasheets` в тексте) — на свой. Если папки пока нет, агент сгенерирует
демо-корпус сам.

---

````
РОЛЬ
Ты — инженер, который поднимает готовый проект на локальной машине и проводит
тестовый прогон. Проект уже написан и лежит в git. Твоя задача — не писать код,
а развернуть, проверить и честно отрапортовать. Код меняй только если что-то
объективно сломано; о каждом таком случае пиши отдельно.

ЦЕЛЬ
1. Поднять сайт на http://localhost:8000 (поиск по маркировке + карточки + RAG
   по даташитам + загрузка PDF через браузер).
2. Поднять OpenSearch на localhost:9200 и подключить к нему индекс.
3. Провести тестовый прогон: распарсить тестовую папку с PDF, получить
   карточки, показать их на сайте.
4. Прислать отчёт по шаблону в конце этого промта.

РЕПОЗИТОРИЙ
  git clone https://github.com/elmosmt-blip/smd-component-finder.git
  cd smd-component-finder
  git checkout arena/01a0568a-smd-component-finder
Ветка важна: в `main` лежит только пустой «Initial commit», весь проект — в
ветке `arena/01a0568a-smd-component-finder`.

АРХИТЕКТУРА (не переделывай, это решения пользователя)
* Парсер №1 — Docling (layout + TableFormer) для таблиц. Парсер №2 и
  дешёвый проход — pdfplumber. Docling включается только на страницах, где
  реально есть таблицы (`--docling-pages tables`), иначе 300k файлов не
  считаются никогда.
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

ШАГ 1. Проверь машину
  python3 --version          # нужно 3.10+
  docker --version && docker compose version
  free -g                    # RAM, в идеале 32 ГБ+
  df -h .                    # место: 3–5 ГБ на каждые 300k карточек
  nproc                      # сколько логических ядер
Если docker нет — не ставь его сам, скажи пользователю. OpenSearch тогда
пропускаем: поиск останется на SQLite (работает, но без векторного канала).

ШАГ 2. Зависимости
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r tools/requirements.txt
  node --version && npm install          # тесты интерфейса (jsdom)

ШАГ 3. Проверь, что всё стоит — тесты
  npm test                                # 73 проверки интерфейса и поиска
  .venv/bin/python tools/test_rag.py      # 26 (25, если не стоит botocore)
  .venv/bin/python tools/test_cards.py    # 40
  .venv/bin/python tools/test_ingest.py   # 23
  .venv/bin/python tools/test_opensearch.py   # 43 (без кластера, на заглушке)
  .venv/bin/python tools/test_docling.py      # 32 (без весов моделей)
Всего 237 проверок (236 без botocore: проверка подписи SigV4 тогда
пропускается — это норма). Если что-то падает — разберись до следующего шага.

ШАГ 4. OpenSearch
  sudo sysctl -w vm.max_map_count=262144
  echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-opensearch.conf
  docker compose -f tools/rag/opensearch-compose.yml up -d
  curl -sf http://localhost:9200 && echo "OpenSearch отвечает"
Подожди зелёного healthcheck (до 2 минут):
  docker ps --format '{{.Names}} {{.Status}}'
Если контейнер рестартует — смотри логи: `docker logs smd-opensearch`
(почти всегда это vm.max_map_count или мало памяти под heap: поправь
OPENSEARCH_JAVA_OPTS в compose-файле).
Дальше во всех командах:
  export SMD_OPENSEARCH_URL=http://localhost:9200

ШАГ 5. Подними сайт
  .venv/bin/python tools/rag/serve.py --host 0.0.0.0 --port 8000 --jobs 4
Проверь:
  curl -s http://localhost:8000/api/health | head -c 300
  curl -s http://localhost:8000/ -o /dev/null -w "index.html: %{http_code}\n"
Открой в браузере http://localhost:8000 — первый блок на странице
«1 · Load your PDFs», это и есть загрузка PDF.

ШАГ 6. Тестовый корпус
Если у пользователя есть свои PDF — путь к папке: /mnt/datasheets
(замени на фактический). Если нет:
  .venv/bin/python tools/rag/sample_datasheets.py --out data/datasheets
Это 13 демо-даташитов — их хватит, чтобы увидеть карточки.

ШАГ 7. Замер скорости (обязательно, без него дальше нет смысла)
  .venv/bin/python tools/rag/bench.py --corpus /mnt/datasheets \
      --files 200 --jobs 8,16,24,32
Скрипт печатает PDF/с, МБ/с, пик RSS и extrapolation на 300 000 файлов.
Запиши лучшее число рабочих — его и используй дальше.

ШАГ 8. Тестовый прогон карточек
Сначала на малом, чтобы увидеть всё целиком:
  .venv/bin/python tools/rag/build_cards.py --corpus /mnt/datasheets \
      --limit 100 --jobs <лучшее_число> --no-shards --verbose
Потом проверь сам механизм докачки (должен всё пропустить):
  .venv/bin/python tools/rag/build_cards.py --corpus /mnt/datasheets \
      --limit 100 --jobs <число> --no-shards
  # ожидаемо: "Nothing new: every file is already in the database"
И итог:
  .venv/bin/python tools/rag/build_cards.py --stats
  .venv/bin/python tools/rag/build_cards.py --show <любой part_number из --stats>
`--no-shards` — потому что статические JSON (файл на карточку) для большого
прогона не нужны; выгрузить их можно потом: `--dump-shards`.

ШАГ 9. Индекс поиска
  .venv/bin/python tools/rag/pipeline.py --rebuild --backend auto --verbose
  curl -s "http://localhost:8000/api/search?q=MMBT3904&k=5" | head -c 400
`--backend auto` возьмёт OpenSearch, если он отвечает, иначе SQLite.
Принудительно: `--backend opensearch` или `--backend sqlite`.

ШАГ 10. Docling (тяжёлый шаг, делай после того, как всё остальное работает)
  .venv/bin/pip install docling        # ~2–3 ГБ: torch, transformers
Первый запуск скачает веса моделей с HuggingFace (~1–2 ГБ) — нужен интернет.
  .venv/bin/python tools/rag/pipeline.py --rebuild --parser docling \
      --docling-pages tables --limit 20 --verbose
Замерь, во сколько раз это медленнее pdfplumber:
  .venv/bin/python tools/rag/bench.py --corpus /mnt/datasheets \
      --parser docling --files 20 --jobs 8
Для офлайн-машин: `export DOCLING_ARTIFACTS_PATH=/mnt/models`.
Важно: Docling работает только на страницах с таблицами; на остальных идёт
pdfplumber. Это сделано специально — не «исправляй».

ШАГ 11. Прогон из браузера (то, что будет делать пользователь)
Открой http://localhost:8000, блок «1 · Load your PDFs»:
  а) введи путь до папки с PDF → «Parse folder»;
  б) либо «Choose folder» → выбрать папку в браузере → «Upload & parse»
     (файлы уедут на сервер в data/datasheets/uploaded/<дата-время>/).
Во время прогона: прогресс done/total, скорость, ETA, кнопка «Stop»,
список файлов с ошибками. После — сетка карточек и фильтры обновятся сами.

ШАГ 12. Приёмка (пройди и отметь каждый пункт)
 [ ] http://localhost:8000 открывается, в консоли браузера нет ошибок
 [ ] /api/health отвечает ok=true, cards > 0
 [ ] /api/cards показывает карточки, /api/card?part=<PART> — полную карточку
 [ ] поиск /api/search?q=<part> находит нужный даташит
 [ ] повторный прогон той же папки ничего не пересчитывает (skipped)
 [ ] кнопка «Stop» прерывает прогон, сервер продолжает отвечать
 [ ] битый PDF попадает в список ошибок и не роняет прогон
 [ ] docker ps показывает smd-opensearch в состоянии healthy
 [ ] /api/stats показывает backend=opensearch (если кластер поднят)
 [ ] записано: PDF/с и оценка времени на 300 000 файлов из bench.py

ШАГ 13. Отчёт (пришли ровно по этой форме)
```
Машина: CPU ..., ядер N, RAM ... ГБ, диск свободно ... ГБ
Окружение: python X.Y, docker Z, ветка arena/... коммит <hash>
Тесты: 237/237, либо 236/236 без botocore (если меньше — что упало и почему)
OpenSearch: поднят / не поднят (причина)
Сайт: http://localhost:8000 — работает / нет
Тестовый прогон: N файлов, M карточек, P ошибок, время T
Скорость: X PDF/с при Y рабочих → оценка на 300 000: <из bench.py>
Docling: установлен / нет; если да — скорость и качество таблиц
Проблемы: список того, что не заработало, с командами и выводом
Что делать дальше: 3–5 пунктов, по моему мнению
```

ЕСЛИ ЧТО-ТО НЕ ПОШЛО — типичные причины
* `docker: permission denied` — добавить пользователя в группу docker или sudo.
* OpenSearch падает при старте — vm.max_map_count (см. шаг 4) или heap больше,
  чем свободной памяти: уменьши -Xms/-Xmx в compose-файле.
* `ModuleNotFoundError: pdfplumber` — не активирован venv; используй
  `.venv/bin/python`, а не системный python3.
* `SMD_OPENSEARCH_URL` не подхватывается — export в том же терминале, где
  запускаешь команду, либо передай `--backend opensearch`.
* Docling не может скачать веса — нет доступа к huggingface.co: положи веса
  вручную и укажи DOCLING_ARTIFACTS_PATH.
* Порт 8000 занят — `--port 8001` (и поменяй URL в отчёте).
* В папке «нет PDF» — проверь регистр `.pdf` и права на чтение.
````

---

## Что получится после прогона

- Сайт на `http://localhost:8000` с карточками компонентов.
- `data/cards/cards.db` — база карточек (SQLite).
- OpenSearch на `localhost:9200` с гибридным индексом (если docker есть).
- Отчёт от агента со скоростью в PDF/с и оценкой времени на 300 000 файлов.

Если агент пришёл с пунктом «Проблемы» — просто перешлите его мне, я поправлю.
