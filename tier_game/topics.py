"""ティア表パーティゲームのお題生成モジュール。

AIによるリアルタイムお題生成と、安全なフォールバック用プリセットお題集を提供します。
"""
from __future__ import annotations

import asyncio
from collections import deque
import logging
import os
import random
import re
from typing import Any, Final

import aiosqlite

from lib.config_utils import cfg, cfg_bool, cfg_float
from .models import TopicData

log = logging.getLogger("tier_game.topics")

# お題着想で除外する対話傾向・内省・関係性の memory_type
EXCLUDED_MEMORY_TYPES: Final[tuple[str, ...]] = (
    "communication_style",
    "behavioral_principle",
    "persona_reflection",
    "self_belief",
    "agent_belief",
    "relationship",
)

# 下ネタ・性的表現・過激な怨嗟や攻撃的ワードを排除する正規表現（BAN-86）
_SENSITIVE_THEME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:"
    r"下ネタ|エロ|アダルト|ちんこ|まんこ|ちんぽ|おっぱい|セックス|オナニー|自慰|性器|陰茎|膣|射精|風俗|\bAV\b|ポルノ|殺害|自殺|虐待|暴力|ドラッグ|麻薬|覚醒剤|"
    r"チンコ|マンコ|バカマンコ|アホマンコ|クソマンコ|チンポ|ちんちん|チンチン|おまんこ|オマンコ|フェラ|パイズリ|レイプ|"
    r"おちんちん|おちっこ|うんこ|うんち|精液|陰部|性交|巨乳|爆乳|淫乱|18禁|R-?18|変態|"
    r"嫉妬で狂|狂いそう|死ね|殺す|殺意|消えろ|発狂|怨嗟|怨念|呪う"
    r")",
    re.IGNORECASE,
)

# 最近出題したお題のキャッシュ（重複防止用）
_RECENT_TOPIC_CACHE: deque[str] = deque(maxlen=30)

TIER_TOPIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic": {
            "type": "string",
            "description": "個人の好みや価値観・こだわりでS〜Dの評価が分かれる短くキャッチーな疑問形のお題（例: カレーに入れる最強の具材は？）。※完全な自然な日本語のみ。正解のある客観知識クイズや分類当ては厳禁",
        },
        "options": {
            "type": "array",
            "items": {
                "type": "string",
                "description": "誰もが日常で知っている実在の身近な定番一般名詞（例: 『牛肉』『豚肉』『チキン』等）。※数字のみ、架空の造語、マイナーな専門用語、お題そのものの単語は厳禁",
            },
            "minItems": 5,
            "maxItems": 5,
        },
        "category": {
            "type": "string",
            "description": "お題のジャンル（例: グルメ、ライフスタイル、エンタメ、もしも等）。※必ず自然な日本語。中国語や外国語は厳禁",
        },
    },
    "required": ["topic", "options", "category"],
}

# AI生成のお題が主観的な好みの議論に集中するための着想ジャンル集
INSPIRATION_THEMES: Final[list[tuple[str, str]]] = [
    ("【グルメ・食べ物】", "ラーメンの味、寿司のネタ、おにぎりの具、焼肉の部位、冬の鍋料理、コンビニスナック、パンの種類、カレーのトッピングなど"),
    ("【日常・ライフスタイル】", "休日の過ごし方、朝起きて最初にすること、家事の中で好きな作業、部屋のインテリア、お風呂の楽しみ方、睡眠のこだわりなど"),
    ("【エンタメ・趣味】", "ゲームのジャンル、映画のジャンル、漫画の王道展開、RPGの職業、カラオケの定番曲タイプ、休日のインドア趣味など"),
    ("【学校・季節・イベント】", "学校の好きだった教科、文化祭の出し物、夏休みの思い出、四季のイベント、修学旅行の楽しみ、冬の楽しみなど"),
    ("【もしも・空想】", "1つだけ手に入る超能力、無人島に1つ持っていくアイテム、タイムマシンで行きたい時代、魔法が1つ使えるなら、ファンタジー世界の住む場所など"),
    ("【乗り物・旅行・お出かけ】", "旅行で行きたい場所、好きな移動手段、温泉地の楽しみ、遊園地のアトラクション、ドライブで聴きたい音楽の雰囲気など"),
    ("【飲み物・カフェ・スイーツ】", "カフェの定番メニュー、好きなソフトドリンク、アイスクリームのフレーバー、ケーキの種類、お茶の種類、冬の温かい飲み物など"),
    ("【動物・生き物・ペット】", "飼ってみたいペット、動物園で人気の動物、水族館で見たい生き物、猫の可愛い仕草、鳥の種類など"),
    ("【文房具・ガジェット・日用品】", "デスク周りの必須アイテム、便利なスマホアプリの種類、好きな文房具、キッチン家電、カバンの中身の必需品など"),
    ("【スポーツ・アウトドア】", "観戦するのが好きなスポーツ、キャンプでやりたいこと、公園の遊具、筋トレ・運動メニュー、球技の種類など"),
]

