"""AIエージェント・ハーネス 画像検索ツールアダプター (adapters/image_search.py)

画像URLから逆画像検索API（SauceNAO / trace.moe）および自前ビジョン検証を用いて、
写っている人物・キャラクター名・作品名・元ネタ・出典を客観的・正直に特定する ToolDefinition を提供する。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
import urllib.parse
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

import aiohttp

from ..types import ToolCategory, ToolDefinition, ToolParameter, ToolResult

log = logging.getLogger("harness.agent.tool_registry.adapters.image_search")

_IMAGE_URL_EXT_RE = re.compile(r"\.(?:png|jpe?g|webp|gif|bmp)(?:\?.*)?$", re.IGNORECASE)
_DISCORD_CDN_RE = re.compile(
    r"https?://(?:cdn|media)\.discordapp\.(?:com|net)/attachments/[^\s<>\"']+",
    re.IGNORECASE,
)


def _get_cfg(key: str, default: Any = None) -> Any:
    try:
        from ollama_bot.common.config_helpers import cfg

        return cfg(key, default)
    except Exception:
        return default


def extract_image_url_from_context(context: dict[str, Any] | Any = None, **kwargs: Any) -> str:
    """コンテキストや kwargs から最も関連性の高い画像URLを安全に抽出する。"""
    candidates: list[str] = []

    # kwargs からの直接取得
    for key in (
        "image_url",
        "target_image_url",
        "recent_image_url",
        "last_image_url",
        "attachment_url",
        "url",
    ):
        val = kwargs.get(key)
        if val and isinstance(val, str) and (val.startswith("http://") or val.startswith("https://")):
            candidates.append(val.strip())

    # context 辞書からの取得
    if isinstance(context, dict):
        for key in (
            "image_url",
            "target_image_url",
            "recent_image_url",
            "last_image_url",
            "attachment_url",
            "url",
        ):
            val = context.get(key)
            if val and isinstance(val, str) and (val.startswith("http://") or val.startswith("https://")):
                candidates.append(val.strip())

        # attachments / images リスト
        atts = context.get("attachments") or context.get("images")
        if isinstance(atts, (list, tuple)):
            for item in atts:
                if isinstance(item, str) and (item.startswith("http://") or item.startswith("https://")):
                    candidates.append(item.strip())
                elif isinstance(item, dict) and item.get("url"):
                    candidates.append(str(item["url"]).strip())
                elif hasattr(item, "url") and getattr(item, "url"):
                    candidates.append(str(getattr(item, "url")).strip())

        # テキスト内の Discord CDN / 画像URL探索
        for text_key in ("user_text", "prompt", "context_text", "last_message", "message"):
            txt = str(context.get(text_key) or "")
            if not txt:
                continue
            found_discord = _DISCORD_CDN_RE.findall(txt)
            candidates.extend(found_discord)
            for u in re.findall(r"https?://[^\s<>\"']+", txt):
                if _IMAGE_URL_EXT_RE.search(u):
                    candidates.append(u.strip())

    # 画像特有URL（Discord CDNまたは画像拡張子）を最優先
    for cand in candidates:
        if _DISCORD_CDN_RE.search(cand) or _IMAGE_URL_EXT_RE.search(cand):
            return cand

    # それ以外の http/https URL
    for cand in candidates:
        if cand.startswith("http://") or cand.startswith("https://"):
            return cand

    return ""


_IMAGE_MAGIC_PREFIXES: tuple[bytes, ...] = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"BM")


def _safe_url_for_log(url: str) -> str:
    """ログ出力用にURLから署名クエリやパス詳細を除去し、ホスト名とファイル名のみを返す。"""
    if not url or not isinstance(url, str):
        return ""
    try:
        parsed = urlparse(url.strip())
        host = parsed.hostname or ""
        filename = parsed.path.rsplit("/", 1)[-1] if parsed.path else ""
        if host and filename:
            return f"{host}/{filename}"
        return host or filename
    except Exception:
        return ""


def _sanitize_error_msg(err: BaseException, *, image_url: str = "", api_key: str = "") -> str:
    """例外メッセージからAPIキーや署名付きURL・クエリパラメータを除去し短く要約する。"""
    msg = str(err or "")
    if api_key:
        msg = msg.replace(api_key, "***")
        msg = msg.replace(urllib.parse.quote(api_key, safe=""), "***")
    if image_url:
        safe_u = _safe_url_for_log(image_url)
        msg = msg.replace(image_url, safe_u)
        msg = msg.replace(urllib.parse.quote(image_url, safe=""), safe_u)
    msg = re.sub(r"https?://[^\s<>\"']+", lambda m: _safe_url_for_log(m.group(0)), msg)
    return msg[:120]


def _estimate_b64_bytes(b64_str: str) -> int:
    """Base64文字列から元画像のおおよそのバイト数を算出する。"""
    if not b64_str:
        return 0
    try:
        padded = b64_str + "=" * ((4 - len(b64_str) % 4) % 4)
        return len(base64.b64decode(padded, validate=False))
    except Exception:
        return len(b64_str)


def _looks_like_image_bytes(data: bytes) -> bool:
    """ダウンロードしたバイト列が画像らしいか（マジックバイトと最小サイズ）を確認する。"""
    if not data or len(data) < 32:
        return False
    if data.startswith(_IMAGE_MAGIC_PREFIXES):
        return True
    # WebP: RIFF....WEBP
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


async def _read_valid_image_b64(resp: Any, url: str) -> str:
    """HTTP 200 レスポンスから画像バイト列を検証し Base64 文字列を返す。"""
    if resp.status != 200:
        return ""
    data = await resp.read()
    if not isinstance(data, (bytes, bytearray)) or not _looks_like_image_bytes(bytes(data)):
        log.debug(
            "[image_search] Downloaded non-image payload: url='%s', size=%d",
            _safe_url_for_log(url),
            len(data) if data else 0,
        )
        return ""
    return base64.b64encode(data).decode("utf-8")


async def _download_image_as_b64(
    url: str,
    session: Optional[aiohttp.ClientSession] = None,
    timeout_sec: float = 8.0,
) -> str:
    """URLから画像をダウンロードして Base64 文字列を返す。失敗時や非画像時は空文字。"""
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return ""
    host = urlparse(url).hostname or ""
    b64_str = ""
    try:
        if session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as resp:
                b64_str = await _read_valid_image_b64(resp, url)
        else:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec)) as local_session:
                async with local_session.get(url) as resp:
                    b64_str = await _read_valid_image_b64(resp, url)
    except Exception as e:
        log.debug("[image_search] Failed to download image for b64: host='%s', err=%s", host, type(e).__name__)
    if b64_str:
        log.debug("[image_search] target downloaded: bytes=%d", _estimate_b64_bytes(b64_str))
        return b64_str
    log.warning("[image_search] target download failed: host=%s", host)
    return ""


async def fetch_saucenao_candidates(
    image_url: str,
    session: aiohttp.ClientSession,
    api_key: str = "",
    num_results: int = 6,
) -> list[dict[str, Any]]:
    """SauceNAO API を呼び出して正規化済み候補リストを返す。"""
    if not api_key:
        log.warning("[image_search] saucenao skipped: SAUCENAO_API_KEY is not configured")
        return []

    endpoint = "https://saucenao.com/search.php"
    params: dict[str, Any] = {
        "output_type": 2,
        "numres": num_results,
        "url": image_url,
        "api_key": api_key,
    }

    try:
        async with session.get(endpoint, params=params) as resp:
            if resp.status != 200:
                log.warning("[image_search] SauceNAO HTTP error: status=%d (count=0)", resp.status)
                return []
            data = await resp.json()
    except Exception as e:
        log.warning(
            "[image_search] SauceNAO fetch error: %s: %s (count=0)",
            type(e).__name__,
            _sanitize_error_msg(e, image_url=image_url, api_key=api_key),
        )
        return []

    results = data.get("results", [])
    if not isinstance(results, list):
        results = []

    candidates: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        header = item.get("header", {})
        data_block = item.get("data", {})
        if not isinstance(header, dict) or not isinstance(data_block, dict):
            continue

        try:
            similarity = float(header.get("similarity", 0.0))
        except (ValueError, TypeError):
            similarity = 0.0

        title = (
            data_block.get("title")
            or data_block.get("material")
            or data_block.get("source")
            or data_block.get("jp_name")
            or data_block.get("eng_name")
            or ""
        )
        creator = (
            data_block.get("member_name")
            or data_block.get("author_name")
            or data_block.get("creator")
            or data_block.get("artist")
            or data_block.get("author")
            or ""
        )

        source_url = ""
        ext_urls = data_block.get("ext_urls", [])
        if isinstance(ext_urls, list) and ext_urls:
            pixiv_url = next((u for u in ext_urls if isinstance(u, str) and "pixiv.net" in u), None)
            source_url = pixiv_url if pixiv_url else str(ext_urls[0])
        elif isinstance(ext_urls, str):
            source_url = ext_urls

        thumbnail_url = str(header.get("thumbnail") or "").strip()

        candidates.append({
            "source": "saucenao",
            "title": str(title).strip(),
            "creator": str(creator).strip(),
            "source_url": source_url.strip(),
            "similarity": similarity,
            "thumbnail_url": thumbnail_url,
            "extra": {
                "index_name": header.get("index_name", ""),
                "index_id": header.get("index_id"),
            },
            "verdict": "unverified",
            "confidence": 0,
            "reason": "",
            "verified": False,
        })

    top_sim = max((c.get("similarity", 0.0) for c in candidates), default=0.0)
    log.info("[image_search] saucenao results: count=%d top_similarity=%s", len(candidates), top_sim)
    return candidates


async def fetch_tracemoe_candidates(
    image_url: str,
    session: aiohttp.ClientSession,
) -> list[dict[str, Any]]:
    """trace.moe API を呼び出して正規化済み候補リストを返す。"""
    endpoint = f"https://api.trace.moe/search?cutBorders&anilistInfo=1&url={urllib.parse.quote(image_url, safe='')}"

    try:
        async with session.get(endpoint) as resp:
            if resp.status != 200:
                log.warning("[image_search] trace.moe HTTP error: status=%d (count=0)", resp.status)
                return []
            data = await resp.json()
    except Exception as e:
        log.warning(
            "[image_search] trace.moe fetch error: %s: %s (count=0)",
            type(e).__name__,
            _sanitize_error_msg(e, image_url=image_url),
        )
        return []

    results = data.get("result", [])
    if not isinstance(results, list):
        results = []

    candidates: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue

        try:
            similarity = round(float(item.get("similarity", 0.0)) * 100.0, 2)
        except (ValueError, TypeError):
            similarity = 0.0

        anilist = item.get("anilist", {})
        title = ""
        anilist_id: Any = None
        if isinstance(anilist, dict):
            anilist_id = anilist.get("id")
            title_dict = anilist.get("title", {})
            if isinstance(title_dict, dict):
                title = (
                    title_dict.get("native")
                    or title_dict.get("romaji")
                    or title_dict.get("english")
                    or ""
                )
            elif isinstance(title_dict, str):
                title = title_dict
        elif isinstance(anilist, int) and not isinstance(anilist, bool):
            anilist_id = anilist
            title = ""
        elif isinstance(anilist, str):
            title = anilist

        source_url = f"https://anilist.co/anime/{anilist_id}" if anilist_id else ""
        thumbnail_url = str(item.get("image") or "").strip()
        episode = item.get("episode")
        from_sec = item.get("from", 0.0)
        try:
            from_f = float(from_sec)
            m = int(from_f // 60)
            s = int(from_f % 60)
            time_str = f"{m}:{s:02d}"
        except (ValueError, TypeError):
            from_f = 0.0
            time_str = "0:00"

        candidates.append({
            "source": "tracemoe",
            "title": str(title).strip(),
            "creator": "",
            "source_url": source_url,
            "similarity": similarity,
            "thumbnail_url": thumbnail_url,
            "extra": {
                "episode": episode,
                "from_sec": from_f,
                "time_str": time_str,
                "anilist_id": anilist_id,
            },
            "verdict": "unverified",
            "confidence": 0,
            "reason": "",
            "verified": False,
        })

    top_sim = max((c.get("similarity", 0.0) for c in candidates), default=0.0)
    log.info("[image_search] tracemoe results: count=%d top_similarity=%s", len(candidates), top_sim)
    return candidates


def _candidate_work_key(cand: dict[str, Any]) -> tuple[str, Any]:
    """候補の作品同一性キー。trace.moe は anilist_id、無ければ (source, 正規化タイトル)。"""
    source = str(cand.get("source") or "")
    extra = cand.get("extra") or {}
    anilist_id = extra.get("anilist_id") if isinstance(extra, dict) else None
    if source == "tracemoe" and anilist_id:
        return (source, anilist_id)
    title = re.sub(r"\s+", " ", str(cand.get("title") or "").strip()).casefold()
    return (source, title)


def _dedupe_candidates_by_work(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一作品の候補は類似度が最も高い 1 件だけを残す（入力順＝類似度降順を前提に最初の出現を保持）。"""
    seen: set[tuple[str, Any]] = set()
    deduped: list[dict[str, Any]] = []
    for cand in candidates:
        key = _candidate_work_key(cand)
        # タイトルも ID も無い候補は重複判定の対象外として常に残す
        if key[1] in ("", None):
            deduped.append(cand)
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cand)
    return deduped


