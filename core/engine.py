"""The reusable model/tool loop. No terminal, HTTP, persistence or team threads."""

from .messages import extract_text, json_value, tool_calls
from .models import RecoveryState
from .ports import ContextPort, DispatcherPort, ModelPort, SystemPrompt, ToolPool
from .events import EventSink
from ..config import HarnessConfig


class AgentEngine:
    def __init__(
        self,
        config: HarnessConfig,
        gateway: ModelPort,
        dispatcher: DispatcherPort,
        context: ContextPort,
        emit: EventSink,
        stop_event,
    ):
        self.config = config
        self.gateway = gateway
        self.dispatcher = dispatcher
        self.context = context
        self.emit = emit
        self.stop_event = stop_event

    def run(
        self,
        history: list,
        request: str,
        *,
        tool_pool: ToolPool,
        system_prompt: SystemPrompt,
        before_turn=None,
        after_model=None,
        checkpoint=None,
        max_iterations=None,
        allow_background=True,
        scope="agent",
    ):
        state = RecoveryState(self.config.model)
        max_tokens = self.config.max_tokens
        iteration = 0
        while not self.stop_event.is_set():
            if max_iterations is not None and iteration >= max_iterations:
                break
            iteration += 1
            if before_turn:
                injected_request = before_turn(history)
                if injected_request:
                    request = f"{request}\n{injected_request}".strip()
            self.context.prepare_context(history, request)
            definitions, handlers = tool_pool()
            self.emit("model_started", {"model": state.current_model, "iteration": iteration})
            try:
                answer = self.gateway.complete(
                    state=state,
                    system=system_prompt(history),
                    messages=history,
                    tools=definitions,
                    max_tokens=max_tokens,
                )
            except Exception as error:
                if (
                    self.gateway.is_prompt_too_long_error(error)
                    and not state.has_attempted_reactive_compact
                ):
                    history[:] = self.context.reactive_compact(history, request)
                    state.has_attempted_reactive_compact = True
                    continue
                raise
            if after_model:
                after_model()
            if answer.stop_reason == "max_tokens" and not state.has_escalated:
                max_tokens = self.config.escalated_max_tokens
                state.has_escalated = True
                continue
            content = json_value(answer.content)
            history.append({"role": "assistant", "content": content})
            if checkpoint:
                checkpoint()
            spoken = extract_text(content)
            if spoken:
                self.emit("assistant", {"text": spoken, "scope": scope})
            calls = tool_calls(content)
            if not calls:
                if (
                    answer.stop_reason == "max_tokens"
                    and state.recovery_count < self.config.max_recovery_retries
                ):
                    history.append(
                        {
                            "role": "user",
                            "content": "Continue from the previous response. Do not repeat completed work.",
                        }
                    )
                    state.recovery_count += 1
                    continue
                return spoken
            max_tokens = self.config.max_tokens
            state.has_escalated = False
            results = []
            compact = False
            for call in calls:
                if call["name"] == "compact":
                    output = "[Compaction requested. This completed turn will be summarized.]"
                    compact = True
                else:
                    output = self.dispatcher.execute(
                        call, handlers, allow_background=allow_background
                    )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "content": output,
                    }
                )
            history.append({"role": "user", "content": results})
            if compact:
                history[:] = self.context.compact_history(history, request)
            if checkpoint:
                checkpoint()
        if self.stop_event.is_set():
            raise InterruptedError("Run cancelled")
        return extract_text(history[-1].get("content", []))
