# Документация: каталог даташитов

Сюда кладутся PDF, по которым строится поиск (`tools/rag/pipeline.py`).
Каталог — вход RAG, не часть сайта: без него интерфейс работает, но панель
«2 · Search your datasheets (RAG)» показывает подсказку, как всё поднять.

## Что лежит сейчас

13 файлов двух видов:

1. **7 реальных PDF** из репозитория [Robotips/uConfig](https://github.com/Robotips/uConfig)
   (там они используются как тестовые даташиты для извлечения цоколёвок):
   `ATmega328P_pins.pdf`, `ATtiny24_pins.pdf`, `IFX9201SG_pins.pdf`,
   `PIC32MM_GPM_pins.pdf`, `dsPIC33EPXXGS50X_pins.pdf`, `ticc_pins.pdf`,
   `ucc27212a-q1_pins.pdf`.

2. **6 сгенерированных PDF** (`*_datasheet.pdf`: MMBT3904, 2N7002, BAV99,
   AMS1117-3.3, TP4056, SI2301) — сделаны `tools/rag/sample_datasheets.py`, чтобы
   в индексе были каноничные разделы (Absolute Maximum Ratings, Electrical
   Characteristics, Package Dimensions) с числами, совпадающими с записями
   `assets/js/data.js`. Перегенерировать:

   ```bash
   python3 tools/rag/sample_datasheets.py
   ```

Сгенерированные файлы — демо-данные, а не документация производителя. Перед
реальной работой замените их на настоящие даташиты: расположите файлы так, чтобы
номер детали читался из имени или из текста первой страницы.

## Как пополнять

```bash
cp ~/Downloads/*.pdf data/datasheets/
python3 tools/rag/pipeline.py --corpus data/datasheets --rebuild
```

Имя файла не обязано содержать номер детали, но это помогает: парсер сначала
пробует извлечь Part Number из текста, а имя файла — запасной вариант.

## Лицензии

Даташиты принадлежат их производителям (Microchip, Infineon, TI, Diodes Inc. и
др.) и приводятся здесь только как технический материал для проверки поиска.
В продакшене кладите сюда PDF, которые у вас есть право хранить.