async def _verify_target_candidates_with_vision(
    clean_url: str,
    target_candidates: list[dict[str, Any]],
    vision_verify: Callable[[str, list[str]], Awaitable[list[dict[str, Any]]]],
) -> bool:
    """対象画像と候補サムネイルのダウンロード・健全性確認を行い、ビジョン検証結果を書き戻す。"""
    target_b64 = await _download_image_as_b64(clean_url)
    if not target_b64:
        log.warning(
            "[IMAGE_SEARCH] failed to download valid target image; vision verification skipped (stage=vision_verify, host=%s)",
            urlparse(clean_url).hostname or "",
        )
        return False

    cand_b64_tasks = [_download_image_as_b64(c.get("thumbnail_url", "")) for c in target_candidates]
    cand_b64s = await asyncio.gather(*cand_b64_tasks, return_exceptions=False)
    verify_pairs: list[tuple[dict[str, Any], str]] = [
        (cand, b64)
        for cand, b64 in zip(target_candidates, cand_b64s)
        if isinstance(b64, str) and b64
    ]
    if not verify_pairs:
        log.info("[IMAGE_SEARCH] no valid candidate thumbnails available; vision verification skipped")
        return True

    verify_results = await vision_verify(target_b64, [b64 for _, b64 in verify_pairs])
    if not isinstance(verify_results, list):
        return True

    for idx, v_res in enumerate(verify_results):
        if idx < len(verify_pairs) and isinstance(v_res, dict):
            cand, _ = verify_pairs[idx]
            cand["verdict"] = v_res.get("verdict", cand["verdict"])
            cand["confidence"] = int(v_res.get("confidence", cand["confidence"]))
            cand["reason"] = str(v_res.get("reason", cand["reason"]))[:30]
            cand["verified"] = True
    return True


