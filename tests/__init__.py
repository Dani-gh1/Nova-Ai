import os as _os
import tempfile as _tempfile

# testene må aldrig røre serverens rigtige login-nøgle (logs/session.key)
_os.environ.setdefault("HUB_SESSION_FILE", _os.path.join(_tempfile.mkdtemp(prefix="nova-test-"), "session.key"))
