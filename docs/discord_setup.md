[← READMEに戻る](../README.md)

# ⚙️ Discord Bot 設定と必要な権限 (Setup & Permissions)

Discord AI Bot を正常に稼働させるためには、Discord Developer Portal でのインテント有効化、サーバー招待時の権限付与、およびチャンネルやロールの設定が必要です。

---

## 1. Privileged Gateway Intents の有効化（必須）

Discord Developer Portal の **[Applications] ＞ 該当Bot ＞ [Bot]** タブにある **Privileged Gateway Intents** で、以下の2項目を必ず **ON** にして **Save Changes** で保存してください。未設定の場合、起動時にエラーとなります。

| インテント名 | 必須度 | 利用目的 |
| :--- | :---: | :--- |
| **Message Content Intent** | **必須** | メッセージ本文の読み取り。OFF の場合、Bot宛てメンション以外の発言本文が空文字となりAIが会話できません。 |
| **Server Members Intent** | **必須** | サーバーメンバー一覧・プロファイル情報の取得。会話中のメンバー名解決（User-First照合）やニックネーム同期に必須です。 |

---

## 2. 必要な Discord 権限 (Bot Permissions)

サーバー招待リンクの生成時、またはサーバー内の Bot 用ロールに付与すべき権限一覧です。

| 権限名 (Permission) | 種別 | 利用目的・影響 |
| :--- | :---: | :--- |
| **View Channels** (`閲覧`) | **必須** | 動作対象チャンネルの閲覧。 |
| **Send Messages** (`送信`) | **必須** | メッセージへのAI応答、自発発言の送信。 |
| **Send Messages in Threads** | **必須** | スレッド内での会話・返信。 |
| **Read Message History** | **必須** | 会話文脈の取得、返信ツリー親メッセージの追跡（Anti-Hijack保護）、長期記憶の抽出。 |
| **Embed Links** | **推奨** | ティア表パーティゲーム（`/tier_game`）のEmbed表示、リッチカード返答。 |
| **Attach Files** | **推奨** | 画像認識（Vision）や生成画像の添付。 |
| **Add Reactions** | **推奨** | 会話への共感・感情リアクション（Good/Bad）、フィードバック観察。 |
| **Use External Emojis** | 任意 | 他サーバーのカスタム絵文字を使用したリアクション付与。 |
| **Change Nickname** | **推奨** | 感情パラメータ（喜怒哀楽）に応じたBot自身のニックネーム・表情タグの動的同期。 |

> [!IMPORTANT]
> **ロール階層（Role Hierarchy）の注意点**:
> Bot が自身のニックネームを変更（`Change Nickname`）するには、Discord サーバー設定の **[ロール]** 一覧において、**Botに付与されたロールが一般メンバーよりも上位** に配置されている必要があります。

---

## 3. サーバー招待リンクの生成 (OAuth2 URL Generator)

Developer Portal の **[OAuth2] ＞ [URL Generator]** で招待リンクを作成します。
1. **SCOPES**:
   - `bot`（現在の Discord 仕様ではスラッシュコマンド権限も自動付加されます）
2. **BOT PERMISSIONS**:
   - 上記表の各権限にチェックを入れます（テスト環境等では `Administrator` による一括許可も可能）。
3. 生成された URL をブラウザで開き、導入先サーバーを選択して認証・追加します。

---

## 4. チャンネル動作・応答モードの設定

本 Bot はチャンネルごとに柔軟な応答モードを設定できます（`.env` または `config_*.py` で指定）。

- 🏠 **メイン応答チャンネル (`OLLAMA_CHANNEL_ID`)**:
  - Bot 専用の会話チャンネル。メンション不要で、チャンネル内のすべての通常メッセージに対して 100% 応答します。
- 💬 **サブチャンネル (`OTHER_CHANNEL_IDS`)**:
  - 一般の雑談チャンネル。通常メッセージに対して独立確率（サイコロ方式: `OTHER_CHANNEL_RANDOM_MIN` 〜 `MAX`）で自発的に会話に混ざります。
  - クールダウン（`OTHER_CHANNEL_COOLDOWN_MESSAGES`）により短時間での連続返信を防止。
  - ※ メンション（`@Bot`）またはBotへの返信（Reply）が行われた場合は、確率抽選をバイパスして 100% 即時応答します。
- 🎮 **パーティゲーム専用チャンネル (`TIER_GAME_CHANNEL_ID`)**:
  - ティア表パーティゲーム（`/tier_game`）を行う専用チャンネル。他チャンネルでコマンドが実行された場合、このチャンネルへ自動誘導します（0で全チャンネル許可）。
- 🔒 **Bot稼働・停止制御コマンド (`BOT_ADMIN_USER_IDS`)**:
  - 管理者ユーザーのみが `/bot start`, `/bot stop`, `/bot status`（またはテキストコマンド `!bot`）でBotの稼働／スタンバイ状態を切り替えられます。
  - 停止中は Presence が「DND / 停止中（スタンバイ）」となり、メッセージ処理や自発発言を安全に休止します。
- 🚫 **除外ユーザー (`IGNORE_USER_IDS`)**:
  - 指定したユーザーID（他Botや荒らし等）からの全インタラクション（チャット、スラッシュコマンド、ボタン等）を完全に遮断します。
