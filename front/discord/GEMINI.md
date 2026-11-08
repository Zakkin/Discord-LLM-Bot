# 💬 Front Discord サブプロジェクト開発ガイドライン (GEMINI.md)

本ディレクトリ（`front/discord/`）は、**Discord API 入出力・イベント受信・返信生成フロー・スラッシュコマンド/コンテキストメニュー・ライフサイクル制御**を担当する独立サブプロジェクトです。

詳細な開発規約・境界条件・検証コマンドは [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/front/discord/AGENTS.md) を参照してください。

### 🎯 主な責務
1. **Bot 起動・エントリポイント**: `main.py`, `cog.py`, 多重起動防止。
2. **イベント・返信フロー**: `events.py`, `reply/classifier.py`, `reply/reply_core.py`（Orchestrator呼び出し）。
3. **コマンド・UI**: `admin.py`, `sing.py`, `commands/`（`/bot`, `/slang`, `/image_search` 等）。
4. **Discord 共通ヘルパー**: `common/`, `utils.py`, `adapter.py`。

### 🚫 境界条件と重要禁止事項
1. **スコープ外ファイル編集禁止**: `front/discord/` 外のファイルを直接編集しないこと。
2. **`PYTHONPATH` 規約**: `front/` を直接 `PYTHONPATH` に入れないこと（`PYTHONPATH=mock_libs:.` のみ使用）。
3. **バックグラウンドタスク直列化（項目169）**: 返信後の重いタスク（記憶抽出・所感・要約）は `_yield_to_user_replies` でユーザー返信を最優先譲歩し、直列化すること。
4. **ContextMenu 引数制約（項目151, 153）**: コールバックは `(self, interaction, message)` の2引数。`response_sent` 管理で二重エラー送信を防止。
5. **Vision・画像検索規約（項目180, 181）**: 2段階Vision（`caption_then_main`）、推論後即時アンロード、リプライ元画像解決。
6. **スラング・URL・フォールバック規約（項目182, 183, 184）**: スラング辞書による検索クエリ汚染防止、System内部メモURL除外、Web調査404時のLLM会話フォールバック、ふたば複数URL先頭1件制限。

### 🛠️ 検証手順
```bash
PYTHONPATH=mock_libs:. pytest --import-mode=importlib front/discord/tests ollama_bot/tests/test_reply_classifier.py
PYTHONPATH=mock_libs:. mypy --config-file mypy.ini front/discord
```
