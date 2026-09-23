# dsh-scout

`dsh-scout` is a Codex skill for delegating repository work to a local [DeepSeek Harness](https://github.com/deepseek-ai/DeepSeek-Harness) agent without copying prompts between tools.

It keeps one writer session alive across related turns so the model can reuse conversation context and provider KV cache. An optional second scout runs with project roots mounted read-only.

## What it provides

- one persistent full-access writer;
- one optional read-only scout alongside the writer;
- stable session keys for an Issue, OpenSpec change, or pull request;
- context-pressure tracking from the DSH session store;
- a 250,000-token soft rotation warning;
- automatic handoff and rotation at 400,000 tokens;
- enforced one-writer/one-reader concurrency locks;
- explicit reporting when a controller restart loses live session history.

## Status

The first public release is tracked in [Issue #1](https://github.com/jooxee/dsh-scout/issues/1).

Installation and operating instructions will be added with the implementation.

## License

[MIT](LICENSE)
