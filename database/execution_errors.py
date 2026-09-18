"""Fail-closed execution classification shared by the driver and offline report."""


def classify_error(error, stage='execute'):
    """Only an identified statement timeout retains the historical 30 s penalty.

    SQLSTATE 57014 alone also includes user cancellation. Setup, source/driver,
    connection, resource and server failures cannot safely resume the controller.
    Accept mappings so archived events use exactly the same classification.
    """
    if isinstance(error, dict):
        code = error.get('sqlstate')
        message = error.get('error', '')
        primary = error.get('message_primary', message)
        stage = error.get('stage', stage)
        kind = error.get('type', 'Exception')
    else:
        code = getattr(error, 'pgcode', None)
        message = str(error)
        primary = getattr(getattr(error, 'diag', None), 'message_primary', None) or message
        kind = type(error).__name__
    timeout = (stage == 'execute' and code == '57014' and
               ('statement timeout' in primary.lower() or primary.lower() == 'timeout'))
    return dict(type=kind, error=message, sqlstate=code, message_primary=primary,
                stage=stage, classification='PROTOCOL_TIMEOUT' if timeout else 'FATAL',
                fatal=not timeout, protocol_penalty_ms=30000. if timeout else 0.)
