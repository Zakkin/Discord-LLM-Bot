"""ティア表パーティゲームのDiscord UIコンポーネント（View & Button）。"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Coroutine

import discord
from discord import ui

from .models import (
    TIER_EMOJIS,
    TIER_LABELS,
    TIER_RANKS,
    PlayerState,
    TopicData,
    is_valid_tier_assignment,
)
from .renderer import render_tier_table_text

if TYPE_CHECKING:
    from .session import TierGameSession

log = logging.getLogger("tier_game.views")


class TierLobbyView(ui.View):
    """参加者募集フェーズのロビーView。"""

    def __init__(self, session: TierGameSession, timeout: float = 300.0) -> None:
        super().__init__(timeout=timeout)
        self.session = session

    @ui.button(label="参加する", style=discord.ButtonStyle.success, emoji="✋")
    async def join_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        user = interaction.user
        added = await self.session.add_player(user.id, user.display_name, user.mention)
        if added:
            await interaction.response.send_message(
                f"✅ **{user.display_name}** さんがゲームに参加しました！",
                ephemeral=True,
            )
            await self.session.update_lobby_message()
        else:
            await interaction.response.send_message(
                "すでに参加登録されています！",
                ephemeral=True,
            )

    @ui.button(label="参加取消", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def leave_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        user = interaction.user
        removed = await self.session.remove_player(user.id)
        if removed:
            await interaction.response.send_message(
                f"↩️ **{user.display_name}** さんが参加を取り消しました。",
                ephemeral=True,
            )
            await self.session.update_lobby_message()
        else:
            await interaction.response.send_message(
                "参加登録されていません。",
                ephemeral=True,
            )

    @ui.button(label="ゲーム開始", style=discord.ButtonStyle.primary, emoji="🚀")
    async def start_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        user = interaction.user
        if not self.session.is_creator(user.id):
            await interaction.response.send_message(
                f"❌ ゲームを開始できるのはコマンド実行者（**{self.session.creator_name}**）のみです。",
                ephemeral=True,
            )
            return

        if len(self.session.players) < 2:
            await interaction.response.send_message(
                "❌ ゲームを開始するには最低2名の参加者が必要です！",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        self.stop()
        await self.session.start_game()

    @ui.button(label="中止", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        user = interaction.user
        if not self.session.is_host_or_admin(user.id):
            await interaction.response.send_message(
                "❌ 募集を中止できるのは募集者または管理者のみです。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message("🛑 ゲームの募集を中止しました。")
        self.stop()
        await self.session.cancel_game()

    async def on_timeout(self) -> None:
        log.info("Lobby timed out for session in channel %s", self.session.channel_id)
        await self.session.handle_lobby_timeout()


class TierAnswerSetupView(ui.View):
    """ステップ1: 参加者全員が自分のお題で正解Tier表を作成するフェーズのメインView。"""

    def __init__(self, session: TierGameSession, timeout: float = 600.0) -> None:
        super().__init__(timeout=timeout)
        self.session = session

    @ui.button(label="📋 自分のTier表を作成する", style=discord.ButtonStyle.primary, emoji="✍️")
    async def create_secret_tier_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        user_id = interaction.user.id
        if user_id not in self.session.players:
            await interaction.response.send_message(
                "❌ あなたはこのゲームの参加プレイヤーではありません。",
                ephemeral=True,
            )
            return

        player = self.session.players[user_id]
        if player.secret_topic is None:
            await interaction.response.send_message(
                "⚠️ あなたのお題がまだ準備できていません。少々お待ちください。",
                ephemeral=True,
            )
            return

        # 既に作成済みの場合は確認・再編集
        if player.secret_submission is not None:
            preview = render_tier_table_text(player.secret_submission.tier_assignments)
            view = TierAlreadySubmittedView(
                self.session,
                player.secret_submission.tier_assignments,
                is_secret_setup=True,
            )
            await interaction.response.send_message(
                f"🔒 あなたはすでに以下の正解Tier表を作成済みです！\n\n{preview}\n\n作り直しますか？",
                view=view,
                ephemeral=True,
            )
            return

        input_view = TierInputStepView(
            session=self.session,
            user_id=user_id,
            user_name=interaction.user.display_name,
            is_secret_setup=True,
        )
        content = input_view.build_current_message()
        await interaction.response.send_message(content, view=input_view, ephemeral=True)

    @ui.button(label="🛑 ゲーム中断", style=discord.ButtonStyle.secondary, emoji="⏹️")
    async def stop_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        user = interaction.user
        if not self.session.is_host_or_admin(user.id):
            await interaction.response.send_message(
                "❌ ゲームを中断できるのはホストまたは管理者のみです。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message("🛑 ゲームを中断しました。")
        self.stop()
        await self.session.cancel_game()


class TierRoundGuessView(ui.View):
    """ステップ2 & 3: お題公開 & 180秒予想フェーズのメインView。"""

    def __init__(self, session: TierGameSession, timeout: float = 300.0) -> None:
        super().__init__(timeout=timeout)
        self.session = session

    @ui.button(label="🎯 主役の並びを予想する", style=discord.ButtonStyle.primary, emoji="🧠")
    async def guess_tier_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        user_id = interaction.user.id
        if user_id not in self.session.players:
            await interaction.response.send_message(
                "❌ あなたはこのゲームの参加プレイヤーではありません。",
                ephemeral=True,
            )
            return

        # 主役本人は予想できない
        if user_id == self.session.current_host_id:
            await interaction.response.send_message(
                f"👑 あなたは今回の【主役】です！他の参加者全員の予想が終わるまでお待ちください。",
                ephemeral=True,
            )
            return

        # 既に予想提出済みの場合は再編集または確認
        existing_guess = self.session.current_guesses.get(user_id)
        if existing_guess is not None:
            preview = render_tier_table_text(existing_guess)
            view = TierAlreadySubmittedView(
                self.session,
                existing_guess,
                is_secret_setup=False,
            )
            await interaction.response.send_message(
                f"🎯 あなたはすでに以下の予想を提出済みです！\n\n{preview}\n\n予想を変更しますか？",
                view=view,
                ephemeral=True,
            )
            return

        input_view = TierInputStepView(
            session=self.session,
            user_id=user_id,
            user_name=interaction.user.display_name,
            is_secret_setup=False,
        )
        content = input_view.build_current_message()
        await interaction.response.send_message(content, view=input_view, ephemeral=True)

    @ui.button(label="🛑 ゲーム中断", style=discord.ButtonStyle.secondary, emoji="⏹️")
    async def stop_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        user = interaction.user
        if not self.session.is_host_or_admin(user.id):
            await interaction.response.send_message(
                "❌ ゲームを中断できるのはホストまたは管理者のみです。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message("🛑 ゲームを中断しました。")
        self.stop()
        await self.session.cancel_game()


# 後方互換用エイリアス
TierRoundMainView = TierRoundGuessView


class TierAlreadySubmittedView(ui.View):
    """提出済みのプレイヤーが再入力するためのView。"""

    def __init__(
        self,
        session: TierGameSession,
        current_assignments: dict[str, str],
        is_secret_setup: bool = False,
    ) -> None:
        super().__init__(timeout=120.0)
        self.session = session
        self.current_assignments = current_assignments
        self.is_secret_setup = is_secret_setup

    @ui.button(label="🔄 最初から作り直す", style=discord.ButtonStyle.danger)
    async def redo_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        input_view = TierInputStepView(
            session=self.session,
            user_id=interaction.user.id,
            user_name=interaction.user.display_name,
            is_secret_setup=self.is_secret_setup,
        )
        content = input_view.build_current_message()
        await interaction.response.edit_message(content=content, view=input_view)

    @ui.button(label="このままでOK", style=discord.ButtonStyle.secondary)
    async def keep_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.edit_message(content="✅ 現在の提出内容を維持します！", view=None)


class TierInputStepView(ui.View):
    """ステップ式に1アイテムずつS〜Dを選択するエフェメラル対話View。"""

    def __init__(
        self,
        session: TierGameSession,
        user_id: int,
        user_name: str,
        is_secret_setup: bool = False,
        timeout: float = 240.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.session = session
        self.user_id = user_id
        self.user_name = user_name
        self.is_secret_setup = is_secret_setup
        self.current_index: int = 0
        self.tier_assignments: dict[str, str] = {}
        self._processing: bool = False
        self._setup_buttons()

    @property
    def topic(self) -> TopicData:
        if self.is_secret_setup:
            player = self.session.players.get(self.user_id)
            if player and player.secret_topic:
                return player.secret_topic
        return self.session.current_topic

    @property
    def options(self) -> list[str]:
        if self.is_secret_setup:
            return self.topic.options
        if self.session.current_shuffled_options:
            return self.session.current_shuffled_options
        return self.topic.options

    @property
    def is_host(self) -> bool:
        return self.user_id == self.session.current_host_id

    def _setup_buttons(self) -> None:
        self.clear_items()
        used_ranks = set(self.tier_assignments.values())

        for rank in TIER_RANKS:
            emoji = TIER_EMOJIS.get(rank, "⬜")
            is_used = rank in used_ranks
            if is_used:
                # 既に別の選択肢に設定済みのティアは無効化（各ティア1回のみ選択可能）
                btn = ui.Button(
                    label=f"{rank} (設定済)",
                    style=discord.ButtonStyle.secondary,
                    emoji=emoji,
                    disabled=True,
                    row=0,
                )
            else:
                btn = ui.Button(
                    label=f"{rank} ランク",
                    style=discord.ButtonStyle.primary if rank in ("S", "A") else discord.ButtonStyle.secondary,
                    emoji=emoji,
                    custom_id=f"tier_select_{rank}",
                    disabled=False,
                    row=0,
                )
                btn.callback = self._make_tier_callback(rank)
            self.add_item(btn)

        # 1つ前の選択に戻るボタン（ステップ2以降で表示）
        if self.current_index > 0:
            back_btn = ui.Button(
                label="↩️ 1つ前の選択に戻る",
                style=discord.ButtonStyle.danger,
                row=1,
            )
            back_btn.callback = self._make_back_callback()
            self.add_item(back_btn)

    def _make_tier_callback(self, rank: str) -> Callable[[discord.Interaction], Coroutine[Any, Any, None]]:
        async def _callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("❌ 他のユーザーの操作です。", ephemeral=True)
                return

            if self._processing:
                if not interaction.response.is_done():
                    await interaction.response.defer()
                return

            self._processing = True
            try:
                if rank in self.tier_assignments.values():
                    await interaction.response.send_message(
                        f"⚠️ **{rank} ランク** は既に別の選択肢に設定されています。\n各ティアは1つの選択肢のみ設定できます。",
                        ephemeral=True,
                    )
                    return

                opts = self.options
                if self.current_index >= len(opts):
                    if not interaction.response.is_done():
                        await interaction.response.defer()
                    return

                current_item = opts[self.current_index]
                self.tier_assignments[current_item] = rank
                self.current_index += 1

                if self.current_index < len(opts):
                    # 次のアイテムの選択へ（使用済みティアをdisabled化）
                    self._setup_buttons()
                    content = self.build_current_message()
                    await interaction.response.edit_message(content=content, view=self)
                else:
                    # 5アイテムすべて完了 ➔ 確認Viewへ
                    self.stop()
                    confirm_view = TierConfirmView(
                        session=self.session,
                        user_id=self.user_id,
                        user_name=self.user_name,
                        tier_assignments=self.tier_assignments,
                        is_secret_setup=self.is_secret_setup,
                    )
                    content = confirm_view.build_confirm_message()
                    await interaction.response.edit_message(content=content, view=confirm_view)
            except Exception as e:
                log.exception("Error in TierInputStepView callback: %s", e)
                if not interaction.response.is_done():
                    await interaction.response.send_message("⚠️ エラーが発生しました。もう一度お試しください。", ephemeral=True)
            finally:
                self._processing = False

        return _callback

    def _make_back_callback(self) -> Callable[[discord.Interaction], Coroutine[Any, Any, None]]:
        async def _callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("❌ 他のユーザーの操作です。", ephemeral=True)
                return

            if self._processing:
                if not interaction.response.is_done():
                    await interaction.response.defer()
                return

            self._processing = True
            try:
                if self.current_index <= 0:
                    if not interaction.response.is_done():
                        await interaction.response.defer()
                    return

                opts = self.options
                self.current_index -= 1
                prev_item = opts[self.current_index]
                self.tier_assignments.pop(prev_item, None)

                self._setup_buttons()
                content = self.build_current_message()
                await interaction.response.edit_message(content=content, view=self)
            except Exception as e:
                log.exception("Error in TierInputStepView back callback: %s", e)
                if not interaction.response.is_done():
                    await interaction.response.send_message("⚠️ エラーが発生しました。もう一度お試しください。", ephemeral=True)
            finally:
                self._processing = False

        return _callback

    def build_current_message(self) -> str:
        opts = self.options
        current_item = opts[self.current_index]

        if self.is_secret_setup:
            role_desc = (
                "🔒 **あなただけの特別なお題です！**\n"
                "自分の直感・好みに従ってS〜Dに格付けしてください！\n"
                "（※他のプレイヤーには非公開です）"
            )
        else:
            role_desc = (
                f"🧠 **主役: {self.session.current_host_name} さん**\n"
                f"主役ならどう並べるかを180秒以内に予想してください！"
            )

        history_lines: list[str] = []
        for i in range(self.current_index):
            item = opts[i]
            rank = self.tier_assignments.get(item, "?")
            emoji = TIER_EMOJIS.get(rank, "")
            history_lines.append(f"・{item} ➔ {emoji} **{rank}**")

        history_str = "\n".join(history_lines) if history_lines else "（まだ選択していません）"

        return (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 **お題: 『 {self.topic.topic} 』**\n"
            f"{role_desc}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"【ステップ {self.current_index + 1} / 5】\n"
            f"👉 **『 {current_item} 』** のティアを選んでください：\n"
            f"（※各ティアは1つの選択肢にのみ設定可能です）\n\n"
            f"**現在の選択履歴:**\n{history_str}\n"
        )


class TierConfirmView(ui.View):
    """選択完了後のプレビュー確認 & 提出確定View。"""

    def __init__(
        self,
        session: TierGameSession,
        user_id: int,
        user_name: str,
        tier_assignments: dict[str, str],
        is_secret_setup: bool = False,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.session = session
        self.user_id = user_id
        self.user_name = user_name
        self.tier_assignments = tier_assignments
        self.is_secret_setup = is_secret_setup
        self._submitted: bool = False

    def build_confirm_message(self) -> str:
        table_str = render_tier_table_text(self.tier_assignments)
        role_label = "🔒 あなたの正解ティア表" if self.is_secret_setup else f"🎯 あなたの予想ティア表 (主役: {self.session.current_host_name}さん)"
        return (
            f"✨ **{role_label}が完成しました！**\n\n"
            f"{table_str}\n\n"
            f"この内容で提出してよろしいですか？"
        )

    @ui.button(label="✅ この内容で確定・提出", style=discord.ButtonStyle.success, emoji="📤")
    async def submit_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 他のユーザーの操作です。", ephemeral=True)
            return

        if self._submitted:
            if not interaction.response.is_done():
                await interaction.response.send_message("すでに提出処理済みです。", ephemeral=True)
            return

        player = self.session.players.get(self.user_id)
        if self.is_secret_setup and player is not None and player.secret_topic is not None:
            expected_opts = player.secret_topic.options
        else:
            expected_opts = self.session.current_shuffled_options or self.session.current_topic.options

        if not is_valid_tier_assignment(self.tier_assignments, expected_opts):
            await interaction.response.send_message(
                "❌ エラー: 各ティア（S, A, B, C, D）を重複なく1つずつ設定してください。",
                ephemeral=True,
            )
            return

        self._submitted = True
        self.stop()

        # 最優先でユーザーへ即時応答（edit_message）を行い、Discord APIの3秒タイムアウトを完全に防止する！
        if self.is_secret_setup:
            await interaction.response.edit_message(
                content="✅ **あなたの正解Tier表を作成・保存しました！**\n全員の作成が完了すると、自動的に第1ラウンドのお題公開に進みます！",
                view=None,
            )
            try:
                success = await self.session.submit_secret_answer(self.user_id, self.user_name, self.tier_assignments)
                if not success:
                    log.warning("submit_secret_answer returned False for user %s", self.user_id)
            except Exception as e:
                log.exception("Error while processing submit_secret_answer for %s: %s", self.user_id, e)
        else:
            await interaction.response.edit_message(
                content="✅ **予想の提出が完了しました！**\n制限時間（180秒）が終了するか、全員が提出すると結果発表に進みます！",
                view=None,
            )
            try:
                success = await self.session.submit_guess(self.user_id, self.user_name, self.tier_assignments)
                if not success:
                    log.warning("submit_guess returned False for user %s", self.user_id)
            except Exception as e:
                log.exception("Error while processing submit_guess for %s: %s", self.user_id, e)

    @ui.button(label="🔄 最初から選び直す", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def redo_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 他のユーザーの操作です。", ephemeral=True)
            return

        if self._submitted:
            if not interaction.response.is_done():
                await interaction.response.send_message("すでに提出済みのため選び直せません。", ephemeral=True)
            return

        try:
            input_view = TierInputStepView(
                session=self.session,
                user_id=self.user_id,
                user_name=self.user_name,
                is_secret_setup=self.is_secret_setup,
            )
            content = input_view.build_current_message()
            self.stop()
            await interaction.response.edit_message(content=content, view=input_view)
        except Exception as e:
            log.exception("Error in redo_button: %s", e)
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ エラーが発生しました。もう一度お試しください。", ephemeral=True)


class TierRoundResultView(ui.View):
    """ラウンド結果メッセージのView。"""

    def __init__(self, session: TierGameSession, timeout: float = 300.0) -> None:
        super().__init__(timeout=timeout)
        self.session = session

    @ui.button(label="▶️ 次のラウンドへ進む", style=discord.ButtonStyle.primary, emoji="⏩")
    async def next_round_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        user = interaction.user
        if not self.session.is_host_or_admin(user.id):
            await interaction.response.send_message(
                "❌ 次のラウンドへ進められるのはホストまたは管理者のみです。",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        self.stop()
        await self.session.advance_to_next_round()

    @ui.button(label="🛑 ゲームを終了する", style=discord.ButtonStyle.secondary, emoji="⏹️")
    async def end_game_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        user = interaction.user
        if not self.session.is_host_or_admin(user.id):
            await interaction.response.send_message(
                "❌ ゲームを終了できるのはホストまたは管理者のみです。",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        self.stop()
        await self.session.finish_game()


class TierFinalView(ui.View):
    """最終結果メッセージのView。"""

    def __init__(self, session: TierGameSession, timeout: float = 300.0) -> None:
        super().__init__(timeout=timeout)
        self.session = session

    @ui.button(label="🔄 もう一度遊ぶ", style=discord.ButtonStyle.success, emoji="🎉")
    async def play_again_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.defer()
        self.stop()
        # 同じチャンネルで新セッションのロビーを開始
        await self.session.restart_new_game(interaction.user)