# 多彩なジャンルのフォールバックプリセットお題（50種類以上）
PRESET_TOPICS: list[TopicData] = [
    TopicData("カレーに入れるなら？", ["ほうれん草", "生卵", "納豆", "玉ねぎ", "じゃがいも"], "グルメ"),
    TopicData("無人島に1つだけ持っていくなら？", ["サバイバルナイフ", "ライター", "電子書籍リーダー(満充電)", "テント", "調味料セット"], "サバイバル"),
    TopicData("深夜2時に罪悪感と戦いながら食べたい背徳飯", ["カップラーメン", "コンビニのからあげ棒", "冷凍ピザ", "バニラアイス", "ポテトチップス"], "グルメ"),
    TopicData("もし1つだけ超能力を手に入れるなら？", ["瞬間移動", "時間停止", "透明化", "テレパシー(読心術)", "飛行能力"], "もしも"),
    TopicData("映画館で絶対に買いたいフード・ドリンク", ["ポップコーン(塩)", "ポップコーン(キャラメル)", "コーラ", "チュロス", "ホットドッグ"], "エンタメ"),
    TopicData("回転寿司で最初に注文したい皿", ["サーモン", "マグロ(赤身)", "いくら", "えんがわ", "たまご"], "グルメ"),
    TopicData("休日の最高の過ごし方", ["1日中ゲーム・動画鑑賞", "お昼まで二度寝", "温泉・サウナでリフレッシュ", "ショッピング・食べ歩き", "大掃除・模様替え"], "ライフスタイル"),
    TopicData("睡眠の質を最も高めるアイテム", ["オーダーメイド高級枕", "完全遮光カーテン", "アロマディフューザー", "炭酸入浴剤", "ノイズキャンセリング耳栓"], "ライフスタイル"),
    TopicData("ハンバーガーチェーンの最強サイドメニュー", ["フライドポテト", "チキンナゲット", "オニオンリング", "アップルパイ", "ソフトツイスト/シェイク"], "グルメ"),
    TopicData("異世界転生したときの初期職業", ["勇者", "大魔導士", "暗殺者(アサシン)", "聖職者(プリースト)", "鍛冶師/商人"], "ファンタジー"),
    TopicData("ゾンビパンデミックが起きた時の立てこもり場所", ["巨大ショッピングモール", "ホームセンター", "学校の屋上", "離島・孤島", "警察署・交番"], "もしも"),
    TopicData("目玉焼きにかけるなら最強の調味料", ["醤油", "塩コショウ", "ウスターソース", "ケチャップ", "マヨネーズ"], "グルメ"),
    TopicData("引っ越し先を選ぶときに一番妥協できない条件", ["家賃の安さ", "駅徒歩5分以内", "バストイレ別", "防音性(RC構造)", "日当たり・眺望"], "ライフスタイル"),
    TopicData("カラオケで1曲目に入れたい曲のジャンル", ["誰もが知る定番アニソン", "ノリの良い最新J-POP", "90年代〜00年代懐メロ", "テンションの上がるロック", "しっとり系バラード"], "エンタメ"),
    TopicData("居酒屋で席に着いたらとりあえず頼むスピードメニュー", ["枝豆", "たこわさ", "フライドポテト", "冷やしトマト", "きゅうりの一本漬け"], "グルメ"),
    TopicData("子どもの頃、一番憧れた学校行事", ["修学旅行", "文化祭", "運動会/体育祭", "林間学校・野外活動", "合唱コンクール"], "学生時代"),
    TopicData("宝くじで10億円当たったら真っ先にやること", ["会社/学校を辞める", "高級タワーマンションを買う", "世界一周旅行に行く", "全額インデックス投資に回す", "親や友人に豪遊させる"], "もしも"),
    TopicData("お弁当に入っているとテンションが上がるおかず", ["鶏のから揚げ", "卵焼き", "ハンバーグ", "タコさんウインナー", "グラタン/コロッケ"], "グルメ"),
    TopicData("一生1つしか食べられないなら選びたい主食", ["白米", "ラーメン", "うどん/そば", "パスタ", "焼きたてパン"], "グルメ"),
    TopicData("夏に一番食べたい冷たいスイーツ", ["かき氷", "ソフトクリーム", "スイカ", "フルーツパフェ", "ガリガリ君/アイスバー"], "グルメ"),
    TopicData("冬にみんなで囲みたい最強の鍋料理", ["キムチ鍋", "すき焼き", "もつ鍋", "寄せ鍋/水炊き", "豆乳鍋"], "グルメ"),
    TopicData("友達の家にお邪魔したとき、密かに見てしまうポイント", ["部屋の匂い・空気感", "本棚やゲームのラインナップ", "トイレ・水回りの清潔さ", "冷蔵庫の中身", "ゴミ箱の散らかり具合"], "人間関係"),
    TopicData("夏休みの宿題を終わらせるスタイル", ["7月中に全て終わらせる", "計画的に毎日コツコツ進める", "8月最終週に焦って徹夜する", "始業式の朝に友達に見せてもらう", "最初から諦めて出さない"], "学生時代"),
    TopicData("ゲームで一番テンションが上がる瞬間", ["超強い裏ボスを撃破した瞬間", "ガチャで最高レアを一発ツモ", "対人戦で神プレイを決めて逆転", "隠し要素やイースターエッグを発見", "感動のエンディングを迎えた瞬間"], "ゲーム"),
    TopicData("格闘ゲームで一番強いと思う要素", ["圧倒的な近距離ラッシュ力", "近寄らせない鉄壁の飛び道具・牽制", "一撃で戦況をひっくり返す超火力", "見切れない中下段の二択崩し", "無敵技や当身などの切り返し力"], "ゲーム"),
    TopicData("もし過去か未来に行けるなら体験したい時代", ["恐竜が生きていた白亜紀", "戦国時代（信長や秀吉の時代）", "江戸時代の城下町", "100年後の未来世界", "1000年後の超テクノロジー未来"], "もしも"),
    TopicData("疲れた夜に一番癒やされるコンテンツ", ["推しの配信・推し活", "お気に入りの音楽を聴く", "動物のほのぼの動画を見る", "温かいお風呂に長風呂する", "何も考えずにひたすら寝る"], "ライフスタイル"),
    TopicData("最強のサンドイッチの具材", ["たまごサラダ", "ハム＆レタス＆チーズ", "カツサンド(豚カツ)", "BLT(ベーコン・レタス・トマト)", "フルーツ＆生クリーム"], "グルメ"),
    TopicData("RPGでパーティに1人だけ入れるなら欲しい仲間", ["全体回復ができるヒーラー", "どんな攻撃も受け止める重騎士タンク", "広範囲を一掃する大魔術師", "罠解除や盗みができるシーフ", "面白くて場を和ませるマスコットキャラ"], "ファンタジー"),
    TopicData("朝起きて一番最初にしたい行動", ["スマホでSNSや通知のチェック", "コップ1杯の冷たい水を飲む", "熱いシャワーを浴びる", "コーヒーや温かいお茶を淹れる", "あと5分…と二度寝する"], "ライフスタイル"),
    TopicData("無制限におかわり自由なら食べまくりたいもの", ["焼肉(カルビ・牛タン)", "高級寿司(ウニ・大トロ)", "ケーキ・スイーツビュッフェ", "新鮮な牡蠣・海鮮焼き", "本格中華の点心・小籠包"], "グルメ"),
    TopicData("デスゲームに巻き込まれた時、生き残りそうなタイプ", ["冷静沈着な頭脳派インテリ", "圧倒的武力を持つ元軍人/格闘家", "裏切りも辞さない利己的な策略家", "誰にでも優しく人望を集めるリーダー", "一見無害だが実は黒幕のサイコパス"], "もしも"),
    TopicData("人生で最も大切にしたい価値観", ["自由と好奇心", "経済的な豊かさ・安心", "家族や大切な人との絆", "仕事での成功・自己実現", "心身の健康と穏やかな日常"], "人生観"),
    TopicData("自販機で喉が渇いた時に真っ先に選ぶもの", ["冷たいミネラルウォーター", "緑茶/ウーロン茶", "炭酸飲料(コーラやサイダー)", "缶コーヒー(無糖ブラック)", "スポーツドリンク"], "グルメ"),
    TopicData("新幹線や飛行機でのベストな過ごし方", ["駅弁やお菓子を食べながら景色を眺める", "読書や勉強に没頭する", "映画やアニメをダウンロードして観る", "到着までひたすら熟睡する", "同行者とおしゃべりを楽しむ"], "旅行"),
    TopicData("焼肉で一番最初に網に乗せたい肉", ["牛タン(塩タン)", "カルビ", "ロース", "ハラミ", "ホルモン"], "グルメ"),
    TopicData("ラーメンに絶対トッピングしたい具材", ["味付け煮卵", "チャーシュー増量", "のり", "メンマ", "白髪ねぎ"], "グルメ"),
    TopicData("小学生の給食で一番テンションが上がったメニュー", ["揚げパン", "カレーライス", "冷凍みかん", "ソフト麺", "フルーツポンチ/ゼリー"], "学生時代"),
    TopicData("もし神様から1つだけ免除されるなら", ["睡眠（寝なくても常に元気）", "食事（食べなくても飢えない・太らない）", "労働（働かなくても一生暮らせる）", "掃除・洗濯（部屋が常に清潔）", "老化（一生20代の肉体を維持）"], "もしも"),
    TopicData("カラオケで誰かが歌ってくれたら嬉しい曲", ["誰もが盛り上がれるダンスナンバー", "男女デュエット曲", "魂の叫び系アツいロック", "綺麗な高音バラード", "ネタ曲・お笑いソング"], "エンタメ"),
    TopicData("スマホのバッテリーが残り5%の時、一番優先すること", ["連絡相手に『充電切れる』と即送信", "帰宅ルートのマップ・乗換案内を確認", "画面を極限まで暗くして機内モードにする", "モバイルバッテリーを探してコンビニへ走る", "諦めてそのままSNSを見続ける"], "ライフスタイル"),
    TopicData("最強のピザのトッピング組み合わせ", ["王道マルゲリータ(トマト&モッツァレラ)", "クアトロフォルマッジ(4種チーズ&蜂蜜)", "照り焼きチキンマヨネーズ", "シーフード(エビ・イカ・アサリ)", "ペパロニ＆ハラペーニョ"], "グルメ"),
]


