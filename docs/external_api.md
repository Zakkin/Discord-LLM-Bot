[← READMEに戻る](../README.md)

# ☁️ 外部 LLM API 連携 (OpenAI / Google Gemini)

本 Bot はローカルの Ollama / llama-server だけでなく、**OpenAI API 互換モード (`OLLAMA_USE_OPENAI_API=true`)** を標準搭載しており、**OpenAI (GPT-4o, GPT-4o-mini)** や **Google Gemini (Gemini 2.0 Flash, Gemini 1.5 Pro)** 等のクラウド API と接続して運用できます。

ローカル GPU を持たないマシンで運用したい場合や、中間タスクをクラウドにオフロードしてローカルの GPU 負荷を下げたい場合に最適です。

---

## 1. OpenAI API への接続設定

OpenAI 公式 API または互換プロキシ（LiteLLM / One API / vLLM）を使用する場合：

`.env` 設定例:
```ini
# OpenAI API 互換モードを有効化
OLLAMA_USE_OPENAI_API=true

# API エンドポイント
OLLAMA_BASE_URL=https://api.openai.com

# 使用モデル名
OLLAMA_MODEL=gpt-4o
OLLAMA_MIDDLE_MODEL=gpt-4o-mini
OLLAMA_VISION_MODEL=gpt-4o
```
※ API キー認証が必要な場合は、LiteLLM などのローカルプロキシを介して `Authorization: Bearer <KEY>` を付与するか、リバースプロキシでヘッダーを注入して中継します。

---

## 2. Google Gemini API への接続設定

Google Gemini は Google AI Studio が提供する **OpenAI 互換エンドポイント**（または LiteLLM）を利用して接続できます。

`.env` 設定例:
```ini
# OpenAI 互換モードを有効化
OLLAMA_USE_OPENAI_API=true

# Google Gemini の OpenAI 互換エンドポイント
OLLAMA_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai

# 使用モデル名
OLLAMA_MODEL=gemini-2.0-flash
OLLAMA_MIDDLE_MODEL=gemini-2.0-flash
OLLAMA_VISION_MODEL=gemini-2.0-flash
```

---

## 3. ローカル ＋ クラウドのハイブリッド運用 (おすすめ)

- **メイン対話モデル**: ローカルの `Qwen3.6` や `llama-server`（Modelfile で独自のキャラクター性や口調を厳密に固定）
- **中間分類タスク (`middle-llm`)**: 外部 API（`Gemini 2.0 Flash` や `GPT-4o-mini` など、超高速・安価なクラウドモデル）

このハイブリッド構成により、ローカル GPU の VRAM をメイン対話モデル専用に 100% 割り当てつつ、高精度かつ高速な意図判定や感情シミュレーションを実現できます。
