from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dvd2hevc_app.frontend import session_recovery_main


raise SystemExit(session_recovery_main())
