"""ティア表パーティゲームのDiscord Embedおよびテキスト描画モジュール。"""
from __future__ import annotations

import discord

from .models import (
    TIER_EMOJIS,
    TIER_LABELS,
    TIER_RANKS,
    CompatibilityPair,
    PlayerState,
    RoundResult,
    TopicData,
)


def render_tier_table_text(tier_assignments: dict[str, str]) -> str:
    """ティア割り当て（アイテム -> S/A/B/C/D）を格ゲー風ティア表テキストにフォーマットする。"""
    grouped: dict[str, list[str]] = {r: [] for r in TIER_RANKS}
    for item, rank in tier_assignments.items():
        if rank in grouped:
            grouped[rank].append(item)

    lines: list[str] = []
    for rank in TIER_RANKS:
        emoji = TIER_EMOJIS.get(rank, "⬜")
        label = TIER_LABELS.get(rank, "")
        items = grouped[rank]
        items_str = "、 ".join(items) if items else "（なし）"
        lines.append(f"{emoji} **【 {rank} 】** {label}\n┗ {items_str}")

    return "\n".join(lines)


def render_lobby_embed(
    host_name: str,
    players: list[PlayerState],
    is_ready_to_start: bool,
) -> discord.Embed:
    """参加者募集（ロビー）用Embedを生成する。"""
    embed = discord.Embed(
        title="🎮 ティア表パーティゲーム - 参加者募集中！",
        description=(
            "格闘ゲーム等でおなじみの「ティア表」を使った心理・価値観当てパーティゲーム！\n\n"
            "**【ゲームの流れ】**\n"
            "1. 🎯 **正解を作る** : 全員に別々のお題と5つのワードを配布！自分の感覚でS〜DのTierを作成（他プレイヤーには非公開）。\n"
            "2. 📢 **お題を公開** : 順番に1人が主役となり、その人のお題とワード（順不同）だけが全体に公開！\n"
            "3. 🧠 **予想する** : 主役以外の全員が「主役ならどう並べるか」を**180秒間**で予想！\n"
            "4. 🎉 **結果発表** : 主役の本当の並びが明かされ、当たった位置の数が得点に！全て当たれば『**パーフェクト**』！\n"
        ),
        color=0xF1C40F,  # ゴールド
    )

    if players:
        member_list = "\n".join(f"{i+1}. {p.display_name}" for i, p in enumerate(players))
    else:
        member_list = "（まだ誰も参加していません）"

    status_str = "✅ **2名以上集まりました！いつでも開始できます！**" if is_ready_to_start else "⏳ **開始には最低2名が必要です（推奨3〜8名）**"

    embed.add_field(
        name=f"👥 参加プレイヤー ({len(players)}名)",
        value=member_list,
        inline=False,
    )
    embed.add_field(
        name="📢 進行状況",
        value=f"募集者: **{host_name}**\n{status_str}",
        inline=False,
    )
    embed.set_footer(text="下のボタンから「参加する」を押してエントリーしてください！")
    return embed


def render_answer_setup_embed(
    players: list[PlayerState],
    submitted_user_ids: set[int],
) -> discord.Embed:
    """ステップ1: 全員が自分のお題で正解のTier表を作成するフェーズのEmbed。"""
    embed = discord.Embed(
        title="🎯 【ステップ1: 正解作成中】自分の感覚でTier表を作ろう！",
        description=(
            "参加者全員に**それぞれ別々のお題と5つのワード**が配られました！\n\n"
            "下の『📋 自分のTier表を作成する』ボタンを押して、\n"
            "**自分の直感・好み**に従ってS〜DのTier表を作成してください。\n\n"
            "🔒 **完全秘匿**: あなたのお題・ワード・Tier表は、あなたの主役ラウンドまで他の人には一切見えません！"
        ),
        color=0x9B59B6,  # パープル
    )

    status_lines: list[str] = []
    for p in players:
        icon = "✅ 作成完了" if p.user_id in submitted_user_ids else "⏳ 作成中…"
        status_lines.append(f"・{p.display_name}: {icon}")

    embed.add_field(
        name=f"📝 全員の作成状況 ({len(submitted_user_ids)}/{len(players)})",
        value="\n".join(status_lines),
        inline=False,
    )
    embed.set_footer(text="下の『📋 自分のTier表を作成する』ボタンを押して作成を開始してください")
    return embed


