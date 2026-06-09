# Built-in tools — reference

Per-tool описание + способы запустить вручную **без агента** (через `harness.tools_cli`).
Шаблон для разработки своих тулов — в [tools/README.md](../tools/README.md).

## Запуск вручную

```bash
# изнутри контейнера
docker compose exec -T harness python -m harness.tools_cli <name> '<json-args>'

# локально (если установлены deps)
python -m harness.tools_cli <name> '<json-args>'

# вспомогательное
python -m harness.tools_cli --list       # список зарегистрированных тулов
python -m harness.tools_cli --schemas    # все JSON-схемы (то, что видит LLM)
```

CLI собирает реальный `ToolContext` из тех же env vars, что использует агент
(`HARNESS_MEMORY_DIR` / `HARNESS_TOOLS_DIR` / `HARNESS_STATE_DIR`) — поэтому
побочные эффекты на диск идут туда же, куда положил бы агент. Никакой LLM,
сервера UI/мониторинга/control не стартуют.

---

## echo

**Как работает.** Возвращает входной `text` буквально. Pure-compute, без побочных эффектов.

**Ожидаемое поведение.**
- `{"text": "hi"}` → `"hi"`.
- Отсутствует ключ `text` → пустая строка.

**Запуск:**
```bash
docker compose exec -T harness python -m harness.tools_cli echo '{"text":"ping"}'
# ping
```

**Зачем он есть.** Smoke-target. Если работает echo — значит registry, ctx-инжекция и dispatch исправны. Боевой ценности нет.

---

## now

**Как работает.** Возвращает текущее UTC-время в одном из трёх форматов через `datetime.now(timezone.utc)`. Никаких сетевых вызовов, никакой памяти.

**Ожидаемое поведение.**
- `format=iso`     → `"2026-06-08T13:47:45.812+00:00"` (default).
- `format=epoch`   → `"1780926464"`.
- `format=human`   → `"2026-06-08 13:47:45 UTC"`.
- Неизвестный формат → fallback на `iso`.

**Запуск:**
```bash
docker compose exec -T harness python -m harness.tools_cli now '{"format":"human"}'
# 2026-06-08 13:47:45 UTC
```

---

## knowledge_write

**Как работает.** Пишет содержимое в файл `memory/knowledge/<topic>.md` через `MemoryStore.write_knowledge()`. Перезаписывает целиком, если файл существует.

**Ожидаемое поведение.**
- `{"topic":"foo","content":"bar"}` → файл `memory/knowledge/foo.md` с содержимым `bar`. Вывод: `"ok, wrote 3 chars to knowledge/foo.md"`.
- Пустой `content` → отказ: `"⚠️ refusing to write empty knowledge entry"`.
- `topic` с `/`, пробелами, точками, или длиной >64 → отказ: `"⚠️ invalid topic ..."`.

**Запуск:**
```bash
docker compose exec -T harness python -m harness.tools_cli knowledge_write \
  '{"topic":"manual-test","content":"# Note\n\nhello kb"}'

# проверка
cat ~/harness-M64/memory/knowledge/manual-test.md
```

**Topic slug regex.** `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$` — без слешей, без точек, не начинается с дефиса.

---

## knowledge_read

**Как работает.** Читает файл `memory/knowledge/<topic>.md` через `MemoryStore.read_knowledge()`.

**Ожидаемое поведение.**
- Существующий топик → содержимое файла.
- Не существует → `"(no knowledge entry for topic 'X')"`.
- Невалидный slug → `"⚠️ invalid topic ..."`.

**Запуск:**
```bash
docker compose exec -T harness python -m harness.tools_cli knowledge_read '{"topic":"manual-test"}'
# # Note
#
# hello kb
```

---

## knowledge_list

**Как работает.** `MemoryStore.list_knowledge()` — сканит `memory/knowledge/` и возвращает имена `.md`-файлов без расширения.

**Ожидаемое поведение.**
- Не пусто → имена топиков по строке.
- Пусто → `"(no knowledge entries yet)"`.

**Запуск:**
```bash
docker compose exec -T harness python -m harness.tools_cli knowledge_list '{}'
# manual-test
# user-profile
```

---

## read_url

