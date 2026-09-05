"""Helpers shared by the vault ingesters (documents, runs, discovery errors)."""


def resolve_corpus(manifest_value, default_corpus):
    """Resolve a line's corpus against the service's expectation.

    Absent field -> the service default applies. Present and equal ->
    accepted (self-describing input; the env var acts as a consistency
    check). Present and different -> None, meaning the line must be
    rejected: this service is being fed another corpus's data, and
    guessing would corrupt the registry.
    """
    if manifest_value is None:
        return default_corpus
    if manifest_value == default_corpus:
        return manifest_value
    return None
