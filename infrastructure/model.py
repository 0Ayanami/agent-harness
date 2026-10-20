import random
import time
from ..core.models import RecoveryState
from ..core.messages import extract_text, tool_calls


class ModelGateway:
    extract_text = staticmethod(extract_text)
    has_tool_use = staticmethod(lambda content: bool(tool_calls(content)))

    def complete(self, *, state=None, **arguments):
        state = state or RecoveryState(self.config.model)
        return self.with_retry(
            lambda: self.client.messages.create(model=state.current_model, **arguments),
            state,
        )

    def __init__(self, config, client):
        self.config = config
        self.client = client

    def retry_delay(self, attempt: int) -> float:
        base = min(self.config.retry_delay_ms * 2**attempt, 32000) / 1000
        return base + random.uniform(0, base * 0.25)

    def with_retry(self, fn, state: RecoveryState):
        for attempt in range(self.config.max_retries):
            try:
                result = fn()
                state.consecutive_529 = 0
                return result
            except Exception as e:
                name = type(e).__name__.lower()
                msg = str(e).lower()
                if "ratelimit" in name or "429" in msg:
                    delay = self.retry_delay(attempt)
                    print(
                        f"  \x1b[33m[429] retry {attempt + 1}/{self.config.max_retries} after {delay:.1f}s\x1b[0m"
                    )
                    time.sleep(delay)
                    continue
                if "overloaded" in name or "529" in msg or "overloaded" in msg:
                    state.consecutive_529 += 1
                    if (
                        state.consecutive_529 >= self.config.max_consecutive_overload
                        and self.config.fallback_model
                    ):
                        state.current_model = self.config.fallback_model
                        state.consecutive_529 = 0
                        print(f"  \x1b[31m[529] switching to {self.config.fallback_model}\x1b[0m")
                    delay = self.retry_delay(attempt)
                    print(
                        f"  \x1b[33m[529] retry {attempt + 1}/{self.config.max_retries} after {delay:.1f}s\x1b[0m"
                    )
                    time.sleep(delay)
                    continue
                raise
        raise RuntimeError(f"Max retries ({self.config.max_retries}) exceeded")

    def is_prompt_too_long_error(self, e: Exception) -> bool:
        msg = str(e).lower()
        return (
            "prompt" in msg
            and "long" in msg
            or "context_length_exceeded" in msg
            or "max_context_window" in msg
        )
