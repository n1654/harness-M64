# GigaChat functions — заметки по wire-формату

Короткая шпаргалка по тому, как GigaChat v1 ведёт цепочку function-calling
(на примере OpenAI-compat прокси). Собрано через прогон
[scripts/probe_gigachat.sh](../scripts/probe_gigachat.sh).

## Инварианты payload'а

| Что | Round 1 | Round 2+ |
|---|---|---|
| `functions[]` | **отправляется** | не отправляется |
| `function_call: "auto"` | отправляется | не отправляется |
| `messages` | `[system, user]` | `[system, user, assistant(function_call), function(result)]` |
| `functions_state_id` | в ответе сервера | **нигде не отправляем обратно** (кладём в логи) |
| `content` у assistant с `function_call` | — | `""` (пустая строка, не `null` и не пропуск) |
| `function_call.arguments` | — | **JSON-объект**, не строка |
| `content` у function-result | — | **JSON-valid строка** (текст оборачиваем в `json.dumps`) |
| `name` у function-result | — | имя совпадает с `function_call.name` |
| `RqUID` (header) | каноничный UUID с дефисами | то же |

Самое неочевидное — последняя строчка для function-result:

```jsonc
// неправильно (наш плейн-текст не парсится как JSON)
{"role": "function", "name": "now", "content": "2026-06-08 13:47:45 UTC"}

// правильно (строка обёрнута в JSON)
{"role": "function", "name": "now", "content": "\"2026-06-08 13:47:45 UTC\""}

// тоже правильно — число / объект / массив уже валидный JSON
{"role": "function", "name": "calc", "content": "14"}
{"role": "function", "name": "calc", "content": "{\"result\":14}"}
```

## Чем GigaChat (functions) отличается от OpenAI (tools)

Оба построены вокруг одной идеи (модель просит вызвать функцию → клиент исполняет
→ возвращает результат), но **wire-формат принципиально разный**. Кто переходит
с OpenAI на GigaChat — почти каждое поле меняется.

| Концепт | OpenAI (`tools` API) | GigaChat v1 (`functions` API) |
|---|---|---|
| Поле в запросе со списком инструментов | `tools: [{"type":"function","function":{...}}]` | `functions: [{...}]` (плоский список) |
| Поле выбора инструмента | `tool_choice: "auto"` | `function_call: "auto"` |
| Параллельные вызовы в одном ходе | **да**, `tool_calls: [...]` (массив) | **нет**, `function_call: {...}` (один на ход) |
| Корреляция вызов ↔ результат | по `tool_call_id` (клиентский) | по позиции в `messages[]` (assistant сразу за ним function) |
| Серверная привязка к сессии | нет | `functions_state_id` (server-side, в нашем случае не нужен на проводе) |
| `arguments` от модели | **JSON-строка** (нужно `JSON.parse`) | **JSON-объект** (уже распарсенный) |
| `content` ассистента с tool-вызовом | `null` или текст | `""` (именно пустая строка) |
| Роль сообщения с результатом | `role: "tool"` + `tool_call_id` | `role: "function"` + `name` |
| `content` у результата | произвольная строка | **обязательно JSON-valid** (число / объект / JSON-кавычки вокруг текста) |
| Повторная отправка `tools[]` / `functions[]` | **на каждом ходе** | **только в первом** запросе цепочки |
| Сохранение истории | клиент сам пишет всю историю | то же, сервер также хранит свою копию по `functions_state_id` |
| Расширения схемы | `strict: true` для JSON Schema | `few_shot_examples`, `return_parameters` |
| Streaming chunks для tool-calls | дельтами через `tool_calls` deltas | тоже стримит, но другой формой |

Главные грабли при портировании OpenAI → GigaChat:
1. **Параллельных вызовов нет** — если у тебя цикл предполагает массив `tool_calls`,
   на GigaChat он схлопывается в один вызов за ход; нужна реализация на следующий round.
2. **`functions[]` нельзя слать на втором round'е** — иначе 422
   `functions or thinking_functions should only appeal in user, function messages or random role messages`.
3. **`content` результата = JSON-valid строка**, не сырое сообщение от инструмента.

## Где это в коде

* [src/harness/llm/gigachat.py](../src/harness/llm/gigachat.py)
  * `_serialize_message` — упаковка assistant с `function_call` и function-result.
  * `chat()` — условное отключение `functions[]`/`function_call:"auto"` на втором ходе.
* [src/harness/agent.py](../src/harness/agent.py)
  * Хранит полную историю — пара `assistant(function_call) + function(result)`
    автоматически попадает в payload каждого следующего вызова.

## Источники

- [Function calling overview](https://developers.sber.ru/docs/ru/gigachat/guides/functions/overview)
- [Generating arguments](https://developers.sber.ru/docs/ru/gigachat/guides/functions/generating-arguments-for-custom-functions)
- [POST /oauth](https://developers.sber.ru/docs/ru/gigachat/api/reference/rest/post-token)
- [POST /chat/completions](https://developers.sber.ru/docs/ru/gigachat/api/reference/rest/post-chat)
