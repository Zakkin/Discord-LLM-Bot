# 共通ライブラリパッケージ (`lib/`)

discord-ai-bot プロジェクト全体で利用される共通ユーティリティおよびインフラ基盤モジュール群です。

## 📦 提供モジュール一覧

| モジュール | 責務・概要 |
| :--- | :--- |
| `error_report.py` | プロセス内例外集計、サマリログ出力、閾値超過時の通知発報 |
| `config_utils.py` | Bot設定、環境変数、コンフィグ値の解決・取得 |
| `date_utils.py` | タイムゾーン処理（JST/UTC）、静粛時間帯判定 |
| `db_utils.py` | SQLite接続・データベース操作ユーティリティ |
| `discord_utils.py` | Discord API 関連の補助処理 |
| `dispatch_utils.py` | イベントディスパッチ・タスク実行制御 |
| `html_utils.py` | HTML解析・サニタイズ処理 |
| `img2chan_utils.py` | 画像掲示板連携向けユーティリティ |
| `json_utils.py` | JSON シリアライズ・デシリアライズ補助 |
| `media_utils.py` | メディアファイル処理・検証 |
| `text_utils.py` | テキスト整形・抽出・正規化 |

---

## 🚨 例外集計レポーター (`error_report.py`)

### 概要
プロセス全体で発生した例外を集計し、一定時間（ウィンドウ）ごとのサマリ警告ログ出力、および頻発する例外の早期検知・通知（Notifier呼び出し）を行う共通基盤です。

### 設計原則（完全防御原則）
`record()` および `report_error()` は、いかなる内部エラーや設定不備、Notifierの失敗が発生しても、**呼び出し元のメイン処理を巻き込んで例外を漏らすことは絶対にありません**（`try...except BaseException` 完全防御）。

### 主なインターフェース

```python
class ErrorReporter:
    def record(self, site: str, exc: BaseException) -> None:
        """例外を記録・集計し、必要に応じてログ出力やNotifier呼び出しを行う。"""
        ...

    def summary(self) -> dict[str, int]:
        """現在ウィンドウ内の集計結果を取得する（キー: 'site|ExceptionClassName'）。"""
        ...

    def reset(self) -> None:
        """集計カウント、通知済みフラグ、ウィンドウ開始時刻を初期化する。"""
        ...

    def set_notifier(self, fn: Callable[[str, int, BaseException], None] | None) -> None:
        """閾値超過時に呼び出す通知コールバックを設定する。"""
        ...

def get_error_reporter() -> ErrorReporter:
    """プロセス内シングルトンの ErrorReporter インスタンスを取得する。"""
    ...

def report_error(site: str, exc: BaseException) -> None:
    """例外を記録する安全なラッパー関数（一切の例外を外部に漏らさない）。"""
    ...
```

### 使い方

```python
from lib.error_report import report_error

try:
    some_risky_operation()
except Exception as e:
    # 呼び出し元でログ出力しつつ、共通レポーターへ記録
    log.exception("Operation failed: %s", e)
    report_error("some_service.operation", e)
```

### 設定パラメータ

| 環境変数 / 設定キー | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `ERROR_REPORT_ENABLED` | `True` | 例外集計レポーターの有効・無効化フラグ |
| `ERROR_REPORT_INTERVAL_SEC` | `300` (秒) | 集計ウィンドウ間隔（最小10秒） |
| `ERROR_REPORT_NOTIFY_THRESHOLD` | `20` (回) | 同一キー（`site|ExceptionClassName`）で通知を発報する閾値（最小1回） |

---

## 🧪 テストの実行

```bash
# 全体テスト
pytest

# error_report 単体テスト
pytest tests/test_error_report.py
```
