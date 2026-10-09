"""ティア表パーティゲームのスコア計算および相性度診断モジュール。"""
from __future__ import annotations

import logging
from typing import Final

from .models import CompatibilityPair, RoundResult, TIER_SCORES, TopicData

log = logging.getLogger("tier_game.scoring")

# アイテム1つあたりのスコア配分
SCORE_PER_HIT: Final[int] = 1    # 当たった位置1つあたりの得点（ユーザー仕様: 当たった位置の数が得点）
PERFECT_SCORE_THRESHOLD: Final[int] = 5  # 全て当たればパーフェクト


def calculate_item_diff(tier_a: str, tier_b: str) -> int:
    """2つのティア間のランク距離（0〜4）を算出する。"""
    val_a = TIER_SCORES.get(tier_a, 2)
    val_b = TIER_SCORES.get(tier_b, 2)
    return abs(val_a - val_b)


def calculate_round_result(
    round_number: int,
    host_id: int,
    host_name: str,
    topic: TopicData,
    host_tiers: dict[str, str],
    guesses: dict[int, dict[str, str]],
    shuffled_options: list[str] | None = None,
) -> RoundResult:
    """1ラウンドの主役ティア表と参加者の予想ティア表を突き合わせ、スコアと結果を算出する。

    ユーザー仕様:
    - 当たった位置の数が得点になる（0〜5点）。
    - 5つ全て当たれば「パーフェクト」。
    """
    round_scores: dict[int, int] = {}
    item_diffs: dict[int, dict[str, int]] = {}
    exact_matches: dict[int, int] = {}
    perfect_users: list[int] = []

    guesser_scores: list[int] = []

    for guesser_id, guess_tiers in guesses.items():
        if guesser_id == host_id:
            continue

        diff_map: dict[str, int] = {}
        exact_cnt = 0

        for item in topic.options:
            h_tier = host_tiers.get(item, "B")
            g_tier = guess_tiers.get(item, "B")
            diff = calculate_item_diff(h_tier, g_tier)
            diff_map[item] = diff
            if diff == 0:
                exact_cnt += 1

        # 当たった位置の数が得点になる
        total_pts = exact_cnt * SCORE_PER_HIT
        if exact_cnt >= PERFECT_SCORE_THRESHOLD:
            perfect_users.append(guesser_id)

        round_scores[guesser_id] = total_pts
        item_diffs[guesser_id] = diff_map
        exact_matches[guesser_id] = exact_cnt
        guesser_scores.append(total_pts)

    # 主役ボーナス: 参加者の平均獲得得点（四捨五入）
    host_bonus = 0
    if guesser_scores:
        avg_score = sum(guesser_scores) / len(guesser_scores)
        host_bonus = max(0, round(avg_score))

    round_scores[host_id] = host_bonus

    return RoundResult(
        round_number=round_number,
        host_id=host_id,
        host_name=host_name,
        topic=topic,
        host_tiers=host_tiers,
        shuffled_options=shuffled_options or list(topic.options),
        guesses=guesses,
        round_scores=round_scores,
        item_diffs=item_diffs,
        exact_matches=exact_matches,
        perfect_users=perfect_users,
        host_bonus_score=host_bonus,
    )


def evaluate_compatibility(
    round_results: list[RoundResult],
    players: dict[int, str],
) -> list[CompatibilityPair]:
    """全ラウンドの全プレイヤー提出からペアごとの相性度（0%〜100%）と称号を算出する。"""
    user_ids = list(players.keys())
    if len(user_ids) < 2:
        return []

    # ペアごとの距離の合計とアイテム数を集計
    pair_stats: dict[tuple[int, int], tuple[float, int, int]] = {}
    # (user_a, user_b) -> (sum_match_rate, total_items, exact_matches)

    for i in range(len(user_ids)):
        for j in range(i + 1, len(user_ids)):
            u1, u2 = user_ids[i], user_ids[j]
            pair_stats[(u1, u2)] = (0.0, 0, 0)

    for r in round_results:
        # 主役と全参加者の提出をまとめる
        all_tiers: dict[int, dict[str, str]] = dict(r.guesses)
        all_tiers[r.host_id] = r.host_tiers

        for (u1, u2), (sum_rate, total_items, exact_cnt) in list(pair_stats.items()):
            if u1 in all_tiers and u2 in all_tiers:
                t1 = all_tiers[u1]
                t2 = all_tiers[u2]
                for item in r.topic.options:
                    if item in t1 and item in t2:
                        diff = calculate_item_diff(t1[item], t2[item])
                        # diff 0: 1.0, diff 1: 0.75, diff 2: 0.5, diff 3: 0.25, diff 4: 0.0
                        item_rate = 1.0 - (diff / 4.0)
                        sum_rate += item_rate
                        total_items += 1
                        if diff == 0:
                            exact_cnt += 1
                pair_stats[(u1, u2)] = (sum_rate, total_items, exact_cnt)

    pairs: list[CompatibilityPair] = []
    for (u1, u2), (sum_rate, total_items, exact_cnt) in pair_stats.items():
        if total_items == 0:
            score_pct = 50.0
        else:
            score_pct = round((sum_rate / total_items) * 100.0, 1)

        title, comment = _determine_pair_title(score_pct)
        pairs.append(
            CompatibilityPair(
                user_a_id=u1,
                user_a_name=players.get(u1, f"User {u1}"),
                user_b_id=u2,
                user_b_name=players.get(u2, f"User {u2}"),
                score=score_pct,
                total_comparisons=total_items,
                exact_matches=exact_cnt,
                title=title,
                comment=comment,
            )
        )

    # 相性度が高い順にソート
    pairs.sort(key=lambda p: p.score, reverse=True)
    return pairs


def _determine_pair_title(score_pct: float) -> tuple[str, str]:
    """相性度に応じたパーティ感溢れる称号とコメントを決定する。"""
    if score_pct >= 90.0:
        return (
            "💖 魂の双子（ソウルメイト）",
            "言葉がなくても全て通じ合える奇跡の一致！前世は同じ魂だったかも！？",
        )
    if score_pct >= 75.0:
        return (
            "🤝 息ピッタリの親友",
            "価値観がほぼ完全に一致！一緒に旅行に行っても絶対に揉めない最高の関係！",
        )
    if score_pct >= 60.0:
        return (
            "🍻 気の合う仲間",
            "基本はバッチリ共感！たまにある好みの違いも楽しいスパイスになる好相性！",
        )
    if score_pct >= 45.0:
        return (
            "🎲 凸凹コンビ",
            "似ているようで微妙に違う、絶妙なバランス！お互いのこだわりが刺激的！",
        )
    if score_pct >= 30.0:
        return (
            "⚡ 運命の好敵手（ライバル）",
            "ことごとく好みが真っ二つ！白熱の意見交換が永遠に止まらない刺激的ペア！",
        )
    return (
        "💥 パラレルワールドの住人",
        "価値観が完全なる真逆！同じカレーを見ていても見えている宇宙が違うレベル！？",
    )
