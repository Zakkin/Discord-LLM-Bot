# Front X (`front/x/`)

`x.com`（旧Twitter）を操作するスクリプト群を格納するサブプロジェクトです。

## 構成
- `parser.py`: URL生成・スナップショット解析・ログイン壁判定・要求判定
- `loader.py`: ドメインヘルパーローダーとフォールバック
- `timeline.py`: Chrome MCP によるタイムライン・検索ページのブラウザ読み取り
- `tools.py`: `x_timeline_dispatch` / `x_search_dispatch`
- `proactive_feed.py`: 自発発話向けタイムライン取得・ローテーション・整形
- `adapter.py`: `XAdapter`（`PlatformAdapter` 実装）