**Как работает.** httpx GET, follow redirects, кастомный `User-Agent: harness-m64/0.0 (+read_url)`, 20-секундный таймаут. Если ответ HTML (по `Content-Type` или начальному `<`) — режется скриптами/тегами через regex (`_strip_html`). Затем труннкируется по `max_chars`. К результату добавляется header с URL/HTTP-кодом/Content-Type.

**Ожидаемое поведение.**
- `{"url":"https://example.com/"}` → текст страницы с заголовком и обрезкой по 20K chars.
- `{"url":"https://api.example/data.json", "strip_html":false}` → сырой ответ.
- Не http(s) URL → `"⚠️ url must start with http:// or https://"`.
- Таймаут / DNS-fail → `"⚠️ fetch failed: ConnectError: ..."`.
- HTTP ≥400 → возвращается как обычный ответ (с header'ом и кодом), но без `⚠️` — модель сама решает, как трактовать.

**Запуск:**
```bash
docker compose exec -T harness python -m harness.tools_cli read_url \
  '{"url":"https://example.com/", "max_chars":500}'

# GET https://example.com/
# HTTP 200  Content-Type: text/html; charset=UTF-8
# length: 500 chars
# ---
# Example Domain ...
```

**Лимиты:** default 20K chars, hard cap 100K, timeout 20s. Меняются константами в [src/harness/tools/web.py](../src/harness/tools/web.py).

---

## file_write

**Как работает.** Записывает UTF-8 файл в `<state_dir>/sandbox/<path>`. Создаёт родительские директории. Проверяет, что resolved-путь **внутри** sandbox-корня — иначе отказ.

**Ожидаемое поведение.**
- `{"path":"a.txt","content":"hi"}` → файл `state/sandbox/a.txt`. Вывод: `"ok, wrote 2 chars to a.txt"`.
- `{"path":"sub/b.txt", ...}` → создаст `state/sandbox/sub/`.
- Абсолютный путь (`/etc/x`) → `"⚠️ absolute paths are not allowed"`.
- `..`-escape (`../../etc/passwd`) → `"⚠️ path escapes the sandbox"`.
- `>200 KB` контент → `"⚠️ content too large: N bytes (cap 200000)"`.

**Запуск:**
```bash
docker compose exec -T harness python -m harness.tools_cli file_write \
  '{"path":"demo/note.md","content":"# Demo\n\ntext"}'

# проверка
ls -la ~/harness-M64/state/sandbox/demo/
cat ~/harness-M64/state/sandbox/demo/note.md
```

---

## file_read

**Как работает.** Та же sandbox-проверка, читает UTF-8 (с replace на невалидных байтах).

**Ожидаемое поведение.**
- Существующий файл → его содержимое.
- Не файл (директория или нет такого) → `"⚠️ not a file: ..."`.
- `>200 KB` файл → `"⚠️ file too large: N bytes (cap 200000)"`.
- Escape attempts → отказ.

**Запуск:**
```bash
docker compose exec -T harness python -m harness.tools_cli file_read '{"path":"demo/note.md"}'
# # Demo
#
# text
```

---

## file_list

**Как работает.** Рекурсивный `rglob("*")` по sandbox-корню (или его поддиректории, если задан `prefix`). Возвращает только файлы (не директории), по одному на строку.

**Ожидаемое поведение.**
- Без аргументов → все файлы в sandbox'е.
- `{"prefix":"sub"}` → только из `state/sandbox/sub/`.
- Пусто → `"(empty)"`.
- Не существует prefix → `"(no such directory: <prefix>)"`.
- > 500 файлов → срезается с маркером `"... [+more, truncated at 500]"`.

**Запуск:**
```bash
docker compose exec -T harness python -m harness.tools_cli file_list '{}'
# a.txt
# demo/note.md
# sub/b.txt

docker compose exec -T harness python -m harness.tools_cli file_list '{"prefix":"demo"}'
# demo/note.md
```

---

## Диагностика «почему агент не вызывает тул»

Если в UI кажется, что агент не использует тул, который явно подходит:

1. **Прогони руками** через `tools_cli` тот же запрос с теми же аргументами. Тул работает? Если да — проблема не в тулe.
2. **Глянь `--schemas`** — что видит LLM. Description достаточно ясный?
3. **Поправь `memory/level_1.md`** — добавь строку «для X используй тул Y». Это самое мощное воздействие.
4. **Проверь `tool_choice`** — может быть, мы случайно шлём `"none"`. По дефолту шлётся `"auto"`.
