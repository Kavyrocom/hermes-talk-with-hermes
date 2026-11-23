# Notices

Talk with Hermes takes ideas, and where named below adapted code, from these
projects.

## cruzlxyz/livetalk

https://github.com/cruzlxyz/livetalk, reviewed at `34f4ebb`.

Adapted into `talk_with_hermes/persona.py` and `talk_with_hermes/gemini_live.py`:
the delegation persona (acknowledge-first, context-first), the `talk` tool
declaration, the per-turn voice note, the spoken-answer directive, the
transcription language hints and the redial-with-handle policy. Rewritten in
French and restructured. Extended-thinking models are refused rather than wired
as NON_BLOCKING, because livetalk found they never emit the tool call.

```
MIT License

Copyright (c) 2026 cruzlxyz (github.com/cruzlxyz)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## TheSmokeDev/hermes-talk

https://github.com/TheSmokeDev/hermes-talk, MIT. No code taken. The Gemini Live
wire facts recorded in `talk_with_hermes/gemini_live.py` come from its live
probe notes.

## Synero/hermes-live-voice

MIT. Desktop interface ideas only, no code taken.
