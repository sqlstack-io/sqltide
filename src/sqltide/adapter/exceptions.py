"""Neutral adapter exceptions raised in place of driver-specific errors."""


class AdapterError(Exception):
    """Base class for every neutral adapter failure."""


class UnknownAdapterError(AdapterError):
    """Raised when configuration names an adapter that is not registered."""


class DuplicateAdapterError(AdapterError):
    """Raised when two adapter registrations claim the same adapter name."""


class AdapterResultError(AdapterError):
    """Raised when an adapter query result cannot be interpreted."""


class AdapterCapabilityError(AdapterError):
    """Raised when an adapter cannot provide a required SQLTide capability."""


class AdapterConfigurationError(AdapterError):
    """Raised when adapter-owned connection configuration is invalid."""


class AdapterReplayError(AdapterError):
    """Raised when an adapter cannot realize a valid replay request."""


class AdapterWarehouseError(AdapterError):
    """Raised when the warehouse itself rejects a connection or statement."""


class AdapterUnavailableError(AdapterWarehouseError):
    """Raised when the warehouse refuses the connection or its host cannot be resolved."""


class AdapterTimeoutError(AdapterWarehouseError):
    """Raised when a warehouse operation exceeds its available time."""


class AdapterAuthenticationError(AdapterWarehouseError):
    """Raised when the warehouse rejects the supplied credentials."""


class AdapterDatabaseNotFoundError(AdapterWarehouseError):
    """Raised when the warehouse reports that the target database is missing."""


class AdapterRelationNotFoundError(AdapterWarehouseError):
    """Raised when the warehouse reports that a referenced relation is missing."""


class AdapterTargetMutationLockError(AdapterWarehouseError):
    """Raised when exclusive target mutation ownership cannot be acquired or released."""
