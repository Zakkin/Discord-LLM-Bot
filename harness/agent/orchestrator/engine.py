"""AIエージェント・ハーネス オーケストレーター実行エンジン (engine.py)

1-2. オーケストレーター（フロー制御）のコア実装。
ユーザーからのプロンプト受領から最終応答生成までの5段階内部処理フロー：
1. コンテキストの構築 (Context)
2. 推論と計画立案 (Thought)
3. ツール選択と実行 (Action)
4. 結果の観察と評価 (Observation)
5. 最終応答の生成 (Final Response)
を安全に統括・制御する。
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from typing import Any

from ..observation import (
    EvaluationVerdict,
    LoopDetector,
    NextStepPhase,
    Observation,
    ObservationEvaluator,
    ObservationFacade,
    ObservationParser,
    ObservationProcessor,
    ObservationRouter,
    ObservationSynthesizer,
    ScratchpadManager,
)
from ..prompt_parser.parser import (
    ParsedToolCall,
    extract_thought_and_action,
    sanitize_generated_reply,
)
from ..test_harness.telemetry import (
    DistributedTracer,
    MetricsCollector,
    SpanKind,
    TelemetryStorage,
)
from ..tool_registry import ToolExecutor, ToolRegistry, get_default_registry
from .llm_client import DefaultLLMClient, LLMClientProtocol
from .types import AgentState, OrchestratorConfig, OrchestratorResult
from lib.text_utils import trim_text_at_safe_boundary

log = logging.getLogger("harness.agent.orchestrator.engine")


def _is_casual_conversational_utterance(text: str, target_name: str = "") -> bool:
    """発話が「対話相手からの短い相槌・感情確認（例: 好きだよね？ 等）」であるかどうか判定する。"""
    raw = (text or "").strip()
    if not raw:
        return False

    # <current_reply_target> 内のメッセージ抽出
    m = re.search(r"<current_reply_target>.*?メッセージ:\s*(.*?)</current_reply_target>", raw, re.DOTALL)
    msg = m.group(1).strip() if m else raw

    # URLが含まれる場合は外部調査の可能性があるため除外
    if "http://" in msg or "https://" in msg:
        return False

    # 明示的な調査・検索・ニュース要求キーワードが含まれる場合は除外
    investigation_keywords = (
        "調べて", "検索", "教えて", "とは？", "とは", "って何", "どういう意味",
        "いつから", "いつまで", "どこで", "ニュース", "天気", "最新情報",
        "wiki", "事実確認", "誰ですか", "何ですか", "何時",
    )
    if any(kw in msg for kw in investigation_keywords):
        return False

    # 短い相槌・感情確認の明確な語彙・パターン
    # 例: 「えでも好きだよね？(^○^)」「好きでしょ」「そうだよね」「うん」「草」「かわいい」等
    conversational_patterns = (
        r"(?:好き|すき|嫌い|きらい)(?:だよね|でしょ|よね|かな)",
        r"(?:だよね|でしょ|じゃん|よね|ね|かな)(?:[?？!！〜~\s\^○\(\)]*)$",
        r"^(?:え[ー〜でもっ]*|あ[ー〜っ]*|お[ー〜っ]*|う[ー〜ん]*|うん|そう|なるほど|たしかに|確かに|へ[ー〜え]*|ほ[ー〜]*|ふ[ー〜ん]*)",
        r"^(?:草+|[wWｗＷ]+|笑+|かわいい|可愛い|すごい|スゴイ|まじで|マジで|ほんと|本当)[?？!！〜~\s\^○\(\)]*$",
        r"^(?:おはよう|おはよ|おやすみ|おつかれ|お疲れ|おつ|ありがとう|サンクス|りょ|了解)[?？!！〜~\s\^○\(\)]*$",
    )
    # 50文字以内で、上記の相槌・感情確認パターンに合致する場合
    if len(msg) <= 50 and any(re.search(pat, msg) for pat in conversational_patterns):
        return True

    return False


def _trim_user_prompt_preserving_target(user_prompt: str, max_chars: int) -> str:
    """user_prompt をトリミングするが、<current_reply_target> を含む末尾の重要ブロックは必ず保護する。"""
    if len(user_prompt) <= max_chars:
        return user_prompt

    target_marker = "<current_reply_target>"
    target_idx = user_prompt.find(target_marker)

    if target_idx != -1:
        # 直前の見出しがあればそこから tail とする
        reminder_header = "【最重要: 今あなたが返信すべき対象メッセージ】"
        header_idx = user_prompt.rfind(reminder_header, 0, target_idx)
        split_idx = header_idx if header_idx != -1 else target_idx

        head = user_prompt[:split_idx].rstrip()
        tail = user_prompt[split_idx:].lstrip()

        if len(tail) >= max_chars:
            # tail 単体でも上限を超えている場合でも、末尾の重要ブロックは最優先で保持
            return tail

        head_budget = max_chars - len(tail) - 2
        trimmed_head = trim_text_at_safe_boundary(head, head_budget)
        if trimmed_head:
            return f"{trimmed_head}\n\n{tail}"
        return tail

    return trim_text_at_safe_boundary(user_prompt, max_chars) or user_prompt[:max_chars]


def _estimate_tokens_from_chars(text: str) -> int:
    """文字種（ASCII / 非ASCII日本語）に応じたトークン数を事前推定する。

    日本語・非ASCII文字は 1.15 token/char、ASCII文字は 0.35 token/char で計算する。
    """
    if not text:
        return 0
    ascii_count = 0
    non_ascii_count = 0
    for ch in text:
        if ord(ch) < 128:
            ascii_count += 1
        else:
            non_ascii_count += 1
    val = ascii_count * 0.35 + non_ascii_count * 1.15
    return int(math.ceil(val))


def _estimate_messages_tokens(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> tuple[int, int]:
    """対話メッセージ履歴およびツール定義の総文字数と推定トークン数を事前計測する。

    戻り値: (total_chars, estimated_tokens)
    """
    total_chars = 0
    total_tokens = 0

    for msg in messages:
        content = str(msg.get("content") or "")
        if content:
            total_chars += len(content)
            total_tokens += _estimate_tokens_from_chars(content)
        tool_calls = msg.get("tool_calls")
        if tool_calls and isinstance(tool_calls, list):
            try:
                tc_str = json.dumps(tool_calls, ensure_ascii=False)
                total_chars += len(tc_str)
                total_tokens += _estimate_tokens_from_chars(tc_str)
            except Exception:
                pass

    if tools:
        try:
            tools_str = json.dumps(tools, ensure_ascii=False)
            total_chars += len(tools_str)
            total_tokens += _estimate_tokens_from_chars(tools_str)
        except Exception:
            pass

    return total_chars, total_tokens


def _resolve_dynamic_generation_limits(
    total_chars: int,
    estimated_tokens: int,
    *,
    base_num_ctx: int,
    base_num_predict: int | None,
    think: bool,
) -> tuple[int, int | None]:
    """プロンプト推定トークン数に応じた動的num_ctx拡張および安全なnum_predictを算出する。

    1. 事前計測した estimated_tokens と必要出力トークン数（think=True 時は最低 1536、
       通常時は base_num_predict または 512）の合計が base_num_ctx の80%を超える場合、
       num_ctx を 8192 単位のバケットで段階的に自動拡張（上限 OLLAMA_MAX_NUM_CTX、デフォルト 32768）。
    2. num_ctx が上限に達している場合は、コンテキスト超過（HTTP 400）を防ぐため
       num_predict を max(256, min(num_predict, effective_num_ctx - estimated_tokens - 256))
       に安全に可変調整する。
    """
    try:
        from lib.config_utils import cfg
        max_num_ctx = int(cfg("OLLAMA_MAX_NUM_CTX", 32768) or 32768)
    except Exception:
        max_num_ctx = 32768

    if base_num_ctx > max_num_ctx:
        max_num_ctx = base_num_ctx

    if think:
        needed_predict = max(base_num_predict, 1536) if base_num_predict is not None else 1536
    else:
        needed_predict = base_num_predict if base_num_predict is not None else 512

    required_tokens = estimated_tokens + needed_predict
    effective_num_ctx = base_num_ctx

    # 80%を超える場合、8192単位のバケットで段階的に拡張
    while required_tokens > int(effective_num_ctx * 0.8) and effective_num_ctx < max_num_ctx:
        effective_num_ctx = min(effective_num_ctx + 8192, max_num_ctx)

    # num_ctx が上限に達している場合は、コンテキスト超過（HTTP 400）を防ぐため安全に調整
    effective_num_predict: int | None = base_num_predict
    if effective_num_ctx >= max_num_ctx:
        target_predict = base_num_predict if base_num_predict is not None else needed_predict
        safe_predict = max(256, min(target_predict, effective_num_ctx - estimated_tokens - 256))
        if base_num_predict is not None:
            effective_num_predict = min(base_num_predict, safe_predict)
        elif required_tokens > int(effective_num_ctx * 0.8):
            effective_num_predict = safe_predict

    return effective_num_ctx, effective_num_predict


class AgentOrchestrator:
    """5段階フローを自律制御するAIエージェント・オーケストレーター"""

    _estimate_tokens_from_chars = staticmethod(_estimate_tokens_from_chars)
    _estimate_messages_tokens = staticmethod(_estimate_messages_tokens)
    _resolve_dynamic_generation_limits = staticmethod(_resolve_dynamic_generation_limits)

    def __init__(
        self,
        config: OrchestratorConfig | None = None,
        tool_registry: ToolRegistry | None = None,
        llm_client: LLMClientProtocol | Any = None,
        tracer: DistributedTracer | None = None,
        metrics: MetricsCollector | None = None,
        storage: TelemetryStorage | None = None,
        observation_facade: ObservationFacade | None = None,
        logger: logging.Logger | None = None,
        memory_store: Any = None,
        model_client: Any = None,
        **extra: Any,
    ) -> None:
        self.config = config or OrchestratorConfig()
        self.tool_registry = tool_registry if tool_registry is not None else get_default_registry()
        self.tool_executor = ToolExecutor()
        resolved_client = llm_client if llm_client is not None else model_client
        if callable(resolved_client) and not hasattr(resolved_client, "chat"):
            self.llm_client: LLMClientProtocol = DefaultLLMClient(custom_chat_fn=resolved_client)
        else:
            self.llm_client = resolved_client or DefaultLLMClient()
        self.memory_store = memory_store

        # フェーズ4: Observation コンポーネント群 & ファサード
        self.loop_detector = LoopDetector(
            max_identical_calls=2,
            max_consecutive_errors=3,
            max_total_turns=self.config.max_turns,
        )
        self.obs_processor = ObservationProcessor()
        self.obs_evaluator = ObservationEvaluator(loop_detector=self.loop_detector)
        self.obs_synthesizer = ObservationSynthesizer()
        self.obs_router = ObservationRouter()
        self.scratchpad = ScratchpadManager()

        self.observation_facade = observation_facade or ObservationFacade(
            parser=self.obs_processor,
            scratchpad=self.scratchpad,
            loop_detector=self.loop_detector,
            evaluator=self.obs_evaluator,
            router=self.obs_router,
            synthesizer=self.obs_synthesizer,
            max_turns=self.config.max_turns,
        )

        # テストハーネス: テレメトリ基盤
        self.tracer = tracer or DistributedTracer()
        self.metrics = metrics or MetricsCollector()
        self.storage = storage

        self.log = logger or log

    def _get_default_num_ctx(self) -> int:
        """デフォルトのnum_ctxを取得（設定値または16384）"""
        if self.config.num_ctx:
            return self.config.num_ctx
        try:
            from lib.config_utils import cfg
            return int(cfg("OLLAMA_NUM_CTX", 16384) or 16384)
        except Exception:
            return 16384

    async def run(
        self,
        prompt: str = "",
        context: dict[str, Any] | str | None = None,
        *,
        user_prompt: str = "",
        system_prompt: str = "",
        extra_ctx: dict[str, Any] | None = None,
    ) -> OrchestratorResult:
        """ユーザープロンプトとコンテキストを受け取り、5段階フローを実行して OrchestratorResult を返す。

        AgentOrchestrator.run(prompt, context) の標準インターフェースに対応しつつ、
        従来の run(user_prompt, system_prompt, context) との後方互換性を完全維持する。
        """
        start_time = time.perf_counter()

        # 引数の正規化
        effective_user_prompt = (prompt or user_prompt).strip()
        effective_system_prompt = system_prompt
        merged_ctx: dict[str, Any] = {}

        if isinstance(context, str):
            # 第2引数に文字列が渡された場合（従来の run(user_prompt, system_prompt)）
            if not effective_system_prompt:
                effective_system_prompt = context
        elif isinstance(context, dict):
            merged_ctx.update(context)

        if extra_ctx:
            merged_ctx.update(extra_ctx)

        # context 辞書内に system_prompt が指定されている場合
        if not effective_system_prompt and "system_prompt" in merged_ctx:
            effective_system_prompt = str(merged_ctx.get("system_prompt") or "")

        # prompt が空で context 辞書内に prompt / user_prompt がある場合
        if not effective_user_prompt:
            effective_user_prompt = str(merged_ctx.get("user_prompt") or merged_ctx.get("prompt") or "").strip()

        state = AgentState(user_prompt=effective_user_prompt)
        extra_ctx = merged_ctx
        self.observation_facade.reset()

        self.log.info(
            "[orchestrator] Starting session task_id=%s max_turns=%d prompt='%s'",
            state.task_id,
            self.config.max_turns,
            effective_user_prompt[:80],
        )

        with self.tracer.span("orchestrator_session", kind=SpanKind.INTERNAL) as session_span:
            session_span.add_event("session_started", {"user_prompt": effective_user_prompt})

            # =================================================================
            # 1. コンテキストの構築（知覚・記憶の引き出し）
            # =================================================================
            with self.tracer.span("phase_1_context", kind=SpanKind.CONTEXT):
                effective_sys_prompt = await self._build_context(effective_system_prompt, effective_user_prompt, extra_ctx)
                if extra_ctx and extra_ctx.get("trimmed_user_prompt") is not None:
                    effective_user_prompt = str(extra_ctx["trimmed_user_prompt"])
                state.system_prompt = effective_sys_prompt

                # 最終出力制約サフィックス（思考抑止・直接返答指示）の分離
                # フェーズ2（Thought / 計画立案）ではモデルがツール呼び出しを行えるよう、サフィックスを除外したプロンプトを注入
                thought_user_prompt = effective_user_prompt
                try:
                    from harness.agent.prompt_parser.prompt_manager.retry_prompts import CHAT_REPLY_OUTPUT_SUFFIX
                    if CHAT_REPLY_OUTPUT_SUFFIX and CHAT_REPLY_OUTPUT_SUFFIX in thought_user_prompt:
                        thought_user_prompt = thought_user_prompt.replace(CHAT_REPLY_OUTPUT_SUFFIX, "").strip()
                except ImportError:
                    pass

                # 初期対話履歴を構築
                if effective_sys_prompt:
                    state.add_message("system", effective_sys_prompt)

                # context内に既存メッセージ履歴（history/messages）が指定されている場合は展開
                history_msgs = extra_ctx.get("messages") or extra_ctx.get("history")
                if isinstance(history_msgs, list):
                    for h_msg in history_msgs:
                        if isinstance(h_msg, dict) and "role" in h_msg and "content" in h_msg:
                            state.add_message(str(h_msg["role"]), str(h_msg["content"]))

                state.add_message("user", thought_user_prompt)

                self.metrics.record_tokens(
                    "context",
                    prompt_tokens=_estimate_tokens_from_chars(effective_sys_prompt) + _estimate_tokens_from_chars(thought_user_prompt),
                    completion_tokens=0,
                )

            # =================================================================
            # 推論・ツール実行・観察の自律ループ (Turns 1..max_turns)
            # =================================================================
            schemas = self.tool_registry.get_schemas() if self.tool_registry else None

            while state.current_turn < self.config.max_turns and not state.is_finished:
                state.current_turn += 1
                turn_idx = state.current_turn
                self.log.info("[orchestrator] --- Beginning Turn #%d ---", turn_idx)

                # -------------------------------------------------------------
                # 2. 推論と計画立案 (Thought)
                # -------------------------------------------------------------
                thought_text = ""
                tool_calls: list[ParsedToolCall] = []
                with self.tracer.span(f"phase_2_thought_turn_{turn_idx}", kind=SpanKind.THOUGHT):
                    self.metrics.start_timer(f"turn_{turn_idx}_thought")

                    model_val = str(extra_ctx.get("model") or extra_ctx.get("model_name") or self.config.model_name)
                    think_val = bool(extra_ctx.get("think", self.config.think))
                    temp_val = float(extra_ctx.get("temperature", self.config.temperature))
                    timeout_val = float(extra_ctx.get("timeout_sec", self.config.timeout_sec))
                    raw_num_ctx = extra_ctx.get("num_ctx", self.config.num_ctx)
                    raw_num_predict = extra_ctx.get("num_predict", self.config.num_predict)

                    # メッセージ履歴・ツール定義から総文字数と推定トークン数を事前計測
                    total_chars, est_tokens = _estimate_messages_tokens(state.messages, schemas)
                    base_ctx = int(raw_num_ctx) if raw_num_ctx else self._get_default_num_ctx()
                    base_predict = int(raw_num_predict) if raw_num_predict is not None else None

                    dyn_num_ctx, dyn_num_predict = _resolve_dynamic_generation_limits(
                        total_chars=total_chars,
                        estimated_tokens=est_tokens,
                        base_num_ctx=base_ctx,
                        base_num_predict=base_predict,
                        think=think_val,
                    )

                    llm_response = await self.llm_client.chat(
                        messages=state.messages,
                        tools=schemas,
                        model=model_val,
                        think=think_val,
                        temperature=temp_val,
                        timeout_sec=timeout_val,
                        num_ctx=dyn_num_ctx,
                        num_predict=dyn_num_predict,
                    )

                    self.metrics.stop_timer(f"turn_{turn_idx}_thought", phase="thought")

                    # レスポンスのパース
                    raw_reply = ""
                    msg_obj = llm_response.get("message") or {}
                    if isinstance(msg_obj, dict):
                        raw_reply = str(msg_obj.get("content") or "")
                        # Function Callingの tool_calls が直接返ってきた場合のパース
                        if "tool_calls" in msg_obj and isinstance(msg_obj["tool_calls"], list):
                            from ..prompt_parser.parser.actions import ParsedToolCall
                            for tc in msg_obj["tool_calls"]:
                                fn = tc.get("function") or {}
                                tool_calls.append(
                                    ParsedToolCall(
                                        name=str(fn.get("name") or ""),
                                        arguments=fn.get("arguments") or {},
                                        call_id=str(tc.get("id") or ""),
                                    )
                                )

                    # 出力テキストから思考とアクションを抽出
                    parsed_thought, parsed_actions, clean_content = extract_thought_and_action(
                        raw_reply,
                        registry=self.tool_registry,
                    )
                    thought_text = parsed_thought
                    if not tool_calls:
                        tool_calls = parsed_actions

                    self.log.info(
                        "[orchestrator] Thought parsed: calls=%d thought_len=%d",
                        len(tool_calls),
                        len(thought_text),
                    )
                    self.log.info(
                        "[orchestrator] Turn #%d Thought result: text_len=%d tool_calls=%s",
                        turn_idx,
                        len(thought_text),
                        [c.name for c in tool_calls],
                    )

                # ツール呼び出しのゲーティング（対話相手名クエリの遮断・短文相槌でのWeb検索遮断）
                if tool_calls:
                    target_name = str(extra_ctx.get("target_name") or "").strip()
                    reply_target_user = str(extra_ctx.get("reply_target_user") or "").strip()
                    partner_names = {n.lower() for n in (target_name, reply_target_user) if n}

                    filtered_tool_calls: list[ParsedToolCall] = []
                    for call in tool_calls:
                        if "search" in call.name.lower() or "web" in call.name.lower():
                            query_val = ""
                            if isinstance(call.arguments, dict):
                                query_val = str(call.arguments.get("query") or call.arguments.get("keyword") or "").strip()

                            is_target_name_query = False
                            if query_val and partner_names:
                                q_lower = query_val.lower()
                                for pn in partner_names:
                                    if pn and (pn == q_lower or pn in q_lower):
                                        is_target_name_query = True
                                        break

                            if is_target_name_query:
                                self.log.warning(
                                    "[orchestrator] Web search blocked: query '%s' targets conversation partner '%s'",
                                    query_val,
                                    target_name or reply_target_user,
                                )
                                continue

                            if _is_casual_conversational_utterance(effective_user_prompt, target_name):
                                self.log.warning(
                                    "[orchestrator] Web search gated: skipping search tool for casual conversational utterance '%s'",
                                    query_val,
                                )
                                continue

                        filtered_tool_calls.append(call)

                    tool_calls = filtered_tool_calls

                # ツール呼び出しがない場合は、直接フェーズ5へ進む
                if not tool_calls:
                    self.log.info("[orchestrator] No tool calls required. Moving to final response.")
                    state.final_response = clean_content or raw_reply
                    state.is_finished = True
                    break

                # -------------------------------------------------------------
                # 3. ツール選択と実行 (Action)
                # -------------------------------------------------------------
                executed_results: list[Any] = []
                with self.tracer.span(f"phase_3_action_turn_{turn_idx}", kind=SpanKind.ACTION):
                    # アシスタントの呼び出し表明を履歴に追加
                    state.add_message("assistant", raw_reply)

                    for call in tool_calls:
                        tool_def = self.tool_registry.get(call.name) if self.tool_registry else None
                        if tool_def is None:
                            self.log.warning("[orchestrator] Tool '%s' not found in registry", call.name)
                            from ..tool_registry.types import ToolResult
                            tool_res = ToolResult(
                                name=call.name,
                                success=False,
                                error=f"Tool '{call.name}' not found",
                            )
                        else:
                            tc_obj = call.to_tool_call()
                            if tc_obj is None:
                                from ..tool_registry.types import ToolCall
                                tc_obj = ToolCall(name=call.name, arguments=call.arguments, call_id=call.call_id)
                            tool_res = await self.tool_executor.execute(tool_def, tc_obj)

                        executed_results.append((call, tool_res))

                # -------------------------------------------------------------
                # 4. 結果の観察と評価 (Observation)
                # -------------------------------------------------------------
                with self.tracer.span(f"phase_4_observation_turn_{turn_idx}", kind=SpanKind.OBSERVATION):
                    routing_res = self.observation_facade.handle_turn_observations(
                        executed_results=executed_results,
                        messages=state.messages,
                        observations_history=state.observations,
                        user_intent=effective_user_prompt,
                        turn_index=turn_idx,
                        max_turns=self.config.max_turns,
                    )

                    if routing_res.next_phase == NextStepPhase.FATAL_ERROR:
                        self.log.warning("[orchestrator] Fatal error/loop abort: %s", routing_res.error_message)
                        state.error = routing_res.error_message
                        state.is_finished = True
                        break
                    elif routing_res.next_phase == NextStepPhase.PROCEED_TO_FINAL:
                        self.log.info("[orchestrator] All observation goals achieved in turn #%d", turn_idx)
                        break
                    elif routing_res.next_phase == NextStepPhase.RETRY_WITH_THOUGHT:
                        self.log.info("[orchestrator] Looping back to Phase 2 (Thought) with feedback.")

                    # コンテキスト長の安全なトリミング（スクラッチパッドマネージャー）
                    self.scratchpad.trim_history_window(state.messages)

            # =================================================================
            # 5. 最終応答の生成（Final Response）
            # =================================================================
            with self.tracer.span("phase_5_final_response", kind=SpanKind.FINAL_RESPONSE):
                if state.error:
                    self.log.warning("[orchestrator] Halting session with error: %s", state.error)
                    if not state.final_response:
                        state.final_response = f"申し訳ありません。タスクの実行中に修復不能なエラーまたは安全停止が発生しました: {state.error}"
                else:
                    final_text = await self._generate_final_response(state, extra_ctx=extra_ctx)
                    # サニタイズ
                    ctx = extra_ctx or {}
                    target_name = str(ctx.get("target_name") or "")
                    other_names_raw = ctx.get("other_names")
                    other_names_list: list[str] = [str(n) for n in other_names_raw] if isinstance(other_names_raw, (list, set, tuple)) else []
                    reply_target_user = str(ctx.get("reply_target_user") or "").strip()
                    if reply_target_user and reply_target_user != target_name and reply_target_user not in other_names_list:
                        other_names_list.append(reply_target_user)

                    cleaned_final = sanitize_generated_reply(
                        final_text,
                        user_text=effective_user_prompt,
                        target_name=target_name,
                        other_names=other_names_list,
                    )
                    state.final_response = cleaned_final

                    # メモリ保存（Prune/Save）
                    if self.config.auto_store_memory and extra_ctx.get("memory_store"):
                        await self._save_memory(effective_user_prompt, cleaned_final, extra_ctx, state)

            session_span.add_event("session_completed", {"success": state.error is None})

        # テレメトリ永続化
        if self.storage:
            self.storage.persist_spans(self.tracer.get_spans())
            self.storage.persist_metrics_summary(self.metrics.get_summary(), trace_id=self.tracer.trace_id)

        elapsed_total = time.perf_counter() - start_time
        self.log.info(
            "[orchestrator] Session finished in %.2fs turns=%d obs=%d success=%s",
            elapsed_total,
            state.current_turn,
            len(state.observations),
            state.error is None,
        )

        return OrchestratorResult(
            success=state.error is None,
            final_response=state.final_response or "",
            total_turns=state.current_turn,
            observations=state.observations,
            trace_id=self.tracer.trace_id,
            error=state.error,
            telemetry_summary=self.metrics.get_summary(),
            metadata=state.metadata,
        )

    async def _build_context(
        self,
        system_prompt: str,
        user_prompt: str,
        extra_ctx: dict[str, Any],
    ) -> str:
        """システムプロンプト、短期記憶、長期記憶（RAG）を合成"""
        parts: list[str] = []
        if system_prompt:
            parts.append(system_prompt.strip())

        # ツールカタログの統合（利用可能なツール一覧のガイダンス、二重注入防止）
        if self.tool_registry and hasattr(self.tool_registry, "render_catalog"):
            if "【利用可能なツール" not in system_prompt and "【利用可能なツール" not in user_prompt:
                catalog = self.tool_registry.render_catalog()
                if catalog:
                    parts.append(f"【利用可能なツールカタログ】\n{catalog}")

        # ツールが利用可能なセッションにおける調査思考ポリシー（推論・計画立案ポリシー）
        has_tools = False
        if self.tool_registry:
            if hasattr(self.tool_registry, "list_tools"):
                has_tools = bool(self.tool_registry.list_tools())
            elif hasattr(self.tool_registry, "tools") and isinstance(self.tool_registry.tools, dict):
                has_tools = bool(self.tool_registry.tools)

        target_name = str(extra_ctx.get("target_name") or "").strip()
        is_casual = _is_casual_conversational_utterance(user_prompt, target_name)
        if has_tools and not is_casual:
            parts.append(
                "【調査思考ポリシー】\n"
                "発言内に意味のわからない単語、造語、スラング、倒語、または確認が必要な固有名詞が含まれている場合は、"
                "推測で返答したり聞き返したりせず、必ず `search_web` ツールを呼び出して調査計画を立ててください。"
            )

        store = extra_ctx.get("memory_store")
        persona = str(extra_ctx.get("persona") or "assistant")
        channel_id = extra_ctx.get("channel_id")
        user_id = extra_ctx.get("user_id")

        # 長期記憶/RAGコンテキストの動的読み込み (Read)
        # ※user_prompt や system_prompt 内にすでに記憶が注入されている場合は二重注入をスキップ
        already_has_memories = (
            "【この相手に関係すると確認できた記憶】" in user_prompt
            or "【関連する出来事の記憶】" in user_prompt
            or "【関連する記憶・マニュアル情報】" in system_prompt
        )
        memories = list(extra_ctx.get("memories") or [])
        if not already_has_memories and store and user_prompt:
            try:
                if hasattr(store, "search_memories_hybrid"):
                    records = await store.search_memories_hybrid(
                        query=user_prompt,
                        persona=persona,
                        channel_id=channel_id,
                        user_id=user_id,
                        limit=3,
                    )
                    for r in records:
                        c = r.get("content") if isinstance(r, dict) else getattr(r, "content", "")
                        if c and c not in memories:
                            memories.append(c[:120])
                elif hasattr(store, "get_recent_user_memories") and user_id is not None:
                    records = await store.get_recent_user_memories(
                        persona=persona,
                        user_id=user_id,
                        limit=3,
                    )
                    for r in records:
                        c = r.get("content") if isinstance(r, dict) else getattr(r, "content", "")
                        if c and c not in memories:
                            memories.append(c[:120])
            except Exception as e:
                self.log.warning("[orchestrator] Error reading memories from store: %s", e)

        if memories and not already_has_memories:
            parts.append("【関連する記憶・マニュアル情報】")
            for m in memories[:3]:
                parts.append(f"- {m[:120]}")

        # 会話履歴・チャンネル要約（短期記憶）の動的読み込み
        already_has_summary = (
            "最近の会話要約:" in user_prompt
            or "【直前の会話要約】" in system_prompt
        )
        history_summary = extra_ctx.get("channel_summary")
        if not already_has_summary and not history_summary and store and channel_id is not None:
            try:
                if hasattr(store, "get_channel_summary"):
                    s_rec = await store.get_channel_summary(
                        persona=persona,
                        channel_id=channel_id,
                    )
                    if s_rec:
                        history_summary = s_rec.get("summary") if isinstance(s_rec, dict) else getattr(s_rec, "summary", "")
            except Exception as e:
                self.log.warning("[orchestrator] Error reading channel summary from store: %s", e)

        if history_summary and not already_has_summary:
            parts.append(f"【直前の会話要約】\n{history_summary[:250]}")

        # バジェット制御: システムプロンプト全体の文字数をチェック
        orig_sys = "\n\n".join(parts)
        orig_total = len(orig_sys) + len(user_prompt)
        orig_sys_chars = len(orig_sys)
        orig_user_chars = len(user_prompt)

        # 基本となるnum_ctxの取得
        base_num_ctx = extra_ctx.get("num_ctx") if extra_ctx else None
        if not base_num_ctx:
            base_num_ctx = self.config.num_ctx
        if not base_num_ctx:
            base_num_ctx = self._get_default_num_ctx()
        base_num_ctx = int(base_num_ctx)

        # 動的 num_ctx 拡張の事前評価:
        # メッセージ履歴（messages/history）も含めた総推定トークン数を算出し、
        # 推論時に _resolve_dynamic_generation_limits で拡張される num_ctx を先回りして解決する
        history_msgs = (extra_ctx.get("messages") or extra_ctx.get("history")) if extra_ctx else None
        history_chars = 0
        history_tokens = 0
        if isinstance(history_msgs, list):
            for h_msg in history_msgs:
                if isinstance(h_msg, dict) and "content" in h_msg:
                    c = str(h_msg.get("content") or "")
                    history_chars += len(c)
                    history_tokens += _estimate_tokens_from_chars(c)

        total_input_chars = orig_total + history_chars
        total_est_tokens = (
            _estimate_tokens_from_chars(orig_sys)
            + _estimate_tokens_from_chars(user_prompt)
            + history_tokens
        )

        raw_predict = extra_ctx.get("num_predict", self.config.num_predict) if extra_ctx else self.config.num_predict
        base_predict = int(raw_predict) if raw_predict is not None else None
        think_flag = bool(extra_ctx.get("think", False)) if extra_ctx else False

        resolved_ctx, _ = _resolve_dynamic_generation_limits(
            total_chars=total_input_chars,
            estimated_tokens=total_est_tokens,
            base_num_ctx=base_num_ctx,
            base_num_predict=base_predict,
            think=think_flag,
        )

        try:
            from lib.config_utils import cfg
            agent_max_prompt_chars = int(cfg("AGENT_MAX_PROMPT_CHARS", 0) or 0)
            configured_max_chars = int(cfg("OLLAMA_PROMPT_MAX_TOTAL_CHARS", 12000) or 12000)
            hard_max_chars = int(cfg("OLLAMA_PROMPT_HARD_MAX_CHARS", 20000) or 20000)
        except Exception:
            agent_max_prompt_chars = 0
            configured_max_chars = 12000
            hard_max_chars = 20000

        # resolved_ctx から逆算した動的許容文字数（安全枠として75%程度を目安枠とする）
        # 動的拡張により resolved_ctx が 24576 の場合は 18432 文字、32768 の場合は hard_max_chars（20000文字）まで許容
        dynamic_max_chars = min(int(resolved_ctx * 0.75), hard_max_chars)
        max_prompt_chars = max(configured_max_chars, dynamic_max_chars, agent_max_prompt_chars)

        trimmed_user = user_prompt
        combined_sys = orig_sys

        if orig_total > max_prompt_chars:
            self.log.warning(
                "[orchestrator] Prompt budget exceeded (total=%d max=%d base_ctx=%d resolved_ctx=%d); trimming context safely by blocks",
                orig_total,
                max_prompt_chars,
                base_num_ctx,
                resolved_ctx,
            )

            core_sys = parts[0] if parts else ""
            extra_parts = parts[1:]
            trimmed_blocks: list[str] = []

            # -------------------------------------------------------------
            # Step 1: 補助的な動的パーツ（要約・記憶情報等）の優先削減
            # ※ツールカタログや調査思考ポリシーは残す
            # -------------------------------------------------------------
            auxiliary_prefixes = (
                "【直前の会話要約】",
                "【関連する記憶・マニュアル情報】",
            )
            retained_parts: list[str] = [core_sys] if core_sys else []
            for p in extra_parts:
                is_aux = any(p.startswith(prefix) for prefix in auxiliary_prefixes)
                if is_aux:
                    matched_prefix = next(prefix for prefix in auxiliary_prefixes if p.startswith(prefix))
                    trimmed_blocks.append(f"{matched_prefix} (-{len(p)} chars)")
                else:
                    retained_parts.append(p)

            combined_sys = "\n\n".join(retained_parts)
            cur_total = len(combined_sys) + len(trimmed_user)

            # -------------------------------------------------------------
            # Step 2: ツールカタログを削る前に、user_prompt 内の低優先ブロック（会話履歴等）を優先トリミング
            # -------------------------------------------------------------
            if cur_total > max_prompt_chars:
                needed_reduction = cur_total - max_prompt_chars
                max_user_chars = max(len(trimmed_user) - needed_reduction, 1000)
                if len(trimmed_user) > max_user_chars:
                    orig_user_len = len(trimmed_user)
                    trimmed_user = _trim_user_prompt_preserving_target(trimmed_user, max_user_chars)
                    user_cut = orig_user_len - len(trimmed_user)
                    if user_cut > 0:
                        trimmed_blocks.append(f"user_prompt (history/context): {orig_user_len}->{len(trimmed_user)} (-{user_cut} chars)")
                    cur_total = len(combined_sys) + len(trimmed_user)

            # -------------------------------------------------------------
            # Step 3: user_prompt を切り詰めてもなお予算を超える極端な場合のみ、
            # 調査思考ポリシーやツールカタログなどのパーツを順次削減
            # -------------------------------------------------------------
            if cur_total > max_prompt_chars:
                if any(p.startswith("【調査思考ポリシー】") for p in retained_parts):
                    new_parts: list[str] = []
                    for p in retained_parts:
                        if p.startswith("【調査思考ポリシー】"):
                            trimmed_blocks.append(f"【調査思考ポリシー】 (-{len(p)} chars)")
                        else:
                            new_parts.append(p)
                    retained_parts = new_parts
                    combined_sys = "\n\n".join(retained_parts)
                    cur_total = len(combined_sys) + len(trimmed_user)

            if cur_total > max_prompt_chars:
                if any(p.startswith("【利用可能なツールカタログ】") for p in retained_parts):
                    new_parts = []
                    for p in retained_parts:
                        if p.startswith("【利用可能なツールカタログ】"):
                            trimmed_blocks.append(f"【利用可能なツールカタログ】 (-{len(p)} chars)")
                        else:
                            new_parts.append(p)
                    retained_parts = new_parts
                    combined_sys = "\n\n".join(retained_parts)
                    cur_total = len(combined_sys) + len(trimmed_user)

            # -------------------------------------------------------------
            # Step 4: 万が一コアシステムプロンプト単体で max_prompt_chars を超過する極端なケースのみ安全トリミング
            # -------------------------------------------------------------
            if len(combined_sys) > max_prompt_chars:
                orig_cs_len = len(combined_sys)
                combined_sys = trim_text_at_safe_boundary(combined_sys, max_prompt_chars) or combined_sys
                trimmed_blocks.append(f"core_system_prompt: {orig_cs_len}->{len(combined_sys)} (-{orig_cs_len - len(combined_sys)} chars)")

            new_total = len(combined_sys) + len(trimmed_user)
            self.log.info(
                "[orchestrator] Prompt budget trimming performed: trimmed_blocks=[%s] total=%d->%d max=%d sys_chars=%d->%d user_chars=%d->%d",
                "; ".join(trimmed_blocks) if trimmed_blocks else "none",
                orig_total,
                new_total,
                max_prompt_chars,
                orig_sys_chars,
                len(combined_sys),
                orig_user_chars,
                len(trimmed_user),
            )
            if extra_ctx is not None:
                extra_ctx["trimmed_user_prompt"] = trimmed_user

        return combined_sys

    # _build_context のエイリアス
    _prepare_context = _build_context

    async def _generate_final_response(
        self,
        state: AgentState,
        extra_ctx: dict[str, Any] | None = None,
    ) -> str:
        """観察結果を元に最終返信テキストを生成"""
        # 観察結果（Verified Facts）を合成
        verified_facts = self.obs_synthesizer.synthesize_for_final_response(state.observations)

        if not verified_facts and state.final_response:
            return state.final_response

        final_prompt_messages = list(state.messages)

        # 指示書要求: Phase 5開始時にログ出力
        self.log.info(
            "[orchestrator] Generating final response: msg_count=%d verified_facts_len=%d",
            len(final_prompt_messages),
            len(verified_facts),
        )
        ctx = extra_ctx or {}
        target_name = str(ctx.get("target_name") or "").strip()
        if verified_facts:
            name_instruction = f"対話相手『{target_name}』の名前を取り違えないこと。" if target_name else ""
            final_instruction = (
                f"{verified_facts}\n"
                "【最重要指示】上記の確定事実（単語の意味など）を踏まえ、ユーザーの言葉を受け止めて返答を作成してください。"
                "解説調やWikipediaのような客観的説明は絶対に避け、必ずあなたのキャラクター設定（口調、一人称、感情パラメータ）を維持した自然なチャット（1〜2文程度）としてリアクションしてください。"
                f"{name_instruction}"
            )
            try:
                from harness.agent.prompt_parser.prompt_manager.retry_prompts import append_chat_reply_output_suffix
                final_instruction = append_chat_reply_output_suffix(final_instruction)
            except ImportError:
                pass
            final_prompt_messages.append({
                "role": "user",
                "content": final_instruction,
            })

        model_val = str(ctx.get("model") or ctx.get("model_name") or self.config.model_name)
        temp_val = float(ctx.get("temperature", self.config.temperature))
        timeout_val = float(ctx.get("timeout_sec", self.config.timeout_sec))
        raw_num_ctx = ctx.get("num_ctx", self.config.num_ctx)
        raw_num_predict = ctx.get("num_predict", self.config.num_predict)

        # 送信メッセージ全体の文字数・推定トークン数を事前計測
        total_chars, est_tokens = _estimate_messages_tokens(final_prompt_messages, None)
        base_ctx = int(raw_num_ctx) if raw_num_ctx else self._get_default_num_ctx()
        base_predict = int(raw_num_predict) if raw_num_predict is not None else None

        dyn_num_ctx, dyn_num_predict = _resolve_dynamic_generation_limits(
            total_chars=total_chars,
            estimated_tokens=est_tokens,
            base_num_ctx=base_ctx,
            base_num_predict=base_predict,
            think=False,
        )

        res = await self.llm_client.chat(
            messages=final_prompt_messages,
            model=model_val,
            think=False,
            temperature=temp_val,
            timeout_sec=timeout_val,
            num_ctx=dyn_num_ctx,
            num_predict=dyn_num_predict,
        )
        msg_obj = res.get("message") or {}
        raw_reply = str(msg_obj.get("content") or "")

        other_names_raw = ctx.get("other_names")
        other_names_list: list[str] = [str(n) for n in other_names_raw] if isinstance(other_names_raw, (list, set, tuple)) else []
        reply_target_user = str(ctx.get("reply_target_user") or "").strip()
        if reply_target_user and reply_target_user != target_name and reply_target_user not in other_names_list:
            other_names_list.append(reply_target_user)

        cleaned_reply = sanitize_generated_reply(
            raw_reply,
            user_text=state.user_prompt,
            target_name=target_name,
            other_names=other_names_list,
        )
        return cleaned_reply or raw_reply

    async def _prune_and_save_context(
        self,
        context: dict[str, Any],
        prompt: str = "",
        reply: str = "",
        state: AgentState | None = None,
        **kwargs: Any,
    ) -> None:
        """会話コンテキストの剪定およびメモリ保存（後方互換・インターフェース）"""
        await self._save_memory(prompt=prompt, reply=reply, context=context, state=state)

    async def _save_memory(
        self,
        prompt: str,
        reply: str,
        context: dict[str, Any],
        state: AgentState | None = None,
    ) -> None:
        """会話の要約・記憶をメモリに保存（Prune/Save）"""
        store = context.get("memory_store") or getattr(self, "memory_store", None)
        if not store:
            return

        persona = str(context.get("persona") or "assistant")
        guild_id = context.get("guild_id")
        channel_id = context.get("channel_id")
        user_id = context.get("user_id")

        try:
            # 1. Bot発話の記録（オウム返し防止・文脈参照用）
            if hasattr(store, "add_bot_utterance"):
                await store.add_bot_utterance(
                    persona=persona,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    content=reply,
                )

            # 1-2. 全フロント共通行動ログ（bot_activities）への記録
            add_act_fn = getattr(store, "add_bot_activity", None)
            if callable(add_act_fn) and bool(context.get("activity_log_enabled", True)):
                try:
                    # front名: context指定があれば優先、無ければguild/channel両方Noneなら"x"、それ以外は"discord"
                    front_name = str(context.get("front") or "").strip()
                    if not front_name:
                        front_name = "x" if (guild_id is None and channel_id is None) else "discord"
                    kind_name = "reply" if prompt else "post"
                    await add_act_fn(
                        persona=persona,
                        front=front_name,
                        kind=kind_name,
                        summary=str(reply or "")[:400],
                        guild_id=guild_id,
                        channel_id=channel_id,
                    )
                except Exception as act_e:
                    log.debug("failed to store bot activity from orchestrator: %s", act_e)

            # 2. 観察結果（確定事実: Verified Facts）の合成と長期記憶への定着
            obs_list = state.observations if state else []
            verified_facts = ""
            if obs_list:
                verified_facts = self.obs_synthesizer.synthesize_for_final_response(obs_list)

            # 長期記憶として残すコンテンツの構築
            memory_content = ""
            if verified_facts:
                memory_content = f"Q: {prompt[:80]} -> 事実（言葉の意味・確定事実）: {verified_facts[:200]}。これを受け止めて会話した。"
            elif reply:
                memory_content = f"Q: {prompt[:80]} -> A: {reply[:150]}"

            if memory_content and hasattr(store, "add_memory"):
                await store.add_memory(
                    persona=persona,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    memory_type="fact",
                    content=memory_content,
                    score=0.7,
                )

            # 3. エピソード記憶の記録（ツール実行を伴うタスクの場合）
            if hasattr(store, "add_episode") and obs_list:
                tool_names = [o.tool_name for o in obs_list]
                await store.add_episode(
                    persona=persona,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    topic=prompt[:60],
                    event_summary=f"ツール実行 ({', '.join(tool_names)}): ユーザーの発言（{prompt[:40]}）に含まれる言葉・用語について調査し、{verified_facts[:80]} と理解して会話した",
                    bot_action=reply[:100],
                    outcome="success",
                )

            self.log.info(
                "[orchestrator] Successfully saved interaction memory to MemoryStore (facts=%s, obs=%d)",
                bool(verified_facts),
                len(obs_list),
            )
        except Exception as e:
            self.log.warning("[orchestrator] Failed to save memory: %s", e)
