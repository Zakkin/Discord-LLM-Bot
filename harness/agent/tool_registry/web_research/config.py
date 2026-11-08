"""
web_research/config.py

このファイルはウェブリサーチ機能全体で使用される設定値・定数・設定取得関数を管理します。
- デフォルト値・上限値などの定数定義
- キーワードリスト（調査要求・時事性・製品仕様など）
- 正規表現パターン
- 設定値を環境変数から読み込むゲッター関数

他のモジュールはこのファイルから設定をインポートして使用します。
AIエージェントが新しい設定を追加する場合はここに追記してください。
"""

from __future__ import annotations

import re
from typing import Any

from ollama_bot.common.config_helpers import cfg, cfg_bool, cfg_float, cfg_int


# ---------------------------------------------------------------------------
# Research dispatch mode type alias (str literal union)
# ---------------------------------------------------------------------------

from typing import Literal

ResearchDispatchMode = Literal[
    "auto",
    "simple_search",
    "browser_search",
    "browser_read_url",
    "x_timeline",
    "x_search",
    "img_top5",
    "img_thread",
    "fact_check",
    "image_search",
]

# ---------------------------------------------------------------------------
# Protocol / error constants
# ---------------------------------------------------------------------------

_MCP_PROTOCOL_VERSION = "2025-03-26"
_NO_RESULTS_TEXT = "検索結果が見つかりませんでした。"

# ---------------------------------------------------------------------------
# X.com / login-wall fallback constants
# ---------------------------------------------------------------------------

_FALLBACK_XCOM_DYNAMIC_BROWSER_HOSTS: tuple[str, ...] = ("x.com", "twitter.com")
_FALLBACK_XCOM_LOGIN_WALL_PHRASES: tuple[str, ...] = (
    "don't miss what's happening",
    "don\u2019t miss what\u2019s happening",
    "people on x are the first to know",
    "something went wrong",
    "please log in",
    "please sign in",
    "sign in to",
    "log in to",
    "you need to sign in",
    "login required",
    "access denied",
    "403 forbidden",
    "page not found",
    "404 not found",
    "this content isn't available",
    "this tweet is from an account that no longer exists",
    "something went wrong, but don't fret",
    "something went wrong, but don\u2019t fret",
    "some privacy related extensions may cause issues on x.com",
)
_FALLBACK_XCOM_STRONG_LOGIN_WALL_PHRASES: tuple[str, ...] = (
    "don't miss what's happening",
    "don\u2019t miss what\u2019s happening",
    "people on x are the first to know",
    "some privacy related extensions may cause issues on x.com",
)
_FALLBACK_XCOM_LOGIN_WALL_MAX_CONTENT_LEN = 600

# ---------------------------------------------------------------------------
# Keyword lists for research detection
# ---------------------------------------------------------------------------