def _build_result_text(
    candidates: list[dict[str, Any]],
    image_url: str,
    lens_url: str,
    clues: list[str] | None = None,
) -> str:
    """正規化・判定済み候補リストから正直で客観的な結果テキストを組み立てる。"""
    best_match = next((c for c in candidates if c.get("verdict") == "match"), None)
    partial_matches = [c for c in candidates if c.get("verdict") == "partial"]

    lines: list[str] = ["### 🔍 画像検索・特定結果"]
    lines.append(f"- **対象画像**: {image_url}")

    if best_match:
        lines.append("\n**【特定結果】**")
        title = best_match.get("title") or "（タイトル不明）"
        lines.append(f"- **作品・タイトル**: {title}")
        if best_match.get("creator"):
            lines.append(f"- **作者・投稿者**: {best_match['creator']}")
        if best_match.get("source_url"):
            lines.append(f"- **出典**: <{best_match['source_url']}>")
        lines.append(f"- **類似度**: {best_match.get('similarity', 0.0):.1f}% ({best_match.get('source', '')})")
        if best_match.get("extra", {}).get("episode") is not None:
            extra = best_match["extra"]
            lines.append(f"- **該当箇所**: 第{extra.get('episode')}話 ({extra.get('time_str', '')})")
        reason = str(best_match.get("reason") or "").strip()
        if not best_match.get("verified", False):
            if reason:
                reason = f"{reason}（ビジョン未検証）"
            else:
                reason = "（ビジョン未検証）"
        if reason:
            lines.append(f"- **判定理由**: {reason}")

    elif partial_matches:
        lines.append("\n**【候補（未確認）】** (※確定ではありません)")
        for idx, cand in enumerate(partial_matches[:3], start=1):
            title = cand.get("title") or "（名称不明）"
            sim = cand.get("similarity", 0.0)
            src = cand.get("source", "")
            creator_str = f" / 作者: {cand['creator']}" if cand.get("creator") else ""
            lines.append(f"{idx}. **{title}** (類似度: {sim:.1f}%, ソース: {src}{creator_str})")
            if cand.get("source_url"):
                lines.append(f"   - 出典: <{cand['source_url']}>")
            if cand.get("reason"):
                lines.append(f"   - 備考: {cand['reason']}")

    else:
        lines.append("\n**特定できませんでした。**")
        lines.append("画像に対応する高精度な一致・候補は見つかりませんでした。")
        if clues:
            lines.append("\n**【未確認の手がかり】**:")
            for clue in clues:
                lines.append(f"- {clue}")

    lines.append(f"\n*(手動確認用 Google Lens: <{lens_url}>)*")
    return "\n".join(lines)