def get_fallback_topic() -> TopicData:
    """プリセットから最近出題していないお題をランダムに取得する。"""
    available = [t for t in PRESET_TOPICS if t.topic not in _RECENT_TOPIC_CACHE]
    if not available:
        _RECENT_TOPIC_CACHE.clear()
        available = list(PRESET_TOPICS)

    chosen = random.choice(available)
    _RECENT_TOPIC_CACHE.append(chosen.topic)
    return chosen


async def fetch_random_db_contexts(
    db_path: str | None = None,
    *,
    bot: Any | None = None,
    guild_id: int | None = None,
    channel_id: int | None = None,
    player_user_ids: list[int] | None = None,
    limit: int = 3,
) -> list[str]:
    """AIが使用しているデータベース（MemoryStore / SQLite DB）から記憶・話題・興味をランダムに取得する。
    DBが存在しない場合やデータがない場合は空リストを安全に返す。
    """
    target_path = ""
    if bot is not None:
        try:
            chat_cog = bot.get_cog("OllamaChatCog")
            mem_store = getattr(chat_cog, "memory_store", None)
            if mem_store is not None and getattr(mem_store, "db_path", None):
                target_path = str(mem_store.db_path).strip()
        except Exception as e:
            log.debug("Failed to resolve db_path from bot: %r", e)

    if not target_path:
        from lib.config_utils import resolve_isolated_path
        target_path = db_path or resolve_isolated_path(
            "MEMORY_DB_PATH",
            "/var/lib/ollama-bot/memory_{config_name}.db",
            legacy_fallback_config="config_img",
        )

    if not target_path or not os.path.exists(target_path):
        log.debug("Memory database not found at %r; skipping DB contexts", target_path)
        return []

    results: list[str] = []
    conn = None
    try:
        conn = await aiosqlite.connect(target_path, timeout=5.0)
        conn.row_factory = aiosqlite.Row

        # テーブル情報を取得して memory_type カラムが存在するか判定
        has_memory_type = False
        try:
            cur = await conn.execute("PRAGMA table_info(memories)")
            cols = [r[1] for r in await cur.fetchall()]
            await cur.close()
            has_memory_type = "memory_type" in cols
        except Exception as e:
            log.debug("fetch memories pragma table_info error: %r", e)

        excluded_types_sql = (
            f" AND memory_type NOT IN ({','.join(repr(t) for t in EXCLUDED_MEMORY_TYPES)})"
            if has_memory_type
            else ""
        )

        # 1. 参加プレイヤーに関する記憶があるか確認（指定されている場合）
        if player_user_ids:
            valid_uids = [int(u) for u in player_user_ids if int(u) > 0]
            if valid_uids:
                placeholders = ",".join("?" for _ in valid_uids)
                try:
                    sql = f"""
                        SELECT content FROM memories
                        WHERE user_id IN ({placeholders})
                          AND TRIM(COALESCE(content, '')) <> ''
                          {excluded_types_sql}
                        ORDER BY RANDOM()
                        LIMIT 2
                    """
                    cur = await conn.execute(sql, valid_uids)
                    try:
                        rows = await cur.fetchall()
                        for r in rows:
                            c = str(r["content"]).strip()
                            if c and c not in results:
                                results.append(c)
                    finally:
                        await cur.close()
                except Exception as e:
                    log.debug("fetch user memories error: %r", e)

        # 2. memories テーブルからランダム取得
        needed = limit - len(results)
        if needed > 0:
            where_clauses = ["TRIM(COALESCE(content, '')) <> ''"]
            params: list[Any] = []
            if guild_id is not None:
                where_clauses.append("(guild_id IS NULL OR guild_id = ? OR guild_id = -1)")
                params.append(guild_id)
            if has_memory_type:
                where_clauses.append(
                    f"memory_type NOT IN ({','.join(repr(t) for t in EXCLUDED_MEMORY_TYPES)})"
                )
            try:
                sql = f"""
                    SELECT content FROM memories
                    WHERE {' AND '.join(where_clauses)}
                    ORDER BY RANDOM()
                    LIMIT ?
                """
                params.append(needed * 2)
                cur = await conn.execute(sql, params)
                try:
                    rows = await cur.fetchall()
                    for r in rows:
                        c = str(r["content"]).strip()
                        if c and c not in results:
                            results.append(c)
                            if len(results) >= limit:
                                break
                finally:
                    await cur.close()
            except Exception as e:
                log.debug("fetch random memories error: %r", e)

        # 3. user_interests テーブルがあればトピックもランダムサンプリング
        if len(results) < limit:
            needed = limit - len(results)
            try:
                cur = await conn.execute(
                    "SELECT topic FROM user_interests WHERE TRIM(COALESCE(topic, '')) <> '' ORDER BY RANDOM() LIMIT ?",
                    (needed,),
                )
                try:
                    rows = await cur.fetchall()
                    for r in rows:
                        t = str(r["topic"]).strip()
                        if t and t not in results:
                            results.append(f"興味・関心: {t}")
                finally:
                    await cur.close()
            except Exception as e:
                log.debug("fetch user_interests error: %r", e)

        # 4. memory_episodes テーブルがあればエピソードも
        if len(results) < limit:
            needed = limit - len(results)
            try:
                cur = await conn.execute(
                    "SELECT topic, summary FROM memory_episodes WHERE (TRIM(COALESCE(topic, '')) <> '' OR TRIM(COALESCE(summary, '')) <> '') ORDER BY RANDOM() LIMIT ?",
                    (needed,),
                )
                try:
                    rows = await cur.fetchall()
                    for r in rows:
                        top = str(r["topic"] or "").strip()
                        sm = str(r["summary"] or "").strip()
                        txt = f"{top}: {sm}" if top and sm else (top or sm)
                        if txt and txt not in results:
                            results.append(f"過去のエピソード: {txt}")
                finally:
                    await cur.close()
            except Exception as e:
                log.debug("fetch memory_episodes error: %r", e)
    except Exception as e:
        log.warning("Failed to query memory DB %s: %r", target_path, e)
        return []
    finally:
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass

    return results