_RESEARCH_REQUEST_KEYWORDS = (
    "調べて",
    "調べろ",
    "調査して",
    "調査しろ",
    "検索して",
    "検索しろ",
    "検索",
    "ググって",
    "ググれ",
    "確認して",
    "確認しろ",
    "見てきて",
    "見てきてほしい",
)
_IMAGE_SEARCH_TRIGGER_KEYWORDS: tuple[str, ...] = (
    "これ誰",
    "このキャラ誰",
    "誰これ",
    "この人誰",
    "何のキャラ",
    "何の画像",
    "画像検索",
    "元ネタ",
    "出典教えて",
    "誰の画像",
    "元の画像",
    "元画像",
    "画像元",
)
_FACT_CHECK_KEYWORDS: tuple[str, ...] = (
    "ファクトチェック",
    "factcheck",
    "fact_check",
    "検証",
    "真偽",
    "裏取り",
    "裏を取",
    "デマ",
    "ガセ",
    "事実関係",
    "事実確認",
    "信憑性",
    "信ぴょう性",
    "公式設定",
    "公式発表",
    "公式情報",
    "原作設定",
    "公式見解",
    "裏設定",
    "エピローグ",
)
_FACT_CHECK_QUESTION_RE: re.Pattern[str] = re.compile(
    r"(?:本当|ほんと|ホント|事実|デマ|ガセ|マジ|まじ|正しい|(?<!付き)(?<!つき)(?<!話し)(?<!はなし)(?<!居合)(?<!居)(?<!持ち)(?<!もち)(?<!巡り)(?<!めぐり)(?<!重なり)(?<!かさなり)(?<!噛み)(?<!かみ)(?<!食い)(?<!くい)(?<!助け)(?<!たすけ)(?<!譲り)(?<!ゆずり)(?<!見合)(?<!みあ)(?<!似)(?<!見)(?<!出)(?<!釣り)(?<!つり)(?<!愛し)(?<!あいし)(?<!知り)(?<!しり)合ってる|本当のところ)"
    r"(?:な?の|なのか|なんですか|ですか|なの？|なの\?|か|？|\?|って本当|ってほんと|ってホント|ってマジ|ってまじ|かどうか|なのかどうか)"
)
_FACT_CHECK_HEARSAY_RE: re.Pattern[str] = re.compile(
    r"(?:(?:とのこと|との事|という話|って話|らしい|と聞いた|って聞いた)(?:らしい|けど|が|だが|ですが|とのこと|本当|ほんと|ホント|なのか|なのかな|のか|って本当|ってマジ))"
    r"|(?:(?:とのこと|との事)らしい)"
    r"|(?:(?:という|って)?噂(?:だけど|だが|ですが|らしい|本当|ほんと|ホント|なのか))"
    r"|(?:(?:って|は|が)(?:本当|ほんと|ホント|事実|マジ|まじ)(?:な?の|なのか|なんですか|ですか|？|\?))"
    r"|(?:らしい(?:けど|が|だが|ですか|本当|ほんと|ホント))"
)
_SIMULATION_IMPACT_KEYWORDS: tuple[str, ...] = (
    "人体への影響",
    "人体影響",
    "健康被害",
    "致死量",
    "致死率",
    "致死電流",
    "致死電圧",
    "危険性",
    "への影響",
    "による影響",
    "悪影響",
    "後遺症",
    "副作用",
)
_SIMULATION_IMPACT_QUESTION_RE: re.Pattern[str] = re.compile(
    r"(?:"
    r"(?:食らっ|くらっ|喰らっ|浴び|あび|受け|うけ|食べ|たべ|当たっ|あたっ|触っ|さわっ|刺され|さされ|刺し|さし|落ち|おち|吸っ|すっ|漏れ|もれ|流れ|ながれ|噛まれ|かまれ|撃たれ|うたれ|撃ち|し|され|なっ|起き|おき)たら"
    r"|(?:飲ん|のん|踏ん|ふん|噛ん|かん|死ん|しん)だら"
    r"|(?:する|受ける|浴びる|食らう|喰らう|触る|飲む|食べる|落ちる|吸う|流れる|起きる|噛む|踏む)と"
    r")"
    r"(?:[^\n。！？!?]{0,25}?)"
    r"(?:どうなる|どうなるの|どうなるのか|どうなりますか|何が起きる|何が起こる|死ぬの|死ぬのか|死にますか|危険なの|大丈夫なの|耐えられるの|影響は|影響ある)"
)
_TRUSTED_FACT_DOMAINS: tuple[str, ...] = (
    "wikipedia.org",
    "dic.pixiv.net",
    "dic.nicovideo.jp",
    "w.atwiki.jp",
    "kotobank.jp",
    "weblio.jp",
    "animedia.jp",
    "famitsu.com",
    "4gamer.net",
    "dengekionline.com",
    "game.watch.impress.co.jp",
    "itmedia.co.jp",
    "oricon.co.jp",
    "natalie.mu",
    "nhk.or.jp",
    "news.yahoo.co.jp",
    "yomiuri.co.jp",
    "asahi.com",
    "mainichi.jp",
    "nikkei.com",
    "sankei.com",
)
_NOISE_DOMAINS: tuple[str, ...] = (
    "girlschannel.net",
    "chiebukuro.yahoo.co.jp",
    "detail.chiebukuro.yahoo.co.jp",
    "z-z.jp",
    "komachi.yomiuri.co.jp",
    "oshiete.goo.ne.jp",
    "5ch.net",
    "2ch.sc",
    "open2ch.net",
    "2ch.net",
    "shitaraba.net",
    "jbbs.shitaraba.net",
    "togetter.com",
    "hatelabo.jp",
    "anond.hatelabo.jp",
    "b.hatena.ne.jp",
    "bakusai.com",
    "hostlove.com",
    "hitomi.la",
    "e-hentai.org",
    "nhentai.net",
    "exhentai.org",
    "doujinshi.org",
    "wnacg.com",
    "eromanga-cafe.com",
    "nyaa.si",
    "kemono.su",
    "coomer.su",
    "rapidgator.net",
    "uploaded.net",
    "mediafire.com",
)
_NOISE_DOMAIN_KEYWORD_PATTERNS: tuple[str, ...] = (
    "eromanga",
    "hentai",
    "doujin",
    "pornhub",
    "xvideos",
    "jav",
    "matome",
)
_SOURCE_REQUEST_KEYWORDS = (
    "ソース",
    "出典",
    "根拠",
    "情報源",
    "参照",
    "source",
)
_RECENCY_KEYWORDS = (
    "今",
    "いま",
    "現在",
    "最新",
    "最近",
    "今日",
    "きょう",
    "昨日",
    "明日",
    "今年",
    "いまどう",
    "今どう",
    "アップデート",
    "更新",
    "まだ",
    "やってる",
    "開催中",
    "配布",
    "リアルタイム",
)
_NEWS_KEYWORDS = (
    "ニュース",
    "報道",
    "速報",
    "話題",
)
_PRODUCT_SPEC_KEYWORDS = (
    "仕様",
    "スペック",
    "spec",
    "価格",
    "値段",
    "料金",
    "発売日",
    "対応",
    "互換",
    "ベンチ",
    "性能",
    "モデル",
)
_EXPLANATION_KEYWORDS = (
    "って何",
    "ってなに",
    "とは",
    "とは何",
    "とはなに",
    "何それ",
    "なにそれ",
    "何者",
    "誰",
    "だれ",
    "意味",
    "詳しく",
    "教えて",
    "教えろ",
    "説明して",
    "説明しろ",
    "何があった",
    "何があったのか",
    "何が起きた",
    "どういうこと",
    "何の話",
    "どういう話",
    "kwsk",
    "詳細教えて",
)
_CONVERSATIONAL_FOLLOWUP_MARKERS = (
    "強いて",
    "しいて",
    "あえて",
    "じゃあ",
    "なら",
    "だったら",
    "で言う",
    "でいう",
    "誰？",
    "誰?",
    "だれ？",
    "だれ?",
    "どれ",
    "どっち",
)
_PREFERENCE_OR_OPINION_MARKERS = (
    "好き",
    "推し",
    "お気に入り",
    "好み",
    "興味",
    "気になる",
    "おすすめ",
    "オススメ",
    "付き合っ",
    "付きあい",
    "付き合い",
    "恋人",
    "彼氏",
    "彼女",
    "仲良し",
    "仲が",
)
_CONTEXT_URL_TARGET_KEYWORDS = (
    "url",
    "リンク",
    "記事",
    "ページ",
    "投稿",
    "ポスト",
    "ツイート",
    "スレ",
    "スレッド",
    "先",
    "中身",
    "内容",
    "本文",
    "これ",
    "それ",
    "あれ",
)
_CONTEXT_URL_ACTION_KEYWORDS = (
    "見て",
    "読んで",
    "開いて",
    "確認",
    "調べ",
    "検索",
    "要約",
    "解説",
    "教えて",
    "まとめ",
    "本当",
    "ほんと",
    "真偽",
    "根拠",
    "ソース",
    "何",
    "なに",
    "どういう",
    "どう思う",
    "詳しく",
)
_CONTEXT_URL_STRONG_ACTION_KEYWORDS = (
    "見て",
    "読んで",
    "開いて",
    "要約",
    "解説",
    "まとめ",
)
_GENERIC_TERM_STOPWORDS = {
    # 指示語・時間・基本副詞
    "これ",
    "それ",
    "あれ",
    "今",
    "今日",
    "昨日",
    "明日",
    "現在",
    "最近",
    "最新",
    # Bot一般呼称（自身の名前・愛称は helpers.get_generic_term_stopwords() で動的合流）
    "アシスタント",
    "AI",
    "ボット",
    "bot",
    # 人称代名詞・呼称
    "貴様",
    "お前",
    "手前",
    "貴方",
    "自分",
    "相手",
    "味方",
    "敵",
    # 人間関係・一般名詞
    "恋人",
    "彼氏",
    "彼女",
    "今恋人",
    "友達",
    "記憶",
    "出来",
    # 既存の一般語
    "意味",
    "詳細",
    "仕様",
    "スペック",
    "ニュース",
    "ソース",
    "出典",
    "情報",
    "こと",
    "やつ",
    "もの",
    "なん",
    "なに",
    "何",
    "誰",
    "だれ",
    "教えて",
    "方法",
    "やり方",
    "やりかた",
    "仕方",
    "しかた",
    "使い方",
    "つかいかた",
    "テスト",
    "てすと",
    "チェック",
    "画数",
    "文字",
    "数字",
    "記号",
    "暗号",
    "正解",
    "問題",
    "答え",
    "法則",
    "一体",
    "一体何",
    "一体全体",
    "何事",
    "何故",
    "何時",
    "何年",
    "何年か",
    "何年前",
    "何年か前",
    "何ヶ月",
    "何日",
    "何回",
    "何個",
    "何人",
    "交換",
    "配布",
    "保存",
    "詠み手",
    "Assistant",
    "assistant",
    "User",
    "user",
    "System",
    "system",
    "Bot",
    "bot",
}
_COMMON_KATAKANA_STOPWORDS = {
    # 開発・チャット・日常IT
    "テスト",
    "チェック",
    "サンプル",
    "チャット",
    "ボット",
    "メッセージ",
    "チャンネル",
    "サーバー",
    "グループ",
    "メンバー",
    "ユーザー",
    "アカウント",
    "ゲーム",
    "アニメ",
    "オッケー",
    "オーケー",
    "バイバイ",
    "サンキュー",
    "レコーズ",
    "ランキング",
    # PC・ITハードウェア・周辺機器・日常デバイス用語
    "パソコン",
    "ノートパソコン",
    "デスクトップ",
    "マック",
    "スマホ",
    "タブレット",
    "モニター",
    "ディスプレイ",
    "キーボード",
    "マウス",
    "ハードディスク",
    "ストレージ",
    "メモリ",
    "プロセッサ",
    "プロセッサー",
    "グラフィックボード",
    "グラフィックカード",
    "マザーボード",
    "ルーター",
    "プリンター",
    "スキャナー",
    "スキャナ",
    "スピーカー",
    "ヘッドホン",
    "ヘッドフォン",
    "イヤホン",
    "マイク",
    "カメラ",
    "ウェブカメラ",
    "ドライブ",
    "ケーブル",
    "アダプター",
    "バッテリー",
    "モデム",
    "ハブ",
    "プロジェクター",
    "スイッチ",
    "ハードウェア",
    "ソフトウェア",
    "ファームウェア",
    "デバイス",
    "ブラウザ",
    "ブラウザー",
    "アプリ",
    "アプリケーション",
    "クラウド",
    "レンタル",
    "リース",
    "サポート",
    "サービス",
    # 日常会話・コミュニケーション・ソーシャル・感情・状態
    "アドバイス",
    "トラブル",
    "ストレス",
    "リフレッシュ",
    "テンション",
    "モチベーション",
    "モチベ",
    "ポジティブ",
    "ネガティブ",
    "サプライズ",
    "ハプニング",
    "チャンス",
    "ピンチ",
    "リスク",
    "プライベート",
    "リクエスト",
    "アンケート",
    "インタビュー",
    "プレゼン",
    "プレゼンテーション",
    "ミーティング",
    "スケジュール",
    "キャンセル",
    "ミス",
    "エラー",
    "クレーム",
    "フォロー",
    "フォロワー",
    "シェア",
    "ブロック",
    "ミュート",
    "スパム",
    "トレンド",
    "バズ",
    "リツイート",
    "リポスト",
    "プロフィール",
    "アイコン",
    "ヘッダー",
    "タイムライン",
    "フィード",
    "アラート",
    "ステータス",
    "オンライン",
    "オフライン",
    "サインイン",
    "サインアウト",
    "パスワード",
    "ボイスチャット",
    "ビデオチャット",
    "グループチャット",
    "スレッド",
    "トピック",
    "コミュニティ",
    "フレンド",
    "コンタクト",
    "マナー",
    "エチケット",
    "ハッシュタグ",
    "メンション",
    "プライバシー",
    "ポリシー",
    "オフィシャル",
    "テンプレ",
    "テンプレート",
    "コンディション",
    "ルーティン",
    "ルーティーン",
    # PC・スマホ・Web・デジタル操作・UI・ソフトウェア
    "クリック",
    "ダブルクリック",
    "タップ",
    "ダブルタップ",
    "スワイプ",
    "スクロール",
    "ドラッグ",
    "ドロップ",
    "ショートカット",
    "コピー",
    "ペースト",
    "コピペ",
    "カット",
    "アンドゥ",
    "リドゥ",
    "セーブ",
    "ロード",
    "バックアップ",
    "リストア",
    "シンク",
    "アップロード",
    "ダウンロード",
    "インストール",
    "アンインストール",
    "アップデート",
    "バージョンアップ",
    "アップグレード",
    "ダウングレード",
    "パッチ",
    "リセット",
    "リブート",
    "シャットダウン",
    "スリープ",
    "スクリーンショット",
    "スクショ",
    "タッチパネル",
    "トラックパッド",
    "タッチパッド",
    "スペースキー",
    "エンターキー",
    "タブキー",
    "シフトキー",
    "コントロールキー",
    "ファンクションキー",
    "テンキー",
    "タブ",
    "ウィンドウ",
    "ブックマーク",
    "キャッシュ",
    "クッキー",
    "アドレスバー",
    "ハイパーリンク",
    "リンク",
    "ドメイン",
    "ホスティング",
    "フォルダー",
    "フォルダ",
    "ファイル",
    "ツールバー",
    "タスクバー",
    "メニューバー",
    "メニュー",
    "ポップアップ",
    "ダイアログ",
    "ボタン",
    "フォーム",
    "テキストボックス",
    "チェックボックス",
    "ラジオボタン",
    "プルダウン",
    "ドロップダウン",
    "スライダー",
    "スクロールバー",
    "プログレスバー",
    "インジケーター",
    "カーソル",
    "ポインター",
    "フォント",
    "テキストエディタ",
    "エディタ",
    "ビューア",
    "プレイヤー",
    "プラグイン",
    "アドオン",
    "インストーラー",
    "アンインストーラー",
    "ライセンス",
    "フリーソフト",
    "シェアウェア",
    "オープンソース",
    "プログラミング",
    "コーディング",
    "アルゴリズム",
    "データベース",
    "スクリプト",
    "コマンド",
    "ターミナル",
    "コンソール",
    "ログ",
    "エラーログ",
    "デバッグ",
    "バグ",
    # ネットワーク・通信・セキュリティ
    "ワイファイ",
    "ブルートゥース",
    "ネットワーク",
    "インターネット",
    "イントラネット",
    "アクセスポイント",
    "ホットスポット",
    "テザリング",
    "ギガ",
    "パケット",
    "ストリーミング",
    "サブスク",
    "サブスクリプション",
    "セキュリティ",
    "アンチウイルス",
    "ファイアウォール",
    "マルウェア",
    "スパイウェア",
    "フィッシング",
    "プロキシ",
    "ゲートウェイ",
    "イーサネット",
    "ブロードバンド",
    "ファイバー",
    # ゲーム・エンタメ・ホビー
    "コントローラー",
    "ゲームパッド",
    "ジョイスティック",
    "セーブデータ",
    "マルチプレイ",
    "シングルプレイ",
    "オンラインプレイ",
    "ランク",
    "ランキング",
    "スコア",
    "ハイスコア",
    "コンボ",
    "ダメージ",
    "ヒットポイント",
    "マジックポイント",
    "スタミナ",
    "ゲージ",
    "ボス",
    "ラスボス",
    "ダンジョン",
    "マップ",
    "ステージ",
    "エリア",
    "フィールド",
    "クエスト",
    "ミッション",
    "イベント",
    "ガチャ",
    "アイテム",
    "インベントリ",
    "スロット",
    "スキル",
    "アビリティ",
    "レベルアップ",
    "チュートリアル",
    "オープニング",
    "エンディング",
    "バッドエンド",
    "グッドエンド",
    "トゥルーエンド",
    "リスポーン",
    "リトライ",
    "ゲームオーバー",
    "コンティニュー",
    "チェックポイント",
    "ムービー",
    "カットシーン",
    "グラフィック",
    "サウンド",
    "エフェクト",
    "アニメーション",
    "キャラクター",
    "アバター",
    "スキン",
    "コスチューム",
    "エモート",
    "モーション",
    "モブ",
    "ギルド",
    "クラン",
    "パーティー",
    "チーム",
    "マッチング",
    "ロビー",
    "レイド",
    "クラフト",
    "ビルド",
    "カスタマイズ",
    # レア度・強調接頭辞・ゲーム的表現
    "ノーマル",
    "レア",
    "スーパーレア",
    "ウルトラ",
    "ウルトラレア",
    "ハイパー",
    "メガ",
    "マスター",
    "アルティメット",
    "エクストラ",
    "プレミアム",
    "シークレット",
    # 音楽・音響・映像・映画・メディア
    "ミュージック",
    "ソング",
    "ボーカル",
    "コーラス",
    "メロディ",
    "リズム",
    "ビート",
    "テンポ",
    "サビ",
    "イントロ",
    "アウトロ",
    "インスト",
    "インストゥルメンタル",
    "ギター",
    "アコースティックギター",
    "エレキギター",
    "ベースギター",
    "ドラム",
    "ドラムセット",
    "ピアノ",
    "グランドピアノ",
    "シンセサイザー",
    "オルガン",
    "バイオリン",
    "チェロ",
    "トランペット",
    "サックス",
    "トロンボーン",
    "フルート",
    "クラリネット",
    "ハーモニカ",
    "ウクレレ",
    "アンプ",
    "エフェクター",
    "チューナー",
    "メトロノーム",
    "アルバム",
    "シングル",
    "ミニアルバム",
    "ベストアルバム",
    "サウンドトラック",
    "トラック",
    "プレイリスト",
    "ジャケット",
    "ライブ",
    "コンサート",
    "フェス",
    "ツアー",
    "チケット",
    "シネマ",
    "シアター",
    "スクリーン",
    "フィルム",
    "ドラマ",
    "バラエティ",
    "ドキュメンタリー",
    "ニュース",
    "エピソード",
    "シーズン",
    "シリーズ",
    "キャスト",
    "スタッフ",
    "プロデューサー",
    "ディレクター",
    "シナリオ",
    "ストーリー",
    "プロット",
    "クライマックス",
    "トレーラー",
    "ティーザー",
    # 日常会話・クイズ・ゲーム・システム基盤
    "ヒント",
    "クイズ",
    "パズル",
    "ルール",
    "アンサー",
    "スタート",
    "ゴール",
    "クリア",
    "レベル",
    "ポイント",
    "パターン",
    "タイプ",
    "スタイル",
    "イメージ",
    "モデル",
    "システム",
    "データ",
    "コメント",
    "リプライ",
    "スタンプ",
    "リアクション",
    "タイミング",
    "バランス",
    "メリット",
    "デメリット",
    "ジャンル",
    "テーマ",
    "ワード",
    "テキスト",
    "ナンバー",
    "リスト",
    "セット",
    "ベース",
    "メイン",
    "サブ",
    # コンビニ・外食・流通チェーン
    "セブンイレブン",
    "ファミリーマート",
    "ローソンストア",
    "ミニストップ",
    "デイリーヤマザキ",
    "マクドナルド",
    "スターバックス",
    "モスバーガー",
    "ケンタッキー",
    "ミスタードーナツ",
    "イトーヨーカドー",
    "イオンモール",
    "ドンキホーテ",
    "ヨドバシカメラ",
    "ビックカメラ",
    # 飲食・料理・食材・調味料・カフェ・お菓子
    "カレー",
    "カレーライス",
    "シチュー",
    "ホワイトシチュー",
    "ビーフシチュー",
    "ハヤシライス",
    "パスタ",
    "スパゲッティ",
    "マカロニ",
    "ペンネ",
    "ラザニア",
    "ピザ",
    "ピッツァ",
    "ハンバーガー",
    "チーズバーガー",
    "サンドイッチ",
    "ホットドッグ",
    "トースト",
    "オムライス",
    "オムレツ",
    "グラタン",
    "ドリア",
    "リゾット",
    "ステーキ",
    "ハンバーグ",
    "ローストビーフ",
    "コロッケ",
    "メンチカツ",
    "エビフライ",
    "フライドチキン",
    "フライドポテト",
    "チキンナゲット",
    "ナゲット",
    "ソーセージ",
    "ウインナー",
    "ハム",
    "ベーコン",
    "サラミ",
    "ローストチキン",
    "バーベキュー",
    "サラダ",
    "ポテトサラダ",
    "マカロニサラダ",
    "シーザーサラダ",
    "スープ",
    "コーンスープ",
    "ポタージュ",
    "コンソメスープ",
    "ミネストローネ",
    "チャウダー",
    "クラムチャウダー",
    "フォンデュ",
    "チーズフォンデュ",
    "バイキング",
    "ビュッフェ",
    "ケーキ",
    "ショートケーキ",
    "チーズケーキ",
    "ロールケーキ",
    "モンブラン",
    "タルト",
    "アップルパイ",
    "パイ",
    "プリン",
    "ゼリー",
    "アイスクリーム",
    "シャーベット",
    "ジェラート",
    "パフェ",
    "サンデー",
    "ビスケット",
    "クラッカー",
    "キャンディ",
    "グミ",
    "キャラメル",
    "マシュマロ",
    "ポップコーン",
    "ポテトチップス",
    "スナック",
    "ドーナツ",
    "ワッフル",
    "マフィン",
    "スコーン",
    "ティラミス",
    "マカロン",
    "バウムクーヘン",
    "チョコレート",
    "チョコ",
    "ホワイトチョコ",
    "ビターチョコ",
    "バニラ",
    "ストロベリー",
    "ブルーベリー",
    "ラズベリー",
    "マンゴー",
    "バナナ",
    "パイナップル",
    "オレンジ",
    "グレープフルーツ",
    "レモン",
    "ライム",
    "ピーチ",
    "メロン",
    "スイカ",
    "カステラ",
    "シュークリーム",
    "パンケーキ",
    "クレープ",
    "コーヒー",
    "アイスコーヒー",
    "ホットコーヒー",
    "カフェオレ",
    "アメリカンコーヒー",
    "ブラックコーヒー",
    "ブレンドコーヒー",
    "カプチーノ",
    "カフェラテ",
    "エスプレッソ",
    "ティー",
    "アイスティー",
    "ストレートティー",
    "レモンティー",
    "ミルクティー",
    "ハーブティー",
    "ジャスミンティー",
    "ジュース",
    "オレンジジュース",
    "アップルジュース",
    "グレープジュース",
    "トマトジュース",
    "コーラ",
    "サイダー",
    "ソーダ",
    "ジンジャーエール",
    "カルピス",
    "スムージー",
    "シェイク",
    "ココア",
    "ホットココア",
    "ミネラルウォーター",
    "ウォーター",
    "ビール",
    "クラフトビール",
    "ワイン",
    "スパークリングワイン",
    "シャンパン",
    "ウイスキー",
    "ハイボール",
    "ブランデー",
    "ウォッカ",
    "ジン",
    "ラム",
    "テキーラ",
    "カクテル",
    "サワー",
    "レモンサワー",
    "チューハイ",
    "リキュール",
    "マヨネーズ",
    "ケチャップ",
    "マスタード",
    "ソース",
    "ウスターソース",
    "ドレッシング",
    "バター",
    "マーガリン",
    "チーズ",
    "プロセスチーズ",
    "ナチュラルチーズ",
    "モッツァレラ",
    "パルメザン",
    "チェダー",
    "ヨーグルト",
    "ミルク",
    "ホイップクリーム",
    "シロップ",
    "メープルシロップ",
    "ハチミツ",
    "ジャム",
    "イチゴジャム",
    "マーマレード",
    "オリーブオイル",
    "スパイス",
    "ハーブ",
    "ペッパー",
    "ブラックペッパー",
    "タバスコ",
    "バジル",
    "オレガノ",
    "パセリ",
    "シナモン",
    "ナツメグ",
    # 生活・住まい・家具・家電・日用品
    "エアコン",
    "クーラー",
    "ヒーター",
    "ストーブ",
    "ファンヒーター",
    "オイルヒーター",
    "シャワー",
    "バスタブ",
    "トイレ",
    "キッチン",
    "システムキッチン",
    "テーブル",
    "ダイニングテーブル",
    "チェア",
    "デスク",
    "ソファー",
    "ベッド",
    "マットレス",
    "シーツ",
    "カーテン",
    "クローゼット",
    "ハンガー",
    "タオル",
    "バスタオル",
    "フェイスタオル",
    "ブランケット",
    "クッション",
    "カーペット",
    "ラグ",
    "マット",
    "ランプ",
    "ライト",
    "スタンドライト",
    "シーリングライト",
    "シャンデリア",
    "ランタン",
    "スリッパ",
    "ルームシューズ",
    "シャンプー",
    "リンス",
    "トリートメント",
    "コンディショナー",
    "ボディーソープ",
    "ハンドソープ",
    "スキンケア",
    "ローション",
    "クリーム",
    "ハンドクリーム",
    "ボディークリーム",
    "リップクリーム",
    "リップ",
    "パック",
    "フェイスパック",
    "マスク",
    "ティッシュ",
    "ティッシュペーパー",
    "トイレットペーパー",
    "ウェットティッシュ",
    "プラスチック",
    "ビニール",
    "ダンボール",
    "カレンダー",
    "ボールペン",
    "サインペン",
    "マーカー",
    "シャープペン",
    "シャーペン",
    "ノート",
    "ルーズリーフ",
    "メモ",
    "テープ",
    "セロハンテープ",
    "ガムテープ",
    "マスキングテープ",
    "ホッチキス",
    "バインダー",
    "クリップ",
    "ペーパークリップ",
    "ハサミ",
    "カッター",
    "カッターナイフ",
    "ルーラー",
    # ファッション・衣料・バッグ・靴・アクセサリー
    "バッグ",
    "トートバッグ",
    "ショルダーバッグ",
    "リュック",
    "リュックサック",
    "バックパック",
    "ハンドバッグ",
    "クラッチバッグ",
    "エコバッグ",
    "スーツケース",
    "キャリーケース",
    "キャリーバッグ",
    "ポーチ",
    "ウォレット",
    "コインケース",
    "パスケース",
    "キーケース",
    "キーホルダー",
    "メガネ",
    "サングラス",
    "コンタクトレンズ",
    "スニーカー",
    "サンダル",
    "ミュール",
    "ブーツ",
    "ショートブーツ",
    "ロングブーツ",
    "パンプス",
    "ヒール",
    "ローファー",
    "スリッポン",
    "ソックス",
    "タイツ",
    "ストッキング",
    "レギンス",
    "インナー",
    "シャツ",
    "ワイシャツ",
    "Tシャツ",
    "ティーシャツ",
    "カットソー",
    "ポロシャツ",
    "タンクトップ",
    "キャミソール",
    "パーカー",
    "スウェット",
    "トレーナー",
    "セーター",
    "ニット",
    "カーディガン",
    "ベスト",
    "テーラードジャケット",
    "ブルゾン",
    "ジャンパー",
    "コート",
    "トレンチコート",
    "ダッフルコート",
    "チェスターコート",
    "ダウンジャケット",
    "ダウンコート",
    "ダウン",
    "スーツ",
    "セットアップ",
    "ネクタイ",
    "ボウタイ",
    "ベルト",
    "マフラー",
    "ストール",
    "ショール",
    "グローブ",
    "キャップ",
    "ハット",
    "アクセサリー",
    "ジュエリー",
    "ネックレス",
    "ペンダント",
    "ピアス",
    "イヤリング",
    "イヤーカフ",
    "リング",
    "ブレスレット",
    "バングル",
    "アンクレット",
    "ブローチ",
    "コサージュ",
    "ウォッチ",
    "スマートウォッチ",
    # スポーツ・アウトドア・フィットネス・レジャー
    "サッカー",
    "フットサル",
    "ベースボール",
    "バスケットボール",
    "バスケ",
    "バレーボール",
    "バレー",
    "ビーチバレー",
    "テニス",
    "ソフトテニス",
    "バドミントン",
    "ピンポン",
    "ゴルフ",
    "ボウリング",
    "ビリヤード",
    "ダーツ",
    "マラソン",
    "ハーフマラソン",
    "ジョギング",
    "ウォーキング",
    "ランニング",
    "トレーニング",
    "フィットネス",
    "ジム",
    "スポーツジム",
    "フィットネスクラブ",
    "ワークアウト",
    "ストレッチ",
    "ヨガ",
    "ホットヨガ",
    "ピラティス",
    "エアロビクス",
    "ダンベル",
    "バーベル",
    "ランニングマシン",
    "エアロバイク",
    "プロテイン",
    "サプリ",
    "サプリメント",
    "ハイキング",
    "トレッキング",
    "クライミング",
    "ボルダリング",
    "キャンプ",
    "キャンピングカー",
    "グランピング",
    "スキー",
    "スノーボード",
    "スノボ",
    "スケート",
    "アイススケート",
    "ローラースケート",
    "スケートボード",
    "スケボー",
    "サーフィン",
    "ボディボード",
    "ダイビング",
    "スキューバダイビング",
    "シュノーケリング",
    "プール",
    "スイミング",
    "ラフティング",
    "カヌー",
    "カヤック",
    # ビジネス・オフィス・社会・金融・店舗・交通
    "オフィス",
    "リモートワーク",
    "テレワーク",
    "コワーキングスペース",
    "シェアオフィス",
    "レジュメ",
    "ドキュメント",
    "レポート",
    "タスク",
    "プロジェクト",
    "マネジメント",
    "マネージャー",
    "リーダー",
    "チーフ",
    "エグゼクティブ",
    "クライアント",
    "カスタマー",
    "センター",
    "コールセンター",
    "ヘルプデスク",
    "マニュアル",
    "ハンドブック",
    "ガイドライン",
    "コンプライアンス",
    "ガバナンス",
    "コスト",
    "パフォーマンス",
    "コストパフォーマンス",
    "コスパ",
    "タイムパフォーマンス",
    "タイパ",
    "バジェット",
    "マーケティング",
    "セールス",
    "プロモーション",
    "キャンペーン",
    "ディスカウント",
    "セール",
    "バーゲン",
    "クリアランスセール",
    "クーポン",
    "ポイントカード",
    "クレジット",
    "クレジットカード",
    "キャッシュレス",
    "デビットカード",
    "プリペイドカード",
    "レジ",
    "セルフレジ",
    "レシート",
    "コンビニ",
    "スーパー",
    "スーパーマーケット",
    "デパート",
    "ショッピング",
    "ショッピングモール",
    "モール",
    "ブティック",
    "アウトレット",
    "ドラッグストア",
    "タクシー",
    "バス",
    "リムジンバス",
    "シャトルバス",
    "メトロ",
    "モノレール",
    "ロープウェイ",
    "フェリー",
    "クルーズ",
    "エアポート",
    "フライト",
    "ジェット",
    "チェックイン",
    "ホテル",
    "リゾートホテル",
    "ビジネスホテル",
    "カプセルホテル",
    "ホステル",
    "ゲストハウス",
    "フロント",
    "チェックアウト",
    "ルームサービス",
    # 一般概念・抽象名詞・フォーマット・デザイン・属性
    "カテゴリ",
    "カテゴリー",
    "タイトル",
    "コンテンツ",
    "フォーマット",
    "オプション",
    "カスタム",
    "バージョン",
    "エディション",
    "スタンダード",
    "プレミアム",
    "プロフェッショナル",
    "ベーシック",
    "アドバンス",
    "シンプル",
    "スマート",
    "モダン",
    "レトロ",
    "クラシック",
    "ユニーク",
    "オリジナル",
    "クローン",
    "ダミー",
    "デフォルト",
    "ランダム",
    "シャッフル",
    "ソート",
    "フィルター",
    "サーチ",
    "カウント",
    "トータル",
    "サマリー",
    "プレビュー",
    "レビュー",
    "フィードバック",
    "クオリティ",
    "ボリューム",
    "キャパシティ",
    "スケール",
    "スコープ",
    "プロセス",
    "メソッド",
    "カセットテープ",
    "コンパクトディスク",
    # 一般的な物理・計量単位（単体での未知語・スラング誤認防止）
    "ボルト",
    "アンペア",
    "ワット",
    "ヘルツ",
    "オーム",
    "メートル",
    "キロメートル",
    "センチメートル",
    "ミリメートル",
    "グラム",
    "キログラム",
    "ミリグラム",
    "リットル",
    "ミリリットル",
    "ジュール",
    "カロリー",
    "キロカロリー",
    "デシベル",
    "ルーメン",
    "ルクス",
    "パスカル",
    # 日常会話の感情・口語カタカナ表記
    "ナイショ",
    "ヒミツ",
    "ホント",
    "ウソ",
    "ダメ",
    "ムリ",
    "イヤ",
    "スゴイ",
    "カワイイ",
    "キレイ",
    "ヤバイ",
    "マジ",
    "ヨロシク",
    "オネガイ",
    "ゴメン",
    "アリガトウ",
}

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_YEAR_OR_DATE_RE = re.compile(
    r"(?:19|20)\d{2}年?|(?:19|20)\d{2}|[01]?\d/[0-3]?\d|[01]?\d月[0-3]?\d日|[0-3]?\d日|[01]?\d:[0-5]\d"
)
_NUMBER_UNIT_TERM_RE = re.compile(
    r"(?<![A-Za-z0-9ァ-ヶー])"
    r"(?:"
    r"\d+(?:\.\d+)?(?:[万億兆千百])?(?:[ァ-ヶー]{2,}|[A-Za-z]+(?:/[A-Za-z]+)?|℃|℉|度|倍|馬力|画素|円)"
    r"|"
    r"[〇一二三四五六七八九十百千万億兆]{2,}(?:[ァ-ヶー]{2,}|[A-Za-z]+(?:/[A-Za-z]+)?|℃|℉|度|倍|馬力|画素|円)"
    r")"
    r"(?![A-Za-z0-9ァ-ヶー])"
)
_ASCII_TERM_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+._/-]{2,}\b")
_KATAKANA_TERM_RE = re.compile(r"[ァ-ヶー]{3,}")
_KANJI_TERM_RE = re.compile(r"[一-龥]{2,12}")
_QUERY_TERM_RE = re.compile(r"[A-Za-z0-9+._/-]{2,}|[ァ-ヶー]{2,}|[一-龥]{2,}")
_BROWSER_BOILERPLATE_PATTERNS = (
    "cookie",
    "クッキー",
    "プライバシー",
    "利用規約",
    "ログイン",
    "会員登録",
    "メニュー",
    "ナビゲーション",
    "広告",
    "スポンサー",
    "共有",
    "シェア",
    "フォロー",
)

