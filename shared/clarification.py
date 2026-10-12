"""Pure contracts for grouped questions and authenticated record selections."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.schemas import TargetType


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Candidate(_Base):
    record_id: str = Field(min_length=1, max_length=128)
    target_type: TargetType


class QuestionField(_Base):
    leg_id: str = Field(pattern=r"^leg-[1-4]$")
    field: Literal["target", "source"]
    prompt: str = Field(min_length=1, max_length=500)
    candidates: list[Candidate] = Field(min_length=1, max_length=100)


class Selection(_Base):
    field_id: str = Field(min_length=1, max_length=128)
    leg_id: str = Field(pattern=r"^leg-[1-4]$")
    field: Literal["target", "source"]
    choice_id: str = Field(min_length=1, max_length=128)