def _extract_clean_db_theme(raw_text: Any) -> str | None:
    """DBから取得した記憶テキストから、パーティゲームのお題の着想に適した安全で簡潔な単一テーマを抽出する。
    長文のファクトチェック記録やBot行動指針・感情ルール・内省文は除外またはクレンジングする。
    """
    import re
    if isinstance(raw_text, dict):
        raw_text = raw_text.get("content", "") or raw_text.get("text", "") or ""
    if not isinstance(raw_text, str):
        return None
    text = raw_text.strip()
    if not text:
        return None

    # 0. 下ネタ・性的表現・過激な怨嗟や攻撃的ワードを除外（BAN-86）
    if _SENSITIVE_THEME_RE.search(text):
        return None

    # 述語・文節断片（「〜している」「〜いない」「〜存在する」「〜という呼び方」等）や括弧付き暴言の除外
    if re.search(
        r"(?:許容して|存在す|飲んでい|食べてい|知ってい|思ってい|言ってい|やってい|してい|"
        r"という(?:呼び方|品種|意味|設定|こと|もの|話)|"
        r"まだ[^\s]{1,10}ない|ている$|ていない$|ていた$|する$|ない$|だ$|である$)",
        text,
    ):
        return None

    # 1. Bot行動指針・内省文・メタ発言を除外
    if re.search(r"(?:に対して[、\s]*.*?姿勢で応じる|と呼ぶ|と呼称する|に配慮する|約束を交わした|対応する|呼び捨て|敬称)", text):
        return None

    # 1-1. 対話分析・行動傾向メモ・メタ記述を除外（「ユーザー」「傾向」「称賛」「言葉で感謝」「追加パート」等）
    if any(k in text for k in ("ユーザー", "傾向", "称賛", "言葉で感謝", "追加パート")):
        return None

    # 1-2. 人物の行動傾向・発話スタイルの記述文を除外（例: 「〜を使う」「〜を示す」「〜は...傾向」）
    if re.search(r"(?:を使う|を示す|を見せる|を好む|に関心|について言及)", text):
        return None
    if re.search(r"^.+?(?:は|が).+?(?:傾向|使う|示す|好む|関心|言及)", text):
        return None

    # 2. ファクトチェック記録からのキーワード抽出
    fc_match = re.search(r"以前ファクトチェックした話題[「『](.*?)[」』]", text)
    if fc_match:
        fc_inner = fc_match.group(1).strip()
        if _SENSITIVE_THEME_RE.search(fc_inner):
            return None
        # 末尾の付加語（のファン、ファン、マニア等）をトリム
        fc_inner = re.sub(r"(?:のファン|ファン|マニア)$", "", fc_inner).strip()
        # 複数単語が入っている場合、主要な作品名等を取り出す
        parts = fc_inner.split()
        stop_words = {"ファン", "マニア", "ミフテリー", "の", "を", "に", "が", "と", "で"}
        valid_parts = [p for p in parts if len(p) >= 2 and p not in stop_words and "ミフテリー" not in p]
        from .models import _contains_simplified_chinese
        if valid_parts:
            topic_candidate = max(valid_parts, key=len)
            if 2 <= len(topic_candidate) <= 25 and not _contains_simplified_chinese(topic_candidate) and not _SENSITIVE_THEME_RE.search(topic_candidate):
                return topic_candidate
        if len(fc_inner) <= 25 and "ミフテリー" not in fc_inner and not _contains_simplified_chinese(fc_inner):
            return fc_inner
        return None

    # 結論部分の長文は破棄
    if "の結論:" in text:
        text = text.split("の結論:")[0].strip()

    # 「過去のエピソード: 」「興味・関心: 」のプレフィックスを除去
    for prefix in ("過去のエピソード: ", "興味・関心: "):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()

    # 末尾の助詞や状態表現を削る
    text = re.sub(r"(?:がある|を持ってい?る|が好き|をやってい?る|について|のこと)$", "", text).strip()

    # トリム後も述語・文節断片が残っていないか除外チェック
    if re.search(
        r"(?:許容して|存在す|飲んでい|食べてい|知ってい|思ってい|言ってい|やってい|してい|"
        r"という(?:呼び方|品種|意味|設定|こと|もの|話)|"
        r"まだ[^\s]{1,10}ない|ている$|ていない$|ていた$|する$|ない$|だ$|である$)",
        text,
    ):
        return None

    # 短くて自然な日本語名詞フレーズ（2〜25文字）ならテーマとして採用
    if 2 <= len(text) <= 25:
        from .models import _contains_simplified_chinese
        if not _contains_simplified_chinese(text) and "ミフテリー" not in text and not _SENSITIVE_THEME_RE.search(text):
            return text

    return None


