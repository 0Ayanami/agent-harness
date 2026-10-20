"""Lifecycle extension points; UI concerns are supplied through ports."""

from collections import defaultdict


class HookRegistry:
    def __init__(self):
        self.callbacks = defaultdict(list)

    def register(self, event, callback):
        self.callbacks[event].append(callback)

    def trigger(self, event, *arguments):
        for callback in self.callbacks[event]:
            result = callback(*arguments)
            if result is not None:
                return result
        return None
