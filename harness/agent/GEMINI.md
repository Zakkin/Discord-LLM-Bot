# 🪝 Harness Agent Core サブプロジェクト開発ガイドライン (GEMINI.md)

詳細な開発規約・境界条件・検証コマンドは [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/harness/agent/AGENTS.md) を参照してください。

- **担当範囲**: `harness/agent/` 直下（`hooks.py`, `__init__.py`, `tests/`, `test_harness/`）
- **禁止事項**: 配下の独立サブプロジェクト（`prompt_parser/`, `tool_registry/`, `observation/`, `memory_manager/`, `orchestrator/`）や他ディレクトリのファイルを直接編集しないこと。フック基盤（`hooks.py`）の契約（同期・純粋・fail-open・`HOOKS_ENABLED` キルスイッチ）を厳守すること。
