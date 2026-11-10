"""言葉から画像を検索し、AIの目で選んで表示する /image_find コマンド Mixin。"""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, TYPE_CHECKING
from urllib.parse import quote_plus

import discord
from discord import app_commands

from ollama_bot.common.config_helpers import cfg
from ollama_bot.common.http_session import get_shared_http_session
from ollama_bot.common.ollama_helpers import call_ollama
from front.discord.common.message_media import (
    _download_bytes,
    score_image_candidates,
)

if TYPE_CHECKING:
    from ollama_bot.ollama_chat_.ollama_chat_types import OllamaChatProtocol
    _ImageFindBase = OllamaChatProtocol
else:
    _ImageFindBase = object

log = logging.getLogger("front.discord.commands.image_find")


class OllamaChatImageFindMixin(_ImageFindBase):
    """言葉から画像を検索し、AIの目で選んで表示する Mixin"""

    @app_commands.command(
        name="image_find",
        description="言葉から画像を検索し、AIの目で選んで表示します",
    )
    @app_commands.describe(
        query="探したい画像の特徴やシチュエーション（例: 青髪ツインテールの魔法少女）",
    )
    async def slash_image_find(
        self,
        interaction: discord.Interaction,
        query: str,
    ) -> None:
        """スラッシュコマンド /image_find のハンドラー (BAN-161, BAN-162準拠)"""
        # 1. 冒頭で defer (thinking=True)
        await interaction.response.defer(thinking=True)

        user = getattr(interaction, "user", None)
        log.info(
            "[IMAGE_FIND_CMD] Slash /image_find triggered: user=%s (id=%s), query='%s'",
            user,
            getattr(user, "id", None),
            query,
        )

        try:
            # 2. 中位モデルで Danbooru タグと補助キーワードを抽出 (JSON)
            middle_model = str(cfg("OLLAMA_MIDDLE_MODEL", "middle-llm") or "middle-llm").strip()
            system_prompt = (
                "あなたはDanbooruのタグ検索用エキスパートです。\n"
                "ユーザーが希望する画像の説明から、Danbooruの検索に適した英語タグ（小文字、スペース区切り、単語内スペースはアンダースコア）を抽出してください。\n"
                "キャラクターの特徴（髪色、髪型、服装、ポーズ等）や作品名など、検索精度を高める重要なタグを2〜5個程度選定してください。\n"
                "以下のJSON形式【のみ】を出力してください。前置きや解説は一切不要です。\n"
                '{"tags": ["1girl", "blue_hair", "twintails"], "query_summary": "青髪ツインテールの少女"}'
            )
            user_prompt = f"希望する画像: {query}\nDanbooru検索用のタグをJSON形式で抽出してください。"

            tags_list: list[str] = []
            try:
                resp = await call_ollama(
                    user_prompt,
                    system_prompt=system_prompt,
                    think=False,
                    model=middle_model,
                    temperature=0.1,
                    num_predict=128,
                    timeout_sec=float(cfg("IMAGE_FIND_TRANSLATE_TIMEOUT_SEC", 15.0) or 15.0),
                    retries=1,
                )
                m = re.search(r"\{.*\}", resp or "", re.DOTALL)
                if m:
                    parsed = json.loads(m.group(0))
                    raw_tags = parsed.get("tags")
                    if isinstance(raw_tags, list):
                        tags_list = [str(t).strip().lower() for t in raw_tags if str(t).strip()]
                    elif isinstance(raw_tags, str):
                        tags_list = [t.strip().lower() for t in raw_tags.split() if t.strip()]
            except Exception as e:
                log.warning("[image_find] Tag extraction LLM failed: %r", e)

            # タグが空の場合のフォールバック
            if not tags_list:
                cleaned_words = [w.lower() for w in re.findall(r"[a-zA-Z0-9_]+", query) if len(w) >= 2]
                tags_list = cleaned_words[:3] if cleaned_words else ["1girl"]

            # 3. Danbooru API GETリクエスト。IMAGE_FIND_ALLOW_EXPLICIT が偽なら -rating:explicit をタグに強制付加
            allow_explicit = bool(cfg("IMAGE_FIND_ALLOW_EXPLICIT", False))
            if not allow_explicit and "-rating:explicit" not in tags_list:
                tags_list.append("-rating:explicit")

            tag_query = " ".join(tags_list)
            danbooru_url = f"https://danbooru.donmai.us/posts.json?tags={quote_plus(tag_query)}&limit=20"
            log.info("[image_find] Querying Danbooru: url='%s'", danbooru_url)

            posts: list[dict[str, Any]] = []
            headers = {"User-Agent": "discord-ai-bot/1.0"}
            try:
                session = await get_shared_http_session(timeout_sec=10.0, headers=headers)
                async with session.get(danbooru_url) as http_resp:
                    if http_resp.status == 200:
                        data = await http_resp.json()
                        if isinstance(data, list):
                            posts = data
                    else:
                        log.warning("[image_find] Danbooru API returned status %s", http_resp.status)
            except Exception as e:
                log.exception("[image_find] Danbooru API request failed: %r", e)

            # 4. 候補が0件なら「見つかりませんでした」と翻訳タグを添えて送信
            if not posts:
                await interaction.followup.send(
                    f"ぴゃっ…！条件に合う画像が見つかりませんでした…！\n"
                    f"（検索タグ: `{tag_query}`）"
                )
                return

            # 5. 候補画像URLを _download_bytes で取得し Base64 化
            fetch_limit = int(cfg("IMAGE_FIND_FETCH_LIMIT", 10) or 10)
            downloaded_candidates: list[dict[str, Any]] = []
            image_b64s: list[str] = []

            for post in posts[:fetch_limit]:
                img_url = post.get("large_file_url") or post.get("file_url") or post.get("preview_file_url")
                if not img_url or not isinstance(img_url, str):
                    continue
                if img_url.startswith("/"):
                    img_url = f"https://danbooru.donmai.us{img_url}"

                data = await _download_bytes(img_url, max_bytes=10 * 1024 * 1024, timeout_sec=5.0)
                if not data:
                    continue

                b64 = base64.b64encode(data).decode("ascii")
                image_b64s.append(b64)
                downloaded_candidates.append({
                    "post": post,
                    "image_url": img_url,
                    "b64": b64,
                    "post_url": f"https://danbooru.donmai.us/posts/{post.get('id')}",
                })

            if not downloaded_candidates:
                await interaction.followup.send(
                    f"ぴゃっ…！候補画像の取得・ダウンロードに失敗しました…！\n"
                    f"（検索タグ: `{tag_query}`）"
                )
                return

            # 6. score_image_candidates(query, image_b64s) で一括採点
            scores = await score_image_candidates(query, image_b64s)
            scored_candidates: list[dict[str, Any]] = []
            for cand, sc in zip(downloaded_candidates, scores):
                cand["match_score"] = sc.get("match_score", 0)
                cand["reason"] = sc.get("reason", "")
                scored_candidates.append(cand)

            scored_candidates.sort(key=lambda x: int(x.get("match_score", 0)), reverse=True)

            # 7. match_score >= IMAGE_FIND_MIN_SCORE（既定60）の候補を点数降順でソートし、
            #    最大 IMAGE_FIND_MAX_RESULTS（既定5）件を Embed で送信
            min_score = int(cfg("IMAGE_FIND_MIN_SCORE", 60) or 60)
            max_results = int(cfg("IMAGE_FIND_MAX_RESULTS", 5) or 5)
            passed = [c for c in scored_candidates if c.get("match_score", 0) >= min_score]

            if passed:
                embeds: list[discord.Embed] = []
                for idx, cand in enumerate(passed[:max_results], 1):
                    score = cand.get("match_score", 0)
                    reason = cand.get("reason", "")
                    post_url = cand.get("post_url", "")
                    image_url = cand.get("image_url", "")
                    post_id = cand["post"].get("id", "")

                    embed = discord.Embed(
                        title=f"🎯 候補 {idx} (スコア: {score}点)",
                        description=f"**判定理由:** {reason or '合致'}\n**出典:** [Danbooru #{post_id}](<{post_url}>)",
                        color=0x2ECC71 if score >= 80 else 0xF1C40F,
                    )
                    if callable(getattr(embed, "set_image", None)):
                        embed.set_image(url=image_url)
                    else:
                        setattr(embed, "_image", {"url": image_url})
                    embed.set_footer(text=f"タグ: {tag_query} | 検索: {query}")
                    embeds.append(embed)

                await interaction.followup.send(
                    content=f"『{query}』の画像検索結果です！（{len(passed)}件合致）",
                    embeds=embeds,
                )
            else:
                # 8. 合格が0件の場合は最高点1件を「一番近い候補（未確認）」として添えて正直に報告
                best = scored_candidates[0]
                score = best.get("match_score", 0)
                reason = best.get("reason", "")
                post_url = best.get("post_url", "")
                image_url = best.get("image_url", "")
                post_id = best["post"].get("id", "")

                embed = discord.Embed(
                    title=f"⚠️ 一番近い候補（未確認 / スコア: {score}点）",
                    description=(
                        f"希望条件に合致する画像（基準: {min_score}点以上）は見つかりませんでした。\n"
                        f"**判定理由:** {reason or '基準点未満'}\n"
                        f"**出典:** [Danbooru #{post_id}](<{post_url}>)"
                    ),
                    color=0xE74C3C,
                )
                if callable(getattr(embed, "set_image", None)):
                    embed.set_image(url=image_url)
                else:
                    setattr(embed, "_image", {"url": image_url})
                embed.set_footer(text=f"タグ: {tag_query} | 検索: {query}")

                await interaction.followup.send(
                    content=f"ぴゃっ…！『{query}』に完全に合致する画像は見つかりませんでした…（基準点 {min_score}点 未満）。\n一番近かった候補を参考までに表示します：",
                    embed=embed,
                )

        except Exception as e:
            log.exception("[image_find] Slash /image_find error: %r", e)
            await interaction.followup.send(
                f"画像検索中にエラーが発生しました…: {e}",
            )


__all__ = ["OllamaChatImageFindMixin"]