# ---------------------------------------------------------------------------
# Deep-dive followup detection constants
# ---------------------------------------------------------------------------

# img_top5 結果への深掘りをトリガーするキーワード
_IMG_DEEPDIVE_FOLLOWUP_KEYWORDS: tuple[str, ...] = (
    "その中で", "その中の", "その中から", "その中",
    "もっと詳しく", "詳しく教えて", "詳しく",
    "一番上", "一番目", "一番", "1番目", "1番", "1位",
    "2番目", "2番", "二番目", "二番", "2位",
    "3番目", "3番", "三番目", "三番", "3位",
    "4番目", "4番", "四番目", "四番", "4位",
    "5番目", "5番", "五番目", "五番", "5位",
    "読んで", "見て", "スレ読んで", "内容教えて", "スレの中身",
    "面白そうなの", "面白いの", "面白そうな", "気になるの",
    "教えろ", "教えてくれ", "見せて", "見せろ",
    "何があった", "何があったのか", "何が起きた", "どういうこと", "何の話", "どういう話", "kwsk",
    "気になる", "気に入った", "それ",
)

# x_timeline 結果への深掘りをトリガーするキーワード
_X_DEEPDIVE_FOLLOWUP_KEYWORDS: tuple[str, ...] = (
    "その中で", "その中の", "その中から", "それについて",
    "もっと詳しく", "詳しく",
    "どれが", "何が", "一番", "気になる", "面白い", "面白そう",
    "教えろ", "教えて", "見せて",
    "何があった", "何があったのか", "何が起きた", "どういうこと", "何の話", "どういう話", "kwsk",
)

