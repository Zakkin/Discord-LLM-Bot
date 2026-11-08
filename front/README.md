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
