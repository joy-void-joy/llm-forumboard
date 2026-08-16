"""Output models for the agent.

This is a TEMPLATE. Customize these models for your domain.

The key pattern is:
1. Define Pydantic models for structured agent output
2. The agent submits them through the runtime's turn-bound submit_output tool
3. Store results in notes/traces/<version>/sessions/ for feedback loop analysis
"""

from pydantic import BaseModel, Field

from lup.workspace.history import SessionResult


# =============================================================================
# DOMAIN OUTPUT MODELS
# =============================================================================


# lup: template: redefine the factor fields for your domain. Examples:
# - For forecasting: text, direction (pro/con), weight
# - For coaching: insight, relevance, actionability
# - For game playing: consideration, evaluation, confidence
class Factor(BaseModel):
    """A single factor influencing the output."""

    text: str = Field(description="Description of the factor")
    factor_type: str = Field(description="Type of factor (customize for your domain)")
    weight: float = Field(default=1.0, description="Relative importance (1-5 scale)")


class AgentOutput(BaseModel):
    """Structured output from the agent.

    Customize this for your domain. This model should capture:
    1. The main output/decision
    2. The reasoning that led to it
    3. Confidence or uncertainty measures

    Examples:

    For forecasting:
        probability: float
        summary: str
        factors: list[Factor]

    For coaching:
        response: str
        insights: list[str]
        suggested_actions: list[str]

    For game playing:
        move: str
        evaluation: float
        considerations: list[Factor]
    """

    summary: str = Field(description="Summary of the output/decision")
    factors: list[Factor] = Field(default=[], description="Key factors")
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Confidence in the output (0-1)",
    )

    # lup: template: add your domain's output fields
    # probability: float  # For forecasting
    # move: str           # For game playing
    # response: str       # For coaching


# Domain-specific type alias: SessionResult parameterized with AgentOutput
AgentSessionResult = SessionResult[AgentOutput]
