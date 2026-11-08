# Ollama Bot (`ollama_bot/`)

ペルソナ設定（`config_*.py`）、Ollama クライアント（`common/ollama/`）、感情状態・関係値シミュレーション（`ollama_chat_/emotion/`）、および `front/discord/` への後方互換シムを担当するサブプロジェクトです。

## 構成
- `config_base.py`: 全ペルソナ共通の基本設定およびデフォルト環境変数定義
- `config_loader.py` / `config_sample.py`: 設定ローダーおよび設定サンプル
- `common/ollama/`: Ollama API クライアント、トークン推定、モデル制御
- `ollama_chat_/`: 感情状態（`emotion/`）および旧パス互換シム層。感情採点プロンプト構築時のタイムスタンプ（`[HH:MM]`）付き会話履歴の安全な話者分割・正規化（`_split_context_line`）および簡体字混入検出（`contains_cjk_non_japanese`）を包含。

## 主要環境変数・設定一覧

### ふたば（img2chan）連携・Discord誘導設定
| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `IMG2CHAN_PROMOTION_ENABLED` | `false` | ふたば自律投稿時におけるDiscord招待URL自動誘導の有効化フラグ（**安全のため既定値は無効**）。明示的に `"true"` を指定した場合のみ有効化されます。 |
| `IMG2CHAN_PROMOTION_PROBABILITY` | `0.05` | 誘導メッセージが付与される確率（5%） |
| `IMG2CHAN_PROMOTION_MIN_INTERVAL_SEC` | `21600` | 誘導メッセージ付与の最小インターバル（秒、既定: 6時間） |
| `IMG2CHAN_PROMOTION_INVITE_URL` | 各ペルソナ設定準拠 | Discord招待リンクURL |
| `IMG2CHAN_PROMOTION_MESSAGE` | 各ペルソナ設定準拠 | 投稿末尾に付加される案内文面 |

### 自発発話・モデル設定
| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `AGENT_PROACTIVE_POST_ENABLED` | `true` | 自発発話（会話が途切れた際の自律発話）の有効/無効 |
| `OLLAMA_MAIN_MODEL` | ペルソナ準拠 | メイン推論用モデル名 |
| `OLLAMA_NUM_CTX` | `16384` | コンテキスト長上限 |
| `OLLAMA_EMBED_KEEP_ALIVE` | `0` | 埋め込みモデル（bge-m3等）使用後の即時アンロード設定（VRAM競合・メインモデルCPUオフロード防止） |

### 2段階Visionパイプライン設定（画像読み取り専用前処理とVRAM即時解放）
| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `OLLAMA_VISION_MODEL` | `qwen3-vl:8b` | 画像解析・キャプション生成用Visionモデル名 |
| `OLLAMA_VISION_KEEP_ALIVE` | `0` | Visionモデル推論完了後のアンロード待機時間（即時解放） |
| `OLLAMA_VISION_DESCRIBE_TIMEOUT_SEC` | `60.0` | 画像キャプション生成のタイムアウト秒数（Cold Start対応） |
| `OLLAMA_VISION_NUM_CTX` | `4096` | Visionモデルのコンテキスト長（画像解析・プロンプト用） |
| `OLLAMA_VISION_NUM_PREDICT` | `256` | 画像説明・文字起こしの最大出力トークン数 |
| `OLLAMA_VISION_TEMPERATURE` | `0.1` | 客観的描写のための生成温度（低温度） |
| `VISION_PIPELINE_MODE` | `caption_then_main` | 画像処理パイプラインモード |

### サブチャンネル（草むら）応答・割り込み設定
| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `OTHER_CHANNEL_SKIP_LOW_INFO_REPLY` | `true` | 他ユーザー宛ての返信で、本文が数字・記号・顔文字のみ（意味の取れる語がない）の場合にサイコロを消費せずスキップする設定（bool） |

### メッセージ受信・返信トリガー仕様（画像付きメッセージ対応）
- **画像付きメッセージ・画像単体メッセージの返信許可**:
  - 画像（添付ファイルまたは画像URL）が含まれるメッセージは、テキストの有無にかかわらず無視されず返信対象となります。
  - テキストが空であっても、画像またはサポート対象メディアが含まれていれば2段階Visionパイプライン経由で画像内容（キャプション・文字起こし）が抽出され、Bot返信が生成されます。
  - テキスト・画像・サポート対象メディアが一切存在しない完全な空メッセージのみが無視（スキップ）されます。

## 型検査
```bash
mypy --config-file mypy.ini ollama_bot
```
