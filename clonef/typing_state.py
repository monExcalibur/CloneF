from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TypingStep:
    generation: int
    index: int
    character: str


class ManualTyper:
    def __init__(self):
        self.text = ""
        self.index = 0
        self.pending = 0
        self.active = False
        self.generation = 0

    def load(self, text: str):
        self.pause()
        self.text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.index = 0

    def resume(self) -> bool:
        self.active = self.index < len(self.text)
        return self.active

    def pause(self):
        self.active = False
        self.pending = 0
        self.generation += 1

    def request_step(self):
        if self.active:
            self.pending = min(self.pending + 1, len(self.text) - self.index)

    def next_step(self) -> TypingStep | None:
        if self.active and self.pending and self.index < len(self.text):
            return TypingStep(self.generation, self.index, self.text[self.index])
        return None

    def accepts(self, step: TypingStep) -> bool:
        return self.active and self.pending > 0 and step.generation == self.generation and step.index == self.index

    def complete(self, step: TypingStep):
        if not self.accepts(step):
            raise ValueError("Устаревший шаг ввода")
        self.index += 1
        self.pending -= 1
        if self.index == len(self.text):
            self.pause()