# 深掘りキャッシュの有効期限（秒）
_DEEPDIVE_CACHE_TTL_SEC: float = 180.0

# 序数→インデックスのマッピング
_ORDINAL_TO_INDEX: dict[str, int] = {
    "一番目": 0, "一番": 0, "1番目": 0, "1番": 0, "1位": 0, "上から1": 0,
    "二番目": 1, "二番": 1, "2番目": 1, "2番": 1, "2位": 1,
    "三番目": 2, "三番": 2, "3番目": 2, "3番": 2, "3位": 2,
    "四番目": 3, "四番": 3, "4番目": 3, "4番": 3, "4位": 3,
    "五番目": 4, "五番": 4, "5番目": 4, "5番": 4, "5位": 4,
    "一番上": 0,
}

# ---------------------------------------------------------------------------
# LLM schemas and prompts
# ---------------------------------------------------------------------------

_BROWSER_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string", "maxLength": 80},
            "minItems": 1,
            "maxItems": 3,
        },
        "query": {"type": "string", "maxLength": 80},
        "open_top_n": {"type": "integer"},
        "recency_priority": {"type": "boolean"},
    },
    "required": ["queries", "open_top_n", "recency_priority"],
    "additionalProperties": False,
}
_BROWSER_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "confidence": {"type": "number"},
        "insufficient": {"type": "boolean"},
        "insufficiency_reason": {
            "type": "string",
            "maxLength": 60,
            "description": "情報不足時のみ60文字以内で簡潔に記述。insufficient=falseの場合は空文字",
        },
    },
    "required": ["summary", "confidence", "insufficient", "insufficiency_reason"],
    "additionalProperties": False,
}
_BROWSER_QUERY_SYSTEM_PROMPT = (
    "あなたはWeb調査用の高精度検索語生成器です。"
    "ユーザーの質問文と補助情報から、検索エンジンに投げる短い検索クエリのリスト（queries）を作ってください。\n"
    "【主張・要素分解（Claim Decomposition / Query Fan-out）】\n"
    "ユーザー入力に複数の観点（前提や時期、動機や理由・背景、特有の呼称やスラング・エピソード）が含まれる場合、1つのクエリに詰め込まず、役割の異なる2〜3個のクエリに展開（Query Fan-out）してください。\n"
    "  - クエリ例1（前提・時期・基本設定）: [対象名] [時期・期数・基本情報]\n"
    "  - クエリ例2（動機・理由・因果関係）: [対象名] [なぜ・理由・背景]\n"
    "  - クエリ例3（スラング・固有表現）: [対象名] \"[スラング・特徴的フレーズ]\"\n"
    "【スラング・固有表記の保持ルール】\n"
    "質問の核となっている俗語、空耳、ネットミーム、カギ括弧等の特有表現は、勝手に標準語へ言い換えたり削除したりせず、必ず少なくとも1つのクエリに原文のまま含めてください。\n"
    "【話者名・補助文脈ラベルの混入禁止（最重要）】\n"
    "「補助文脈」に含まれる会話参加者の表示名・発言者ラベル（「Assistant」や行頭の「話者名:」）やプロンプト内の説明語句・指示語は、「ユーザー質問」本体に明記されていない限り、検索クエリに絶対に含めてはなりません（汚染防止）。\n"
    "【時事・最新情報】\n"
    "最新情報やニュース、近況を調べる際は、提示された現在日時コンテキストの現在年号（2026年）をクエリに明示的にバインドし、古い情報の混入を防いでください。\n"
    "出力は必ずJSON形式（queries, open_top_n, recency_priority）だけにしてください。"
)
_BROWSER_SUMMARY_SYSTEM_PROMPT = (
    "あなたはWeb調査の観測メモ要約器です。"
    "複数ページから抜いた本文と検索抜粋を精査し、後段の会話モデルが事実に基づいて正確に回答できるよう、詳細かつ構造的な調査メモ（summary）を作成してください。\n"
    "提示された現在日時コンテキストの現在年（2026年等）を未来の架空日付と誤認せず、現在の有効な事実として扱ってください。\n"
    "単なる1文のあらすじに丸めず、以下の要素を具体名を落とさずに含めてください:\n"
    "1. 時期・作品名・登場枠などの前提事実\n"
    "2. 行動や事象の理由・経緯・段階的な因果関係\n"
    "3. 話題となった特有のエピソード、俗称・空耳・ネットスラングの真偽や位置づけ\n"
    "事実の断定は観測できた範囲に限り、情報が不足している場合のみ insufficient=true とし、不足理由（insufficiency_reason）を60文字以内で簡潔に記述してください（insufficient=false の場合は必ず空文字 \"\" にしてください）。\n"
    "出力は必ずJSON形式（summary, confidence, insufficient, insufficiency_reason）だけにしてください。"
)