def render_round_guess_embed(
    round_number: int,
    total_rounds: int,
    host_name: str,
    topic: TopicData,
    shuffled_options: list[str],
    end_timestamp: int,
    submitted_user_ids: set[int],
    all_players: list[PlayerState],
    current_host_id: int,
) -> discord.Embed:
    """ステップ2 & 3: お題公開 & 180秒予想フェーズのEmbed。"""
    embed = discord.Embed(
        title=f"📢 【第 {round_number} / {total_rounds} ラウンド】",
        description=(
            f"👑 今回の主役は **{host_name}** さんです！\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 **お題: 『 {topic.topic} 』**\n"
            f"ジャンル: `{topic.category}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"主役の **{host_name}** さんならどう並べるかを予想してください！\n"
            f"⏰ **制限時間: 180秒**（残り: <t:{end_timestamp}:R> まで）\n"
        ),
        color=0x3498DB,  # ブルー
    )

    options_text = "\n".join(f"**{i+1}.** {opt}" for i, opt in enumerate(shuffled_options))
    embed.add_field(
        name="📋 5つのワード（順不同）",
        value=options_text,
        inline=False,
    )

    # 予想者の提出状況
    guesser_players = [p for p in all_players if p.user_id != current_host_id]
    status_lines: list[str] = []
    for p in guesser_players:
        icon = "✅ 予想完了" if p.user_id in submitted_user_ids else "⏳ 予想中…"
        status_lines.append(f"・{p.display_name}: {icon}")

    status_lines.append(f"・{host_name} (👑 主役): ☕ 結果待ち")

    embed.add_field(
        name=f"🧠 予想提出状況 ({len(submitted_user_ids)}/{len(guesser_players)})",
        value="\n".join(status_lines),
        inline=False,
    )

    embed.set_footer(text="下の『🎯 主役の並びを予想する』ボタンから予想を作成してください")
    return embed


def render_topic_embed(
    round_number: int,
    total_rounds: int,
    host_name: str,
    topic: TopicData,
    submitted_user_ids: set[int],
    all_players: list[PlayerState],
) -> discord.Embed:
    """後方互換用: 旧お題Embed。"""
    return render_round_guess_embed(
        round_number=round_number,
        total_rounds=total_rounds,
        host_name=host_name,
        topic=topic,
        shuffled_options=topic.options,
        end_timestamp=0,
        submitted_user_ids=submitted_user_ids,
        all_players=all_players,
        current_host_id=0,
    )


def render_round_result_embed(
    result: RoundResult,
    player_states: list[PlayerState],
) -> discord.Embed:
    """ラウンド結果発表用Embedを生成する。"""
    embed = discord.Embed(
        title=f"🎉 【第 {result.round_number} ラウンド 結果発表！】",
        description=f"🎯 お題: **『 {result.topic.topic} 』**\n👑 主役: **{result.host_name}** さん",
        color=0xE67E22,  # オレンジ
    )

    # 主役の本当のティア表
    host_table_str = render_tier_table_text(result.host_tiers)
    embed.add_field(
        name=f"👑 主役（{result.host_name}さん）の【本当の並び】",
        value=host_table_str,
        inline=False,
    )

    # 各参加者のスコアと結果
    guess_results: list[str] = []
    # 獲得スコアが高い順にソート
    sorted_guesses = sorted(
        [(uid, score) for uid, score in result.round_scores.items() if uid != result.host_id],
        key=lambda x: x[1],
        reverse=True,
    )

    id_to_name = {p.user_id: p.display_name for p in player_states}

    for uid, score in sorted_guesses:
        name = id_to_name.get(uid, f"User {uid}")
        exact_cnt = result.exact_matches.get(uid, 0)
        # 詳細な一致情報
        match_details: list[str] = []
        user_guesses = result.guesses.get(uid, {})
        for item in result.shuffled_options:
            h_tier = result.host_tiers.get(item, "?")
            u_tier = user_guesses.get(item, "?")
            diff = result.item_diffs.get(uid, {}).get(item, 0)
            if diff == 0:
                mark = "🎯一致"
            elif diff == 1:
                mark = "👍惜しい"
            else:
                mark = "⚡ズレ"
            match_details.append(f"{item}({u_tier})[{mark}]")

        details_str = " / ".join(match_details)
        perfect_badge = " 🌟 **【パーフェクト！！】**" if exact_cnt == 5 else ""
        guess_results.append(
            f"**{name}**: **+{score} pt** （的中: {exact_cnt}/5）{perfect_badge}\n┗ {details_str}"
        )

    if guess_results:
        embed.add_field(
            name="🎯 参加者の予想結果 & 獲得ポイント",
            value="\n\n".join(guess_results),
            inline=False,
        )

    if result.perfect_users:
        perfect_names = [id_to_name.get(uid, f"User {uid}") for uid in result.perfect_users]
        embed.add_field(
            name="🌟 奇跡のパーフェクト達成！ 🌟",
            value=f"**{', '.join(perfect_names)}** さんが全5項目の並びを完全に的中させました！お見事！",
            inline=False,
        )

    if result.host_bonus_score > 0:
        embed.add_field(
            name="👑 主役ボーナス",
            value=f"みんなの共感・理解度により、主役の **{result.host_name}** さんに **+{result.host_bonus_score} pt** 付与！",
            inline=False,
        )

    # 現在の総合順位
    sorted_players = sorted(player_states, key=lambda p: p.total_score, reverse=True)
    rank_items: list[str] = []
    for p in sorted_players:
        perf_str = f" (🌟×{p.perfect_count})" if p.perfect_count > 0 else ""
        rank_items.append(f"{p.display_name}: **{p.total_score}pt**{perf_str}")
    rank_str = " ➔ ".join(rank_items)
    embed.add_field(
        name="📊 現在の総合スコア",
        value=rank_str,
        inline=False,
    )

    return embed


