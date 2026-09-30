"""Schema for llm_synthesis's structured output.

Enforces "structured output rather than free-form text" for the one new
LLM call this feature adds. Validation happens in the validate_synthesis
guardrail node, not here — this model only defines the required shape;
the guardrail additionally cross-checks the cited numbers against the
deterministic engine results already in state.
"""
from pydantic import BaseModel, Field, ValidationError

__all__ = ["LLMSynthesisOutput", "ValidationError"]


class LLMSynthesisOutput(BaseModel):
    model_config = {"extra": "ignore"}  # llm_analysis also carries raw_output, not part of this schema

    agreement: bool
    confidence: float = Field(ge=0, le=100)
    reasoning: str = Field(min_length=1)
    notable_risks: list[str] = Field(default_factory=list)
    cited_fair_value: float | None = None
    cited_entry_price: float | None = None
