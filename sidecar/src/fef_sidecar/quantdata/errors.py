class QuantDataError(Exception):
    pass


class QuantDataAuthError(QuantDataError):
    pass


class QuantDataRateLimitError(QuantDataError):
    pass


class QuantDataNotConfiguredError(QuantDataError):
    pass
