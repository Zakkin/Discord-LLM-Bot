# Harness Agent Core (`harness/agent/`)

AIエージェント・ハーネスのトップレベルパッケージおよび共通フック機構（`hooks.py`）を提供するサブプロジェクトです。

## 構成
- `hooks.py`: 全フロント共通の送信前・保存前フック機構（接点・判定・レジストリ・ランナー）
- `__init__.py`: ハーネス各サブパッケージのファサードエクスポート
- `tests/`: `harness/agent/` 直下モジュールの単体テスト（`test_hooks.py` 等）
- `test_harness/`: サンドボックス・テレメトリ・評価ハーネス基盤
- 配下サブプロジェクト（個別ワークスペース管理）:
  - `prompt_parser/`: フェーズ1（Context）・フェーズ2（Thought）・フェーズ5（Final Response）
  - `tool_registry/`: フェーズ3（Action / ツール・MCP・Web調査）
  - `observation/`: フェーズ4（Observation / 評価・ループ検知）
  - `memory_manager/`: メモリ管理（SQLite / RAG / 要約 / 習慣）
  - `orchestrator/`: 全体統括（5段階推論ループ / 自発発話 / 定期内省 / モノローグ）

## フック機構（`hooks.py`）の仕様
- **接点（ポイント）**:
  - `REPLY_PRE_SEND = "reply_pre_send"`: Discord 返信の送信直前
  - `POST_PRE_SEND = "post_pre_send"`: ふたば投稿の書き込み直前
  - `MEMORY_PRE_STORE = "memory_pre_store"`: 記憶の保存直前
- **主要API**:
  - `register_hook(point, name, func, *, order=100)`: フック関数の登録
  - `run_hooks(point, context) -> HookOutcome`: `order` 昇順（同順は登録順）でのフック連鎖実行
  - `clear_hooks(point=None)`: テスト用フック初期化
- **契約**:
  - 同期・純粋関数のみを対象とし、例外時は WARNING ログを出して `allow` として続行（fail-open、`applied` に `"名前(error)"` を記録）。
  - `HOOKS_ENABLED`（`cfg_bool("HOOKS_ENABLED", True)`）によるキルスイッチ対応。