async def fetch_single_db_theme(
    db_path: str | None = None,
    *,
    bot: Any | None = None,
    guild_id: int | None = None,
    channel_id: int | None = None,
    player_user_ids: list[int] | None = None,
) -> str | None:
    """DBから記憶をサンプリングし、クレンジングされた安全な『単一の着想テーマ』を1つだけ返す。
    複数の無関係な記憶を同時に渡してLLMがジャンルを混同するのを防ぐ。
    """
    raw_contexts = await fetch_random_db_contexts(
        db_path=db_path,
        bot=bot,
        guild_id=guild_id,
        channel_id=channel_id,
        player_user_ids=player_user_ids,
        limit=5,
    )
    if not raw_contexts:
        return None

    valid_themes: list[str] = []
    for c in raw_contexts:
        cleaned = _extract_clean_db_theme(c)
        if cleaned and cleaned not in valid_themes:
            valid_themes.append(cleaned)

    if valid_themes:
        chosen = random.choice(valid_themes)
        log.info("Selected single clean DB theme for tier game: %r (from %d candidates)", chosen, len(valid_themes))
        return chosen

    return None


async def generate_topic_with_ai(
    model_override: str | None = None,
    *,
    bot: Any | None = None,
    db_path: str | None = None,
    guild_id: int | None = None,
    channel_id: int | None = None,
    player_user_ids: list[int] | None = None,
    force_db_topic: bool = False,
    excluded_categories: set[str] | None = None,
    excluded_topics: list[str] | None = None,
) -> TopicData:
    """AIモデル（Ollama）を用いてお題と5つの選択肢を生成する。
    一定確率でAIが使用しているデータベース（MemoryStore）を着想のヒントにしてお題を生成する。失敗時はフォールバックする。
    """
    try:
        from ollama_bot.common.ollama_helpers import call_ollama_json
    except Exception as e:
        log.warning("call_ollama_json import failed: %r; using fallback topic", e)
        return get_fallback_topic()

    model_name = (
        model_override
        or str(cfg("OLLAMA_MIDDLE_MODEL", cfg("OLLAMA_MAIN_MODEL", cfg("OLLAMA_MODEL", ""))) or "").strip()
    )
    if not model_name:
        log.info("No Ollama model configured; using fallback topic")
        return get_fallback_topic()

    use_db_topic = cfg_bool("TIER_GAME_USE_DB_TOPIC", True)
    db_topic_prob = cfg_float("TIER_GAME_DB_TOPIC_PROBABILITY", 0.35)

    # 毎回出なくていい: 確率でDB参照モードを抽選（force_db_topic指定時は常に試行）
    should_try_db = force_db_topic or (use_db_topic and random.random() < db_topic_prob)
    db_theme: str | None = None
    if should_try_db:
        try:
            db_theme = await fetch_single_db_theme(
                db_path=db_path,
                bot=bot,
                guild_id=guild_id,
                channel_id=channel_id,
                player_user_ids=player_user_ids,
            )
            if db_theme:
                log.info("Using clean DB theme for tier topic generation: %r", db_theme)
            else:
                log.info("DB topic triggered, but no clean theme found; falling back to standard topic generation")
        except Exception as e:
            log.warning("Failed to fetch clean DB theme for tier game: %r; continuing with standard topic", e)
            db_theme = None

    # 直近のお題を提示して被りを防ぐ（除外指定お題も統合）
    recent_list = list(_RECENT_TOPIC_CACHE)[-5:]
    if excluded_topics:
        for t in excluded_topics:
            if t and t not in recent_list:
                recent_list.append(t)

    available_themes = [
        t for t in INSPIRATION_THEMES
        if not excluded_categories or t[0].strip("【】") not in {c.strip("【】") for c in excluded_categories}
    ] or INSPIRATION_THEMES
    theme_name, theme_examples = random.choice(available_themes)

    system_prompt = (
        "あなたはDiscordパーティゲーム『ティア表バトル』のお題作家です。\n"
        "各プレイヤーが『S（最高）〜 D（最下位）』の5段階にランク付けして予想し合い、価値観や好みの違いで盛り上がるゲームです。\n\n"
        "【普遍的生成原則】\n"
        "1. プレイヤーは人間です: 回答するプレイヤーはDiscordサーバーの人間参加者です。\n"
        "   ⚠️『あなたがAIとして』『AIなら』『過去の記憶から守るなら』など、プレイヤーをAI扱いする表現や、Botの内部システム・記憶構造に言及するメタ表現はお題・選択肢・カテゴリともに【一切禁止】です。\n"
        "2. お題の本質（人間の主観的格付け）: プレイヤー個人の好み・直感・こだわりで順位が分かれる、人間向けの自然な疑問形のお題にしてください（例: 『休日に一番食べたいラーメンの味は？』『もし1つだけ超能力を手に入れるなら？』など）。\n"
        "   ⚠️ 正解のある客観知識クイズ（分類、学名、類似、正誤、数値当てなど）はゲームの趣旨に反するため厳禁です。\n"
        "   ⚠️ topic（問題文）の末尾に『5つ選んでS〜Dにランク付けするとしたら？』等のルール説明を含めないこと（短くキャッチーな疑問形のお題のみにすること）。\n"
        "3. 選択肢の統一性（同ジャンル・同列の定番名詞）: 5つの選択肢は必ず【同じジャンル・同じカテゴリの実在する定番名詞】で統一してください。\n"
        "   ❌ 厳禁（ジャンルバラバラ）: 『ちいかわ』『金田一少年』『カップラーメン』『瞬間移動』『目玉焼き』のような寄せ集め\n"
        "   ⭕ 必須（同ジャンル統一）: 推理作品なら『金田一少年の事件簿』『名探偵コナン』『古畑任三郎』『相棒』『ガリレオ』のように同じジャンルの定番名詞5つ\n"
        "   ⚠️【別作品キャラ混入防止と正確な名称】特定の作品・シリーズ（例: 東方Project等）をテーマにする場合、絶対に他作品（ポケモン等）のキャラクターを混ぜないこと。またキャラクター名を勝手に創作・改変しないこと（実在する公式の正確な名前のみ）。\n"
        "4. 言語の完全性: 必ず【完全な自然な日本語（JIS水準の漢字・ひらがな・カタカナ）】のみを使用してください。\n"
        "   ⚠️ 架空の造語、不自然なカタカナ表記（ミフテリー等）、簡体字中国語、数字や記号のみ、お題そのものの単語（同語反復）は厳禁です。\n"
        "5. 必ず指定のJSONフォーマットのみを出力してください。"
    )

    def _build_user_prompt(r_list: list[str]) -> str:
        r_text = "、".join(r_list) if r_list else "なし"
        if db_theme:
            return (
                f"今回の着想テーマ: 【{db_theme}】\n"
                f"直近で出たお題（重複禁止）: {r_text}\n\n"
                "上記のテーマを着想のきっかけとして、プレイヤー（人間）同士でS〜Dの好みが分かれて盛り上がる、"
                "自然でスッキリしたお題と、【同じジャンルで誰もが知る定番の実在名詞5つ】を生成してください。\n"
                "※ 着想元の文面から単語を抜き出してそのまま並べることは厳禁です。そのジャンルの定番名詞5つを新しく揃えてください。\n"
                "※ 特定の作品・シリーズ（例: 東方Project等）をテーマにする場合、絶対に他作品（ポケモン等）のキャラクターを混ぜず、キャラ名を勝手に創作・改変しないでください。\n"
                "※ topic（問題文）の末尾に『5つ選んでS〜Dにランク付けするとしたら？』等のルール説明を含めないでください。\n"
                "※ プレイヤーをAI扱いする表現（『AIとして』等）は厳禁です。人間同士の好みを問うお題にしてください。\n"
                "※ category も『AIの記憶』などのメタ表現ではなく、自然な日本語ジャンル（グルメ、エンタメ、日常、趣味等）にしてください。"
            )
        else:
            return (
                f"今回の着想テーマ: {theme_name}\n"
                f"テーマの具体例: {theme_examples}\n"
                f"直近で出たお題（重複禁止）: {r_text}\n\n"
                "上記のテーマを着想元として、誰もが知っていて意見や好みがS〜Dに分かれる、"
                "新しくて盛り上がる自然なお題と、【同じジャンルで誰もが知る定番の実在名詞5つ】を生成してください。\n"
                "※ 特定の作品・シリーズ（例: 東方Project等）をテーマにする場合、絶対に他作品（ポケモン等）のキャラクターを混ぜず、キャラ名を勝手に創作・改変しないでください。\n"
                "※ topic（問題文）の末尾に『5つ選んでS〜Dにランク付けするとしたら？』等のルール説明を含めないでください。\n"
                "※ 必ず自然な日本語で、プレイヤー（人間）同士が好みを競い合うお題にしてください。"
            )

    for attempt in range(2):
        current_prompt = _build_user_prompt(recent_list)
        if attempt > 0:
            current_prompt += (
                "\n\n【再生成の厳格な指示】前回の出力はお題または選択肢が無効、あるいは既存お題と重複していました。"
                "直近で出たお題（重複禁止）とは異なる、必ず【完全な自然な日本語】で、プレイヤー（人間）向けのお題と、同じジャンルの誰もが知る身近な定番名詞5つを再生成してください！"
            )
        try:
            result_json = await call_ollama_json(
                current_prompt,
                system_prompt=system_prompt,
                schema=TIER_TOPIC_SCHEMA,
                model=model_name,
                think=False,
                timeout_sec=25.0,
                temperature=0.85,
                top_p=0.9,
            )
            if isinstance(result_json, dict):
                topic_str = str(result_json.get("topic", "") or "").strip()
                # 問題文末尾の冗長なランク付け指示文を除去
                topic_str = re.sub(
                    r"[、。，．\s]*(?:この中から|以下から|選択肢から)?\s*(?:5\s*つ|５つ)?(?:選んで)?\s*S\s*[〜～\-ーから]\s*D\s*に(?:ランク付け|格付け|順位付け|評価)?(?:する|した|つけ)?(?:としたら|なら|すると|して|るなら)?[？?]?\s*$",
                    "",
                    topic_str,
                    flags=re.IGNORECASE,
                ).strip()
                topic_str = re.sub(
                    r"[、。，．\s]*(?:この中から|以下から|選択肢から)?\s*(?:5\s*つ|５つ)?(?:選んで)?\s*S\s*[〜～\-ーから]\s*D\s*にランク付けするとしたら[？?]?\s*$",
                    "",
                    topic_str,
                    flags=re.IGNORECASE,
                ).strip()
                options_raw = result_json.get("options") or []
                options = [str(opt).strip() for opt in options_raw if str(opt).strip()]
                default_cat = theme_name.strip("【】").split("・")[0] if theme_name else "一般"
                category = str(result_json.get("category", default_cat) or default_cat).strip()
                # 'AIの記憶: グルメ' などのメタプレフィックスを安全に除去
                category = re.sub(r"^(?:AI(?:の記憶)?|記憶|システム|ログ)[\s:：_\-・]*", "", category, flags=re.IGNORECASE).strip()
                category = category.strip("【】")
                if not category or any(w in category for w in ("AI", "記憶", "システム", "ログ")):
                    category = default_cat

                candidate = TopicData(topic=topic_str, options=options, category=category)
                is_valid = candidate.is_valid()
                is_duplicate = candidate.topic in _RECENT_TOPIC_CACHE or bool(excluded_topics and candidate.topic in excluded_topics)

                if is_valid and not is_duplicate:
                    _RECENT_TOPIC_CACHE.append(candidate.topic)
                    log.info(
                        "Successfully generated AI topic (attempt=%d, db_theme=%s): %r (options=%r, category=%r)",
                        attempt + 1,
                        bool(db_theme),
                        candidate.topic,
                        candidate.options,
                        candidate.category,
                    )
                    return candidate

                if is_duplicate and candidate.topic not in recent_list:
                    recent_list.append(candidate.topic)

                if not is_valid:
                    log.warning(
                        "AI topic validation failed (attempt=%d): topic=%r options=%r category=%r (valid=False, duplicate=%s)",
                        attempt + 1,
                        candidate.topic,
                        candidate.options,
                        candidate.category,
                        is_duplicate,
                    )
                else:
                    log.warning(
                        "AI topic duplicated in recent cache (attempt=%d): topic=%r category=%r (valid=True, duplicate=True)",
                        attempt + 1,
                        candidate.topic,
                        candidate.category,
                    )
        except Exception as e:
            log.warning("AI topic generation attempt %d failed: %r", attempt + 1, e)

    log.info("AI topic generation exhausted or invalid; falling back to curated preset topic")
    return get_fallback_topic()


