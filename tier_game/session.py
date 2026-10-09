"""ティア表パーティゲームのセッション管理モジュール。"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Callable, Coroutine, TYPE_CHECKING

import discord

from .models import (
    GamePhase,
    PlayerState,
    PlayerSubmission,
    RoundResult,
    TopicData,
    is_valid_tier_assignment,
)
from .renderer import (
    render_answer_setup_embed,
    render_final_result_embed,
    render_lobby_embed,
    render_round_guess_embed,
    render_round_result_embed,
    render_topic_embed,
)
from .scoring import calculate_round_result, evaluate_compatibility
from .topics import generate_multiple_topics, generate_topic_with_ai

log = logging.getLogger("tier_game.session")


class TierGameSession:
    """単一チャンネルにおけるティア表パーティゲームの1セッションを管理するクラス。"""

    def __init__(
        self,
        channel: Any,
        creator: discord.User | discord.Member,
        on_cleanup: Callable[[int], Coroutine[Any, Any, None]] | None = None,
        admin_user_ids: set[int] | None = None,
        bot: Any | None = None,
    ) -> None:
        self.channel = channel
        self.channel_id: int = int(getattr(channel, "id", 0))
        self.creator_id: int = creator.id
        self.creator_name: str = creator.display_name
        self.admin_user_ids: set[int] = admin_user_ids or set()
        self.on_cleanup = on_cleanup
        self.bot = bot

        self.phase: GamePhase = GamePhase.LOBBY
        self.players: dict[int, PlayerState] = {}
        self.host_order: list[int] = []
        self.current_round: int = 0
        self.total_rounds: int = 0

        self.current_topic: TopicData = TopicData("未定", [])
        self.current_shuffled_options: list[str] = []
        self.current_host_id: int = 0
        self.current_host_name: str = ""
        self.current_guesses: dict[int, dict[str, str]] = {}
        self.current_submissions: dict[int, PlayerSubmission] = {}
        self.round_results: list[RoundResult] = []

        self.guess_end_timestamp: int = 0
        self._guess_timer_task: asyncio.Task[None] | None = None

        self.main_message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._timeout_task: asyncio.Task[None] | None = None

        # 初期プレイヤーとして募集者を追加
        self.players[creator.id] = PlayerState(
            user_id=creator.id,
            display_name=creator.display_name,
            mention=creator.mention,
        )

    def _cancel_timer(self) -> None:
        """予想タイマータスクを安全にキャンセルする。"""
        if self._guess_timer_task is not None and not self._guess_timer_task.done():
            self._guess_timer_task.cancel()
        self._guess_timer_task = None

    def is_creator(self, user_id: int) -> bool:
        """指定ユーザーがゲームのコマンド実行者（募集者）かを判定する。"""
        return user_id == self.creator_id

    def is_host_or_admin(self, user_id: int) -> bool:
        """指定ユーザーがゲームの募集者またはBot管理者かを判定する。"""
        return user_id == self.creator_id or user_id in self.admin_user_ids

    # -------------------------------------------------------------
    # ロビーフェーズ
    # -------------------------------------------------------------
    async def add_player(self, user_id: int, display_name: str, mention: str) -> bool:
        """プレイヤーを参加させる。"""
        async with self._lock:
            if self.phase != GamePhase.LOBBY:
                return False
            if user_id in self.players:
                return False
            self.players[user_id] = PlayerState(
                user_id=user_id,
                display_name=display_name,
                mention=mention,
            )
            log.info("Player %s (%s) joined tier game in %s", display_name, user_id, self.channel_id)
            return True

    async def remove_player(self, user_id: int) -> bool:
        """プレイヤーの参加を取り消す。"""
        async with self._lock:
            if self.phase != GamePhase.LOBBY:
                return False
            if user_id not in self.players:
                return False
            del self.players[user_id]
            log.info("Player %s left tier game in %s", user_id, self.channel_id)
            return True

    async def update_lobby_message(self) -> None:
        """ロビーの募集メッセージを更新する。"""
        if self.main_message is None:
            return
        is_ready = len(self.players) >= 2
        embed = render_lobby_embed(self.creator_name, list(self.players.values()), is_ready)
        try:
            await self.main_message.edit(embed=embed)
        except Exception as e:
            log.warning("Failed to edit lobby message in %s: %r", self.channel_id, e)

    async def handle_lobby_timeout(self) -> None:
        """ロビー募集がタイムアウトした場合の処理。"""
        async with self._lock:
            if self.phase != GamePhase.LOBBY:
                return
            self.phase = GamePhase.GAME_OVER
            if self.main_message:
                try:
                    await self.main_message.edit(
                        content="⏰ **募集時間がタイムアウトしました。ゲームを終了します。**",
                        view=None,
                    )
                except Exception:
                    pass
        await self._cleanup()

    # -------------------------------------------------------------
    # ステップ1: 正解を作る（各自のTier表作成）
    # -------------------------------------------------------------
    async def start_game(self) -> None:
        """ロビーを終了し、ステップ1: 全員に別々のお題を配って正解Tier表を作成するフェーズを開始する。"""
        from .views import TierAnswerSetupView

        async with self._lock:
            if len(self.players) < 2:
                return
            self.phase = GamePhase.CREATING_ANSWERS
            # 主役の順番をシャッフル決定
            player_ids = list(self.players.keys())
            random.shuffle(player_ids)
            self.host_order = player_ids
            self.total_rounds = len(player_ids)
            self.current_round = 0

        # 全員に別々のお題を生成・配布（ロック外で取得）
        guild = getattr(self.channel, "guild", None)
        gid = getattr(guild, "id", None) if guild is not None else None
        cid = self.channel_id
        player_ids = list(self.players.keys())

        topics = await generate_multiple_topics(
            count=len(self.players),
            bot=self.bot,
            guild_id=gid,
            channel_id=cid,
            player_user_ids=player_ids,
        )

        async with self._lock:
            for p, topic in zip(self.players.values(), topics):
                p.secret_topic = topic
                shuffled = list(topic.options)
                random.shuffle(shuffled)
                p.shuffled_options = shuffled

            embed = render_answer_setup_embed(
                players=list(self.players.values()),
                submitted_user_ids=set(),
            )
            view = TierAnswerSetupView(self)
            try:
                msg = await self.channel.send(
                    content=(
                        f"🎮 **【ステップ1: 正解を作る】**\n"
                        f"参加者全員に別々のお題と5つのワードを配りました！\n"
                        f"下のボタンから自分の感覚でS〜DのTier表を作成してください（他の人には見えません）！"
                    ),
                    embed=embed,
                    view=view,
                )
                self.main_message = msg
            except Exception as e:
                log.exception("Failed to send answer setup message: %s", e)

    async def submit_secret_answer(
        self,
        user_id: int,
        user_name: str,
        tier_assignments: dict[str, str],
    ) -> bool:
        """プレイヤーが自分のお題に対する正解Tier表を提出する。全員揃ったら第1ラウンドへ進む。"""
        player = self.players.get(user_id)
        if player is None or player.secret_topic is None:
            return False

        if not is_valid_tier_assignment(tier_assignments, player.secret_topic.options):
            log.warning("Rejected invalid secret tier submission from %s: %r", user_id, tier_assignments)
            return False

        all_done = False
        async with self._lock:
            if self.phase != GamePhase.CREATING_ANSWERS:
                return False

            player.secret_submission = PlayerSubmission(
                user_id=user_id,
                user_name=user_name,
                tier_assignments=tier_assignments,
                submitted_at=time.time(),
            )
            log.info(
                "Player %s completed secret tier table (%d/%d)",
                user_name,
                sum(1 for p in self.players.values() if p.secret_submission is not None),
                len(self.players),
            )

            # メインメッセージの作成状況を更新
            if self.main_message is not None:
                done_uids = {p.user_id for p in self.players.values() if p.secret_submission is not None}
                embed = render_answer_setup_embed(
                    players=list(self.players.values()),
                    submitted_user_ids=done_uids,
                )
                try:
                    await self.main_message.edit(embed=embed)
                except Exception as e:
                    log.warning("Failed to update answer setup embed: %r", e)

            # 全員作成完了したかチェック
            all_done = all(p.secret_submission is not None for p in self.players.values())

        if all_done:
            await asyncio.sleep(1.0)
            try:
                await self.channel.send("✨ **全員の正解Tier表が完成しました！いよいよゲームを開始します！**")
            except Exception as e:
                log.warning("Failed to send secret answer completion message: %r", e)
            try:
                await self._start_round_guess()
            except Exception as e:
                log.exception("Error starting round guess in submit_secret_answer: %s", e)

        return True

    # -------------------------------------------------------------
    # ステップ2: お題公開 & ステップ3: 180秒予想フェーズ
    # -------------------------------------------------------------
    async def _start_round_guess(self) -> None:
        """主役のお題とワード（順不同）を公開し、180秒の予想受付を開始する。"""
        from .views import TierRoundGuessView

        async with self._lock:
            self.current_round += 1
            if self.current_round > self.total_rounds:
                await self.finish_game()
                return

            self.phase = GamePhase.ROUND_GUESS
            self.current_guesses.clear()
            self.current_submissions.clear()

            # 今回の主役を決定
            self.current_host_id = self.host_order[self.current_round - 1]
            host_player = self.players[self.current_host_id]
            self.current_host_name = host_player.display_name
            host_player.rounds_as_host += 1

            for p in self.players.values():
                if p.user_id != self.current_host_id:
                    p.rounds_as_guesser += 1

            self.current_topic = host_player.secret_topic or TopicData("未定", [])
            self.current_shuffled_options = list(host_player.shuffled_options or self.current_topic.options)

            # 180秒タイマーの設定
            duration = 180.0
            self.guess_end_timestamp = int(time.time() + duration)
            self._cancel_timer()
            self._guess_timer_task = asyncio.create_task(self._guess_countdown_task(duration))

            embed = render_round_guess_embed(
                round_number=self.current_round,
                total_rounds=self.total_rounds,
                host_name=self.current_host_name,
                topic=self.current_topic,
                shuffled_options=self.current_shuffled_options,
                end_timestamp=self.guess_end_timestamp,
                submitted_user_ids=set(),
                all_players=list(self.players.values()),
                current_host_id=self.current_host_id,
            )
            view = TierRoundGuessView(self)
            try:
                msg = await self.channel.send(
                    content=(
                        f"📢 **【第 {self.current_round} / {self.total_rounds} ラウンド 開始！】**\n"
                        f"👑 今回の主役: **{host_player.mention}** さん！\n"
                        f"主役のお題と5つのワード（順不同）が公開されました！\n"
                        f"主役以外の全員は **180秒以内** に「主役ならどう並べるか」を予想してください！"
                    ),
                    embed=embed,
                    view=view,
                )
                self.main_message = msg
            except Exception as e:
                log.exception("Failed to send round guess message: %s", e)

    async def _guess_countdown_task(self, duration: float) -> None:
        """180秒の制限時間を計測するバックグラウンドタスク。"""
        try:
            await asyncio.sleep(duration)
            async with self._lock:
                if self.phase != GamePhase.ROUND_GUESS:
                    return
            try:
                await self.channel.send("⏰ **制限時間（180秒）が終了しました！結果発表に進みます！**")
            except Exception:
                pass
            await self._reveal_round_result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("Error in guess countdown task: %s", e)

    async def submit_guess(
        self,
        user_id: int,
        user_name: str,
        tier_assignments: dict[str, str],
    ) -> bool:
        """参加者からの予想提出を受け付け、主役以外全員が揃ったら結果発表へ進む。"""
        if user_id == self.current_host_id:
            return False

        expected_opts = self.current_shuffled_options or self.current_topic.options
        if not is_valid_tier_assignment(tier_assignments, expected_opts):
            log.warning("Rejected invalid guess submission from %s: %r", user_id, tier_assignments)
            return False

        all_done = False
        async with self._lock:
            if self.phase != GamePhase.ROUND_GUESS:
                return False

            self.current_guesses[user_id] = tier_assignments
            self.current_submissions[user_id] = PlayerSubmission(
                user_id=user_id,
                user_name=user_name,
                tier_assignments=tier_assignments,
                submitted_at=time.time(),
            )
            log.info(
                "Player %s submitted guess for round %s (total: %s/%s)",
                user_name,
                self.current_round,
                len(self.current_guesses),
                len(self.players) - 1,
            )

            # メインメッセージの提出状況表示を更新
            if self.main_message is not None:
                embed = render_round_guess_embed(
                    round_number=self.current_round,
                    total_rounds=self.total_rounds,
                    host_name=self.current_host_name,
                    topic=self.current_topic,
                    shuffled_options=self.current_shuffled_options,
                    end_timestamp=self.guess_end_timestamp,
                    submitted_user_ids=set(self.current_guesses.keys()),
                    all_players=list(self.players.values()),
                    current_host_id=self.current_host_id,
                )
                try:
                    await self.main_message.edit(embed=embed)
                except Exception as e:
                    log.warning("Failed to update round guess embed: %r", e)

            # 主役以外の全員が提出したかチェック
            needed = len(self.players) - 1
            if len(self.current_guesses) >= needed:
                all_done = True

        if all_done:
            self._cancel_timer()
            await asyncio.sleep(1.0)
            try:
                await self._reveal_round_result()
            except Exception as e:
                log.exception("Error revealing round result in submit_guess: %s", e)

        return True

    async def submit_tiers(
        self,
        user_id: int,
        user_name: str,
        tier_assignments: dict[str, str],
    ) -> bool:
        """互換用ラッパー: フェーズに応じて正解提出または予想提出に振り分ける。"""
        if self.phase == GamePhase.CREATING_ANSWERS:
            return await self.submit_secret_answer(user_id, user_name, tier_assignments)
        return await self.submit_guess(user_id, user_name, tier_assignments)

    # -------------------------------------------------------------
    # ステップ4: 結果発表
    # -------------------------------------------------------------
    async def _reveal_round_result(self) -> None:
        """主役の本当の並びと各プレイヤーの予想結果を発表する。"""
        from .views import TierRoundResultView

        self._cancel_timer()

        async with self._lock:
            if self.phase != GamePhase.ROUND_GUESS:
                return
            self.phase = GamePhase.ROUND_RESULT

            host_player = self.players[self.current_host_id]
            if host_player.secret_submission is not None:
                host_tiers = host_player.secret_submission.tier_assignments
            else:
                host_tiers = {opt: "B" for opt in self.current_topic.options}

            result = calculate_round_result(
                round_number=self.current_round,
                host_id=self.current_host_id,
                host_name=self.current_host_name,
                topic=self.current_topic,
                host_tiers=host_tiers,
                guesses=dict(self.current_guesses),
                shuffled_options=self.current_shuffled_options,
            )
            self.round_results.append(result)

            # 各プレイヤーの合計スコアとパーフェクトカウントを加算
            for uid in result.perfect_users:
                if uid in self.players:
                    self.players[uid].perfect_count += 1

            for uid, score in result.round_scores.items():
                if uid in self.players:
                    self.players[uid].total_score += score

            embed = render_round_result_embed(result, list(self.players.values()))
            view = TierRoundResultView(self)

            try:
                await self.channel.send(
                    content=f"🎊 **第 {self.current_round} ラウンドの結果発表です！**",
                    embed=embed,
                    view=view,
                )
            except Exception as e:
                log.exception("Failed to send round result embed: %s", e)

    async def advance_to_next_round(self) -> None:
        """次のラウンドに進むか、全ラウンド終了なら最終結果を発表する。"""
        if self.current_round >= self.total_rounds:
            await self.finish_game()
        else:
            await self._start_round_guess()

    # -------------------------------------------------------------
    # ゲーム終了 & 最終結果・相性診断
    # -------------------------------------------------------------
    async def finish_game(self) -> None:
        """全ラウンド終了時の最終結果・ベスト相性度診断を発表する。"""
        from .views import TierFinalView

        async with self._lock:
            self.phase = GamePhase.GAME_OVER
            players_dict = {p.user_id: p.display_name for p in self.players.values()}
            pairs = evaluate_compatibility(self.round_results, players_dict)

            embed = render_final_result_embed(list(self.players.values()), pairs)
            view = TierFinalView(self)

            try:
                await self.channel.send(
                    content="👑 **ティア表パーティゲームの全ラウンドが終了しました！**",
                    embed=embed,
                    view=view,
                )
            except Exception as e:
                log.exception("Failed to send final results embed: %s", e)

        await self._cleanup()

    async def restart_new_game(self, user: discord.User | discord.Member) -> None:
        """同じチャンネルで新しいゲームを開始する。"""
        from .views import TierLobbyView

        self._cancel_timer()

        async with self._lock:
            self.creator_id = user.id
            self.creator_name = user.display_name
            self.phase = GamePhase.LOBBY
            self.players.clear()
            self.host_order.clear()
            self.current_round = 0
            self.total_rounds = 0
            self.round_results.clear()
            self.current_submissions.clear()
            self.current_guesses.clear()
            self.current_shuffled_options.clear()

            self.players[user.id] = PlayerState(
                user_id=user.id,
                display_name=user.display_name,
                mention=user.mention,
            )

            embed = render_lobby_embed(self.creator_name, list(self.players.values()), False)
            view = TierLobbyView(self)
            try:
                msg = await self.channel.send(
                    content="🎮 **新しいティア表パーティゲームを開始します！参加者募集中！**",
                    embed=embed,
                    view=view,
                )
                self.main_message = msg
            except Exception as e:
                log.exception("Failed to send restart lobby message: %s", e)

    async def cancel_game(self) -> None:
        """ゲームを中断・キャンセルする。"""
        self._cancel_timer()
        async with self._lock:
            self.phase = GamePhase.GAME_OVER
            if self.main_message:
                try:
                    await self.main_message.edit(view=None)
                except Exception:
                    pass
        await self._cleanup()

    async def _cleanup(self) -> None:
        """終了時のクリーンアップ処理（親Cogへの通知）。"""
        if self.on_cleanup:
            try:
                await self.on_cleanup(self.channel_id)
            except Exception as e:
                log.warning("Cleanup callback error for channel %s: %r", self.channel_id, e)
