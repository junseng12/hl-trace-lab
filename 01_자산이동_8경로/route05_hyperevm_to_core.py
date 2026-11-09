"""경로 5: 사용자 HyperEVM TX와 동일 주소의 HyperCore credit을 연결한다.

이 파일은 전송을 만들거나 개인키를 읽지 않는다. 사용자가 HYPE System
Address로 보낸 일반 EVM TX와 성공 receipt를 기준으로 공개 Core 원장에서
주소, 자산, 수량, 시각이 일치하는 credit 후보를 찾는다.
"""

from __future__ import annotations

import sys

from core_evm import main


if __name__ == "__main__":
    sys.argv.insert(1, "evm-to-core")
    main()
