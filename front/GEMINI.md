# 🌉 Front Bridge サブプロジェクト開発ガイドライン (GEMINI.md)

詳細な開発規約・境界条件・検証コマンドは [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/front/AGENTS.md) を参照してください。

- **担当範囲**: `front/` 直下（`ir.py`, `adapter.py`, `bridge.py`, `registry.py`, `tests/`）
- **禁止事項**: `front/` 自体を `PYTHONPATH` に入れないこと（`front/discord` が `discord.py` を隠すため）。他サブプロジェクトのファイルを直接編集しないこと。新設時は `customize_for_github.py` の `allowed_items` 同期を必須とすること（項目166）。
