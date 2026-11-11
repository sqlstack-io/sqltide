"""Authentication and account-domain failures."""

from sqltide._native import AccountConflictError as AccountConflictError
from sqltide._native import AccountNotFoundError as AccountNotFoundError
from sqltide._native import AccountValidationError as AccountValidationError
from sqltide._native import AuthConfigurationError as AuthConfigurationError
from sqltide._native import AuthenticationError as AuthenticationError
from sqltide._native import AuthorizationError as AuthorizationError
from sqltide._native import ControlStoreError as ControlStoreError
from sqltide._native import PasswordValidationError as PasswordValidationError
