"""Reconcile domain types."""

type NativeObjectKey = tuple[str | None, str, str]
type NativeReconcileTable = tuple[str, list[str], str | None, list[tuple[str, str, str | None]]]
type NativeReconcileObject = tuple[
    str, NativeObjectKey, NativeReconcileTable | None, tuple[str, str] | None
]
type NativeReconcileOutcome = tuple[NativeObjectKey, str, tuple[int, int | None] | None, list[str]]
