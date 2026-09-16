"""`python -m jejusched`와 PyInstaller 번들의 공통 엔트리.

번들은 이 파일을 패키지가 아닌 최상위 `__main__`으로 실행하므로 **절대 임포트**여야 한다.
상대 임포트(`from .main import main`)는 exe에서 ImportError로 죽는다.
"""

from jejusched.main import main

raise SystemExit(main())
