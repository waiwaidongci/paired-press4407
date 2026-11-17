from pydantic import BaseModel, Field
class JointRunInput(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    study_ids: list[int] = Field(min_length=2)
    parameters: dict = Field(default_factory=dict)
