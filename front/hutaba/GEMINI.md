# 🌸 Front Hutaba サブプロジェクト開発ガイドライン (GEMINI.md)

本ディレクトリ（`front/hutaba/`）は、**ふたば（`*.2chan.net`）フロント層（スレッド取得・Shift_JIS解析・カタログ巡回・投稿・監視・そうだね学習）**を担当する独立サブプロジェクトです。

詳細な開発規約・境界条件・検証コマンドは [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/front/hutaba/AGENTS.md) を参照してください。

### 🎯 主な責務
1. **スレッド取得・解析**: `client/`（Shift_JIS解析、スレ画URL抽出、あぷ小画像抽出、中盤レス保持）。
2. **スレッド監視・投稿**: `monitor/`（巡回・監視・自律投稿・Focus Patrol・あぷ小動画スキップ）。
3. **要約・調査ツール**: `thread_reader.py`, `tools.py`（スレ画キャプション、スレ画特定画像検索）。

### 🚫 境界条件と重要禁止事項
1. **他ディレクトリ編集禁止**: `front/hutaba/` 外のファイルを直接編集しないこと。
2. **引用ルールの決定版仕様（項目179）**:
   - `target_reply is not None`（個別返信）: **引用先不変性ガード**により被引用数チェックや親レス補正・最多合流を一切行わず、対象レスを100%固定。
   - `target_reply is None` かつ 10レス以上: 事前特定した最多被返信レスまたは親レスへ強制合流して確実に引用成立。
   - `target_reply is None` かつ 10レス未満: 孤立レス引用は剥奪し、空配列 `[]` の単独レスとして安全投稿。
3. **スレ画キャプションとVRAM即時解放（項目180）**: `caption_hutaba_thread_image` の `finally:` で必ず `release_ollama_model` を呼び、VRAMを即時解放。LRUキャッシュを維持。
4. **あぷ小動画スキップ（項目184）**: `fu\d+\.(mp4|webm)` を含むレスへの自律返信を遮断。
5. **既出ツッコミパクり禁止（項目185）**: 既存レスの語尾変えパクり（`sibling_reply_plagiarism`）、キメラ合体、Hook Echo＋抽象ポエムを水際棄却。

### 🛠️ 検証手順
```bash
./front/hutaba/check.sh
# またはルートから:
PYTHONPATH=mock_libs:. pytest --import-mode=importlib front/hutaba/tests tests/test_img2channet.py
PYTHONPATH=mock_libs:. mypy --config-file mypy.ini front/hutaba
```
