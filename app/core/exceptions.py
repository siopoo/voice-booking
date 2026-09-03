class PawPilotError(Exception):
    """Base class for expected application failures."""


class ConfigurationError(PawPilotError, ValueError):
    pass


class BookingValidationError(PawPilotError, ValueError):
    pass


class BookingConflictError(PawPilotError, RuntimeError):
    pass


class OnboardingValidationError(PawPilotError, ValueError):
    pass


class RepositoryError(PawPilotError, RuntimeError):
    pass


class AgentInvocationError(PawPilotError, RuntimeError):
    pass


class InvalidStateTransition(PawPilotError, RuntimeError):
    pass


class SpeechRecognitionError(PawPilotError, RuntimeError):
    pass