def render_final_result_embed(
    players: list[PlayerState],
    compatibility_pairs: list[CompatibilityPair],
) -> discord.Embed:
    """全ラウンド終了時の最終結果・ベスト相性度診断Embedを生成する。"""
    embed = discord.Embed(
        title="🏆 ティア表パーティゲーム - 最終結果発表！ 🏆",
        description="全ラウンド終了！熱い心理戦と価値観のぶつかり合い、お疲れ様でした！",
        color=0x9B59B6,  # パープル
    )

    # 1. 総合ランキング
    sorted_players = sorted(players, key=lambda p: (p.total_score, p.perfect_count), reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    ranking_lines: list[str] = []
    for i, p in enumerate(sorted_players):
        medal = medals[i] if i < len(medals) else f"**{i+1}位**"
        perf_badge = f" (🌟 パーフェクト: {p.perfect_count}回)" if p.perfect_count > 0 else ""
        ranking_lines.append(f"{medal} **{p.display_name}**: **{p.total_score} pt**{perf_badge}")

    embed.add_field(
        name="👑 最終順位ランキング",
        value="\n".join(ranking_lines),
        inline=False,
    )

    # 2. ベスト相性度診断（目玉機能）
    if compatibility_pairs:
        best_pair = compatibility_pairs[0]
        worst_pair = compatibility_pairs[-1]

        best_bar = _render_progress_bar(best_pair.score)
        embed.add_field(
            name="💖 【ベストカップル賞】 魂の双子！",
            value=(
                f"**{best_pair.user_a_name}** × **{best_pair.user_b_name}**\n"
                f"相性度: **{best_pair.score}%** {best_bar}\n"
                f"称号: **{best_pair.title}**\n"
                f"💬 *「{best_pair.comment}」*"
            ),
            inline=False,
        )

        if len(compatibility_pairs) > 1 and worst_pair.score < 50.0:
            worst_bar = _render_progress_bar(worst_pair.score)
            embed.add_field(
                name="⚡ 【好敵手賞】 運命のライバル！",
                value=(
                    f"**{worst_pair.user_a_name}** × **{worst_pair.user_b_name}**\n"
                    f"相性度: **{worst_pair.score}%** {worst_bar}\n"
                    f"称号: **{worst_pair.title}**\n"
                    f"💬 *「{worst_pair.comment}」*"
                ),
                inline=False,
            )

        # 全ペアの相性マトリクス一覧
        matrix_lines: list[str] = []
        for pair in compatibility_pairs:
            matrix_lines.append(
                f"・{pair.user_a_name} × {pair.user_b_name}: **{pair.score}%** ({pair.title})"
            )
        embed.add_field(
            name="📈 参加者全員の相性度一覧",
            value="\n".join(matrix_lines),
            inline=False,
        )

    embed.set_footer(text="遊んでくれてありがとう！『もう一度遊ぶ』で新しいお題に挑戦できます！")
    return embed


def _render_progress_bar(pct: float, length: int = 8) -> str:
    """パーセンテージに応じたプログレスバーを生成する。"""
    filled = int(round((pct / 100.0) * length))
    filled = max(0, min(length, filled))
    empty = length - filled
    return f"`[{'█' * filled}{'░' * empty}]`"
