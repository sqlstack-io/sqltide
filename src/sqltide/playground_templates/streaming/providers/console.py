"""Local notification provider with no network side effects."""

from sqltide.providers import Provider


class ConsoleNotifier(Provider):
    """Print audit transitions to the SQLTide process console."""

    def notify(self, message: str) -> None:
        print(f"[sqltide-streaming] {message}", flush=True)  # noqa: T201 - intentional local output
