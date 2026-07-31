"""
PyInstaller 진입점 전용 스크립트.

app/main.py를 직접 PyInstaller 진입점으로 지정하면, 실행 시 `app.main`이 아니라
`__main__`으로 로드되어 그 안의 상대 import(`from . import ...`)가
"attempted relative import with no known parent package" 로 깨진다.
그래서 패키지 바깥의 이 스크립트를 진입점으로 두고, app.main을 절대 import 방식으로
불러온다.
"""

from app.main import main

if __name__ == "__main__":
    main()
