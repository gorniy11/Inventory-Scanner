# Конфликт Git: описание экспорта

5 ноября 2026 выполнен **демонстрационный** конфликт документации. Он создан намеренно для показа работы с ветками и разрешения конфликта, не выдаётся за случайную ошибку команды. Код приложения не менялся.

## Ветки

Общая база: [153900b](https://github.com/gorniy11/Inventory-Scanner/commit/153900ba467825cfcc0aeae9a0af6b3179f6593d).

Обе ветки изменили одну строку `docs/MVP.md`:

| Ветка | Коммит | Текст |
|---|---|---|
| docs/csv-export | [325c9be](https://github.com/gorniy11/Inventory-Scanner/commit/325c9be05df75377ee3e676e2c245057392977a8) | Экспорт: CSV с поддержкой русского текста в Excel. |
| docs/json-export | [a8a92b1](https://github.com/gorniy11/Inventory-Scanner/commit/a8a92b1e47f60468502e67dbcb0d0af35f90b480) | Экспорт: JSON с идентификаторами и атрибутами предметов. |

## Фактический результат Git

При слиянии JSON-ветки в CSV-ветку Git завершился с кодом 1:

```text
Auto-merging docs/MVP.md
CONFLICT (content): Merge conflict in docs/MVP.md
Automatic merge failed; fix conflicts and then commit the result.
```

`git status --short` показал `UU docs/MVP.md`. Это настоящий конфликт, воспроизведённый Git в отдельной рабочей копии.

## Решение

Сохранены оба требования:

> Экспорт: CSV с поддержкой русского текста в Excel и JSON с идентификаторами и атрибутами предметов.

Убраны маркеры конфликта, файл добавлен в индекс. Решение сохранено merge-коммитом с двумя родителями — CSV и JSON. История обеих веток остаётся доступной; принудительной перезаписи main нет.

Почему это корректно: `exporting.py` уже поддерживает оба формата. CSV использует UTF-8 с BOM и фиксированные колонки, JSON сохраняет поля предмета. Выбор только одной версии текста потерял бы описание второй существующей возможности.

## Как воспроизвести

В отдельной чистой копии репозитория:

```console
git fetch origin
git switch -c reproduce-export-conflict 325c9be05df75377ee3e676e2c245057392977a8
git merge --no-ff --no-commit a8a92b1e47f60468502e67dbcb0d0af35f90b480
git status --short
```

Откройте `docs/MVP.md`, замените конфликтующий блок итоговой фразой выше, затем:

```console
git add docs/MVP.md
git diff --cached --check
git commit -m "docs: resolve CSV and JSON export conflict"
```

Для отмены только этого незавершённого пробного слияния: `git merge --abort`.

## Проверки

- Незавершённых конфликтов после разрешения нет: `git diff --name-only --diff-filter=U` пуст.
- `git diff --cached --check` проходит.
- Базовая программа: 62 теста, Ruff и GUI-сценарий прошли; изменялась документация.
- Проверки итогового опубликованного коммита доступны в [Actions](https://github.com/gorniy11/Inventory-Scanner/actions).
