"""Agent registry — the single lookup the Director uses."""

from __future__ import annotations

from typing import Any

from app.agents.base import BaseAgent
from app.agents.brand_guardian import BrandGuardianAgent
from app.agents.content import ContentAgent
from app.agents.creative import CreativeAgent
from app.agents.growth_analyst import GrowthAnalystAgent
from app.agents.research import ResearchAgent
from app.agents.strategy import StrategyAgent

_AGENT_CLASSES: dict[str, type[BaseAgent]] = {
    ResearchAgent.name: ResearchAgent,
    StrategyAgent.name: StrategyAgent,
    ContentAgent.name: ContentAgent,
    CreativeAgent.name: CreativeAgent,
    BrandGuardianAgent.name: BrandGuardianAgent,
    GrowthAnalystAgent.name: GrowthAnalystAgent,
}

# Agents the Director may delegate to inside a campaign workflow.
DELEGATABLE = ("research", "strategy", "content", "creative", "growth_analyst")


class AgentRegistry:
    def __init__(self) -> None:
        self._cache: dict[str, BaseAgent] = {}

    def names(self) -> list[str]:
        return sorted(_AGENT_CLASSES)

    def get(self, name: str) -> BaseAgent:
        if name not in _AGENT_CLASSES:
            raise KeyError(
                f"Unknown agent '{name}'. Available: {', '.join(self.names())}"
            )
        if name not in self._cache:
            self._cache[name] = _AGENT_CLASSES[name]()
        return self._cache[name]

    def specs(self) -> list[dict[str, Any]]:
        return [_AGENT_CLASSES[name].spec() for name in self.names()]

    def ensure(self, names: list[str]) -> list[str]:
        unknown = [n for n in names if n not in _AGENT_CLASSES]
        if unknown:
            raise KeyError(f"Unknown agents: {unknown}")
        return names


_registry = AgentRegistry()


def get_registry() -> AgentRegistry:
    return _registry