# ---------------------------------------------------------------------------
# MCP tool allow/block lists
# ---------------------------------------------------------------------------

_DEFAULT_ALLOWED_MCP_TOOLS = (
    "chrome_navigate",
    "chrome_get_web_content",
    "chrome_read_page",
    "chrome_close_tabs",
    "chrome_get_page_metadata",
    "chrome_get_page_title",
    "chrome_screenshot",
    "chrome_capture_screenshot",
    "chrome_take_screenshot",
)
_DEFAULT_BLOCKED_MCP_TOOL_KEYWORDS = (
    "click",
    "press",
    "type",
    "fill",
    "submit",
    "download",
    "upload",
    "delete",
    "remove",
    "purchase",
    "buy",
    "checkout",
    "cart",
    "payment",
)
_DEFAULT_ALLOWED_URL_SCHEMES = {"http", "https"}

# ---------------------------------------------------------------------------
# Numeric defaults
# ---------------------------------------------------------------------------

_DEFAULT_MAX_RESULTS_HARD_LIMIT = 6
_DEFAULT_MAX_CRAWL_URLS = 4
_DEFAULT_BROWSER_PAGES_HARD_LIMIT = 5
_DEFAULT_BROWSER_CONTENT_CHARS_HARD_LIMIT = 2400
_DEFAULT_WEB_RESEARCH_SUMMARY_NUM_PREDICT = 768
_DEFAULT_BROWSER_LOCK_PATH = "/tmp/discord-ai-bot-browser.lock"
_DEFAULT_MCP_SESSION_PATH = "/tmp/discord-ai-bot-chrome-mcp-session"
_DEFAULT_KNOWN_TERM_CACHE_TTL_SEC = 12 * 60 * 60
_DEFAULT_KNOWN_TERM_CACHE_MAX_SIZE = 256
_DEFAULT_GOOGLE_SEARCH_URL_TEMPLATE = "https://www.google.com/search?q={query}&hl=ja"
_DEFAULT_DUCKDUCKGO_SEARCH_URL_TEMPLATE = "https://duckduckgo.com/html/?q={query}&kl=jp-jp"
_DEFAULT_SEARCH_URL_TEMPLATE = _DEFAULT_DUCKDUCKGO_SEARCH_URL_TEMPLATE
_DEFAULT_X_SEARCH_URL_TEMPLATE = "https://x.com/search?q={query}&f=live"
_DEFAULT_X_SEARCH_TOP_URL_TEMPLATE = "https://x.com/search?q={query}"
_DEFAULT_BROWSER_POST_NAVIGATION_DELAY_SEC = 0.75
_DEFAULT_BROWSER_DYNAMIC_SITE_DELAY_SEC = 3.0
_DEFAULT_BROWSER_READ_RETRIES = 3
_DEFAULT_BROWSER_READ_RETRY_DELAY_SEC = 1.0
_DEFAULT_X_TIMELINE_LIMIT = 10  # overridden after xcom helper load
_DEFAULT_X_TIMELINE_HARD_LIMIT = 20
_DEFAULT_X_TIMELINE_EXTRA_WAIT_SEC = 3.0

