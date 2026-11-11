from dataclasses import dataclass

from sqltide.adapter.models import AdapterConnectionConfig


@dataclass(frozen=True)
class ResolveAdapterTestCase:
    description: str
    adapter_name: str
    expected_identity_name: str


@dataclass(frozen=True)
class UnknownAdapterTestCase:
    description: str
    adapter_name: str
    expected_message_fragments: tuple[str, ...]


@dataclass(frozen=True)
class BuiltinAdapterRegistryTestCase:
    description: str
    expected_adapter_names: tuple[str, ...]


@dataclass(frozen=True)
class DuplicateAdapterRegistryTestCase:
    description: str
    registration_count: int
    expected_message_fragments: tuple[str, ...]


@dataclass(frozen=True)
class AdapterQueryResultDecodingTestCase:
    description: str
    column_names: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    expected_named_rows: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class AdapterQueryResultErrorTestCase:
    description: str
    column_names: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    expected_message_fragment: str


@dataclass(frozen=True)
class AdapterConnectionConfigRedactionTestCase:
    description: str
    password: str
    expected_absent_fragment: str
    expected_present_fragments: tuple[str, ...]


@dataclass(frozen=True)
class AdapterConnectionConfigReprTestCase:
    description: str
    config: AdapterConnectionConfig
    expected_repr: str
