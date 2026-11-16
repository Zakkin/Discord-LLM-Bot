# 🪝 Harness Agent Core サブプロジェクト開発ガイドライン (AGENTS.md)

本ディレクトリ（`harness/agent/` 直下）は、AIエージェント・ハーネスの共通基盤（フック機構 `hooks.py`、トップレベルファサード `__init__.py`、およびテストハーネス `test_harness/`）を担当するサブプロジェクトです。

---

## 🎯 担当範囲と責務
1. **共通フック機構基盤（`harness/agent/hooks.py`）**:
   - 各フロント（Discord / ふたば等）の送信直前や記憶保存直前などの決まった接点（`REPLY_PRE_SEND`, `POST_PRE_SEND`, `MEMORY_PRE_STORE`）に検査・書き換え・遮断ロジックを登録・実行する共通レジストリとランナー（`register_hook`, `clear_hooks`, `run_hooks`）。
   - データ構造（`HookContext`, `HookVerdict`, `HookOutcome`）およびフック関数型（`HookFunc`）。
   - **4大契約**:
     1. **同期・純粋関数（Synchronous & Pure）**: フック関数は同期の純粋関数に限定（LLM呼び出し・ネットワーク/DB I/O・非同期処理は対象外）。
     2. **Fail-Open**: フックの例外は必ず握りつぶし、`WARNING` ログを出して `allow` として続行する（`applied` には `"名前(error)"` を記録）。
     3. **キルスイッチ**: 設定 `HOOKS_ENABLED`（`ollama_bot.common.config_helpers.cfg_bool`、既定 `True`）が `False` の場合は何も実行せず即座に `allow` を返す（import失敗時は `True` 扱い）。
     4. **実行順序と連鎖**: `order` 昇順（同順は登録順）で実行し、`rewrite` は `context.text` を差し替えて後続へ引き継ぎ、`block` は即座に後続を停止して `blocked=True` と理由を返す。
2. **ハーネス共通エクスポート（`harness/agent/__init__.py`）**:
   - 各サブシステム（`prompt_parser`, `orchestrator`, `memory_manager`, `tool_registry`, `observation`, `test_harness`）の統合公開インターフェース。
3. **テストハーネス（`harness/agent/test_harness/`）**:
   - サンドボックス実行・トレーシング・機械的アサーション・評価レポート基盤。

---

## 🚫 境界条件と禁止事項（最重要）
1. **【スコープ外ファイル編集の厳禁】**:
   - **本サブプロジェクト単体での作業時は、`harness/agent/` 直下のファイル（`hooks.py`, `__init__.py`, `tests/`, `test_harness/`）のみを編集すること。**
   - 配下の独立サブプロジェクト（`prompt_parser/`, `tool_registry/`, `observation/`, `memory_manager/`, `orchestrator/`）や外部パッケージ（`front/`, `ollama_bot/`, `tier_game/`）のファイルを直接編集してはならない。
2. **【フック関数の純粋性・Fail-Open 死守】**:
   - `hooks.py` に非同期処理や外部 I/O・LLM 推論を持ち込まないこと。
   - フック実行中のいかなる例外によっても呼び出し元の送信・保存フローを停止させないこと（常に fail-open）。
3. **【公開・デプロイスクリプトとの同期義務（項目166）】**:
   - 新規モジュールを追加した際は、[`customize_for_github.py`](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/customize_for_github.py) の `allowed_items`（エクスポート許可リスト）に登録されていることを確認すること。

---

## 🛠️ 検証手順
```bash
# サブプロジェクト単体テスト
PYTHONPATH=.pkg:../..:../../mock_libs:. pytest tests/test_hooks.py -q
# リポジトリルートからの検証
PYTHONPATH=mock_libs:. pytest harness/agent/tests/test_hooks.py -q
PYTHONPATH=mock_libs:. mypy --config-file mypy.ini harness/agent/hooks.py
```