# NOTE: _DEFAULT_DYNAMIC_BROWSER_HOSTS is set in __init__.py after loading the xcom helper,
# because it depends on the helper's DEFAULT_DYNAMIC_BROWSER_HOSTS attribute.


# ---------------------------------------------------------------------------
# Config getter functions
# ---------------------------------------------------------------------------

def _cfg_csv_values(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = cfg(name, default)
    if isinstance(raw, str):
        values = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        values = [str(item or "").strip() for item in raw]
    else:
        values = [str(item or "").strip() for item in default]
    return tuple(value for value in values if value)


def _browser_allowed_tool_names() -> set[str]:
    return {
        str(name or "").strip()
        for name in _cfg_csv_values("WEB_RESEARCH_MCP_ALLOWED_TOOLS", _DEFAULT_ALLOWED_MCP_TOOLS)
        if str(name or "").strip()
    }


def _browser_blocked_tool_keywords() -> tuple[str, ...]:
    return tuple(
        keyword.lower()
        for keyword in _cfg_csv_values(
            "WEB_RESEARCH_MCP_BLOCKED_TOOL_KEYWORDS",
            _DEFAULT_BLOCKED_MCP_TOOL_KEYWORDS,
        )
        if str(keyword or "").strip()
    )


def _browser_debug_enabled() -> bool:
    return cfg_bool("WEB_RESEARCH_BROWSER_DEBUG", False)


def _browser_debug_preview_chars() -> int:
    return max(cfg_int("WEB_RESEARCH_BROWSER_DEBUG_PREVIEW_CHARS", 0), 0)


def _safe_result_limit(requested: int) -> int:
    hard_limit = max(cfg_int("WEB_RESEARCH_MAX_RESULTS_HARD_LIMIT", _DEFAULT_MAX_RESULTS_HARD_LIMIT), 1)
    return max(1, min(int(requested or 1), hard_limit))


def _safe_browser_page_limit(requested: int) -> int:
    hard_limit = max(cfg_int("WEB_RESEARCH_BROWSER_MAX_PAGES_HARD_LIMIT", _DEFAULT_BROWSER_PAGES_HARD_LIMIT), 1)
    return max(1, min(int(requested or 1), hard_limit))


def _safe_browser_content_char_limit(requested: int) -> int:
    hard_limit = max(
        cfg_int(
            "WEB_RESEARCH_BROWSER_MAX_CONTENT_CHARS_HARD_LIMIT",
            _DEFAULT_BROWSER_CONTENT_CHARS_HARD_LIMIT,
        ),
        400,
    )
    return max(200, min(int(requested or 200), hard_limit))


def _safe_x_timeline_limit(requested: int | None = None) -> int:
    default_limit = max(cfg_int("WEB_RESEARCH_X_TIMELINE_DEFAULT_POSTS", _DEFAULT_X_TIMELINE_LIMIT), 1)
    hard_limit = max(cfg_int("WEB_RESEARCH_X_TIMELINE_MAX_POSTS", _DEFAULT_X_TIMELINE_HARD_LIMIT), 1)
    value = default_limit if requested is None else int(requested or default_limit)
    return max(1, min(value, hard_limit))


def _x_timeline_extra_wait_sec() -> float:
    return min(
        max(cfg_float("WEB_RESEARCH_X_TIMELINE_EXTRA_WAIT_SEC", _DEFAULT_X_TIMELINE_EXTRA_WAIT_SEC), 0.0),
        15.0,
    )


def _browser_read_retries() -> int:
    return max(cfg_int("WEB_RESEARCH_BROWSER_READ_RETRIES", _DEFAULT_BROWSER_READ_RETRIES), 1)


def _browser_read_retry_delay_sec() -> float:
    return max(cfg_float("WEB_RESEARCH_BROWSER_READ_RETRY_DELAY_SEC", _DEFAULT_BROWSER_READ_RETRY_DELAY_SEC), 0.0)


def _known_term_cache_ttl_sec() -> float:
    return max(cfg_float("WEB_RESEARCH_KNOWN_TERM_CACHE_TTL_SEC", _DEFAULT_KNOWN_TERM_CACHE_TTL_SEC), 60.0)


def _known_term_cache_max_size() -> int:
    return max(cfg_int("WEB_RESEARCH_KNOWN_TERM_CACHE_MAX_SIZE", _DEFAULT_KNOWN_TERM_CACHE_MAX_SIZE), 16)


def _browser_lock_file_path() -> str:
    path = str(cfg("WEB_RESEARCH_BROWSER_LOCK_PATH", _DEFAULT_BROWSER_LOCK_PATH) or "").strip()
    return path or _DEFAULT_BROWSER_LOCK_PATH


def _mcp_session_file_path() -> str:
    path = str(cfg("WEB_RESEARCH_MCP_SESSION_PATH", _DEFAULT_MCP_SESSION_PATH) or "").strip()
    return path or _DEFAULT_MCP_SESSION_PATH


def _browser_dynamic_host_keywords(default_hosts: tuple[str, ...]) -> tuple[str, ...]:
    cfg_hosts = tuple(
        keyword.lower()
        for keyword in _cfg_csv_values("WEB_RESEARCH_BROWSER_DYNAMIC_HOSTS", default_hosts)
        if str(keyword or "").strip()
    )
    return tuple(dict.fromkeys((*[h.lower() for h in default_hosts], *cfg_hosts)))


def _is_dynamic_browser_site(url: str, *, xcom: object | None = None) -> bool:
    """URLが動的ブラウザアクセスを必要とするサイト（X.com、ふたば等）かどうかを判定する。"""
    lowered = str(url or "").lower()
    default_hosts = ("x.com", "twitter.com", "img.2chan.net", "may.2chan.net")
    if xcom is not None:
        dynamic_hosts = _browser_dynamic_host_keywords(
            tuple(getattr(xcom, "DEFAULT_DYNAMIC_BROWSER_HOSTS", default_hosts))
        )
        if hasattr(xcom, "is_dynamic_browser_site"):
            return bool(xcom.is_dynamic_browser_site(url, dynamic_hosts=dynamic_hosts))
    
    dynamic_hosts = _browser_dynamic_host_keywords(default_hosts)
    return any(h in lowered for h in dynamic_hosts)


def _should_use_background_browser_access(url: str, *, xcom: object | None = None) -> bool:
    if not cfg_bool("WEB_RESEARCH_BROWSER_BACKGROUND_NAVIGATION", True):
        return False
    if _is_dynamic_browser_site(url, xcom=xcom) and cfg_bool("WEB_RESEARCH_BROWSER_FOREGROUND_DYNAMIC_SITES", True):
        return False
    return True


def _browser_post_navigation_delay_sec(url: str, *, xcom: object | None = None) -> float:
    default_delay = cfg_float(
        "WEB_RESEARCH_BROWSER_POST_NAVIGATION_DELAY_SEC",
        _DEFAULT_BROWSER_POST_NAVIGATION_DELAY_SEC,
    )
    delay = max(float(default_delay or 0.0), 0.0)
    if _is_dynamic_browser_site(url, xcom=xcom):
        dynamic_delay = cfg_float("WEB_RESEARCH_BROWSER_DYNAMIC_SITE_DELAY_SEC", _DEFAULT_BROWSER_DYNAMIC_SITE_DELAY_SEC)
        delay = max(delay, float(dynamic_delay or 0.0))
    return min(delay, 15.0)


def _browser_search_url_template() -> str:
    template = cfg("WEB_RESEARCH_BROWSER_SEARCH_URL_TEMPLATE", "")
    if template:
        return str(template).strip()
    engine = str(cfg("WEB_RESEARCH_SEARCH_ENGINE", "") or "").strip().lower()
    if engine in {"google", "g"}:
        return _DEFAULT_GOOGLE_SEARCH_URL_TEMPLATE
    if engine in {"duckduckgo", "ddg"}:
        return _DEFAULT_DUCKDUCKGO_SEARCH_URL_TEMPLATE
    return _DEFAULT_SEARCH_URL_TEMPLATE


def _x_search_url_template(*, live: bool = True) -> str:
    template = cfg("WEB_RESEARCH_X_SEARCH_URL_TEMPLATE", "")
    if template:
        return str(template).strip()
    return _DEFAULT_X_SEARCH_URL_TEMPLATE if live else _DEFAULT_X_SEARCH_TOP_URL_TEMPLATE


def _web_research_recency_prefer_x_search() -> bool:
    return cfg_bool("WEB_RESEARCH_RECENCY_PREFER_X_SEARCH", True)