class ImageSearchToolAdapter:
    """画像検索ツールのアダプター"""

    @classmethod
    def create_tool(cls, name: str = "search_image", timeout_sec: float = 40.0) -> ToolDefinition:
        """search_image ツール定義を作成する（デフォルトタイムアウト: 40秒）"""
        return ToolDefinition(
            name=name,
            description=(
                "画像URLを指定して逆画像検索を行い、写っている人物・キャラクター名・作品名・元ネタ・類似画像・出典Webページを客観的に調査します。"
                "添付画像や直前の画像について『これ誰？』『何のキャラ？』『元ネタは何？』と質問された場合に使用してください。"
            ),
            parameters=[
                ToolParameter(
                    name="image_url",
                    type="string",
                    description="検索対象の画像Web URL（http/https）。省略時は直近の添付画像や文脈上の画像が自動適用されます。",
                    required=False,
                ),
                ToolParameter(
                    name="query",
                    type="string",
                    description="追加の絞り込みキーワードや質問（キャラクター名、推測される特徴など。任意）",
                    required=False,
                ),
            ],
            handler=cls.execute_image_search,
            category=ToolCategory.WEB,
            tags={"web", "search", "image", "research"},
            timeout_sec=timeout_sec,
        )

    @classmethod
    async def execute_image_search(
        cls,
        image_url: str = "",
        query: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        """画像検索ツールの実行エントリーポイント"""
        resolved_url = (image_url or "").strip()
        if not resolved_url:
            resolved_url = extract_image_url_from_context(kwargs.get("context"), **kwargs)

        clean_query = str(query or "").strip()
        log.info(
            "[IMAGE_SEARCH] execute_image_search: url='%s', query='%s'",
            _safe_url_for_log(resolved_url),
            clean_query,
        )

        if not resolved_url:
            # 画像URLが見つからない場合、クエリがあればWeb検索へフォールバック
            if clean_query and not (clean_query.startswith("http://") or clean_query.startswith("https://")):
                from ..web_research import search_web_entries_unified

                entries = await search_web_entries_unified(clean_query, max_results=3)
                if entries:
                    lines = [
                        f"- [{e.get('title', '無題')}](<{e.get('url', '')}>): {e.get('snippet', '')}"
                        for e in entries
                    ]
                    out_text = (
                        f"画像URLが指定されていないため、キーワード '{clean_query}' でWeb検索しました:\n"
                        + "\n".join(lines)
                    )
                    log.info("[IMAGE_SEARCH] execute_image_search finished: success=True, source='web_search'")
                    return ToolResult(
                        name="search_image",
                        success=True,
                        output=out_text,
                        metadata={"fallback": "web_search", "query": clean_query},
                    )
            log.info("[IMAGE_SEARCH] execute_image_search finished: success=False, source='no_image_url'")
            return ToolResult(
                name="search_image",
                success=False,
                output="画像URLが指定されておらず、直前の会話や添付ファイルからも有効な画像URLを特定できませんでした。画像URLを指定するか画像を添付してください。",
                metadata={"error": "no_image_url"},
            )

        res = await cls.search_image_unified(resolved_url, query=clean_query, **kwargs)
        log.info(
            "[IMAGE_SEARCH] execute_image_search finished: success=%s, source='%s'",
            res.get("success"),
            res.get("source"),
        )
        return ToolResult(
            name="search_image",
            success=res.get("success", False),
            output=res.get("text", "検索結果を取得できませんでした。"),
            metadata=res,
        )

    @classmethod
    async def search_image_unified(
        cls,
        image_url: str,
        query: str = "",
        *,
        vision_describe: Optional[Callable[[str], Awaitable[str]]] = None,
        vision_verify: Optional[Callable[[str, list[str]], Awaitable[list[dict[str, Any]]]]] = None,
        timeout_sec: float = 35.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """逆画像検索API（SauceNAO / trace.moe）とビジョン検証による高精度特定

        vision_describe は「base64 の画像データを受け取り、記述テキストを返す関数」である。
        呼び出し側は ollama_bot.common.vision_caption の describe_image_for_identification をそのまま渡してよい。
        URL を渡してはならない。
        """
        start_time = time.monotonic()
        log.info(
            "[IMAGE_SEARCH] Starting unified image search: url='%s', query='%s'",
            _safe_url_for_log(image_url),
            query,
        )

        # 1. 対象画像URLバリデーション
        clean_url = (image_url or "").strip()
        if not clean_url or not (clean_url.startswith("http://") or clean_url.startswith("https://")):
            log.warning("[image_search] search failed at stage=url_validation: invalid image_url")
            log.info(
                "[image_search] outcome=%s reason=%s saucenao=%d tracemoe=%d candidates=%d verified_match=%d elapsed_sec=%.1f",
                "not_identified",
                "error",
                0,
                0,
                0,
                0,
                time.monotonic() - start_time,
            )
            return {
                "success": False,
                "source": "invalid_url",
                "text": "有効な画像URLが指定されていません。",
                "candidates": [],
                "lens_url": "",
                "raw_chars": 0,
            }

        encoded_url = urllib.parse.quote(clean_url, safe="")
        lens_url = f"https://lens.google.com/uploadbyurl?url={encoded_url}"

        # 2. 候補収集（SauceNAO & trace.moe 並行呼び出し）
        api_timeout = float(_get_cfg("IMAGE_SEARCH_API_TIMEOUT_SEC", 15.0) or 15.0)
        saucenao_key = str(_get_cfg("SAUCENAO_API_KEY", "") or "").strip()
        max_candidates = int(_get_cfg("IMAGE_SEARCH_MAX_CANDIDATES", 5) or 5)

        saucenao_count = 0
        tracemoe_count = 0
        fetch_error_occurred = False
        candidates: list[dict[str, Any]] = []
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=api_timeout)) as session:
                tasks = [
                    fetch_saucenao_candidates(clean_url, session, api_key=saucenao_key),
                    fetch_tracemoe_candidates(clean_url, session),
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                if len(results) >= 1 and isinstance(results[0], list):
                    saucenao_count = len(results[0])
                if len(results) >= 2 and isinstance(results[1], list):
                    tracemoe_count = len(results[1])
                for item_res in results:
                    if isinstance(item_res, list):
                        candidates.extend(item_res)
                    elif isinstance(item_res, Exception):
                        fetch_error_occurred = True
                        log.warning(
                            "[IMAGE_SEARCH] Candidate fetch error: %s: %s",
                            type(item_res).__name__,
                            _sanitize_error_msg(item_res, image_url=clean_url, api_key=saucenao_key),
                        )
        except Exception as e:
            fetch_error_occurred = True
            log.warning(
                "[IMAGE_SEARCH] Session error during candidate fetch: %s: %s",
                type(e).__name__,
                _sanitize_error_msg(e, image_url=clean_url, api_key=saucenao_key),
            )

        log.info(
            "[image_search] candidates collected: saucenao=%d tracemoe=%d total=%d",
            saucenao_count,
            tracemoe_count,
            len(candidates),
        )

        # 類似度降順ソート＆作品単位の重複排除＆件数上限
        candidates.sort(key=lambda c: float(c.get("similarity", 0.0)), reverse=True)
        candidates = _dedupe_candidates_by_work(candidates)
        candidates = candidates[:max_candidates]

        # 3. 強い一致の近道判定（および基本判定）
        for cand in candidates:
            cand.setdefault("verified", False)
            sim = float(cand.get("similarity", 0.0))
            src = cand.get("source")
            title = str(cand.get("title") or "").strip()
            if (src == "saucenao" and sim >= 85.0) or (src == "tracemoe" and sim >= 87.0):
                if not title:
                    cand["verdict"] = "partial"
                    cand["confidence"] = int(sim)
                    cand["reason"] = "タイトル不明のため候補扱い"
                else:
                    cand["verdict"] = "match"
                    cand["confidence"] = int(sim)
                    cand["reason"] = "API高類似度一致"
            elif (src == "saucenao" and sim >= 60.0) or (src == "tracemoe" and sim >= 65.0):
                cand["verdict"] = "partial"
                cand["confidence"] = int(sim)
                cand["reason"] = "API中類似度候補"
            else:
                cand["verdict"] = "no"
                cand["confidence"] = int(sim)
                cand["reason"] = "類似度基準未満"

        sim_match = sum(1 for c in candidates if c.get("verdict") == "match")
        sim_partial = sum(1 for c in candidates if c.get("verdict") == "partial")
        sim_no = sum(1 for c in candidates if c.get("verdict") == "no")
        log.info(
            "[image_search] similarity verdicts: match=%d partial=%d no=%d",
            sim_match,
            sim_partial,
            sim_no,
        )

        # 4. 自前ビジョン検証（vision_verify が渡されている場合）
        verify_enabled = bool(_get_cfg("IMAGE_SEARCH_VISION_VERIFY_ENABLED", True))
        verify_top_n = int(_get_cfg("IMAGE_SEARCH_VERIFY_TOP_N", 3) or 3)
        target_download_failed = False

        if verify_enabled and vision_verify is None:
            log.warning("[IMAGE_SEARCH] vision_verify not provided by caller; verification skipped")

        if vision_verify is None or not verify_enabled:
            log.info("[image_search] vision verify skipped: reason=%s", "no_vision_verify")
        elif not candidates:
            log.info("[image_search] vision verify skipped: reason=%s", "no_targets")
        else:
            # 検証はステップ3で候補以上（partial / match）とされたものだけに使う。
            # 全て基準未満なら検証自体を省略し、同定フォールバックへ進む。
            verify_pool = [c for c in candidates if c.get("verdict") in ("partial", "match")]
            target_candidates = verify_pool[:verify_top_n]
            if not target_candidates:
                log.info("[IMAGE_SEARCH] no candidates at or above the partial threshold; vision verification skipped")
                log.info("[image_search] vision verify skipped: reason=%s", "no_targets")
            else:
                try:
                    verify_ok = await _verify_target_candidates_with_vision(clean_url, target_candidates, vision_verify)
                    if verify_ok is False:
                        target_download_failed = True
                except Exception as e:
                    log.warning(
                        "[IMAGE_SEARCH] vision_verify failed: %s: %s",
                        type(e).__name__,
                        _sanitize_error_msg(e, image_url=clean_url),
                    )

            # 未検証 match の格下げ（本丸）:
            # vision_verify が提供され検証が有効な場合、ステップ4の終了時点で、
            # 検証の対象にならなかった候補（上位 IMAGE_SEARCH_VERIFY_TOP_N 件の外側）や
            # 検証結果が書き込まれなかった候補に残っている match 判定は、
            # すべて partial に格下げし、理由を「検証対象外のため候補扱い」とする。
            for cand in candidates:
                if cand.get("verdict") == "match" and not cand.get("verified", False):
                    cand["verdict"] = "partial"
                    cand["reason"] = "検証対象外のため候補扱い"

            if target_candidates:
                v_match = sum(1 for c in target_candidates if c.get("verdict") == "match")
                v_partial = sum(1 for c in target_candidates if c.get("verdict") == "partial")
                v_no = sum(1 for c in target_candidates if c.get("verdict") == "no")
                log.info(
                    "[image_search] vision verify: targets=%d match=%d partial=%d no=%d",
                    len(target_candidates),
                    v_match,
                    v_partial,
                    v_no,
                )

        # 5. 同定フォールバック（候補なし または 全て no 判定時）
        clues: list[str] = []
        has_any_match = any(c.get("verdict") in ("match", "partial") for c in candidates)

        if not has_any_match:
            desc_text = ""
            if vision_describe is not None:
                target_b64 = await _download_image_as_b64(clean_url)
                fallback_reason = "no_candidates" if not candidates else "all_candidates_rejected"
                target_bytes = _estimate_b64_bytes(target_b64)
                log.info(
                    "[image_search] describe fallback: reason=%s target_bytes=%d",
                    fallback_reason,
                    target_bytes,
                )
                if target_b64:
                    try:
                        desc_text = await vision_describe(target_b64)
                    except Exception as e:
                        log.warning(
                            "[IMAGE_SEARCH] vision_describe failed: %s: %s",
                            type(e).__name__,
                            _sanitize_error_msg(e, image_url=clean_url),
                        )
                else:
                    target_download_failed = True
                    log.warning(
                        "[IMAGE_SEARCH] vision_describe skipped: target download failed (host=%s)",
                        urlparse(clean_url).hostname or "",
                    )

            if desc_text:
                clues.append(f"画像の特徴: {desc_text.strip()}")
                try:
                    from ..web_research import search_web_entries_unified

                    search_query = desc_text.splitlines()[0][:80] if desc_text else ""
                    if search_query:
                        entries = await search_web_entries_unified(search_query, max_results=3)
                        for entry in entries:
                            clues.append(f"Web手がかり: [{entry.get('title', '無題')}](<{entry.get('url', '')}>)")
                except Exception as e:
                    log.debug(
                        "[IMAGE_SEARCH] fallback web search failed: %s: %s",
                        type(e).__name__,
                        _sanitize_error_msg(e, image_url=clean_url),
                    )

        # 6. 正直な結果テキスト（text）の組み立て
        result_text = _build_result_text(candidates, clean_url, lens_url, clues=clues)

        # 7. 戻り値
        best_match = next((c for c in candidates if c.get("verdict") == "match"), None)
        partial_matches = [c for c in candidates if c.get("verdict") == "partial"]
        verified_match_count = sum(
            1 for c in candidates if c.get("verdict") == "match" and c.get("verified", False)
        )
        success = True if (best_match or partial_matches) else False

        if best_match:
            outcome = "identified"
            reason = "verified_match" if best_match.get("verified", False) else "similarity_only"
        elif partial_matches:
            outcome = "partial"
            reason = "error" if target_download_failed else "similarity_only"
        else:
            outcome = "not_identified"
            if target_download_failed or (fetch_error_occurred and not candidates):
                reason = "error"
            elif clues:
                reason = "describe_fallback"
            elif not candidates:
                reason = "no_candidates"
            else:
                reason = "all_candidates_rejected"

        source_str = "failed"
        if best_match:
            source_str = best_match.get("source", "matched")
        elif partial_matches:
            source_str = "partial_candidates"

        res = {
            "success": success,
            "source": source_str,
            "text": result_text,
            "candidates": candidates,
            "lens_url": lens_url,
            "raw_chars": len(result_text),
        }
        log.info(
            "[IMAGE_SEARCH] Unified image search completed: success=%s, source='%s', candidates=%d",
            success,
            source_str,
            len(candidates),
        )
        log.info(
            "[image_search] outcome=%s reason=%s saucenao=%d tracemoe=%d candidates=%d verified_match=%d elapsed_sec=%.1f",
            outcome,
            reason,
            saucenao_count,
            tracemoe_count,
            len(candidates),
            verified_match_count,
            time.monotonic() - start_time,
        )
        return res
