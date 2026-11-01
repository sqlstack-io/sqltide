"""Janitor domain types."""

type NativeRetainedMapping = tuple[str | None, bool, str, str, str]
type NativeRetainedDeployment = tuple[str, str, bool, list[NativeRetainedMapping]]
type NativeRetainedPublication = tuple[
    str, tuple[int, str], int, list[str], list[tuple[str, str, str]]
]