def _extract_topic_core_keywords(topic_str: str) -> set[str]:
    """お題文字列から重複比較用の主要な主題キーワード（名詞フレーズ）を抽出する。"""
    clean = re.sub(r"[？\?！!。、，．\s\(\)（）「」『』〜～\-ー/／]", " ", topic_str)
    stop_words = {
        "一番", "最高", "最強", "定番", "人気", "好き", "おすすめ", "欲しい",
        "選ぶ", "どれ", "何", "誰", "どこ", "いつ", "どう", "もしも", "なら",
        "といえば", "ランキング", "ランク", "決定戦", "選手権", "勝負", "バトル",
        "するもの", "すること", "したい", "食べたい", "入れたい", "ある", "いる",
        "この中から", "選択肢", "項目", "理由", "もの", "こと",
    }
    tokens = re.split(
        r"(?:の具材|の具|のトッピング|のサイドメニュー|のメニュー|で一番|に一番|が一番|は一番|で最高|に最高|で最強|に最強|について|にとって|として|には|では|とは|から|より|で|に|は|が|を|と|の)",
        clean,
    )
    keywords: set[str] = set()
    from .models import _normalize_option_key
    for tok in tokens:
        t = tok.strip()
        norm = _normalize_option_key(t)
        if len(norm) >= 2 and norm not in stop_words and t not in stop_words:
            keywords.add(norm)
    return keywords


