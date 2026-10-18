# DeepSeek live replies (redacted)

Two real replies from the production provider, captured 2026-10-18 (Australia/Melbourne) by
POST https://api.deepseek.com/v1/chat/completions with model `deepseek-chat`, max_tokens 16 and
the single user message "Reply with exactly the word: ok":

| file | request | Content-Type returned |
|---|---|---|
| chat_nonstream.json | non-streaming | application/json |
| chat_stream.sse | `stream: true`, `stream_options.include_usage: true` | text/event-stream; charset=utf-8 |

Only the response bodies were kept, never the request headers. Redacted: every `id` is
`chatcmpl-redacted`, `system_fingerprint` is `fp_redacted`, `created` is 0. Everything else is
byte-for-byte what came back, including `"model": "deepseek-flash"` (the provider's name for the
model that answered) and, in the stream, `finish_reason: null` on the delta events with
`finish_reason: "stop"` and the usage on the same final event before `data: [DONE]`.

Used by tests/unit/test_p1_deepseek_live_fixtures.py.
