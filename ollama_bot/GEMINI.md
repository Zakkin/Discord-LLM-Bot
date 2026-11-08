# 🤖 Discord Bot 設定・感情・互換シム層 サブプロジェクト開発ガイドライン (GEMINI.md)

本ディレクトリ（`ollama_bot/`）は、**ペルソナ設定（`config_*.py`）、Ollama クライアント（`common/ollama/`）、感情状態・関係値シミュレーション（`ollama_chat_/emotion/`, `ollama_chat_relationships.py`）、および後方互換シム層**を担当するサブプロジェクトです。

詳細な開発規約は [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/ollama_bot/AGENTS.md) を参照してください。

> [!NOTE]
> Discord 操作・返信生成・コマンドUIの実装本体は `front/discord/` へ、ふたば（`*.2chan.net`）監視・投稿イベントの実装本体は `front/hutaba/monitor/` へ移設されました。
> Discord 入出力や返信生成を修正する場合は `front/discord/` を、ふたば連携を修正する場合は `front/hutaba/` を開いて作業してください。

### 🎯 主な責務
1. **ペルソナ・Bot設定管理**: `config_*.py`, `config_base.py`, 環境変数優先解決。
2. **Ollama 通信＆排他制御**: `common/ollama/`, セマフォ排他, `release_ollama_model`, VRAM制御。
3. **感情状態・関係値モデル**: 8基本感情, tension算出, 恋人 `is_lover` 永続化, サーバー別Bio同期。
4. **後方互換シム保守**: `ollama_chat_/` 配下の転送シム維持。

### 🚫 境界条件と禁止事項
1. **他ディレクトリ編集禁止**: `ollama_bot/` 外のファイルを直接編集しないこと。
2. **単一プロセス・サーバー別Bio同期（項目9, 122, 124）**: `PATCH /guilds/{guild_id}/members/@me` による独立Bio管理。
3. **ハードコード禁止（項目5, 11）**: 口調・キャラ台詞は `config_*.py` 駆動。
4. **Ollama/VRAM解放ライフサイクル（項目15, 144, 180）**: Vision/Embeddingの即時解放、推論前middle-llmアンロード。
5. **感情・関係性モデル保護（項目1, 5, 22, 160, 177）**: tension誤算出防止、`is_lover` 永続化。

### 🛠️ 検証手順
```bash
PYTHONPATH=mock_libs:. pytest ollama_bot/tests/
PYTHONPATH=mock_libs:. mypy --config-file mypy.ini ollama_bot
```