def _is_duplicate_in_pool(candidate: TopicData, pool: list[TopicData]) -> bool:
    """直近プール内に同一お題、同一カテゴリ（一般/その他を除く）、または同一主題キーワードが存在するかを判定する。"""
    cand_norm_topic = candidate.topic.strip()
    cand_cat = candidate.category.strip()
    cand_keywords = _extract_topic_core_keywords(cand_norm_topic)

    for item in pool:
        # 1. お題文字列の完全一致
        if item.topic.strip() == cand_norm_topic:
            return True
        # 2. カテゴリの重複チェック（'その他' / '一般' を除く）
        item_cat = item.category.strip()
        if cand_cat not in ("その他", "一般") and item_cat not in ("その他", "一般") and cand_cat == item_cat:
            return True
        # 3. 正規化後の主題キーワードの重複チェック
        if cand_keywords:
            item_keywords = _extract_topic_core_keywords(item.topic)
            if cand_keywords & item_keywords:
                return True
    return False


class TopicPool:
    """バックグラウンドでAIお題を事前生成し、ゲーム開始時に待ち時間0秒で供給するプール。"""

    def __init__(self, target_size: int = 3) -> None:
        self.target_size = target_size
        self._pool: list[TopicData] = []
        self._lock = asyncio.Lock()
        self._filling_task: asyncio.Task[None] | None = None

    @property
    def size(self) -> int:
        return len(self._pool)

    def trigger_fill(
        self,
        *,
        bot: Any | None = None,
        guild_id: int | None = None,
        channel_id: int | None = None,
        player_user_ids: list[int] | None = None,
    ) -> None:
        """プールがお題で満たされていない場合、バックグラウンドでお題補充タスクを非同期起動する。"""
        if self.size >= self.target_size:
            return
        if self._filling_task is not None and not self._filling_task.done():
            return
        self._filling_task = asyncio.create_task(
            self._fill_worker(
                bot=bot,
                guild_id=guild_id,
                channel_id=channel_id,
                player_user_ids=player_user_ids,
            )
        )

    async def _fill_worker(
        self,
        *,
        bot: Any | None = None,
        guild_id: int | None = None,
        channel_id: int | None = None,
        player_user_ids: list[int] | None = None,
    ) -> None:
        """バックグラウンドでお題を生成してプールに蓄積する。"""
        needed = self.target_size - self.size
        max_attempts = min(max(needed * 2, 2), 6)
        attempts = 0
        while self.size < self.target_size and attempts < max_attempts:
            attempts += 1
            try:
                async with self._lock:
                    excluded_cats = {t.category for t in self._pool if t.category}
                    excluded_topics = [t.topic for t in self._pool]

                topic = await generate_topic_with_ai(
                    bot=bot,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    player_user_ids=player_user_ids,
                    excluded_categories=excluded_cats,
                    excluded_topics=excluded_topics,
                )
                if topic and topic.is_valid():
                    async with self._lock:
                        if not _is_duplicate_in_pool(topic, self._pool):
                            self._pool.append(topic)
                            log.info(
                                "Prefetched topic added to pool (%d/%d, cat=%r): %r",
                                len(self._pool),
                                self.target_size,
                                topic.category,
                                topic.topic,
                            )
                        else:
                            log.debug(
                                "Prefetched topic skipped due to category or topic keyword duplication: topic=%r cat=%r",
                                topic.topic,
                                topic.category,
                            )
            except Exception as e:
                log.warning("Background topic prefetch error: %r", e)
                break
            await asyncio.sleep(0.5)

    async def pop_topics(self, count: int) -> list[TopicData]:
        """プールからお題を取り出す（存在分のみ、即時返却）。"""
        async with self._lock:
            taken = self._pool[:count]
            self._pool = self._pool[count:]
            return taken


