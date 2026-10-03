"""Structured document readings, without a claim of document authenticity."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

DOCUMENT_FIELD_NAMES = frozenset(
    {
        "name",
        "document_number",
        "birth_date",
        "expiry_date",
        "sex",
        "nationality",
        "parentage",
        "father_name",
        "mother_name",
    }
)


class DocumentField(BaseModel):
    model_config = {"extra": "forbid"}

    value: str = Field(min_length=1, max_length=256)
    # Tesseract's text confidence is not an identity or authenticity probability.
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    source_side: Literal["front", "back", "both"]
    validation: Literal["valid", "invalid", "unvalidated"] = "unvalidated"
    source: Literal["ocr", "mrz"] = "ocr"


class DocumentConflict(BaseModel):
    model_config = {"extra": "forbid"}

    field: str = Field(min_length=1, max_length=40)
    front_value: str = Field(min_length=1, max_length=256)
    back_value: str = Field(min_length=1, max_length=256)

    @field_validator("field")
    @classmethod
    def known_field(cls, value):
        if value not in DOCUMENT_FIELD_NAMES:
            raise ValueError("Campo documental desconhecido.")
        return value


class DocumentData(BaseModel):
    model_config = {"extra": "forbid"}

    status: Literal["extracted", "partial", "unavailable"]
    document_type: Literal["bi", "passport", "unknown"] = "unknown"
    issuing_country: Literal["AO"] | None = None
    fields: dict[str, DocumentField] = Field(default_factory=dict)
    conflicts: list[DocumentConflict] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    ocr_provider: str = "tesseract"
    ocr_version: str | None = None
    processing_version: str = "muth-document-ocr-v3"
    mrz_checks: dict[str, bool] = Field(default_factory=dict)
    authenticity_confirmed: Literal[False] = False

    @field_validator("fields")
    @classmethod
    def known_fields(cls, value):
        if not set(value).issubset(DOCUMENT_FIELD_NAMES):
            raise ValueError("Campo documental desconhecido.")
        return value
