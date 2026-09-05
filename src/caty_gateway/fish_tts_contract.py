import os


SUPPORTED_MODELS = ("s1", "s2-pro", "s2.1-pro", "s2.1-pro-free")
DEFAULT_MODEL = "s2.1-pro"
DEV_TEST_MODEL = "s2.1-pro-free"
LEGACY_MODEL_ALIAS = "speech-01-turbo"
_INFERENCE_CONTRACT_PREFIX = "fish-tts-v1"
_UNSET = object()


def _normalize(raw_value):
    if raw_value is None:
        return ""
    return str(raw_value).strip()


def _supported_models_message():
    return ", ".join(SUPPORTED_MODELS)


def resolve_model(raw_value=_UNSET):
    if raw_value is _UNSET:
        raw_value = os.environ.get("FISH_MODEL")
    model = _normalize(raw_value)
    if not model:
        return DEFAULT_MODEL
    if model == LEGACY_MODEL_ALIAS:
        raise RuntimeError(
            f"FISH_MODEL={LEGACY_MODEL_ALIAS} is no longer supported. "
            f"Use {DEFAULT_MODEL} for production or {DEV_TEST_MODEL} for development/testing."
        )
    if model not in SUPPORTED_MODELS:
        raise RuntimeError(
            f"Invalid FISH_MODEL={model!r}. Supported values: {_supported_models_message()}."
        )
    return model


def inference_contract_version(raw_value=_UNSET):
    return f"{_INFERENCE_CONTRACT_PREFIX}-{resolve_model(raw_value)}"
