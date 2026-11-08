# 🐦 Front X サブプロジェクト開発ガイドライン (AGENTS.md)

本ディレクトリ（`front/x/`）は、**X（`x.com` / 旧Twitter）のタイムライン取得・検索・スナップショット解析・ログイン壁判定・自発発話向けフィード整形**を担当する独立サブプロジェクトです。

---

## 🎯 担当範囲と責務
1. **スナップショット解析・要求判定（`parser.py`）**:
   - `x.com` のタイムライン・検索 URL 生成、アクセシビリティツリー / DOM テキストからのポスト抽出、ログイン壁・エラーページ判定、タイムライン/検索リクエスト判定。
2. **ブラウザ読み取り・ディスパッチ（`timeline.py`, `tools.py`, `loader.py`）**:
   - Chrome MCP を用いたホームタイムライン・検索結果の読み取り（`read_x_timeline_with_browser`）、`x_timeline_dispatch` / `x_search_dispatch`、ヘルパーローダーとフォールバック（`_MissingXComHelpers`）。
3. **自発発話フィード管理・アダプター（`proactive_feed.py`, `adapter.py`）**:
   - 自発発話向けの直近ポスト取得・既読ローテーション・プロンプト整形、`XAdapter`。

---

## 🚫 境界条件と禁止事項（最重要）
1. **【スコープ外ファイル編集の厳禁】**:
   - **自身が担当する `front/x/` 配下のファイルのみを編集すること。**
   - 他プラットフォーム（`front/discord/`, `front/hutaba/`）や `front/` 直下、`harness/agent/*`, `ollama_bot/` のファイルを直接編集してはならない。
2. **【他プラットフォーム直接 import の禁止】**:
   - `front.discord` や `front.hutaba` を直接 import しないこと。
3. **ログイン壁・UIノイズ混入防止（BAN-139）**:
   - 未ログイン画面やナビゲーション文言（「いまどうしてる？」「おすすめユーザー」等）をポスト本文に混入させないよう、`parser.py` のフィルタと `is_login_wall_or_error_page` による多重防御を維持すること。
   - X固有のUIメタデータ・メトリクス（`[0-9.,万億kmbKMB]+\s*件の表示` などのインプレッション数/表示回数、`引用を表示`、`返信をポスト`、`ポスト会話`、`会話を表示`、`スレッドを表示`、`Post your reply`、`View quotes` 等のアクションボタン）がポスト本文や要約抜粋に混入しないよう、`_clean_timeline_line`、`_is_timeline_noise_line`、`_trim_timeline_post_text` の3層で確実にノイズを除外・置換すること。
4. **【公開・デプロイスクリプトとの同期義務（項目166）】**:
   - ディレクトリ構造変更や新規共通モジュール追加時は、`customize_for_github.py` の `allowed_items`、`mypy.ini`、`pytest.ini`、`discord-ai-bot.code-workspace` の同期漏れがないか確認すること。

---

## 🛠️ 検証手順
```bash
PYTHONPATH=mock_libs:. pytest --import-mode=importlib front/x/tests tests/test_web_research.py -k "x_timeline or x_search or xcom or FrontX"
PYTHONPATH=mock_libs:. mypy --config-file mypy.ini front/x
```
