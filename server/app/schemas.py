"""Shared request-model base."""

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Unknown fields are rejected, so a client-supplied user_id can never be smuggled in."""

    model_config = ConfigDict(extra="forbid")
