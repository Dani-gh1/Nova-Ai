import os
import tempfile

# Testene må aldrig røre den rigtige var/-mappe
os.environ["DESK_DATA_DIR"] = tempfile.mkdtemp(prefix="desk-test-")
os.environ["NTFY_TOPIC"] = ""
