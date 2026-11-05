"""Stand-in keyring backends so no test reaches the real secret store.

use(backend) patches keyring.core._keyring_backend, the object every
keyring.get/set/delete_password call resolves to. Never call
keyring.get_keyring() or set_keyring() to restore it: get_keyring()
initializes the real backend. keyring is imported on use, not with this
module.
"""
from unittest.mock import patch


def use(backend):
    import keyring.core

    return patch.object(keyring.core, "_keyring_backend", backend)


class MemoryKeyring:
    name = "memory Keyring"

    def __init__(self):
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        from keyring.errors import PasswordDeleteError

        if self.items.pop((service, username), None) is None:
            raise PasswordDeleteError("Password not found")


class NoKeyring:
    """Like keyring.backends.fail; the message echoes the arguments."""

    name = "fail Keyring"

    def get_password(self, *args):
        from keyring.errors import NoKeyringError

        raise NoKeyringError(f"no backend for {args!r}")

    set_password = delete_password = get_password


class NullKeyring:
    """Like keyring.backends.null: accepts everything, returns None."""

    name = "null Keyring"

    def get_password(self, *args):
        return None

    set_password = delete_password = get_password


class LockedKeyring(MemoryKeyring):
    """Writes work; reads and deletes fail with the arguments in the message."""

    name = "locked Keyring"

    def get_password(self, service, username):
        raise RuntimeError(f"locked: {service!r} {username!r}")

    def delete_password(self, service, username):
        self.get_password(service, username)