# グローバルお題プール（常時ストック）
GLOBAL_TOPIC_POOL = TopicPool(target_size=3)


async def generate_multiple_topics(
    count: int,
    *,
    bot: Any | None = None,
    guild_id: int | None = None,
    channel_id: int | None = None,
    player_user_ids: list[int] | None = None,
) -> list[TopicData]:
    """参加プレイヤー全員に別々のお題を配るため、重複のない count 個のお題リストを生成する。
    1. 事前作成プール（TopicPool）から即座に取得（待ち時間0秒）。
    2. 不足分はAI並列生成またはプリセット集から安全に補完。
    3. 取得後はバックグラウンドで次回用のプール補充を自動起動。
    """
    if count <= 0:
        return []

    collected: list[TopicData] = []
    used_titles: set[str] = set()

    # 1. 事前作成プール（TopicPool）から即座に取得
    prefetched = await GLOBAL_TOPIC_POOL.pop_topics(count)
    for t in prefetched:
        if t.is_valid() and t.topic not in used_titles and t.topic not in _RECENT_TOPIC_CACHE:
            collected.append(t)
            used_titles.add(t.topic)
            _RECENT_TOPIC_CACHE.append(t.topic)
            if len(collected) >= count:
                break

    # 2. プールで不足している場合、AI並列生成を短時間試行
    if len(collected) < count:
        remaining_needed = count - len(collected)
        ai_attempts = min(remaining_needed, 2)
        ai_tasks = [
            generate_topic_with_ai(
                bot=bot,
                guild_id=guild_id,
                channel_id=channel_id,
                player_user_ids=player_user_ids,
                force_db_topic=(i == 0),
            )
            for i in range(ai_attempts)
        ]
        try:
            done, _ = await asyncio.wait(
                [asyncio.create_task(t) for t in ai_tasks],
                timeout=6.0,
            )
            for task in done:
                try:
                    res = task.result()
                    if res and res.is_valid() and res.topic not in used_titles:
                        collected.append(res)
                        used_titles.add(res.topic)
                        if len(collected) >= count:
                            break
                except Exception as e:
                    log.debug("AI topic task error: %r", e)
        except Exception as e:
            log.warning("Failed during parallel AI topic generation: %r", e)

    # 3. 不足分をプリセットお題から重複なく補填
    if len(collected) < count:
        candidates = [t for t in PRESET_TOPICS if t.topic not in used_titles and t.topic not in _RECENT_TOPIC_CACHE]
        if len(candidates) < (count - len(collected)):
            _RECENT_TOPIC_CACHE.clear()
            candidates = [t for t in PRESET_TOPICS if t.topic not in used_titles]
        random.shuffle(candidates)

        for t in candidates:
            topic_copy = TopicData(topic=t.topic, options=list(t.options), category=t.category)
            collected.append(topic_copy)
            used_titles.add(topic_copy.topic)
            _RECENT_TOPIC_CACHE.append(topic_copy.topic)
            if len(collected) >= count:
                break

    # 万が一それでも足りない場合
    while len(collected) < count:
        t = get_fallback_topic()
        collected.append(TopicData(topic=f"{t.topic} ({len(collected)+1})", options=list(t.options), category=t.category))

    # 4. お題を取り出したので、バックグラウンドでプールの補充を開始（次回用に備える）
    GLOBAL_TOPIC_POOL.trigger_fill(
        bot=bot,
        guild_id=guild_id,
        channel_id=channel_id,
        player_user_ids=player_user_ids,
    )

    return collected[:count]

