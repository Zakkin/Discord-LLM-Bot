# Front Bridge Layer (`front/`)

Discord・ふたば☆ちゃんねる（`*.2chan.net`）・X（`x.com`）の各フロント層と `harness/agent/` を橋渡しする中間表現（IR）およびブリッジパッケージです。

## 構成
- `ir.py`: `FrontEvent`, `AgentTask`, `AgentOutcome`, `FrontAction`, `Location`（年齢制限 `is_nsfw` フラグ対応）
- `adapter.py`: `PlatformAdapter` プロトコル
- `bridge.py`: `AgentBridge`（`AgentTask` ⇄ `AgentOrchestrator`）
- `registry.py`: アダプター・ドメインヘルパー登録
- `discord/`: Discord 操作サブプロジェクト
- `hutaba/`: `*.2chan.net` 操作サブプロジェクト
- `x/`: `x.com` 操作サブプロジェクト

## 主な機能・共通仕様

### 📜 全フロント共通行動ログ（自分の体験の台帳: `bot_activities`）
各フロント（Discord, ふたば, X）で発生したボット自身の行動（発言・リアクション・タイムライン観察）を共通台帳（SQLite `bot_activities` テーブル）に集約記録し、プラットフォームを越えて自己体験として一貫した想起・回答を可能にする仕組みです。

- **連携方式と書き込み規則**:
  - **Discord (`front/discord/`)**:
    - 通常返信 (`reply`)、相槌やスコアリアクション (`reaction`) を記録。
  - **ふたば (`front/hutaba/`)**:
    - スレッドへの書き込み成功時 (`post`) を記録。
  - **X (`front/x/`)**:
    - 自発投稿向けのタイムライン観察 (`observed`)、オーケストレータ発話による返信やポスト (`reply`/`post`) を記録。
- **境界制約とプライバシー保護**:
  - **他者発言の要約制限**: 他者の発言内容は最大80文字（`ACTIVITY_TARGET_TEXT_MAX_CHARS`）の要旨としてのみ記録し、逐語引用を禁止。
  - **保持期限**: 180日間保持とし、定期ループで安全に自動パージ。
  - **機能フラグ**: `ACTIVITY_LOG_ENABLED=false` により記録および検索を完全停止可能。
